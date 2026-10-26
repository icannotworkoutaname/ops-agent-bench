"""场景稳定性测试：连续做“注入 → 确认症状 → 恢复 → 确认健康”若干轮，全程不调用模型。

stage-2 要求每个新场景都先过这一关：记录
  - verify_injected 成功了几次；
  - 从注入到症状出现用了多久；
  - 从开始恢复到恢复健康用了多久。
任何一轮没能稳定做到，都是需要报告，不做重试来掩盖。
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from bench.scenarios.base import Scenario


@dataclass
class Round:
    round: int
    symptom_ok: bool
    symptom_detail: str
    inject_to_symptom_seconds: float
    restore_ok: bool
    restore_detail: str
    restore_to_healthy_seconds: float


def run_stability(
    make_scenario: Callable[[], Scenario],
    rounds: int,
    run_cfg: dict,
    check_health: Callable[[], bool],
    on_progress: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[Round]:
    """每轮开始前环境必须是健康的，否则这一轮没有意义，直接停下来。恢复失败也立刻停下来。"""

    def say(text: str) -> None:
        if on_progress:
            on_progress(text)

    results: list[Round] = []
    sc_cfg = run_cfg["scenario"]
    for n in range(1, rounds + 1):
        if not check_health():
            say(f"第 {n} 轮开始前环境不健康，停止")
            break
        scenario = make_scenario()

        started = clock()
        scenario.inject()
        try:
            injected = scenario.verify_injected(sc_cfg["verify_injected_timeout"])
        finally:
            # 无论症状是否出现都要恢复，避免把环境留在坏的状态里
            symptom_seconds = clock() - started
            restore_started = clock()
            scenario.restore()
        restored = scenario.verify_restored(sc_cfg["verify_restored_timeout"])
        restore_seconds = clock() - restore_started

        results.append(
            Round(
                round=n,
                symptom_ok=injected.ok,
                symptom_detail=injected.detail,
                inject_to_symptom_seconds=round(symptom_seconds, 1),
                restore_ok=restored.ok,
                restore_detail=restored.detail,
                restore_to_healthy_seconds=round(restore_seconds, 1),
            )
        )
        say(
            f"第 {n} 轮: 症状{'出现' if injected.ok else '未出现'}（{symptom_seconds:.0f}s）；"
            f"恢复{'成功' if restored.ok else '失败'}（{restore_seconds:.0f}s）"
        )
        if not restored.ok:
            say("恢复失败：必须停止，先人工检查集群")
            break
    return results


def summarize(results: list[Round]) -> dict:
    """汇总成适合写进报告的几个数字。"""
    n = len(results)
    ok_symptoms = [r for r in results if r.symptom_ok]
    return {
        "rounds": n,
        "symptom_ok": len(ok_symptoms),
        "restore_ok": sum(1 for r in results if r.restore_ok),
        "avg_inject_to_symptom_seconds": (
            round(sum(r.inject_to_symptom_seconds for r in ok_symptoms) / len(ok_symptoms), 1)
            if ok_symptoms
            else None
        ),
        "avg_restore_to_healthy_seconds": (
            round(sum(r.restore_to_healthy_seconds for r in results) / n, 1) if n else None
        ),
        "details": [asdict(r) for r in results],
    }
