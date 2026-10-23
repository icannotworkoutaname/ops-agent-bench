"""query-probe-misconfig：query-service 的 readinessProbe 路径改成一个不存在的路径。

症状：应用本身启动正常、也在消费，但探针一直拿到 404，Pod 永远不 Ready，
Service 没有可用的后端，查询失败。这个场景的难点是“容器里一切正常，问题出在探针配置”。
"""

from datetime import UTC, datetime

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class QueryProbeMisconfig(Scenario):
    id = "query-probe-misconfig"
    ground_truth = GroundTruth(
        cause_category="probe_misconfiguration",
        component="query-service",
        expected_action="把 query-service 的 readinessProbe 路径改回 /actuator/health",
    )
    # 错误的探针路径本身，以及探针收到 404 时的事件
    signatures = ("/actuator/healthz", "statuscode: 404")

    def inject(self) -> None:
        d = self.deployment("query-service")
        self.redeploy_with_patch(
            d,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "query-service",
                                    "readinessProbe": {
                                        "httpGet": {"path": self.params["bad_path"], "port": 8082}
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        )
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        d = self.deployment("query-service")
        min_running = self.params["min_running_seconds"]

        def check() -> Outcome:
            pods = [
                p
                for p in self.k8s.pods_info(d.namespace, "app=query-service")
                if not p["terminating"]
            ]
            if not pods:
                return Outcome(False, "没有 Pod")
            newest = pods[-1]
            age = (datetime.now(UTC) - newest["created"]).total_seconds()
            stats = self.k8s.traffic_stats(self.seconds_since_injected())
            query_fails = stats["query"]["fail"]
            # 区分“还在启动”和“探针配错了”：容器已经 Running 了一段时间，仍然不 Ready，没有重启过
            stuck = (
                newest["phase"] == "Running"
                and not newest["ready"]
                and newest["restarts"] == 0
                and age >= min_running
            )
            detail = (
                f"Pod {newest['name']} phase={newest['phase']} ready={newest['ready']} "
                f"已存在 {age:.0f}s（要求 ≥ {min_running}s）；查询失败 {query_fails} 次"
            )
            return Outcome(stuck and query_fails >= 2, detail)

        return wait_until(check, timeout)

    def restore(self) -> None:
        self.restore_from_manifest("query_service")
