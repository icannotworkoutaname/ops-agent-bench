# ops-agent-bench

> English first, Chinese below.

Inject a known fault into the music-streaming system running on minikube, let an LLM agent
diagnose it with read-only tools, and score the result with fixed rules. The goal is to compare
diagnosis accuracy, out-of-bounds attempts and cost across models and levels of autonomy.

Stage 1 delivers one complete, recorded chain: pick a scenario -> check the environment is healthy
-> inject -> confirm the symptom -> agent investigates and submits a structured diagnosis ->
score -> restore -> confirm healthy again. It does **not** try to make the agent accurate.

## Quick start (inside WSL)

```bash
uv sync
cp .env.example .env          # then put DEEPSEEK_API_KEY=... in .env (never commit it)
uv run bench health           # every component + traffic + control plane must be OK
uv run bench traffic up       # deploy the in-cluster traffic generator (once)
uv run bench list             # the three scenarios
uv run bench run redis-down --fake      # whole chain with a scripted model: no network, free
uv run bench run redis-down             # real model; refuses if prices/budget are missing,
                                        # the run cap/budget is reached, or during peak hours
uv run bench show runs/<run-dir>        # read a whole run step by step (--full: no truncation)
uv run pytest                           # unit tests
uv run pytest -m integration -s         # needs the cluster; ~13 min, injects and restores faults
```

Other commands: `bench inject <scenario>`, `bench restore <scenario>`, `bench traffic down|stats`.

## Layout

| Path | What it is |
|---|---|
| `config/env.yaml`, `config/run.yaml` | resource names; limits, thresholds, prices, budget, peak hours |
| `src/bench/admin_k8s.py`, `health.py`, `scenarios/`, `traffic/` | the **write side**: inject, restore, health |
| `src/bench/tools/` | the **agent side**: read-only MCP tools. Must never import `admin_k8s` |
| `src/bench/agent/` | hand-written loop, prompts, `submit_diagnosis`, model clients |
| `runs/` | one directory per run, never overwritten, not in git |
| `docs/` | stage plan, ADRs, walkthrough |

Read `CLAUDE.md` for the working conventions and isolation principles, and
`docs/walkthrough-stage-1.md` for an explanation of how everything fits together.

## Safety notes

- The harness only talks to the `minikube` kube-context. It never modifies the music-streaming repository.
- Real runs are capped (15 runs, a cost budget) and refused during DeepSeek peak hours unless `--allow-peak`.
- The tools are read-only **by code**, not by RBAC yet; see `docs/adr/002-tool-permissions-and-pods-exec.md`.

---

# ops-agent-bench（中文）

在 minikube 上的 music-streaming 系统里注入已知故障，让 LLM agent 用只读工具诊断，再用固定规则打分。
目标是比较不同模型、不同自主程度下的诊断准确率、越权尝试和成本。

第 1 阶段交付一条完整且有记录的链路：选场景 → 确认环境健康 → 注入 → 确认症状 → agent 调查并提交结构化诊断
→ 打分 → 恢复 → 再次确认健康。**不**追求 agent 诊断得准。

## 快速开始（在 WSL 里）

```bash
uv sync
cp .env.example .env          # 然后在 .env 里填 DEEPSEEK_API_KEY=...（绝不要提交）
uv run bench health           # 各组件、流量、控制面都必须 OK
uv run bench traffic up       # 部署集群内流量发生器（只需一次）
uv run bench list             # 三个场景
uv run bench run redis-down --fake      # 用假模型跑完整链路：不联网、不花钱
uv run bench run redis-down             # 真实模型；价格 / 预算没填、次数或预算到上限、高峰时段都会拒绝
uv run bench show runs/<运行目录>       # 逐步查看一次运行的全过程（--full 不截断）
uv run pytest                           # 单元测试
uv run pytest -m integration -s         # 需要集群；约 13 分钟，会真的注入并恢复故障
```

其他命令：`bench inject <场景>`、`bench restore <场景>`、`bench traffic down|stats`。

## 目录

| 路径 | 说明 |
|---|---|
| `config/env.yaml`、`config/run.yaml` | 资源名；上限、阈值、价格、预算、高峰时段 |
| `src/bench/admin_k8s.py`、`health.py`、`scenarios/`、`traffic/` | **写权限一侧**：注入、恢复、健康检查 |
| `src/bench/tools/` | **agent 一侧**：只读 MCP 工具，绝不能 import `admin_k8s` |
| `src/bench/agent/` | 手写循环、提示词、`submit_diagnosis`、模型客户端 |
| `runs/` | 每次运行一个目录，从不覆盖，不进 git |
| `docs/` | 阶段计划、ADR、讲解文档 |

工作约定和隔离原则见 `CLAUDE.md`，整体怎么运转见 `docs/walkthrough-stage-1.md`。

## 安全说明

- 测试框架只连 `minikube` 这个 kube-context，不修改 music-streaming 仓库。
- 真实运行有次数（15 次）和费用预算上限；DeepSeek 高峰时段拒绝运行，除非加 `--allow-peak`。
- 工具的“只读”目前靠代码保证，还不是靠 RBAC；见 `docs/adr/002-tool-permissions-and-pods-exec.md`。
