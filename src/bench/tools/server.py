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

env = load_env()
limits = load_run()["tools"]
max_chars = limits["max_output_chars"]

k8s = K8sReader(env.kube_context, env.agent_visible_namespaces, limits)
kafka = KafkaReader(env.kube_context, env.kafka)

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


@mcp.tool()
@as_tool(max_chars)
def get_pod_logs(namespace: str, name: str, tail: int = 100, previous: bool = False) -> str:
    """Return the last `tail` log lines of a pod (capped by a server-side maximum).
    Set previous=true to read the log of the previous, terminated container instance
    (useful when the container keeps restarting)."""
    return k8s.get_pod_logs(namespace, name, tail, previous)


@mcp.tool()
@as_tool(max_chars)
def get_events(namespace: str) -> str:
    """Return the most recent Kubernetes events in a namespace, newest first."""
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
