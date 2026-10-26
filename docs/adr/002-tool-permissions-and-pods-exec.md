# ADR 002: Permission boundary of the stage-1 agent tools, and the `pods/exec` problem

> English version first, Chinese version below.

## Status

Accepted for stage 1. The `pods/exec` part is a **known limitation** to be fixed in stage 3.

## Context

The agent investigates a cluster in which a fault has been injected. The evaluation is only
meaningful if the agent really can only look, and cannot change anything or see what the
harness knows (the scenario, the injector, the traffic generator).

## Decision

### Boundaries enforced in code (stage 1)

1. **Two separate code bases.** The injector and health check use `bench/admin_k8s.py`, which
   has write access. Everything the agent can use lives in `bench/tools/` and **must not import
   `admin_k8s`**. `tests/test_tools.py` checks this statically (AST scan) and at runtime
   (importing the tool modules must not load `admin_k8s`).
2. **Read-only calls only.** Tool code only calls list / read APIs. A test scans the sources and
   fails if a `create_`, `delete_`, `patch_` or `replace_` call appears.
3. **Namespace allow-list.** Only `default`, `kafka` and `data` are visible. The `bench`
   namespace (traffic generator) and system namespaces are rejected before any API call.
4. **Secrets are never read.** No Secret API is called. Environment variables that come from a
   Secret are shown only as a reference (`POSTGRES_PASSWORD <- secret postgres-credentials`).
   Caveat: pod logs are shown as they are; if an application logged a secret, the agent would
   see it. This system does not.
5. **Bounded output.** Every tool output is truncated and says so, with the original length.
   Log `tail` has a server-side cap. Tool errors come back as text instead of exceptions.
6. **The tool process does not receive the LLM API key.** The MCP server is a child process with
   the default minimal environment, so `DEEPSEEK_API_KEY` stays in the agent process only.
7. **No leftovers in the cluster.** The injector uses a neutral field manager name and does not
   write scenario names into labels, annotations or object names. Before every run the harness
   deletes the Event objects of the three visible namespaces, otherwise events of the previous
   run would leak into `get_events`.

### The `pods/exec` problem (known limitation)

`get_consumer_lag` runs `kafka-consumer-groups.sh --describe` inside the Kafka pod, which needs
`pods/exec`. In Kubernetes RBAC, `pods/exec` is **not** a read-only permission: whoever holds it
can run any command in the pod.

In stage 1 the tools run with the same kubeconfig as the harness anyway, so the read-only
guarantee comes from the code, not from RBAC. To keep the exec path as narrow as possible:

- the command is built by one function (`build_lag_command`) and is always
  `kafka-consumer-groups.sh --bootstrap-server <fixed> --describe --group <group>`;
- it is passed as an argument list, never through a shell;
- `group` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$` (it cannot start with `-`, so it cannot be
  read as another option; a unit test found this gap during development);
- the tool has no parameter that can change the script or the sub-command.

### Stage 3 plan

- Run the agent tools with a dedicated ServiceAccount whose RBAC allows only `get/list` on pods,
  `pods/log`, events and deployments in the three namespaces. No Secret access, no `pods/exec`.
- Replace the exec-based lag tool with something that needs no exec, for example a Kafka
  consumer-lag exporter read through Prometheus, or the Kafka admin API
  (`list_consumer_group_offsets`) called from a pod that can reach the broker.
- Add an audit log of every attempted call, including rejected ones, to measure out-of-bounds
  attempts.

## Consequences

- Stage-1 results about "out-of-bounds attempts" only reflect what the code allows; they say
  nothing about RBAC.
- Until stage 3, anyone who can run the tool process can also use the harness kubeconfig.

---

# ADR 002：第 1 阶段 agent 工具的权限边界，以及 `pods/exec` 问题

## 状态

第 1 阶段采纳。`pods/exec` 这一部分是**已知限制**，第 3 阶段处理。

## 背景

agent 要在一个被注入了故障的集群里做诊断。评估只有在 agent 真的只能“看”、不能改任何东西、
也看不到测试框架自己知道的东西（场景、注入器、流量发生器）时才有意义。

## 决定

### 在代码里落实的边界（第 1 阶段）

1. **两套代码。** 注入器和健康检查用 `bench/admin_k8s.py`（有写权限）。agent 能用的一切都在
   `bench/tools/` 里，并且**不得 import `admin_k8s`**。`tests/test_tools.py` 做了静态检查（扫描
   AST）和运行时检查（导入工具模块后 `admin_k8s` 不能被加载）。
2. **只调用读接口。** 工具代码只调用 list / read 类接口。有一个测试会扫描源码，出现
   `create_`、`delete_`、`patch_`、`replace_` 调用就失败。
3. **namespace 白名单。** 只能看 `default`、`kafka`、`data`。`bench` namespace（流量发生器）和系统
   namespace 在发起任何 API 调用之前就被拒绝。
4. **从不读取 Secret。** 不调用任何 Secret 接口。环境变量里来自 Secret 的值只显示引用
   （`POSTGRES_PASSWORD <- secret postgres-credentials`）。注意：Pod 日志是原样显示的，如果应用
   把密码打进了日志，agent 能看到。本系统没有这种情况。
5. **输出有上限。** 每个工具的输出都会截断并注明原长度；日志 `tail` 在服务端有上限；工具出错
   返回文本而不是抛异常。
6. **工具进程拿不到 LLM 的 API key。** MCP server 是子进程，只继承最小的默认环境变量，
   `DEEPSEEK_API_KEY` 只留在 agent 进程里。
7. **集群里不留痕迹。** 注入器使用中性的 field manager 名字，不把场景名写进 label、annotation
   或对象名。每次运行前，测试框架会删除三个可见 namespace 里的 Event 对象，否则上一次运行的事件
   会通过 `get_events` 漏给下一次。

### `pods/exec` 问题（已知限制）

`get_consumer_lag` 要在 Kafka Pod 里执行 `kafka-consumer-groups.sh --describe`，这需要
`pods/exec`。在 Kubernetes RBAC 里，`pods/exec` **不是**只读权限：拥有它的人可以在 Pod 里执行任意命令。

第 1 阶段工具本来就用和测试框架同一份 kubeconfig，所以“只读”是靠代码保证的，不是靠 RBAC。
为了把 exec 这条路尽量收窄：

- 命令只由一个函数（`build_lag_command`）生成，永远是
  `kafka-consumer-groups.sh --bootstrap-server <固定值> --describe --group <group>`；
- 以参数列表的形式传递，不经过 shell；
- `group` 必须匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`（不能以 `-` 开头，所以不会被当成另一个选项；
  这个漏洞是开发时被一条单元测试发现的）；
- 工具没有任何参数能改变脚本或子命令。

### 第 3 阶段计划

- agent 工具改用专门的 ServiceAccount，RBAC 只允许在三个 namespace 里对 Pod、`pods/log`、
  Event、Deployment 做 `get/list`；不能访问 Secret，不能 `pods/exec`。
- 把基于 exec 的 lag 工具换成不需要 exec 的方式，例如通过 Prometheus 读 Kafka consumer-lag
  exporter，或者在一个能访问 broker 的 Pod 里调用 Kafka admin API（`list_consumer_group_offsets`）。
- 增加审计日志，记录每一次尝试的调用（包括被拒绝的），用来统计越权尝试。

## 影响

- 第 1 阶段关于“越权尝试”的结果只反映代码允许什么，说明不了 RBAC 层面的情况。
- 到第 3 阶段之前，能运行工具进程的人也能使用测试框架的 kubeconfig。
