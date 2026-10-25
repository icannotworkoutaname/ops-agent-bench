# 第 2 阶段结果（批次 20261025T045650Z-deepseek-flash-seed2）

## 1. 批次信息

- 模型: `deepseek-flash`；响应里的实际模型名: deepseek-flash
- 固定输入的哈希（整个批次不变）: prompts `25abbde70071`，model `70868ee6a782`，agent_config `f74e45c94ab9`，tools `d25d276c03c2`
- 运行顺序的随机种子: `2`；计划 55 次（11 个场景 × 5 次）
- 实际尝试 55 次：有效 55，无效 0
- 时间范围: 2026-10-25T04:56:50.798885+00:00 → 2026-10-25T19:42:51.706823+00:00；因高峰时段暂停 0 次
- 总费用: 3.7630（有效运行 3.7630，无效运行 0.0000）

## 2. 每个场景（只含有效运行）

| 场景 | 标准答案 | 有效次数 | 分类正确 | 组件正确 | 两者都对 | 未提交 | 平均步数 | 平均费用 | 平均耗时(s) |
|---|---|---|---|---|---|---|---|---|---|
| `redis-down` | dependency_unavailable / redis | 5 | 5/5 | 5/5 | 5/5 | 0 | 15.0 | 0.0712 | 830 |
| `bad-kafka-bootstrap` | misconfiguration_env / stream-processor | 5 | 5/5 | 5/5 | 5/5 | 0 | 12.8 | 0.0433 | 788 |
| `missing-pg-secret` | missing_secret_or_config / query-service | 5 | 5/5 | 5/5 | 5/5 | 0 | 9.8 | 0.0352 | 868 |
| `sp-pod-killed` | transient_restart_no_action / stream-processor | 5 | 4/5 | 4/5 | 4/5 | 1 | 21.0 | 0.0926 | 787 |
| `sp-cpu-throttle` | resource_limit_cpu / stream-processor | 5 | 2/5 | 2/5 | 2/5 | 3 | 22.0 | 0.0811 | 1137 |
| `sp-oom` | resource_limit_memory / stream-processor | 5 | 5/5 | 5/5 | 5/5 | 0 | 12.0 | 0.0422 | 803 |
| `kafka-down` | dependency_unavailable / kafka | 5 | 5/5 | 5/5 | 5/5 | 0 | 18.4 | 0.0779 | 860 |
| `postgres-down` | dependency_unavailable / postgresql | 5 | 5/5 | 5/5 | 5/5 | 0 | 14.6 | 0.0741 | 921 |
| `query-probe-misconfig` | probe_misconfiguration / query-service | 5 | 5/5 | 5/5 | 5/5 | 0 | 12.0 | 0.0403 | 837 |
| `ingestion-overload` | load_saturation / ingestion-service | 5 | 1/5 | 1/5 | 1/5 | 4 | 25.0 | 0.1004 | 857 |
| `no-fault` | no_fault / none | 5 | 0/5 | 0/5 | 0/5 | 4 | 24.0 | 0.0944 | 771 |

## 3. 总体准确率

| 范围 | 有效运行 | 分类正确 | 组件正确 | 两者都对 |
|---|---|---|---|---|
| 全部场景 | 55 | 42/55 (76%) | 42/55 (76%) | 42/55 (76%) |
| 仅 10 个故障场景（不含 `no-fault` 对照） | 50 | 42/50 (84%) | 42/50 (84%) | 42/50 (84%) |

## 4. 混淆矩阵（原因分类）

行是标准答案的原因分类，列是 agent 给出的分类；对角线上是答对的。

| 标准答案 \ agent 答案 | `(未提交诊断)` | `dependency_unavailable` | `load_saturation` | `misconfiguration_env` | `missing_secret_or_config` | `partition_skew` | `probe_misconfiguration` | `resource_limit_cpu` | `resource_limit_memory` | `transient_restart_no_action` | 合计 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `dependency_unavailable` | · | 15 | · | · | · | · | · | · | · | · | 15 |
| `load_saturation` | 4 | · | 1 | · | · | · | · | · | · | · | 5 |
| `misconfiguration_env` | · | · | · | 5 | · | · | · | · | · | · | 5 |
| `missing_secret_or_config` | · | · | · | · | 5 | · | · | · | · | · | 5 |
| `no_fault` | 4 | · | · | · | · | 1 | · | · | · | · | 5 |
| `probe_misconfiguration` | · | · | · | · | · | · | 5 | · | · | · | 5 |
| `resource_limit_cpu` | 3 | · | · | · | · | · | · | 2 | · | · | 5 |
| `resource_limit_memory` | · | · | · | · | · | · | · | · | 5 | · | 5 |
| `transient_restart_no_action` | 1 | · | · | · | · | · | · | · | · | 4 | 5 |

## 5. 稳定性

同一个场景的多次有效运行给出的（分类 / 组件）是否一致。

- `redis-down`: **一致**（5 次都是 dependency_unavailable / redis）
- `bad-kafka-bootstrap`: **一致**（5 次都是 misconfiguration_env / stream-processor）
- `missing-pg-secret`: **一致**（5 次都是 missing_secret_or_config / query-service）
- `sp-pod-killed`: **不一致**
  - 20261025T053120Z-sp-pod-killed-deepseek-flash: transient_restart_no_action / stream-processor
  - 20261025T085353Z-sp-pod-killed-deepseek-flash: transient_restart_no_action / stream-processor
  - 20261025T112700Z-sp-pod-killed-deepseek-flash: transient_restart_no_action / stream-processor
  - 20261025T143729Z-sp-pod-killed-deepseek-flash: transient_restart_no_action / stream-processor
  - 20261025T182245Z-sp-pod-killed-deepseek-flash: (未提交诊断) / -
