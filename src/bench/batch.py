"""批量运行器：把很多次运行排成一个批次，可复现、可中断续跑、遇到异常会停下。

设计要点（对应 docs/plan/stage-2.md 第 4 节）：
  - 顺序可复现：用固定的随机种子打乱，同一个场景的几次不会连着跑；
  - 可续跑：每次运行完成就追加一行到 attempts.jsonl；续跑时只读这个文件还原状态，
    “下一个该跑哪个场景”是已完成情况的纯函数，不依赖内存里的任何东西；
  - 高峰时段：不在高峰开始新的运行（含一个安全余量，因为冷却期 + 运行要十几分钟），
    等高峰过去自动继续，暂停和恢复都写进 events.jsonl；
  - 无效运行（症状没出现、疑似污染、接口出错）不计入准确率，批次最后自动补跑；
  - 同一个场景连续 3 次无效、恢复失败、连续接口错误、输入被改动，都会停下来报告。

批次目录 runs/batches/<batch_id>/ 下:
  batch.json     计划（种子、场景、次数、顺序、输入的哈希），创建后不再修改
  attempts.jsonl 每次运行完成后追加一行
  events.jsonl   暂停 / 恢复 / 停止等事件
"""

import hashlib
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bench.pricing import is_peak

MAX_CONSECUTIVE_INVALID = 3  # 同一个场景连续无效这么多次就停下来报告
MAX_CONSECUTIVE_ERRORS = 3  # 连续这么多次接口 / 基础设施出错就停下来


# ---------- 计划 ----------


def make_order(scenarios: list[str], repeats: int, seed: int) -> list[str]:
    """第一轮的执行顺序：分 repeats 轮，每轮把所有场景各排一次，轮内用种子打乱。

    这样每个场景的几次运行被均匀地分散在整个批次里（不会连着跑），而且顺序只取决于种子。
    相邻两次不会是同一个场景：轮与轮的衔接处如果撞上，就和该轮里的下一个交换。
    """
    rng = random.Random(seed)
    order: list[str] = []
    for _ in range(repeats):
        round_ = list(scenarios)
        rng.shuffle(round_)
        if order and len(round_) > 1 and round_[0] == order[-1]:
            round_[0], round_[1] = round_[1], round_[0]
        order.extend(round_)
    return order


def make_retry_rank(scenarios: list[str], seed: int) -> dict[str, int]:
    """补跑阶段用来打破平局的固定顺序（同样只取决于种子）。"""
    rng = random.Random(f"retry-{seed}")
    ranked = list(scenarios)
    rng.shuffle(ranked)
    return {s: i for i, s in enumerate(ranked)}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_tools(tool_definitions: list[dict]) -> str:
    """工具定义的哈希。批次开始时用 MCP 取到的定义算一次，每次运行结束后再拿
    transcript 里 setup 事件记录的定义算一次对照。"""
    return sha256(json.dumps(tool_definitions, sort_keys=True))


def hash_inputs(
    system_prompt: str,
    task_prompt: str,
    nudge_prompt: str,
    model_entry: dict,
    agent_cfg: dict,
    lookback_minutes: int,
    tool_definitions: list[dict] | None = None,
) -> dict[str, str]:
    """一个批次内必须保持不变的输入的哈希：系统提示、工具定义、模型和 agent 配置。

    tool_definitions 要启动 MCP 工具进程才拿得到，所以是可选的：批次开始时算一次存下来，
    之后每次运行结束时拿 transcript 里的 setup 事件再算一次对照。
    """
    parts = {
        "prompts": sha256(json.dumps([system_prompt, task_prompt, nudge_prompt])),
        # 价格不影响 agent 看到什么，所以不进哈希；影响行为的是模型名、地址和各项上限
        "model": sha256(
            json.dumps(
                {k: model_entry[k] for k in ("model", "base_url", "api_key_env")}, sort_keys=True
            )
        ),
        "agent_config": sha256(
            json.dumps({**agent_cfg, "lookback": lookback_minutes}, sort_keys=True)
        ),
    }
    if tool_definitions is not None:
        parts["tools"] = hash_tools(tool_definitions)
    return parts


