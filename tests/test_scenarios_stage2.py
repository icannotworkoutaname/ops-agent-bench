"""第 2 阶段的 11 个场景：清单、特征字符串、各场景的症状判断逻辑（用假集群）、稳定性测试流程。"""

from datetime import UTC, datetime, timedelta

import pytest

from bench.agent.diagnosis import CAUSE_CATEGORIES, COMPONENTS
from bench.config import load_env, load_run
from bench.scenarios import SCENARIOS
from bench.scenarios.base import Outcome, Scenario
from bench.stability import run_stability, summarize

# docs/plan/stage-2.md 第 3 节的场景清单（标准答案是仓库所有者定的，改动前必须先问）
EXPECTED = {
    "redis-down": ("dependency_unavailable", "redis"),
    "bad-kafka-bootstrap": ("misconfiguration_env", "stream-processor"),
    "missing-pg-secret": ("missing_secret_or_config", "query-service"),
    "sp-pod-killed": ("transient_restart_no_action", "stream-processor"),
    "sp-cpu-throttle": ("resource_limit_cpu", "stream-processor"),
    "sp-oom": ("resource_limit_memory", "stream-processor"),
    "kafka-down": ("dependency_unavailable", "kafka"),
    "postgres-down": ("dependency_unavailable", "postgresql"),
    "query-probe-misconfig": ("probe_misconfiguration", "query-service"),
    "ingestion-overload": ("load_saturation", "ingestion-service"),
    "no-fault": ("no_fault", "none"),
}


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """wait_until 轮询之间会 sleep；测试里不需要真的等。"""
    monkeypatch.setattr("bench.scenarios.base.time.sleep", lambda seconds: None)


def test_the_scenario_list_and_ground_truths_match_the_plan():
    assert list(SCENARIOS) == list(EXPECTED)
    for sid, cls in SCENARIOS.items():
        assert cls.id == sid
        assert (cls.ground_truth.cause_category, cls.ground_truth.component) == EXPECTED[sid]


def test_ground_truths_use_the_fixed_taxonomy():
    for cls in SCENARIOS.values():
        assert cls.ground_truth.cause_category in CAUSE_CATEGORIES
        assert cls.ground_truth.component in COMPONENTS


def test_signatures_never_collide_between_scenarios():
    """某个场景的特征字符串如果是另一个场景输出的一部分，污染检查就会误报。"""
    pairs = [(sid, s) for sid, cls in SCENARIOS.items() for s in cls.signatures]
    for sid_a, a in pairs:
        for sid_b, b in pairs:
            if sid_a != sid_b:
                assert a not in b, f"{sid_a} 的 {a!r} 是 {sid_b} 的 {b!r} 的一部分"


def test_signatures_do_not_contain_scenario_ids_or_look_like_healthy_output():
    healthy_samples = [
        "redis-master-0",
        "kafka-controller-0",
        "postgres-postgresql-0",
        "REDIS_HOST=redis-master.data.svc.cluster.local",
        "KAFKA_BOOTSTRAP_SERVERS=kafka.kafka.svc.cluster.local:9092",
        "POSTGRES_HOST=postgres-postgresql.data.svc.cluster.local",
        "readinessProbe: httpGet /actuator/health :8082",
        "requests={'cpu': '250m', 'memory': '256Mi'} limits={'cpu': '500m', 'memory': '512Mi'}",
        "limits={'cpu': '150m', 'ephemeral-storage': '2Gi', 'memory': '192Mi'}",
    ]
    for cls in SCENARIOS.values():
        for sig in cls.signatures:
            assert not any(sid in sig for sid in SCENARIOS)
            for sample in healthy_samples:
                assert sig not in sample, (
                    f"{cls.id} 的特征字符串 {sig!r} 出现在健康输出 {sample!r} 里"
                )


def test_scenarios_that_need_parameters_have_them_in_the_config():
    params = load_run()["scenario_params"]
    assert {"min_lag_growth"} <= set(params["sp-pod-killed"])
    assert {
        "cpu_limit",
        "cpu_request",
        "traffic_rate",
        "min_lag_growth",
        "min_throttle_ratio",
    } <= set(params["sp-cpu-throttle"])
    assert {"memory_limit"} <= set(params["sp-oom"])
    assert {"min_lag_growth"} <= set(params["postgres-down"])
    assert {"bad_path", "min_running_seconds"} <= set(params["query-probe-misconfig"])
    assert {"traffic_rate", "min_cpu_cores"} <= set(params["ingestion-overload"])


