"""一次完整运行的编排：

  运行前确认环境健康 -> 清理旧事件 -> 注入 -> 确认症状出现 -> 跑 agent
  -> （无论成败）恢复 -> 再次确认健康 -> 打分、写记录

隔离原则 5：每次运行前确认环境健康，运行后恢复并再次确认健康；恢复失败就停止整个批次，
不在坏掉的环境里继续跑。所以恢复放在 finally 里，保证 agent 崩了、症状没出现、甚至注入
中途出错，都会尝试恢复；恢复失败通过 RunOutcome.restore_ok=False 告诉调用方“必须停下”。
"""

import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bench.agent.loop import AgentResult, ModelClient, run_agent
from bench.agent.prompts import SYSTEM_PROMPT, TASK_PROMPT
from bench.config import EnvConfig, load_raw
from bench.contamination import find_contamination
from bench.cooldown import CooldownResult
from bench.pricing import build_usage_report, is_peak, pricing_missing
from bench.runlog import RunLog, cumulative_usage, git_info
from bench.scenarios import SCENARIOS
from bench.scenarios.base import Scenario
from bench.scoring import score


@dataclass
class RunDeps:
    env: EnvConfig
    run_cfg: dict
    scenario: Scenario
    model_key: str
    model: ModelClient
    tool_source: Callable[[], AbstractAsyncContextManager]
    check_health: Callable[[], tuple[bool, list[dict]]]
    clear_events: Callable[[], list[str]]
    log: RunLog
    fake: bool
    # 冷却期：返回 CooldownResult。None 表示跳过（只用于假模型的开发调试）
    cooldown: Callable[[], CooldownResult] | None = None


@dataclass
class RunOutcome:
    status: str  # diagnosed / no_diagnosis / invalid / error
    reason: str
    valid: bool  # 这次运行能不能算进结果（症状没出现、环境不健康、接口出错的都不算）
    restore_ok: bool
    score: dict | None
    run_dir: Path
    cost: float | None
    contaminated: bool = False


def preflight(
    run_cfg: dict, model_key: str, fake: bool, allow_peak: bool, runs_dir: Path, now: datetime
) -> list[str]:
    """真实运行前的各项检查，返回拒绝的理由；空列表表示可以运行。

    假模型运行不花钱，不受这些限制（也不占次数和预算）。
    """
    problems = []
    entry = run_cfg["models"].get(model_key)
    if entry is None:
        return [f"未知模型 {model_key!r}，可选: {', '.join(run_cfg['models'])}"]
    if fake:
        return problems

    billing = run_cfg["billing"]
    missing = pricing_missing(billing, entry)
    if missing:
        problems.append("以下价格 / 预算配置还没有填写，无法控制费用: " + ", ".join(missing))
    if not os.environ.get(entry["api_key_env"]):
        problems.append(f"环境变量 {entry['api_key_env']} 没有设置（应写在 .env 里）")

    used = cumulative_usage(runs_dir)
    if used["real_runs"] >= billing["max_real_runs"]:
        problems.append(
            f"真实运行次数已达上限 {billing['max_real_runs']}（已用 {used['real_runs']} 次）"
        )
    budget = billing.get("budget")
    if budget is not None and used["cost"] >= budget:
        problems.append(f"累计费用 {used['cost']:.4f} 已达到预算 {budget}，需要手动调高预算")
    if not allow_peak and is_peak(now, billing["peak"]):
        problems.append("当前是高峰时段（价格是非高峰的两倍）。等高峰结束，或加 --allow-peak")
    return problems


def _health_record(healthy: bool, checks: list[dict]) -> dict:
    return {"healthy": healthy, "checks": checks}


