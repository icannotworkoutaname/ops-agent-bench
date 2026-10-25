"""命令行入口。后面的阶段会逐步加上 list / inject / restore / run / show。"""

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from bench import monitoring, traffic
from bench.admin_k8s import AdminK8s
from bench.agent.diagnosis import SUBMIT_DIAGNOSIS_TOOL
from bench.agent.fake import FakeModel
from bench.agent.mcp_tools import McpToolSource
from bench.agent.model import AnthropicModel
from bench.agent.prompts import NUDGE_PROMPT, SYSTEM_PROMPT, TASK_PROMPT
from bench.batch import (
    BatchHooks,
    BatchStore,
    estimate_duration,
    hash_inputs,
    hash_tools,
    make_order,
    run_batch,
    valid_counts,
)
from bench.config import load_dotenv, load_env, load_run
from bench.cooldown import wait_for_cooldown
from bench.health import is_healthy, run_health
from bench.report import build_report
from bench.runlog import (
    RUNS_DIR,
    RunLog,
    cumulative_usage,
    read_transcript,
    recover_interrupted_runs,
)
from bench.runner import RunDeps, execute_run, preflight
from bench.scenarios import SCENARIOS
from bench.show import render_run
from bench.stability import run_stability, summarize

app = typer.Typer(no_args_is_help=True, help="ops-agent-bench 命令行")
traffic_app = typer.Typer(no_args_is_help=True, help="管理 bench namespace 里的流量发生器")
app.add_typer(traffic_app, name="traffic")
monitoring_app = typer.Typer(no_args_is_help=True, help="管理 monitoring namespace 里的 Prometheus")
app.add_typer(monitoring_app, name="monitoring")


@app.command()
def health() -> None:
    """检查各组件和链路是否健康，全部正常才返回 0。"""
    env, run_cfg = load_env(), load_run()
    checks = run_health(env, run_cfg, AdminK8s(env))
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = "OK  " if c.ok else "FAIL"
        typer.echo(f"[{mark}] {c.name.ljust(width)}  {c.detail}")
    healthy = is_healthy(checks)
    typer.echo("\n结论: " + ("健康" if healthy else "不健康"))
    raise typer.Exit(0 if healthy else 1)


def _get_scenario(scenario_id: str):
    env, run_cfg = load_env(), load_run()
    if scenario_id not in SCENARIOS:
        typer.echo(f"未知场景 {scenario_id!r}，可选: {', '.join(SCENARIOS)}")
        raise typer.Exit(2)
    return SCENARIOS[scenario_id](env, run_cfg, AdminK8s(env)), env, run_cfg


@app.command("list")
def list_scenarios() -> None:
    """列出所有场景和它们的标准答案草稿。"""
    for sid, cls in SCENARIOS.items():
        gt = cls.ground_truth
        typer.echo(f"{sid:22} {gt.cause_category} / {gt.component}")


@app.command()
def inject(scenario_id: str) -> None:
    """只注入并确认症状，不跑 agent，用于手动排查。注入前环境必须是健康的。"""
    scenario, env, run_cfg = _get_scenario(scenario_id)
    if not is_healthy(run_health(env, run_cfg, scenario.k8s)):
        typer.echo("环境当前不健康，拒绝注入。先运行 bench health 查看，必要时 bench restore。")
        raise typer.Exit(1)
    scenario.inject()
    typer.echo(f"已注入 {scenario_id}，等待症状出现...")
    outcome = scenario.verify_injected(run_cfg["scenario"]["verify_injected_timeout"])
    typer.echo(("症状已出现: " if outcome.ok else "症状未出现: ") + outcome.detail)
    typer.echo(f"用完请运行: bench restore {scenario_id}")
    raise typer.Exit(0 if outcome.ok else 1)