def test_every_signature_has_a_reason_it_is_specific():
    # 没有独有字符串的场景要明确写成空元组，而不是漏写
    for cls in SCENARIOS.values():
        assert isinstance(cls.signatures, tuple)


# ---------- 症状判断逻辑（假集群）----------


class FakeK8s:
    def __init__(self):
        self.lags = {"stream-processor": [10, 40], "query-service": [5, 30]}
        self.pods: list[dict] = []
        self.stats = {"write": {"fail": 0, "avg_ms": 5, "last_error": ""}, "query": {"fail": 0}}
        self.sts_ready = (True, "ready 1/1")
        self.prom = None
        self.calls: list = []

    def consumer_lag(self, group):
        seq = self.lags[group]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def pods_info(self, namespace, selector):
        return self.pods

    def traffic_stats(self, window):
        return self.stats

    log_text = ""

    def pod_logs(self, namespace, name, tail=200, previous=False, since_seconds=None):
        return self.log_text

    def statefulset_ready(self, namespace, name):
        return self.sts_ready

    def prom_value(self, query):
        return self.prom

    def set_traffic_rate(self, rate):
        self.calls.append(("rate", rate))

    def scale_deployment(self, namespace, name, replicas):
        self.calls.append(("scale", name, replicas))

    def wait_pods_deleted(self, namespace, selector):
        self.calls.append(("wait_deleted", selector))

    def patch_deployment(self, namespace, name, patch):
        self.calls.append(("patch", name, patch))

    def apply_manifest(self, path):
        self.calls.append(("apply", path.rsplit("/", 1)[-1]))

    def delete_pods(self, namespace, selector):
        self.calls.append(("delete_pods", selector))

    def scale_statefulset(self, namespace, name, replicas):
        self.calls.append(("scale_sts", name, replicas))


def make(sid, k8s=None):
    scenario = SCENARIOS[sid](load_env(), load_run(), k8s or FakeK8s())
    scenario.mark_injected()
    return scenario


def pod(**over):
    base = {
        "name": "p-1",
        "created": datetime.now(UTC) - timedelta(seconds=120),
        "phase": "Running",
        "terminating": False,
        "ready": False,
        "waiting_reasons": [],
        "last_terminated": [],
        "restarts": 0,
    }
    return {**base, **over}


def test_sp_pod_killed_needs_no_ready_instance_and_growing_lag():
    k8s = FakeK8s()
    k8s.lags["stream-processor"] = [10, 40]
    k8s.pods = [pod(ready=False)]
    assert make("sp-pod-killed", k8s).verify_injected(5).ok


def test_sp_pod_killed_a_ready_instance_means_no_symptom():
    k8s = FakeK8s()
    k8s.pods = [pod(ready=True)]
    out = make("sp-pod-killed", k8s).verify_injected(0)
    assert not out.ok and "可用实例 1/1" in out.detail


def test_sp_pod_killed_ignores_terminating_pods_that_still_look_ready():
    k8s = FakeK8s()
    k8s.pods = [pod(ready=True, terminating=True), pod(name="p-2", ready=False)]
    assert make("sp-pod-killed", k8s).verify_injected(5).ok


def test_sp_pod_killed_restore_does_nothing():
    k8s = FakeK8s()
    make("sp-pod-killed", k8s).restore()
    assert k8s.calls == []


def test_sp_cpu_throttle_sets_traffic_first_then_redeploys_with_small_limits():
    k8s = FakeK8s()
    make("sp-cpu-throttle", k8s).inject()
    kinds = [c[0] for c in k8s.calls]
    assert kinds == ["rate", "scale", "wait_deleted", "patch", "scale"]
    patch = k8s.calls[3][2]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert patch["limits"]["cpu"] == "100m" and patch["requests"]["cpu"] == "50m"


