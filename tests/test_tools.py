"""agent 只读工具的单元测试：不需要集群。

重点是权限边界：namespace 白名单、输出截断、Secret 值不外泄、
工具只读、tools/ 不依赖管理用的 k8s 客户端、get_consumer_lag 只能执行固定命令。
"""

import ast
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kubernetes import client
from kubernetes.client import ApiException

from bench.config import KafkaConfig
from bench.tools import common
from bench.tools.common import ToolError, as_tool, check_namespace, truncate
from bench.tools.k8s_read import (
    K8sReader,
    format_deployment,
    format_events,
    format_pod_describe,
    format_pod_list,
)
from bench.tools.kafka_read import build_lag_command, format_lag

TOOLS_DIR = Path(common.__file__).parent
ALLOWED = ["default", "kafka", "data"]
NOW = datetime.now(UTC)


# ---------- namespace 白名单 ----------


@pytest.mark.parametrize("ns", ["default", "kafka", "data"])
def test_allowed_namespaces_pass(ns):
    check_namespace(ns, ALLOWED)


@pytest.mark.parametrize("ns", ["kube-system", "bench", "", "default ", "DEFAULT", "default,bench"])
def test_other_namespaces_are_rejected(ns):
    with pytest.raises(ToolError) as e:
        check_namespace(ns, ALLOWED)
    assert "default, kafka, data" in str(e.value)


def make_reader(read_log=None) -> K8sReader:
    """不连集群的 K8sReader：用假的 core API 记录被调用的参数。"""
    reader = K8sReader.__new__(K8sReader)
    reader.allowed = ALLOWED
    reader.limits = {
        "default_log_tail_lines": 100,
        "max_log_tail_lines": 300,
        "max_events": 30,
        "lookback_minutes": 10,
    }

    class FakeCore:
        def __init__(self):
            self.calls = []
            self.since = []
            self.events = []

        def read_namespaced_pod_log(self, name, namespace, tail_lines, previous, since_seconds):
            self.calls.append((name, namespace, tail_lines, previous))
            self.since.append(since_seconds)
            return read_log(previous) if read_log else "line"

        def list_namespaced_event(self, namespace):
            return client.CoreV1EventList(items=self.events)

    reader.core = FakeCore()
    return reader


def test_disallowed_namespace_never_reaches_the_api():
    reader = make_reader()
    with pytest.raises(ToolError):
        reader.get_pod_logs("kube-system", "p", 10, False)
    assert reader.core.calls == []


def test_log_tail_is_capped_and_defaulted():
    reader = make_reader()
    reader.get_pod_logs("default", "p", 100000, False)
    reader.get_pod_logs("default", "p", None, False)
    assert [c[2] for c in reader.core.calls] == [300, 100]
    with pytest.raises(ToolError):
        reader.get_pod_logs("default", "p", 0, False)


def test_previous_log_missing_gives_clear_message():
    def boom(previous):
        raise ApiException(status=400, reason="Bad Request")

    reader = make_reader(read_log=boom)
    with pytest.raises(ToolError) as e:
        reader.get_pod_logs("default", "p", 10, True)
    assert "没有上一次" in str(e.value)


# ---------- 截断 ----------


def test_short_output_is_untouched():
    assert truncate("abc", 10) == "abc"


def test_truncation_states_original_length():
    out = truncate("x" * 5000, 100)
    assert out.startswith("x" * 100)
    assert "已截断" in out and "5000" in out


# ---------- 错误转文本，不抛异常 ----------


def test_as_tool_turns_errors_into_text():
    @as_tool(1000)
    def tool(kind: str) -> str:
        if kind == "tool":
            raise ToolError("参数不对")
        if kind == "404":
            raise ApiException(status=404, reason="Not Found")
        if kind == "403":
            raise ApiException(status=403, reason="Forbidden")
        raise RuntimeError("意外")

    assert tool("tool") == "错误: 参数不对"
    assert "不存在" in tool("404")
    assert "403" in tool("403")
    assert "RuntimeError" in tool("other")


def test_as_tool_truncates_results():
    @as_tool(50)
    def tool() -> str:
        return "y" * 500

    assert "已截断" in tool() and "500" in tool()


# ---------- Secret 值不外泄、工具只读 ----------


