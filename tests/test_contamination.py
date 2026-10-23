from bench.contamination import find_contamination
from bench.scenarios import SCENARIOS

SIGS = {sid: cls.signatures for sid, cls in SCENARIOS.items()}


def call(result: str, name: str = "get_pod_logs", step: int = 1) -> dict:
    return {"name": name, "step": step, "input": {"namespace": "default"}, "result": result}


def test_clean_output_has_no_hits():
    out = "NAME READY STATUS\nredis-master-0 1/1 Running\nquery-service-abc 1/1 Running"
    assert find_contamination([call(out)], "redis-down", SIGS) == []


def test_other_scenarios_signature_is_flagged():
    line = 'Error: secret "postgres-credentials" not found'
    hits = find_contamination([call(line)], "redis-down", SIGS)
    assert [h["from_scenario"] for h in hits] == ["missing-pg-secret"]
    assert hits[0]["kind"] == "other_scenario_signature" and line[:20] in hits[0]["excerpt"]


def test_own_scenario_signature_is_not_contamination():
    line = 'Error: secret "postgres-credentials" not found'
    assert find_contamination([call(line)], "missing-pg-secret", SIGS) == []


def test_every_scenario_id_is_flagged_even_the_own_one():
    # 场景 id 绝不该出现在集群里，哪怕是本场景自己的
    hits = find_contamination([call("labels: scenario=redis-down")], "redis-down", SIGS)
    assert any(h["kind"] == "scenario_id" and h["needle"] == "redis-down" for h in hits)


def test_harness_terms_are_flagged():
    hits = find_contamination([call("managedFields manager: ops-tool")], "redis-down", SIGS)
    assert any(h["kind"] == "harness_term" for h in hits)


def test_the_models_own_submission_is_ignored():
    submit = call("redis-down, CreateContainerConfigError", name="submit_diagnosis")
    assert find_contamination([submit], "redis-down", SIGS) == []


def test_no_fault_run_treats_every_signature_as_foreign():
    # 无故障对照场景没有自己的特征字符串，任何场景的痕迹都算污染
    sigs = {**SIGS, "no-fault": ()}
    hits = find_contamination([call("No resolvable bootstrap urls given")], "no-fault", sigs)
    assert [h["from_scenario"] for h in hits] == ["bad-kafka-bootstrap"]


def test_signatures_are_specific_enough_not_to_match_a_healthy_environment():
    healthy = (
        "NAME READY STATUS RESTARTS AGE\nredis-master-0 1/1 Running 0 3d\n"
        "stream-processor-75cb6f4db-x 1/1 Running 0 3d\n"
        "REDIS_HOST=redis-master.data.svc.cluster.local\n"
        "KAFKA_BOOTSTRAP_SERVERS=kafka.kafka.svc.cluster.local:9092\n"
        "POSTGRES_PASSWORD <- secret postgres-credentials (key postgres-password)"
    )
    assert find_contamination([call(healthy)], "no-fault", {**SIGS, "no-fault": ()}) == []
