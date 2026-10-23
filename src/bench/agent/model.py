"""真实模型客户端：用 Anthropic Python SDK 连 DeepSeek 的 Anthropic 兼容端点。

只是对一次 messages.create 的薄封装。base_url、模型名、读哪个环境变量当 key，
全部来自 config/run.yaml 的“模型条目”，这里不写死任何一个。
"""

import os
from typing import Any

import anthropic


class AnthropicModel:
    def __init__(self, entry: dict, max_output_tokens: int, timeout_seconds: float):
        api_key = os.environ.get(entry["api_key_env"])
        if not api_key:
            # 只说变量名，绝不打印 key 的内容
            raise RuntimeError(f"环境变量 {entry['api_key_env']} 没有设置（应写在 .env 里）")
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=entry["base_url"], timeout=timeout_seconds
        )
        self.model = entry["model"]
        self.max_output_tokens = max_output_tokens

    async def create(self, system: str, messages: list, tools: list[dict]) -> Any:
        # 不写 cache_control：兼容端点会忽略它，缓存靠请求前缀稳定自动命中
        return await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_output_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
