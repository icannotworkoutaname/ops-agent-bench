"""假模型：按固定脚本返回响应，不联网、不花钱。

用于开发和调试 agent 循环、运行记录、bench show 等“循环本身”的部分。
它不看场景，也不知道标准答案，每次都走同样的三步：先看 Pod，再看 lag 和事件，最后提交一个
固定的诊断。所以它的诊断通常是错的，这是预期的。
"""

from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage


def _message(blocks: list, step: int) -> Message:
    return Message(
        id=f"fake-{step}",
        type="message",
        role="assistant",
        model="fake-model",
        content=blocks,
        stop_reason="tool_use",
        stop_sequence=None,
        usage=Usage(
            input_tokens=900 + 100 * step,
            output_tokens=60,
            # 第一步之后假装命中了一部分缓存，让 usage 里的缓存字段有数可看
            cache_read_input_tokens=0 if step == 1 else 128,
        ),
    )


def _use(id_: str, name: str, args: dict) -> ToolUseBlock:
    return ToolUseBlock(id=id_, name=name, input=args, type="tool_use")


class FakeModel:
    def __init__(self):
        self.step = 0

    async def create(self, system: str, messages: list, tools: list[dict]) -> Message:
        self.step += 1
        if self.step == 1:
            blocks = [
                ThinkingBlock(
                    thinking="先看看各个 namespace 里的 Pod。", signature="fake", type="thinking"
                ),
                TextBlock(text="Checking pods in default and data.", type="text"),
                _use("f1", "list_pods", {"namespace": "default"}),
                _use("f2", "list_pods", {"namespace": "data"}),
            ]
        elif self.step == 2:
            blocks = [
                ThinkingBlock(
                    thinking="再看消费者 lag 和事件。", signature="fake", type="thinking"
                ),
                _use("f3", "get_consumer_lag", {"group": "stream-processor"}),
                _use("f4", "get_events", {"namespace": "default"}),
            ]
        else:
            blocks = [
                _use(
                    "f5",
                    "submit_diagnosis",
                    {
                        "cause_category": "no_fault",
                        "component": "none",
                        "evidence": [
                            {"source": "list_pods(default)", "observation": "（假模型的固定内容）"}
                        ],
                        "recommended_action": "none",
                        "confidence": "low",
                    },
                )
            ]
        return _message(blocks, self.step)
