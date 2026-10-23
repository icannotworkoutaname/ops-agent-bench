"""批量运行的结果报告（bench batch report）。

完全由代码生成，不用 LLM 总结：每个数字都能从 runs/ 下的记录重新算出来。
只统计“有效”运行；无效运行（症状没出现、疑似污染、接口出错）在最后单独列出，不计入准确率。

“完全正确”= 原因分类和组件两个都和标准答案精确一致（规则见 scoring.py）。
"""

import json
import random
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from bench.batch import BatchStore

NO_SUBMISSION = "(未提交诊断)"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_run(attempt: dict) -> dict:
    """把一次尝试对应的运行目录读成一个字典。目录丢了也不报错，只是缺字段。"""
    d = Path(attempt["run_dir"])
    meta, usage = _read(d / "meta.json") or {}, _read(d / "usage.json") or {}
    diag, score = _read(d / "diagnosis.json") or {}, _read(d / "score.json") or {}
    env = _read(d / "env.json") or {}
    answer = diag.get("diagnosis") if diag.get("submitted") else None
    started, finished = meta.get("started_at"), meta.get("finished_at")
    seconds = (
        (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
        if started and finished
        else None
    )
    return {
        **attempt,
        "dir": d,
        "answer_category": answer["cause_category"] if answer else None,
        "answer_component": answer["component"] if answer else None,
        "category_correct": score.get("cause_category_correct"),
        "component_correct": score.get("component_correct"),
        "steps": len(usage.get("steps", [])),
        "cost": (usage.get("total") or {}).get("cost") or 0.0,
        "seconds": seconds,
        "models": usage.get("models_seen", []),
        "contamination": env.get("contamination") or [],
        "started": started,
        "finished": finished,
    }


def _pct(n: int, total: int) -> str:
    return f"{n}/{total} ({n / total:.0%})" if total else "0/0"


def _avg(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _fmt(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def build_report(store: BatchStore, truths: dict[str, tuple[str, str]]) -> str:
    plan = store.plan
    runs = [load_run(a) for a in store.attempts()]
    valid = [r for r in runs if r["valid"]]
    invalid = [r for r in runs if not r["valid"]]
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for r in valid:
        by_scenario[r["scenario"]].append(r)

    out: list[str] = [f"# 第 2 阶段结果（批次 {plan['batch_id']}）", ""]
    out += _section_info(store, plan, runs, valid, invalid)
    out += _section_per_scenario(plan, truths, by_scenario)
    out += _section_overall(truths, valid)
    out += _section_confusion(truths, valid)
    out += _section_stability(plan, by_scenario)
    out += _section_wrong(truths, valid)
    out += _section_sample(plan, valid)
    out += _section_excluded(invalid)
    return "\n".join(out) + "\n"


def _section_info(store, plan, runs, valid, invalid) -> list[str]:
    reasons = Counter()
    for r in invalid:
        if r.get("contaminated"):
            reasons["疑似污染"] += 1
        elif r["status"] == "invalid":
            reasons["症状没出现 / 环境不满足条件"] += 1
        elif r["status"] == "error":
            reasons["接口或基础设施出错"] += 1
        else:
            reasons["其他"] += 1
    events = store.events()
    pauses = [e for e in events if e["type"] == "pause"]
    starts = [r["started"] for r in runs if r["started"]]
    ends = [r["finished"] for r in runs if r["finished"]]
    models = sorted({m for r in runs for m in r["models"]})
    hashes = plan.get("input_hashes", {})
    lines = [
        "## 1. 批次信息",
        "",
        f"- 模型: `{plan['model_key']}`；响应里的实际模型名: {', '.join(models) or '（无）'}",
        "- 固定输入的哈希（整个批次不变）: "
        + "，".join(f"{k} `{v[:12]}`" for k, v in hashes.items()),
        (
            f"- 运行顺序的随机种子: `{plan['seed']}`；计划 {len(plan['order'])} 次"
            f"（{len(plan['scenarios'])} 个场景 × {plan['repeats']} 次）"
        ),
        f"- 实际尝试 {len(runs)} 次：有效 {len(valid)}，无效 {len(invalid)}"
        + (f"（{'；'.join(f'{k} {v}' for k, v in reasons.items())}）" if reasons else ""),
        (
            f"- 时间范围: {min(starts) if starts else '-'} → {max(ends) if ends else '-'}；"
            f"因高峰时段暂停 {len(pauses)} 次"
        ),
        (
            f"- 总费用: {sum(r['cost'] for r in runs):.4f}"
            f"（有效运行 {sum(r['cost'] for r in valid):.4f}，"
            f"无效运行 {sum(r['cost'] for r in invalid):.4f}）"
        ),
        "",
    ]
    return lines


def _section_per_scenario(plan, truths, by_scenario) -> list[str]:
    lines = [
        "## 2. 每个场景（只含有效运行）",
        "",
        "| 场景 | 标准答案 | 有效次数 | 分类正确 | 组件正确 | 两者都对 | 未提交 | 平均步数 | 平均费用 | 平均耗时(s) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for sid in plan["scenarios"]:
        rs = by_scenario.get(sid, [])
        n = len(rs)
        cat = sum(1 for r in rs if r["category_correct"])
        comp = sum(1 for r in rs if r["component_correct"])
        both = sum(1 for r in rs if r["category_correct"] and r["component_correct"])
        none = sum(1 for r in rs if r["answer_category"] is None)
        truth = f"{truths[sid][0]} / {truths[sid][1]}"
        lines.append(
            f"| `{sid}` | {truth} | {n} | {cat}/{n} | {comp}/{n} | {both}/{n} | {none} | "
            f"{_fmt(_avg([r['steps'] for r in rs]))} | {_fmt(_avg([r['cost'] for r in rs]), 4)} | "
            f"{_fmt(_avg([r['seconds'] for r in rs]), 0)} |"
        )
    return [*lines, ""]


def _section_overall(truths, valid) -> list[str]:
    def rate(rs):
        n = len(rs)
        return (
            _pct(sum(1 for r in rs if r["category_correct"]), n),
            _pct(sum(1 for r in rs if r["component_correct"]), n),
            _pct(sum(1 for r in rs if r["category_correct"] and r["component_correct"]), n),
        )

    faults = [r for r in valid if r["scenario"] != "no-fault"]
    lines = [
        "## 3. 总体准确率",
        "",
        "| 范围 | 有效运行 | 分类正确 | 组件正确 | 两者都对 |",
        "|---|---|---|---|---|",
        f"| 全部场景 | {len(valid)} | " + " | ".join(rate(valid)) + " |",
        f"| 仅 10 个故障场景（不含 `no-fault` 对照） | {len(faults)} | "
        + " | ".join(rate(faults))
        + " |",
        "",
    ]
    return lines


def _section_confusion(truths, valid) -> list[str]:
    cats = sorted({truths[r["scenario"]][0] for r in valid})
    answers = sorted({r["answer_category"] or NO_SUBMISSION for r in valid})
    matrix: dict[str, Counter] = defaultdict(Counter)
    for r in valid:
        matrix[truths[r["scenario"]][0]][r["answer_category"] or NO_SUBMISSION] += 1
    lines = [
        "## 4. 混淆矩阵（原因分类）",
        "",
        "行是标准答案的原因分类，列是 agent 给出的分类；对角线上是答对的。",
        "",
        "| 标准答案 \\ agent 答案 | " + " | ".join(f"`{a}`" for a in answers) + " | 合计 |",
        "|---" * (len(answers) + 2) + "|",
    ]
    for c in cats:
        row = [str(matrix[c][a]) if matrix[c][a] else "·" for a in answers]
        lines.append(f"| `{c}` | " + " | ".join(row) + f" | {sum(matrix[c].values())} |")
    return [*lines, ""]


def _section_stability(plan, by_scenario) -> list[str]:
    lines = ["## 5. 稳定性", "", "同一个场景的多次有效运行给出的（分类 / 组件）是否一致。", ""]
    inconsistent = []
    for sid in plan["scenarios"]:
        rs = by_scenario.get(sid, [])
        answers = [
            (r["answer_category"] or NO_SUBMISSION, r["answer_component"] or "-") for r in rs
        ]
        if len(set(answers)) <= 1:
            shown = f"{answers[0][0]} / {answers[0][1]}" if answers else "（没有有效运行）"
            lines.append(f"- `{sid}`: **一致**（{len(rs)} 次都是 {shown}）")
        else:
            inconsistent.append(sid)
            lines.append(f"- `{sid}`: **不一致**")
            for r, (cat, comp) in zip(rs, answers, strict=True):
                lines.append(f"  - {r['dir'].name}: {cat} / {comp}")
    lines.append("")
    lines.append(f"不一致的场景数: {len(inconsistent)}/{len(plan['scenarios'])}")
    return [*lines, ""]


def _section_wrong(truths, valid) -> list[str]:
    wrong = [r for r in valid if not (r["category_correct"] and r["component_correct"])]
    lines = ["## 6. 答错的运行", ""]
    if not wrong:
        return [*lines, "（没有）", ""]
    lines += [
        "用 `bench show <目录>` 查看全过程。",
        "",
        "| 运行目录 | 场景 | 标准答案 | agent 的答案 |",
        "|---|---|---|---|",
    ]
    for r in wrong:
        t = truths[r["scenario"]]
        lines.append(
            f"| `{r['dir']}` | `{r['scenario']}` | {t[0]} / {t[1]} | "
            f"{r['answer_category'] or NO_SUBMISSION} / {r['answer_component'] or '-'} |"
        )
    return [*lines, ""]


def _section_sample(plan, valid) -> list[str]:
    correct = sorted(
        (r for r in valid if r["category_correct"] and r["component_correct"]),
        key=lambda r: r["dir"].name,
    )
    # 用批次种子抽样：同一个批次每次生成报告抽到的都是同样的 5 次，可复现
    picked = random.Random(f"sample-{plan['seed']}").sample(correct, min(5, len(correct)))
    lines = [
        "## 7. 抽查：随机抽取的答对的运行",
        "",
        "请用 `bench show` 检查这些答对的运行是不是真的基于正确的证据，而不是碰巧。",
        "",
    ]
    lines += [f"- `{r['dir']}`（`{r['scenario']}`）" for r in picked] or ["（没有答对的运行）"]
    return [*lines, ""]


def _section_excluded(invalid) -> list[str]:
    lines = ["## 8. 被排除的运行（不计入准确率）", ""]
    if not invalid:
        return [*lines, "（没有）", ""]
    for r in invalid:
        why = (
            "疑似污染: "
            + ", ".join(sorted({f"{h['kind']}:{h['needle']}" for h in r["contamination"]}))
            if r.get("contaminated")
            else r.get("reason", r["status"])
        )
        lines.append(f"- `{r['dir']}`（`{r['scenario']}`，{r['status']}）: {why}")
    return [*lines, ""]
