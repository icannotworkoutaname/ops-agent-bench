"""批量运行器：顺序可复现、续跑、停止条件、高峰暂停、固定输入的哈希。不需要集群。"""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from bench.batch import (
    BatchHooks,
    BatchStore,
    decide_next,
    estimate_duration,
    hash_inputs,
    make_order,
    run_batch,
    trailing_invalid,
    valid_counts,
)

SCENARIOS = ["a", "b", "c", "d"]
PEAK = {"weekdays_only": True, "windows_utc": [["01:00", "04:00"], ["06:00", "10:00"]]}


def plan(repeats=2, seed=7, scenarios=SCENARIOS):
    return {
        "batch_id": "t",
        "seed": seed,
        "scenarios": scenarios,
        "repeats": repeats,
        "order": make_order(scenarios, repeats, seed),
    }


def attempt(scenario, valid=True, status="diagnosed", restore_ok=True):
    return {"scenario": scenario, "valid": valid, "status": status, "restore_ok": restore_ok}


# ---------- 顺序 ----------


def test_the_order_is_reproducible_from_the_seed():
    assert make_order(SCENARIOS, 5, 42) == make_order(SCENARIOS, 5, 42)
    assert make_order(SCENARIOS, 5, 42) != make_order(SCENARIOS, 5, 43)


def test_each_scenario_appears_exactly_repeats_times_spread_over_balanced_rounds():
    order = make_order(SCENARIOS, 5, 1)
    assert len(order) == 20
    for i in range(0, 20, 4):  # 每一轮恰好把每个场景各排一次
        assert sorted(order[i : i + 4]) == SCENARIOS


@pytest.mark.parametrize("seed", range(40))
def test_the_same_scenario_never_runs_twice_in_a_row(seed):
    order = make_order(SCENARIOS, 5, seed)
    assert all(a != b for a, b in itertools.pairwise(order))


def test_a_single_scenario_is_allowed_even_though_it_must_repeat():
    assert make_order(["only"], 3, 0) == ["only", "only", "only"]


# ---------- 决策 ----------


def test_the_first_pass_follows_the_planned_order():
    p = plan()
    done = [attempt(s) for s in p["order"][:3]]
    d = decide_next(p, done)
    assert d.action == "run" and d.scenario == p["order"][3]


def test_when_every_scenario_has_enough_valid_runs_the_batch_is_done():
    p = plan(repeats=2)
    assert decide_next(p, [attempt(s) for s in p["order"]]).action == "done"


def test_invalid_runs_are_made_up_at_the_end_for_the_right_scenario():
    p = plan(repeats=2)
    attempts = [attempt(s, valid=(s != "b" or i % 2 == 0)) for i, s in enumerate(p["order"])]
    counts = valid_counts(attempts)
    assert counts["b"] == 1 and all(counts[s] == 2 for s in "acd")
    d = decide_next(p, attempts)
    assert d.action == "run" and d.scenario == "b" and "补跑" in d.reason


def test_makeup_runs_prefer_the_scenario_with_most_missing_and_avoid_repeating_the_last():
    p = plan(repeats=3)
    attempts = [attempt(s) for s in p["order"]]
    attempts[-1] = attempt(p["order"][-1], valid=False)  # 最后一个无效
    last = attempts[-1]["scenario"]
    attempts.append(attempt("a", valid=False))  # 再让 a 缺一个
    d = decide_next(p, attempts)
    assert d.action == "run"
    assert d.scenario in {last, "a"}


def test_three_invalid_in_a_row_for_one_scenario_stops_the_batch():
    p = plan(repeats=5)
    attempts = [attempt("a", valid=False)] * 3
    assert trailing_invalid(attempts, "a") == 3
    d = decide_next(p, attempts)
    assert d.action == "stop" and "a" in d.reason


def test_a_valid_run_resets_the_invalid_streak():
    attempts = [attempt("a", valid=False)] * 2 + [attempt("a")] + [attempt("a", valid=False)]
    assert trailing_invalid(attempts, "a") == 1


def test_other_scenarios_in_between_do_not_break_a_scenarios_streak():
    attempts = [
        attempt("a", valid=False),
        attempt("b"),
        attempt("a", valid=False),
        attempt("c"),
        attempt("a", valid=False),
    ]
    assert trailing_invalid(attempts, "a") == 3


def test_a_failed_restore_stops_everything():
    d = decide_next(plan(), [attempt("a", restore_ok=False)])
    assert d.action == "stop" and "恢复失败" in d.reason


def test_three_api_errors_in_a_row_stop_the_batch():
    errs = [attempt("a", valid=False, status="error")] * 3
    assert decide_next(plan(), errs).action == "stop"
    mixed = [
        attempt("a", valid=False, status="error"),
        attempt("b"),
        attempt("c", valid=False, status="error"),
    ]
    assert decide_next(plan(), mixed).action == "run"


# ---------- 续跑与主循环 ----------


