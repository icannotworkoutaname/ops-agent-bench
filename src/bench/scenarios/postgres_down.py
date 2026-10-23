"""postgres-down：PostgreSQL StatefulSet 缩到 0。

症状：query-service 先写 PostgreSQL 再写 Redis，PostgreSQL 写不进去，消费卡住，
query-service 消费者组 lag 上涨，Redis 里的数据停止更新；但查询 Redis 的接口仍然正常。
"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class PostgresDown(Scenario):
    id = "postgres-down"
    ground_truth = GroundTruth(
        cause_category="dependency_unavailable",
        component="postgresql",
        expected_action="恢复 PostgreSQL（把 StatefulSet 副本数调回 1）",
    )
    # JDBC 连不上时的异常，以及 PostgreSQL 被停掉的事件
    signatures = ("PSQLException", "Stopping container postgresql")

    def inject(self) -> None:
        p = self.env.postgres
        self.k8s.scale_statefulset(p.namespace, p.name, 0)
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        p = self.env.postgres
        min_growth = self.params["min_lag_growth"]
        baseline: list[int] = []

        def check() -> Outcome:
            lag = self.k8s.consumer_lag("query-service")
            if not baseline:
                baseline.append(lag)
            growth = lag - baseline[0]
            ready, pg_detail = self.k8s.statefulset_ready(p.namespace, p.name)
            detail = (
                f"query-service 消费者组 lag {baseline[0]} -> {lag}"
                f"（涨了 {growth}，要求 ≥ {min_growth}）；PostgreSQL {pg_detail}"
            )
            return Outcome(growth >= min_growth and not ready, detail)

        return wait_until(check, timeout)

    def restore(self) -> None:
        p = self.env.postgres
        self.k8s.scale_statefulset(p.namespace, p.name, 1)
