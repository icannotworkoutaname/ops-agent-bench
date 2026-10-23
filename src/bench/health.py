"""环境健康检查：每个组件一项，全部正常才算健康。

每一项检查都自己 try/except：某个组件查不到时，那一项记为“不健康”并写明原因，
而不是让整个检查崩掉。这样 bench health 永远能给出一份完整的报告。
"""

from dataclasses import dataclass

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


def run_health(env: EnvConfig, run_cfg: dict, k8s: AdminK8s) -> list[Check]:
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
    checks.append(_safe("traffic (写入/查询)", lambda: check_traffic(k8s, cfg)))
    checks.append(_safe("control-plane (认证错误)", lambda: check_control_plane(k8s, cfg)))
    return checks


def is_healthy(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)