def deployment_with_secret_env():
    secret_ref = client.V1EnvVarSource(
        secret_key_ref=client.V1SecretKeySelector(
            name="postgres-credentials", key="postgres-password"
        )
    )
    container = client.V1Container(
        name="query-service",
        image="query-service:v1",
        env=[
            client.V1EnvVar(name="REDIS_HOST", value="redis-master.data.svc.cluster.local"),
            client.V1EnvVar(name="POSTGRES_PASSWORD", value_from=secret_ref),
        ],
    )
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name="query-service", namespace="default"),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(),
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=[container])),
        ),
        status=client.V1DeploymentStatus(),
    )


def test_secret_env_shows_reference_only():
    out = format_deployment(deployment_with_secret_env())
    assert "POSTGRES_PASSWORD <- secret postgres-credentials" in out
    assert "REDIS_HOST=redis-master.data.svc.cluster.local" in out  # 普通值照常显示


def test_tools_never_call_secret_apis_or_write_verbs():
    """直接扫描 tools/ 的源码：不碰 Secret 的 API，也没有任何写操作的调用。"""
    forbidden = [
        "read_namespaced_secret",
        "list_namespaced_secret",
        "list_secret_for_all_namespaces",
        ".create_",
        ".delete_",
        ".patch_",
        ".replace_",
    ]
    for path in TOOLS_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in source, f"{path.name} 里出现了 {word}"


# ---------- 隔离：tools/ 不依赖 admin_k8s ----------


def test_tools_do_not_import_admin_k8s_statically():
    for path in TOOLS_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            assert not any("admin_k8s" in n for n in names), f"{path.name} import 了 admin_k8s"


def test_tools_do_not_load_admin_k8s_at_runtime():
    """真正导入工具模块，看 admin_k8s 有没有被间接加载进来（server.py 导入时会连集群，所以不测它）。"""
    code = (
        "import sys; import bench.tools.common, bench.tools.k8s_read, bench.tools.kafka_read; "
        "sys.exit(1 if 'bench.admin_k8s' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# ---------- get_consumer_lag 只能执行固定命令 ----------

KAFKA = KafkaConfig(
    namespace="kafka",
    pod="kafka-controller-0",
    container="kafka",
    consumer_groups_script="/opt/bitnami/kafka/bin/kafka-consumer-groups.sh",
    bootstrap_server="localhost:9092",
    exec_timeout_seconds=30,
)


def test_lag_command_is_the_fixed_describe_command():
    cmd = build_lag_command(KAFKA, "stream-processor")
    assert cmd == [
        "/opt/bitnami/kafka/bin/kafka-consumer-groups.sh",
        "--bootstrap-server",
        "localhost:9092",
        "--describe",
        "--group",
        "stream-processor",
    ]


@pytest.mark.parametrize(
    "group",
    ["", "a b", "a;b", "$(id)", "`id`", "a|b", "../x", "a\nb", "--delete", "x" * 101, "g && ls"],
)
def test_lag_command_rejects_suspicious_group_names(group):
    # “--delete” 这类以连字符开头的值也要拒绝，否则会被脚本当成另一个选项
    with pytest.raises(ToolError):
        build_lag_command(KAFKA, group)


def test_lag_group_cannot_start_with_dash():
    with pytest.raises(ToolError):
        build_lag_command(KAFKA, "-x")


def test_lag_output_with_no_partitions_shows_raw_output():
    out = format_lag("nope", "Error: Consumer group 'nope' does not exist.")
    assert "does not exist" in out


# ---------- 格式化：给模型看的摘要要包含诊断需要的信息 ----------


def stuck_pod():
    waiting = client.V1ContainerStateWaiting(
        reason="CreateContainerConfigError", message='secret "postgres-credentials" not found'
    )
    status = client.V1ContainerStatus(
        name="query-service",
        image="query-service:v1",
        image_id="",
        ready=False,
        restart_count=0,
        state=client.V1ContainerState(waiting=waiting),
    )
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="query-service-abc", namespace="default", creation_timestamp=NOW
        ),
        spec=client.V1PodSpec(
            node_name="minikube",
            containers=[client.V1Container(name="query-service", image="query-service:v1")],
        ),
        status=client.V1PodStatus(phase="Pending", container_statuses=[status]),
    )