async def execute_run(deps: RunDeps) -> RunOutcome:
    log, scenario, cfg = deps.log, deps.scenario, deps.run_cfg
    entry = cfg["models"][deps.model_key]
    started = datetime.now(UTC)
    env_record: dict = {}
    agent_result: AgentResult | None = None
    status, reason, valid = "error", "", False
    restore_ok = True

    # 各阶段的时间线：某一次运行莫名其妙慢了，靠它才能看出时间花在哪个环节
    timeline: list[dict] = []
    env_record["timeline"] = timeline

    def mark(phase: str) -> None:
        timeline.append({"phase": phase, "at": datetime.now(UTC).isoformat()})

    def meta(finished: bool) -> dict:
        return {
            "run_id": log.path.name,
            "scenario": scenario.id,
            "model_key": deps.model_key,
            "model_name": entry["model"],
            "fake": deps.fake,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat() if finished else None,
            "status": status if finished else "running",
            "reason": reason,
            "git": {
                "ops_agent_bench": git_info(Path(__file__).resolve().parents[2]),
                "music_streaming": git_info(deps.env.music_streaming_repo),
            },
            # 配置快照：之后改了配置，也能知道这次运行当时用的是什么
            "config_snapshot": {"run": load_raw("run.yaml"), "env": load_raw("env.yaml")},
        }

    log.write_json("meta.json", meta(finished=False))

    mark("start")
    not_ready_reason = "运行前环境不健康，没有注入"
    if deps.cooldown is not None:
        # 冷却期：注入前环境必须连续 W 分钟健康，这样回看窗口里不会有上一次运行的痕迹
        cooldown = deps.cooldown()
        mark("cooldown_done")
        env_record["cooldown"] = {
            "ok": cooldown.ok,
            "waited_seconds": round(cooldown.waited_seconds, 1),
            "checks": cooldown.checks,
            "resets": cooldown.resets,
            "detail": cooldown.detail,
        }
        healthy, checks = cooldown.ok, cooldown.last_checks
        not_ready_reason = f"冷却期没有通过，没有注入: {cooldown.detail}"
    else:
        # 跳过冷却期只允许用在假模型的开发调试里（见 cli.py），记录里如实写明
        env_record["cooldown"] = None
        healthy, checks = deps.check_health()
    mark("health_before_done")
    env_record["health_before"] = _health_record(healthy, checks)
    if not healthy:
        # 环境一开始就不健康：不注入，也没有可“恢复”的东西
        status, reason = "invalid", not_ready_reason
        env_record["note"] = "未注入"
    else:
        # 清掉旧事件，避免上一次运行的痕迹漏进这一次 agent 能看到的 get_events（隔离原则 2）
        env_record["events_cleared_in"] = deps.clear_events()
        mark("events_cleared")
        try:
            scenario.inject()
            mark("injected")
            injected = scenario.verify_injected(cfg["scenario"]["verify_injected_timeout"])
            mark("verify_injected_done")
            env_record["verify_injected"] = {"ok": injected.ok, "detail": injected.detail}
            if not injected.ok:
                status, reason = "invalid", f"症状没有出现，不让 agent 开始: {injected.detail}"
            else:
                mark("agent_start")
                async with deps.tool_source() as tools:
                    mark("tools_started")
                    agent_result = await run_agent(
                        deps.model,
                        tools,
                        cfg["agent"],
                        SYSTEM_PROMPT,
                        TASK_PROMPT,
                        on_event=log.append_event,
                    )
                mark("agent_end")
                status, reason = agent_result.status, agent_result.reason
                valid = status in ("diagnosed", "no_diagnosis")
        except Exception as e:  # noqa: BLE001 - 任何异常都要先走完下面的恢复，再如实记录
            status, reason = "error", f"{type(e).__name__}: {e}"
        finally:
            try:
                mark("restore_start")
                scenario.restore()
                restored = scenario.verify_restored(cfg["scenario"]["verify_restored_timeout"])
                mark("verify_restored_done")
                restored_ok, restored_detail = restored.ok, restored.detail
                after_ok, after_checks = deps.check_health()
                mark("health_after_done")
            except Exception as e:  # noqa: BLE001 - 恢复本身出错也要如实记录，而不是丢掉整份记录
                restored_ok, restored_detail = False, f"恢复过程出错: {type(e).__name__}: {e}"
                after_ok, after_checks = False, []
            env_record["verify_restored"] = {"ok": restored_ok, "detail": restored_detail}
            env_record["health_after"] = _health_record(after_ok, after_checks)
            restore_ok = restored_ok and after_ok

    # ---- 跨运行污染的事后检查 ----
    # 扫描这次运行里所有工具返回的内容，发现其他场景的特征字符串、场景 id、测试框架的名字，
    # 就标记为“疑似污染”：不计入准确率，报告里单独列出（见 contamination.py）
    contamination: list[dict] = []
    if agent_result is not None:
        calls = [e for e in log.events if e["type"] == "tool_call"]
        signatures = {sid: cls.signatures for sid, cls in SCENARIOS.items()}
        contamination = find_contamination(calls, scenario.id, signatures)
        if contamination and valid:
            valid = False
            seen = sorted({f"{h['kind']}:{h['needle']}" for h in contamination})
            reason += f"；疑似污染（不计入准确率）: {', '.join(seen)}"
    env_record["contamination"] = contamination

    # ---- 写记录 ----
    usage = build_usage_report(log.events, entry, cfg["billing"], deps.fake)
    diagnosis = agent_result.diagnosis if agent_result else None
    run_score = score(diagnosis, scenario.ground_truth) if valid else None

    if agent_result and agent_result.diagnosis:
        log.write_json("diagnosis.json", {"submitted": True, "diagnosis": agent_result.diagnosis})
    else:
        log.write_json("diagnosis.json", {"submitted": False, "reason": reason, "status": status})
    log.write_json(
        "score.json",
        run_score if run_score is not None else {"valid": False, "reason": reason},
    )
    if agent_result:
        usage["models_seen"] = agent_result.models_seen
    log.write_json("usage.json", usage)
    log.write_json("env.json", env_record)
    if not restore_ok:
        reason += "；【恢复失败，必须停止整个批次】"
    log.write_json("meta.json", meta(finished=True))

    return RunOutcome(
        status=status,
        reason=reason,
        valid=valid,
        restore_ok=restore_ok,
        score=run_score,
        run_dir=log.path,
        cost=usage["total"]["cost"],
        contaminated=bool(contamination),
    )
