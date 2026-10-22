"""流量发生器的部署 / 卸载。

脚本本身（gen.py）通过 ConfigMap 挂进 python:3.12-slim，不需要构建镜像。
这里用 kubectl 而不是 kubernetes 客户端来 apply，是因为 `kubectl apply` 对“已存在就更新”
的处理最直接，代码也最短。
"""

from pathlib import Path

from bench.admin_k8s import kubectl as _kubectl
from bench.config import EnvConfig

HERE = Path(__file__).parent


def render_manifest(env: EnvConfig) -> str:
    text = (HERE / "manifest.yaml").read_text(encoding="utf-8")
    t = env.traffic
    for key, value in {
        "__NAMESPACE__": env.bench_namespace,
        "__NAME__": t.name,
        "__PORT__": str(t.port),
        "__IMAGE__": t.image,
        "__INGEST_URL__": env.ingestion_url,
        "__QUERY_URL__": env.query_url,
    }.items():
        text = text.replace(key, value)
    return text


def up(env: EnvConfig) -> None:
    # namespace 和 Deployment 先 apply，ConfigMap 随后更新；脚本变化时需要重启 Pod 才生效
    _kubectl(env, "apply", "--field-manager=ops-tool", "-f", "-", stdin=render_manifest(env))
    cm = _kubectl(
        env,
        "create",
        "configmap",
        f"{env.traffic.name}-script",
        "-n",
        env.bench_namespace,
        f"--from-file=gen.py={HERE / 'gen.py'}",
        "--dry-run=client",
        "-o",
        "yaml",
    )
    _kubectl(env, "apply", "--field-manager=ops-tool", "-f", "-", stdin=cm)
    _kubectl(env, "rollout", "restart", f"deployment/{env.traffic.name}", "-n", env.bench_namespace)
    _kubectl(
        env,
        "rollout",
        "status",
        f"deployment/{env.traffic.name}",
        "-n",
        env.bench_namespace,
        "--timeout=180s",
    )


def down(env: EnvConfig) -> None:
    _kubectl(env, "delete", "namespace", env.bench_namespace, "--ignore-not-found")