def test_sp_cpu_throttle_symptom_needs_both_lag_growth_and_a_high_throttle_ratio():
    k8s = FakeK8s()
    k8s.lags["stream-processor"] = [100, 5000]
    k8s.pods = [pod(ready=False)]
    k8s.prom = 0.9
    assert make("sp-cpu-throttle", k8s).verify_injected(5).ok

    # 只是停机：lag 涨了，但没有被限流 -> 不能算 cpu 限流的症状
    k8s = FakeK8s()
    k8s.lags["stream-processor"] = [100, 5000]
    k8s.pods = [pod(ready=False)]
    k8s.prom = 0.02
    assert not make("sp-cpu-throttle", k8s).verify_injected(0).ok

    # Prometheus 还没有数据：不能当成功
    k8s = FakeK8s()
    k8s.lags["stream-processor"] = [100, 5000]
    k8s.pods = [pod(ready=False)]
    k8s.prom = None
    assert not make("sp-cpu-throttle", k8s).verify_injected(0).ok


def test_sp_cpu_throttle_restore_reapplies_the_manifest_and_resets_the_rate():
    k8s = FakeK8s()
    make("sp-cpu-throttle", k8s).restore()
    assert ("apply", "stream-processor.yaml") in k8s.calls
    assert k8s.calls[-1] == ("rate", load_env().traffic.default_rate)


def test_sp_oom_detects_crash_loop_and_reports_how_the_container_died():
    k8s = FakeK8s()
    k8s.pods = [
        pod(
            waiting_reasons=["CrashLoopBackOff"],
            restarts=3,
            last_terminated=["OOMKilled(exit 137)"],
        )
    ]
    out = make("sp-oom", k8s).verify_injected(0)
    assert out.ok and "OOMKilled(exit 137)" in out.detail


def test_sp_oom_a_pod_that_is_simply_starting_is_not_a_symptom():
    k8s = FakeK8s()
    k8s.pods = [pod(ready=False)]
    assert not make("sp-oom", k8s).verify_injected(0).ok


def test_sp_oom_limits_memory_requests_and_limits_together():
    k8s = FakeK8s()
    make("sp-oom", k8s).inject()
    # 调用顺序: scale 0 -> 等旧 Pod 消失 -> patch -> scale 1，patch 是第 3 个
    assert [c[0] for c in k8s.calls] == ["scale", "wait_deleted", "patch", "scale"]
    res = k8s.calls[2][2]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res["limits"]["memory"] == res["requests"]["memory"] == "128Mi"


def test_kafka_down_needs_kafka_gone_and_connection_errors_in_the_service_logs():
    # 实测：Kafka 停掉后写入请求仍返回 202，症状只在日志里（见 kafka_down.py 的说明）
    k8s = FakeK8s()
    k8s.pods = [pod(ready=True)]  # 三个服务的 Pod 都保持 Ready
    k8s.log_text = "WARN NetworkClient : Error connecting to node kafka-controller-0:9092"
    k8s.sts_ready = (False, "ready 0/0")
    out = make("kafka-down", k8s).verify_injected(0)
    assert out.ok and "ingestion-service" in out.detail


def test_kafka_down_without_log_errors_is_not_a_symptom():
    k8s = FakeK8s()
    k8s.pods = [pod(ready=True)]
    k8s.log_text = "INFO normal line"
    k8s.sts_ready = (False, "ready 0/0")
    assert not make("kafka-down", k8s).verify_injected(0).ok


def test_kafka_down_with_kafka_still_running_is_not_a_symptom():
    k8s = FakeK8s()
    k8s.pods = [pod(ready=True)]
    k8s.log_text = "Error connecting to node kafka-controller-0"
    k8s.sts_ready = (True, "ready 1/1")
    assert not make("kafka-down", k8s).verify_injected(0).ok


def test_kafka_down_only_reads_logs_written_after_the_injection():
    seen = []

    class Recording(FakeK8s):
        def pod_logs(self, namespace, name, tail=200, previous=False, since_seconds=None):
            seen.append(since_seconds)
            return "Error connecting to node kafka-controller-0"

    k8s = Recording()
    k8s.pods = [pod(ready=True)]
    k8s.sts_ready = (False, "ready 0/0")
    make("kafka-down", k8s).verify_injected(0)
    assert seen and all(s is not None and s < 30 for s in seen)


def test_kafka_down_scales_the_kafka_statefulset():
    k8s = FakeK8s()
    s = make("kafka-down", k8s)
    s.inject()
    s.restore()
    assert k8s.calls == [("scale_sts", "kafka-controller", 0), ("scale_sts", "kafka-controller", 1)]


