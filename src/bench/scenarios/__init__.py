from bench.scenarios.bad_kafka_bootstrap import BadKafkaBootstrap
from bench.scenarios.base import Scenario
from bench.scenarios.missing_pg_secret import MissingPgSecret
from bench.scenarios.redis_down import RedisDown

SCENARIOS: dict[str, type[Scenario]] = {
    cls.id: cls for cls in (RedisDown, BadKafkaBootstrap, MissingPgSecret)
}