@app.command()
def stability(
    scenario_id: str,
    rounds: int = typer.Option(3, help="连续做几轮"),
) -> None:
    """场景稳定性测试：连续做“注入→确认症状→恢复→确认健康”，不调用模型，结果存到 runs/stability/。"""
    scenario, env, run_cfg = _get_scenario(scenario_id)
    k8s = scenario.k8s

    def healthy() -> bool:
        return is_healthy(run_health(env, run_cfg, k8s))

    results = run_stability(
        lambda: SCENARIOS[scenario_id](env, run_cfg, k8s),
        rounds,
        run_cfg,
        healthy,
        on_progress=typer.echo,
    )
    summary = summarize(results)
    out_dir = RUNS_DIR / "stability"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{scenario_id}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(
        f"\n{scenario_id}: 症状出现 {summary['symptom_ok']}/{summary['rounds']}，"
        f"恢复成功 {summary['restore_ok']}/{summary['rounds']}；"
        f"注入到症状平均 {summary['avg_inject_to_symptom_seconds']}s，"
        f"恢复到健康平均 {summary['avg_restore_to_healthy_seconds']}s"
    )
    typer.echo(f"详细结果: {out}")
    ok = summary["symptom_ok"] == rounds and summary["restore_ok"] == rounds
    raise typer.Exit(0 if ok else 1)


@app.command()
def restore(scenario_id: str) -> None:
    """恢复场景并确认环境重新健康。可以重复执行。"""
    scenario, _, run_cfg = _get_scenario(scenario_id)
    scenario.restore()
    typer.echo("已执行恢复，等待环境变回健康...")
    outcome = scenario.verify_restored(run_cfg["scenario"]["verify_restored_timeout"])
    typer.echo(("已恢复: " if outcome.ok else "恢复失败: ") + outcome.detail)
    raise typer.Exit(0 if outcome.ok else 1)


def _recover_interrupted(run_cfg: dict) -> None:
    """上次被中途杀掉的运行如果已经花了 API 费用，先补录进累计统计，预算保护才不会少算。"""
    for name in recover_interrupted_runs(RUNS_DIR, run_cfg):
        typer.echo(f"已补录上次被中断的运行的费用: {name}（标记为无效）")


def build_run_deps(scenario, env, run_cfg, model_key, fake, no_cooldown) -> RunDeps:
    """组装一次运行需要的全部依赖。`bench run` 和批量运行器共用，保证两者做的事完全一样。"""
    k8s = scenario.k8s
    entry = run_cfg["models"][model_key]
    agent_model = (
        FakeModel()
        if fake
        else AnthropicModel(
            entry,
            run_cfg["agent"]["max_output_tokens"],
            run_cfg["agent"]["request_timeout_seconds"],
        )
    )

    def check_health() -> tuple[bool, list[dict]]:
        checks = run_health(env, run_cfg, k8s)
        return is_healthy(checks), [asdict(c) for c in checks]

    cooldown_cfg = run_cfg["cooldown"]

    def check_cooldown_health() -> tuple[bool, list[dict]]:
        # 冷却期比平时多查一项：回看窗口内有没有容器异常终止（比如 Kafka 自己被 OOMKilled）
        checks = run_health(
            env, run_cfg, k8s, recent_termination_window_seconds=run_cfg["lookback_minutes"] * 60
        )
        return is_healthy(checks), [asdict(c) for c in checks]

    def cooldown():
        return wait_for_cooldown(
            check_cooldown_health,
            window_seconds=run_cfg["lookback_minutes"] * 60,
            interval_seconds=cooldown_cfg["check_interval_seconds"],
            max_wait_seconds=cooldown_cfg["max_wait_minutes"] * 60,
            on_progress=typer.echo,
        )

    return RunDeps(
        env=env,
        run_cfg=run_cfg,
        scenario=scenario,
        model_key=model_key,
        model=agent_model,
        tool_source=McpToolSource,
        check_health=check_health,
        clear_events=lambda: k8s.clear_events(env.agent_visible_namespaces),
        log=RunLog(RUNS_DIR, scenario.id, model_key),
        fake=fake,
        cooldown=None if no_cooldown else cooldown,
    )


