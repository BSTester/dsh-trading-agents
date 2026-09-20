# 监控落地（规格 §8.3）：Prometheus 抓取 + 告警规则

本目录只放**可直接用的配置文件与说明**，不安装任何系统服务、不改 `systemd`。
平台侧的改动是一个 Prometheus 文本出口（`platform/server/observability.py` 的
`GET /metrics`）、两个探测结果落盘（`/api/v3/sources/status` 写 `v3-datasource-probe.json`；
`POST /api/v3/metrics/probe/refresh` 写 `v3-risk-probe.json`），以及**平台内规则求值器**
（`platform/server/v3_alerts.py`，`GET /api/v3/ops/alerts`），**只加不删**。

| 文件 | 作用 |
|---|---|
| `prometheus.yml` | 抓取配置：目标 `127.0.0.1:8397`、路径 `/metrics`、间隔 15s |
| `alerts.yml` | 33 条告警规则（8 组），每条阈值都写明依据。**两个求值方读同一份**：Prometheus（`rule_files`）与平台内求值器 `platform/server/v3_alerts.py` |
| `dump_metrics.py` | 离线导出 `/metrics` 文本，供 `promtool check metrics` 自检（服务没起也能校验） |
| `verify_industry_gate.py` | **真机只读**验证行业红线闸门：读真实 `~/.dsh/v3-risk-probe.json`、用构造订单过 `check_order`/`_upsert`，打印真实 reasons/history 与 fail-open 样例（不下单、不写盘） |
| `README.md` | 本文件 |

> **不装 Grafana/Prometheus 也能用**：见 §九「无 Grafana 的替代方案」——
> `/metrics`（标准文本，谁都能抓）+ `platform/server/v3_alerts.py`（进程内规则求值，
> `GET /api/v3/ops/alerts` 三态）+ 工作台「网关与调度」页的「运维告警」卡片。
> 将来接 Prometheus/Grafana/Alertmanager 时**不需要改平台代码**（同一份 `alerts.yml`）。

---

## 一、先认清一件事：`/metrics` 是本次**新增**的

任务书预设「`/metrics`（Prometheus 文本）已存在」。**实测并非如此**：

```console
$ curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://127.0.0.1:8397/metrics
200 text/html; charset=utf-8          # ← 是 SPA 首页，不是指标
$ grep -rn 'get("/metrics")' platform/
（无输出）
```

原因：`platform/server/app.py` 末尾有 SPA 兜底路由 `@app.get("/{path:path}")`，
任何未注册的路径都会返回 `platform/web-pro` 的 `index.html`。当时仓库里只有
`/api/v3/metrics`（给人读的 JSON），没有 Prometheus 文本出口。

因此本次**新建**了 `platform/server/observability.py` 并把它接进 `app.py` 的子模块注册循环
（**必须排在 SPA 兜底之前**，否则仍会被吃掉——`tests/test_observability.py` 里有专门的
`test_route_wins_over_spa_catch_all` 守住这条）。

> **部署提醒**：运行中的服务进程（`python -m server.run`）是**改动前**启动的，
> 必须由运维统一重启一次，`/metrics` 才会出现。重启前想验证，用下面的
> `dump_metrics.py` 离线校验——它建的是真实应用，只是不跑 lifespan。

---

## 二、启动 Prometheus

### 方式 A：本地二进制（无需容器）

```bash
# 版本随意，1.x/2.x/3.x 均可；把配置目录指向本目录
prometheus --config.file=platform/deploy/monitoring/prometheus.yml \
           --storage.tsdb.path=/tmp/prom-data
# 打开 http://127.0.0.1:9090/targets 看 quantwb-v3 是否 UP
```

### 方式 B：容器（本机已装 docker，且已实测跑通）

```bash
cd /home/penn/workspace/dsh-trading-agents
docker run --rm -p 9090:9090 \
  -v "$PWD/platform/deploy/monitoring:/etc/prometheus:ro" \
  prom/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/prometheus
```

> **桥接网络下抓不到 8397，这不是 bug。** 平台服务默认只绑回环
> （`server/config.py`: `DEFAULTS = {"port": 8397, "host": "127.0.0.1"}`），
> 而 `host.docker.internal` 指向的是宿主在 docker 网桥上的地址——服务压根没监听那个地址。
> 三种可行做法：
> 1. **`--network host`（Linux 最省事，推荐）**：容器共享宿主网络栈，
>    `127.0.0.1:8397` 就是宿主回环，`prometheus.yml` 一字不用改：
>    `docker run --rm --network host -v "$PWD/platform/deploy/monitoring:/etc/prometheus:ro" prom/prometheus`
> 2. 让服务监听全地址：改 `<DSH_HOME>/trading-platform.json`
>    （平台**没有** host 环境变量，只有 `TRADING_SERVICE_PORT` 这一个端口环境变量）：
>    `{"service": {"host": "0.0.0.0", "port": 8397}}`，**重启服务**后把 targets 改成
>    `host.docker.internal:8397` 并加 `--add-host=host.docker.internal:host-gateway`。
>    注意这会扩大暴露面，需自行加防火墙。
> 3. 在 Prometheus 侧不做容器、直接在宿主跑（方式 A）。

---

## 三、校验配置与指标

### 3.1 规则语法（已实测通过）

```console
$ docker run --rm -v "$PWD/platform/deploy/monitoring:/etc/prometheus:ro" \
    --entrypoint promtool prom/prometheus check rules /etc/prometheus/alerts.yml
Checking /etc/prometheus/alerts.yml
  SUCCESS: 23 rules found

$ docker run --rm -v "$PWD/platform/deploy/monitoring:/etc/prometheus:ro" \
    --entrypoint promtool prom/prometheus check config /etc/prometheus/prometheus.yml
Checking /etc/prometheus/prometheus.yml
  SUCCESS: 1 rule files found
 SUCCESS: /etc/prometheus/prometheus.yml is valid prometheus config file syntax
Checking /etc/prometheus/alerts.yml
  SUCCESS: 23 rules found
```

