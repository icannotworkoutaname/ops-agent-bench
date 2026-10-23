"""冷却期：连续健康满 W 才算通过，中途不健康就重新计时。时间和 sleep 都用假的，测试瞬间完成。"""

from bench.cooldown import wait_for_cooldown


class FakeTime:
    """clock() 返回当前假时间；sleep(n) 只是把假时间往前拨 n 秒；每次检查本身也耗 10 秒。"""

    def __init__(self):
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def scripted_health(t: FakeTime, results: list[bool], check_cost: float = 10.0):
    """第 i 次检查返回 results[i]（用完后一直返回最后一个）。每次检查耗时 check_cost 秒。"""
    calls = {"n": 0}

    def check():
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        t.now += check_cost
        ok = results[i]
        return ok, [{"name": "traffic", "ok": ok, "detail": "" if ok else "write fail"}]

    return check, calls


def run(results, window=600, interval=30, max_wait=1800):
    t = FakeTime()
    check, calls = scripted_health(t, results)
    out = wait_for_cooldown(check, window, interval, max_wait, clock=t.clock, sleep=t.sleep)
    return out, calls["n"]


def test_always_healthy_passes_after_the_window():
    out, checks = run([True])
    assert out.ok and out.healthy and out.resets == []
    # 每轮 10s 检查 + 30s 间隔 = 40s；首次健康在 t=10s，要到 >= 610s 才满足 -> 第 16 次检查
    assert checks == 16 and 600 <= out.waited_seconds - 10 < 640


def test_an_unhealthy_check_restarts_the_clock():
    # 前 5 次健康，第 6 次不健康，之后一直健康
    out, checks = run([True] * 5 + [False] + [True])
    assert out.ok
    assert len(out.resets) == 1 and "write fail" in out.resets[0]["reason"]
    # 打断之后还得重新连续健康满 600s，所以总次数远多于没被打断时的 16 次
    assert checks > 16 + 5


def test_never_healthy_gives_up_at_max_wait():
    out, _ = run([False], max_wait=300)
    assert not out.ok and not out.healthy
    assert "没有连续健康" in out.detail
    assert out.waited_seconds >= 300


def test_healthy_but_not_long_enough_before_max_wait_fails():
    out, _ = run([True], window=600, max_wait=200)
    assert not out.ok  # 一直健康，但在放弃之前没能凑够 W


def test_result_carries_the_last_health_records_for_the_run_log():
    out, _ = run([True], window=60)
    assert out.last_checks and out.last_checks[0]["name"] == "traffic"


def test_progress_messages_are_reported():
    t = FakeTime()
    check, _ = scripted_health(t, [True, False, True, True, True, True])
    messages: list[str] = []
    wait_for_cooldown(
        check, 100, 30, 1000, clock=t.clock, sleep=t.sleep, on_progress=messages.append
    )
    assert any("重新计时" in m for m in messages)
    assert any("已连续健康" in m for m in messages)
