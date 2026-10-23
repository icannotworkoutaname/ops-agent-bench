"""评分、计费、预检、运行记录、运行器编排、bench show。全部不需要集群，也不联网。"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bench.agent.fake import FakeModel
from bench.config import load_env, load_run
from bench.cooldown import CooldownResult
from bench.pricing import build_usage_report, is_peak, pricing_missing, request_cost
from bench.runlog import RunLog, cumulative_usage, read_transcript
from bench.runner import RunDeps, execute_run, preflight
from bench.scenarios.base import GroundTruth, Outcome, Scenario
from bench.scoring import score
from bench.show import render_run

TRUTH = GroundTruth("dependency_unavailable", "redis", "restore redis")

PEAK_CFG = {"weekdays_only": True, "windows_utc": [["01:00", "04:00"], ["06:00", "10:00"]]}
TIER = {"input_cache_miss": 1.0, "input_cache_hit": 0.1, "output": 4.0}
PRICING = {"peak": {k: v * 2 for k, v in TIER.items()}, "offpeak": TIER}
BILLING = {"currency": "CNY", "budget": 2.0, "max_real_runs": 15, "peak": PEAK_CFG}


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ---------- 打分 ----------


def test_score_checks_the_two_fields_independently():
    both = {"cause_category": "dependency_unavailable", "component": "redis"}
    assert score(both, TRUTH) == {"cause_category_correct": True, "component_correct": True}
    wrong_component = {**both, "component": "postgresql"}
    assert score(wrong_component, TRUTH) == {
        "cause_category_correct": True,
        "component_correct": False,
    }
    wrong_cause = {**both, "cause_category": "other"}
    assert score(wrong_cause, TRUTH) == {"cause_category_correct": False, "component_correct": True}


def test_no_diagnosis_scores_false_false():
    assert score(None, TRUTH) == {"cause_category_correct": False, "component_correct": False}


# ---------- 高峰判断 ----------


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("2026-10-26T01:00", True),  # 周一，窗口起点包含
        ("2026-10-26T03:59", True),
        ("2026-10-26T04:00", False),  # 窗口终点不包含
        ("2026-10-26T05:30", False),  # 两个窗口之间
        ("2026-10-26T06:00", True),
        ("2026-10-26T09:59", True),
        ("2026-10-26T10:00", False),
        ("2026-10-26T00:59", False),
        ("2026-10-31T02:00", False),  # 周六
        ("2026-11-01T07:00", False),  # 周日
    ],
)
def test_is_peak(when, expected):
    assert is_peak(utc(when), PEAK_CFG) is expected


def test_is_peak_converts_other_timezones_to_utc():
    from datetime import timedelta, timezone

    beijing = timezone(timedelta(hours=8))
    # 北京时间 09:30 = UTC 01:30，周一
    assert is_peak(datetime(2026, 10, 26, 9, 30, tzinfo=beijing), PEAK_CFG)


# ---------- 费用 ----------


def test_cost_prices_cached_uncached_and_output_separately():
    usage = {
        "input_tokens": 1_000_000,
        "cache_read_input_tokens": 2_000_000,
        "output_tokens": 500_000,
    }
    # 非高峰: 1*1 + 2*0.1 + 0.5*4 = 3.2 ；高峰翻倍 = 6.4
    assert request_cost(usage, False, PRICING) == pytest.approx(3.2)
    assert request_cost(usage, True, PRICING) == pytest.approx(6.4)


def test_missing_pricing_is_listed():
    entry = {"pricing": {"peak": {"output": 1}, "offpeak": {}}}
    missing = pricing_missing({"currency": None, "budget": None}, entry)
    assert "billing.currency" in missing and "billing.budget" in missing
    assert "pricing.peak.input_cache_miss" in missing
    assert "pricing.peak.output" not in missing
    assert pricing_missing(BILLING, {"pricing": PRICING}) == []


def make_events(sent_times: list[str]) -> list[dict]:
    events = []
    for i, t in enumerate(sent_times, start=1):
        events.append({"type": "request", "step": i, "sent_at": utc(t).isoformat()})
        events.append(
            {
                "type": "response",
                "step": i,
                "model": "m",
                "usage": {
                    "input_tokens": 1_000_000,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
            }
        )
    return events


def test_each_request_is_billed_by_its_own_send_time():
    # 同一次运行跨越高峰边界：第一步在高峰(03:59)，第二步在非高峰(04:00)
    events = make_events(["2026-10-26T03:59", "2026-10-26T04:00"])
    report = build_usage_report(events, {"pricing": PRICING}, BILLING, fake=False)
    assert [s["peak"] for s in report["steps"]] == [True, False]
    assert [s["cost"] for s in report["steps"]] == [pytest.approx(2.0), pytest.approx(1.0)]
    assert report["total"]["cost"] == pytest.approx(3.0)
    assert report["steps"][0]["usage"]["input_tokens"] == 1_000_000  # 原始 usage 原样保存


def test_fake_runs_are_not_priced():
    report = build_usage_report(make_events(["2026-10-26T03:59"]), {}, BILLING, fake=True)
    assert report["fake"] is True and report["total"]["cost"] is None


# ---------- 运行记录 ----------


def test_run_dirs_are_never_overwritten(tmp_path):
    first = RunLog(tmp_path, "s", "m")
    first.write_json("meta.json", {"a": 1})
    # 同一秒内再创建：目录名必须不同，已有内容不被覆盖
    second = RunLog(tmp_path, "s", "m")
    assert first.path != second.path
    assert json.loads((first.path / "meta.json").read_text()) == {"a": 1}


def test_events_are_written_one_line_each(tmp_path):
    log = RunLog(tmp_path, "s", "m")
    log.append_event({"type": "a", "text": "中文"})
    log.append_event({"type": "b"})
    assert [e["type"] for e in read_transcript(log.path)] == ["a", "b"]


def write_usage(runs: Path, name: str, fake: bool, steps: int, cost: float | None):
    d = runs / name
    d.mkdir(parents=True)
    (d / "usage.json").write_text(
        json.dumps({"fake": fake, "steps": [{}] * steps, "total": {"cost": cost}})
    )


def test_cumulative_usage_counts_only_real_runs_that_called_the_model(tmp_path):
    write_usage(tmp_path, "a", fake=False, steps=3, cost=0.5)
    write_usage(tmp_path, "b", fake=False, steps=2, cost=0.25)
    write_usage(tmp_path, "c", fake=True, steps=3, cost=None)  # 假模型：不计
    write_usage(tmp_path, "d", fake=False, steps=0, cost=0.0)  # 症状没出现，agent 没开始：不占次数
    assert cumulative_usage(tmp_path) == {"cost": 0.75, "real_runs": 2}


# ---------- 预检 ----------

MONDAY_NOON = utc("2026-10-26T12:00")  # 非高峰
MONDAY_PEAK = utc("2026-10-26T02:00")


def cfg_with(billing_over=None, pricing=PRICING) -> dict:
    billing = {**BILLING, **(billing_over or {})}
    models = {"m": {"model": "x", "api_key_env": "TEST_KEY_VAR", "pricing": pricing}}
    return {"billing": billing, "models": models}


def test_preflight_passes_when_everything_is_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY_VAR", "k")
    assert preflight(cfg_with(), "m", False, False, tmp_path, MONDAY_NOON) == []


def test_preflight_refuses_without_prices_or_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY_VAR", "k")
    cfg = cfg_with({"currency": None, "budget": None}, pricing={"peak": {}, "offpeak": {}})
    problems = preflight(cfg, "m", False, False, tmp_path, MONDAY_NOON)
    assert any("还没有填写" in p for p in problems)


def test_preflight_refuses_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_KEY_VAR", raising=False)
    problems = preflight(cfg_with(), "m", False, False, tmp_path, MONDAY_NOON)
    assert any("TEST_KEY_VAR" in p for p in problems)


def test_preflight_refuses_during_peak_unless_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY_VAR", "k")
    assert any("高峰" in p for p in preflight(cfg_with(), "m", False, False, tmp_path, MONDAY_PEAK))
    assert preflight(cfg_with(), "m", False, True, tmp_path, MONDAY_PEAK) == []


def test_preflight_refuses_over_budget_and_over_run_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY_VAR", "k")
    write_usage(tmp_path, "a", fake=False, steps=1, cost=2.5)
    problems = preflight(cfg_with(), "m", False, False, tmp_path, MONDAY_NOON)
    assert any("预算" in p for p in problems)

    cfg = cfg_with({"max_real_runs": 1, "budget": 100})
    assert any("次数" in p for p in preflight(cfg, "m", False, False, tmp_path, MONDAY_NOON))


def test_preflight_does_not_restrict_fake_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_KEY_VAR", raising=False)
    cfg = cfg_with({"currency": None, "budget": None}, pricing={})
    assert preflight(cfg, "m", True, False, tmp_path, MONDAY_PEAK) == []


def test_preflight_unknown_model(tmp_path):
    assert "未知模型" in preflight(cfg_with(), "nope", False, False, tmp_path, MONDAY_NOON)[0]


# ---------- 运行器编排（假场景 + 假模型 + 假工具来源） ----------


class FakeScenario(Scenario):
    id = "fake-scenario"
    ground_truth = TRUTH

    def __init__(self, calls, symptom_ok=True, restore_ok=True, restore_raises=False):
        self.calls = calls
        self.symptom_ok, self.restore_ok, self.restore_raises = (
            symptom_ok,
            restore_ok,
            restore_raises,
        )

    def inject(self):
        self.calls.append("inject")

    def verify_injected(self, timeout):
        self.calls.append("verify_injected")
        return Outcome(self.symptom_ok, "symptom detail")

    def restore(self):
        self.calls.append("restore")
        if self.restore_raises:
            raise RuntimeError("kubectl 挂了")

    def verify_restored(self, timeout):
        self.calls.append("verify_restored")
        return Outcome(self.restore_ok, "restore detail")


class FakeToolSource:
    """代替 MCP：同样是 async with，对外提供 definitions / call。"""

    def __init__(self, calls, fail_to_start=False, payload=None):
        self.calls, self.fail_to_start, self.payload = calls, fail_to_start, payload

    async def __aenter__(self):
        if self.fail_to_start:
            raise RuntimeError("工具进程起不来")
        self.calls.append("tools_start")
        return self

    async def __aexit__(self, *exc):
        self.calls.append("tools_stop")

    async def definitions(self):
        return [{"name": "list_pods", "description": "d", "input_schema": {"type": "object"}}]

    async def call(self, name, arguments):
        if self.payload:  # 让所有工具都返回指定内容，用来模拟“环境里有别的场景的痕迹”
            return self.payload, False
        return f"result of {name} {arguments}", False


def run_once(
    tmp_path,
    scenario_kwargs=None,
    healthy=True,
    tools_fail=False,
    runs_dir=None,
    cooldown=None,
    payload=None,
):
    calls: list[str] = []
    health = [
        (True, [{"name": "x", "ok": True, "detail": ""}]),
        (healthy, [{"name": "x", "ok": healthy, "detail": "bad" if not healthy else ""}]),
    ]
    first_health = (healthy, [{"name": "x", "ok": healthy, "detail": "" if healthy else "bad"}])

    def check_health():
        calls.append("health")
        return first_health if calls.count("health") == 1 else health[0]

    scenario = FakeScenario(calls, **(scenario_kwargs or {}))
    run_cfg = load_run()
    deps = RunDeps(
        env=load_env(),
        run_cfg=run_cfg,
        scenario=scenario,
        model_key="deepseek-flash",
        model=FakeModel(),
        tool_source=lambda: FakeToolSource(calls, tools_fail, payload),
        check_health=check_health,
        clear_events=lambda: calls.append("clear_events") or ["default"],
        log=RunLog(runs_dir or tmp_path, scenario.id, "deepseek-flash"),
        fake=True,
        cooldown=(lambda: cooldown(calls)) if cooldown else None,
    )
    outcome = asyncio.run(execute_run(deps))
    return outcome, calls


def test_happy_path_order_and_files(tmp_path):
    outcome, calls = run_once(tmp_path)

    assert calls == [
        "health",
        "clear_events",
        "inject",
        "verify_injected",
        "tools_start",
        "tools_stop",
        "restore",
        "verify_restored",
        "health",
    ]
    assert outcome.status == "diagnosed" and outcome.valid and outcome.restore_ok
    assert outcome.score == {
        "cause_category_correct": False,
        "component_correct": False,
    }  # 假模型总是答 no_fault

    for name in (
        "meta.json",
        "transcript.jsonl",
        "usage.json",
        "diagnosis.json",
        "score.json",
        "env.json",
    ):
        assert (outcome.run_dir / name).exists(), name
    env = json.loads((outcome.run_dir / "env.json").read_text())
    assert (
        env["health_before"]["healthy"]
        and env["verify_injected"]["ok"]
        and env["health_after"]["healthy"]
    )
    meta = json.loads((outcome.run_dir / "meta.json").read_text())
    assert (
        meta["status"] == "diagnosed"
        and meta["finished_at"]
        and meta["git"]["ops_agent_bench"]["commit"]
    )
    assert "run" in meta["config_snapshot"] and "env" in meta["config_snapshot"]


def passing_cooldown(calls):
    calls.append("cooldown")
    return CooldownResult(
        ok=True,
        waited_seconds=612.0,
        checks=16,
        resets=[],
        healthy=True,
        last_checks=[{"name": "x", "ok": True, "detail": ""}],
        detail="连续健康 600s",
    )


def failing_cooldown(calls):
    calls.append("cooldown")
    return CooldownResult(
        ok=False,
        waited_seconds=1800.0,
        checks=40,
        resets=[{"at_seconds": 5, "reason": "write fail"}],
        healthy=False,
        last_checks=[{"name": "x", "ok": False, "detail": "write fail"}],
        detail="等了 1800s 仍没有连续健康",
    )


def test_cooldown_replaces_the_plain_health_check_before_injection(tmp_path):
    outcome, calls = run_once(tmp_path, cooldown=passing_cooldown)
    assert calls[:3] == ["cooldown", "clear_events", "inject"]
    assert calls.count("health") == 1  # 只剩恢复之后的那一次
    env = json.loads((outcome.run_dir / "env.json").read_text())
    assert env["cooldown"]["ok"] and env["cooldown"]["checks"] == 16
    assert outcome.valid


def test_failed_cooldown_means_no_injection_and_an_invalid_run(tmp_path):
    outcome, calls = run_once(tmp_path, cooldown=failing_cooldown)
    assert calls == ["cooldown"]  # 没清事件、没注入、也没有要恢复的东西
    assert outcome.status == "invalid" and not outcome.valid
    assert "冷却期没有通过" in outcome.reason


def test_skipped_cooldown_is_recorded_as_skipped(tmp_path):
    outcome, _ = run_once(tmp_path)
    env = json.loads((outcome.run_dir / "env.json").read_text())
    assert env["cooldown"] is None


def test_foreign_scenario_traces_mark_the_run_as_suspected_contamination(tmp_path):
    leaked = 'Warning Failed: secret "postgres-credentials" not found'
    outcome, _ = run_once(tmp_path, payload=leaked)
    assert outcome.contaminated and not outcome.valid and outcome.score is None
    assert "疑似污染" in outcome.reason
    score_file = json.loads((outcome.run_dir / "score.json").read_text())
    assert score_file["valid"] is False
    env = json.loads((outcome.run_dir / "env.json").read_text())
    assert env["contamination"][0]["from_scenario"] == "missing-pg-secret"
    # 即使被判污染，环境照样恢复、记录照样完整
    assert outcome.restore_ok and (outcome.run_dir / "usage.json").exists()
    assert "疑似跨运行污染" in render_run(outcome.run_dir)


def test_clean_run_has_no_contamination_record(tmp_path):
    outcome, _ = run_once(tmp_path)
    assert not outcome.contaminated and outcome.valid
    env = json.loads((outcome.run_dir / "env.json").read_text())
    assert env["contamination"] == []


def test_symptom_not_observed_invalidates_the_run_but_still_restores(tmp_path):
    outcome, calls = run_once(tmp_path, {"symptom_ok": False})
    assert outcome.status == "invalid" and not outcome.valid
    assert "tools_start" not in calls  # agent 没有开始
    assert calls[-3:] == ["restore", "verify_restored", "health"]
    score_file = json.loads((outcome.run_dir / "score.json").read_text())
    assert score_file["valid"] is False


def test_unhealthy_environment_means_no_injection_and_no_restore(tmp_path):
    outcome, calls = run_once(tmp_path, healthy=False)
    assert outcome.status == "invalid" and not outcome.valid
    assert calls == ["health"]  # 没有注入，所以也没有什么需要恢复


def test_agent_side_crash_still_restores(tmp_path):
    outcome, calls = run_once(tmp_path, tools_fail=True)
    assert outcome.status == "error" and "工具进程起不来" in outcome.reason and not outcome.valid
    assert "restore" in calls and "verify_restored" in calls


def test_failed_restore_is_flagged_so_the_batch_stops(tmp_path):
    outcome, _ = run_once(tmp_path, {"restore_ok": False})
    assert not outcome.restore_ok
    assert "必须停止整个批次" in outcome.reason


def test_restore_raising_is_recorded_not_lost(tmp_path):
    outcome, _ = run_once(tmp_path, {"restore_raises": True})
    assert not outcome.restore_ok
    env = json.loads((outcome.run_dir / "env.json").read_text())
    assert "kubectl 挂了" in env["verify_restored"]["detail"]
    assert (outcome.run_dir / "meta.json").exists()  # 记录没有因为恢复出错而丢失


def test_transcript_has_every_step_and_tool_call(tmp_path):
    outcome, _ = run_once(tmp_path)
    events = read_transcript(outcome.run_dir)
    kinds = [e["type"] for e in events]
    assert kinds.count("request") == 3 and kinds.count("response") == 3
    assert kinds.count("tool_call") == 5  # 2 + 2 + 提交
    assert kinds[0] == "setup" and kinds[-1] == "stop"


# ---------- bench show ----------


def test_show_renders_the_whole_run(tmp_path):
    outcome, _ = run_once(tmp_path)
    text = render_run(outcome.run_dir)
    for expected in (
        "fake-scenario",
        "第 1 步",
        "第 3 步",
        "list_pods",
        "get_consumer_lag",
        "思考:",
        "最终诊断",
        "no_fault",
        "评分",
        "症状确认: 已出现",
        "恢复后健康检查: 健康",
    ):
        assert expected in text, expected


def test_show_truncates_long_tool_results_unless_full(tmp_path):
    outcome, _ = run_once(tmp_path)
    # 把某个工具结果改成很长，再看截断提示
    path = outcome.run_dir / "transcript.jsonl"
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    next(e for e in lines if e["type"] == "tool_call")["result"] = "z" * 5000
    path.write_text("\n".join(json.dumps(e) for e in lines) + "\n")

    short, full = render_run(outcome.run_dir), render_run(outcome.run_dir, full=True)
    assert "共 5000 字符" in short and "z" * 5000 not in short
    assert "z" * 5000 in full
