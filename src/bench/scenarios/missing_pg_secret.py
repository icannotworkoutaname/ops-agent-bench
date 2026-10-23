"""missing-pg-secret：删除 postgres-credentials，然后重启 query-service。"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class MissingPgSecret(Scenario):
    id = "missing-pg-secret"
    ground_truth = GroundTruth(
        cause_category="missing_secret_or_config",
        component="query-service",
        expected_action="重建 default 里的 postgres-credentials Secret（从 data 里的源 Secret 复制）",
    )

    # Pod 卡住时的状态，以及 kubelet 报的缺 Secret 的原因
    signatures = ('secret "postgres-credentials" not found', "CreateContainerConfigError")

    @property
    def _dep(self):
        return next(d for d in self.env.deployments if d.name == "query-service")

    def inject(self) -> None:
        s = self.env.secret_target
        self.k8s.delete_secret(s.namespace, s.name)
        # 只有新建的容器才会去解析 Secret 引用，所以必须重启，症状才会出现
        self.k8s.rollout_restart(self._dep.namespace, self._dep.name)
        self.mark_injected()

    def verify_injected(self, timeout: float) -> Outcome:
        d = self._dep

        def check() -> Outcome:
            pods = self.k8s.pods_info(d.namespace, "app=query-service")
            stuck = [p for p in pods if "CreateContainerConfigError" in p["waiting_reasons"]]
            summary = [
                f"{p['name']} ready={p['ready']} waiting={p['waiting_reasons']}" for p in pods
            ]
            if stuck:
                return Outcome(True, f"新 Pod 卡在 CreateContainerConfigError: {summary}")
            return Outcome(False, f"没有 Pod 处于 CreateContainerConfigError: {summary}")

        return wait_until(check, timeout)

    def restore(self) -> None:
        # 按 README 的方式从 data 里的源 Secret 重建；kubelet 会自动重试卡住的容器。
        # 不依赖“注入前把 Secret 内容存到本地文件”，磁盘上不会留下密码。
        self.k8s.copy_secret_from_source()
