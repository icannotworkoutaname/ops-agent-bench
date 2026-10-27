# Examples

[English](#english) · [中文](#中文)

## English

Five real runs from the stage-2 formal batch (`deepseek-flash`, seed 2, 2026-10-25), rendered with `bench show`.
All five were scored correct on both checks. They are not a random sample: they were picked to show what a diagnosis looks like.
The batch-level failures (no-fault false alarm, step-cap runs) are summarised in [`../docs/results-stage-2.md`](../docs/results-stage-2.md), not here.

| File | Scenario | Steps | Cost (CNY) | What to look at |
|---|---|---|---|---|
| [`missing-pg-secret.txt`](missing-pg-secret.txt) | missing-pg-secret | 7 | 0.019 | Short and clean. The old pod is still serving, so the system looks healthy; the agent finds the stuck new pod and says the outage has not happened yet. |
| [`redis-down.txt`](redis-down.txt) | redis-down | 8 | 0.027 | The scale-down is visible in the events. At steps 5–6 the agent questions whether Redis is really the root cause and checks the rest of the pipeline before submitting. |
| [`sp-oom.txt`](sp-oom.txt) | sp-oom | 8 | 0.039 | Reads the previous container's logs and the pod's last termination state, then cross-checks memory usage, the memory limit and OOM counters in Prometheus. |
| [`kafka-down.txt`](kafka-down.txt) | kafka-down | 21 | 0.079 | Write requests still return 202, so the symptom is only in logs and events. The agent takes a long detour through CPU-throttling metrics before settling. |
| [`ingestion-overload.txt`](ingestion-overload.txt) | ingestion-overload | 25 | 0.118 | The one correct run out of five for this scenario. It submitted on the 25th and last allowed step; the other four hit the cap without submitting. |

Reading these files:

- The labels (`第 N 步`, `调用工具`, `用量` …) are written by the harness and are in Chinese; the agent's own reasoning and diagnosis are in English.
- Long thinking and tool output are truncated in the rendering (`加 --full 查看全文`). The full `transcript.jsonl` of each run is not in the repository (0.1–2.3 MB each, in the git-ignored `runs/`).
- The "环境" section (health checks, cooldown, symptom confirmation, restore) is what the harness did around the agent; the agent never sees it.
- In `redis-down`, `kafka-down` and `ingestion-overload` the agent's thinking wonders whether the system is a test with an injected fault (it says things like "the injected fault" or "this benchmark scenario"). Whether this affects the results was not tested in this project.
- The score appears at the end; the ground truth is never shown to the agent.

## 中文

第 2 阶段正式批量（`deepseek-flash`，种子 2，2026-10-25）里的 5 次真实运行，用 `bench show` 渲染成文本。
5 次都是两项评分全对。它们不是随机抽样，是为了展示“一次诊断长什么样”挑的；批量里的失败（无故障误报、撞步数上限）见 [`../docs/results-stage-2.md`](../docs/results-stage-2.md)。

| 文件 | 场景 | 步数 | 费用（元） | 看点 |
|---|---|---|---|---|
| [`missing-pg-secret.txt`](missing-pg-secret.txt) | missing-pg-secret | 7 | 0.019 | 简短干净。旧 Pod 还在服务，系统看起来是健康的；agent 找到了卡住的新 Pod，并指出故障还没有真正发生。 |
| [`redis-down.txt`](redis-down.txt) | redis-down | 8 | 0.027 | 事件里能看到缩容。第 5–6 步 agent 质疑 Redis 是否真是根因，并检查了管线的其余部分才提交。 |
| [`sp-oom.txt`](sp-oom.txt) | sp-oom | 8 | 0.039 | 读上一个容器的日志和 Pod 的上次终止状态，再用 Prometheus 交叉核对内存用量、内存上限和 OOM 计数。 |
| [`kafka-down.txt`](kafka-down.txt) | kafka-down | 21 | 0.079 | 写入请求仍返回 202，症状只在日志和事件里。agent 在 CPU 限流指标上绕了很长一段才收敛。 |
| [`ingestion-overload.txt`](ingestion-overload.txt) | ingestion-overload | 25 | 0.118 | 这个场景 5 次里唯一答对的一次。它在允许的最后一步（第 25 步）才提交；另外 4 次撞上步数上限，没有提交。 |

阅读说明：

- 标签（`第 N 步`、`调用工具`、`用量` 等）是评测框架写的，用中文；模型的思考和诊断是英文。
- 渲染时长文本被截断（`加 --full 查看全文`）。每次运行完整的 `transcript.jsonl` 没有入库（每份 0.1–2.3 MB，在被 gitignore 的 `runs/` 里）。
- “环境”部分（健康检查、冷却、症状确认、恢复）是框架在 agent 之外做的事，agent 看不到。
- `redis-down`、`kafka-down`、`ingestion-overload` 这三份里，模型会猜“这是不是注入的故障 / 评测场景”（原话如 “the injected fault”“this benchmark scenario”）。这是否影响结果，本项目没有测。
- 评分在末尾；标准答案不会给 agent 看。
