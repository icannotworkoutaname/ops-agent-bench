"""集成测试：每个场景连续执行 “注入 → 确认症状 → 恢复 → 确认健康” 3 次，全程无人工干预。

需要真实的 minikube 集群，默认不跑。运行方式：
    uv run pytest -m integration -s

这是第 1 阶段的完成标准之一。任何一步失败都直接让测试失败并写明原因，
不做重试：场景不能稳定注入或恢复，本身就是需要报告的结果。
"""

import pytest

from bench.admin_k8s import AdminK8s
from bench.config import load_env, load_run
from bench.health import is_healthy, run_health
from bench.scenarios import SCENARIOS

ROUNDS = 3

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cluster():
    env, run_cfg = load_env(), load_run()
    return env, run_cfg, AdminK8s(env)


@pytest.mark.parametrize("scenario_id", list(SCENARIOS))
def test_inject_restore_cycles(cluster, scenario_id):
    env, run_cfg, k8s = cluster
    sc_cfg = run_cfg["scenario"]

    for round_no in range(1, ROUNDS + 1):
        scenario = SCENARIOS[scenario_id](env, run_cfg, k8s)

        # 注入前环境必须是健康的，否则这一轮的结果没有意义
        checks = run_health(env, run_cfg, k8s)
        assert is_healthy(checks), f"第 {round_no} 轮开始前环境不健康: " + "；".join(
            f"{c.name}: {c.detail}" for c in checks if not c.ok
        )

        scenario.inject()
        try:
            injected = scenario.verify_injected(sc_cfg["verify_injected_timeout"])
            assert injected.ok, f"第 {round_no} 轮症状未出现: {injected.detail}"
        finally:
            # 无论症状是否出现，都先恢复，避免把环境留在坏的状态里
            scenario.restore()

        restored = scenario.verify_restored(sc_cfg["verify_restored_timeout"])
        assert restored.ok, f"第 {round_no} 轮恢复失败: {restored.detail}"
        print(f"[{scenario_id}] 第 {round_no} 轮通过: {injected.detail} | {restored.detail}")
