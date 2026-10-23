"""ingestion-overload：资源限制不变，把流量调到明显超过 ingestion-service 的处理能力。

症状：ingestion-service 的 CPU 打满（并被 CFS 限流）、请求延迟上升，
而 Kafka 的消费 lag 基本为 0（瓶颈在入口，不在下游）。
"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class IngestionOverload(Scenario):
    id = "ingestion-overload"
    ground_truth = GroundTruth(
        cause_category="load_saturation",
        component="ingestion-service",
        expected_action="限制或分流入口流量，或给 ingestion-service 扩容（增加副本 / 提高 CPU limit）",
    )
    # 没有独有的错误字符串：过载只体现在数字（CPU、延迟）上，不留下特别的日志或事件
    signatures = ()

    def inject(self) -> None:
        self.k8s.set_traffic_rate(self.params["traffic_rate"])
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        min_cores = self.params["min_cpu_cores"]
        query = (
            "sum(rate(container_cpu_usage_seconds_total"
            '{namespace="default",pod=~"ingestion-service.*"}[30s]))'
        )

        def check() -> Outcome:
            cores = self.k8s.prom_value(query)
            stats = self.k8s.traffic_stats(self.seconds_since_injected())["write"]
            detail = (
                f"ingestion-service CPU {cores if cores is None else round(cores, 2)} 核"
                f"（要求 ≥ {min_cores}）；写入延迟均值 {stats['avg_ms']}ms，失败 {stats['fail']} 次"
            )
            return Outcome(cores is not None and cores >= min_cores, detail)

        return wait_until(check, timeout)

    def restore(self) -> None:
        self.k8s.set_traffic_rate(self.env.traffic.default_rate)
