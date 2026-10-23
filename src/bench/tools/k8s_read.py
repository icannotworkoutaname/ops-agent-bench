"""agent 能用的只读 k8s 工具。

分成两层，方便测试和阅读：
- format_* 函数：把 kubernetes 的对象整理成给模型看的文字摘要（纯函数，不碰集群）。
  不会把整个对象原样丢回去，只挑诊断需要的字段。
- K8sReader：调用 API 拿到对象，再交给 format_*。只调用 list/read 类接口。

本模块只做“读”。对 Secret 的处理：环境变量里来自 Secret 的值只显示引用了哪个 Secret，
绝不去读取 Secret 的内容（这里甚至不调用任何 Secret 相关的 API）。
"""

from kubernetes import client, config
from kubernetes.client import ApiException

from bench.tools.common import ToolError, age, check_namespace


def pod_status(pod) -> str:
    """类似 kubectl 的 STATUS 列：优先显示容器卡住的原因，比如 CrashLoopBackOff。"""
    if pod.metadata.deletion_timestamp:
        return "Terminating"
    for cs in pod.status.container_statuses or []:
        if cs.state and cs.state.waiting and cs.state.waiting.reason:
            return cs.state.waiting.reason
        if cs.state and cs.state.terminated and cs.state.terminated.reason:
            return cs.state.terminated.reason
    return pod.status.phase or "Unknown"


def format_pod_list(pods: list) -> str:
    if not pods:
        return "(该 namespace 里没有 Pod)"
    lines = [f"{'NAME':45} {'READY':6} {'STATUS':28} {'RESTARTS':9} AGE"]
    for p in sorted(pods, key=lambda p: p.metadata.name):
        statuses = p.status.container_statuses or []
        ready = f"{sum(1 for s in statuses if s.ready)}/{len(statuses)}"
        restarts = sum(s.restart_count for s in statuses)
        lines.append(
            f"{p.metadata.name:45} {ready:6} {pod_status(p):28} {restarts:<9} "
            f"{age(p.metadata.creation_timestamp)}"
        )
    return "\n".join(lines)


def _format_state(state) -> str:
    if state is None:
        return "-"
    if state.running:
        return f"running (since {state.running.started_at})"
    if state.waiting:
        msg = (state.waiting.message or "")[:300]
        return f"waiting (reason={state.waiting.reason}, message={msg})"
    if state.terminated:
        t = state.terminated
        msg = f", message={(t.message or '')[:300]}" if t.message else ""
        return f"terminated (reason={t.reason}, exitCode={t.exit_code}, finishedAt={t.finished_at}{msg})"
    return "-"


def _format_resources(res) -> str:
    if res is None:
        return "-"
    req, lim = res.requests or {}, res.limits or {}
    return f"requests={dict(req)} limits={dict(lim)}"


def format_pod_describe(pod) -> str:
    m, s = pod.metadata, pod.status
    owner = ", ".join(f"{o.kind}/{o.name}" for o in m.owner_references or []) or "-"
    lines = [
        f"Pod: {m.namespace}/{m.name}",
        f"Status: {pod_status(pod)} (phase={s.phase})",
        f"Node: {pod.spec.node_name}  PodIP: {s.pod_ip}  Age: {age(m.creation_timestamp)}",
        f"Owner: {owner}",
        "Conditions: " + ", ".join(f"{c.type}={c.status}" for c in s.conditions or []),
        "Containers:",
    ]
    spec_by_name = {c.name: c for c in pod.spec.containers}
    for cs in s.container_statuses or []:
        spec = spec_by_name.get(cs.name)
        lines += [
            f"  - {cs.name}: image={cs.image} ready={cs.ready} restarts={cs.restart_count}",
            f"      state: {_format_state(cs.state)}",
            f"      lastState: {_format_state(cs.last_state)}",
            f"      resources: {_format_resources(spec.resources if spec else None)}",
        ]
    # 还没有 container_statuses（例如刚创建）时，至少把 spec 里的容器列出来
    if not s.container_statuses:
        for c in pod.spec.containers:
            lines.append(f"  - {c.name}: image={c.image} (还没有状态)")
    return "\n".join(lines)


def format_events(events: list, limit: int) -> str:
    if not events:
        return "(该 namespace 里没有事件)"

    def when(e):
        return e.last_timestamp or e.event_time or e.metadata.creation_timestamp

    newest_first = sorted(events, key=when, reverse=True)[:limit]
    lines = [f"最近 {len(newest_first)} 条事件（共 {len(events)} 条，新的在前）:"]
    for e in newest_first:
        obj = f"{e.involved_object.kind}/{e.involved_object.name}"
        count = f" x{e.count}" if e.count and e.count > 1 else ""
        lines.append(
            f"{age(when(e)):>5} ago  {e.type:7} {e.reason}{count}  {obj}  {(e.message or '')[:300]}"
        )
    return "\n".join(lines)


