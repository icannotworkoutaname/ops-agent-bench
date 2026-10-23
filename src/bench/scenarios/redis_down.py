"""redis-down：Redis StatefulSet 缩到 0。"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class RedisDown(Scenario):
    id = "redis-down"
    ground_truth = GroundTruth(
        cause_category="dependency_unavailable",
        component="redis",
        expected_action="恢复 Redis（把 StatefulSet 副本数调回 1）",
    )

    # query-service 连不上 Redis 时的日志，以及 Redis Pod 被停掉时的事件。
    # 不能用 "redis-master-0" 这样的名字：健康环境里 list_pods 也会列出它；
    # 也不能用 "No matching pods found"：任何工作负载没有 Pod 时 PodDisruptionBudget 都会报这句
    # （试跑里 kafka-down 的运行就因此被误判为污染）。所以要带上 Redis 自己的对象名
    signatures = (
        "Cannot reconnect to [redis-master",
        "ConnectionWatchdog",
        "Stopping container redis",
        "PodDisruptionBudget/redis-master",
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
