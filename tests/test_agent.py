"""agent 循环和 submit_diagnosis 的单元测试。

用一个按脚本返回固定响应的假模型驱动循环，不发任何真实请求。
响应用 Anthropic SDK 自己的类型构造，这样 model_dump、原样回传等行为和真实情形一致。
"""

import asyncio
import json
import re

import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage

from bench.agent import prompts
from bench.agent.diagnosis import (
    CAUSE_CATEGORIES,
    COMPONENTS,
    SUBMIT_DIAGNOSIS_TOOL,
    validate_diagnosis,
)
from bench.agent.loop import run_agent, to_jsonable
from bench.scenarios import SCENARIOS

CFG = {"max_steps": 25, "max_seconds": 600, "max_total_tokens": 500_000, "max_nudges": 2}

VALID = {
    "cause_category": "dependency_unavailable",
    "component": "redis",
    "evidence": [{"source": "list_pods(data)", "observation": "no redis pod"}],
    "recommended_action": "restore redis",
    "confidence": "high",
}


# ---------- 构造假响应 / 假依赖 ----------


def tool_use(id_: str, name: str, args: dict) -> ToolUseBlock:
    return ToolUseBlock(id=id_, name=name, input=args, type="tool_use")


def thinking(text: str = "hmm") -> ThinkingBlock:
    return ThinkingBlock(thinking=text, signature="sig", type="thinking")


def msg(blocks, stop_reason="tool_use", inp=100, out=10, cache=0, model="deepseek-test") -> Message:
    return Message(
        id="m",
        type="message",
        role="assistant",
        model=model,
        content=blocks,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cache),
    )


