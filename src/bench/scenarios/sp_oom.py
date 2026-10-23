"""sp-oom：stream-processor 的内存 limit 压到启动不起来。

症状：容器反复退出，CrashLoopBackOff。退出的方式有两种，留下的证据不同：
  - 被内核 OOMKilled（exit 137）：describe_pod 的 lastState 里是 OOMKilled；
  - JVM 自己抛 OutOfMemoryError 后退出：日志里有异常栈，lastState 是 Error。
verify_injected 的结果里会写明实际看到的是哪种，供仓库所有者决定标准答案怎么写。
"""

from bench.scenarios.base import GroundTruth, Outcome, Scenario, wait_until


class SpOom(Scenario):
    id = "sp-oom"
    ground_truth = GroundTruth(
        cause_category="resource_limit_memory",
        component="stream-processor",
        expected_action="提高 stream-processor 的内存 limit（恢复到 512Mi）",
    )
    # 实测后再补全（OOMKilled 还是 OutOfMemoryError，见 docs 里的稳定性报告）
    signatures = ("OOMKilled", "java.lang.OutOfMemoryError")

    def inject(self) -> None:
        d = self.deployment("stream-processor")
        limit = self.params["memory_limit"]
        self.redeploy_with_patch(
            d,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "stream-processor",
                                    # requests 不能大于 limits，所以一起改
                                    "resources": {
                                        "requests": {"memory": limit},
                                        "limits": {"memory": limit},
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
        d = self.deployment("stream-processor")

        def check() -> Outcome:
            pods = self.k8s.pods_info(d.namespace, "app=stream-processor")
            if not pods:
                return Outcome(False, "没有 Pod")
            newest = pods[-1]
            crash_loop = "CrashLoopBackOff" in newest["waiting_reasons"]
            died = newest["restarts"] >= 1 and bool(newest["last_terminated"])
            detail = (
                f"Pod {newest['name']} ready={newest['ready']} 重启 {newest['restarts']} 次；"
                f"waiting={newest['waiting_reasons']}；上次终止={newest['last_terminated']}"
            )
            return Outcome(crash_loop or died, detail)

        return wait_until(check, timeout)

    def restore(self) -> None:
        self.restore_from_manifest("stream_processor")
