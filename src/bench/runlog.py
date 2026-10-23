"""运行记录：每次运行一个目录，从不覆盖、从不删除。

runs/<UTC 时间>-<scenario>-<model>/
  meta.json         场景、模型、配置快照、开始结束时间、git commit
  transcript.jsonl  每一步的完整请求和响应，包括每次工具调用的参数、结果、耗时
  usage.json        每一步和总计的 token 与费用
  diagnosis.json    最终诊断（或“未提交”及原因）
  score.json
  env.json          注入前健康检查、verify_injected、恢复后健康检查
runs/ 不进 git。
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from bench.config import ROOT

RUNS_DIR = ROOT / "runs"


def git_info(repo: Path) -> dict:
    """某个仓库当前的 commit 和是否有未提交改动。拿不到就如实写 unknown。"""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        ).stdout.strip()

    commit = git("rev-parse", "HEAD")
    if not commit:
        return {"commit": "unknown", "dirty": None}
    return {"commit": commit, "dirty": bool(git("status", "--porcelain"))}


class RunLog:
    def __init__(self, runs_dir: Path, scenario_id: str, model_key: str):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = f"{stamp}-{scenario_id}-{model_key}"
        path = runs_dir / base
        suffix = 1
        while path.exists():  # 同一秒内重复运行也绝不覆盖已有目录
            suffix += 1
            path = runs_dir / f"{base}-{suffix}"
        path.mkdir(parents=True)
        self.path = path
        self.events: list[dict] = []

    def write_json(self, name: str, obj) -> None:
        (self.path / name).write_text(
            json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )

    def append_event(self, event: dict) -> None:
        """每个事件追加一行并立即落盘：运行中途崩溃，已经发生的步骤也不会丢。"""
        self.events.append(event)
        with (self.path / "transcript.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def read_transcript(run_dir: Path) -> list[dict]:
    path = run_dir / "transcript.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def cumulative_usage(runs_dir: Path, stage: int | None = None) -> dict:
    """runs/ 下所有真实运行（不含假模型）的累计费用和次数，用来做预算和次数上限的检查。

    给了 stage 就只统计属于该阶段的运行：每个阶段有自己的次数上限和预算，
    第 1 阶段的记录没有 stage 字段，按第 1 阶段算。
    """
    cost, count = 0.0, 0
    if runs_dir.exists():
        for usage_file in sorted(runs_dir.glob("*/usage.json")):
            usage = json.loads(usage_file.read_text(encoding="utf-8"))
            if usage.get("fake"):
                continue
            if stage is not None and usage.get("stage", 1) != stage:
                continue
            cost += usage["total"].get("cost") or 0.0
            # 只有真的向模型发过请求的运行才算一次“真实 agent 运行”；
            # 症状没出现而被判无效、根本没让 agent 开始的，不占 15 次的额度
            if usage["steps"]:
                count += 1
    return {"cost": cost, "real_runs": count}
