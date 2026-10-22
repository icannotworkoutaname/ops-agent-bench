"""命令行入口。后面的阶段会逐步加上 list / inject / restore / run / show。"""

import json

import typer

from bench import traffic
from bench.admin_k8s import AdminK8s
from bench.config import load_env, load_run
from bench.health import is_healthy, run_health
from bench.scenarios import SCENARIOS

app = typer.Typer(no_args_is_help=True, help="ops-agent-bench 命令行")
traffic_app = typer.Typer(no_args_is_help=True, help="管理 bench namespace 里的流量发生器")
app.add_typer(traffic_app, name="traffic")


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
