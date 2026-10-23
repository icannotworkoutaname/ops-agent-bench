"""kafka-down：Kafka StatefulSet 缩到 0。

症状：ingestion-service 写 Kafka 时卡住 / 失败（写入请求超时），stream-processor 与 broker 断连。
"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class KafkaDown(Scenario):
    id = "kafka-down"
    ground_truth = GroundTruth(
        cause_category="dependency_unavailable",
        component="kafka",
        expected_action="恢复 Kafka（把 StatefulSet 副本数调回 1）",
    )
    # Kafka 客户端连不上 broker 时的日志，以及 broker 被停掉的事件
    signatures = ("Broker may not be available", "Stopping container kafka")

    def inject(self) -> None:
        k = self.env.kafka_sts
        self.k8s.scale_statefulset(k.namespace, k.name, 0)
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        k = self.env.kafka_sts

        def check() -> Outcome:
            stats = self.k8s.traffic_stats(self.seconds_since_injected())
            fails = stats["write"]["fail"]
            ready, kafka_detail = self.k8s.statefulset_ready(k.namespace, k.name)
            ok = fails >= 2 and not ready
            return Outcome(
                ok,
                f"注入后写入失败 {fails} 次（最近错误: {stats['write']['last_error'][:60]}）；"
                f"Kafka {kafka_detail}",
            )

        return wait_until(check, timeout)

    def restore(self) -> None:
        k = self.env.kafka_sts
        self.k8s.scale_statefulset(k.namespace, k.name, 1)
