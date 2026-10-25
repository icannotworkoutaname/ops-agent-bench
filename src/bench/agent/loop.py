"""手写的 agent 循环。

不用任何 agent 框架，也不用 Anthropic SDK 自带的 tool runner：模型发出的每一次工具调用、
我们回传的每一个结果，都在这个文件里清清楚楚地走一遍，方便读、方便记录、方便出问题时排查。

一次运行的流程（对应 `for step in ...` 里的每一轮）：
  1. 把“系统提示 + 工具定义 + 到目前为止的全部消息”发给模型。
  2. 模型的回复里可能有 thinking、文字、以及 0 个或多个 tool_use。
  3. 没有 tool_use：提醒它用工具或提交诊断（最多提醒 max_nudges 次）。
  4. 有 tool_use：逐个执行，所有结果放进“同一条”user 消息里，按 tool_use 的 id 一一对应地发回去。
     调用 submit_diagnosis 且参数合法 -> 运行结束。
  5. 到达步数 / 时间 / token 上限 -> 记为“未提交诊断”，这不算崩溃。

缓存相关：DeepSeek 按请求前缀自动缓存，所以已经发出去的消息只追加、不改写、不重排；
assistant 的内容块（包括 thinking）原样放回历史。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from bench.agent.diagnosis import SUBMIT_DIAGNOSIS_TOOL, validate_diagnosis
from bench.agent.prompts import NUDGE_PROMPT


class ModelClient(Protocol):
    async def create(self, system: str, messages: list, tools: list[dict]) -> Any: ...


class ToolSource(Protocol):
    async def definitions(self) -> list[dict]: ...

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """返回 (结果文本, 是否出错)。"""
        ...


@dataclass
class AgentResult:
    status: str  # diagnosed / no_diagnosis / error
    reason: str
    diagnosis: dict | None = None
    steps: int = 0
    elapsed_seconds: float = 0.0
    usage_total: dict = field(default_factory=dict)
    models_seen: list[str] = field(default_factory=list)


def to_jsonable(obj: Any) -> Any:
    """把消息 / SDK 对象递归转成可以写进 JSON 的普通结构（只用于记录，不影响发给模型的内容）。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    return obj


def _usage_dict(usage: Any) -> dict:
    return usage.model_dump(mode="json") if hasattr(usage, "model_dump") else dict(usage or {})


def _counted_tokens(usage: dict) -> int:
    """算进 token 上限的部分：未命中缓存的输入 + 输出，**不含**命中缓存的输入。

    为什么不含缓存命中：agent 每一步都要把整段历史重新发一遍，命中缓存的部分每步都会重复计一次，
    累计下来只反映“步数 × 历史长度”，不是模型真正新读、新写的内容。试跑里 4 次没提交诊断有 3 次
    撞上旧算法的 50 万上限，上限数值没变，只是改了口径（用户 2026-10-25 决定）。
    实测 DeepSeek 的 input_tokens 只是未命中缓存的那部分，缓存命中单独放在
    cache_read_input_tokens 里，所以直接相加 input_tokens 和 output_tokens 就行。
    """
    return sum(usage.get(k) or 0 for k in ("input_tokens", "output_tokens"))


