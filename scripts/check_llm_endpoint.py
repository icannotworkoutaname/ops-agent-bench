"""检查 DeepSeek 的 Anthropic 兼容端点和 Anthropic 原生 API 的行为差异。

只用一个假工具，不碰集群。目的是在写 agent 循环之前，把下面几件事用事实确认下来：
  1. 模型名是否可用
  2. tool_use -> tool_result 能否完整往返
  3. 一次响应里出现多个 tool_use 时的格式
  4. 响应里有没有 thinking 块，原样回传是否正常
  5. usage 里实际有哪些字段，能不能看出缓存命中

用法：uv run python scripts/check_llm_endpoint.py [--pro]
真实调用总数很少（每项只发几次很小的请求），不计入阶段 1 的 15 次 agent 运行。
"""

import json
import sys
from pathlib import Path

import anthropic

BASE_URL = "https://api.deepseek.com/anthropic"
FLASH = "deepseek-flash"
PRO = "deepseek-v4-pro"

# 假工具：返回固定内容，只用来观察调用格式
TOOLS = [
    {
        "name": "get_number",
        "description": "Return the secret number for a given name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }
]
FAKE_NUMBERS = {"alpha": 11, "beta": 22, "gamma": 33}


def load_key() -> str:
    """手写读取 .env，不为此多装一个依赖。只取值，不打印。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    for line in env_path.read_text().splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(".env 里没有 DEEPSEEK_API_KEY")


def show(title: str, resp) -> None:
    print(f"\n--- {title}")
    print("stop_reason:", resp.stop_reason)
    print("content 块类型:", [b.type for b in resp.content])
    print("usage 原始字段:", json.dumps(resp.usage.model_dump(), ensure_ascii=False))


def run_tools(resp) -> list[dict]:
    """逐个执行 tool_use，按 id 生成 tool_result。"""
    results = []
    for block in resp.content:
        if block.type == "tool_use":
            value = FAKE_NUMBERS.get(block.input.get("name"), -1)
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": str(value)}
            )
    return results


def main() -> None:
    client = anthropic.Anthropic(api_key=load_key(), base_url=BASE_URL)

    # 1+2：单个工具调用的完整往返，同时观察 thinking 块和 usage
    messages = [{"role": "user", "content": "What is the secret number for 'alpha'? Use the tool."}]
    r1 = client.messages.create(model=FLASH, max_tokens=1024, tools=TOOLS, messages=messages)
    show("第 1 次响应（期望 tool_use）", r1)

    # 原样把 assistant 的内容块放回历史，包括 thinking 块
    messages.append({"role": "assistant", "content": r1.content})
    messages.append({"role": "user", "content": run_tools(r1)})
    r2 = client.messages.create(model=FLASH, max_tokens=1024, tools=TOOLS, messages=messages)
    show("第 2 次响应（带着 tool_result 回去，期望文本答案）", r2)
    for b in r2.content:
        if b.type == "text":
            print("最终文本:", b.text[:200])

    # 3：要求一次调用多个工具；顺便试 disable_parallel_tool_use 是否被忽略
    multi_prompt = [
        {
            "role": "user",
            "content": "Get the secret numbers for 'alpha', 'beta' and 'gamma'. "
            "Call the tool for all three in the same turn.",
        }
    ]
    r3 = client.messages.create(
        model=FLASH,
        max_tokens=1024,
        tools=TOOLS,
        tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        messages=multi_prompt,
    )
    show("多工具响应（已设置 disable_parallel_tool_use=True）", r3)
    n_calls = sum(1 for b in r3.content if b.type == "tool_use")
    print("同一响应里的 tool_use 数量:", n_calls)

    multi_prompt.append({"role": "assistant", "content": r3.content})
    multi_prompt.append({"role": "user", "content": run_tools(r3)})
    r4 = client.messages.create(model=FLASH, max_tokens=1024, tools=TOOLS, messages=multi_prompt)
    show("多工具：回传全部 tool_result 之后", r4)

    # 5：缓存。完全相同的前缀再发一次，看 usage 里命中字段有没有变化
    r5 = client.messages.create(model=FLASH, max_tokens=1024, tools=TOOLS, messages=messages)
    show("缓存检查：与第 2 次完全相同的请求再发一次", r5)

    # 1：另一个模型名只发一个极小请求确认能不能用
    if "--pro" in sys.argv:
        try:
            r6 = client.messages.create(
                model=PRO, max_tokens=16, messages=[{"role": "user", "content": "ping"}]
            )
            show(f"{PRO} 可用性", r6)
        except anthropic.APIStatusError as e:
            print(f"\n--- {PRO} 调用失败: HTTP {e.status_code} {e.message[:200]}")


if __name__ == "__main__":
    main()
