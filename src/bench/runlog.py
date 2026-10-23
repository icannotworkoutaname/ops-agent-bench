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
from bench.pricing import build_usage_report

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


def recover_interrupted_runs(runs_dir: Path, run_cfg: dict) -> list[str]:
    """补录被中途杀掉的运行的费用。

    进程被杀（内存不足、断电、休眠）时，那次运行的目录里只有 meta.json（状态 running）和已经写下的
    transcript.jsonl，没有 usage.json。它已经花掉的 API 费用就不在累计统计里，预算和次数保护会少算。
    这里从 transcript 里重新算出 usage.json，并标记这次运行无效（记录不完整，不计入准确率）。

    只新增文件（usage.json、score.json、interrupted.json），不修改任何已有的记录：运行记录从不覆盖。
    重复执行是安全的：补录过的目录有 usage.json，不会再处理。返回被处理的目录名。
    """
    if not runs_dir.exists():
        return []
    recovered = []
    for meta_file in sorted(runs_dir.glob("*/meta.json")):
        d = meta_file.parent
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if meta.get("status") != "running" or (d / "usage.json").exists():
            continue
        events = read_transcript(d)
        if not any(e["type"] == "request" for e in events):
            continue  # agent 还没开始就被杀了：没有花过 API 费用，不用补录
        entry = run_cfg["models"].get(meta["model_key"], {})
        usage = build_usage_report(
            events, entry, run_cfg["billing"], meta["fake"], meta.get("stage", 1)
        )
        usage["recovered_from_transcript"] = True
        reason = "运行被中途打断，记录不完整（费用由 transcript 补录），不计入准确率"
        (d / "usage.json").write_text(
            json.dumps(usage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (d / "score.json").write_text(
            json.dumps({"valid": False, "reason": reason}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        submitted = any(
            e["type"] == "tool_call" and e["name"] == "submit_diagnosis" and not e["is_error"]
            for e in events
        )
        (d / "interrupted.json").write_text(
            json.dumps(
                {
                    "reason": reason,
                    "steps_in_transcript": sum(1 for e in events if e["type"] == "request"),
                    "had_submitted_a_diagnosis": submitted,
                    "recovered_at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        recovered.append(d.name)
    return recovered


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
