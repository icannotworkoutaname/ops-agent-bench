"""注入器和健康检查用的 k8s 客户端（有写权限）。

重要：src/bench/tools/ 里的 agent 工具不得 import 这个模块（隔离原则 1）。
注入器和 agent 工具是两套代码、两套权限，否则 agent 一旦能走到这里，
就等于有了修改集群的能力，评估结果就不可信了。
"""

import json
import subprocess

from kubernetes import client, config
from kubernetes.stream import stream

from bench.config import EnvConfig

# 注入时用的 field manager 名字。刻意取一个和场景无关的中性名字（隔离原则 2），
# 集群里不能留下能暴露场景名的痕迹
FIELD_MANAGER = "ops-tool"


def kubectl(env: EnvConfig, *args: str, stdin: str | None = None) -> str:
    """执行一条 kubectl 命令并返回 stdout。

    写操作（缩容、改环境变量、删 Secret、apply）都走这里：命令一眼能看懂，
    读代码的人可以直接复制到终端里复现。固定带 --context，不依赖 kubeconfig 的当前 context。
    """
    result = subprocess.run(
        ["kubectl", "--context", env.kube_context, *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,  # 下面自己检查返回码，这样报错信息里能带上 stderr
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} 失败: {result.stderr.strip()}")
    return result.stdout


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
        """在 Pod 里执行一条固定命令并返回 stdout。command 由调用方以列表形式给出，不经过 shell。

        带超时：连接卡住时到点就返回（可能是不完整的输出，由调用方当作“检查失败”处理），
        而不是让整个运行无限期挂起。
        """
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
            _request_timeout=self.env.kafka.exec_timeout_seconds,
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

    def _traffic_get(self, path: str) -> dict:
        """通过 API server 的 Pod proxy 访问流量发生器。

        不用 port-forward（它绑定某个具体 Pod，Pod 一重建就断），也不用 exec，
        只需要 API server 的访问权限。官方生成的 proxy 方法不支持 query string，
        所以参数都放在路径里（/stats/<秒数>、/rate/<速率>）。
        """
        t = self.env.traffic
        pods = self.core.list_namespaced_pod(
            self.env.bench_namespace, label_selector=f"app={t.name}"
        ).items
        # 不能选正在被终止的旧 Pod：重启发生器后，旧 Pod 要过 30 秒才会真正消失，
        # 期间它的状态仍是 Running，但跑的是旧代码、计数也不是当前的
        live = [
            p for p in pods if p.status.phase == "Running" and p.metadata.deletion_timestamp is None
        ]
        if not live:
            raise RuntimeError(f"bench namespace 里没有运行中的 {t.name} Pod")
        resp = self.core.connect_get_namespaced_pod_proxy_with_path(
            f"{live[0].metadata.name}:{t.port}",
            self.env.bench_namespace,
            path=path,
            _preload_content=False,
            _request_timeout=10,
        )
        return json.loads(resp.data)

    def traffic_stats(self, window_seconds: int) -> dict:
        return self._traffic_get(f"stats/{window_seconds}")

    def set_traffic_rate(self, events_per_second: float) -> dict:
        """调整流量发生器的写入速率（不重启 Pod）。有些场景需要高速率，结束后再调回默认值。"""
        return self._traffic_get(f"rate/{events_per_second}")

    def control_plane_auth_errors(self, window_seconds: int) -> dict[str, int]:
        """统计控制面组件最近 window_seconds 秒日志里的认证错误条数。

        背景：机器休眠 / 黑屏后时钟跳变，控制面用的 ServiceAccount token 会过期，
        之后控制器对 apiserver 的写操作会报 Unauthorized。这类错误不会让任何 Pod 变成
        NotReady，所以普通的健康检查看不出来，但它会以事件的形式漏给 agent，污染评测结果。
        只有测试框架（管理侧）会去读 kube-system，agent 的工具看不到这个 namespace。
        """
        counts: dict[str, int] = {}
        for component in ("kube-controller-manager", "kube-apiserver"):
            pods = self.core.list_namespaced_pod(
                "kube-system", label_selector=f"component={component}"
            ).items
            n = 0
            for pod in pods:
                text = self.core.read_namespaced_pod_log(
                    pod.metadata.name, "kube-system", since_seconds=window_seconds
                )
                n += sum(
                    1
                    for line in text.splitlines()
                    if "unauthorized" in line.lower() or "token has expired" in line.lower()
                )
            counts[component] = n
        return counts

    def pods_info(self, namespace: str, label_selector: str) -> list[dict]:
        """某个工作负载的 Pod 概况，包括容器卡在什么状态（waiting reason）。"""
        info = []
        for p in self.core.list_namespaced_pod(namespace, label_selector=label_selector).items:
            statuses = p.status.container_statuses or []
            waiting = [s.state.waiting.reason for s in statuses if s.state and s.state.waiting]
            # 上一次终止的原因和退出码：区分 OOMKilled（内核杀的）和 Error（进程自己退出）
            last_terminated = [
                f"{s.last_state.terminated.reason}(exit {s.last_state.terminated.exit_code})"
                for s in statuses
                if s.last_state and s.last_state.terminated
            ]
            info.append(
                {
                    "name": p.metadata.name,
                    "created": p.metadata.creation_timestamp,
                    "phase": p.status.phase,
                    # 正在被终止的 Pod（比如刚被删除）在退出的几秒里仍显示 Ready，
                    # 判断“有没有可用实例”时必须排除它们
                    "terminating": p.metadata.deletion_timestamp is not None,
                    "ready": bool(statuses) and all(s.ready for s in statuses),
                    "waiting_reasons": waiting,
                    "last_terminated": last_terminated,
                    "restarts": sum(s.restart_count for s in statuses),
                }
            )
        return sorted(info, key=lambda i: i["created"])

    def abnormal_terminations(self, namespaces: list[str]) -> list[dict]:
        """这些 namespace 里所有容器“异常终止”的记录（退出码非 0），当前状态和上一次状态都算。

        为什么需要：容器在原地被杀（比如 Kafka 超过内存上限 OOMKilled）后，Pod 还在、很快又 Ready，
        普通的就绪检查看不出来，但 describe_pod 会一直显示 lastState。冷却期要靠它知道
        “最近发生过意外”，等回看窗口过去以后再开始下一次运行。
        """
        out = []
        for ns in namespaces:
            for p in self.core.list_namespaced_pod(ns).items:
                for s in p.status.container_statuses or []:
                    for state in (s.state, s.last_state):
                        t = state.terminated if state else None
                        if t and t.exit_code != 0 and t.finished_at:
                            out.append(
                                {
                                    "namespace": ns,
                                    "pod": p.metadata.name,
                                    "container": s.name,
                                    "reason": t.reason,
                                    "exit_code": t.exit_code,
                                    "finished_at": t.finished_at,
                                }
                            )
        return out

    def pod_logs(
        self,
        namespace: str,
        name: str,
        tail: int = 200,
        previous: bool = False,
        since_seconds: int | None = None,
    ) -> str:
        return self.core.read_namespaced_pod_log(
            name, namespace, tail_lines=tail, previous=previous, since_seconds=since_seconds
        )

    def consumer_lag(self, group: str) -> int:
        from bench.kafka_lag import parse_describe, total_lag

        return total_lag(parse_describe(self.consumer_group_describe(group)))

    # ---------- 写操作：注入和恢复用 ----------

    def scale_statefulset(self, namespace: str, name: str, replicas: int) -> None:
        kubectl(self.env, "scale", "statefulset", name, "-n", namespace, f"--replicas={replicas}")

    def clear_events(self, namespaces: list[str]) -> list[str]:
        """删除这些 namespace 里的 Event 对象（只是临时的诊断记录，不是业务数据）。

        每次运行注入之前清一次：k8s 的事件默认保留约 1 小时，不清的话，上一次运行的
        痕迹（比如“secret not found”）会通过 get_events 漏给下一次运行的 agent。
        """
        for ns in namespaces:
            kubectl(self.env, "delete", "events", "--all", "-n", ns)
        return list(namespaces)

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        kubectl(self.env, "scale", "deployment", name, "-n", namespace, f"--replicas={replicas}")

    def patch_deployment(self, namespace: str, name: str, patch: dict) -> None:
        """对 Deployment 做一次 strategic merge patch。容器按 name 合并，只改 patch 里写到的字段。"""
        kubectl(
            self.env,
            "patch",
            "deployment",
            name,
            "-n",
            namespace,
            "--type=strategic",
            "-p",
            json.dumps(patch),
            f"--field-manager={FIELD_MANAGER}",
        )

    def delete_pods(self, namespace: str, label_selector: str) -> None:
        """删除匹配的 Pod（不等它们真正消失）。Deployment 会自动重建。"""
        kubectl(self.env, "delete", "pod", "-l", label_selector, "-n", namespace, "--wait=false")

    def prom_value(self, query: str) -> float | None:
        """管理侧直接查一次 Prometheus 的即时查询，取第一条序列的值；没有数据返回 None。

        场景用它来确认“压力真的打上去了”（比如 CPU 打满）。这是管理侧的代码，
        不受 agent 的回看窗口限制，也不会出现在 agent 的工具里。
        """
        from urllib.parse import urlencode

        m = self.env.monitoring
        api = self.core.api_client
        url = (
            f"{api.configuration.host}/api/v1/namespaces/{m.namespace}"
            f"/services/{m.name}:{m.port}/proxy/api/v1/query?{urlencode({'query': query})}"
        )
        headers = {"Accept": "application/json"}
        auth = api.configuration.auth_settings().get("BearerToken") or {}
        if auth.get("value"):
            headers[auth["key"]] = auth["value"]
        resp = api.call_api("GET", url, header_params=headers, _request_timeout=15)
        body = resp.data if resp.data is not None else resp.read()
        result = json.loads(body)["data"]["result"]
        return float(result[0]["value"][1]) if result else None

    def wait_pods_deleted(self, namespace: str, label_selector: str, timeout_s: int = 90) -> None:
        kubectl(
            self.env,
            "wait",
            "--for=delete",
            "pod",
            "-l",
            label_selector,
            "-n",
            namespace,
            f"--timeout={timeout_s}s",
        )

    def set_deployment_env(
        self, namespace: str, name: str, container: str, var: str, value: str
    ) -> None:
        # strategic merge patch：env 列表按 name 合并，只改这一个变量，不动其他字段
        patch = json.dumps(
            {"spec": {"template": {"spec": {"containers": [
                {"name": container, "env": [{"name": var, "value": value}]}
            ]}}}}
        )  # fmt: skip
        kubectl(
            self.env,
            "patch",
            "deployment",
            name,
            "-n",
            namespace,
            "--type=strategic",
            "-p",
            patch,
            f"--field-manager={FIELD_MANAGER}",
        )

    def rollout_restart(self, namespace: str, deployment: str) -> None:
        kubectl(self.env, "rollout", "restart", f"deployment/{deployment}", "-n", namespace)

    def apply_manifest(self, path: str) -> None:
        """apply 一个 manifest 文件。只读取 music-streaming 仓库里的文件，不修改它。"""
        kubectl(self.env, "apply", f"--field-manager={FIELD_MANAGER}", "-f", path)

    def secret_exists(self, namespace: str, name: str) -> bool:
        try:
            self.core.read_namespaced_secret(name, namespace)
            return True
        except client.ApiException as e:
            if e.status == 404:
                return False
            raise

    def delete_secret(self, namespace: str, name: str) -> None:
        kubectl(self.env, "delete", "secret", name, "-n", namespace, "--ignore-not-found")

    def copy_secret_from_source(self) -> None:
        """按 music-streaming README 的方式，从 data 里的源 Secret 重建目标 Secret。

        直接在内存里把源 Secret 的 data 原样（仍是 base64）复制过去：密码不落盘，
        也不会出现在任何命令行参数里（命令行参数其他进程能看到）。
        目标 Secret 已经存在时什么都不做，所以 restore 可以重复执行。
        """
        src, dst = self.env.secret_source, self.env.secret_target
        if self.secret_exists(dst.namespace, dst.name):
            return
        source = self.core.read_namespaced_secret(src.name, src.namespace)
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=dst.name, namespace=dst.namespace),
            type="Opaque",
            data={dst.key: source.data[src.key]},
        )
        self.core.create_namespaced_secret(dst.namespace, body, field_manager=FIELD_MANAGER)
