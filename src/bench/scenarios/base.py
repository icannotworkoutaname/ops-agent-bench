"""场景基类。

一个场景 = 一种已知故障的“注入 / 验证症状 / 恢复 / 验证恢复”。
场景 id 只出现在本地代码和运行记录里，绝不写进集群（隔离原则 2）。
"""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from bench.admin_k8s import AdminK8s
from bench.config import EnvConfig, Workload
from bench.health import is_healthy, run_health


@dataclass(frozen=True)
class GroundTruth:
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
    # 本场景特有的错误信息的特征字符串，用来做“跨运行污染”检查：
    # 检查别的场景的运行时，如果工具返回的内容里出现了这些字符串，说明本场景的痕迹漏进去了。
    # 要写得足够具体，不能是在健康环境里也会出现的普通词（比如服务名本身）。
    signatures: tuple[str, ...] = ()

    def __init__(self, env: EnvConfig, run_cfg: dict, k8s: AdminK8s):
        self.env = env
        self.run_cfg = run_cfg
        self.k8s = k8s
        self.injected_at: float | None = None

    @property
    def params(self) -> dict:
        """本场景在 config/run.yaml 的 scenario_params 里的参数。"""
        return self.run_cfg.get("scenario_params", {}).get(self.id, {})

    def deployment(self, name: str) -> Workload:
        return next(d for d in self.env.deployments if d.name == name)

    def redeploy_with_patch(self, dep: Workload, patch: dict) -> None:
        """先把旧 Pod 全部停掉，再带着修改重新起一个。

        为什么不直接改：对只有 1 个副本的 Deployment 直接改，会触发滚动更新，新 Pod 起不来
        （配置错了 / 内存太小 / 探针不对）时，旧 Pod 会一直保留并继续服务，对外几乎没有症状
        （第 1 阶段在 bad-kafka-bootstrap 上实测过）。先停旧 Pod，才是“改错了又重启”的真实情形。
        """
        self.k8s.scale_deployment(dep.namespace, dep.name, 0)
        self.k8s.wait_pods_deleted(dep.namespace, f"app={dep.name}")
        self.k8s.patch_deployment(dep.namespace, dep.name, patch)
        self.k8s.scale_deployment(dep.namespace, dep.name, 1)

    def restore_from_manifest(self, manifest_key: str) -> None:
        """恢复的唯一依据是 music-streaming 仓库里对应的 k8s/*.yaml（只读使用）。"""
        self.k8s.apply_manifest(str(self.env.manifest_path(manifest_key)))

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
