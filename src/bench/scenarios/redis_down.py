"""redis-down：Redis StatefulSet 缩到 0。"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class RedisDown(Scenario):
    id = "redis-down"
    ground_truth = GroundTruth(
        cause_category="dependency_unavailable",
        component="redis",
        expected_action="恢复 Redis（把 StatefulSet 副本数调回 1）",
    )

    def inject(self) -> None:
        r = self.env.redis
        self.k8s.scale_statefulset(r.namespace, r.name, 0)
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        r = self.env.redis

        def check() -> Outcome:
            # 流量发生器的 top 查询读 Redis，Redis 没了它应该开始失败
            stats = self.k8s.traffic_stats(self.seconds_since_injected())
            fails = stats["query"]["fail"]
            ready, redis_detail = self.k8s.statefulset_ready(r.namespace, r.name)
            if fails >= 2 and not ready:
                return Outcome(True, f"注入后查询失败 {fails} 次；Redis {redis_detail}")
            return Outcome(False, f"查询失败 {fails} 次；Redis {redis_detail}")

        return wait_until(check, timeout)

    def restore(self) -> None:
        r = self.env.redis
        self.k8s.scale_statefulset(r.namespace, r.name, 1)
