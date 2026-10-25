# ADR 004: Cross-run contamination, the lookback window and the cooldown

> English version first, Chinese version below.

## Status

Accepted for stage 2.

## Context

A run ends, the environment is restored, and the next run starts. Traces of the previous fault can still be
in the environment when the next agent starts looking:

- Kubernetes keeps Events for about an hour. `get_events` could return the previous scenario's `Killing`,
  `OOMKilled`, `secret ... not found` events.
- Pods that were **not** recreated keep their old log lines. `tail` limits the number of lines, not their age; with
  low traffic an old error stays within the last few hundred lines.
- With Prometheus, the metric history contains the previous fault's curve.
- (Found during stage 1) after the laptop slept, expired ServiceAccount tokens made controllers emit
  `Unauthorized` events that leaked into `get_events`.

A leaked trace can make an agent answer wrong, and, worse, answer right *because* it saw a trace. Both make
the accuracy numbers untrustworthy.

## Decision

Prevent first, detect afterwards:

1. **Lookback window W** (config `lookback_minutes`, default 10): `get_events`, `get_pod_logs` (with
   `since_seconds`, in addition to `tail`) and `promql_query` only return the last W minutes. A PromQL
   expression is refused if its range, its `[..]` selectors and its `offset` would reach further back than W;
   the `@` modifier is refused. Events outside the window are not even counted in the output.
2. **Cooldown**: before injecting, the full health check must pass continuously for W minutes (checked every 30 s, a
   failure restarts the timer, and giving up after 30 min makes the run invalid). So the earliest moment the window can
   reach is already after the previous restore.
3. **Events are deleted** in the three visible namespaces right before injection (Events are transient diagnostics,
   not data).
4. **Control-plane check** in `bench health`: auth errors in controller-manager / apiserver logs in the last 5
   minutes make the environment unhealthy.
5. **Pod terminations (added 2026-10-25 after the trial run).** The trial showed that the Kafka broker can be
   OOMKilled by its own 768Mi limit while nobody touched it (twice in about a day), and that the pod then keeps
   `lastState: OOMKilled` until it is recreated, which made an agent chase it for 66 tool calls. Two changes:
   the cooldown also requires that no container in the visible namespaces and `monitoring` terminated
   abnormally (exit code != 0) within the last W minutes (`check_recent_terminations`; the plain health check used
   before and after a run does not include it, because injected faults terminate containers on purpose), and
   `describe_pod` no longer shows a `lastState` whose `finishedAt` is older than W (the restart count stays visible).
6. **After-the-fact check** (`src/bench/contamination.py`): every tool result of every run is scanned for
   scenario ids, harness names and the *other* scenarios' signature strings. A hit marks the run as suspected
   contamination: excluded from accuracy and listed separately in the report. Each scenario declares its own signatures;
   they are unit-tested not to collide with each other or to match healthy output.

## Cost

Each run waits W (+1) minutes before injection: 55 runs at W=10 add about 10 hours of pure waiting to the
batch (about 14.7 hours in total for the planned 55 runs). A larger W shows the agent a longer healthy
baseline but costs more time per run.

## Alternatives considered

- *Rebuild the cluster / namespaces between runs*: cleanest, but far too slow and destructive for 55+ runs.
- *Tag and filter by run id*: the tag would itself be a trace that reveals the harness (isolation principle 2).
- *Only the after-the-fact check*: it can only tell us a run was contaminated, after paying for it.

## Known gaps

- The window applies to events, logs, metrics and (since 2026-10-25) `lastState`. **Pod age and restart
  counts** shown by `list_pods` / `describe_pod` are cumulative and not windowed. Pods that were recreated by a
  restore start from zero, others keep their history.
- A Kafka OOMKill *during* a run is not detected by the cooldown (it only looks before injection). For every
  scenario except `sp-oom` the `OOMKilled` signature flags it; for `sp-oom` it would not, so the formal batch is
  checked afterwards against the Kafka pod's restart timestamps.
- Scenarios that leave no unique string (`sp-pod-killed`, `ingestion-overload`) cannot be detected by the
  signature check; for them only the window and cooldown protect.
- Stage-1 analysis (`scripts/analyze_stage1_contamination.py`): the four real runs showed no foreign signature
  in any tool result; the detector was also checked against the same runs treated as the control scenario,
  where it found each run's own symptoms, so "no hits" is meaningful.

---

# ADR 004：跨运行污染、回看窗口与冷却期

## 状态

第 2 阶段采纳。

## 背景

一次运行结束、环境恢复后，下一次运行就开始了。下一个 agent 开始调查时，环境里可能还留着上一个故障的痕迹：

