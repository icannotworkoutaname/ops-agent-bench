"""第 1 阶段的打分：最简单的确定性规则。

只看两件事：cause_category 和 component 是否分别和标准答案一致。输出两个布尔值，
不加权、不用 LLM 判断，这里不做任何“差不多算对”的宽松处理。
"""

from bench.scenarios.base import GroundTruth


def score(diagnosis: dict | None, truth: GroundTruth) -> dict:
    """没有提交诊断（diagnosis 为 None）时两项都是 False。"""
    if diagnosis is None:
        return {"cause_category_correct": False, "component_correct": False}
    return {
        "cause_category_correct": diagnosis.get("cause_category") == truth.cause_category,
        "component_correct": diagnosis.get("component") == truth.component,
    }