def submit(args=None, id_="s1") -> Message:
    return msg([tool_use(id_, "submit_diagnosis", VALID if args is None else args)])


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.snapshots: list[list] = []  # 每次请求发出时 messages 的 JSON 快照

    async def create(self, system, messages, tools):
        self.snapshots.append(json.loads(json.dumps(to_jsonable(messages))))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeTools:
    def __init__(self, clock: Clock | None = None, advance: float = 0.0, raises: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.clock, self.advance, self.raises = clock, advance, raises

    async def definitions(self):
        return [{"name": "list_pods", "description": "d", "input_schema": {"type": "object"}}]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if self.clock:
            self.clock.now += self.advance
        if self.raises:
            raise RuntimeError("工具进程挂了")
        if name == "nope":
            return "错误: 未知工具", True
        return f"result of {name}", False


def run(model, tools=None, cfg=None, clock=None):
    events: list[dict] = []
    result = asyncio.run(
        run_agent(
            model,
            tools or FakeTools(),
            cfg or CFG,
            "SYS",
            "TASK",
            on_event=events.append,
            clock=clock or Clock(),
        )
    )
    return result, events


# ---------- 正常流程 ----------


def test_tool_call_then_submit():
    model = ScriptedModel(
        [msg([thinking(), tool_use("t1", "list_pods", {"namespace": "data"})]), submit()]
    )
    tools = FakeTools()
    result, events = run(model, tools)

    assert result.status == "diagnosed"
    assert result.diagnosis == VALID
    assert result.steps == 2
    assert tools.calls == [("list_pods", {"namespace": "data"})]
    assert [e["type"] for e in events if e["type"] in ("setup", "stop")] == ["setup", "stop"]


def test_thinking_block_is_sent_back_unchanged():
    model = ScriptedModel([msg([thinking("step one"), tool_use("t1", "list_pods", {})]), submit()])
    run(model)
    second_request = model.snapshots[1]
    assistant = second_request[1]
    assert assistant["role"] == "assistant"
    assert {"type": "thinking", "thinking": "step one", "signature": "sig"} in assistant["content"]


def test_multiple_tool_uses_in_one_response_are_answered_in_one_message():
    model = ScriptedModel(
        [
            msg(
                [
                    thinking(),
                    TextBlock(text="checking three things", type="text"),
                    tool_use("a", "list_pods", {"namespace": "default"}),
                    tool_use("b", "list_pods", {"namespace": "data"}),
                    tool_use("c", "list_pods", {"namespace": "kafka"}),
                ]
            ),
            submit(),
        ]
    )
    tools = FakeTools()
    run(model, tools)

    assert [c[1]["namespace"] for c in tools.calls] == [
        "default",
        "data",
        "kafka",
    ]  # 按顺序逐个执行
    reply = model.snapshots[1][-1]
    assert reply["role"] == "user"
    assert [r["tool_use_id"] for r in reply["content"]] == ["a", "b", "c"]
    assert all(r["type"] == "tool_result" for r in reply["content"])


def test_history_is_append_only_so_the_prefix_stays_cacheable():
    model = ScriptedModel(
        [
            msg([tool_use("1", "list_pods", {})]),
            msg([tool_use("2", "list_pods", {})]),
            msg([tool_use("3", "list_pods", {})]),
            submit(),
        ]
    )
    run(model)
    for earlier, later in zip(model.snapshots, model.snapshots[1:], strict=False):
        assert later[: len(earlier)] == earlier  # 前一次发出去的内容，后一次一字不差地还在最前面


# ---------- 出错与容错 ----------


def test_invalid_submission_is_rejected_with_reasons_and_the_loop_continues():
    bad = {**VALID, "cause_category": "made_up", "confidence": "certain"}
    model = ScriptedModel([submit(bad, "s1"), submit(VALID, "s2")])
    result, _ = run(model)

    assert result.status == "diagnosed" and result.steps == 2
    reply = model.snapshots[1][-1]["content"][0]
    assert reply["is_error"] is True
    assert "made_up" in reply["content"] and "confidence" in reply["content"]


def test_tool_error_text_is_passed_back_to_the_model():
    model = ScriptedModel([msg([tool_use("t", "nope", {})]), submit()])
    result, _ = run(model)
    assert result.status == "diagnosed"
    reply = model.snapshots[1][-1]["content"][0]
    assert reply["is_error"] is True and "未知工具" in reply["content"]


def test_a_crashing_tool_does_not_crash_the_loop():
    model = ScriptedModel([msg([tool_use("t", "list_pods", {})]), submit()])
    result, _ = run(model, FakeTools(raises=True))
    assert result.status == "diagnosed"
    reply = model.snapshots[1][-1]["content"][0]
    assert reply["is_error"] is True and "工具进程挂了" in reply["content"]


def test_model_api_failure_is_reported_as_error_not_raised():
    model = ScriptedModel([ConnectionError("网络断了")])
    result, _ = run(model)
    assert result.status == "error" and "网络断了" in result.reason


# ---------- 没有调用工具时的提醒 ----------


def test_reply_without_tools_gets_a_nudge_then_continues():
    model = ScriptedModel([msg([TextBlock(text="all good?", type="text")], "end_turn"), submit()])
    result, _ = run(model)
    assert result.status == "diagnosed" and result.steps == 2
    assert model.snapshots[1][-1] == {"role": "user", "content": prompts.NUDGE_PROMPT}


def test_gives_up_after_max_nudges():
    talk = lambda: msg([TextBlock(text="hmm", type="text")], "end_turn")
    model = ScriptedModel([talk(), talk(), talk()])
    result, _ = run(model)
    assert result.status == "no_diagnosis" and "没有调用工具" in result.reason
    assert result.steps == 3  # 第 1 次 + 2 次提醒之后的回复


# ---------- 停止条件：到上限算“未提交诊断”，不算崩溃 ----------


def test_step_limit():
    cfg = {**CFG, "max_steps": 3}
    model = ScriptedModel([msg([tool_use(str(i), "list_pods", {})]) for i in range(5)])
    result, _ = run(model, cfg=cfg)
    assert result.status == "no_diagnosis" and "步数" in result.reason
    assert result.steps == 3 and len(model.snapshots) == 3


def test_time_limit():
    clock = Clock()
    model = ScriptedModel(
        [msg([tool_use("1", "list_pods", {})]), msg([tool_use("2", "list_pods", {})])]
    )
    result, _ = run(model, FakeTools(clock, advance=700), clock=clock)
    assert result.status == "no_diagnosis" and "时间" in result.reason
    assert len(model.snapshots) == 1  # 超时后不再发起新的请求


def test_token_limit_counts_uncached_input_plus_output_but_not_cache_hits():
    cfg = {**CFG, "max_total_tokens": 1000}
    # 每步 400 未缓存 + 300 缓存命中 + 50 输出：只有 450 算进上限；
    # 第三步后累计 1350 ≥ 1000 才停（如果把缓存命中也算进去，第二步后就会停）
    step = lambda i: msg([tool_use(str(i), "list_pods", {})], inp=400, cache=300, out=50)
    model = ScriptedModel([step(1), step(2), step(3), step(4)])
    result, _ = run(model, cfg=cfg)
    assert result.status == "no_diagnosis" and "token" in result.reason
    assert result.steps == 3
    assert result.usage_total["cache_read_input_tokens"] == 900


# ---------- 记录 ----------


def test_events_cover_every_step_and_tool_call():
    model = ScriptedModel([msg([tool_use("t1", "list_pods", {"namespace": "data"})]), submit()])
    _, events = run(model)
    kinds = [e["type"] for e in events]
    assert kinds == [
        "setup",
        "request",
        "response",
        "tool_call",
        "request",
        "response",
        "tool_call",
        "stop",
    ]
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["name"] == "list_pods" and call["input"] == {"namespace": "data"}
    assert "duration_s" in call and call["is_error"] is False
    assert all(e["sent_at"] for e in events if e["type"] == "request")
    json.dumps(events)  # 每个事件都必须能直接写成一行 JSON


def test_actual_model_name_from_the_response_is_recorded():
    model = ScriptedModel([msg([tool_use("1", "list_pods", {})], model="served-model-x"), submit()])
    result, _ = run(model)
    assert "served-model-x" in result.models_seen


# ---------- submit_diagnosis 校验 ----------


def test_valid_diagnosis_passes():
    assert validate_diagnosis(VALID) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cause_category", "nonsense"),
        ("cause_category", None),
        ("component", "mysql"),
        ("confidence", "very high"),
        ("evidence", []),
        ("evidence", "just text"),
        ("evidence", [{"source": "x"}]),
        ("evidence", [{"source": "", "observation": "y"}]),
        ("evidence", ["plain string"]),
        ("recommended_action", ""),
        ("recommended_action", 5),
    ],
)
def test_invalid_diagnosis_is_rejected(field, value):
    assert validate_diagnosis({**VALID, field: value})


def test_missing_fields_and_wrong_type_are_rejected():
    assert validate_diagnosis({}) and validate_diagnosis("text") and validate_diagnosis(None)
    assert len(validate_diagnosis({})) == 5  # 五个字段各报一条


def test_schema_enums_match_the_validator():
    props = SUBMIT_DIAGNOSIS_TOOL["input_schema"]["properties"]
    assert props["cause_category"]["enum"] == list(CAUSE_CATEGORIES)
    assert props["component"]["enum"] == COMPONENTS


# ---------- 提示词：不能包含任何场景信息 ----------


def test_prompts_do_not_mention_scenarios():
    text = f"{prompts.SYSTEM_PROMPT} {prompts.TASK_PROMPT} {prompts.NUDGE_PROMPT}".lower()
    for scenario_id in SCENARIOS:
        assert scenario_id not in text
    # 按完整单词匹配：否则 "default"（namespace 名）里的 "fault" 会误报
    for hint in ("secret", "crashloop", "bootstrap", "unavailable", "down", "missing", "fault"):
        assert not re.search(rf"\b{hint}\b", text), f"提示词里不应出现 {hint!r}"
