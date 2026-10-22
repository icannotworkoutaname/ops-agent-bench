"""场景基类。

一个场景 = 一种已知故障的“注入 / 验证症状 / 恢复 / 验证恢复”。
场景 id 只出现在本地代码和运行记录里，绝不写进集群（隔离原则 2）。
"""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from bench.admin_k8s import AdminK8s
from bench.config import EnvConfig
from bench.health import is_healthy, run_health


@dataclass(frozen=True)
class GroundTruth:
    """标准答案。目前是草稿（来自 stage-1.md），第 2 阶段由仓库所有者最终确认。"""

    cause_category: str
    component: str
    expected_action: str


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str


def wait_until(check: Callable[[], Outcome], timeout: float, interval: float = 3.0) -> Outcome:
    """反复调用 check，直到成功或超时。超时返回最后一次的结果，并注明耗时。

    这里的 timeout 来自配置文件。超时就如实报告“没出现 / 没恢复”，
    不要靠调大 timeout 或悄悄重试来掩盖问题（CLAUDE.md 的约定）。
    """
    start = time.monotonic()
    last = Outcome(False, "还没有检查过")
    while True:
        try:
            last = check()
        except Exception as e:  # noqa: BLE001 - 检查过程出错也算“还没满足”，把原因带上
            last = Outcome(False, f"检查出错: {type(e).__name__}: {e}")
        elapsed = time.monotonic() - start
        if last.ok:
            return Outcome(True, f"{last.detail}（用时 {elapsed:.0f}s）")
        if elapsed >= timeout:
            return Outcome(False, f"{timeout:.0f}s 内未满足。最后一次: {last.detail}")
        time.sleep(interval)


class Scenario(ABC):
    id: str
    ground_truth: GroundTruth

    def __init__(self, env: EnvConfig, run_cfg: dict, k8s: AdminK8s):
        self.env = env
        self.run_cfg = run_cfg
        self.k8s = k8s
        self.injected_at: float | None = None

    def mark_injected(self) -> None:
        self.injected_at = time.time()

    def seconds_since_injected(self) -> int:
        """用来算“注入之后”的流量窗口，多留 2 秒余量覆盖时钟误差。"""
        assert self.injected_at is not None, "还没有注入"
        return int(time.time() - self.injected_at) + 2

    @abstractmethod
    def inject(self) -> None: ...

    @abstractmethod
    def verify_injected(self, timeout: float) -> Outcome:
        """确认症状确实出现。超时没出现，本次运行标记为无效，不让 agent 开始。"""

    @abstractmethod
    def restore(self) -> None:
        """恢复。必须可以重复执行（已经恢复的环境上再执行一次不应出错）。"""

    def verify_restored(self, timeout: float) -> Outcome:
        """恢复后的唯一判据是：整套健康检查全部通过。"""

        def check() -> Outcome:
            checks = run_health(self.env, self.run_cfg, self.k8s)
            if is_healthy(checks):
                return Outcome(True, "健康检查全部通过")
            bad = [f"{c.name}: {c.detail}" for c in checks if not c.ok]
            return Outcome(False, "；".join(bad))

        return wait_until(check, timeout, interval=5.0)