@app.command()
def run(
    scenario_id: str,
    model: str = typer.Option(None, help="run.yaml 里的模型条目名，默认用配置里的 default_model"),
    fake: bool = typer.Option(False, "--fake", help="用假模型跑完整链路，不联网、不花钱"),
    allow_peak: bool = typer.Option(False, "--allow-peak", help="允许在高峰时段发起真实运行"),
    no_cooldown: bool = typer.Option(
        False, "--no-cooldown", help="跳过冷却期（只能和 --fake 一起用，用于开发调试）"
    ),
) -> None:
    """完整跑一次：检查健康 -> 注入 -> 确认症状 -> agent -> 恢复 -> 确认健康 -> 打分。"""
    scenario, env, run_cfg = _get_scenario(scenario_id)
    model_key = model or run_cfg["agent"]["default_model"]
    load_dotenv()
    _recover_interrupted(run_cfg)

    problems = preflight(run_cfg, model_key, fake, allow_peak, RUNS_DIR, datetime.now(UTC))
    if no_cooldown and not fake:
        # 真实运行必须经过冷却期，否则上一次运行的痕迹可能出现在回看窗口里
        problems.append("--no-cooldown 只能和 --fake 一起用")
    if problems:
        typer.echo("拒绝运行:")
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(2)

    deps = build_run_deps(scenario, env, run_cfg, model_key, fake, no_cooldown)
    typer.echo(f"运行记录目录: {deps.log.path}")
    outcome = asyncio.run(execute_run(deps))

    typer.echo(
        f"\n状态: {outcome.status}    有效: {outcome.valid}"
        + ("    【疑似跨运行污染】" if outcome.contaminated else "")
    )
    typer.echo(f"原因: {outcome.reason}")
    if outcome.score:
        typer.echo(
            f"评分: 原因分类正确={outcome.score['cause_category_correct']}  "
            f"组件正确={outcome.score['component_correct']}"
        )
    if not fake:
        used = cumulative_usage(RUNS_DIR, run_cfg.get("stage"))
        cur = run_cfg["billing"]["currency"]
        typer.echo(
            f"本次费用: {outcome.cost} {cur}    runs/ 累计: {used['cost']:.4f} {cur}，"
            f"真实运行 {used['real_runs']}/{run_cfg['billing']['max_real_runs']} 次"
        )
    typer.echo(f"查看全过程: bench show {deps.log.path}")
    if not outcome.restore_ok:
        typer.echo("\n!!! 环境恢复失败。必须停止整个批次，先人工检查集群（bench health）!!!")
        raise typer.Exit(3)
    raise typer.Exit(0 if outcome.valid else 1)


@app.command()
def show(
    run_dir: Path,
    full: bool = typer.Option(False, "--full", help="不截断思考、回复和工具结果"),
) -> None:
    """以人能读的格式打印一次运行的全过程。"""
    typer.echo(render_run(run_dir, full))


batch_app = typer.Typer(
    no_args_is_help=True, help="批量运行：打乱顺序、可续跑、高峰暂停、自动补跑无效运行"
)
app.add_typer(batch_app, name="batch")
BATCHES_DIR = RUNS_DIR / "batches"


def _static_hashes(run_cfg: dict, model_key: str) -> dict:
    """不需要启动工具进程就能算出的、批次内必须不变的输入的哈希。"""
    return hash_inputs(
        SYSTEM_PROMPT,
        TASK_PROMPT,
        NUDGE_PROMPT,
        run_cfg["models"][model_key],
        run_cfg["agent"],
        run_cfg["lookback_minutes"],
    )


def _fetch_tool_definitions() -> list[dict]:
    async def go():
        async with McpToolSource() as tools:
            return [*await tools.definitions(), SUBMIT_DIAGNOSIS_TOOL]

    return asyncio.run(go())


