# 监控落地（规格 §8.3）：Prometheus 抓取 + 告警规则

本目录只放**可直接用的配置文件与说明**，不安装任何系统服务、不改 `systemd`。
平台侧的唯一改动是新增了一个 Prometheus 文本出口（`platform/server/observability.py` 的
`GET /metrics`）与两个探测结果落盘（`/api/v3/sources/status` 写 `v3-datasource-probe.json`；
`POST /api/v3/metrics/probe/refresh` 写 `v3-risk-probe.json`），**只加不删**。

| 文件 | 作用 |
|---|---|
| `prometheus.yml` | 抓取配置：目标 `127.0.0.1:8397`、路径 `/metrics`、间隔 15s |
| `alerts.yml` | 23 条告警规则（7 组），每条阈值都写明依据 |
| `dump_metrics.py` | 离线导出 `/metrics` 文本，供 `promtool check metrics` 自检（服务没起也能校验） |
| `verify_industry_gate.py` | **真机只读**验证行业红线闸门：读真实 `~/.dsh/v3-risk-probe.json`、用构造订单过 `check_order`/`_upsert`，打印真实 reasons/history 与 fail-open 样例（不下单、不写盘） |
| `README.md` | 本文件 |

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
- ❌ 没有建 Grafana 面板（任务书只要求 prometheus + alerts + README；Grafana 面板属可选，
  且本机没有 Grafana 可校验——**未产出未校验的东西**）；
- ❌ 没有接真实通知通道（飞书/邮件），告警止于 Prometheus `/alerts` 与日志；
- ✅ 已覆盖行业集中度红线（`quantwb_risk_industry_pct` + 探测写入器），且**已接闸门**：
  `check_order` 读同一份落盘缓存，超限 → `stage="blocked_industry"`；缺读数 → fail-open
  + 强制留痕（`/api/v3/metrics` 的 `industryGate.failOpen` 可告警）；
- ⚠️ 降级链/行业探测的**自动触发**只到「素材」为止：定时器单元已写好，**没有安装、没有
  enable**。不装会怎样见 `platform/install/README.md`——`DataSourceProbeStale` /
  `RiskIndustryProbeStale` 会响（这是有意的：让静默本身可见）。
