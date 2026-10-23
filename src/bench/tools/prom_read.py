"""agent 的 Prometheus 只读工具：promql_query 和 list_metrics。

和 k8s 工具一样分两层：纯函数（检查查询的回看范围、整理结果，不碰网络，方便测试）
和 PromReader（真的去查）。Prometheus 部署在集群内的 monitoring namespace，
通过 API server 的 Service 代理访问：不用 port-forward（Pod 一重建就断），
也不给 agent 任何 monitoring namespace 里的 kubectl 类能力。

跨运行污染相关的关键约束：查询实际会读到的最早时刻不能早于 W 分钟前。
"""

import json
import math
import re
import time
from urllib.parse import urlencode

from kubernetes import client, config
from kubernetes.client import ApiException

from bench.tools.common import ToolError

_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
_DURATION = r"(?:\d+(?:ms|s|m|h|d|w|y))+"
_DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")
# 范围选择器 [5m]，以及子查询 [30m:1m] / [30m:]
_RANGE_SELECTOR = re.compile(rf"\[\s*({_DURATION})\s*(:\s*(?:{_DURATION})?\s*)?\]")
# offset 修饰符，可以是负数（看未来），负数不会让查询读到更早的数据
_OFFSET = re.compile(rf"\boffset\s+(-?)({_DURATION})")


def duration_seconds(text: str) -> float:
    """把 Prometheus 的时间长度（5m、1h30m、30s）转成秒。不认识的格式抛 ToolError。"""
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text.strip():
        raise ToolError(f"无法识别的时间长度 {text!r}，示例: 30s、5m、1h")
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in parts)


def query_reach_seconds(query: str) -> float:
    """这条 PromQL 表达式本身会往回读多久的数据（不含区间查询的范围）。

    取范围选择器里最长的一个，加上最大的正 offset。含子查询（[30m:1m]）时，
    子查询里面还可能套着别的范围选择器，所以保守地把所有选择器加起来。
    这是个保守的上界：宁可误拒一个合法查询（错误信息会说明原因），也不能让旧数据漏进来。
    """
    if "@" in query:
        # @ 修饰符可以把查询钉在任意时刻，绕过回看窗口
        raise ToolError("不允许使用 @ 修饰符")
    selectors = _RANGE_SELECTOR.findall(query)
    durations = [duration_seconds(d) for d, _ in selectors]
    has_subquery = any(sub for _, sub in selectors)
    selector_reach = (
        (sum(durations) if has_subquery else max(durations, default=0)) if durations else 0
    )
    offsets = [duration_seconds(d) for sign, d in _OFFSET.findall(query) if not sign]
    return selector_reach + max(offsets, default=0)


def check_lookback(query: str, range_seconds: float, window_minutes: int) -> None:
    """查询实际会读到的最早时刻（= 区间长度 + 表达式自己的回看）不得早于 W 分钟前。"""
    reach = range_seconds + query_reach_seconds(query)
    if reach > window_minutes * 60:
        raise ToolError(
            f"这次查询会读到 {reach / 60:.1f} 分钟前的数据，超过了回看窗口 {window_minutes} 分钟"
            f"（区间 {range_seconds / 60:.1f} 分钟 + 表达式里的范围 / offset "
            f"{query_reach_seconds(query) / 60:.1f} 分钟）。请缩小区间或表达式里的范围。"
        )


def _format_labels(metric: dict) -> str:
    name = metric.get("__name__", "")
    labels = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{labels}}}" if labels else (name or "{}")


def _num(value: str) -> str:
    try:
        f = float(value)
    except ValueError:
        return value
    if math.isnan(f) or math.isinf(f):
        return value
    return f"{f:.6g}"


def format_result(data: dict, max_series: int, max_points: int, now: float | None = None) -> str:
    """把 Prometheus 的查询结果整理成给模型看的文字。

    序列数和点数都有上限：超出时只显示一部分，并明确写出被省略了多少，
    免得模型把残缺的结果当成全部。区间查询的每条序列给出统计量（最小 / 最大 / 最后）
    和降采样后的点，而不是把几十个点全部倒出来。
    """
    kind, result = data["resultType"], data["result"]
    if kind == "scalar":
        return f"scalar: {_num(result[1])}"
    if kind == "string":
        return f"string: {result[1]}"
    if not result:
        return "(查询没有返回任何序列)"

    shown, omitted = result[:max_series], len(result) - max_series
    lines = [
        f"返回 {len(result)} 条序列" + (f"，只显示前 {max_series} 条" if omitted > 0 else "") + ":"
    ]

    if kind == "vector":
        for series in shown:
            lines.append(f"  {_format_labels(series['metric'])} => {_num(series['value'][1])}")
    else:  # matrix
        for series in shown:
            points = [(float(t), v) for t, v in series["values"]]
            nums = [float(v) for _, v in points if _is_number(v)]
            ref = now if now is not None else points[-1][0]
            stride = max(1, math.ceil(len(points) / max_points))
            sampled = points[::stride]
            if points[-1] not in sampled:
                sampled.append(points[-1])
            summary = f"{len(points)} 个点"
            if nums:
                summary += f"，最小 {_num(str(min(nums)))} 最大 {_num(str(max(nums)))} 最后 {_num(points[-1][1])}"
            lines.append(f"  {_format_labels(series['metric'])}: {summary}")
            lines.append(
                "    "
                + ", ".join(f"{_ago(ref - t)}={_num(v)}" for t, v in sampled)
                + (f"  （已降采样，每 {stride} 个点取 1 个）" if stride > 1 else "")
            )
    if omitted > 0:
        lines.append(
            f"  ... 还有 {omitted} 条序列未显示（上限 {max_series} 条）。请用标签过滤或聚合缩小范围。"
        )
    return "\n".join(lines)