def test_postgres_down_needs_lag_growth_and_postgres_not_ready():
    k8s = FakeK8s()
    k8s.lags["query-service"] = [5, 30]
    k8s.sts_ready = (False, "ready 0/0")
    assert make("postgres-down", k8s).verify_injected(5).ok


def test_query_probe_misconfig_needs_a_running_but_never_ready_pod_and_failed_queries():
    k8s = FakeK8s()
    k8s.pods = [pod(ready=False, phase="Running", restarts=0)]
    k8s.stats = {"write": {"fail": 0, "avg_ms": 0, "last_error": ""}, "query": {"fail": 4}}
    assert make("query-probe-misconfig", k8s).verify_injected(0).ok


def test_query_probe_misconfig_a_pod_that_is_still_starting_is_not_yet_a_symptom():
    k8s = FakeK8s()
    k8s.pods = [pod(ready=False, created=datetime.now(UTC) - timedelta(seconds=10))]
    k8s.stats = {"write": {"fail": 0, "avg_ms": 0, "last_error": ""}, "query": {"fail": 4}}
    assert not make("query-probe-misconfig", k8s).verify_injected(0).ok


def test_query_probe_misconfig_patches_only_the_probe_path():
    k8s = FakeK8s()
    make("query-probe-misconfig", k8s).inject()
    assert [c[0] for c in k8s.calls] == ["scale", "wait_deleted", "patch", "scale"]
    container = k8s.calls[2][2]["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/actuator/healthz"


def test_ingestion_overload_requires_high_cpu_from_prometheus():
    k8s = FakeK8s()
    k8s.prom = 0.48
    assert make("ingestion-overload", k8s).verify_injected(0).ok
    k8s.prom = 0.1
    assert not make("ingestion-overload", k8s).verify_injected(0).ok
    k8s.prom = None  # Prometheus 暂时没有数据：不能当成成功
    assert not make("ingestion-overload", k8s).verify_injected(0).ok


def test_ingestion_overload_only_changes_the_traffic_rate():
    k8s = FakeK8s()
    s = make("ingestion-overload", k8s)
    s.inject()
    s.restore()
    assert k8s.calls == [("rate", 600), ("rate", load_env().traffic.default_rate)]


def test_no_fault_injects_nothing_and_always_has_its_baseline():
    k8s = FakeK8s()
    s = make("no-fault", k8s)
    s.inject()
    assert s.verify_injected(0).ok
    s.restore()
    assert k8s.calls == []


# ---------- 稳定性测试流程 ----------


class StubScenario(Scenario):
    id = "stub"
    ground_truth = SCENARIOS["no-fault"].ground_truth

    def __init__(self, symptom=True, restore=True):
        self.symptom, self.restore_ok = symptom, restore
        self.log: list[str] = []

    def inject(self):
        self.log.append("inject")

    def verify_injected(self, timeout):
        self.log.append("verify_injected")
        return Outcome(self.symptom, "d")

    def restore(self):
        self.log.append("restore")

    def verify_restored(self, timeout):
        self.log.append("verify_restored")
        return Outcome(self.restore_ok, "d")


def test_stability_runs_every_round_and_counts_successes():
    made: list[StubScenario] = []

    def factory():
        made.append(StubScenario())
        return made[-1]

    results = run_stability(factory, 3, load_run(), lambda: True)
    summary = summarize(results)
    assert len(results) == 3 and summary["symptom_ok"] == 3 and summary["restore_ok"] == 3
    assert made[0].log == ["inject", "verify_injected", "restore", "verify_restored"]


def test_stability_a_missing_symptom_still_restores_and_is_counted():
    results = run_stability(lambda: StubScenario(symptom=False), 2, load_run(), lambda: True)
    summary = summarize(results)
    assert summary["symptom_ok"] == 0 and summary["restore_ok"] == 2
    assert summary["avg_inject_to_symptom_seconds"] is None


def test_stability_stops_immediately_when_a_restore_fails():
    results = run_stability(lambda: StubScenario(restore=False), 3, load_run(), lambda: True)
    assert len(results) == 1 and not results[0].restore_ok


def test_stability_stops_when_the_environment_is_not_healthy_at_the_start():
    results = run_stability(lambda: StubScenario(), 3, load_run(), lambda: False)
    assert results == []


@pytest.mark.parametrize("sid", list(EXPECTED))
def test_every_scenario_can_be_constructed_without_a_cluster(sid):
    assert make(sid).id == sid