没有 `promtool` 时用上面的容器命令即可（本机已 `docker pull prom/prometheus:latest` 成功）。
若镜像拉不动（离线环境），只能做到 YAML 语法层面的自检，**规则语义未校验**——请如实记录。

### 3.2 指标文本格式（`promtool check metrics`）

服务重启后可对真实端点做 lint：

```bash
curl -s localhost:8397/metrics | docker run --rm -i --entrypoint promtool prom/prometheus check metrics
```

**服务尚未重启时用离线导出**（建真实应用、不跑 lifespan、无外部调用）：

```console
$ cd platform
$ ~/.dsh/trading-venv/bin/python -B deploy/monitoring/dump_metrics.py \
      --with-probe-cache --with-risk-probe-cache > /tmp/metrics.txt
$ docker run --rm -i --entrypoint promtool prom/prometheus check metrics < /tmp/metrics.txt
futu_cooldown_remaining_ms metric names should not contain abbreviated units
futu_throttle_wait_ms_total metric names should not contain abbreviated units
```

**只剩这两条是有意保留的**（不是漏修），理由：

- `futu_cooldown_remaining_ms` 是**规格 §8.3 点名要的规则输入**，且直接对应平台字段
  `v3_ratelimit.metrics_view()` 的 `cooldownRemainingMs`；
- `futu_throttle_wait_ms_total` 与它同源（`throttleWaitMs`），改名会让两个同族指标不一致。

lint 是风格建议，不影响抓取与 PromQL。其余 lint 项都已按 Prometheus 约定改正：
耗时用**秒**（`quantwb_mcp_call_duration_seconds`）、不再用 `_count` 后缀
（`quantwb_datasource_available_chains`）、计数器一律带 `_total`。

> `tests/test_observability.py::AlertRulesTests` 会用 AST 提取出口**所有可能输出**的指标名，
> 再断言 `alerts.yml` 里引用的每个 `quantwb_*`/`futu_*` 都在其中——**指标名写错会直接测挂**，
> 不靠人眼核对。

---

## 四、告警怎么接

### 现状：不接 Alertmanager，先用 Prometheus 自身的告警面

本目录**不预设** Alertmanager（本机没有，装它属于运维动作）。默认情况下告警会：
1. 出现在 Prometheus UI 的 **`/alerts`** 页面（含 `pending`/`firing` 状态）；
2. 打进 Prometheus 日志。

**这就够值班用**：`/alerts` 上 `firing` 的条目就是待处理事项。

### 要接 Alertmanager 时