- Kubernetes 事件默认保留约 1 小时，`get_events` 可能返回上一个场景的 `Killing`、`OOMKilled`、
  `secret ... not found` 等事件。
- **没有被重建**的 Pod 会保留旧日志。`tail` 限制的是行数，不是时间；流量低的时候，旧错误仍然在最后几百行里。
- 加了 Prometheus 之后，指标历史里有上一次故障的曲线。
- （第 1 阶段发现的）笔记本休眠后，过期的 ServiceAccount token 会让控制器产生 `Unauthorized` 事件，漏进 `get_events`。

痕迹一旦漏出去，agent 可能因此答错，更糟的是可能因为看到了痕迹而“碰巧”答对。两种情况都让准确率不可信。

## 决定

先预防，再事后检测：

1. **回看窗口 W**（配置 `lookback_minutes`，默认 10 分钟）：`get_events`、`get_pod_logs`（在 `tail` 之外再加
   `since_seconds`）和 `promql_query` 只返回最近 W 分钟的数据。PromQL 表达式的区间、`[..]` 范围选择器和
   `offset` 加起来如果会读到比 W 更早的数据就拒绝；`@` 修饰符直接拒绝。窗口之外的事件连数量都不告诉 agent。
2. **冷却期**：注入前，完整的健康检查必须连续 W 分钟全部通过（每 30 秒检查一次，任何一次失败就重新计时，
   最多等 30 分钟，超时则这次运行无效）。这样窗口能回看到的最早时刻也已经是上一次恢复之后了。
3. **注入前删除事件**：删除三个可见 namespace 里的 Event（事件是临时的诊断记录，不是数据）。
4. **控制面检查**：`bench health` 会检查 controller-manager / apiserver 最近 5 分钟日志里的认证错误，有就判环境不健康。
5. **Pod 异常终止（试跑后 2026-10-25 新增）**：试跑发现 Kafka broker 会在没人动它的情况下被自己的 768Mi
   内存上限 OOMKilled（一天内两次），而且 Pod 在被重建之前一直保留 `lastState: OOMKilled`，让 agent 为它多查了 66 次工具。
   两处改动：冷却期额外要求可见 namespace 和 `monitoring` 里最近 W 分钟内没有容器异常终止（退出码非 0，
   `check_recent_terminations`；运行前后用的普通健康检查不含这一项，因为注入的故障本来就会让容器终止）；
   `describe_pod` 不再显示 `finishedAt` 早于 W 的 `lastState`（重启次数仍然显示）。
6. **事后检查**（`src/bench/contamination.py`）：扫描每次运行所有工具返回的内容，找场景 id、测试框架的名字和**其他**场景的
   特征字符串。命中就把这次运行标记为“疑似污染”：不计入准确率，在报告里单独列出。每个场景自己声明特征字符串，
   单元测试保证它们彼此不冲突、也不会匹配健康环境的输出。

## 代价

每次运行在注入前要多等 W（+1）分钟：W=10 时，55 次运行要多出约 10 小时纯等待（计划的 55 次总共约 14.7 小时）。
W 越大，agent 能看到的健康基线越长，但每次运行的时间越长。

## 考虑过的其他方案

- *每次运行之间重建集群 / namespace*：最干净，但 55+ 次运行下太慢、破坏性太大。
- *给数据打运行 id 标签再过滤*：这个标签本身就是暴露测试框架的痕迹（违反隔离原则 2）。
- *只做事后检查*：它只能在花完钱之后告诉我们这次运行被污染了。

## 已知的缺口

- 窗口作用于事件、日志、指标和（2026-10-25 起）`lastState`。`list_pods` / `describe_pod` 显示的**Pod 年龄和
  重启次数**是累计值，不受窗口限制。被恢复流程重建的 Pod 从零开始，其他的会保留历史。
- 运行**过程中**发生的 Kafka OOMKilled，冷却期发现不了（它只看注入之前）。除 `sp-oom` 外的场景会被 `OOMKilled`
  特征字符串标出来；`sp-oom` 不会，所以正式批量结束后要对照 Kafka Pod 的重启时间再核查一遍。
- 没有独有字符串的场景（`sp-pod-killed`、`ingestion-overload`）无法被特征字符串检查发现，只能靠窗口和冷却期保护。
- 第 1 阶段的分析（`scripts/analyze_stage1_contamination.py`）：4 次真实运行的所有工具返回里都没有发现外来的特征字符串；
  同时也用同一批运行验证了检测器本身——把它们当作对照场景来检查，每次运行自己的症状都被识别了出来，所以“0 命中”是有意义的。
