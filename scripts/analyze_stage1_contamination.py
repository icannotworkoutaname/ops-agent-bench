"""检查第 1 阶段的真实运行有没有受到跨运行污染（stage-2.md 第 1 节第 3 条）。

对 runs/ 里的每次运行（含假模型的，因为它们同样会注入故障、留下痕迹）：
  - 按时间顺序排列，算出和上一次运行结束的间隔、上一次是哪个场景；
  - 用 bench.contamination 扫描所有工具返回的内容，列出命中的“其他场景的特征字符串”等；
  - 另外统计 "Unauthorized" 之类与场景无关的环境噪音。

只分析、不修改任何东西。用法: uv run python scripts/analyze_stage1_contamination.py
"""

import json
from datetime import datetime

from bench.contamination import find_contamination
from bench.runlog import RUNS_DIR, read_transcript
from bench.scenarios import SCENARIOS

SIGNATURES = {sid: cls.signatures for sid, cls in SCENARIOS.items()}
NOISE = ("Unauthorized", "token has expired")


def main() -> None:
    runs = []
    for d in sorted(RUNS_DIR.iterdir()):
        meta = json.loads((d / "meta.json").read_text())
        runs.append((datetime.fromisoformat(meta["started_at"]), d, meta))
    runs.sort(key=lambda r: r[0])

    previous = None
    for started, d, meta in runs:
        calls = [e for e in read_transcript(d) if e["type"] == "tool_call"]
        hits = find_contamination(calls, meta["scenario"], SIGNATURES)
        noise = [c for c in calls if any(n in c["result"] for n in NOISE)]
        if previous:
            gap = (started - datetime.fromisoformat(previous["finished_at"])).total_seconds() / 60
            prev_text = f"上一次 {previous['scenario']}（{'假' if previous['fake'] else '真'}），间隔 {gap:.1f} 分钟"
        else:
            prev_text = "这是第一次运行"
        print("=" * 90)
        print(f"{d.name}   {'[假模型]' if meta['fake'] else '[真实]'}")
        print(f"  {prev_text}")
        print(
            f"  工具调用 {len(calls)} 次；污染命中 {len(hits)} 条；环境噪音（认证错误）出现在 {len(noise)} 个工具结果里"
        )
        for h in hits:
            print(
                f"    - 第 {h['step']} 步 {h['tool']}({h['input']}) 命中 [{h['kind']}] "
                f"{h['needle']!r}（来自 {h['from_scenario']}）"
            )
            print(f"        …{h['excerpt'].replace(chr(10), ' ')[:200]}…")
        previous = meta


if __name__ == "__main__":
    main()