def _tool_result(tool_use_id: str, text: str, is_error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        block["is_error"] = True
    return block


async def run_agent(
    model: ModelClient,
    tools: ToolSource,
    cfg: dict,
    system: str,
    task: str,
    on_event: Callable[[dict], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """cfg 是 run.yaml 里的 agent 一节。on_event 收到的每个事件都是可以直接写成一行 JSON 的字典。"""

    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    # submit_diagnosis 是 agent 侧的工具，追加在 MCP 工具之后；顺序固定，保持前缀稳定
    tool_defs = [*await tools.definitions(), SUBMIT_DIAGNOSIS_TOOL]
    emit({"type": "setup", "system": system, "tools": tool_defs, "task": task})

    messages: list = [{"role": "user", "content": task}]
    start = clock()
    usage_total: dict[str, int] = {}
    models_seen: list[str] = []
    tokens_used = 0
    nudges = 0
    steps = 0

    def finish(status: str, reason: str, diagnosis: dict | None = None) -> AgentResult:
        emit({"type": "stop", "status": status, "reason": reason, "steps": steps})
        return AgentResult(
            status=status,
            reason=reason,
            diagnosis=diagnosis,
            steps=steps,
            elapsed_seconds=clock() - start,
            usage_total=usage_total,
            models_seen=models_seen,
        )

    while True:
        # ---- 停止条件：都在“发起下一次请求之前”检查 ----
        if steps >= cfg["max_steps"]:
            return finish("no_diagnosis", f"达到步数上限 {cfg['max_steps']}")
        if clock() - start >= cfg["max_seconds"]:
            return finish("no_diagnosis", f"达到时间上限 {cfg['max_seconds']}s")
        if tokens_used >= cfg["max_total_tokens"]:
            return finish("no_diagnosis", f"达到 token 上限 {cfg['max_total_tokens']}")

        steps += 1
        emit(
            {
                "type": "request",
                "step": steps,
                # 发出时间用来判断这次请求落在高峰还是非高峰时段（按请求时间计费）
                "sent_at": datetime.now(UTC).isoformat(),
                "messages": to_jsonable(messages),
            }
        )
        try:
            response = await model.create(system, messages, tool_defs)
        except Exception as e:  # noqa: BLE001 - 网络 / 接口错误：记为 error，由运行器决定怎么处理
            return finish("error", f"模型请求失败: {type(e).__name__}: {e}")

        usage = _usage_dict(response.usage)
        for key, value in usage.items():
            if isinstance(value, int):
                usage_total[key] = usage_total.get(key, 0) + value
        tokens_used += _counted_tokens(usage)
        # 记录响应里实际的模型名：兼容端点可能把旧名字映射到新模型
        if response.model not in models_seen:
            models_seen.append(response.model)
        emit(
            {
                "type": "response",
                "step": steps,
                "received_at": datetime.now(UTC).isoformat(),
                "model": response.model,
                "stop_reason": response.stop_reason,
                "content": to_jsonable(response.content),
                "usage": usage,
            }
        )

        # assistant 的内容块（含 thinking）原样放回历史，不删改、不重排
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            if nudges >= cfg["max_nudges"]:
                return finish("no_diagnosis", f"模型连续 {nudges + 1} 次没有调用工具也没有提交诊断")
            nudges += 1
            messages.append({"role": "user", "content": NUDGE_PROMPT})
            continue

        # 一次响应里可能有多个 tool_use（兼容端点忽略 disable_parallel_tool_use）：
        # 逐个执行，结果放进同一条 user 消息，每个结果带上对应的 tool_use_id
        results = []
        for block in tool_uses:
            arguments = block.input if isinstance(block.input, dict) else {}
            began = clock()

            if block.name == SUBMIT_DIAGNOSIS_TOOL["name"]:
                problems = validate_diagnosis(block.input)
                is_error = bool(problems)
                text = (
                    "诊断参数不合法，请修正后重新提交:\n- " + "\n- ".join(problems)
                    if problems
                    else "已收到诊断。"
                )
            else:
                try:
                    text, is_error = await tools.call(block.name, arguments)
                except Exception as e:  # noqa: BLE001 - 工具出错不能让循环崩掉，把错误告诉模型
                    text, is_error = f"错误: 工具调用失败 {type(e).__name__}: {e}", True

            emit(
                {
                    "type": "tool_call",
                    "step": steps,
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                    "result": text,
                    "is_error": is_error,
                    "duration_s": round(clock() - began, 3),
                }
            )

            if block.name == SUBMIT_DIAGNOSIS_TOOL["name"] and not is_error:
                return finish("diagnosed", "模型提交了诊断", diagnosis=block.input)
            results.append(_tool_result(block.id, text, is_error))

        messages.append({"role": "user", "content": results})
