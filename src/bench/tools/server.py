"""agent 用的只读工具，以 MCP server（stdio 传输）的形式提供。

MCP（Model Context Protocol）是一个让“工具提供方”和“使用工具的程序”解耦的协议：
工具放在独立进程里，agent 作为 MCP 客户端启动它、问它有哪些工具、再按需调用。
好处是工具的实现和权限边界（只读、只能看三个 namespace）都集中在这个进程里，
agent 循环的代码里完全看不到 k8s 客户端。

启动方式（由 agent 循环作为子进程拉起）：  python -m bench.tools.server
注意：stdio 传输用 stdout 传协议消息，所以这个进程里不能往 stdout 打印任何别的东西。
"""

from mcp.server.mcpserver import MCPServer  # mcp 2.x：FastMCP 在 2.x 里改名为 MCPServer

from bench.config import load_env, load_run
from bench.tools.common import as_tool
from bench.tools.k8s_read import K8sReader
from bench.tools.kafka_read import KafkaReader
from bench.tools.prom_read import PromReader

env = load_env()
run_cfg = load_run()
# 回看窗口 W 对所有和时间有关的工具生效（见 config/run.yaml 的 lookback_minutes）
limits = {**run_cfg["tools"], "lookback_minutes": run_cfg["lookback_minutes"]}
max_chars = limits["max_output_chars"]
WINDOW = limits["lookback_minutes"]

k8s = K8sReader(env.kube_context, env.agent_visible_namespaces, limits)
kafka = KafkaReader(env.kube_context, env.kafka)
prom = PromReader(
    env.kube_context, env.monitoring.namespace, env.monitoring.name, env.monitoring.port, limits
)

mcp = MCPServer("ops-readonly")
NAMESPACES = ", ".join(env.agent_visible_namespaces)


@mcp.tool(
    description=(
        "List the pods in a namespace: name, ready containers, status, restart count and age. "
        f"Allowed namespaces: {NAMESPACES}."
    )
)
@as_tool(max_chars)
def list_pods(namespace: str) -> str:
    return k8s.list_pods(namespace)


@mcp.tool()
@as_tool(max_chars)
def describe_pod(namespace: str, name: str) -> str:
    """Summarize one pod: status, node, owner, conditions, and for each container its image,
    readiness, restart count, current state, last state (with exit code / reason) and resources."""
    return k8s.describe_pod(namespace, name)


@mcp.tool(
    description=(
        "Return the last `tail` log lines of a pod (capped by a server-side maximum), "
        f"restricted to the last {WINDOW} minutes. "
        "Set previous=true to read the log of the previous, terminated container instance "
        "(useful when the container keeps restarting)."
    )
)
@as_tool(max_chars)
def get_pod_logs(namespace: str, name: str, tail: int = 100, previous: bool = False) -> str:
    return k8s.get_pod_logs(namespace, name, tail, previous)


@mcp.tool(
    description=(
        f"Return the Kubernetes events of the last {WINDOW} minutes in a namespace, newest first."
    )
)
@as_tool(max_chars)
def get_events(namespace: str) -> str:
    return k8s.get_events(namespace)


@mcp.tool()
@as_tool(max_chars)
def get_deployment(namespace: str, name: str) -> str:
    """Show a deployment's replica counts, rollout strategy, and per container: image,
    environment variables (values that come from a Secret are NOT shown, only which Secret
    is referenced), resource requests/limits and readiness/liveness probes."""
    return k8s.get_deployment(namespace, name)


@mcp.tool()
@as_tool(max_chars)
def get_consumer_lag(group: str) -> str:
    """Show the per-partition lag of a Kafka consumer group (current offset, log-end offset, lag)."""
    return kafka.get_consumer_lag(group)


@mcp.tool(
    description=(
        "Run a PromQL query against the system's Prometheus (service metrics and container "
        "CPU / memory / throttling metrics). Without range_minutes it is an instant query; "
        f"with range_minutes (1 to {WINDOW}) it returns the values over that period, "
        "optionally with a step such as '30s'. "
        f"Only the last {WINDOW} minutes of data are available: range_minutes plus any [..] "
        f"range or offset inside the expression must not reach back more than {WINDOW} minutes."
    )
)
@as_tool(max_chars)
def promql_query(query: str, range_minutes: int | None = None, step: str | None = None) -> str:
    return prom.promql_query(query, range_minutes, step)


@mcp.tool(
    description=(
        "Find the names of the metrics available in Prometheus that contain a substring "
        "(case-insensitive). The number of results is capped."
    )
)
@as_tool(max_chars)
def list_metrics(match: str) -> str:
    return prom.list_metrics(match)


if __name__ == "__main__":
    mcp.run(transport="stdio")