class FakeWorld:
    """假的执行环境：记录执行顺序，按脚本决定哪些运行无效，时间只在 sleep 时前进。"""

    def __init__(self, invalid_at=(), start=None):
        self.ran: list[str] = []
        self.invalid_at = set(invalid_at)
        self.t = start or datetime(2026, 10, 24, 12, 0, tzinfo=UTC)  # 周六：不是高峰
        self.sleeps = 0
        self.logs: list[str] = []
        self.refusals: list[str] = []
        self.input_problem: str | None = None
        self.store: BatchStore | None = None  # 设了就按“批次里第几次尝试”来决定哪次无效

    def hooks(self, stop_after=None):
        def execute_one(sid):
            if stop_after is not None and len(self.ran) >= stop_after:
                raise KeyboardInterrupt  # 模拟中途被打断
            n = (len(self.store.attempts()) if self.store else len(self.ran)) + 1
            self.ran.append(sid)
            return {
                "status": "diagnosed",
                "valid": n not in self.invalid_at,
                "restore_ok": True,
                "contaminated": False,
            }

        def sleep(seconds):
            self.sleeps += 1
            self.t += timedelta(seconds=seconds)

        return BatchHooks(
            execute_one=execute_one,
            preflight=lambda: list(self.refusals),
            check_inputs=lambda: self.input_problem,
            now=lambda: self.t,
            sleep=sleep,
            log=self.logs.append,
        )


def make_store(tmp_path, **kw):
    return BatchStore.create(tmp_path, plan(**kw))


def test_a_full_batch_runs_the_planned_order_then_completes(tmp_path):
    store, world = make_store(tmp_path), FakeWorld()
    result = run_batch(store, world.hooks(), None, 20)
    assert result.status == "completed"
    assert world.ran == store.plan["order"]
    assert store.events()[-1]["type"] == "completed"


def test_invalid_runs_are_made_up_until_every_scenario_has_enough(tmp_path):
    store, world = make_store(tmp_path), FakeWorld(invalid_at={2, 5})
    result = run_batch(store, world.hooks(), None, 20)
    assert result.status == "completed" and len(world.ran) == 8 + 2
    counts = valid_counts(store.attempts())
    assert all(counts[s] == 2 for s in SCENARIOS)


def test_resuming_gives_exactly_the_same_sequence_as_never_being_interrupted(tmp_path):
    # A：从头跑到尾，没有中断。第 3 次运行无效
    store_a = BatchStore.create(tmp_path / "a", plan())
    world_a = FakeWorld(invalid_at={3})
    world_a.store = store_a
    run_batch(store_a, world_a.hooks(), None, 20)

    # B：同样的计划，在第 5 次运行开始时被打断，然后“重新启动”（重新打开批次目录）继续
    store_b = BatchStore.create(tmp_path / "b", plan())
    world_b1 = FakeWorld(invalid_at={3})
    world_b1.store = store_b
    with pytest.raises(KeyboardInterrupt):
        run_batch(store_b, world_b1.hooks(stop_after=4), None, 20)
    assert len(store_b.attempts()) == 4  # 已完成的运行已经落盘

    reopened = BatchStore.open(tmp_path / "b", "t")
    world_b2 = FakeWorld(invalid_at={3})
    world_b2.store = reopened
    run_batch(reopened, world_b2.hooks(), None, 20)

    sequence = lambda store: [(a["scenario"], a["valid"]) for a in store.attempts()]
    assert sequence(reopened) == sequence(store_a)
    assert len(sequence(reopened)) == 9  # 8 次计划 + 1 次补跑


def test_resume_does_not_rerun_what_already_finished(tmp_path):
    store = make_store(tmp_path)
    for s in store.plan["order"][:5]:
        store.add_attempt({"index": 0, **attempt(s), "contaminated": False})
    world = FakeWorld()
    run_batch(store, world.hooks(), None, 20)
    assert world.ran == store.plan["order"][5:]


def test_a_failed_restore_in_a_run_stops_the_batch_immediately(tmp_path):
    store, world = make_store(tmp_path), FakeWorld()
    hooks = world.hooks()
    original = hooks.execute_one

    def flaky(sid):
        result = original(sid)
        if len(world.ran) == 3:
            result["restore_ok"] = False
        return result

    hooks.execute_one = flaky
    result = run_batch(store, hooks, None, 20)
    assert result.status == "stopped" and "恢复失败" in result.reason and len(world.ran) == 3


def test_an_acknowledged_restore_failure_lets_the_batch_continue_but_only_for_that_attempt(
    tmp_path,
):
    store = make_store(tmp_path)
    order = store.plan["order"]
    for i, s in enumerate(order[:3], start=1):
        store.add_attempt({"index": i, **attempt(s, restore_ok=(i != 3)), "contaminated": False})
    # 没有确认：照旧停止
    assert run_batch(store, FakeWorld().hooks(), None, 20).status == "stopped"
    # 确认之后：从下一个场景继续，失败的那次仍然留在记录里
    store.add_event("restore_failure_acknowledged", attempt_index=3, note="人工检查过")
    world = FakeWorld()
    result = run_batch(store, world.hooks(), None, 20)
    assert result.status == "completed" and world.ran == order[3:]
    assert store.attempts()[2]["restore_ok"] is False


