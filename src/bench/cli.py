"""命令行入口。后面的阶段会逐步加上 list / inject / restore / run / show。"""

import json

import typer

from bench import traffic
from bench.admin_k8s import AdminK8s
from bench.config import load_env, load_run
from bench.health import is_healthy, run_health

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