1. 部署 Alertmanager 并配好 receiver（邮件/飞书 webhook 等），监听 `127.0.0.1:9093`；
2. 把 `prometheus.yml` 里 `alerting:` 段取消注释：

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['127.0.0.1:9093']
```

3. 重启 Prometheus，在 `/status` 页确认 Alertmanager 已发现。

> 仓库里**没有**任何飞书/邮件告警通道的实现，所以「接 Alertmanager」这一步需要运维另配。
> 本目录只保证规则本身可用。

---

## 五、指标清单（出口真实输出的全部家族）

来自 `platform/server/observability.py`。**没有真实读数就不导出该家族**——所以下面带
「探测」的来源在首次抓取后会缺席几秒，那是**有意的**：导出 0 会被读成「正常」，属于编造。

### 进程 / 构建

| 指标 | 类型 | 含义 |
|---|---|---|
| `quantwb_up` | gauge | 恒为 1；能抓到就说明进程在提供 `/metrics` |
| `quantwb_process_start_time_seconds` | gauge | 进程启动的 Unix 时间戳（重启检测靠它） |
| `quantwb_process_uptime_seconds` | gauge | 已运行秒数 |
| `quantwb_build_info{version,tools,domains}` | gauge | 构建信息，值恒为 1 |
| `quantwb_tools{scope="mcp"}` | gauge | **MCP 工具面真值**（`MCPServer` 注册表动态读取，与 `/mcp` 的 `tools/list` 同源；含 `v3_*` 桥接工具） |
| `quantwb_tools{scope="domain"}` | gauge | 六域工具目录数（`mcp_tools.TOOLS` 枚举，77 代理 + 5 本地计算）；**口径不同，不可与 mcp 相加或互相替代** |

### 工具面 / HTTP

| 指标 | 类型 | 含义 |
|---|---|---|
| `quantwb_http_requests_total` | counter | v3_ops 注册路由的请求数 |
| `quantwb_http_errors_total` | counter | 同上，失败数 |
| `quantwb_mcp_calls_total` | counter | 工具面调用数（HTTP `/api/wb/*` 与 MCP `/mcp` 同一 handle、同一份计数） |
| `quantwb_mcp_errors_total` | counter | 工具面失败数 |
| `quantwb_mcp_call_duration_seconds` | gauge | 工具面平均耗时（秒） |
| `quantwb_tool_calls_total{tool="…"}` | counter | 按工具名的调用数 |

### 富途限流（`v3_ratelimit.metrics_view()` 唯一读数口）

| 指标 | 类型 | 含义 |
|---|---|---|
| `futu_enabled` | gauge | 限流器是否启用 |
| `futu_calls_total` | counter | 经限流器的调用数 |
| `futu_coalesced_total` | counter | 被单飞合并掉的调用数 |
| `futu_retries_total` | counter | 退避重试次数 |
| `futu_rate_limited_total` | counter | **上游返回限流错误的次数** |
| `futu_throttle_wait_ms_total` | counter | 为守速率上限主动等待的累计毫秒 |
| `futu_in_flight` | gauge | 当前在飞调用数 |
| `futu_queued` | gauge | 当前排队数 |
| `futu_cooldown_remaining_ms` | gauge | 冷却剩余毫秒（> 0 表示正在熔断等待） |

### OMS 台账

| 指标 | 类型 | 含义 |
|---|---|---|
| `quantwb_oms_orders{stage="…"}` | gauge | 台账订单按阶段分布；阶段 = `risk_passed`/`manual`/`blocked`/`submitted`/`filled`/`rejected`/`unknown`（表外阶段如实另立样本） |
| `quantwb_risk_single_order_pct_max` | gauge | 台账里**待人工确认**（`stage="manual"`）订单中最大的单笔占比（`value / nav_used`，%），即 2% 单笔红线（`v3_ops.LIMITS.singlePct`）的方向。**没有 manual 单就不导出**（缺席 ≠ 0%）。它是既有台账字段的换算，不需要任何外部探测；**不单独设告警**——它与 `RiskPendingManualConfirmation` 同源，再报一次只是重复 |

### 工作台可达性 / 风控（**缓存探测**，TTL 由 `QUANT_METRICS_PROBE_TTL` 控制，默认 60s）

| 指标 | 类型 | 含义 |
|---|---|---|
| `quantwb_workbench_up` | gauge | 1 = 工作台工具面只读探测可达 |
| `quantwb_workbench_probe_timestamp_seconds` | gauge | 最近一次探测时刻（规则用它做新鲜度守卫） |
| `quantwb_workbench_probe_nav` | gauge | 探测读到的模拟台账净值 |
| `quantwb_risk_drawdown_pct` | gauge | 探测读到的模拟台账最大回撤（%） |
| `quantwb_workbench_probe_failed{error="…"}` | gauge | **仅探测失败时出现**，值恒 1，原因在标签 |

### 调度器 / 推送（与 `/healthz` 同源）

| 指标 | 类型 | 含义 |
|---|---|---|
| `quantwb_scheduler_alive` | gauge | 1 = 调度器线程存活 |
| `quantwb_scheduler_last_error` | gauge | 1 = 存在未清除的最近一次 tick 异常（**成功不清除**） |
| `quantwb_push_enabled` | gauge | WS 推送运行时是否启用 |
| `quantwb_push_connected{channel="quote\|trade"}` | gauge | 该通道是否已连接并鉴权 |
| `quantwb_push_reconnects_total{channel="…"}` | counter | 该通道重连次数 |

### 数据源降级链（**读落盘缓存，抓取路径绝不打外部源**）

| 指标 | 类型 | 含义 |
|---|---|---|
| `quantwb_datasource_chains` | gauge | 缓存里登记的链数量 |
| `quantwb_datasource_available{chain="…"}` | gauge | 1 = 该链至少一级取到数据 |
| `quantwb_datasource_available_chains` | gauge | 可用的链数量 |
| `quantwb_datasource_is_fallback{chain="…"}` | gauge | 1 = 最近一次命中的是**降级源** |
| `quantwb_datasource_probe_timestamp_seconds{chain="…"}` | gauge | 该链最近一次真实探测时刻 |
| `quantwb_datasource_probe_info{chain,source,primary}` | gauge | 最近命中来源（info 指标，值恒 1） |

**为什么降级链指标不在抓取路径上现探测**：探测会真的打富途/akshare/SEC，而富途调用要过
限流器——**用监控去触发「被限流」告警是自伤**。所以缓存由 `GET /api/v3/sources/status`
的真实探测落盘（`<DSH_HOME>/v3-datasource-probe.json`），`/metrics` 只读它。
这意味着**降级链告警需要一个触发者**，见下节。

### 行业集中度红线（**读落盘缓存，抓取路径绝不打外部源**）

| 指标 | 类型 | 含义 |
|---|---|---|
| `quantwb_risk_industry_pct{scope="max"\|"SH"\|"HK"\|"US"}` | gauge | 行业集中度：单一行业占组合的最大权重（%）。`scope="max"` 是各市场最高值。数据来自富途板块行业映射（`v3_industry.industry_exposure`），权重来自平台组合口径 |
| `quantwb_risk_industry_probe_timestamp_seconds` | gauge | 缓存生成时刻（Unix 秒）；规则用 `time() - 该值` 做新鲜度守卫 |
| `quantwb_risk_industry_breach` | gauge | 1 = 最高暴露已超过探测时的 `limit_pct` 红线 |
| `quantwb_risk_industry_limit_pct` | gauge | 探测时实际用的红线（%）；规则里的 20 应与它一致（同源 `v3_ops.LIMITS.industryPct`） |
| `quantwb_risk_industry_missing{market="…"}` | gauge | 最高暴露所在市场里**没取到行业分类**的标的数；> 0 说明该读数只是**下界** |
| `quantwb_risk_industry_probe_info{market,industry,source}` | gauge | 最高暴露的行业/市场/板块来源（info 指标，值恒 1） |
| `quantwb_risk_industry_probe_failed{error="…"}` | gauge | **仅探测出错时出现**，值恒 1，原因在标签 |

缓存文件是 `<DSH_HOME>/v3-risk-probe.json`，由 **`POST /api/v3/metrics/probe/refresh`**
写入（触发者见 §六 第 5 条与 `platform/install/quant-v3-probe.timer`）。三条纪律：

1. **抓取路径不发探测**：`/metrics` 只读文件，富途调用只在写入器里发生；
2. **没有读数就不导出**：缓存缺失/损坏/无 `generated_at` → 整个 family 缺席；
   一个市场都没给出读数 → 不导出 `quantwb_risk_industry_pct`（不是导出 0）；
3. **过期即没有结论**：缓存超过 `QUANT_RISK_PROBE_MAX_AGE`（默认 `21600` 秒 = 6h）时，
   只导出 `..._probe_timestamp_seconds`（让「过期」可见），**不导出取值类指标**。

> **口径状态（2026-09-20 第二轮更新：观测先行 → 闸门生效）**：`v3_ops.OmsLedger` 不再用
> 常量 `0.0` 充当行业读数——`context()` / `check_order` 通过
> `server/v3_risk_gate.industry_context()` 读**同一份落盘探测缓存**（同一新鲜度常量
> `QUANT_RISK_PROBE_MAX_AGE`），行业超限会产生 `stage="blocked_industry"` 的硬阻断，
> 原因原文带 top 行业 / 来源 / `as_of` / 市场 / 探测年龄。
> 本组指标的语义因此从「仅供参考」升级为「与闸门同源」：`quantwb_risk_industry_pct`
> 与 OMS 台账里的 `industry_pct` 是同一份缓存的同一读数；台账侧新增计数见
> `/api/v3/metrics` 的 `industryGate`（`blockedIndustry` / `blockedByIndustry` /
> `failOpen` / `perMarket`）。
> **fail-open（有意为之）**：缓存过期/缺失且现取失败时**不阻断**，但订单 `reasons` 里必须
> 写明「行业暴露数据不可用，未参与阻断（原因：…）」——缺数据既不能静默放行、也不能
> 变成全量阻断。因此 `quantwb_risk_industry_probe_*` 的新鲜度告警现在是**闸门可用性**
> 告警，不只是观测告警。接法、fail-open 风险与调参见 `docs/e2e-and-data-gaps.md`
> §十一～§十四。

---

## 五之二、规格 §8.3 补齐项 + §4.1 分位数（2026-09-20 第二轮新增）

这一组指标的存在理由只有一条：**规格点名的 9 类监控里，原先有 5 类没有指标可判、2 类口径不符**。
数据源全部是平台**已经落库/落盘的事实**，没有为监控新增任何取数路径、没有新增外部调用。

| 指标 | 类型 | 含义 / 口径 |
|---|---|---|
| `quantwb_headless_window_seconds` | gauge | Headless 统计窗口（秒），由 `QUANT_HEADLESS_WINDOW_SECONDS` 配置（默认 86400） |
| `quantwb_headless_log_rows` | gauge | 从 `headless_log` 表（或 `<DSH_HOME>/v3-headless-log.jsonl` 冷备）读到的记录条数。**为 0 时下面的成功率/耗时一律不导出** |
| `quantwb_headless_calls` | gauge | 窗口内调用记录数 |
| `quantwb_headless_successes` | gauge | 窗口内成功记录数 |
| `quantwb_headless_success_rate` | gauge | 窗口内 `successes / calls`（0～1）。§8.3 阈值 0.95 |
| `quantwb_headless_call_duration_seconds` | gauge | 窗口内**平均**耗时（秒）。§8.3 阈值 60s |
| `quantwb_sdk_session_window_seconds` | gauge | SDK 活跃会话判定窗口（秒），由 `QUANT_SDK_SESSION_WINDOW_SECONDS` 配置（默认 300） |
| `quantwb_sdk_active_sessions{source="…"}` | gauge | SDK 活跃会话数。来源见标签：`v3_sdk.metrics_view()` 或 `sdk_turns`（窗口内出现 turn 的不同 `session_id`） |
| `quantwb_sdk_active_sessions_source_missing{reason="…"}` | gauge | **只在两条来源都拿不到时出现**（值恒 1 + 原因标签）；此时不导出 `quantwb_sdk_active_sessions` |
| `quantwb_risk_blocked_total` | counter | 进程内累计**首次观测到**进入阻断阶段（`blocked` / `blocked_industry`）的台账订单数；§8.3「风控阻断次数突增」的 `increase()` 输入 |
| `quantwb_order_execution_latency_max_seconds` | gauge | 窗口内**最大**的「首次登记（`first_seen_at`）→ 进入 `submitted`/`filled`」间隔（秒）。§8.3 阈值 1s |
| `quantwb_datasource_data_age_seconds{chain="…"}` | gauge | 该链最近一次探测命中来源返回的 `as_of` 距今秒数（= 数据延迟）。只对**有 `as_of`** 的链导出 |
| `quantwb_call_duration_window_seconds` | gauge | 分位数滑动窗口长度（秒），`QUANT_LATENCY_WINDOW_SECONDS`（默认 900） |
| `quantwb_call_duration_min_samples` | gauge | 导出分位数所需最小样本数，`QUANT_LATENCY_MIN_SAMPLES`（默认 20）；不足则不导出 |
| `quantwb_call_duration_window_samples{scope="…"}` | gauge | 该 scope 在窗口内的真实观测样本数 |
| `quantwb_call_duration_p50_seconds{scope="…"}` | gauge | 延迟 P50（秒），见下方「分位数口径」 |
| `quantwb_call_duration_p95_seconds{scope="…"}` | gauge | 延迟 P95（秒）。§8.3 MCP 阈值 5s；§4.1 目标：MCP 2s / 订单 0.5s / Headless 30s |
| `quantwb_call_duration_p99_seconds{scope="…"}` | gauge | 延迟 P99（秒） |

### 分位数口径（**不谎称精确**）

* **算法**：滑动窗口内的**原始样本** + **最近秩（nearest-rank，`ceil(q·n)` 名次，不插值）**。
  报告的是窗口里**真实观测到**的那个值——不像 PromQL 的 `histogram_quantile` 那样在桶内插值，
  也不做 `rate()`/`increase()` 那样的区间外推。
* **窗口**：默认 900s（`QUANT_LATENCY_WINDOW_SECONDS`），每个 scope 最多保留 4096 个样本。
* **最小样本**：默认 20（`QUANT_LATENCY_MIN_SAMPLES`）。**不足 20 个样本就不导出该 scope 的分位数**——
  「样本不足」是 no-data，规则随之显示 no-data，绝不拿一个不可信的分位数冒充读数。
* **scope（样本来源，逐一如实标注）**：

  | scope | 样本是什么 | 覆盖范围与已知偏差 |
  |---|---|---|
  | `mcp-tool-http` | `/mcp`、`/api/wb/*`、`/api/v3/*` 里**一次请求恰好一次工具调用**（用 `v3_ops` 计数器差值 `Δcalls == 1` 判定）的 HTTP 请求耗时 | 等于**真实单次工具调用**耗时 + 回环 HTTP 开销。实测命中：`/mcp` 的 JSON-RPC `tools/call`、`/api/v3/audit`、`/api/v3/gateway`、`/api/v3/metrics`。**多调用请求不计入**（如 `/api/v3/overview` 一次算 8 个工具）——宁缺毋滥；不含探测线程里直接调 `v3_run` 的调用，也不含 `Δcalls == 0` 的请求（如 `/api/wb/schedule` 走 store HTTP 桥、不经计数调用器）。因此它对「工具面整体延迟」是**有偏子集**，均值口径仍看 `quantwb_mcp_call_duration_seconds` |
  | `headless-log` | `headless_log` 记录的 `duration_ms / 1000` | 与 `quantwb_headless_calls` 同源；没有 `duration_ms` 字段的记录不喂样本 |
  | `oms-ledger` | 台账订单「`first_seen_at` → `history[].at` 进入 `submitted`/`filled`」的间隔 | **口径差异**：规格 §4.1 写的是「信号生成到订单提交」，台账能拿到的真实两端是**登记时刻**与**阶段迁移时刻** |
  | `datasource-probe` | 降级链探测里每一级上游调用的 `attempts[].ms` | 采样频率 = 探测频率（`/api/v3/sources/status` 被调用时才产生样本），**不是**持续采样；同一份缓存只喂一次 |

* **进度可见性**：`quantwb_call_duration_window_samples{scope=…}` 让人一眼看出「现在到底有几个样本」；
  `evaluator.history_stats`（`/api/v3/ops/alerts` 响应里）给出规则求值器自己的窗口年龄。
* **平台内求值器的窗口历史**是另一件事（规则里的 `rate()/increase()/delta()/changes()` 需要它）：
  默认 2h（`QUANT_ALERTS_HISTORY_SECONDS`），**进程重启后清零**，覆盖率不足 90% 时相关规则
  如实报 `no-data`——不学 PromQL 的区间外推。

---

## 六、当前指标缺口 → 需要平台补哪些 metric

这一节是**如实说明**，不是待办清单的美化版。

### 6.1 已解决（本次新增）

| 原先缺什么 | 现在 |
|---|---|
| Prometheus 文本出口 | ✅ `GET /metrics`（40+ 个家族） |
| 富途限流可观测 | ✅ `futu_*` 9 个（读既有唯一事实源） |
| 工作台工具面可达性 | ✅ `quantwb_workbench_up`（缓存探测） |
| MCP 失败率 | ✅ `quantwb_mcp_errors_total` / `quantwb_mcp_calls_total` |
| 降级链可用性 | ✅ `quantwb_datasource_available`（读探测缓存） |
| 回撤红线 | ✅ `quantwb_risk_drawdown_pct`（探测读模拟台账） |
| 重启/存活时长 | ✅ `quantwb_process_start_time_seconds` / `_uptime_seconds` |
| **行业集中度红线** | ✅ `quantwb_risk_industry_pct{scope="max"}` + 探测写入器 `POST /api/v3/metrics/probe/refresh`（读落盘缓存） |
| **单笔占比（2%）方向** | ✅ `quantwb_risk_single_order_pct_max`（读既有 OMS 台账，不新增探测） |
| **降级链/行业探测的触发者** | ✅ `platform/install/quant-v3-probe.{service,timer}`（本轮产出素材，**未安装**） |

### 6.2 仍是缺口 —— **建议新增的指标名**

| # | 缺口 | 为什么现在没有 | 建议新增的指标名 | 加在哪 |
|---|---|---|---|---|
| 1 | ~~**行业集中度红线**（20%）~~ **已解决** | 上一轮结论「平台没有行业分类数据源」**已过时**：`GET /api/v3/risk/industry` 能取到真实富途板块行业映射（实测 SH top=股份制银行Ⅱ 37.5% > 20% → `breach=true`）。现在读落盘探测缓存导出 | ✅ `quantwb_risk_industry_pct{scope="max"}` | `observability._render_risk_industry` 读 `<home>/v3-risk-probe.json`（写入器见 §六 第 5 条） |
| 2 | ~~**单笔占比红线**（2%）~~ **已解决（只读口径）** | 台账已落盘 `value` 与 `nav_used`，可直接换算待确认单的最大单笔占比 | ✅ `quantwb_risk_single_order_pct_max` | `observability._render_oms` 读既有 `OmsLedger` |
| 3 | **在途/待确认计数** | `oms_orders{stage}` 已覆盖阶段分布，但「业务确认 TTL 120s 内待作答」这件事只在 `confirmation` 工具返回值里 | `quantwb_confirmation_pending` | `v3_ops` 读 `confirmation` 时写 gauge |
| 4 | **对账差异数** | `/api/v3/audit` 有 `tca`/差异行，但是随请求计算的，无进程内状态 | `quantwb_reconcile_diff_rows` | 对账作业落盘后由 `observability` 读 |
| 5 | ~~**降级链探测的自动触发**~~ **已解决（素材）** | 探测要打外部源，**不能**放在抓取路径上。本轮产出可直接安装的 systemd 单元素材（`quant-v3-probe.service` + `.timer`），同时触发 `/api/v3/sources/status` 与 `/api/v3/metrics/probe/refresh`；**未安装到系统**（需运维 `systemctl --user enable --now`） | `platform/install/quant-v3-probe.*` + `platform/install/README.md` | `platform/install/` |
| 6 | **成交质量/滑点** | `v3_quality` 的指标随请求计算 | `quantwb_execution_slippage_bps` | `v3_quality` 落盘后由 `observability` 读 |
| 7 | **跨进程聚合** | `/metrics` 是**单进程**进程内计数。若将来 uvicorn 开多 worker，计数器各算各的 | 需要改为多进程/共享存储口径 | 暂不需要（当前单进程） |

> 第 1 条**已更正**：上一轮写的「平台拿不到行业分类数据」不再成立（`docs/e2e-and-data-gaps.md`
> 的「行业分类与行业暴露」一节与本文 §6 都按此更新）。
> **2026-09-20 第二轮**：闸门也已接同一口径——`check_order` 对 `industry_pct > 20%` 直接
> 硬阻断（`stage="blocked_industry"`），没有新鲜读数时 fail-open 但强制留痕。
> 即：这一条从「观测」升级为「观测 + 阻断」，见上节「口径状态」与
> `docs/e2e-and-data-gaps.md` §十一～§十三。

---

## 七、值班怎么读这些告警

| 告警 | 先看什么 |
|---|---|
| `QuantWorkbenchDown` | `ss -ltnp \| grep 8397`；进程没了就报运维重启 |
| `QuantWorkbenchToolfaceUnreachable` | `curl -s localhost:8397/api/v3/metrics \| jq .workbenchUp`；错误原文在 `/metrics` 的 `quantwb_workbench_probe_failed` |
| `QuantMcpToolCallFailureRateHigh` | `curl -s localhost:8397/api/v3/metrics \| jq .mcp.byTool` 定位工具 |
| `FutuRateLimited` / `FutuCooldownActive` | 调低 `QUANT_FUTU_RATE_PER_SEC`（默认 3.0）/ `QUANT_FUTU_MAX_CONCURRENCY`（默认 2），并查有无旁路调用 |
| `DataSourceChainUnavailable` | `curl -s 'localhost:8397/api/v3/sources/status?keys=<chain>'` 看逐级 attempts 的真实错误 |
| `RiskDrawdownRedLineBreached` | `/api/v3/risk/analytics` + `/api/v3/oms/orders`；**不要改 `LIMITS` 常量消警** |
| `QuantSchedulerThreadDead` | 服务日志里 lifespan 启动是否异常；调度器死了作业链会静默停摆 |
| `QuantSchedulerRecentError` | `curl -s localhost:8397/healthz \| jq .scheduler.last_error`；该标记成功**不清除**，属「需人确认」 |
| `DataSourceProbeStale` | 跑一次 `curl -s localhost:8397/api/v3/sources/status` 刷新缓存 |
| `RiskIndustryConcentrationBreached` / `RiskIndustryConcentrationApproaching` | `curl -s 'localhost:8397/api/v3/risk/industry?market=<SH\|HK\|US>&limit_pct=20'` 看 `exposures`/`missing`/`breach`；缓存是否新鲜看 `quantwb_risk_industry_probe_timestamp_seconds`；**不要**改 `LIMITS` 常量消警。**闸门已接同一口径**：超限会让台账 `stage="blocked_industry"`，先看 `/api/v3/oms/orders?market=<M>` 的 `industry_pct`/`industry_source` 与订单 `risk.reasons` 原文，再决定是调仓还是调阈值（调阈值见 `docs/e2e-and-data-gaps.md` §十四） |
| `RiskIndustryProbeFailed` | 错误原文在 `quantwb_risk_industry_probe_failed` 的 `error` 标签；先查 `futu_rate_limited_total` / `futu_cooldown_remaining_ms`（限流是首要怀疑） |
| `RiskIndustryProbeStale` | 跑 `curl -fsS -X POST 'localhost:8397/api/v3/metrics/probe/refresh'`；长效办法是装 `platform/install/quant-v3-probe.timer` |

---

## 八、本次**没有**做的事（避免误读为「已覆盖」）

- ❌ 没有安装 Prometheus/Grafana/Alertmanager，没有改任何 systemd 单元（**只产出素材**：
  `platform/install/quant-v3-probe.{service,timer}` + `platform/install/README.md`，需运维自行
  `systemctl --user enable --now`）；
- ❌ 没有建 Grafana 面板（本机没有 Grafana 可校验——**未产出未校验的东西**）。
  **替代面**见 §九：平台内规则求值器（`GET /api/v3/ops/alerts`）+ 工作台「运维告警」卡片；
- ⚠️ 通知投递**仍然没有**：firing 只在 `/api/v3/ops/alerts` 与工作台卡片上可见，
  没人打开页面就不会被通知（要通知就接 Alertmanager，见 §9.3）；
- ❌ 没有接真实通知通道（飞书/邮件），告警止于 Prometheus `/alerts`、`/api/v3/ops/alerts` 与日志；
- ✅ 已覆盖行业集中度红线（`quantwb_risk_industry_pct` + 探测写入器），且**已接闸门**：
  `check_order` 读同一份落盘缓存，超限 → `stage="blocked_industry"`；缺读数 → fail-open
  + 强制留痕（`/api/v3/metrics` 的 `industryGate.failOpen` 可告警）；
- ⚠️ 降级链/行业探测的**自动触发**只到「素材」为止：定时器单元已写好，**没有安装、没有
  enable**。不装会怎样见 `platform/install/README.md`——`DataSourceProbeStale` /
  `RiskIndustryProbeStale` 会响（这是有意的：让静默本身可见）。

---

## 九、无 Grafana 的替代方案（`/metrics` 标准文本 + 平台内求值 + 工作台卡片）

**前提**：本机没有（也不为本任务新装）Grafana / Prometheus / Alertmanager。
「不装」不等于「没有监控」——规格 §8.3 要的是**这 9 类事实可被观测、可被判定、可被人看见**。
本方案用三层把这件事落地，**不新增任何事实源**：

```
 ①  指标出口（已有）       platform/server/observability.py → GET /metrics
                          Prometheus 文本格式（0.0.4），谁都能抓；25+ → 40+ family
                                     │
 ②  规则求值（本轮新增）   platform/server/v3_alerts.py
                          解析 platform/deploy/monitoring/alerts.yml（**同一份规则文件**）
                          在进程内对 ① 的文本求值 → firing / pending / ok / no-data / unsupported
                          → GET /api/v3/ops/alerts（只读）· GET /api/v3/ops/alerts/rules
                                     │
 ③  展示面（本轮新增）     工作台「网关与调度」页 → 「运维告警」卡片
                          读 ② 的 JSON；firing=红 / pending=橙 / no-data=中性灰 / unsupported=紫
                          卡内明写「数据来自平台内求值，非 Grafana」
```

三层各自的**边界**（不夸大）：

| 层 | 能做 | 不能做（如实） |
|---|---|---|
| ① `/metrics` | 标准文本，Prometheus/Grafana/任何抓取器都能读；已有 40+ family | 只是**数据出口**，不做判定、不存历史 |
| ② `v3_alerts` | 进程内三态判定、`for` 时钟、`rate/increase/delta/changes` 窗口、UI 的 JSON | 只实现 alerts.yml 实际用到的 **PromQL 子集**；窗口历史在进程内（重启清零）；不做通知投递、不存时序、不画图 |
| ③ 工作台卡片 | 人打开页面就能看到 firing/no-data 与证据 | **不是**时序面板：没有历史曲线、没有告警静默/抑制（silence/inhibition）、没有值班轮转 |

**因此**：本替代方案解决的是「**有没有人在看**」与「**三态是否诚实**」，
不解决「长期趋势图」「通知渠道」「静默抑制」——那三件事要么接 Prometheus/Grafana/Alertmanager，
要么另行实现（本轮不产出未校验的东西）。

### 9.1 三态语义（`no-data` 必须与 `ok` 分开）

| state | 含义 | 卡片配色 | 什么时候出现 |
|---|---|---|---|
| `firing` | 表达式为真，且已持续满 `for` | 红 | 真的越线 |
| `pending` | 表达式为真但 `for` 未满 | 橙 | 刚越线，观察中（**既不冒充 firing，也不冒充 ok**） |
| `ok` | 表达式为假（判定过，且不满足） | 绿 | 有读数、不越线 |
| `no-data` | **判不了**：表达式引用的指标在 `/metrics` 里没有任何样本；或 `rate/increase/delta/changes` 的窗口历史不足（覆盖率 < 90%） | **中性灰** | 没有读数、探测缓存缺失/过期、进程刚重启历史未积累 |
| `unsupported` | 表达式超出求值器的 PromQL 子集（聚合 / `absent()` / `or` / 正则匹配 / `bool` …） | 紫 | 规则文件里出现了不支持的构造——**显式报出，绝不静默当 ok** |

三条纪律：

1. **`no-data` 不是 `ok`**：指标缺席时绝不显示「正常」。这也是 `/metrics` 本身的纪律
   （没有读数就不导出该 family，而不是导出 0）。
2. **不插值、不外推**：分位数用窗口内真实样本的**最近秩**；`rate/increase/delta/changes`
   要求窗口历史覆盖率 ≥ 90%，否则 `no-data`——不学 PromQL 的区间外推。
3. **规则文件唯一**：求值器不复制、不派生 `alerts.yml`；改规则只需改这一份文件，
   Prometheus 与平台内求值器同时生效。

### 9.2 与 Prometheus 的关系（口径差异，逐条写清）

| 项 | Prometheus | 平台内求值器 `v3_alerts` |
|---|---|---|
| 规则来源 | `prometheus.yml` 的 `rule_files: alerts.yml` | **同一份** `alerts.yml`（环境变量 `QUANT_ALERTS_RULES` 可改路径） |
| 表达式 | 完整 PromQL | 子集：选择器 + 标签精确匹配 + 比较 + `and`（含 `on()/ignoring()`）+ 算术 + `rate/increase/delta/changes` + `time()` + `offset`；其余 → `unsupported` |
| `for` | 抓取周期的整数倍 | 采样线程每 `QUANT_ALERTS_SAMPLE_SECONDS`（默认 15s）推进一次 |
| 窗口历史 | TSDB，任意长度 | **进程内内存**，默认 2h（`QUANT_ALERTS_HISTORY_SECONDS`），**重启清零** |
| `rate()`/`increase()` | 区间外推 | 观测跨度内的实际增量（覆盖率 < 90% → `no-data`） |
| 分位数 | `histogram_quantile()` + 直方图桶 | 平台自己算（滑动窗口 + 最近秩），以 gauge 暴露，见 §五之二 |
| 进程自身存活（`up`） | 抓取器生成 `up{job="quantwb-v3"}` | **无法判定**：进程看不到自己死亡 → `QuantWorkbenchDown` 报 `no-data`，证据里写「由外部抓取器判定」 |
| 分位数/`up` 之外的告警语义 | `pending`/`firing` | 同 + 显式的 `no-data` / `unsupported` |

> **平台内求值器发现的既有规则缺陷（本轮修正）**：`RiskIndustryConcentrationBreached` /
> `RiskIndustryConcentrationApproaching` / `RiskIndustryProbeFailed` 原先写作
> ``expr > 阈值 and (time() - <无标签指标>) < N``。PromQL 的 `and` **默认要求两侧标签集完全
> 相同**，而左侧带 `scope`/`error` 标签、右侧守卫是无标签指标 ⇒ 交集为空 ⇒
> **这两条规则永远不会命中**（`promtool check rules` 只查语法，查不出这个）。
> 现改为 `and on() (...)`（空标签列表 = 任意序列都视为匹配），语义与「缓存新鲜才参与判定」一致。

### 9.3 将来接 Prometheus / Grafana / Alertmanager —— **不改平台代码**

平台侧已经就绪，接的时候只动外部组件：

1. **Prometheus**（拉 `/metrics` + 用同一份规则）：
   ```bash
   docker run --rm --network host \
     -v "$PWD/platform/deploy/monitoring:/etc/prometheus:ro" \
     prom/prometheus --config.file=/etc/prometheus/prometheus.yml
   ```
   `prometheus.yml` 已指向 `127.0.0.1:8397/metrics`（15s）并把 `alerts.yml` 挂在 `rule_files`。
   **规则不用改**：同一份文件，同一批指标。
2. **Grafana**（可选，纯展示）：数据源指向上面的 Prometheus（`http://127.0.0.1:9090`），
   面板直接查 `quantwb_*` / `futu_*`。届时工作台卡片仍可用（它读平台内求值），
   两者**同源不同判**：面板看趋势，卡片看三态。
3. **Alertmanager**（可选，通知投递）：在 `prometheus.yml` 里取消 `alerting:` 段注释并指向
   `127.0.0.1:9093`，再配 receiver（邮件/飞书 webhook）。平台侧**不参与**通知投递，
   因此**不需要改任何代码**。
4. 反过来也要说清：若将来接上了 Prometheus，**平台内求值器不必下线**——它是
   「没有 Prometheus 时也能判定」的兜底，且它的 `no-data` 语义（指标缺席即无法判定）
   是外部告警栈不提供的额外诚实性。

### 9.4 规格 §8.3 逐条对照（9 类）

| # | 规格 §8.3 指标 | 告警阈值 | 现状 | 规则名 | 输入指标（口径） |
|---|---|---|---|---|---|
| 1 | Headless 调用成功率 | < 95% | ✅ **本轮补齐** | `HarnessHeadlessSuccessRateLow` | `quantwb_headless_success_rate`（窗口内 successes/calls，窗口默认 24h） |
| 2 | Headless 调用平均耗时 | > 60s | ✅ **本轮补齐** | `HarnessHeadlessDurationHigh` | `quantwb_headless_call_duration_seconds`（窗口内平均；单次口径另有 §4.1 的 P95 规则） |
| 3 | SDK 会话活跃数 | > 10 | ✅ **本轮补齐** | `HarnessSdkActiveSessionsHigh` | `quantwb_sdk_active_sessions`（来源：`v3_sdk.metrics_view()` 或 `sdk_turns`；**两条来源都没有时不导出 → no-data**） |
| 4 | MCP 工具调用延迟 | > 5s | ✅ **本轮补齐** | `ToolMcpCallLatencyP95High` | `quantwb_call_duration_p95_seconds{scope="mcp-tool-http"}`（一请求一调用的真实单次耗时；样本 < 20 不导出） |
| 5 | MCP 工具调用失败率 | > 5% | ✅ 第一轮**已对齐**（规则阈值 10%，见下注） | `QuantMcpToolCallFailureRateHigh` | `rate(quantwb_mcp_errors_total[10m]) / rate(quantwb_mcp_calls_total[10m])` |
| 6 | 数据源连接状态 | 断连 | ✅ 第一轮**已对齐** | `DataSourceChainUnavailable` | `quantwb_datasource_available{chain} == 0`（+ 24h 探测新鲜度守卫） |
| 7 | 数据延迟 | > 5min | ⚠️→✅ **本轮修正口径** | `DataSourceDataDelayHigh` | `quantwb_datasource_data_age_seconds{chain}` = `now - as_of`（**新指标**）。原先只有 `DataSourceProbeStale`（「探测缓存 > 24h 过期」），那是**监控自身的可见性缺口**，不是数据延迟 |
| 8 | 订单执行延迟 | > 1s | ✅ **本轮补齐** | `TradeOrderExecutionLatencyHigh`（另加 §4.1 的 P95 规则） | `quantwb_order_execution_latency_max_seconds`（台账「`first_seen_at` → `submitted`/`filled`」窗口内最大值；**当前 sim 无已提交订单 → no-data**） |
| 9 | 风控阻断次数 | 突增 | ⚠️→✅ **本轮修正口径** | `RiskBlockedSpike` | `quantwb_risk_blocked_total`（**新计数器**）+ `increase(...[1h])` 与前一小时基线比较。原先只有 `RiskBlockedOrderAppeared`（「任何一次新增阻断」，事件级，不构成突增判定） |

> 注（第 5 条，**口径差异，如实标注**）：规格写「失败率 > 5%」，第一轮规则写的是 **10%**
> （`QuantMcpToolCallFailureRateHigh`，注释理由是「工作台工具面是只读数据面，偶发单次失败属正常」）。
> 这是一处**有意放宽**的阈值差异，不是遗漏；若要让规则与规格**逐字一致**，
> 把该规则的 `> 0.1` 改成 `> 0.05` 即可（改一处，两个求值方同时生效）——本轮**没有**擅自改，
> 因为那会改变既有告警的行为基线；这里只把差异摆明。
>
> 注（第 9 条）：规格只说「突增」，没给数字。规则里的「≥3 单/小时且 ≥3× 前一小时」是**运维约定**
> 并在规则注释里写明来源，不是平台常数（平台常数是 `LIMITS` 的 2%/20%/15%）。

### 9.5 规格 §4.1 的分位数口径（本轮补齐）

| §4.1 指标 | 目标 | 规则 | 指标 |
|---|---|---|---|
| MCP 工具调用延迟 | < 2s（单次） | `NfrMcpToolCallLatencyP95AboveTarget` | `quantwb_call_duration_p95_seconds{scope="mcp-tool-http"} > 2` |
| 订单执行延迟 | < 500ms | `NfrOrderExecutionLatencyP95AboveTarget` | `quantwb_call_duration_p95_seconds{scope="oms-ledger"} > 0.5` |
| Headless 调用延迟 | < 30s（单次） | `NfrHeadlessLatencyP95AboveTarget` | `quantwb_call_duration_p95_seconds{scope="headless-log"} > 30` |
| 行情数据延迟 | < 100ms（富途 L2） | **仍缺** | 平台没有「L2 行情端到端延迟」的真实读数；`scope="datasource-probe"` 是**上游探测调用的 RTT**，口径不同，**不拿它顶替**（见 §9.6） |
| SDK 会话首次握手 | < 30s | **仍缺** | 需要 `v3_sdk` 提供握手计时；当前无 SDK 会话事实 |
| 系统可用性 | 99.9% | **仍缺（本替代方案不提供）** | 需要长期时序与跨度统计 → 属 Prometheus/Grafana 的职责；工作台卡片只有「当前三态」 |

分位数的算法、窗口、最小样本与四个 `scope` 的样本来源见 **§五之二**（含已知偏差）。

### 9.6 本方案的**已知缺口**（不装 Grafana 就必须承认的）

- ⚠️ **没有通知投递**：firing 只在 `/api/v3/ops/alerts` 与工作台账面上可见，
  没人打开页面就不会被通知（要通知就接 Alertmanager，见 §9.3）；
- ⚠️ **没有长期历史与趋势图**：进程重启后窗口历史清零，`rate/increase/delta/changes`
  类规则会先报 `no-data`，再随采样重新积累（最长 2h 恢复满窗口）；
- ⚠️ **没有静默/抑制**：同一根因可能同时点亮多条规则（如缓存过期会连带
  `DataSourceProbeStale` + `DataSourceDataDelayHigh` 静默），需要人按 §七 的表逐条处置；
- ⚠️ **`quantwb_risk_blocked_total` 是「首见计数」**：进程内首次观测到某单进入阻断阶段才 +1，
  重启归零、台账删单后重新出现会再计一次（口径写在 HELP 里，不谎称是事件流回放）；
- ⚠️ **`mcp-tool-http` 分位数只覆盖「一请求一调用」**：批量/多调用请求不计入（宁缺毋滥），
  因此它对「工具面整体延迟」是**有偏样本**，不是全量分布；
- ⚠️ **订单延迟口径与规格有差异**：规格 §4.1 是「信号生成 → 订单提交」，
  平台能拿到的真实两端是「台账登记 → 阶段迁移」（见 §五之二表格）。
