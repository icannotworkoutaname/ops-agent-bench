"""kafka-down：Kafka StatefulSet 缩到 0。

实测的症状（2026-10-23，和 stage-2.md 里预期的不完全一样）：
  - ingestion-service 的写入请求**仍然返回 202**：KafkaTemplate.send 是异步的，失败只发生在后台的生产者里，
    所以流量发生器看不到任何写入失败；
  - 三个服务的 Pod 都保持 Ready，Prometheus 里的 up 也都是 1；
  - 真正的症状在日志里：三个 Kafka 客户端持续报 "Error connecting to node kafka-controller-0..."
    （UnknownHostException），同时 Kafka 的 Pod 不存在，get_consumer_lag 也查不了。
所以 agent 要从日志和“Kafka Pod 不见了”推出原因，而不是从某个服务不健康推出。
"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until

# 必须在这两个服务的日志里都看到连接 Kafka 的错误：对应计划里“ingestion-service 写入出问题，
# stream-processor 断连”
CLIENTS_THAT_MUST_SHOW_ERRORS = ("ingestion-service", "stream-processor")
CONNECT_ERROR_MARKERS = ("Error connecting to node", "UnknownHostException")


class KafkaDown(Scenario):
    id = "kafka-down"
    ground_truth = GroundTruth(
        cause_category="dependency_unavailable",
        component="kafka",
        expected_action="恢复 Kafka（把 StatefulSet 副本数调回 1）",
    )
    # Kafka 客户端找不到 broker 时的日志，以及 broker 被停掉的事件
    signatures = (
        "Error connecting to node kafka-controller-0",
        "UnknownHostException: kafka-controller-0",
        "Stopping container kafka",
    )

    def inject(self) -> None:
        k = self.env.kafka_sts
        self.k8s.scale_statefulset(k.namespace, k.name, 0)
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        k = self.env.kafka_sts

        def check() -> Outcome:
            ready, kafka_detail = self.k8s.statefulset_ready(k.namespace, k.name)
            showing = []
            for app in CLIENTS_THAT_MUST_SHOW_ERRORS:
                d = self.deployment(app)
                pods = [
                    p for p in self.k8s.pods_info(d.namespace, f"app={app}") if not p["terminating"]
                ]
                if not pods:
                    continue
                # 只看注入之后的日志，免得把更早的内容当成症状
                logs = self.k8s.pod_logs(
                    d.namespace,
                    pods[-1]["name"],
                    tail=300,
                    since_seconds=self.seconds_since_injected(),
                )
                if any(marker in logs for marker in CONNECT_ERROR_MARKERS):
                    showing.append(app)
            ok = not ready and set(showing) == set(CLIENTS_THAT_MUST_SHOW_ERRORS)
            return Outcome(
                ok,
                f"Kafka {kafka_detail}；日志里出现连接 Kafka 错误的服务: {showing or '无'}",
            )

        return wait_until(check, timeout)

    def restore(self) -> None:
        k = self.env.kafka_sts
        self.k8s.scale_statefulset(k.namespace, k.name, 1)
