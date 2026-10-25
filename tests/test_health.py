"""健康检查的判断逻辑。用假的 k8s 客户端，不需要集群。"""

from datetime import UTC, datetime, timedelta

from bench.health import (
    _safe,
    check_consumer_lag,
    check_control_plane,
    check_recent_terminations,
    check_traffic,
)

HEADER = "GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG CONSUMER-ID HOST CLIENT-ID\n"

CFG = {
    "traffic_window_seconds": 20,
    "min_write_ok": 5,
    "min_query_ok": 2,
    "max_write_fail": 0,
    "max_query_fail": 0,
}


class FakeK8s:
    def __init__(self, describe: str = "", stats: dict | None = None):
        self._describe = describe
        self._stats = stats

    def consumer_group_describe(self, group: str) -> str:
        return self._describe

    def traffic_stats(self, window_seconds: int) -> dict:
        return self._stats


def stats(write_ok=10, write_fail=0, query_ok=4, query_fail=0) -> dict:
    return {
        "window_seconds": 20,
        "write": {"ok": write_ok, "fail": write_fail, "last_error": "boom" if write_fail else ""},
        "query": {"ok": query_ok, "fail": query_fail, "last_error": "boom" if query_fail else ""},
    }


def test_lag_below_threshold_is_healthy():
    k8s = FakeK8s(describe=HEADER + "g t 0 10 12 2 - - -\n")
    ok, _ = check_consumer_lag(k8s, "g", max_lag=50)
    assert ok


def test_lag_above_threshold_is_unhealthy():
    k8s = FakeK8s(describe=HEADER + "g t 0 10 200 190 - - -\n")
    ok, detail = check_consumer_lag(k8s, "g", max_lag=50)
    assert not ok
    assert "190" in detail


def test_missing_group_is_unhealthy_not_zero_lag():
    # describe 没有返回任何分区时不能当作“lag 为 0”，否则组不存在也会显示健康
    ok, _ = check_consumer_lag(FakeK8s(describe="Error: group does not exist"), "g", max_lag=50)
    assert not ok


def test_unknown_lag_partition_is_unhealthy():
    k8s = FakeK8s(describe=HEADER + "g t 0 - 12 - - - -\n")
    ok, _ = check_consumer_lag(k8s, "g", max_lag=50)
    assert not ok


def test_traffic_healthy():
    ok, _ = check_traffic(FakeK8s(stats=stats()), CFG)
    assert ok


def test_traffic_write_failures_reported_with_last_error():
    ok, detail = check_traffic(FakeK8s(stats=stats(write_fail=3)), CFG)
    assert not ok
    assert "boom" in detail


def test_traffic_too_few_successes_is_unhealthy():
    # 流量发生器刚启动或根本没在发时，“没有失败”不等于“链路正常”
    ok, _ = check_traffic(FakeK8s(stats=stats(write_ok=0, query_ok=0)), CFG)
    assert not ok


class ControlPlaneK8s:
    def __init__(self, counts):
        self.counts = counts
        self.window = None

    def control_plane_auth_errors(self, window_seconds):
        self.window = window_seconds
        return self.counts


CP_CFG = {"control_plane_window_seconds": 300, "max_control_plane_auth_errors": 0}


def test_control_plane_without_auth_errors_is_healthy():
    k8s = ControlPlaneK8s({"kube-controller-manager": 0, "kube-apiserver": 0})
    ok, _ = check_control_plane(k8s, CP_CFG)
    assert ok and k8s.window == 300


def test_control_plane_auth_errors_make_the_environment_unhealthy():
    k8s = ControlPlaneK8s({"kube-controller-manager": 2, "kube-apiserver": 0})
    ok, detail = check_control_plane(k8s, CP_CFG)
    assert not ok and "重启 minikube" in detail


def test_safe_turns_exceptions_into_failed_check():
    def boom():
        raise RuntimeError("连不上")

    check = _safe("x", boom)
    assert not check.ok
    assert "连不上" in check.detail


# ---------- 冷却期：最近有没有容器异常终止 ----------

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


class TerminationsK8s:
    def __init__(self, items: list[dict]):
        self.items = items

    def abnormal_terminations(self, namespaces: list[str]) -> list[dict]:
        return self.items


def termination(seconds_ago: float, reason="OOMKilled", exit_code=137) -> dict:
    return {
        "namespace": "kafka",
        "pod": "kafka-controller-0",
        "container": "kafka",
        "reason": reason,
        "exit_code": exit_code,
        "finished_at": NOW - timedelta(seconds=seconds_ago),
    }


def test_termination_inside_the_window_is_unhealthy_and_named():
    k8s = TerminationsK8s([termination(120)])
    ok, detail = check_recent_terminations(k8s, ["kafka"], 600, now=NOW)
    assert not ok
    assert "kafka/kafka-controller-0/kafka" in detail and "OOMKilled" in detail and "120s" in detail


def test_termination_older_than_the_window_is_ignored():
    # 这是试跑里的真实情况：Kafka 15 分钟前 OOMKilled，lastState 还留着，但已经滑出 10 分钟窗口
    k8s = TerminationsK8s([termination(15 * 60)])
    ok, _ = check_recent_terminations(k8s, ["kafka"], 600, now=NOW)
    assert ok


def test_no_terminations_is_healthy():
    ok, _ = check_recent_terminations(TerminationsK8s([]), ["kafka"], 600, now=NOW)
    assert ok
