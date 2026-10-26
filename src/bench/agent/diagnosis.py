"""submit_diagnosis 的结构、原因分类，以及参数校验。

submit_diagnosis 是 agent 侧自己定义的工具（不在 MCP server 里）：
它不是去查询集群，而是模型“交卷”的动作。循环一收到合法的 submit_diagnosis 就结束。

"""

# 分类 -> 给模型看的解释。顺序固定（影响缓存前缀，不要随意调整）
CAUSE_CATEGORIES: dict[str, str] = {
    "dependency_unavailable": "a component that this service depends on is not available",
    "misconfiguration_env": "a configuration value or environment variable is wrong",
    "missing_secret_or_config": "a required Secret or ConfigMap is missing",
    "resource_limit_cpu": "a CPU limit makes the service unable to keep up",
    "resource_limit_memory": "a memory limit causes out-of-memory kills",
    "kafka_topic_config": "a Kafka topic or internal topic is misconfigured",
    "probe_misconfiguration": "a health probe is misconfigured",
    "load_saturation": "the load exceeds the processing capacity",
    "partition_skew": "data is unevenly distributed across partitions",
    "transient_restart_no_action": "a short-lived restart that is recovering by itself; no action needed",
    "no_fault": "the system is healthy",
    "other": "none of the above",
}

COMPONENTS = [
    "ingestion-service",
    "stream-processor",
    "query-service",
    "kafka",
    "redis",
    "postgresql",
    "none",
]

CONFIDENCE_LEVELS = ["low", "medium", "high"]

SUBMIT_DIAGNOSIS_TOOL = {
    "name": "submit_diagnosis",
    "description": (
        "Submit your final diagnosis and finish the investigation. Call this exactly once, "
        "when you are ready to commit to a root cause (or to conclude the system is healthy)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cause_category": {
                "type": "string",
                "enum": list(CAUSE_CATEGORIES),
                "description": "Root cause category. "
                + "; ".join(f"{k}: {v}" for k, v in CAUSE_CATEGORIES.items()),
            },
            "component": {
                "type": "string",
                "enum": COMPONENTS,
                "description": "The component where the root cause is, or none if healthy.",
            },
            "evidence": {
                "type": "array",
                "minItems": 1,
                "description": "The observations that support the diagnosis.",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "Which tool call this came from, e.g. get_pod_logs(default, query-service-abc)",
                        },
                        "observation": {
                            "type": "string",
                            "description": "What you saw in that tool output.",
                        },
                    },
                    "required": ["source", "observation"],
                },
            },
            "recommended_action": {
                "type": "string",
                "description": "What should be done to fix it.",
            },
            "confidence": {"type": "string", "enum": CONFIDENCE_LEVELS},
        },
        "required": [
            "cause_category",
            "component",
            "evidence",
            "recommended_action",
            "confidence",
        ],
    },
}


def validate_diagnosis(args: object) -> list[str]:
    """返回问题列表；空列表表示合法。

    模型有时会给出枚举之外的值或漏字段（兼容端点不一定强制 schema），所以不能只信 schema。
    问题描述会原样作为 tool_result 返回给模型，让它自己改正后重新提交，所以要写得具体。
    """
    if not isinstance(args, dict):
        return ["参数必须是一个 JSON 对象"]
    problems = []

    category = args.get("cause_category")
    if category not in CAUSE_CATEGORIES:
        problems.append(f"cause_category {category!r} 无效，必须是: {', '.join(CAUSE_CATEGORIES)}")

    component = args.get("component")
    if component not in COMPONENTS:
        problems.append(f"component {component!r} 无效，必须是: {', '.join(COMPONENTS)}")

    evidence = args.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        problems.append("evidence 必须是非空列表")
    else:
        for i, item in enumerate(evidence):
            ok = (
                isinstance(item, dict)
                and isinstance(item.get("source"), str)
                and item["source"].strip()
                and isinstance(item.get("observation"), str)
                and item["observation"].strip()
            )
            if not ok:
                problems.append(f"evidence[{i}] 必须包含非空的 source 和 observation 字符串")

    action = args.get("recommended_action")
    if not isinstance(action, str) or not action.strip():
        problems.append("recommended_action 必须是非空字符串")

    confidence = args.get("confidence")
    if confidence not in CONFIDENCE_LEVELS:
        problems.append(f"confidence {confidence!r} 无效，必须是: {', '.join(CONFIDENCE_LEVELS)}")

    return problems
