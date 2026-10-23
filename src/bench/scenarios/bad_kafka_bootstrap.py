"""bad-kafka-bootstrap：把 stream-processor 的 KAFKA_BOOTSTRAP_SERVERS 改成一个解析不到的地址。"""

import contextlib
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

    # 错误地址本身，以及 Kafka 客户端解析不了它时抛的异常
    signatures = ("kafka-0.kafka.svc", "No resolvable bootstrap urls")

    @property
    def _dep(self):
        return next(d for d in self.env.deployments if d.name == "stream-processor")

    def inject(self) -> None:
        d = self._dep
        # 为什么先缩到 0：只改环境变量会触发滚动更新，而单副本时新 Pod 起不来、
        # 旧 Pod 会一直保留并继续消费（实测过），lag 不会上涨，故障就不明显。
        # 先把旧 Pod 全部停掉，再带着错误配置重新起一个，才是“配置错了又重启”的真实情形。
        self.k8s.scale_deployment(d.namespace, d.name, 0)
        self.k8s.wait_pods_deleted(d.namespace, "app=stream-processor")
        self.k8s.set_deployment_env(
            d.namespace, d.name, "stream-processor", "KAFKA_BOOTSTRAP_SERVERS", BAD_BOOTSTRAP
        )
        self.k8s.scale_deployment(d.namespace, d.name, 1)
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
            # 崩溃循环时当前容器可能刚启动、日志还是空的，错误在上一次崩溃的日志里；
            # 还没有“上一次”容器时 API 会报错，忽略即可
            with contextlib.suppress(Exception):
                logs += self.k8s.pod_logs(d.namespace, newest["name"], previous=True)
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
