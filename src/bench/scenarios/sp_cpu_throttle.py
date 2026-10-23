"""sp-cpu-throttle：stream-processor 的 CPU limit 压到 100m，同时把流量调高。

症状：启动明显变慢，启动后也处理不过来，lag 持续上涨；cAdvisor 里 CPU 限流的比例很高。
"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class SpCpuThrottle(Scenario):
    id = "sp-cpu-throttle"
    ground_truth = GroundTruth(
        cause_category="resource_limit_cpu",
        component="stream-processor",
        expected_action="提高 stream-processor 的 CPU limit（恢复到 500m）",
    )
    # 100m 的 limit 只会出现在这个场景里（其他组件的 limit 是 150m / 500m 等）
    signatures = ("limits={'cpu': '100m'",)

    def inject(self) -> None:
        d = self.deployment("stream-processor")
        p = self.params
        # 先调高流量：压力要在 stream-processor 起来之前就在
        self.k8s.set_traffic_rate(p["traffic_rate"])
        self.redeploy_with_patch(
            d,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "stream-processor",
                                    # requests 不能大于 limits，所以一起改小
                                    "resources": {
                                        "requests": {"cpu": p["cpu_request"]},
                                        "limits": {"cpu": p["cpu_limit"]},
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        )
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        d = self.deployment("stream-processor")
        min_growth = self.params["min_lag_growth"]
        min_ratio = self.params["min_throttle_ratio"]
        baseline: list[int] = []
        # CPU 被 CFS 限流的比例 = 被限流的周期数 / 总周期数（cAdvisor 的 Pod 级指标）
        throttle_query = (
            "sum(rate(container_cpu_cfs_throttled_periods_total"
            '{namespace="default",pod=~"stream-processor.*"}[30s]))'
            " / sum(rate(container_cpu_cfs_periods_total"
            '{namespace="default",pod=~"stream-processor.*"}[30s]))'
        )

        def check() -> Outcome:
            lag = self.k8s.consumer_lag("stream-processor")
            if not baseline:
                baseline.append(lag)
            growth = lag - baseline[0]
            ratio = self.k8s.prom_value(throttle_query)
            pods = self.k8s.pods_info(d.namespace, "app=stream-processor")
            newest = pods[-1] if pods else None
            state = f"Pod ready={newest['ready']}" if newest else "没有 Pod"
            shown = "无数据" if ratio is None else f"{ratio:.0%}"
            detail = (
                f"{state}；lag {baseline[0]} -> {lag}（涨了 {growth}，要求 ≥ {min_growth}）；"
                f"CPU 限流比例 {shown}（要求 ≥ {min_ratio:.0%}）"
            )
            # Prometheus 暂时没有数据时不能当成功：宁可多等一轮
            return Outcome(
                growth >= min_growth and ratio is not None and ratio >= min_ratio, detail
            )

        return wait_until(check, timeout)

    def restore(self) -> None:
        self.restore_from_manifest("stream_processor")
        self.k8s.set_traffic_rate(self.env.traffic.default_rate)
