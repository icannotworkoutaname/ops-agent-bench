"""bad-kafka-bootstrap：把 stream-processor 的 KAFKA_BOOTSTRAP_SERVERS 改成一个解析不到的地址。"""

import re

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until

# 看起来像是手误写错的主机名，但集群里没有这个 Service，DNS 解析不到。
# 刻意不带任何场景信息（隔离原则 2）。
BAD_BOOTSTRAP = "kafka-0.kafka.svc.cluster.local:9092"

# stream-processor 日志里“连不上 Kafka”的常见字样
CONNECT_ERROR = re.compile(
    r"bootstrap|resolve|UnknownHost|Connection to node|Timed out", re.IGNORECASE
)


class BadKafkaBootstrap(Scenario):
    id = "bad-kafka-bootstrap"
    ground_truth = GroundTruth(
        cause_category="misconfiguration_env",
        component="stream-processor",
        expected_action="把 stream-processor 的 KAFKA_BOOTSTRAP_SERVERS 改回正确地址",
    )

    @property
    def _dep(self):
        return next(d for d in self.env.deployments if d.name == "stream-processor")

    def inject(self) -> None:
        d = self._dep
        self.k8s.set_deployment_env(
            d.namespace, d.name, "stream-processor", "KAFKA_BOOTSTRAP_SERVERS", BAD_BOOTSTRAP
        )
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        d = self._dep
        min_growth = self.run_cfg["scenario"]["min_lag_growth"]
        baseline: list[int] = []  # 第一次采样的 lag，作为“上涨”的参照

        def check() -> Outcome:
            lag = self.k8s.consumer_lag("stream-processor")
            if not baseline:
                baseline.append(lag)
            growth = lag - baseline[0]

            pods = self.k8s.pods_info(d.namespace, "app=stream-processor")
            newest = pods[-1]
            logs = self.k8s.pod_logs(d.namespace, newest["name"])
            has_error = bool(CONNECT_ERROR.search(logs))

            detail = (
                f"最新 Pod {newest['name']} ready={newest['ready']} "
                f"waiting={newest['waiting_reasons']}；日志有连接错误={has_error}；"
                f"lag {baseline[0]} -> {lag}（涨了 {growth}，要求 ≥ {min_growth}）；Pod 数 {len(pods)}"
            )
            return Outcome(has_error and growth >= min_growth, detail)

        return wait_until(check, timeout)

    def restore(self) -> None:
        # 恢复的唯一依据是 music-streaming 仓库里的 k8s/stream-processor.yaml
        self.k8s.apply_manifest(str(self.env.manifest_path("stream_processor")))
