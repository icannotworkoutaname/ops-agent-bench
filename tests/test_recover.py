"""被中途杀掉的运行：补录费用，让预算保护不会少算。"""

import json

from bench.runlog import cumulative_usage, recover_interrupted_runs

PRICING = {
    "peak": {"input_cache_miss": 2.0, "input_cache_hit": 0.04, "output": 8.0},
    "offpeak": {"input_cache_miss": 1.0, "input_cache_hit": 0.02, "output": 4.0},
}
CFG = {
    "billing": {"peak": {"weekdays_only": True, "windows_utc": [["01:00", "04:00"]]}},
    "models": {"m": {"pricing": PRICING}},
}


def make_dir(root, name, status="running", with_transcript=True, steps=2, with_usage=False):
    d = root / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps({"model_key": "m", "fake": False, "status": status, "stage": 2})
    )
    if with_transcript:
        events = [{"type": "setup", "tools": []}]
        for i in range(1, steps + 1):
            # 2026-10-24 是周六：非高峰价
            events.append({"type": "request", "step": i, "sent_at": "2026-10-24T12:00:00+00:00"})
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
        (d / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if with_usage:
        (d / "usage.json").write_text(
            json.dumps({"fake": False, "steps": [{}], "total": {"cost": 1}})
        )
    return d


def test_an_interrupted_run_gets_its_cost_recovered_from_the_transcript(tmp_path):
    d = make_dir(tmp_path, "r1", steps=2)
    assert recover_interrupted_runs(tmp_path, CFG) == ["r1"]
    usage = json.loads((d / "usage.json").read_text())
    assert usage["total"]["cost"] == 2.0  # 2 步 × 100 万 token × 1 元
    assert usage["recovered_from_transcript"] is True and usage["stage"] == 2
    # 现在预算保护能看到这笔费用和这次运行
    assert cumulative_usage(tmp_path, 2) == {"cost": 2.0, "real_runs": 1}


def test_the_recovered_run_is_marked_invalid_and_explained(tmp_path):
    d = make_dir(tmp_path, "r1")
    recover_interrupted_runs(tmp_path, CFG)
    assert json.loads((d / "score.json").read_text())["valid"] is False
    info = json.loads((d / "interrupted.json").read_text())
    assert info["steps_in_transcript"] == 2 and info["had_submitted_a_diagnosis"] is False


def test_recovery_never_modifies_existing_files_and_is_idempotent(tmp_path):
    d = make_dir(tmp_path, "r1")
    before = (d / "meta.json").read_text()
    assert recover_interrupted_runs(tmp_path, CFG) == ["r1"]
    assert (d / "meta.json").read_text() == before
    assert recover_interrupted_runs(tmp_path, CFG) == []  # 第二次：有 usage.json 了，不再处理


def test_finished_runs_are_left_alone(tmp_path):
    make_dir(tmp_path, "done", status="diagnosed", with_usage=True)
    make_dir(tmp_path, "done2", status="running", with_usage=True)  # 已有 usage.json
    assert recover_interrupted_runs(tmp_path, CFG) == []


def test_a_run_killed_before_the_agent_started_costs_nothing_and_is_skipped(tmp_path):
    make_dir(tmp_path, "early", with_transcript=False)
    d = make_dir(tmp_path, "setup_only", steps=0)
    assert recover_interrupted_runs(tmp_path, CFG) == []
    assert not (d / "usage.json").exists()


def test_missing_runs_directory_is_fine(tmp_path):
    assert recover_interrupted_runs(tmp_path / "nope", CFG) == []
