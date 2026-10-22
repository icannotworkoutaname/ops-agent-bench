"""集群内流量发生器。以 ConfigMap 挂载到 python:3.12-slim 里运行，只用标准库，不需要构建镜像。

为什么需要它：很多故障只有在有流量时才会表现出来（Kafka 连不上时 lag 才会上涨，
Redis 挂掉时查询才会失败）。它低速持续写入播放事件、定期查询，并把最近一段时间的
成功/失败计数通过 /stats 暴露出来，供 health.py 和场景的 verify_* 读取。

它放在 bench namespace，对 agent 不可见（隔离原则 4）。
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

INGEST_URL = os.environ["INGEST_URL"]
QUERY_URL = os.environ["QUERY_URL"]
WRITE_INTERVAL = float(os.environ.get("WRITE_INTERVAL", "0.5"))  # 每秒约 2 个事件
QUERY_INTERVAL = float(os.environ.get("QUERY_INTERVAL", "3"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "3"))
PORT = int(os.environ.get("PORT", "8000"))
KEEP_SECONDS = 600  # 内存里保留最近 10 分钟的结果，/stats 再按 window 参数截取

SONGS = ["song-A", "song-B", "song-C", "song-D", "song-E"]

# 每条记录: (时间戳, 种类 write/query, 是否成功, 错误文本)
results: deque = deque()
lock = threading.Lock()


def record(kind: str, ok: bool, error: str = "") -> None:
    now = time.time()
    with lock:
        results.append((now, kind, ok, error))
        while results and results[0][0] < now - KEEP_SECONDS:
            results.popleft()


def send_event() -> None:
    song = random.choice(SONGS)
    # 现实一点：约 80% 的 START 之后会有 END，这里简单用随机类型，目的只是产生持续流量
    event_type = random.choice(["PLAY_START", "PLAY_START", "PLAY_PROGRESS", "PLAY_END"])
    body = {
        "eventId": str(uuid.uuid4()),
        "userId": f"u{random.randint(1, 50)}",
        "songId": song,
        "eventType": event_type,
        # 用真实的当前时间：stream-processor 按事件时间开窗，旧时间戳会落进已关闭的窗口
        "timestamp": int(time.time() * 1000),
        "positionMs": 0 if event_type == "PLAY_START" else 230000,
        "durationMs": 240000,
    }
    req = urllib.request.Request(
        f"{INGEST_URL}/events",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            record("write", resp.status == 202, "" if resp.status == 202 else f"HTTP {resp.status}")
    except Exception as e:  # noqa: BLE001
        record("write", False, f"{type(e).__name__}: {e}")


def run_queries() -> None:
    # 两个查询各走一条数据路径：top 读 Redis，history 读 PostgreSQL
    for path in ("/api/songs/top?n=5", "/api/songs/song-A/completion/history"):
        try:
            with urllib.request.urlopen(f"{QUERY_URL}{path}", timeout=REQUEST_TIMEOUT) as resp:
                resp.read()
                record(
                    "query", resp.status == 200, "" if resp.status == 200 else f"HTTP {resp.status}"
                )
        except Exception as e:  # noqa: BLE001
            record("query", False, f"{path}: {type(e).__name__}: {e}")


def writer_loop() -> None:
    while True:
        send_event()
        time.sleep(WRITE_INTERVAL)


def query_loop() -> None:
    while True:
        run_queries()
        time.sleep(QUERY_INTERVAL)


def summarize(kind: str, since: float) -> dict:
    with lock:
        rows = [r for r in results if r[1] == kind and r[0] >= since]
    ok = sum(1 for r in rows if r[2])
    fails = [r for r in rows if not r[2]]
    return {"ok": ok, "fail": len(fails), "last_error": fails[-1][3] if fails else ""}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":
            self._reply(200, {"status": "ok"})
        elif url.path == "/stats" or url.path.startswith("/stats/"):
            # 窗口秒数放在路径里（/stats/20），因为经 API server 代理时不方便带 query string
            tail = url.path.removeprefix("/stats").strip("/")
            window = int(tail) if tail else 20
            since = time.time() - window
            self._reply(
                200,
                {
                    "window_seconds": window,
                    "write": summarize("write", since),
                    "query": summarize("query", since),
                },
            )
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
    threading.Thread(target=writer_loop, daemon=True).start()
    threading.Thread(target=query_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