def test_stuck_pod_shows_reason_and_message_in_list_and_describe():
    assert "CreateContainerConfigError" in format_pod_list([stuck_pod()])
    described = format_pod_describe(stuck_pod())
    assert "CreateContainerConfigError" in described
    assert 'secret "postgres-credentials" not found' in described


def event(reason: str, seconds_ago: int):
    return client.CoreV1Event(
        metadata=client.V1ObjectMeta(name=reason, creation_timestamp=NOW),
        involved_object=client.V1ObjectReference(kind="Pod", name="p"),
        type="Warning",
        reason=reason,
        message="m",
        last_timestamp=datetime.fromtimestamp(NOW.timestamp() - seconds_ago, UTC),
    )


def test_events_are_newest_first_and_limited():
    events = [event("older", 300), event("new", 5), event("mid", 100)]
    out = format_events(events, limit=2, lookback_minutes=10)
    assert out.index("new") < out.index("mid")
    assert "older" not in out
    assert "窗口内还有 1 条更早的未显示" in out


# ---------- 回看窗口 W：只返回最近 W 分钟内的数据 ----------


def test_events_older_than_the_window_are_dropped():
    events = [event("stale", 11 * 60), event("fresh", 9 * 60)]
    out = format_events(events, limit=30, lookback_minutes=10)
    assert "fresh" in out and "stale" not in out


def test_the_window_edge_is_respected():
    # 显式传入 now：事件时间是相对 NOW 构造的，不能依赖真实时钟，否则在边界上会不稳定
    assert "edge" in format_events([event("edge", 599)], 30, 10, now=NOW)
    assert "edge" not in format_events([event("edge", 601)], 30, 10, now=NOW)


def test_old_events_are_not_counted_or_hinted_at():
    # 窗口外有多少条旧事件，不能透露给 agent：不然它知道“更早的地方有东西”
    out = format_events([event("stale1", 3600), event("stale2", 3700)], 30, 10)
    assert "stale" not in out and "2" not in out.replace("10", "")
    assert "最近 10 分钟内" in out


def test_event_tool_applies_the_configured_window():
    reader = make_reader()
    reader.core.events = [event("stale", 3600), event("fresh", 30)]
    out = reader.get_events("default")
    assert "fresh" in out and "stale" not in out


def test_logs_are_requested_with_both_since_and_tail():
    reader = make_reader()
    reader.get_pod_logs("default", "p", 50, False)
    assert reader.core.since == [10 * 60]  # 10 分钟 = 600 秒
    assert [c[2] for c in reader.core.calls] == [50]


def test_empty_logs_say_the_window_was_empty():
    reader = make_reader(read_log=lambda previous: "")
    assert "最近 10 分钟内没有日志" in reader.get_pod_logs("default", "p", 50, False)


def killed_pod(finished_seconds_ago: int):
    status = client.V1ContainerStatus(
        name="kafka",
        image="kafka:4",
        image_id="",
        ready=True,
        restart_count=1,
        state=client.V1ContainerState(running=client.V1ContainerStateRunning(started_at=NOW)),
        last_state=client.V1ContainerState(
            terminated=client.V1ContainerStateTerminated(
                exit_code=137,
                reason="OOMKilled",
                finished_at=NOW - timedelta(seconds=finished_seconds_ago),
            )
        ),
    )
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="kafka-controller-0", namespace="kafka", creation_timestamp=NOW
        ),
        spec=client.V1PodSpec(
            node_name="minikube", containers=[client.V1Container(name="kafka", image="kafka:4")]
        ),
        status=client.V1PodStatus(phase="Running", container_statuses=[status]),
    )


def test_describe_shows_recent_last_state():
    out = format_pod_describe(killed_pod(120), lookback_minutes=10, now=NOW)
    assert "OOMKilled" in out


def test_describe_hides_last_state_older_than_the_window_but_keeps_restart_count():
    # 回归：试跑里 Kafka 15 分钟前 OOMKilled，lastState 漏给了下一次运行的 agent
    out = format_pod_describe(killed_pod(15 * 60), lookback_minutes=10, now=NOW)
    assert "OOMKilled" not in out
    assert "回看窗口" in out
    assert "restarts=1" in out


def test_describe_without_a_window_is_unchanged():
    assert "OOMKilled" in format_pod_describe(killed_pod(15 * 60))
