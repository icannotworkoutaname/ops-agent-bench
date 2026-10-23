"""费用核算和高峰时段判断。

DeepSeek 按时段分高峰 / 非高峰两档价格（非高峰是高峰的一半），每档里又分
“缓存命中的输入 / 未命中的输入 / 输出”三种单价。所有数字都写在 config/run.yaml 里，
这里只做计算，不内置任何价格。

按“每次请求的发出时间”判断那次请求落在哪一档，所以一次运行里可以跨越高峰边界。
"""

from datetime import UTC, datetime, time

PRICE_KEYS = ("input_cache_miss", "input_cache_hit", "output")


def _parse_hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def is_peak(when: datetime, peak_cfg: dict) -> bool:
    """when 是否落在高峰时段（按 UTC 判断；窗口左闭右开）。

    写成独立函数：第 2 阶段的批量运行器会复用它，批次进行中进入高峰时段时，
    跑完当前这一次就暂停，等高峰结束再继续。
    """
    when = when.astimezone(UTC)
    if peak_cfg.get("weekdays_only", True) and when.weekday() >= 5:
        return False
    now = when.time()
    return any(
        _parse_hhmm(start) <= now < _parse_hhmm(end) for start, end in peak_cfg["windows_utc"]
    )


def pricing_missing(billing: dict, model_entry: dict) -> list[str]:
    """列出还没填的价格项。非空就不能做真实运行（没有价格就没法控制预算）。"""
    missing = []
    for key in ("currency", "budget"):
        if billing.get(key) is None:
            missing.append(f"billing.{key}")
    pricing = model_entry.get("pricing") or {}
    for tier in ("peak", "offpeak"):
        for key in PRICE_KEYS:
            if (pricing.get(tier) or {}).get(key) is None:
                missing.append(f"pricing.{tier}.{key}")
    return missing


def request_cost(usage: dict, peak: bool, pricing: dict) -> float:
    """一次请求的费用，单价是“每百万 token”。

    实测 DeepSeek 返回的 input_tokens 只是未命中缓存的那部分，命中的部分单独在
    cache_read_input_tokens 里，所以两者分别按各自单价计，不能重复计算。
    """
    tier = pricing["peak" if peak else "offpeak"]
    miss = usage.get("input_tokens") or 0
    hit = usage.get("cache_read_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    return (
        miss * tier["input_cache_miss"] + hit * tier["input_cache_hit"] + out * tier["output"]
    ) / 1_000_000


def build_usage_report(
    events: list[dict], model_entry: dict, billing: dict, fake: bool, stage: int = 1
) -> dict:
    """把一次运行的事件整理成 usage.json：每步的原始 usage + 时段 + 费用，以及总计。

    假模型的运行不计费（cost 为 None），也不影响累计费用。
    """
    sent_at: dict[int, str] = {}
    steps = []
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    total_cost = 0.0
    pricing = None if fake else model_entry.get("pricing")
    for e in events:
        if e["type"] == "request":
            sent_at[e["step"]] = e["sent_at"]
        elif e["type"] == "response":
            usage = e["usage"]
            for k in totals:
                totals[k] += usage.get(k) or 0
            peak = is_peak(datetime.fromisoformat(sent_at[e["step"]]), billing["peak"])
            cost = request_cost(usage, peak, pricing) if pricing else None
            if cost is not None:
                total_cost += cost
            steps.append(
                {
                    "step": e["step"],
                    "sent_at": sent_at[e["step"]],
                    "peak": peak,
                    "model": e.get("model"),
                    "usage": usage,  # 原始字段，原样保存
                    "cost": cost,
                }
            )
    return {
        "stage": stage,
        "fake": fake,
        "currency": None if fake else billing.get("currency"),
        "steps": steps,
        "total": {**totals, "cost": None if fake else round(total_cost, 6)},
    }
