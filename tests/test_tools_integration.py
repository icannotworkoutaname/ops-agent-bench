"""集成测试：真的把 MCP server 作为子进程拉起来，用 MCP 客户端调用，需要集群（健康状态下）。

这也是第 5 步 agent 循环里 MCP 客户端部分的最小原型：
启动子进程 -> 初始化会话 -> 列出工具 -> 调用工具。
"""

import asyncio
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bench.config import ROOT

pytestmark = pytest.mark.integration

EXPECTED_TOOLS = {
    "list_pods",
    "describe_pod",
    "get_pod_logs",
    "get_events",
    "get_deployment",
    "get_consumer_lag",
    "promql_query",
    "list_metrics",
}


async def _session_calls() -> dict:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "bench.tools.server"], cwd=str(ROOT)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()

        async def call(name: str, args: dict) -> str:
            result = await session.call_tool(name, args)
            return "".join(c.text for c in result.content if c.type == "text")

        return {
            "tool_names": {t.name for t in tools.tools},
            "pods": await call("list_pods", {"namespace": "default"}),
            "bench_ns": await call("list_pods", {"namespace": "bench"}),
            "kube_system": await call("list_pods", {"namespace": "kube-system"}),
            "deploy": await call(
                "get_deployment", {"namespace": "default", "name": "query-service"}
            ),
            "missing_pod": await call("describe_pod", {"namespace": "default", "name": "nope"}),
            "lag": await call("get_consumer_lag", {"group": "stream-processor"}),
            "bad_group": await call("get_consumer_lag", {"group": "a; ls"}),
            "events": await call("get_events", {"namespace": "default"}),
            # Prometheus 所在的 namespace 对 kubectl 类工具不可见，只能通过 promql_query 查数据
            "monitoring_ns": await call("list_pods", {"namespace": "monitoring"}),
            "prom_up": await call("promql_query", {"query": "up"}),
            "prom_range": await call("promql_query", {"query": "up", "range_minutes": 3}),
            "prom_too_old": await call("promql_query", {"query": "rate(up[30m])"}),
            "prom_bench": await call(
                "promql_query",
                {"query": 'count(container_memory_working_set_bytes{namespace="bench"})'},
            ),
            "metrics": await call("list_metrics", {"match": "cfs_throttled"}),
        }


def test_mcp_server_end_to_end():
    r = asyncio.run(_session_calls())
    assert r["tool_names"] == EXPECTED_TOOLS
    assert "query-service" in r["pods"] and "stream-processor" in r["pods"]
    # bench namespace（流量发生器所在）和系统 namespace 对 agent 不可见
    assert r["bench_ns"].startswith("错误:")
    assert r["kube_system"].startswith("错误:")
    assert "traffic-gen" not in r["pods"]
    # Secret 引用只显示名字
    assert "POSTGRES_PASSWORD <- secret postgres-credentials" in r["deploy"]
    assert r["missing_pod"].startswith("错误:")
    assert "stream-processor" in r["lag"] and "总 lag" in r["lag"]
    assert r["bad_group"].startswith("错误:")
    assert "事件" in r["events"]
    assert r["monitoring_ns"].startswith("错误:")
    assert 'job="apps"' in r["prom_up"] and "返回" in r["prom_up"]
    assert "个点" in r["prom_range"]
    assert r["prom_too_old"].startswith("错误:") and "回看窗口" in r["prom_too_old"]
    assert "没有返回任何序列" in r["prom_bench"]  # 流量发生器所在 namespace 的指标没有被抓取
    assert "container_cpu_cfs_throttled_periods_total" in r["metrics"]