- `sp-cpu-throttle`: **不一致**
  - 20261025T054438Z-sp-cpu-throttle-deepseek-flash: (未提交诊断) / -
  - 20261025T083824Z-sp-cpu-throttle-deepseek-flash: (未提交诊断) / -
  - 20261025T121816Z-sp-cpu-throttle-deepseek-flash: resource_limit_cpu / stream-processor
  - 20261025T124728Z-sp-cpu-throttle-deepseek-flash: (未提交诊断) / -
  - 20261025T161212Z-sp-cpu-throttle-deepseek-flash: resource_limit_cpu / stream-processor
- `sp-oom`: **一致**（5 次都是 resource_limit_memory / stream-processor）
- `kafka-down`: **一致**（5 次都是 dependency_unavailable / kafka）
- `postgres-down`: **一致**（5 次都是 dependency_unavailable / postgresql）
- `query-probe-misconfig`: **一致**（5 次都是 probe_misconfiguration / query-service）
- `ingestion-overload`: **不一致**
  - 20261025T070358Z-ingestion-overload-deepseek-flash: (未提交诊断) / -
  - 20261025T100155Z-ingestion-overload-deepseek-flash: (未提交诊断) / -
  - 20261025T103057Z-ingestion-overload-deepseek-flash: load_saturation / ingestion-service
  - 20261025T134427Z-ingestion-overload-deepseek-flash: (未提交诊断) / -
  - 20261025T155821Z-ingestion-overload-deepseek-flash: (未提交诊断) / -
- `no-fault`: **不一致**
  - 20261025T051834Z-no-fault-deepseek-flash: (未提交诊断) / -
  - 20261025T074456Z-no-fault-deepseek-flash: (未提交诊断) / -
  - 20261025T115205Z-no-fault-deepseek-flash: (未提交诊断) / -
  - 20261025T142456Z-no-fault-deepseek-flash: partition_skew / ingestion-service
  - 20261025T191647Z-no-fault-deepseek-flash: (未提交诊断) / -

不一致的场景数: 4/11

## 6. 答错的运行

用 `bench show <目录>` 查看全过程。

| 运行目录 | 场景 | 标准答案 | agent 的答案 |
|---|---|---|---|
| `runs/20261025T051834Z-no-fault-deepseek-flash` | `no-fault` | no_fault / none | (未提交诊断) / - |
| `runs/20261025T054438Z-sp-cpu-throttle-deepseek-flash` | `sp-cpu-throttle` | resource_limit_cpu / stream-processor | (未提交诊断) / - |
| `runs/20261025T070358Z-ingestion-overload-deepseek-flash` | `ingestion-overload` | load_saturation / ingestion-service | (未提交诊断) / - |
| `runs/20261025T074456Z-no-fault-deepseek-flash` | `no-fault` | no_fault / none | (未提交诊断) / - |
| `runs/20261025T083824Z-sp-cpu-throttle-deepseek-flash` | `sp-cpu-throttle` | resource_limit_cpu / stream-processor | (未提交诊断) / - |
| `runs/20261025T100155Z-ingestion-overload-deepseek-flash` | `ingestion-overload` | load_saturation / ingestion-service | (未提交诊断) / - |
| `runs/20261025T115205Z-no-fault-deepseek-flash` | `no-fault` | no_fault / none | (未提交诊断) / - |
| `runs/20261025T124728Z-sp-cpu-throttle-deepseek-flash` | `sp-cpu-throttle` | resource_limit_cpu / stream-processor | (未提交诊断) / - |
| `runs/20261025T134427Z-ingestion-overload-deepseek-flash` | `ingestion-overload` | load_saturation / ingestion-service | (未提交诊断) / - |
| `runs/20261025T142456Z-no-fault-deepseek-flash` | `no-fault` | no_fault / none | partition_skew / ingestion-service |
| `runs/20261025T155821Z-ingestion-overload-deepseek-flash` | `ingestion-overload` | load_saturation / ingestion-service | (未提交诊断) / - |
| `runs/20261025T182245Z-sp-pod-killed-deepseek-flash` | `sp-pod-killed` | transient_restart_no_action / stream-processor | (未提交诊断) / - |
| `runs/20261025T191647Z-no-fault-deepseek-flash` | `no-fault` | no_fault / none | (未提交诊断) / - |

## 7. 抽查：随机抽取的答对的运行

请用 `bench show` 检查这些答对的运行是不是真的基于正确的证据，而不是碰巧。

- `runs/20261025T112700Z-sp-pod-killed-deepseek-flash`（`sp-pod-killed`）
- `runs/20261025T141146Z-redis-down-deepseek-flash`（`redis-down`）
- `runs/20261025T105912Z-sp-oom-deepseek-flash`（`sp-oom`）
- `runs/20261025T192926Z-query-probe-misconfig-deepseek-flash`（`query-probe-misconfig`）
- `runs/20261025T131620Z-kafka-down-deepseek-flash`（`kafka-down`）

## 8. 被排除的运行（不计入准确率）

（没有）

