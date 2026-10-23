# ADR 005: Scope of the in-cluster Prometheus

> English version first, Chinese version below.

## Status

Accepted for stage 2.

## Context

Some faults (CPU throttling, memory pressure, overload) are visible mainly as numbers, not as log lines or
events, so the agent needs metrics. The Prometheus that music-streaming ships runs outside the cluster in
docker-compose and scrapes through port-forward, which dies whenever a pod is recreated (music-streaming ADR 007),
exactly what our scenarios do all the time. We must not change the music-streaming repository or its compose file.

## Decision

Deploy a **separate Prometheus inside the cluster**, in its own `monitoring` namespace (image pre-loaded into
minikube, 2 h retention, data in an `emptyDir`; it is disposable evaluation infrastructure).

**Scraped**

- The three services' `/actuator/prometheus`, by **pod-level service discovery** (not through the Service), so every
  replica of a deployment is scraped, and a restarted pod keeps being found.
- The kubelet's **cAdvisor** (container CPU, memory, CFS throttling, OOM events) through the API server's node proxy,
  keeping only series whose `namespace` is `default`, `kafka` or `data`.

**Deliberately not scraped**

- The **traffic generator** (`bench` namespace). Its success / failure counters are effectively "what a user sees";
  they feed the injector and the health check only. Letting the agent query them would hand it the symptom.
- `monitoring` and `kube-system` containers, so the agent cannot be drawn into the evaluation's own plumbing.
- No Kafka broker exporter, no node exporter, no blackbox probes. Consumer lag stays available through the dedicated
  `get_consumer_lag` tool, and every extra data source is another place for a scenario trace to hide.

**How the agent reaches it**: two tools only, `promql_query` and `list_metrics`, through the API server's service proxy.
There is no raw Prometheus API, no `kubectl`-style access to the `monitoring` namespace. Results are capped (series, points,
metric names) with an explicit note when truncated, and all queries obey the lookback window of ADR 004. The prompt gains one
sentence saying the tools exist and **names no metric and hints at no fault**; the agent discovers metrics with `list_metrics`.

## Known limits

- **This minikube (docker runtime) exposes only pod-level cgroup series in cAdvisor**; the `container`, `image`
  and `name` labels are empty. CPU, memory, throttling and OOM counters therefore exist per pod, not per container.
  That is enough for the single-container pods here, but it is not what a production cluster would show.
- Pod-level `container_oom_events_total` is available, but whether a given out-of-memory death is reported there
  depends on how the process died (cgroup OOM kill vs. the JVM exiting by itself); see the `sp-oom` stability report.
- The agent can see scrape targets (`up` series with `app` / `pod` labels). That is the same information `list_pods` gives.
- Retention of 2 hours is shorter than a batch; the agent never needs older data (its window is 10 minutes) and
  the evaluation does not use Prometheus history for scoring.

---

# ADR 005：集群内 Prometheus 的范围

## 状态

第 2 阶段采纳。

## 背景

有些故障（CPU 限流、内存压力、过载）主要体现在数字上，而不是日志或事件里，所以 agent 需要指标。music-streaming 自带的
Prometheus 跑在集群外的 docker-compose 里，靠 port-forward 抓取，Pod 一重建就断（见 music-streaming 的 ADR 007），
而我们的场景恰恰一直在重建 Pod。我们又不能改 music-streaming 仓库和它的 compose 文件。

## 决定

在集群内部署一个**独立的 Prometheus**，放在单独的 `monitoring` namespace 里（镜像预先放进 minikube，保留 2 小时，
数据放 `emptyDir`；它是用完即弃的评测设施）。

**抓取**

- 三个业务服务的 `/actuator/prometheus`，用 **Pod 级服务发现**（不经过 Service）：多副本时每个 Pod 都能被抓到，
  Pod 重启后也能被重新发现。
- kubelet 的 **cAdvisor**（容器 CPU、内存、CFS 限流、OOM 事件），通过 API server 的节点代理访问，
  只保留 `namespace` 为 `default`、`kafka`、`data` 的序列。

**刻意不抓**

- **流量发生器**（`bench` namespace）。它的成功 / 失败计数相当于“用户侧看到的症状”，只给注入器和健康检查使用；
  让 agent 能查到，等于直接把症状递给它。
- `monitoring` 和 `kube-system` 里的容器，避免 agent 被评测基础设施本身带偏。
- 不抓 Kafka broker 的 exporter、节点 exporter、黑盒探测。消费者 lag 继续由专门的 `get_consumer_lag` 工具提供；
  每多一个数据源，就多一个场景痕迹可以藏身的地方。

**agent 怎么访问**：只有两个工具，`promql_query` 和 `list_metrics`，经由 API server 的 Service 代理。没有原始的
Prometheus API，也没有对 `monitoring` namespace 的 kubectl 类访问。结果有上限（序列数、点数、指标名数），截断时会明确写出，
所有查询都遵守 ADR 004 的回看窗口。系统提示只增加一句话说明这两个工具存在，**不列任何指标名，也不暗示任何故障**；
agent 用 `list_metrics` 自己发现指标。

## 已知的局限

- **这个 minikube（docker 运行时）里 cAdvisor 只暴露 Pod 级的 cgroup 序列**，`container`、`image`、`name` 标签都是空的。
  所以 CPU、内存、限流和 OOM 计数是按 Pod 而不是按容器给出的。对这里的单容器 Pod 来说够用，但这不是生产集群会看到的样子。
- Pod 级的 `container_oom_events_total` 存在，但某次内存不足导致的退出会不会记在上面，取决于进程是怎么死的
  （cgroup OOM kill，还是 JVM 自己退出）；见 `sp-oom` 的稳定性报告。
- agent 能看到抓取目标（带 `app` / `pod` 标签的 `up` 序列），这和 `list_pods` 给出的信息相同。
- 保留 2 小时比一个批次短；agent 永远用不到更早的数据（窗口只有 10 分钟），评分也不使用 Prometheus 历史。
