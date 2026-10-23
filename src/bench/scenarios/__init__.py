from bench.scenarios.bad_kafka_bootstrap import BadKafkaBootstrap
from bench.scenarios.base import Scenario
from bench.scenarios.ingestion_overload import IngestionOverload
from bench.scenarios.kafka_down import KafkaDown
from bench.scenarios.missing_pg_secret import MissingPgSecret
from bench.scenarios.no_fault import NoFault
from bench.scenarios.postgres_down import PostgresDown
from bench.scenarios.query_probe_misconfig import QueryProbeMisconfig
from bench.scenarios.redis_down import RedisDown
from bench.scenarios.sp_cpu_throttle import SpCpuThrottle
from bench.scenarios.sp_oom import SpOom
from bench.scenarios.sp_pod_killed import SpPodKilled

# 顺序和 docs/plan/stage-2.md 的场景清单一致：前 3 个是第 1 阶段已有的
SCENARIOS: dict[str, type[Scenario]] = {
    cls.id: cls
    for cls in (
        RedisDown,
        BadKafkaBootstrap,
        MissingPgSecret,
        SpPodKilled,
        SpCpuThrottle,
        SpOom,
        KafkaDown,
        PostgresDown,
        QueryProbeMisconfig,
        IngestionOverload,
        NoFault,
    )
}
