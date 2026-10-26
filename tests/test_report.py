"""结果报告：用假的运行目录生成报告，检查每个数字、混淆矩阵、稳定性、抽样的可复现性。"""

import json

from bench.batch import BatchStore, make_order
from bench.report import build_report

TRUTHS = {
    "redis-down": ("dependency_unavailable", "redis"),
    "sp-oom": ("resource_limit_memory", "stream-processor"),
    "no-fault": ("no_fault", "none"),
}
SCENARIOS = list(TRUTHS)


def make_run(
    root,
    name,
    scenario,
    answer,
    *,
    valid=True,
    status="diagnosed",
    cost=0.05,
    steps=3,
    contaminated=False,
    hits=None,
):
    """写一个最小的运行目录。answer 是 (分类, 组件) 或 None（未提交）。"""
    d = root / "runs" / name
    d.mkdir(parents=True)
    truth = TRUTHS[scenario]
    (d / "meta.json").write_text(
        json.dumps(
            {"started_at": "2026-10-25T10:00:00+00:00", "finished_at": "2026-10-25T10:03:00+00:00"}
        )
    )
    if answer:
        (d / "diagnosis.json").write_text(
            json.dumps(
                {
                    "submitted": True,
                    "diagnosis": {"cause_category": answer[0], "component": answer[1]},
                }
            )
        )
    else:
        (d / "diagnosis.json").write_text(json.dumps({"submitted": False, "reason": "x"}))
    if valid:
        score = {
            "cause_category_correct": bool(answer) and answer[0] == truth[0],
            "component_correct": bool(answer) and answer[1] == truth[1],
        }
    else:
        score = {"valid": False, "reason": "无效"}
    (d / "score.json").write_text(json.dumps(score))
    (d / "usage.json").write_text(
        json.dumps(
            {"steps": [{}] * steps, "total": {"cost": cost}, "models_seen": ["deepseek-flash"]}
        )
    )
    (d / "env.json").write_text(json.dumps({"contamination": hits or []}))
    return {
        "scenario": scenario,
        "run_dir": str(d),
        "status": status,
        "valid": valid,
        "contaminated": contaminated,
        "restore_ok": True,
        "reason": "r",
        "cost": cost,
    }


def build(tmp_path, runs, repeats=2, seed=3):
    plan = {
        "batch_id": "b1",
        "seed": seed,
        "scenarios": SCENARIOS,
        "repeats": repeats,
        "model_key": "deepseek-flash",
        "order": make_order(SCENARIOS, repeats, seed),
        "input_hashes": {"prompts": "a" * 64, "tools": "b" * 64},
    }
    store = BatchStore.create(tmp_path / "batches", plan)
    for i, (name, scenario, answer, kw) in enumerate(runs, start=1):
        store.add_attempt({"index": i, **make_run(tmp_path, name, scenario, answer, **kw)})
    return store


RIGHT_REDIS = ("dependency_unavailable", "redis")
RIGHT_OOM = ("resource_limit_memory", "stream-processor")
RIGHT_NONE = ("no_fault", "none")


def sample_runs():
    return [
        ("r1", "redis-down", RIGHT_REDIS, {}),
        ("r2", "redis-down", ("misconfiguration_env", "redis"), {}),  # 分类错、组件对
        ("r3", "sp-oom", RIGHT_OOM, {}),
        ("r4", "sp-oom", ("resource_limit_memory", "query-service"), {}),  # 分类对、组件错
        ("r5", "no-fault", RIGHT_NONE, {}),
        ("r6", "no-fault", ("dependency_unavailable", "redis"), {}),  # 对照里编造了故障
        ("r7", "sp-oom", None, {"status": "no_diagnosis"}),  # 未提交，但算有效
        ("x1", "redis-down", None, {"valid": False, "status": "invalid", "cost": 0.0, "steps": 0}),
        (
            "x2",
            "sp-oom",
            RIGHT_OOM,
            {
                "valid": False,
                "contaminated": True,
                "hits": [{"kind": "other_scenario_signature", "needle": "Cannot reconnect"}],
            },
        ),
    ]


def test_counts_and_rates_per_scenario(tmp_path):
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    # redis-down: 2 次有效，分类对 1、组件对 2、两者都对 1
    assert "| `redis-down` | dependency_unavailable / redis | 2 | 1/2 | 2/2 | 1/2 | 0 |" in text
    # sp-oom: 3 次有效（含 1 次未提交），分类对 2、组件对 1、都对 1、未提交 1
    assert (
        "| `sp-oom` | resource_limit_memory / stream-processor | 3 | 2/3 | 1/3 | 1/3 | 1 |" in text
    )