def _ago(seconds: float) -> str:
    """距离现在多久。最后一个点可能比查询结束时间略晚几毫秒，不能显示成负数。"""
    return "now" if seconds < 1 else f"-{seconds / 60:.1f}m"


def _is_number(text: str) -> bool:
    try:
        f = float(text)
    except ValueError:
        return False
    return not (math.isnan(f) or math.isinf(f))


def choose_step(range_seconds: float, step_text: str | None, max_points: int) -> tuple[int, str]:
    """选区间查询的 step。步长太小会导致点数超过上限时自动调大，并返回一句说明。"""
    min_step = max(1, math.ceil(range_seconds / max_points))
    if step_text is None:
        step = max(15, min_step)
        return step, ""
    wanted = duration_seconds(step_text)
    if wanted < 1:
        raise ToolError("step 不能小于 1s")
    if wanted < min_step:
        return min_step, f"（step {step_text} 会产生超过 {max_points} 个点，已调大到 {min_step}s）"
    return int(wanted), ""


def filter_metric_names(names: list[str], match: str, limit: int) -> str:
    wanted = match.strip().lower()
    hits = sorted(n for n in names if wanted in n.lower())
    if not hits:
        return f"没有名字包含 {match!r} 的指标"
    shown = hits[:limit]
    head = f"名字包含 {match!r} 的指标共 {len(hits)} 个" + (
        f"，只显示前 {limit} 个，请用更具体的子串缩小范围" if len(hits) > limit else ""
    )
    return head + ":\n" + "\n".join(f"  {n}" for n in shown)


class PromReader:
    def __init__(self, kube_context: str, namespace: str, service: str, port: int, limits: dict):
        config.load_kube_config(context=kube_context)
        self.api = client.ApiClient()
        self.base = (
            f"{self.api.configuration.host}/api/v1/namespaces/{namespace}"
            f"/services/{service}:{port}/proxy"
        )
        self.limits = limits

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base + path + (f"?{urlencode(params)}" if params else "")
        headers = {"Accept": "application/json"}
        # kubeconfig 用 token 认证时，需要手动带上 Authorization；用客户端证书时这里是空的
        auth = self.api.configuration.auth_settings().get("BearerToken") or {}
        if auth.get("value"):
            headers[auth["key"]] = auth["value"]
        try:
            resp = self.api.call_api("GET", url, header_params=headers, _request_timeout=15)
            body = resp.data if resp.data is not None else resp.read()
        except ApiException as e:
            # Prometheus 对语法错误返回 400 和 JSON 说明，原样告诉模型，让它自己改
            try:
                err = json.loads(e.body)
                raise ToolError(
                    f"Prometheus 返回错误: {err.get('errorType')}: {err.get('error')}"
                ) from e
            except (TypeError, ValueError):
                raise ToolError(f"Prometheus 返回 HTTP {e.status}") from e
        payload = json.loads(body)
        if payload.get("status") != "success":
            raise ToolError(
                f"Prometheus 返回错误: {payload.get('errorType')}: {payload.get('error')}"
            )
        return payload["data"]

    def promql_query(self, query: str, range_minutes: int | None, step: str | None) -> str:
        window = self.limits["lookback_minutes"]
        if not query.strip():
            raise ToolError("query 不能为空")
        if range_minutes is None:
            check_lookback(query, 0, window)
            data = self._get("/api/v1/query", {"query": query})
            return format_result(
                data, self.limits["prom_max_series"], self.limits["prom_max_points"]
            )

        if range_minutes < 1 or range_minutes > window:
            raise ToolError(f"range_minutes 必须在 1 到 {window} 之间（回看窗口 {window} 分钟）")
        range_seconds = range_minutes * 60
        check_lookback(query, range_seconds, window)
        step_seconds, note = choose_step(range_seconds, step, self.limits["prom_max_points"])
        end = time.time()
        data = self._get(
            "/api/v1/query_range",
            {"query": query, "start": end - range_seconds, "end": end, "step": step_seconds},
        )
        text = format_result(
            data, self.limits["prom_max_series"], self.limits["prom_max_points"], now=end
        )
        return text + (f"\n{note}" if note else "")

    def list_metrics(self, match: str) -> str:
        names = self._get("/api/v1/label/__name__/values")
        return filter_metric_names(names, match, self.limits["prom_max_metrics"])
