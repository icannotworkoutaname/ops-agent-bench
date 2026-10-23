"""agent 工具共用的小函数：namespace 白名单、输出截断、错误转文本。

注意：src/bench/tools/ 下的任何文件都不得 import bench.admin_k8s（隔离原则 1）。
tests/test_tools.py 里有测试会检查这一点。
"""

import functools
from collections.abc import Callable
from datetime import UTC, datetime

from kubernetes.client import ApiException


class ToolError(Exception):
    """工具能预见的错误（参数不合法、对象不存在等）。消息会原样返回给模型。"""


def check_namespace(namespace: str, allowed: list[str]) -> None:
    if namespace not in allowed:
        raise ToolError(f"namespace {namespace!r} 不在允许范围内，只能查看: {', '.join(allowed)}")


def truncate(text: str, limit: int) -> str:
    """超出上限时截断，并明确告诉模型“被截断了、原来有多长”，避免它把残缺的输出当成全部。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，原长度 {len(text)} 字符，只显示前 {limit} 字符]"


def age(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    seconds = int((datetime.now(UTC) - ts).total_seconds())
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def as_tool(max_chars: int) -> Callable:
    """把函数包装成“永远返回字符串、从不抛异常”的工具。

    工具出错（Pod 不存在、API 拒绝等）时返回清楚的错误文本，让模型自己决定下一步，
    而不是抛异常让整个 agent 循环崩掉。
    """

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> str:
            try:
                result = fn(*args, **kwargs)
            except ToolError as e:
                result = f"错误: {e}"
            except ApiException as e:
                if e.status == 404:
                    result = "错误: 对象不存在（Kubernetes 返回 404）"
                else:
                    result = f"错误: Kubernetes API 返回 {e.status} {e.reason}"
            except Exception as e:  # noqa: BLE001 - 兜底：任何意外都转成文本，不让循环崩
                result = f"错误: 工具执行失败 {type(e).__name__}: {e}"
            return truncate(result, max_chars)

        return wrapper

    return decorator
