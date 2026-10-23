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
