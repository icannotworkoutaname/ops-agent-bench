"""MCP 客户端：把 bench.tools.server 作为子进程拉起来，当作 agent 的工具来源。

agent 循环只认 ToolSource 这个接口（definitions / call），完全不知道工具背后是 k8s。
MCP 的工具定义可以直接转成 Messages API 的工具定义：name、description，
以及 input_schema（JSON Schema）。
"""

import sys
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bench.config import ROOT


class McpToolSource:
    async def __aenter__(self):
        self._stack = AsyncExitStack()
        # 子进程默认只继承一小部分安全的环境变量，所以 .env 里的 API key 不会传给工具进程
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "bench.tools.server"], cwd=str(ROOT)
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def definitions(self) -> list[dict]:
        listed = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in listed.tools
        ]

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        result = await self._session.call_tool(name, arguments)
        text = "".join(c.text for c in result.content if c.type == "text")
        return text, bool(result.is_error)
