"""no-fault：无故障对照。什么都不注入。

作用：检验 agent 会不会在没有问题的时候“编造”一个问题。标准答案是 no_fault / none。
它同样要经过冷却期，运行前后同样检查环境健康，只是 inject 什么也不做。
"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario


class NoFault(Scenario):
    id = "no-fault"
    ground_truth = GroundTruth(
        cause_category="no_fault",
        component="none",
        expected_action="无需处理，系统正常",
    )
    # 对照场景自己没有症状，所以没有特征字符串：任何其他场景的痕迹出现在这里都算污染
    signatures = ()

    def inject(self) -> None:
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        # 没有症状可确认；冷却期已经保证了环境是健康的
        return Outcome(True, "无故障对照：不注入任何东西")

    def restore(self) -> None:
        return None