def test_acknowledging_one_attempt_does_not_cover_a_later_restore_failure():
    attempts = [
        {"index": 1, **attempt("a", restore_ok=False)},
        {"index": 2, **attempt("b", restore_ok=False)},
    ]
    d = decide_next(plan(), attempts, {1})
    assert d.action == "stop" and "恢复失败" in d.reason


def test_changed_inputs_stop_the_batch_before_the_next_run(tmp_path):
    store, world = make_store(tmp_path), FakeWorld()
    world.input_problem = "系统提示的哈希变了"
    result = run_batch(store, world.hooks(), None, 20)
    assert result.status == "stopped" and "哈希" in result.reason and world.ran == []


def test_budget_or_cap_refusals_stop_the_batch(tmp_path):
    store, world = make_store(tmp_path), FakeWorld()
    world.refusals = ["累计费用 15.2 已达到预算 15"]
    result = run_batch(store, world.hooks(), None, 20)
    assert result.status == "stopped" and "预算" in result.reason


# ---------- 高峰时段 ----------


def test_it_pauses_during_peak_hours_and_resumes_by_itself(tmp_path):
    store = make_store(tmp_path)
    world = FakeWorld(start=datetime(2026, 10, 26, 1, 30, tzinfo=UTC))  # 周一 01:30：高峰
    world.refusals = ["当前是高峰时段"]  # 预检会因高峰而拒绝；这不应该让批次停止
    hooks = world.hooks()
    # 高峰一结束，预检也就不再拒绝
    original_sleep = hooks.sleep

    def sleep(seconds):
        original_sleep(seconds)
        if not (world.t.hour < 4):
            world.refusals = []

    hooks.sleep = sleep
    result = run_batch(store, hooks, PEAK, 20, poll_seconds=60)
    assert result.status == "completed"
    kinds = [e["type"] for e in store.events()]
    assert "pause" in kinds and "resume" in kinds and kinds.index("pause") < kinds.index("resume")
    assert world.sleeps > 0


def test_it_does_not_start_a_run_that_would_run_into_the_peak(tmp_path):
    store = make_store(tmp_path, repeats=1, scenarios=["a"])
    world = FakeWorld(
        start=datetime(2026, 10, 26, 0, 50, tzinfo=UTC)
    )  # 周一 00:50，高峰 01:00 开始
    result = run_batch(store, world.hooks(), PEAK, 20, poll_seconds=60)
    assert result.status == "completed"
    # 20 分钟的安全余量: 00:50 + 20min 已经落在高峰里，所以要等到高峰结束（04:00）才开始
    assert world.t >= datetime(2026, 10, 26, 4, 0, tzinfo=UTC)


def test_weekends_are_never_peak(tmp_path):
    store = make_store(tmp_path, repeats=1, scenarios=["a"])
    world = FakeWorld(start=datetime(2026, 10, 31, 2, 0, tzinfo=UTC))  # 周六 02:00
    run_batch(store, world.hooks(), PEAK, 20)
    assert world.sleeps == 0


# ---------- 预计时长 ----------


def test_estimate_without_peak_overlap_is_just_the_sum():
    start = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)  # 周六
    est = estimate_duration(10, 15, start, PEAK, 20)
    assert est["total_minutes"] == 150 and est["paused_minutes"] == 0


def test_estimate_counts_the_pause_for_peak_hours():
    start = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)  # 周一 00:00
    est = estimate_duration(1, 15, start, PEAK, 20)
    # 00:00 + 20min 还不在高峰，可以开始；估算里不应该有暂停
    assert est["paused_minutes"] == 0
    est2 = estimate_duration(1, 15, datetime(2026, 10, 26, 0, 45, tzinfo=UTC), PEAK, 20)
    assert est2["paused_minutes"] >= 180  # 要等整个 01:00-04:00 过去


# ---------- 固定输入的哈希 ----------

MODEL = {"model": "m", "base_url": "u", "api_key_env": "K", "pricing": {"peak": {}}}
AGENT = {"max_steps": 25}


def base_hash(**over):
    args = {
        "system_prompt": "S",
        "task_prompt": "T",
        "nudge_prompt": "N",
        "model_entry": MODEL,
        "agent_cfg": AGENT,
        "lookback_minutes": 10,
        "tool_definitions": [{"name": "x"}],
    }
    return hash_inputs(**{**args, **over})


def test_hashes_change_when_any_fixed_input_changes():
    base = base_hash()
    assert base_hash() == base
    assert base_hash(system_prompt="S2")["prompts"] != base["prompts"]
    assert base_hash(tool_definitions=[{"name": "y"}])["tools"] != base["tools"]
    assert base_hash(agent_cfg={"max_steps": 30})["agent_config"] != base["agent_config"]
    assert base_hash(lookback_minutes=5)["agent_config"] != base["agent_config"]
    assert base_hash(model_entry={**MODEL, "model": "other"})["model"] != base["model"]


def test_prices_are_not_part_of_the_input_hash():
    # 价格只影响费用，不影响 agent 看到什么、做什么
    assert base_hash(model_entry={**MODEL, "pricing": {"peak": {"output": 99}}}) == base_hash()
