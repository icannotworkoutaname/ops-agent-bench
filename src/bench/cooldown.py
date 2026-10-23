"""冷却期：注入前，环境必须连续 W 分钟保持健康。

为什么需要：回看窗口只能让 agent 看不到 W 分钟之前的数据，但如果上一次故障刚恢复就开始下一次，
窗口里仍然会有上一次的痕迹。要求注入前“连续 W 分钟全部健康”之后，窗口回看到的最早时刻
也已经是恢复之后了，agent 能看到的要么是健康的基线，要么是当前这次的故障。

代价：每次运行前至少多等 W 分钟（默认 10 分钟）。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class CooldownResult:
    ok: bool
    waited_seconds: float
    checks: int
    # 连续健康被打断过几次，以及每次的原因，写进 env.json 方便事后查看
    resets: list[dict] = field(default_factory=list)
    healthy: bool = False
    last_checks: list[dict] = field(default_factory=list)
    detail: str = ""


def wait_for_cooldown(
    check_health: Callable[[], tuple[bool, list[dict]]],
    window_seconds: float,
    interval_seconds: float,
    max_wait_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[str], None] | None = None,
) -> CooldownResult:
    """按固定间隔反复做完整的健康检查，直到连续健康满 window_seconds。

    任何一次检查不健康，连续计时就清零重来。总共等了 max_wait_seconds 还没满足就放弃：
    环境一直不稳定时，让这次运行变成“无效”，而不是无限期等下去。
    """
    start = clock()
    streak_start: float | None = None
    checks_done = 0
    resets: list[dict] = []

    def say(text: str) -> None:
        if on_progress:
            on_progress(text)

    while True:
        healthy, checks = check_health()
        checks_done += 1
        now = clock()

        if healthy:
            if streak_start is None:
                streak_start = now
            streak = now - streak_start
            say(f"冷却中: 已连续健康 {streak:.0f}s / {window_seconds:.0f}s")
            if streak >= window_seconds:
                return CooldownResult(
                    ok=True,
                    waited_seconds=now - start,
                    checks=checks_done,
                    resets=resets,
                    healthy=True,
                    last_checks=checks,
                    detail=f"连续健康 {streak:.0f}s，共检查 {checks_done} 次，打断 {len(resets)} 次",
                )
        else:
            bad = "；".join(f"{c['name']}: {c['detail']}" for c in checks if not c["ok"])
            if streak_start is not None or not resets:
                resets.append({"at_seconds": round(now - start, 1), "reason": bad})
            streak_start = None
            say(f"冷却中: 环境不健康，重新计时 ({bad[:120]})")

        if now - start >= max_wait_seconds:
            return CooldownResult(
                ok=False,
                waited_seconds=now - start,
                checks=checks_done,
                resets=resets,
                healthy=healthy,
                last_checks=checks,
                detail=(
                    f"等了 {now - start:.0f}s 仍没有连续健康满 {window_seconds:.0f}s，"
                    f"共检查 {checks_done} 次，打断 {len(resets)} 次"
                ),
            )
        sleep(interval_seconds)
