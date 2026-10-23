"""Prometheus 工具的纯逻辑：回看范围检查、step 选择、结果整理、结果上限。不需要集群。"""

import pytest

from bench.tools.common import ToolError
from bench.tools.prom_read import (
    PromReader,
    check_lookback,
    choose_step,
    duration_seconds,
    filter_metric_names,
    format_result,
    query_reach_seconds,
)

# ---------- 时间长度解析 ----------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("1h30m", 5400), ("2h", 7200), ("500ms", 0.5), ("1d", 86400)],
)
def test_duration_seconds(text, seconds):
    assert duration_seconds(text) == seconds


@pytest.mark.parametrize("text", ["", "5", "m5", "5 minutes", "5x", "-5m"])
def test_bad_durations_are_rejected(text):
    with pytest.raises(ToolError):
        duration_seconds(text)


# ---------- 表达式自己会往回读多久 ----------


@pytest.mark.parametrize(
    ("query", "seconds"),
    [
        ("up", 0),
        ('up{job="apps"}', 0),
        ("rate(http_requests_total[5m])", 300),
        ("rate(a[5m]) / rate(b[2m])", 300),  # 并列的选择器取最长的
        ("up offset 10m", 600),
        ("rate(a[5m] offset 1h)", 300 + 3600),
        ("up offset -5m", 0),  # 负 offset 是看未来，不会读到更早的数据
        ("max_over_time(rate(a[1m])[30m:1m])", 60 + 1800),  # 子查询：保守地相加
        ("max_over_time(a[30m:])", 1800),
    ],
)
def test_query_reach(query, seconds):
    assert query_reach_seconds(query) == seconds


def test_at_modifier_is_forbidden_because_it_can_pin_any_time():
    with pytest.raises(ToolError) as e:
        query_reach_seconds("up @ 1600000000")
    assert "@" in str(e.value)


# ---------- 回看窗口 ----------


def test_lookback_within_the_window_passes():
    check_lookback("up", 0, 10)
    check_lookback("rate(a[5m])", 0, 10)
    check_lookback("rate(a[5m])", 5 * 60, 10)  # 刚好 10 分钟：允许


def test_lookback_beyond_the_window_is_rejected_with_numbers():
    with pytest.raises(ToolError) as e:
        check_lookback("rate(a[5m])", 8 * 60, 10)  # 8 + 5 = 13 分钟
    assert "13.0" in str(e.value) and "10 分钟" in str(e.value)
    with pytest.raises(ToolError):
        check_lookback("rate(a[11m])", 0, 10)
    with pytest.raises(ToolError):
        check_lookback("up offset 15m", 0, 10)


# ---------- step ----------


def test_default_step_is_at_least_15s():
    assert choose_step(600, None, 60) == (15, "")


def test_small_step_is_raised_to_respect_the_point_limit():
    step, note = choose_step(600, "5s", 60)
    assert step == 10 and "已调大" in note


def test_reasonable_step_is_kept():
    assert choose_step(600, "30s", 60) == (30, "")


def test_zero_step_is_rejected():
    with pytest.raises(ToolError):
        choose_step(600, "0s", 60)


# ---------- 结果整理 ----------


def vector(*items):
    return {
        "resultType": "vector",
        "result": [{"metric": m, "value": [1700000000, v]} for m, v in items],
    }


def test_vector_result_shows_labels_and_values():
    data = vector(({"__name__": "up", "job": "apps", "pod": "a"}, "1"))
    out = format_result(data, 20, 60)
    assert 'up{job="apps",pod="a"} => 1' in out and "返回 1 条序列" in out


def test_empty_result_says_so():
    assert "没有返回任何序列" in format_result({"resultType": "vector", "result": []}, 20, 60)


def test_series_cap_states_how_many_were_omitted():
    data = vector(*[({"__name__": "m", "i": str(i)}, "1") for i in range(30)])
    out = format_result(data, 20, 60)
    assert "返回 30 条序列，只显示前 20 条" in out and "还有 10 条序列未显示" in out
    assert out.count("=> 1") == 20