def _print_estimate(plan: dict, run_cfg: dict) -> None:
    """正式批量运行前打印预计总时长（冷却 + 运行 + 恢复，含高峰暂停）和预计费用。"""
    batch_cfg = run_cfg["batch"]
    cooldown_minutes = 0 if plan["no_cooldown"] else run_cfg["lookback_minutes"] + 1
    per_run = cooldown_minutes + batch_cfg["estimated_run_minutes"]
    n = len(plan["order"])
    est = estimate_duration(
        n, per_run, datetime.now(UTC), run_cfg["billing"]["peak"], batch_cfg["peak_guard_minutes"]
    )
    used = cumulative_usage(RUNS_DIR, run_cfg.get("stage"))
    per_cost = (
        used["cost"] / used["real_runs"]
        if used["real_runs"]
        else batch_cfg["fallback_cost_per_run"]
    )
    cur = run_cfg["billing"]["currency"]
    typer.echo(
        f"计划 {n} 次运行（{len(plan['scenarios'])} 个场景 × {plan['repeats']} 次），不含无效运行的补跑"
    )
    typer.echo(
        f"每次约 {per_run:.0f} 分钟（冷却 {cooldown_minutes} + 运行/恢复 {batch_cfg['estimated_run_minutes']}）"
    )
    typer.echo(
        f"预计总时长 {est['total_minutes'] / 60:.1f} 小时"
        f"（运行 {est['active_minutes'] / 60:.1f} + 因高峰暂停 {est['paused_minutes'] / 60:.1f}），"
        f"预计结束于 {est['finish'].isoformat(timespec='minutes')} UTC"
    )
    if plan["fake"]:
        typer.echo("预计费用: 0（假模型）")
    else:
        typer.echo(
            f"预计费用约 {n * per_cost:.2f} {cur}（按 {per_cost:.3f}/次估算；"
            f"本阶段预算 {run_cfg['billing']['budget']}，已用 {used['cost']:.2f}，"
            f"真实运行 {used['real_runs']}/{run_cfg['billing']['max_real_runs']} 次）"
        )


def _drive_batch(store: BatchStore, env, run_cfg: dict) -> None:
    """推进一个批次直到完成或停止。新建和续跑走同一条路径。"""
    plan = store.plan
    k8s = AdminK8s(env)
    model_key, fake = plan["model_key"], plan["fake"]

    def execute_one(scenario_id: str) -> dict:
        scenario = SCENARIOS[scenario_id](env, run_cfg, k8s)
        deps = build_run_deps(scenario, env, run_cfg, model_key, fake, plan["no_cooldown"])
        typer.echo(f"    运行记录目录: {deps.log.path}")
        outcome = asyncio.run(execute_run(deps))
        # 工具定义的哈希要等 MCP 进程跑起来才拿得到，所以在运行结束后用 transcript 对照
        setup = next((e for e in read_transcript(outcome.run_dir) if e["type"] == "setup"), None)
        changed = None
        expected = plan["input_hashes"].get("tools")
        if setup and expected and hash_tools(setup["tools"]) != expected:
            changed = "工具定义的哈希与批次开始时不同"
        return {
            "run_dir": str(outcome.run_dir),
            "status": outcome.status,
            "valid": outcome.valid and changed is None,
            "contaminated": outcome.contaminated,
            "restore_ok": outcome.restore_ok,
            "cost": outcome.cost or 0.0,
            "reason": outcome.reason,
            "inputs_changed": changed,
            "finished_at": datetime.now(UTC).isoformat(),
        }

    def check_inputs() -> str | None:
        now = _static_hashes(run_cfg, model_key)
        for key, value in now.items():
            if value != plan["input_hashes"].get(key):
                return f"固定输入 {key} 的哈希与批次开始时不同，批次内系统提示、工具定义、模型配置都不能变"
        return None

    hooks = BatchHooks(
        execute_one=execute_one,
        # 高峰时段的拒绝由主循环用“暂停”处理，所以这里允许高峰
        preflight=lambda: preflight(run_cfg, model_key, fake, True, RUNS_DIR, datetime.now(UTC)),
        check_inputs=check_inputs,
        log=typer.echo,
    )
    peak_cfg = None if fake else run_cfg["billing"]["peak"]
    result = run_batch(store, hooks, peak_cfg, run_cfg["batch"]["peak_guard_minutes"])
    counts = valid_counts(store.attempts())
    typer.echo(f"\n批次 {plan['batch_id']}: {result.status} — {result.reason}")
    typer.echo(f"已尝试 {result.attempts} 次；各场景有效运行数: {counts}")
    typer.echo(f"生成报告: bench batch report {plan['batch_id']}")
    raise typer.Exit(0 if result.status == "completed" else 3)


