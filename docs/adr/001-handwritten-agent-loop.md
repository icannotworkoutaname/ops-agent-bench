# ADR 001: Hand-written agent loop instead of an agent framework

> English version first, Chinese version below.

## Status

Accepted (stage 1).

## Context

The point of this project is to compare how LLM agents diagnose known faults: accuracy,
out-of-bounds attempts and cost. For that to be trustworthy, the repository owner (who does
not write code) must be able to read exactly what the agent saw and did at every step, and
we must be able to account for every token that was billed.

Options considered:

1. **An agent framework** (LangChain, LangGraph, CrewAI, ...). Convenient, but the message
   history, retries, prompt templates and tool-call plumbing live inside the framework. Reading
   a transcript means reading framework internals, and a framework upgrade can silently change
   what is sent to the model.
2. **The Anthropic SDK's built-in tool runner.** Less code, but the loop that sends tool results
   back is hidden inside the SDK, so we cannot easily record each request, enforce our own
   stop conditions, or handle the quirks of the compatibility endpoint.
3. **A hand-written loop** on top of the plain Messages API (`client.messages.create`).

The model is DeepSeek, reached through its Anthropic-compatible endpoint. Before writing the
loop we verified that endpoint with a small script (`scripts/check_llm_endpoint.py`) instead of
assuming it behaves like Anthropic's own API. What we found:

- A response can contain **several `tool_use` blocks at once**, even when
  `disable_parallel_tool_use=true` is sent: the endpoint ignores that flag.
- Responses contain a **`thinking` block by default**, and sending the previous assistant
  content back unchanged works.
- `usage.input_tokens` counts **only the uncached part** of the input; cache hits are in
  `cache_read_input_tokens` (in multiples of 128 tokens). `cache_creation_input_tokens` stays 0.
- `cache_control` is ignored; caching is automatic and based on a stable request prefix.

## Decision

Write the loop by hand (`src/bench/agent/loop.py`, one file) using the Messages API directly,
and expose the cluster tools through an MCP server (see ADR 002).

- Every tool call of a response is executed in order, and all results go back in **one** user
  message, each matched by `tool_use_id`.
- Assistant content blocks (including thinking) are appended to the history unchanged. The
  history is **append-only**, so the request prefix stays stable and can be cached.
- Stop conditions (steps, wall-clock time, cumulative tokens) are checked before each request.
  Hitting a limit is recorded as "no diagnosis submitted", not as a crash.
- A reply without any tool call gets a short generic reminder (at most `max_nudges` times).
- The loop emits plain dict events (setup / request / response / tool_call / stop); the runner
  writes them to `transcript.jsonl` line by line.
- The loop depends only on two small interfaces (`ModelClient`, `ToolSource`), so it is tested
  with a scripted fake model and fake tools, with no network and no cost.

## Consequences

- More code is ours: we must handle parallel tool calls, bad `submit_diagnosis` arguments,
  retries and stop conditions ourselves. That is about 200 lines and is covered by unit tests.
- Nothing is hidden: `bench show` can reconstruct every step from the transcript.
- Switching to another model only needs a new entry in `config/run.yaml`, as long as it speaks
  the Messages API. Behaviour differences of a new endpoint must be verified with
  `check_llm_endpoint.py` first.

---

# ADR 001：手写 agent 循环，不用 agent 框架

## 状态

已采纳（第 1 阶段）。

## 背景

这个项目要比较不同 LLM agent 诊断已知故障的表现：准确率、越权尝试和成本。要让结论可信，
仓库所有者（不写代码）必须能看清 agent 每一步看到了什么、做了什么，我们也必须能说清楚
每一个被计费的 token 是怎么来的。

考虑过的方案：

1. **agent 框架**（LangChain、LangGraph、CrewAI 等）：方便，但消息历史、重试、提示模板、
   工具调用的来回传递都藏在框架内部。读一份记录就得去读框架的实现，框架升级还可能悄悄改变
   发给模型的内容。
2. **Anthropic SDK 自带的 tool runner**：代码少，但“把工具结果发回去”的循环藏在 SDK 里，
   我们没法方便地记录每次请求、实施自己的停止条件，也没法处理兼容端点的特殊行为。
3. **在普通 Messages API（`client.messages.create`）之上手写循环**。

模型统一用 DeepSeek，通过它的 Anthropic 兼容端点调用。写循环之前，我们先用一个小脚本
（`scripts/check_llm_endpoint.py`）验证了这个端点，而不是假设它和 Anthropic 原生 API 一样。
实测结果：

- 一次响应里可以同时有**多个 `tool_use`**，即使请求里设置了 `disable_parallel_tool_use=true`
  也一样：端点忽略这个参数。
- 响应**默认带 `thinking` 块**，把上一轮 assistant 的内容原样放回去是可以的。
- `usage.input_tokens` 只包含**未命中缓存**的输入；命中的部分在 `cache_read_input_tokens`
  里（以 128 token 为单位）。`cache_creation_input_tokens` 一直是 0。
- `cache_control` 被忽略；缓存是自动的，依赖稳定的请求前缀。

## 决定

直接用 Messages API 手写循环（`src/bench/agent/loop.py`，一个文件），集群工具通过 MCP server
提供（见 ADR 002）。

- 一次响应里的所有工具调用按顺序逐个执行，所有结果放进**同一条** user 消息，用 `tool_use_id`
  一一对应。
- assistant 的内容块（包括 thinking）原样追加到历史里。历史**只追加、不改写**，请求前缀保持
  稳定，才能命中缓存。
- 停止条件（步数、总时间、累计 token）在每次请求之前检查。到达上限记为“未提交诊断”，不算崩溃。
- 模型回复里没有任何工具调用时，给一句通用的提醒（最多 `max_nudges` 次）。
- 循环只产生普通字典事件（setup / request / response / tool_call / stop），由运行器逐行写进
  `transcript.jsonl`。
- 循环只依赖两个很小的接口（`ModelClient`、`ToolSource`），所以可以用脚本化的假模型和假工具测试，
  不联网、不花钱。

## 影响

- 更多代码要自己负责：并行工具调用、不合法的 `submit_diagnosis` 参数、重试、停止条件。大约
  200 行，有单元测试覆盖。
- 没有任何东西是隐藏的：`bench show` 可以从记录里还原每一步。
- 换模型只需要在 `config/run.yaml` 里加一个条目（前提是它支持 Messages API）。新端点的行为差异
  必须先用 `check_llm_endpoint.py` 验证。
