# ADR 003: Cause categories and scoring rules

> English version first, Chinese version below.

## Status

Accepted for stage 2. The taxonomy is the stage-1 draft, confirmed by the repository owner and
**frozen for this stage**: categories are not added, removed or renamed while results are being collected.

## Context

To compare models we need a number per run that means the same thing every time. The agent submits a
structured diagnosis (`cause_category`, `component`, `evidence`, `recommended_action`, `confidence`)
and we must decide, without a human in the loop, whether it was right.

## Decision

1. **A fixed taxonomy of 12 cause categories and 7 components** (`src/bench/agent/diagnosis.py`),
   enforced in the `submit_diagnosis` schema and re-validated in code (the compatibility endpoint does not
   have to honour the schema; a submission with an unknown value is rejected and the model may retry).
   `kafka_topic_config` and `partition_skew` have no scenario in stage 2; they stay as **distractors**, so
   picking them is a measurable mistake.
2. **Exact comparison, no partial credit.** `cause_category` and `component` are each compared for
   equality with the ground truth (`src/bench/scoring.py`). A run is *fully correct* only when both match.
   Reports give three numbers: category correct, component correct, both correct.
3. **No fault means a real answer.** The control scenario's ground truth is `no_fault` / `none`. Not
   submitting a diagnosis scores false/false: giving up is not the same as being right.
4. **`recommended_action` and `evidence` are stored but not scored** in stage 2. They are read by a human
   (the report lists wrong runs, and a seeded random sample of correct ones, to be checked with `bench show`).
5. **No LLM as a judge.** Scoring is a pure function of the stored answer and the ground truth.

## Why exact comparison and not an LLM judge

- **Reproducible and auditable.** The same transcript always scores the same; the repository owner can
  recompute every number by hand. An LLM judge adds a second model whose own errors, biases and version
  changes would be mixed into the result.
- **No circularity.** We are measuring how well a model diagnoses; using a model to grade the answer
  makes the measurement depend on the thing measured.
- **Cheap.** No extra calls, no extra budget.
- **The cost of strictness is accepted knowingly:** a diagnosis that is *almost* right (for example the right
  component with a neighbouring category) counts as wrong in the category column. The confusion matrix and
  the per-run listing show *how* it was wrong, so near-misses are visible without being rewarded.

## Known limits

- Several scenarios share a category (`dependency_unavailable` for redis, kafka and postgresql); the
  component column is what tells them apart. This is why both columns are reported.
- A symptom can sometimes be explained by two categories. Ground truths were not adjusted to the model's
  answers; where a scenario looked ambiguous it is reported to the repository owner instead.
- `transient_restart_no_action` is the correct answer for a pod that was simply killed and is coming
  back; an agent that proposes a fix there is wrong even if its fix is harmless.

---

# ADR 003：原因分类与打分规则

## 状态

第 2 阶段采纳。分类沿用第 1 阶段的草稿，经仓库所有者确认，**本阶段内固定**：收集结果期间不增、不删、不改名。

## 背景

要比较不同模型，每次运行必须得到一个意义始终一致的数字。agent 提交结构化诊断（`cause_category`、
`component`、`evidence`、`recommended_action`、`confidence`），我们需要在没有人参与的情况下判断它对不对。

## 决定

1. **固定的分类：12 个原因分类、7 个组件**（`src/bench/agent/diagnosis.py`），既写在 `submit_diagnosis`
   的 schema 里，也在代码里重新校验（兼容端点不一定遵守 schema；取值不在枚举里的提交会被拒绝，模型可以重试）。
   `kafka_topic_config` 和 `partition_skew` 本阶段没有对应的场景，保留作为**干扰项**：选到它们是可以被度量的错误。
2. **精确比较，不给部分分。** `cause_category` 和 `component` 分别与标准答案比较是否相等
   （`src/bench/scoring.py`），两个都对才算“完全正确”。报告里给三个数字：分类对、组件对、都对。
3. **“没有故障”是一个需要明确给出的答案。** 对照场景的标准答案是 `no_fault` / `none`。
   没有提交诊断记为 false/false：放弃不等于答对。
4. **`recommended_action` 和 `evidence` 本阶段只保存、不打分。** 由人来读（报告会列出所有答错的运行，
   以及用种子随机抽取的答对的运行，用 `bench show` 抽查）。
5. **不用 LLM 当裁判。** 打分是“保存下来的答案”和“标准答案”的纯函数。

## 为什么用精确比较，而不是 LLM 裁判

- **可复现、可审计。** 同一份记录永远得到同一个分数，仓库所有者可以手算每一个数字。LLM 裁判会引入第二个模型，
  它自己的错误、偏好和版本变化都会混进结果里。
- **没有循环论证。** 我们测的是模型诊断得好不好；用模型来批改答案，会让测量依赖被测量的东西。
- **便宜。** 没有额外调用，没有额外预算。
- **严格的代价是有意接受的：** “差一点就对”的诊断（比如组件对、分类选了相邻的）在分类那一列算错。
  混淆矩阵和逐条列出的运行会显示它**错在哪里**，所以近似答对看得见，但不会被奖励。

## 已知的局限

- 几个场景共用一个分类（redis、kafka、postgresql 都是 `dependency_unavailable`），要靠组件那一列区分，
  所以两列都要报告。
- 同一个症状有时可以用两个分类解释。标准答案**没有**根据模型的回答去调整；遇到有歧义的场景，写给仓库所有者看。
- `transient_restart_no_action` 是“Pod 被删除、正在自己恢复”的正确答案；在这种场景里提出修复方案就是答错，
  即使这个方案无害。
