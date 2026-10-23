"""集群内 Prometheus 的部署 / 卸载（管理侧）。

和 traffic/ 一样用 kubectl apply：命令一眼能看懂，读代码的人可以直接复制到终端复现。
不改动 music-streaming 仓库，也不碰它的 docker-compose。
"""

from pathlib import Path

from bench.admin_k8s import kubectl
from bench.config import EnvConfig

HERE = Path(__file__).parent


def render_manifest(env: EnvConfig) -> str:
    text = (HERE / "manifest.yaml").read_text(encoding="utf-8")
    m = env.monitoring
    # agent 可见的三个 namespace 之外的容器指标一律不要（bench、monitoring、kube-system）
    visible = "|".join(env.agent_visible_namespaces)
    for key, value in {
        "__NAMESPACE__": m.namespace,
        "__NAME__": m.name,
        "__PORT__": str(m.port),
        "__IMAGE__": m.image,
        "__RETENTION__": m.retention,
        "__APP_NAMESPACE__": "default",
        "__VISIBLE_NAMESPACES__": visible,
    }.items():
        text = text.replace(key, value)
    return text


def up(env: EnvConfig) -> None:
    m = env.monitoring
    kubectl(env, "apply", "--field-manager=ops-tool", "-f", "-", stdin=render_manifest(env))
    # 配置变了需要重启 Pod 才生效
    kubectl(env, "rollout", "restart", f"deployment/{m.name}", "-n", m.namespace)
    kubectl(env, "rollout", "status", f"deployment/{m.name}", "-n", m.namespace, "--timeout=180s")


def down(env: EnvConfig) -> None:
    kubectl(env, "delete", "namespace", env.monitoring.namespace, "--ignore-not-found")
    kubectl(
        env, "delete", "clusterrole,clusterrolebinding", env.monitoring.name, "--ignore-not-found"
    )