def _format_env(var) -> str:
    """环境变量：普通值原样显示；来自 Secret / ConfigMap 的只显示引用，不显示值。"""
    if var.value_from is None:
        return f"{var.name}={var.value}"
    src = var.value_from
    if src.secret_key_ref:
        r = src.secret_key_ref
        return f"{var.name} <- secret {r.name} (key {r.key})，值不显示"
    if src.config_map_key_ref:
        r = src.config_map_key_ref
        return f"{var.name} <- configmap {r.name} (key {r.key})"
    if src.field_ref:
        return f"{var.name} <- fieldRef {src.field_ref.field_path}"
    return f"{var.name} <- (其他来源)"


def _format_probe(name: str, probe) -> str:
    if probe is None:
        return f"{name}: 未配置"
    if probe.http_get:
        target = f"httpGet {probe.http_get.path} :{probe.http_get.port}"
    elif probe.tcp_socket:
        target = f"tcpSocket :{probe.tcp_socket.port}"
    elif probe._exec:
        target = "exec"
    else:
        target = "(未知类型)"
    return (
        f"{name}: {target} initialDelay={probe.initial_delay_seconds}s "
        f"period={probe.period_seconds}s failureThreshold={probe.failure_threshold}"
    )


def format_deployment(d) -> str:
    s, spec = d.status, d.spec
    strategy = spec.strategy
    rolling = strategy.rolling_update if strategy else None
    replicas = (
        f"desired={spec.replicas} ready={s.ready_replicas or 0} "
        f"updated={s.updated_replicas or 0} available={s.available_replicas or 0} "
        f"total={s.replicas or 0}"
    )
    lines = [
        f"Deployment: {d.metadata.namespace}/{d.metadata.name}",
        f"Replicas: {replicas}",
        f"Strategy: {strategy.type if strategy else '-'}"
        + (
            f" (maxSurge={rolling.max_surge}, maxUnavailable={rolling.max_unavailable})"
            if rolling
            else ""
        ),
        "Containers:",
    ]
    for c in spec.template.spec.containers:
        lines.append(f"  - {c.name}: image={c.image}")
        env = c.env or []
        if env:
            lines.append("      env:")
            lines += [f"        {_format_env(v)}" for v in env]
        else:
            lines.append("      env: (无)")
        for ref in c.env_from or []:
            if ref.secret_ref:
                lines.append(f"      envFrom: secret {ref.secret_ref.name}")
            if ref.config_map_ref:
                lines.append(f"      envFrom: configmap {ref.config_map_ref.name}")
        lines.append(f"      resources: {_format_resources(c.resources)}")
        lines.append("      " + _format_probe("readinessProbe", c.readiness_probe))
        lines.append("      " + _format_probe("livenessProbe", c.liveness_probe))
    conds = s.conditions or []
    lines.append("Conditions: " + ", ".join(f"{c.type}={c.status}({c.reason})" for c in conds))
    return "\n".join(lines)


class K8sReader:
    def __init__(self, kube_context: str, allowed_namespaces: list[str], limits: dict):
        config.load_kube_config(context=kube_context)
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.allowed = allowed_namespaces
        self.limits = limits

    def list_pods(self, namespace: str) -> str:
        check_namespace(namespace, self.allowed)
        return format_pod_list(self.core.list_namespaced_pod(namespace).items)

    def describe_pod(self, namespace: str, name: str) -> str:
        check_namespace(namespace, self.allowed)
        return format_pod_describe(self.core.read_namespaced_pod(name, namespace))

    def get_pod_logs(self, namespace: str, name: str, tail: int | None, previous: bool) -> str:
        check_namespace(namespace, self.allowed)
        max_tail = self.limits["max_log_tail_lines"]
        tail = self.limits["default_log_tail_lines"] if tail is None else tail
        if tail < 1:
            raise ToolError("tail 必须 ≥ 1")
        tail = min(tail, max_tail)  # 模型要求的行数超过上限时，按上限给
        try:
            text = self.core.read_namespaced_pod_log(
                name, namespace, tail_lines=tail, previous=previous
            )
        except ApiException as e:
            if e.status == 400 and previous:
                raise ToolError("没有上一次终止的容器日志（这个容器还没有重启过）") from e
            raise
        return text if text else "(日志为空)"

    def get_events(self, namespace: str) -> str:
        check_namespace(namespace, self.allowed)
        events = self.core.list_namespaced_event(namespace).items
        return format_events(events, self.limits["max_events"])

    def get_deployment(self, namespace: str, name: str) -> str:
        check_namespace(namespace, self.allowed)
        return format_deployment(self.apps.read_namespaced_deployment(name, namespace))
