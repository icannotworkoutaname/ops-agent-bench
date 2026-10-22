"""解析 kafka-consumer-groups.sh --describe 的输出。

这是一个不依赖 k8s 的纯函数模块，管理侧（health.py）和 agent 侧（tools/kafka_read.py）
都会用它。放在这里而不是 admin_k8s.py，是因为 tools/ 不允许 import 管理用的 k8s 客户端
（隔离原则 1），但解析文本本身没有权限问题，两边共用同一份可以避免两套解析逻辑不一致。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PartitionLag:
    topic: str
    partition: int
    current_offset: int | None
    log_end_offset: int | None
    lag: int | None  # 脚本在拿不到值时会输出 "-"，这里记为 None


def _to_int(text: str) -> int | None:
    return None if text == "-" else int(text)


def parse_describe(output: str) -> list[PartitionLag]:
    """按表头的列名定位各列，而不是写死列号，脚本多出一列时不会错位。

    没有表头（例如组不存在、或者脚本报错）时返回空列表，由调用方决定怎么处理。
    """
    rows: list[PartitionLag] = []
    columns: dict[str, int] | None = None
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "GROUP":
            columns = {name: i for i, name in enumerate(parts)}
            continue
        if columns is None or len(parts) <= columns["LAG"]:
            # 只要求数据行至少有到 LAG 为止的列；后面的 CONSUMER-ID / HOST / CLIENT-ID
            # 没有活跃成员时是 "-"，我们用不到。更短的行是提示信息，不是数据行
            continue
        try:
            rows.append(
                PartitionLag(
                    topic=parts[columns["TOPIC"]],
                    partition=int(parts[columns["PARTITION"]]),
                    current_offset=_to_int(parts[columns["CURRENT-OFFSET"]]),
                    log_end_offset=_to_int(parts[columns["LOG-END-OFFSET"]]),
                    lag=_to_int(parts[columns["LAG"]]),
                )
            )
        except (ValueError, KeyError, IndexError):
            continue  # 看不懂的行直接跳过，不让一行异常输出拖垮整个解析
    return rows


def total_lag(rows: list[PartitionLag]) -> int:
    """所有分区 lag 之和。lag 为 None 的分区不计入，调用方应单独判断是否有这种分区。"""
    return sum(r.lag for r in rows if r.lag is not None)