@batch_app.command("run")
def batch_run(
    scenarios: str = typer.Option("all", help="all，或用逗号分隔的场景 id"),
    repeats: int = typer.Option(5, help="每个场景要有几次有效运行"),
    model: str = typer.Option(None, help="run.yaml 里的模型条目名，默认用 default_model"),
    seed: int = typer.Option(None, help="打乱顺序的随机种子；不给就用当前时间，并记录下来"),
    fake: bool = typer.Option(False, "--fake", help="用假模型，不联网、不花钱"),
    no_cooldown: bool = typer.Option(
        False, "--no-cooldown", help="跳过冷却期（只能和 --fake 一起用）"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划和预计时长 / 费用，不运行"),
) -> None:
    """创建一个批次并开始运行。"""
    env, run_cfg = load_env(), load_run()
    load_dotenv()
    _recover_interrupted(run_cfg)
    ids = list(SCENARIOS) if scenarios == "all" else [s.strip() for s in scenarios.split(",")]
    unknown = [s for s in ids if s not in SCENARIOS]
    if unknown:
        typer.echo(f"未知场景: {unknown}，可选: {', '.join(SCENARIOS)}")
        raise typer.Exit(2)
    model_key = model or run_cfg["agent"]["default_model"]
    problems = preflight(run_cfg, model_key, fake, True, RUNS_DIR, datetime.now(UTC))
    if no_cooldown and not fake:
        problems.append("--no-cooldown 只能和 --fake 一起用")
    if problems:
        typer.echo("拒绝运行:")
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(2)

    seed = seed if seed is not None else int(datetime.now(UTC).timestamp())
    hashes = {
        **_static_hashes(run_cfg, model_key),
        "tools": hash_tools(_fetch_tool_definitions()),
    }
    plan = {
        "batch_id": f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{model_key}-seed{seed}",
        "stage": run_cfg.get("stage"),
        "seed": seed,
        "scenarios": ids,
        "repeats": repeats,
        "model_key": model_key,
        "fake": fake,
        "no_cooldown": no_cooldown,
        "order": make_order(ids, repeats, seed),
        "input_hashes": hashes,
        "created_at": datetime.now(UTC).isoformat(),
    }
    _print_estimate(plan, run_cfg)
    if dry_run:
        typer.echo(f"\n计划顺序（种子 {seed}）:")
        for i, s in enumerate(plan["order"], start=1):
            typer.echo(f"  {i:2}. {s}")
        raise typer.Exit(0)
    store = BatchStore.create(BATCHES_DIR, plan)
    typer.echo(f"\n批次目录: {store.path}")
    _drive_batch(store, env, run_cfg)


@batch_app.command("resume")
def batch_resume(
    batch_id: str,
    acknowledge_restore_failure: Annotated[
        str | None,
        typer.Option(
            help="上一次运行恢复失败导致批次停止时：人工检查完集群后，用这个选项写一句确认"
            "（原因/检查了什么）才能续跑。确认只针对最后那一次尝试，写进 events.jsonl，"
            "不改 attempts.jsonl；续跑前仍会做完整的健康检查。"
        ),
    ] = None,
) -> None:
    """续跑一个被中断的批次：跳过已完成的运行，继续按原计划执行。"""
    env, run_cfg = load_env(), load_run()
    load_dotenv()
    _recover_interrupted(run_cfg)
    store = BatchStore.open(BATCHES_DIR, batch_id)
    plan = store.plan
    if plan["model_key"] not in run_cfg["models"]:
        typer.echo(f"批次用的模型 {plan['model_key']} 已不在配置里")
        raise typer.Exit(2)
    if acknowledge_restore_failure:
        attempts = store.attempts()
        if not attempts or attempts[-1].get("restore_ok", True):
            typer.echo("最后一次尝试并没有恢复失败，不需要确认")
            raise typer.Exit(2)
        store.add_event(
            "restore_failure_acknowledged",
            attempt_index=attempts[-1]["index"],
            scenario=attempts[-1]["scenario"],
            note=acknowledge_restore_failure,
        )
    store.add_event("resumed")
    typer.echo(f"续跑批次 {batch_id}：已完成 {len(store.attempts())} 次尝试")
    _drive_batch(store, env, run_cfg)


@batch_app.command("status")
def batch_status(batch_id: str) -> None:
    """查看一个批次的进度。"""
    store = BatchStore.open(BATCHES_DIR, batch_id)
    plan, attempts = store.plan, store.attempts()
    counts = valid_counts(attempts)
    typer.echo(f"批次 {batch_id}  模型 {plan['model_key']}  种子 {plan['seed']}")
    typer.echo(f"已尝试 {len(attempts)} 次，计划 {len(plan['order'])} 次")
    for s in plan["scenarios"]:
        typer.echo(f"  {s:24} 有效 {counts.get(s, 0)}/{plan['repeats']}")
    for e in store.events()[-5:]:
        typer.echo(f"  事件 {e['at']} {e['type']} {e.get('reason', '')}")


@batch_app.command("report")
def batch_report(
    batch_id: str,
    out: Annotated[
        Path | None, typer.Option(help="写到这个文件（例如 docs/results-stage-2.md）；不给就打印")
    ] = None,
) -> None:
    """生成结果报告（完全由代码生成，不用 LLM 总结）。"""
    store = BatchStore.open(BATCHES_DIR, batch_id)
    truths = {
        sid: (cls.ground_truth.cause_category, cls.ground_truth.component)
        for sid, cls in SCENARIOS.items()
    }
    text = build_report(store, truths)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"已写入 {out}")
    else:
        typer.echo(text)


