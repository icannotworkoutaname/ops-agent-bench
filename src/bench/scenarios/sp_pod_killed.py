"""sp-pod-killed：删除 stream-processor 的 Pod。

这是一个“不需要处理”的场景：Deployment 会自动重建 Pod，Pod 就绪后 lag 自行回落。
标准答案是 transient_restart_no_action——考察 agent 能不能认出“这是短暂重启，别瞎折腾”。
"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class SpPodKilled(Scenario):
    id = "sp-pod-killed"
    ground_truth = GroundTruth(
        cause_category="transient_restart_no_action",
        component="stream-processor",
        expected_action="无需处理：Deployment 会自动重建 Pod，就绪后 lag 会自行回落",
    )
    # 没有独有的特征字符串：删除 Pod 留下的事件（Killing 等）和其他会重启
    # stream-processor 的场景一样，写进去只会误报。这个场景的污染只能靠回看窗口和冷却期防住
    signatures = ()

    def inject(self) -> None:
        d = self.deployment("stream-processor")
        self.k8s.delete_pods(d.namespace, "app=stream-processor")
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        d = self.deployment("stream-processor")
        min_growth = self.params["min_lag_growth"]
        baseline: list[int] = []

        def check() -> Outcome:
            lag = self.k8s.consumer_lag("stream-processor")
            if not baseline:
                baseline.append(lag)
            growth = lag - baseline[0]
            pods = [
                p
                for p in self.k8s.pods_info(d.namespace, "app=stream-processor")
                if not p["terminating"]
            ]
            ready = [p for p in pods if p["ready"]]
            detail = (
                f"可用实例 {len(ready)}/{len(pods)}；"
                f"lag {baseline[0]} -> {lag}（涨了 {growth}，要求 ≥ {min_growth}）"
            )
            return Outcome(not ready and growth >= min_growth, detail)

        return wait_until(check, timeout)

    def restore(self) -> None:
        # 什么都不用做：Pod 会自己恢复。verify_restored 会等到健康检查全部通过
        return None