def test_matrix_gets_summary_and_is_downsampled_with_a_note():
    now = 1700000600.0
    values = [[now - 600 + i * 10, str(i)] for i in range(60)]  # 60 个点
    data = {"resultType": "matrix", "result": [{"metric": {"__name__": "m"}, "values": values}]}
    out = format_result(data, 20, 12, now=now)
    assert "60 个点" in out and "最小 0 最大 59 最后 59" in out
    assert "已降采样，每 5 个点取 1 个" in out
    sampled = out.splitlines()[2]
    assert sampled.count("m=") <= 13  # 上限 12 个，再补上最后一个点


def test_the_last_point_is_shown_as_now_never_as_a_negative_age():
    now = 1700000100.0
    # 最后一个点比查询结束时间晚 0.4 秒（真实情况里会出现），以及一个整整 1 分钟前的点
    values = [[now - 60, "1"], [now + 0.4, "2"]]
    data = {"resultType": "matrix", "result": [{"metric": {"__name__": "m"}, "values": values}]}
    out = format_result(data, 20, 60, now=now)
    assert "-1.0m=1" in out and "now=2" in out and "--" not in out


def test_matrix_without_downsampling_has_no_note():
    now = 1700000100.0
    values = [[now - 100 + i * 10, "1"] for i in range(10)]
    data = {"resultType": "matrix", "result": [{"metric": {"__name__": "m"}, "values": values}]}
    assert "降采样" not in format_result(data, 20, 60, now=now)


def test_nan_values_do_not_break_the_summary():
    now = 1700000100.0
    values = [[now - 20, "NaN"], [now - 10, "2"], [now, "4"]]
    data = {"resultType": "matrix", "result": [{"metric": {"__name__": "m"}, "values": values}]}
    out = format_result(data, 20, 60, now=now)
    assert "最小 2 最大 4" in out


def test_scalar_result():
    assert (
        format_result({"resultType": "scalar", "result": [1700000000, "3.5"]}, 20, 60)
        == "scalar: 3.5"
    )


# ---------- list_metrics ----------


def test_metric_name_search_is_case_insensitive_and_sorted():
    names = ["jvm_memory_used_bytes", "http_server_requests", "JVM_threads", "up"]
    out = filter_metric_names(names, "jvm", 100)
    assert out.index("JVM_threads") < out.index("jvm_memory_used_bytes")
    assert "共 2 个" in out and "http_server" not in out


def test_metric_search_cap_says_how_many_exist():
    names = [f"metric_{i:03}" for i in range(250)]
    out = filter_metric_names(names, "metric", 100)
    assert "共 250 个" in out and "只显示前 100 个" in out
    assert out.count("metric_") == 100  # 只列出 100 个名字


def test_metric_search_with_no_hits():
    assert "没有名字包含" in filter_metric_names(["up"], "zzz", 100)


# ---------- PromReader：在发请求之前就拦截 ----------

LIMITS = {
    "lookback_minutes": 10,
    "prom_max_series": 20,
    "prom_max_points": 60,
    "prom_max_metrics": 100,
}


def reader_with_fake_get():
    reader = PromReader.__new__(PromReader)
    reader.limits = LIMITS
    reader.calls = []

    def fake_get(path, params=None):
        reader.calls.append((path, params))
        return {"resultType": "vector", "result": []}

    reader._get = fake_get
    return reader


def test_out_of_window_queries_never_reach_prometheus():
    reader = reader_with_fake_get()
    for kwargs in (
        {"query": "rate(a[30m])", "range_minutes": None, "step": None},
        {"query": "up", "range_minutes": 11, "step": None},
        {"query": "up", "range_minutes": 0, "step": None},
        {"query": "rate(a[5m])", "range_minutes": 8, "step": None},
        {"query": "up @ 100", "range_minutes": None, "step": None},
        {"query": "   ", "range_minutes": None, "step": None},
    ):
        with pytest.raises(ToolError):
            reader.promql_query(**kwargs)
    assert reader.calls == []


def test_instant_query_uses_the_query_endpoint():
    reader = reader_with_fake_get()
    reader.promql_query("up", None, None)
    assert reader.calls == [("/api/v1/query", {"query": "up"})]


def test_range_query_starts_exactly_range_minutes_ago():
    reader = reader_with_fake_get()
    reader.promql_query("up", 5, None)
    path, params = reader.calls[0]
    assert path == "/api/v1/query_range"
    assert params["end"] - params["start"] == pytest.approx(300, abs=0.01)
    assert params["step"] == 15