@monitoring_app.command("up")
def monitoring_up() -> None:
    """部署（或更新）集群内的 Prometheus 并等它 Ready。"""
    monitoring.up(load_env())
    typer.echo("Prometheus 已就绪")


@monitoring_app.command("down")
def monitoring_down() -> None:
    """删除 monitoring namespace 和对应的 RBAC。"""
    monitoring.down(load_env())
    typer.echo("monitoring 已删除")


@traffic_app.command("up")
def traffic_up() -> None:
    """部署（或更新）流量发生器并等它 Ready。"""
    traffic.up(load_env())
    typer.echo("流量发生器已就绪")


@traffic_app.command("down")
def traffic_down() -> None:
    """删除整个 bench namespace。"""
    traffic.down(load_env())
    typer.echo("bench namespace 已删除")


@traffic_app.command("rate")
def traffic_rate(events_per_second: float) -> None:
    """调整流量发生器的写入速率（每秒事件数），不重启。"""
    typer.echo(json.dumps(AdminK8s(load_env()).set_traffic_rate(events_per_second)))


@traffic_app.command("stats")
def traffic_stats(window: int = 20) -> None:
    """打印流量发生器最近 window 秒的成功/失败计数。"""
    env = load_env()
    typer.echo(json.dumps(AdminK8s(env).traffic_stats(window), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
