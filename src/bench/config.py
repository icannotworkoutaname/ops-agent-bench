"""读取 config/env.yaml 和 config/run.yaml。

用几个很小的 dataclass 把配置的形状写清楚，读代码的人一眼能看出有哪些字段。
"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

# 仓库根目录：src/bench/config.py 往上两层的上一层
ROOT = Path(__file__).resolve().parents[2]


def load_raw(name: str) -> dict:
    """把 config/ 下的一个 yaml 原样读成字典（也用来在运行记录里保存配置快照）。"""
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


_load_yaml = load_raw


def load_dotenv() -> None:
    """读取仓库根目录的 .env，把 KEY=VALUE 放进环境变量（已有的不覆盖）。

    手写而不是装 python-dotenv，少一个依赖。只在需要 API key 的命令里调用；
    值不会被打印或写进任何日志。
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


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
    # 在 Kafka Pod 里 exec 命令的最长等待时间；没有它的话，连接卡住会无限期等下去
    exec_timeout_seconds: int


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
class MonitoringConfig:
    namespace: str
    name: str
    port: int
    image: str
    retention: str


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
    monitoring: MonitoringConfig
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
        monitoring=MonitoringConfig(**raw["monitoring"]),
        manifests=raw["manifests"],
    )


def load_run() -> dict:
    """run.yaml 的内容随阶段增长，先保持成普通 dict，等字段稳定了再收成 dataclass。"""
    return _load_yaml("run.yaml")
