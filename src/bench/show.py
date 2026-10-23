"""把一次运行的记录整理成人能读的文字（bench show）。

这是仓库所有者逐条检查 agent 每一步做了什么的主要入口：每一步依次显示
模型的思考、说的话、调用了哪些工具（参数）、工具返回了什么、花了多久，以及这一步的 token 用量。
"""

import json
from datetime import datetime
from pathlib import Path

from bench.runlog import read_transcript


def _cut(text: str, limit: int, full: bool) -> str:
    if full or len(text) <= limit:
        return text
    return text[:limit] + f"…（共 {len(text)} 字符，加 --full 查看全文）"


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines()) or prefix


def _read(run_dir: Path, name: str):
    path = run_dir / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _health_line(record: dict | None) -> str:
    if not record:
        return "（无）"
    bad = [c for c in record["checks"] if not c["ok"]]
    if record["healthy"]:
        return f"健康（{len(record['checks'])} 项全部通过）"
    return "不健康: " + "；".join(f"{c['name']}: {c['detail']}" for c in bad)


def render_run(run_dir: Path, full: bool = False) -> str:
    meta, env = _read(run_dir, "meta.json"), _read(run_dir, "env.json") or {}
    usage, diag, score = (
        _read(run_dir, "usage.json"),
        _read(run_dir, "diagnosis.json"),
        _read(run_dir, "score.json"),
    )
    events = read_transcript(run_dir)
    out: list[str] = []

    # ---------- 概览 ----------
    out.append(f"===== 运行 {run_dir.name} =====")
    if meta:
        out.append(
            f"场景: {meta['scenario']}    模型: {meta['model_key']} ({meta['model_name']})"
            + ("    【假模型，未联网】" if meta["fake"] else "")
        )
        out.append(f"状态: {meta['status']}    原因: {meta['reason']}")
        out.append(f"开始: {meta['started_at']}    结束: {meta['finished_at']}")
        git = meta["git"]
        out.append(
            f"代码版本: ops-agent-bench {git['ops_agent_bench']['commit'][:10]}"
            f"(dirty={git['ops_agent_bench']['dirty']}), "
            f"music-streaming {git['music_streaming']['commit'][:10]}"
            f"(dirty={git['music_streaming']['dirty']})"
        )

    # ---------- 环境 ----------
    out.append("\n----- 环境 -----")
    out.append(f"运行前健康检查: {_health_line(env.get('health_before'))}")
    if "events_cleared_in" in env:
        out.append(f"已清理旧事件的 namespace: {', '.join(env['events_cleared_in'])}")
    if "verify_injected" in env:
        v = env["verify_injected"]
        out.append(f"症状确认: {'已出现' if v['ok'] else '未出现'} — {v['detail']}")
    if "verify_restored" in env:
        v = env["verify_restored"]
        out.append(f"恢复: {'成功' if v['ok'] else '失败'} — {v['detail']}")
        out.append(f"恢复后健康检查: {_health_line(env.get('health_after'))}")
    timeline = env.get("timeline") or []
    if timeline:
        out.append("各阶段耗时（距上一个阶段）:")
        previous = None
        for item in timeline:
            at = datetime.fromisoformat(item["at"])
            gap = f"+{(at - previous).total_seconds():.0f}s" if previous else "起点"
            out.append(f"  {item['phase']:22} {gap}")
            previous = at

    # ---------- 过程 ----------
    setup = next((e for e in events if e["type"] == "setup"), None)
    if setup:
        names = ", ".join(t["name"] for t in setup["tools"])
        out.append("\n----- 任务 -----")
        out.append(f"工具: {names}")
        out.append(f"任务描述: {setup['task']}")
        out.append(
            f"（系统提示 {len(setup['system'])} 字符，完整内容在 transcript.jsonl 的 setup 事件里）"
        )

    per_step: dict[int, dict] = {}
    for e in events:
        if "step" in e and e["type"] in ("request", "response", "tool_call"):
            per_step.setdefault(e["step"], {"calls": {}})
            if e["type"] == "tool_call":
                per_step[e["step"]]["calls"][e["id"]] = e
            else:
                per_step[e["step"]][e["type"]] = e

    usage_by_step = {s["step"]: s for s in (usage or {}).get("steps", [])}
    for step in sorted(per_step):
        data = per_step[step]
        req, resp = data.get("request"), data.get("response")
        peak = usage_by_step.get(step, {}).get("peak")
        out.append(
            f"\n===== 第 {step} 步  发出于 {req['sent_at'] if req else '?'}"
            + (f"  [{'高峰' if peak else '非高峰'}]" if peak is not None else "")
            + " ====="
        )
        if not resp:
            out.append("（没有收到响应）")
            continue
        for block in resp["content"]:
            if block["type"] == "thinking":
                out.append("思考:\n" + _indent(_cut(block["thinking"], 800, full), "  "))
            elif block["type"] == "text":
                out.append("回复:\n" + _indent(_cut(block["text"], 800, full), "  "))
            elif block["type"] == "tool_use":
                call = data["calls"].get(block["id"])
                args = json.dumps(block["input"], ensure_ascii=False)
                timing = f"  ({call['duration_s']}s)" if call else "  (未执行)"
                out.append(f"调用工具: {block['name']} {args}{timing}")
                if call:
                    tag = "【出错】" if call["is_error"] else ""
                    out.append(
                        f"  结果{tag}:\n" + _indent(_cut(call["result"], 1200, full), "    ")
                    )
        u = resp["usage"]
        cost = usage_by_step.get(step, {}).get("cost")
        out.append(
            f"用量: 未缓存输入={u.get('input_tokens')} 缓存命中输入={u.get('cache_read_input_tokens')} "
            f"输出={u.get('output_tokens')}  stop_reason={resp['stop_reason']}  模型={resp['model']}"
            + (f"  费用={cost:.6f}" if cost is not None else "")
        )

    # ---------- 结果 ----------
    out.append("\n----- 最终诊断 -----")
    if diag and diag["submitted"]:
        d = diag["diagnosis"]
        out.append(
            f"原因分类: {d['cause_category']}    出问题的组件: {d['component']}    置信度: {d['confidence']}"
        )
        out.append(f"建议处理: {d['recommended_action']}")
        out.append("证据:")
        for ev in d["evidence"]:
            out.append(f"  - [{ev['source']}] {ev['observation']}")
    else:
        out.append(f"未提交诊断: {diag['reason'] if diag else '（无记录）'}")

    out.append("\n----- 评分 -----")
    if score and "cause_category_correct" in score:
        out.append(
            f"原因分类正确: {score['cause_category_correct']}    组件正确: {score['component_correct']}"
        )
    else:
        out.append("本次运行无效，未评分: " + (score or {}).get("reason", ""))

    if usage:
        t = usage["total"]
        cost_text = f"，费用 {t['cost']} {usage['currency']}" if t["cost"] is not None else ""
        out.append(
            f"\n----- 用量合计 -----\n未缓存输入={t['input_tokens']} 缓存命中输入={t['cache_read_input_tokens']} "
            f"输出={t['output_tokens']}{cost_text}"
        )
        if usage.get("models_seen"):
            out.append(f"响应里的实际模型名: {', '.join(usage['models_seen'])}")
    return "\n".join(out)
