"""跨运行污染检查：agent 有没有在工具返回的内容里看到不该看到的东西。

为什么需要：一次运行结束、环境恢复后，上一个故障的痕迹（事件、没被重建的 Pod 的日志、
指标曲线）可能还留在环境里。agent 看到了，可能因此答错，也可能因此“碰巧”答对，
两种情况都会让结果不可信。回看窗口和冷却期是预防，这里是事后的自动检查：
扫描一次运行里所有工具返回的内容，发现可疑字符串就把这次运行标记为“疑似污染”。

检查三类东西：
  1. 任何场景 id（集群里不应该出现这些字符串，出现说明场景信息漏进了集群）；
  2. 测试框架自己的名字（注入器的 field manager、流量发生器、仓库名）；
  3. **其他**场景的特征字符串（本场景自己的症状当然会出现，不算污染）。
"""

# 测试框架留下的、agent 不该看到的名字
HARNESS_TERMS = ("ops-tool", "traffic-gen", "ops-agent-bench")


def find_contamination(
    tool_calls: list[dict], own_id: str, signatures: dict[str, tuple[str, ...]]
) -> list[dict]:
    """tool_calls 是 transcript 里的 tool_call 事件；signatures 是 {场景 id: 特征字符串}。

    返回命中列表，空列表表示干净。每条命中带上步骤、工具名、命中的字符串和一小段上下文，
    方便人工判断是不是误报。
    """
    scenario_ids = list(signatures)
    hits = []
    for call in tool_calls:
        if call["name"] == "submit_diagnosis":
            continue  # 这是模型自己写的内容，不是环境返回的
        text = call["result"]
        checks = [("scenario_id", None, s) for s in scenario_ids]
        checks += [("harness_term", None, t) for t in HARNESS_TERMS]
        for other_id, sigs in signatures.items():
            if other_id != own_id:
                checks += [("other_scenario_signature", other_id, s) for s in sigs]
        for kind, other_id, needle in checks:
            pos = text.find(needle)
            if pos >= 0:
                hits.append(
                    {
                        "step": call["step"],
                        "tool": call["name"],
                        "input": call["input"],
                        "kind": kind,
                        "from_scenario": other_id,
                        "needle": needle,
                        "excerpt": text[max(0, pos - 80) : pos + len(needle) + 80],
                    }
                )
    return hits