def test_overall_accuracy_is_computed_over_valid_runs_only(tmp_path):
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    # 有效 7 次: 都对的是 r1 r3 r5 -> 3
    assert "| 全部场景 | 7 | " in text and "3/7 (43%)" in text
    # 故障场景（去掉 no-fault 的 2 次）有效 5 次，都对 r1 r3 -> 2
    assert "| 仅 10 个故障场景（不含 `no-fault` 对照） | 5 |" in text and "2/5 (40%)" in text


def test_invalid_and_contaminated_runs_are_listed_separately_and_not_counted(tmp_path):
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    assert "有效 7，无效 2" in text and "疑似污染 1" in text
    section = text.split("## 8.")[1]
    assert (
        "x1" in section
        and "x2" in section
        and "other_scenario_signature:Cannot reconnect" in section
    )
    assert "x1" not in text.split("## 8.")[0].split("## 6.")[1]  # 不在“答错的运行”里


def test_confusion_matrix_rows_are_truth_columns_are_answers(tmp_path):
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    matrix = text.split("## 4.")[1].split("## 5.")[0]
    header = next(line for line in matrix.splitlines() if line.startswith("| 标准答案"))
    assert "(未提交诊断)" in header and "`no_fault`" in header
    no_fault_row = next(line for line in matrix.splitlines() if line.startswith("| `no_fault`"))
    assert no_fault_row.endswith("| 2 |")  # 对照场景共 2 次有效运行
    oom_row = next(
        line for line in matrix.splitlines() if line.startswith("| `resource_limit_memory`")
    )
    assert oom_row.endswith("| 3 |")


def test_stability_marks_consistent_and_inconsistent_scenarios(tmp_path):
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    stability = text.split("## 5.")[1].split("## 6.")[0]
    assert "`redis-down`: **不一致**" in stability and "r1:" not in stability or "r1" in stability
    assert "不一致的场景数: 3/3" in stability  # 三个场景的答案都不完全一样


def test_a_consistent_scenario_is_reported_as_such(tmp_path):
    runs = [("a1", "redis-down", RIGHT_REDIS, {}), ("a2", "redis-down", RIGHT_REDIS, {})]
    text = build_report(build(tmp_path, runs), TRUTHS)
    assert "`redis-down`: **一致**（2 次都是 dependency_unavailable / redis）" in text


def test_wrong_runs_are_listed_with_their_answers(tmp_path):
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    wrong = text.split("## 6.")[1].split("## 7.")[0]
    assert "r2" in wrong and "misconfiguration_env / redis" in wrong
    assert "(未提交诊断) / -" in wrong  # r7
    assert "r1" not in wrong and "r3" not in wrong  # 答对的不在里面


def test_run_directories_are_referenced_without_absolute_paths(tmp_path):
    # 报告要提交、公开：不能带生成它的机器上的绝对路径（用户名等）
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    assert str(tmp_path) not in text
    assert "`runs/r2`" in text


def test_the_sample_of_correct_runs_is_reproducible_and_only_contains_correct_ones(tmp_path):
    store = build(
        tmp_path, sample_runs() + [(f"c{i}", "redis-down", RIGHT_REDIS, {}) for i in range(10)]
    )
    first = build_report(store, TRUTHS).split("## 7.")[1].split("## 8.")[0]
    second = build_report(store, TRUTHS).split("## 7.")[1].split("## 8.")[0]
    assert first == second
    listed = [line for line in first.splitlines() if line.startswith("- `")]
    assert len(listed) == 5
    assert all("r2" not in line and "r4" not in line for line in listed)


def test_per_scenario_averages(tmp_path):
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    line = next(x for x in text.splitlines() if x.startswith("| `redis-down`"))
    assert "| 3.0 | 0.0500 | 180 |" in line  # 3 步、0.05 元、3 分钟


def test_model_name_hashes_and_cost_are_in_the_batch_info(tmp_path):
    text = build_report(build(tmp_path, sample_runs()), TRUTHS)
    info = text.split("## 2.")[0]
    assert "deepseek-flash" in info and "`aaaaaaaaaaaa`" in info and "`bbbbbbbbbbbb`" in info
    assert "随机种子: `3`" in info and "总费用: 0.4000" in info


def test_an_empty_batch_does_not_crash(tmp_path):
    text = build_report(build(tmp_path, []), TRUTHS)
    assert "有效 0，无效 0" in text and "（没有答对的运行）" in text
