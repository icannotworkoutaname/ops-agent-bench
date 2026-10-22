"""读取 config/env.yaml 和 config/run.yaml。

用几个很小的 dataclass 把配置的形状写清楚，读代码的人一眼能看出有哪些字段。
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

# 仓库根目录：src/bench/config.py 往上两层的上一层
ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Workload:
    name: str
    namespace: str


@dataclass(frozen=True)
class KafkaConfig:
    namespace: str
    pod: str
    container: str
    consumer_groups_script: str
    bootstrap_server: str


@dataclass(frozen=True)
class SecretRef:
    namespace: str
    name: str
    key: str


@dataclass(frozen=True)
class TrafficConfig:
    name: str
    port: int
    image: str


@dataclass(frozen=True)
class EnvConfig:
    kube_context: str
    music_streaming_repo: Path
    agent_visible_namespaces: list[str]
    bench_namespace: str
    deployments: list[Workload]
    redis: Workload
    postgres: Workload
    kafka: KafkaConfig
    consumer_groups: list[str]
    secret_target: SecretRef
    secret_source: SecretRef
    ingestion_url: str
    query_url: str
    traffic: TrafficConfig
    manifests: dict[str, str]

    def manifest_path(self, key: str) -> Path:
        """music-streaming 仓库里某个 manifest 的绝对路径（只读使用）。"""
        return self.music_streaming_repo / self.manifests[key]


def load_env() -> EnvConfig:
    raw = _load_yaml("env.yaml")
    return EnvConfig(
        kube_context=raw["kube_context"],
        music_streaming_repo=Path(raw["music_streaming_repo"]),
        agent_visible_namespaces=raw["agent_visible_namespaces"],
        bench_namespace=raw["bench_namespace"],
        deployments=[Workload(**d) for d in raw["deployments"]],
        redis=Workload(**raw["statefulsets"]["redis"]),
        postgres=Workload(**raw["statefulsets"]["postgres"]),
        kafka=KafkaConfig(**raw["kafka"]),
        consumer_groups=raw["consumer_groups"],
        secret_target=SecretRef(**raw["secrets"]["target"]),
        secret_source=SecretRef(**raw["secrets"]["source"]),
        ingestion_url=raw["services"]["ingestion_url"],
        query_url=raw["services"]["query_url"],
        traffic=TrafficConfig(**raw["traffic"]),
        manifests=raw["manifests"],
    )


def load_run() -> dict:
    """run.yaml 的内容随阶段增长，先保持成普通 dict，等字段稳定了再收成 dataclass。"""
    return _load_yaml("run.yaml")
