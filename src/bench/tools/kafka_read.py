"""agent 的消费者 lag 工具。

已知问题（第 3 阶段处理，会写进 ADR）：这个工具需要在 Kafka Pod 里 exec 命令，
而 RBAC 层面 `pods/exec` 不是只读权限，它能执行任意命令。第 1 阶段先这样实现，
但代码里只允许执行下面 build_lag_command 生成的那一条固定命令，不接受任何拼接参数：
  - 命令以列表形式传给 API，不经过 shell，没有注入的可能；
  - 唯一可变的参数 group 先过正则校验；
  - 子命令固定是 --describe，工具没有任何办法让它执行别的 kafka 脚本。
"""

import re

from kubernetes import client, config
from kubernetes.stream import stream

from bench.config import KafkaConfig
from bench.kafka_lag import parse_describe, total_lag
from bench.tools.common import ToolError

# 消费者组名只允许这些字符。Kafka 允许的字符更宽，但本系统的组名都很规矩，
# 宁可严一点：校验不通过就直接拒绝，不尝试“清洗”。
# 第一个字符必须是字母或数字：以 "-" 开头的值会被脚本当成另一个命令行选项（比如 --delete）
GROUP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def build_lag_command(kafka: KafkaConfig, group: str) -> list[str]:
    if not GROUP_PATTERN.fullmatch(group):
        raise ToolError("group 名只能包含字母、数字、点、下划线和连字符")
    return [
        kafka.consumer_groups_script,
        "--bootstrap-server",
        kafka.bootstrap_server,
        "--describe",
        "--group",
        group,
    ]


def format_lag(group: str, output: str) -> str:
    rows = parse_describe(output)
    if not rows:
        # 解析不出分区（组不存在、脚本报错等）时，把原始输出给模型看，不要吞掉
        return f"没有解析到分区信息。命令原始输出:\n{output.strip() or '(空)'}"
    unknown = sum(1 for r in rows if r.lag is None)
    lines = [
        f"Consumer group: {group}  总 lag: {total_lag(rows)}"
        + (f"（另有 {unknown} 个分区 lag 未知）" if unknown else ""),
        f"{'TOPIC':45} {'PARTITION':9} {'CURRENT':>10} {'LOG-END':>10} {'LAG':>8}",
    ]
    for r in rows:
        lines.append(
            f"{r.topic:45} {r.partition:<9} {r.current_offset if r.current_offset is not None else '-':>10} "
            f"{r.log_end_offset if r.log_end_offset is not None else '-':>10} "
            f"{r.lag if r.lag is not None else '-':>8}"
        )
    return "\n".join(lines)


class KafkaReader:
    def __init__(self, kube_context: str, kafka: KafkaConfig):
        config.load_kube_config(context=kube_context)
        self.core = client.CoreV1Api()
        self.kafka = kafka

    def get_consumer_lag(self, group: str) -> str:
        command = build_lag_command(self.kafka, group)
        output = stream(
            self.core.connect_get_namespaced_pod_exec,
            self.kafka.pod,
            self.kafka.namespace,
            container=self.kafka.container,
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
        return format_lag(group, output)