# ---------- 状态与决策（纯函数）----------


def valid_counts(attempts: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in attempts:
        if a["valid"]:
            counts[a["scenario"]] = counts.get(a["scenario"], 0) + 1
    return counts


def trailing_invalid(attempts: list[dict], scenario: str) -> int:
    """某个场景最近连续无效了几次（有效的一次就清零）。"""
    n = 0
    for a in reversed([a for a in attempts if a["scenario"] == scenario]):
        if a["valid"]:
            break
        n += 1
    return n


def trailing_errors(attempts: list[dict]) -> int:
    n = 0
    for a in reversed(attempts):
        if a["status"] != "error":
            break
        n += 1
    return n


@dataclass
class Decision:
    action: str  # run / done / stop
    scenario: str | None = None
    reason: str = ""


def decide_next(plan: dict, attempts: list[dict]) -> Decision:
    """下一步该做什么。只取决于计划和已完成的记录，所以续跑时结果和没中断过完全一致。"""
    if attempts and not attempts[-1].get("restore_ok", True):
        return Decision("stop", reason="上一次运行恢复失败，必须停止整个批次")
    if attempts and attempts[-1].get("inputs_changed"):
        # 批次内系统提示、工具定义、模型配置都不能变，否则前后的运行不可比
        return Decision("stop", reason=f"固定输入被改动: {attempts[-1]['inputs_changed']}")
    if trailing_errors(attempts) >= MAX_CONSECUTIVE_ERRORS:
        return Decision(
            "stop", reason=f"连续 {MAX_CONSECUTIVE_ERRORS} 次运行出错（接口或基础设施问题）"
        )
    for s in plan["scenarios"]:
        if trailing_invalid(attempts, s) >= MAX_CONSECUTIVE_INVALID:
            return Decision("stop", reason=f"场景 {s} 连续 {MAX_CONSECUTIVE_INVALID} 次无效")

    order = plan["order"]
    if len(attempts) < len(order):  # 第一轮：按计划的顺序
        return Decision("run", order[len(attempts)])

    counts = valid_counts(attempts)
    missing = {s: plan["repeats"] - counts.get(s, 0) for s in plan["scenarios"]}
    missing = {s: m for s, m in missing.items() if m > 0}
    if not missing:
        return Decision("done")
    rank = make_retry_rank(plan["scenarios"], plan["seed"])
    last = attempts[-1]["scenario"] if attempts else None
    candidates = sorted(missing, key=lambda s: (-missing[s], rank[s]))
    # 尽量不连着跑同一个场景
    pick = next((s for s in candidates if s != last), candidates[0])
    return Decision("run", pick, reason="补跑无效运行")


# ---------- 预计时长 ----------


def estimate_duration(
    n_runs: int,
    minutes_per_run: float,
    start: datetime,
    peak_cfg: dict,
    guard_minutes: float,
) -> dict:
    """按“一次接一次、高峰时段不开始新运行”估算整批要多久。

    逐分钟模拟：每次运行开始前要求 now 和 now+guard 都不在高峰。返回结束时间、
    实际运行分钟数和因高峰而暂停的分钟数。
    """
    now, paused = start, 0
    for _ in range(n_runs):
        while is_peak(now, peak_cfg) or is_peak(now + timedelta(minutes=guard_minutes), peak_cfg):
            now += timedelta(minutes=1)
            paused += 1
        now += timedelta(minutes=minutes_per_run)
    total = (now - start).total_seconds() / 60
    return {
        "finish": now,
        "total_minutes": total,
        "paused_minutes": paused,
        "active_minutes": total - paused,
    }


# ---------- 批次存储 ----------


class BatchStore:
    def __init__(self, path: Path):
        self.path = path
        self.attempts_file = path / "attempts.jsonl"
        self.events_file = path / "events.jsonl"

    @classmethod
    def create(cls, root: Path, plan: dict) -> "BatchStore":
        path = root / plan["batch_id"]
        path.mkdir(parents=True)  # 已存在就报错：批次从不覆盖
        (path / "batch.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return cls(path)

    @classmethod
    def open(cls, root: Path, batch_id: str) -> "BatchStore":
        path = root / batch_id
        if not (path / "batch.json").exists():
            raise FileNotFoundError(f"找不到批次 {batch_id}")
        return cls(path)

    @property
    def plan(self) -> dict:
        return json.loads((self.path / "batch.json").read_text(encoding="utf-8"))

    def attempts(self) -> list[dict]:
        return _read_jsonl(self.attempts_file)

    def events(self) -> list[dict]:
        return _read_jsonl(self.events_file)

    def add_attempt(self, attempt: dict) -> None:
        _append_jsonl(self.attempts_file, attempt)

    def add_event(self, kind: str, **fields) -> None:
        _append_jsonl(
            self.events_file, {"type": kind, "at": datetime.now(UTC).isoformat(), **fields}
        )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


# ---------- 主循环 ----------


@dataclass
class BatchHooks:
    """主循环需要的外部能力。都做成可注入的，单元测试里用假的，不碰集群也不真的等待。"""

    execute_one: Callable[[str], dict]  # 跑一次运行，返回 attempt 摘要
    preflight: Callable[[], list[str]]  # 真实运行前的检查，返回拒绝理由
    check_inputs: Callable[[], str | None]  # 固定输入有没有被改动；返回问题描述或 None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = print


@dataclass
class BatchResult:
    status: str  # completed / stopped
    reason: str
    attempts: int
    notes: list[str] = field(default_factory=list)


def run_batch(
    store: BatchStore,
    hooks: BatchHooks,
    peak_cfg: dict | None,
    guard_minutes: float,
    poll_seconds: float = 60,
) -> BatchResult:
    plan = store.plan
    while True:
        attempts = store.attempts()
        decision = decide_next(plan, attempts)
        if decision.action == "done":
            store.add_event("completed")
            return BatchResult("completed", "每个场景都有足够的有效运行", len(attempts))
        if decision.action == "stop":
            store.add_event("stopped", reason=decision.reason)
            return BatchResult("stopped", decision.reason, len(attempts))

        problem = hooks.check_inputs()
        if problem:
            store.add_event("stopped", reason=problem)
            return BatchResult("stopped", problem, len(attempts))
        refusals = hooks.preflight()
        # 高峰时段的拒绝由下面的等待处理，不算停止的理由
        blocking = [r for r in refusals if "高峰" not in r]
        if blocking:
            reason = "运行前检查未通过: " + "；".join(blocking)
            store.add_event("stopped", reason=reason)
            return BatchResult("stopped", reason, len(attempts))

        if peak_cfg is not None:
            _wait_off_peak(store, hooks, peak_cfg, guard_minutes, poll_seconds)

        hooks.log(
            f"[{len(attempts) + 1}] 场景 {decision.scenario}"
            + (f"（{decision.reason}）" if decision.reason else "")
        )
        attempt = hooks.execute_one(decision.scenario)
        attempt = {"index": len(attempts) + 1, "scenario": decision.scenario, **attempt}
        store.add_attempt(attempt)
        hooks.log(
            f"    -> {attempt['status']} 有效={attempt['valid']}"
            + (" 疑似污染" if attempt.get("contaminated") else "")
        )


def _wait_off_peak(
    store: BatchStore, hooks: BatchHooks, peak_cfg: dict, guard_minutes: float, poll_seconds: float
) -> None:
    def blocked() -> bool:
        now = hooks.now()
        return is_peak(now, peak_cfg) or is_peak(now + timedelta(minutes=guard_minutes), peak_cfg)

    if not blocked():
        return
    store.add_event("pause", reason="高峰时段（含开始前的安全余量）")
    hooks.log("进入高峰时段（或即将进入），暂停，等高峰结束后自动继续...")
    while blocked():
        hooks.sleep(poll_seconds)
    store.add_event("resume")
    hooks.log("高峰已过，继续")
