"""注入器和健康检查用的 k8s 客户端（有写权限）。

重要：src/bench/tools/ 里的 agent 工具不得 import 这个模块（隔离原则 1）。
注入器和 agent 工具是两套代码、两套权限，否则 agent 一旦能走到这里，
就等于有了修改集群的能力，评估结果就不可信了。
"""

import json

from kubernetes import client, config
from kubernetes.stream import stream

from bench.config import EnvConfig

# 注入时用的 field manager 名字。刻意取一个和场景无关的中性名字（隔离原则 2），
# 集群里不能留下能暴露场景名的痕迹
FIELD_MANAGER = "ops-tool"


class AdminK8s:
    def __init__(self, env: EnvConfig):
        self.env = env
        # 明确指定 context：只允许对配置里的集群操作，不依赖 kubeconfig 的“当前 context”，
        # 这样即使用户的 kubectl 切到了别的集群，也不会误伤
        config.load_kube_config(context=env.kube_context)
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()

    # ---------- 只读：状态查询 ----------

    def deployment_ready(self, namespace: str, name: str) -> tuple[bool, str]:
        d = self.apps.read_namespaced_deployment(name, namespace)
        desired = d.spec.replicas or 0
        ready = d.status.ready_replicas or 0
        total = d.status.replicas or 0
        # total > desired 说明还有旧 Pod 在被替换（滚动更新中），也不算稳定
        ok = desired > 0 and ready == desired and total == desired
        return ok, f"ready {ready}/{desired}, total {total}"

    def statefulset_ready(self, namespace: str, name: str) -> tuple[bool, str]:
        s = self.apps.read_namespaced_stateful_set(name, namespace)
        desired = s.spec.replicas or 0
        ready = s.status.ready_replicas or 0
        return desired > 0 and ready == desired, f"ready {ready}/{desired}"

    def pod_ready(self, namespace: str, name: str) -> tuple[bool, str]:
        pod = self.core.read_namespaced_pod(name, namespace)
        for cond in pod.status.conditions or []:
            if cond.type == "Ready":
                return cond.status == "True", f"phase {pod.status.phase}, Ready={cond.status}"
        return False, f"phase {pod.status.phase}, no Ready condition"

    def exec_in_pod(self, namespace: str, pod: str, container: str, command: list[str]) -> str:
        """在 Pod 里执行一条固定命令并返回 stdout。command 由调用方以列表形式给出，不经过 shell。"""
        return stream(
            self.core.connect_get_namespaced_pod_exec,
            pod,
            namespace,
            container=container,
            command=command,
            stderr=False,
            stdin=False,
            stdout=True,
            tty=False,
        )

    def consumer_group_describe(self, group: str) -> str:
        k = self.env.kafka
        return self.exec_in_pod(
            k.namespace,
            k.pod,
            k.container,
            [
                k.consumer_groups_script,
                "--bootstrap-server",
                k.bootstrap_server,
                "--describe",
                "--group",
                group,
            ],
        )

    def traffic_stats(self, window_seconds: int) -> dict:
        """通过 API server 的 Pod proxy 读流量发生器的计数。

        不用 port-forward（它绑定某个具体 Pod，Pod 一重建就断），也不用 exec，
        只需要 API server 的访问权限。
        """
        t = self.env.traffic
        pods = self.core.list_namespaced_pod(
            self.env.bench_namespace, label_selector=f"app={t.name}"
        ).items
        running = [p for p in pods if p.status.phase == "Running"]
        if not running:
            raise RuntimeError(f"bench namespace 里没有运行中的 {t.name} Pod")
        pod = running[0].metadata.name
        # 官方生成的 proxy 方法不支持 query string，所以窗口秒数放在路径里：/stats/<秒数>
        resp = self.core.connect_get_namespaced_pod_proxy_with_path(
            f"{pod}:{t.port}",
            self.env.bench_namespace,
            path=f"stats/{window_seconds}",
            _preload_content=False,
            _request_timeout=10,
        )
        return json.loads(resp.data)
