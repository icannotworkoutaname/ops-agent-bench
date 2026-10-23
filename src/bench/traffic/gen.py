"""集群内流量发生器。以 ConfigMap 挂载到 python:3.12-slim 里运行，只用标准库，不需要构建镜像。

为什么需要它：很多故障只有在有流量时才会表现出来（Kafka 连不上时 lag 才会上涨，
Redis 挂掉时查询才会失败）。它持续写入播放事件、定期查询，并把最近一段时间的
成功 / 失败计数和写入延迟通过 /stats 暴露出来，供 health.py 和场景的 verify_* 读取。

写入速率可以在运行中调整（GET /rate/<每秒事件数>）：有些场景（cpu 限流、过载）需要把速率调得很高，
结束后再调回默认值。高速率靠多个写入线程 + 每个线程复用一条 HTTP 连接来实现。

它放在 bench namespace，对 agent 不可见（隔离原则 4）。
"""

import http.client
import json
import os
import random
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

INGEST_URL = os.environ["INGEST_URL"]
QUERY_URL = os.environ["QUERY_URL"]
DEFAULT_RATE = float(os.environ.get("DEFAULT_RATE", "2"))  # 每秒事件数
WRITER_THREADS = int(os.environ.get("WRITER_THREADS", "16"))
QUERY_INTERVAL = float(os.environ.get("QUERY_INTERVAL", "3"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "3"))
PORT = int(os.environ.get("PORT", "8000"))
KEEP_SECONDS = 600  # 按秒聚合，内存里保留最近 10 分钟

SONGS = ["song-A", "song-B", "song-C", "song-D", "song-E"]
_ingest = urlparse(INGEST_URL)


