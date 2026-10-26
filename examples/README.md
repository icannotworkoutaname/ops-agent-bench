# Examples / 示例

Five real runs from the stage-2 formal batch (`deepseek-flash`, seed 2, 2026-10-25), rendered with `bench show`.
All five were scored correct on both checks. They are not a random sample: they were picked to show what a diagnosis looks like.
The batch-level failures (no-fault false alarm, step-cap runs) are summarised in [`../docs/results-stage-2.md`](../docs/results-stage-2.md), not here.

第 2 阶段正式批量（`deepseek-flash`，种子 2，2026-10-25）里的 5 次真实运行，用 `bench show` 渲染成文本。
5 次都是两项评分全对。它们不是随机抽样，是为了展示“一次诊断长什么样”挑的；批量里的失败（无故障误报、撞步数上限）见 [`../docs/results-stage-2.md`](../docs/results-stage-2.md)。

| File | Scenario | Steps | Cost (CNY) | What to look at |
|---|---|---|---|---|
| [`missing-pg-secret.txt`](missing-pg-secret.txt) | missing-pg-secret | 7 | 0.019 | Short and clean. The old pod is still serving, so the system looks healthy; the agent finds the stuck new pod and says the outage has not happened yet. |
| [`redis-down.txt`](redis-down.txt) | redis-down | 8 | 0.027 | The scale-down is visible in the events. At steps 5–6 the agent questions whether Redis is really the root cause and checks the rest of the pipeline before submitting. |
| [`sp-oom.txt`](sp-oom.txt) | sp-oom | 8 | 0.039 | Reads the previous container's logs and the pod's last termination state, then cross-checks memory usage, the memory limit and OOM counters in Prometheus. |
| [`kafka-down.txt`](kafka-down.txt) | kafka-down | 21 | 0.079 | Write requests still return 202, so the symptom is only in logs and events. The agent takes a long detour through CPU-throttling metrics before settling. |
| [`ingestion-overload.txt`](ingestion-overload.txt) | ingestion-overload | 25 | 0.118 | The one correct run out of five for this scenario. It submitted on the 25th and last allowed step; the other four hit the cap without submitting. |

## Reading these files / 阅读说明

- The labels (`第 N 步`, `调用工具`, `用量` …) are the harness's; the agent's own reasoning and diagnosis are in English.
  标签是评测框架写的，模型的思考和诊断是英文。
- Long thinking and tool output are truncated in the rendering (`加 --full 查看全文`). The full `transcript.jsonl` of each run is not in the repository (0.1–2.3 MB each, in the git-ignored `runs/`).
  渲染时长文本被截断；完整的 `transcript.jsonl` 在被 gitignore 的 `runs/` 里，没有入库。
- The "环境" section (health checks, cooldown, symptom confirmation, restore) is what the harness did around the agent; the agent never sees it.
  “环境”部分是框架在 agent 之外做的事，agent 看不到。
- In `redis-down`, `kafka-down` and `ingestion-overload` the agent's thinking wonders whether the system is a test with an injected fault (it says things like "the injected fault" or "this benchmark scenario"). The prompt does not say so; the model infers it from the task wording. Whether that helps it is not measured here.
  这三份里模型会猜“这是不是注入的故障 / 评测场景”。提示词里没有这么说，是模型从任务措辞推测的；这对结果有没有帮助，本项目没有测。
- The score appears at the end; the ground truth is never shown to the agent.
  评分在末尾；标准答案不会给 agent 看。
