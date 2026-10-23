"""命令行入口。后面的阶段会逐步加上 list / inject / restore / run / show。"""

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import typer

from bench import monitoring, traffic
from bench.admin_k8s import AdminK8s
from bench.agent.fake import FakeModel
from bench.agent.mcp_tools import McpToolSource
from bench.agent.model import AnthropicModel
from bench.config import load_dotenv, load_env, load_run
from bench.cooldown import wait_for_cooldown
from bench.health import is_healthy, run_health
from bench.runlog import RUNS_DIR, RunLog, cumulative_usage
from bench.runner import RunDeps, execute_run, preflight
from bench.scenarios import SCENARIOS
from bench.show import render_run

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
def restore(scenario_id: str) -> None:
    """恢复场景并确认环境重新健康。可以重复执行。"""
    scenario, _, run_cfg = _get_scenario(scenario_id)
    scenario.restore()
    typer.echo("已执行恢复，等待环境变回健康...")
    outcome = scenario.verify_restored(run_cfg["scenario"]["verify_restored_timeout"])
    typer.echo(("已恢复: " if outcome.ok else "恢复失败: ") + outcome.detail)
    raise typer.Exit(0 if outcome.ok else 1)


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

    problems = preflight(run_cfg, model_key, fake, allow_peak, RUNS_DIR, datetime.now(UTC))
    if no_cooldown and not fake:
        # 真实运行必须经过冷却期，否则上一次运行的痕迹可能出现在回看窗口里
        problems.append("--no-cooldown 只能和 --fake 一起用")
    if problems:
        typer.echo("拒绝运行:")
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(2)

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

    def cooldown():
        return wait_for_cooldown(
            check_health,
            window_seconds=run_cfg["lookback_minutes"] * 60,
            interval_seconds=cooldown_cfg["check_interval_seconds"],
            max_wait_seconds=cooldown_cfg["max_wait_minutes"] * 60,
            on_progress=typer.echo,
        )

    deps = RunDeps(
        env=env,
        run_cfg=run_cfg,
        scenario=scenario,
        model_key=model_key,
        model=agent_model,
        tool_source=McpToolSource,
        check_health=check_health,
        clear_events=lambda: k8s.clear_events(env.agent_visible_namespaces),
        log=RunLog(RUNS_DIR, scenario_id, model_key),
        fake=fake,
        cooldown=None if no_cooldown else cooldown,
    )
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
        used = cumulative_usage(RUNS_DIR)
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


@traffic_app.command("stats")
def traffic_stats(window: int = 20) -> None:
    """打印流量发生器最近 window 秒的成功/失败计数。"""
    env = load_env()
    typer.echo(json.dumps(AdminK8s(env).traffic_stats(window), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