class Stats:
    """按“秒”聚合的计数：高速率下不逐条保存，内存和锁的开销都是固定的。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.buckets: dict[int, dict] = {}
        self.last_error = {"write": "", "query": ""}

    def record(self, kind: str, ok: bool, latency_ms: float, error: str = "") -> None:
        second = int(time.time())
        with self.lock:
            b = self.buckets.setdefault(
                second,
                {
                    "write": [0, 0, 0.0, 0.0],
                    "query": [0, 0, 0.0, 0.0],
                },  # ok, fail, 延迟和, 最大延迟
            )
            item = b[kind]
            item[0 if ok else 1] += 1
            if ok:
                item[2] += latency_ms
                item[3] = max(item[3], latency_ms)
            else:
                self.last_error[kind] = error
            if len(self.buckets) > KEEP_SECONDS + 30:
                for old in [s for s in self.buckets if s < second - KEEP_SECONDS]:
                    del self.buckets[old]

    def summarize(self, kind: str, window: int) -> dict:
        since = int(time.time()) - window
        ok = fail = 0
        total_ms = max_ms = 0.0
        with self.lock:
            for second, b in self.buckets.items():
                if second >= since:
                    o, f, s, m = b[kind]
                    ok, fail, total_ms, max_ms = ok + o, fail + f, total_ms + s, max(max_ms, m)
            last = self.last_error[kind] if fail else ""
        return {
            "ok": ok,
            "fail": fail,
            "last_error": last,
            "avg_ms": round(total_ms / ok, 1) if ok else 0.0,
            "max_ms": round(max_ms, 1),
        }


stats = Stats()
rate_lock = threading.Lock()
rate_state = {"rate": DEFAULT_RATE, "version": 0}


def set_rate(value: float) -> None:
    with rate_lock:
        rate_state["rate"] = value
        rate_state["version"] += 1


def current_rate() -> tuple[float, int]:
    with rate_lock:
        return rate_state["rate"], rate_state["version"]


def make_event() -> bytes:
    event_type = random.choice(["PLAY_START", "PLAY_START", "PLAY_PROGRESS", "PLAY_END"])
    return json.dumps(
        {
            "eventId": str(uuid.uuid4()),
            "userId": f"u{random.randint(1, 50)}",
            "songId": random.choice(SONGS),
            "eventType": event_type,
            # 用真实的当前时间：stream-processor 按事件时间开窗，旧时间戳会落进已关闭的窗口
            "timestamp": int(time.time() * 1000),
            "positionMs": 0 if event_type == "PLAY_START" else 230000,
            "durationMs": 240000,
        }
    ).encode()


def writer_loop() -> None:
    conn = None
    seen_version = -1
    next_time = time.monotonic() + random.random()  # 各线程错开启动，避免同时发
    while True:
        rate, version = current_rate()
        if version != seen_version:  # 速率被改过：马上按新速率重新排期，不用等旧的长间隔
            seen_version = version
            next_time = time.monotonic() + random.random() * min(1.0, WRITER_THREADS / rate)
        wait = next_time - time.monotonic()
        if wait > 0:
            time.sleep(min(wait, 0.2))
            continue
        started = time.monotonic()
        try:
            if conn is None:
                conn = http.client.HTTPConnection(
                    _ingest.hostname, _ingest.port, timeout=REQUEST_TIMEOUT
                )
            conn.request(
                "POST", "/events", body=make_event(), headers={"Content-Type": "application/json"}
            )
            resp = conn.getresponse()
            resp.read()
            ok = resp.status == 202
            stats.record(
                "write",
                ok,
                (time.monotonic() - started) * 1000,
                "" if ok else f"HTTP {resp.status}",
            )
            if resp.getheader("Connection", "").lower() == "close":
                conn.close()
                conn = None
        except Exception as e:  # noqa: BLE001
            stats.record("write", False, 0.0, f"{type(e).__name__}: {e}")
            if conn is not None:
                conn.close()
            conn = None
        # 每个线程负责总速率的 1/WRITER_THREADS
        next_time += WRITER_THREADS / rate
        if next_time < time.monotonic() - 1:  # 落后太多（发不动）就别补发，免得越堆越多
            next_time = time.monotonic()


def run_queries() -> None:
    # 两个查询各走一条数据路径：top 读 Redis，history 读 PostgreSQL
    for path in ("/api/songs/top?n=5", "/api/songs/song-A/completion/history"):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(f"{QUERY_URL}{path}", timeout=REQUEST_TIMEOUT) as resp:
                resp.read()
                ok = resp.status == 200
                stats.record(
                    "query",
                    ok,
                    (time.monotonic() - started) * 1000,
                    "" if ok else f"HTTP {resp.status}",
                )
        except Exception as e:  # noqa: BLE001
            stats.record("query", False, 0.0, f"{path}: {type(e).__name__}: {e}")


def query_loop() -> None:
    while True:
        run_queries()
        time.sleep(QUERY_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._reply(200, {"status": "ok"})
        elif path == "/stats" or path.startswith("/stats/"):
            # 窗口秒数放在路径里（/stats/20），因为经 API server 代理时不方便带 query string
            tail = path.removeprefix("/stats").strip("/")
            window = int(tail) if tail else 20
            rate, _ = current_rate()
            self._reply(
                200,
                {
                    "window_seconds": window,
                    "rate": rate,
                    "write": stats.summarize("write", window),
                    "query": stats.summarize("query", window),
                },
            )
        elif path.startswith("/rate/"):
            try:
                value = float(path.removeprefix("/rate/"))
                if not 0.1 <= value <= 5000:
                    raise ValueError
            except ValueError:
                self._reply(400, {"error": "rate must be a number between 0.1 and 5000"})
                return
            set_rate(value)
            self._reply(200, {"rate": value})
        else:
            self._reply(404, {"error": "not found"})

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 不把每个 /stats 请求都打到日志里
        pass


if __name__ == "__main__":
    for _ in range(WRITER_THREADS):
        threading.Thread(target=writer_loop, daemon=True).start()
    threading.Thread(target=query_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
