"""环境健康检查：每个组件一项，全部正常才算健康。

每一项检查都自己 try/except：某个组件查不到时，那一项记为“不健康”并写明原因，
而不是让整个检查崩掉。这样 bench health 永远能给出一份完整的报告。
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from bench.admin_k8s import AdminK8s
from bench.config import EnvConfig
from bench.kafka_lag import parse_describe, total_lag


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _safe(name: str, fn) -> Check:
    try:
        ok, detail = fn()
        return Check(name, ok, detail)
    except Exception as e:  # noqa: BLE001 - 这里就是要兜住任何错误并如实报告
        return Check(name, False, f"检查失败: {type(e).__name__}: {e}")


def check_consumer_lag(k8s: AdminK8s, group: str, max_lag: int) -> tuple[bool, str]:
    rows = parse_describe(k8s.consumer_group_describe(group))
    if not rows:
        return False, "describe 没有返回任何分区（组不存在或脚本报错）"
    unknown = [r for r in rows if r.lag is None]
    if unknown:
        return False, f"{len(unknown)} 个分区的 lag 未知"
    lag = total_lag(rows)
    return lag <= max_lag, f"总 lag {lag}（阈值 {max_lag}），{len(rows)} 个分区"


def check_traffic(k8s: AdminK8s, cfg: dict) -> tuple[bool, str]:
    s = k8s.traffic_stats(cfg["traffic_window_seconds"])
    w, q = s["write"], s["query"]
    problems = []
    if w["ok"] < cfg["min_write_ok"]:
        problems.append(f"写入成功过少 {w['ok']}<{cfg['min_write_ok']}")
    if q["ok"] < cfg["min_query_ok"]:
        problems.append(f"查询成功过少 {q['ok']}<{cfg['min_query_ok']}")
    if w["fail"] > cfg["max_write_fail"]:
        problems.append(f"写入失败 {w['fail']}（最近错误: {w['last_error']}）")
    if q["fail"] > cfg["max_query_fail"]:
        problems.append(f"查询失败 {q['fail']}（最近错误: {q['last_error']}）")
    detail = (
        f"最近 {s['window_seconds']}s：写入 ok={w['ok']} fail={w['fail']}，"
        f"查询 ok={q['ok']} fail={q['fail']}"
    )
    return not problems, detail if not problems else detail + "；" + "；".join(problems)


def check_control_plane(k8s: AdminK8s, cfg: dict) -> tuple[bool, str]:
    window = cfg["control_plane_window_seconds"]
    counts = k8s.control_plane_auth_errors(window)
    total = sum(counts.values())
    detail = (
        f"最近 {window}s 内的认证错误条数 {counts}（允许 ≤ {cfg['max_control_plane_auth_errors']}）"
    )
    if total > cfg["max_control_plane_auth_errors"]:
        detail += "。可能是休眠后 token 过期，需要重启 minikube"
    return total <= cfg["max_control_plane_auth_errors"], detail


def check_recent_terminations(
    k8s: AdminK8s, namespaces: list[str], window_seconds: float, now: datetime | None = None
) -> tuple[bool, str]:
    """最近 window_seconds 内有没有容器异常终止（OOMKilled、崩溃退出等）。

    只用在冷却期：场景注入故意制造的终止，在恢复时 Pod 会被整个替换掉，记录跟着消失；
    还留在现有 Pod 上的、窗口内发生的终止就是意外，要等它滑出回看窗口。
    """
    now = now or datetime.now(UTC)
    recent = [
        t
        for t in k8s.abnormal_terminations(namespaces)
        if (now - t["finished_at"]).total_seconds() < window_seconds
    ]
    if not recent:
        return True, f"最近 {window_seconds:.0f}s 内没有容器异常终止"
    detail = "；".join(
        f"{t['namespace']}/{t['pod']}/{t['container']} {t['reason']}(exit {t['exit_code']}) "
        f"{(now - t['finished_at']).total_seconds():.0f}s 前"
        for t in recent
    )
    return False, detail


def run_health(
    env: EnvConfig,
    run_cfg: dict,
    k8s: AdminK8s,
    recent_termination_window_seconds: float | None = None,
) -> list[Check]:
    """recent_termination_window_seconds 不为 None 时多检查一项“窗口内有没有容器异常终止”（冷却期用）。"""
    cfg = run_cfg["health"]
    checks: list[Check] = []

    for d in env.deployments:
        checks.append(
            _safe(
                f"deployment/{d.name}",
                lambda d=d: k8s.deployment_ready(d.namespace, d.name),
            )
        )
    for label, sts in (("redis", env.redis), ("postgresql", env.postgres)):
        checks.append(
            _safe(
                f"statefulset/{sts.name} ({label})",
                lambda sts=sts: k8s.statefulset_ready(sts.namespace, sts.name),
            )
        )
    checks.append(
        _safe(
            f"pod/{env.kafka.pod} (kafka)",
            lambda: k8s.pod_ready(env.kafka.namespace, env.kafka.pod),
        )
    )
    for group in env.consumer_groups:
        checks.append(
            _safe(
                f"consumer-group/{group}",
                lambda group=group: check_consumer_lag(k8s, group, cfg["max_consumer_lag"]),
            )
        )
    # agent 的 promql_query 依赖它；Prometheus 挂了，agent 会拿到错误而不是数据，结果就不可信
    mon = env.monitoring
    checks.append(
        _safe(
            f"deployment/{mon.name} (prometheus)",
            lambda: k8s.deployment_ready(mon.namespace, mon.name),
        )
    )
    checks.append(_safe("traffic (写入/查询)", lambda: check_traffic(k8s, cfg)))
    checks.append(_safe("control-plane (认证错误)", lambda: check_control_plane(k8s, cfg)))
    if recent_termination_window_seconds is not None:
        namespaces = list(dict.fromkeys([*env.agent_visible_namespaces, env.monitoring.namespace]))
        checks.append(
            _safe(
                "容器异常终止 (窗口内)",
                lambda: check_recent_terminations(
                    k8s, namespaces, recent_termination_window_seconds
                ),
            )
        )
    return checks


def is_healthy(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)
