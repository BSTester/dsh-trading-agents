# V3.0 能力对齐审计（「其他能力都对齐了吗？」）

> 🧭 **先读哪里**：**第三轮收口**（本节）→ **第二轮核对**（§「第二轮核对（基于 `docs/v3-spec.md` 原文）」）
> → 其下的 §一~§八 是**第一轮（快照 `8b75fcc`）归档**，仅用于对照。
> 规格原文已落库为 **`docs/v3-spec.md`**（942 行，逐字提取，自检 `grep -c '^#### FR-' = 21` ✅）。

## 第三轮收口（2026-09-21，快照 `122d2f8` + 在途 PIT/安装工作）

针对第二轮点名的主要缺口，本轮已落地（均真机验证，证据见 `docs/e2e-and-data-gaps.md` §17~§22）：

| 条目 | 第二轮 → 第三轮 | 依据 |
|---|---|---|
| FR-GATEWAY-002（SDK JSON-RPC，唯一"缺失"） | **缺失 → 对齐（带已登记限制）** | `v3_sdk.py` 自实现换行分帧 JSON-RPC（venv 无 `deepseek-harness-sdk`）；真机握手 `deepseek-harness-sdk-runtime` 逐字命中、真实提示消耗 token、白名单真机摘除 16 件写工具；线上 `quant-sdk` profile 已装并握手成功 |
| FR-GATEWAY-003/004（Headless Runner/调度/熔断） | 部分 → **对齐（带已登记限制）** | `v3_headless.py`：stdout/stderr 分离、退出码 0/1/130、并发 3/超时 300s/token 预算 200K、9 条触发条件（真实数据源）、`headless_log` 落库+读取；线上安装后调度 tick 真实唤醒 `risk_breach` → completed(exit 0, 182s)。保留限制：300s 预算未按提示词体量标定（两条真实 timeout 行） |
| FR-MON-003（Headless 调用日志） | 部分 → **对齐** | `headless_log` 写入方与读取端点齐备；线上 6 条 profile-missing 历史未篡改，新行为 completed/timeout 真实记录 |
| FR-TOOLS-003（工具发现代理） | 部分 → **对齐（带已登记取舍）** | `mcp_discovery.py`：discovery（默认）6 件/3,248 字符 ≈812 token vs direct ≈21.7k token，省 96%；诚实代价=每新工具多 1 次检索往返 |
| FR-DATA-003（PIT 唯一入口） | 部分 → **对齐（带已登记限制）** | `server/data/cache.py`：as_of 显式二选一、缓存键含 as_of/mode/数据源身份、5 处迁移逐字段等价、鉴别力真实验证（注入缺陷 24 红）。保留限制：**价量因子的 as_of 未生效**（工具面 `factors` 不接受 as_of，其 PIT 由工具内部决定；响应里两个 as_of 并列不互相冒充） |
| FR-EXEC-003（事前/事中/事后风控） | 部分 → 部分（四子项已补，覆盖仍非全量） | 杠杆率/流动性/归因/资金检查接进 `risk_detail` 与页面（真读数 + 缺数据 null+原因）；`margin_debt_pct` 因上游无融资字段恒 null；`byFactor` 因缺 PIT 建仓敞口恒 null |
| FR-STRAT-001（六类因子） | 部分 → 部分（四类已入矩阵，覆盖受数据侧限制） | 23 条注册表 + 覆盖率逐因子暴露；gross_margin/net_margin 5/5；roe/roa（上游 16s/标的，需离线落库）、成长（缺 2025 期 PIT 行）、情绪（采集新鲜度）、另类（需 `all` 显式取数）——全部 null+原因，不填 0 |
| §8.3 监控九条 | 2 对齐/2 口径不符/5 缺失 → **9 条全部有着落** | `v3_alerts.py` 进程内求值（不装 Grafana）；alerts.yml 23→33 条；修掉 3 条 `and` 标签集不匹配的**死规则**；`/metrics` +18 family 与 P50/P95/P99（口径写明） |
| §4.2 密码加密存储 | 维持「被替代」结论 | appkey 架构无登录/解锁密码字段；RSA 私钥明文 PKCS8 + 0600；RBAC 仍未实现 |

**新发现并已修的真实漏洞（安全相关）**：默认 discovery 面下写工具经 `call_tool` 可达（钩子按工具名匹配看不到内层名），且已真实发生——一次 quant-headless 决策唤醒误领 2 条值班任务。修复：**只读面 `/mcp/ro`**（服务侧按注册表 annotations 硬边界），两个决策 profile 指向它；全局 `/mcp` 不变，官方值班链不受影响。

**修正后计数（31 条）**：对齐 3→**7**、部分 25→**18**、缺失 1→**0**、不适用 1、未验证 1（NFR §4.1 六项中三项已可判：MCP P95 已有口径；行情 <100ms 与 99.9% 可用性仍无埋点/长时序）。对齐+部分 = 29/29 可评项 = 100%——但**"部分"的 18 条各自带明确限制**，逐条见第二轮明细与上表"保留限制"列；本文件不把"部分"升格为"对齐"。

---

> 审计对象：`量化交易决策平台需求规格说明书与系统详细设计文档` **V3.0**（2026-09-19），
> 全文见会话记录（用户消息，31,324 字符）→ 已落库 `docs/v3-spec.md`。**以规格条目为唯一尺子**。
>
> **审计快照**：`git rev-parse --short HEAD` = **8b75fcc**；工作区同时有并行 agent 的未提交改动
> （`platform/server/{app,mcp_tools,v3_ops}.py` 已改，`v3_nlp.py` / `v3_risk_gate.py` / `v3_mcp.py`
> 未跟踪），**运行中的 8397 进程早于这些改动**。下文 `文件:行` 均为**该快照**的工作区行号，
> 并附**符号名**以便行号漂移后重新定位；凡「运行进程」与「工作区」不一致处**逐条注明**。
>
> **只读纪律**：全程仅 `GET` 与只读 `POST /api/wb/<只读端点>`（`POST` 是工具面读端点的调用方式，
> 用来查 `/api/wb/rt_quote` 等；**未调用**任何 `trade_*`/`sim_trade_*`/`plan-execute`/
> `confirm-decide`/`switch-mode`/`rules-decide`）。未重启服务、未改任何代码或配置。

---

## 第二轮核对（基于 `docs/v3-spec.md` 原文）

> **本节是第二轮结论；凡与下文 §一~§八 冲突，一律以本节为准。**
>
> **规格原文已落库**：`docs/v3-spec.md`（942 行 / 48,933 B），来源标注在文件头 3 行元信息里 ——
> `/home/penn/.dsh/sessions/--home-penn-workspace-dsh-trading-agents--/session-adcad99b-79f9-43df-a6a1-2e3586121b63/session.v3.jsonl.zstd`
> 第 1135 行 `user/message`（`seq=1133`）逐字提取，未改写。自检：`grep -c '^#### FR-' docs/v3-spec.md` = **21** ✅；
> §4.1~4.4 / §5.1 / §5.2 / §6 / §8.2 / §8.3 / §10 / 附录 A / B / C **全部在位** ✅；表格 88 行、代码块 42 个（未压成散文）。
> 同会话另有两份副本（第 484 行 `seq=482`、第 6769 行 `seq=6767`），正文**逐字符一致**，仅前置用户口语不同；
> 全量 356 份 session 日志里**没有** V1/V2 或其它标称版本的规格原文（`zstdgrep -c 文档版本` 命中的 5 个会话中，出现的全部是 `文档版本**：V3.0`）
> —— 因此「标称 V3.0 唯一一份」= 标题下三行元信息 `**文档版本**：V3.0` / `**编制日期**：2026-09-19` / `**文档状态**：正式发布`。
>
> **第二轮快照**：`git rev-parse --short HEAD` = **48cd3d7**（第一轮 = `8b75fcc`；工作区仅新增未跟踪的 `docs/v3-spec.md`）。
> 运行中的 8397 进程 `quantwb_process_uptime_seconds` = **4338 s**（约 21:25 前 **72 分钟**重启过）
> → **第一轮「运行进程早于工作区改动」的前提已失效，本轮所有实测都是重启后的新进程**。
>
> **只读纪律**（与第一轮同）：全程只 `GET`，加一次只读 MCP 握手（`initialize` + `tools/list`，不调用任何工具）。
> **未调用** `trade_*` / `sim_trade_*` / `plan-execute` / `confirm-decide` / `switch-mode`；未重启服务；未改 `platform/**`（本轮只写文档）。
> 探测窗口：**2026-09-20 21:21–21:31 +08:00**。
>
> **本轮搜索证据口径**：凡「未实现」都附「搜了什么 + 命中数」，不猜。

### 2.1 修正后的汇总计数

| 状态 | 第一轮 | **第二轮** | 变化 |
|---|---|---|---|
| ✅ 对齐 | 5 | **3** | −2 |
| 🟡 部分对齐 | 21 | **25** | +4 |
| ❌ 缺失 | 2 | **1** | −1 |
| ⚪ 不适用 | 2 | **1** | −1 |
| ❓ 未验证 | 1 | **1** | 0 |
| **合计** | 31 | **31** | — |

- 对齐 + 部分对齐 = **28/29 = 96.6%**（第一轮 26/28 = 92.9%）。分母变化来自「不适用」由 2 降到 1。
- 唯一完全缺失：**`FR-GATEWAY-002`**（SDK JSON-RPC 会话通道）。
- 唯一不适用：`DES-5.1`（K8s/Kafka/Redis/PG → 单容器 SQLite，**用户显式授权**）。
- 唯一未验证：`NFR-PERF §4.1`。
- 净变化说明：**涨的 2 条（`FR-TOOLS-002`、`FR-GATEWAY-001`）不是「做好了」，是第一轮口径判错**；
  **跌的 4 条（`FR-TOOLS-001`、`FR-EXEC-003`、`FR-GATEWAY-005`→未跌、`FR-GATEWAY-001` 由不适用转部分）里没有一条是能力退步**，全部是「第一轮判浅 / 判宽」。

### 2.2 状态变化清单（第一轮 → 第二轮，含推翻理由）

| 编号 | 第一轮 | **第二轮** | 推翻理由（新证据） |
|---|---|---|---|
| **FR-GATEWAY-001** | ⚪ 不适用（用户豁免 adapter） | 🟡 **部分** | 用户豁免的是 **adapter 这个实现载体**，不是这条需求的三个功能子项。子项①inbound 连通 ✅；②命名契约 `mcp:<serverId>:<toolName>` ❌（Harness 侧实际是 `mcp__quantwb__<toolName>`，见 §2.3）；③**outbound 反向暴露（`ctx.tools.schemas()` → `McpServer` → `serveStdio`，命名 `dsh:<toolName>`）在整台机器上 0 实现**：`grep -rl "serveStdio\|McpServer\|createMcpServer" ~/.dsh/profiles/node_modules/@deepseek-ai/ ~/.dsh/profiles/web/node_modules/@bstester/` → **0 命中**；`@helibeiqi` 未安装；`dsh-harness-mcp-server` 未安装。第一轮把「载体被豁免」当成了「整条不适用」，漏掉了 outbound 这个真缺口。 |
| **FR-TOOLS-001** | ✅ 对齐 | 🟡 **部分** | 第一轮只核了「6 个域齐不齐」，没核规格表里的 **MCP 方法名**。规格逐条给了 13 个 `mcp:<域>:<工具>` 方法名（`mcp:quant_data:query_quote` …），实测 MCP `tools/list` 的 116 条里**逐字命中 0 条**：名字是裸 `series` / `rt_quote` / `capital_flow` … + `v3_*` 桥接前缀（`mcp__quantwb__` 命名空间由客户端加，不由服务端给）。域覆盖 6/6 ✅，但方法名契约 0/13。 |
| **FR-TOOLS-002** | ❌ 缺失 | 🟡 **部分** | 第一轮的证据是 `grep -n "isConcurrencySafe\|render\|annotations\|readOnlyHint" platform/server/mcp_tools.py` **0 命中** —— **找错了文件**：`annotations` 在 `platform/server/v3_mcp.py:646-648`（`readOnlyHint` / `destructiveHint` / `idempotentHint` 三件套，覆盖 39 条桥接工具；实测 `tools/list` 里 **39/116 条带 annotations**）。且工具描述确为模型视角的契约文本（`platform/server/mcp_tools.py:265` `ToolDefinition`，`v3_mcp.py` 逐路由 `PARAM_DOCS`）。五条子规范仍 **4 条未达**（等长 null 对齐 / `render` 分离 / 全量 `isConcurrencySafe` / `skill/quant-research` 层），故只能到「部分」。 |
| **FR-EXEC-003** | ✅ 对齐 | 🟡 **部分** | 第一轮实测了 VaR/CVaR/Beta/Alpha/IR/Kupiec/最大回撤（确实全在，本轮复现），但把规格的**事中/事后另外三项**用一句「未见实现」并入了说明，**没有降级状态**。逐项查：**杠杆率**（`grep -rni "leverage\|杠杆" platform/server` → 5 命中，全部是窝轮 `leverage_direction`/`leverage_multiple` **请求参数**，无组合杠杆读数）；**流动性风险**（`grep -rni "liquidity\|流动性" platform/server` → **0 命中**）；**绩效归因**（`grep -rni "attribution\|performance_attribution\|归因分析" platform/server` → **0 命中**；`归因` 的 27 处是「成交按市场归因」，不是归因分析）。三项 0 实现 → 不能算对齐。 |
| **FR-GATEWAY-005** | ✅ 对齐 | ✅ 对齐（不变） | 复查后**维持对齐**：规格正文的硬要求（专属 Profile + `package.json` manifest + `cordis.patch.yml` + `dsh-base`+`dsh-headless` 组合）全部满足且实测跑通；规格里那份 `bundles` 是 **yaml 代码块示例**，其中 adapter 一项**用户显式豁免**、`dsh-quant-data-mcp` 一项与 `FR-DATA-002` 同源（不重复扣分）。差异如实列出但不改状态。 |

**证据级更正、状态不变（2 条）**

| 编号 | 状态 | 第一轮结论 → 第二轮结论 |
|---|---|---|
| **FR-STRAT-003** | 🟡 部分（不变） | 第一轮：「**运行进程未挂载**：实测 `GET /api/v3/sentiment` 返回 SPA 兜底 HTML → 该能力**当前不可用**」。第二轮**推翻**：实测 `GET /api/v3/sentiment?symbol=600519&days=7` → `HTTP 200 application/json`，`{"ok":true,"source":"akshare/stock_news_em","as_of":"2026-09-20T13:26:00Z","score":-0.105574,"documents":7,"scored":4,"coverage":0.571429,"top_terms":[{"term":"被执行","polarity":-0.7,"count":3},…]}`。**能力已在线**。仍判部分的真实原因变成：①「事件识别」无独立实现（0 命中）；②**情绪分未进因子矩阵、未进策略流水线**（`grep -rn "sentiment" platform/server/v3_analytics.py platform/server/v3_ml.py` → **0 命中**，唯一命中在 `v3_ops.py:239` 的域名正则）。 |
| **FR-EXEC-002** | 🟡 部分（不变） | 第一轮：「**运行进程仍是 `no-data`** … 行业红线**不下闸** → 工作区已接、待重启生效」。第二轮**推翻**：重启后实测 `GET /api/v3/oms/orders` → `"industry_source":"cache/futu/info_owner_plate"`、`"industry_pct":50.0`、`"industry_top":"半导体"`、`"industry_as_of":"2026-09-20T13:01:39Z"`，且 `industry_markets` 三市场全部 `breach:true`（SH 37.5% / HK 40.0% / US 50.0%，阈值 20%），来源文件 `~/.dsh/v3-risk-probe.json`（21:01 写入）。闸门调用点 `platform/server/v3_ops.py:1077`（`industry_context`）+ `:1113`（`check_order` 收到 `context["industry_pct"]`）**在线生效**。仍判部分的原因收窄为：live 写**未做真实下单验证**（本轮禁写），且运行台账里 `stages={"manual":10}` 无 `blocked_industry` 实例。 |

### 2.3 逐条复核表（31 条 · 规格原文为唯一尺子）

> 读法：`规格原句` 是 `docs/v3-spec.md` 的**直接摘录**（行号给出），`file:line` 是本轮 `48cd3d7` 快照的工作区行号。
> 实测证据一律带 `source` / `as_of` 或探测时刻；`未实现` 一律附搜索命令与命中数。

#### A. Harness 集成网关（规格 §3.1）

| 编号 | 规格原句（摘录） | 状态 | 实现位置 file:line | 真实证据 | 与第一轮差异 |
|---|---|---|---|---|---|
| **FR-GATEWAY-001** | 「应部署 `@helibeiqi/dsh-cordis-universal-adapter`…命名 `mcp:<serverId>:<toolName>`…**Outbound 方向**：通过 `ctx.tools.schemas()` 枚举已注册工具，注册到 `McpServer`，经 `serveStdio`（或 HTTP）暴露…命名 `dsh:<toolName>`」（spec:116-124） | 🟡 **部分** | inbound：`platform/install/quant-headless/cordis.patch.yml:156-166`（insert `@deepseek-ai/dsh-mcp-client`，`serverName: quantwb`，`url: http://127.0.0.1:8397/mcp`）；`~/.dsh/profiles/headless/cordis.patch.yml:24-31`；`~/.dsh/.agent-presets/dsh-trading-agents/agent.cordis.yml:182-194`（`disabled: false`）。**outbound：无（0 命中）** | 实测 `GET /api/v3/gateway`（21:25）→ `channels.mcp={"status":"running","protocol":"MCP streamable-http（/mcp，SDK 2.2.0 的 streamable_http_app）","tools":116,"tools_total":116,"tools_domain_catalog":82,"tools_bridge":39}`。MCP 握手 `tools/list` → 116 条。**Harness 侧工具名 = `mcp__quantwb__<toolName>`（配置级推断 + 文档旁证，非直接握手观测）**：命名模板出处 `~/.dsh/profiles/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js` 的 `` `mcp__${serverName}__${rawName}` ``（serverName 由配置给，**不**由服务端给），而 `serverName: quantwb` 写在 `~/.dsh/.agent-presets/dsh-trading-agents/agent.cordis.yml:186`（安装版 `disabled: false`；**注意**：仓库工作区同名文件 `agent.cordis.yml:184` 是 `disabled: true`，两者不一致）。旁证：`platform/install/quant-headless/README.md:156` 记录真跑一次 `dsh --profile quant-headless` 时「模型列出 9 个本地工具 + **77 个 MCP 工具**」；`scripts/research_duty.sh:53,56` 与预设 persona/SKILL 文本均逐字写 `mcp__quantwb__research_tasks_claim` / `mcp__quantwb__research_tasks_report`。**「当前 web 会话是否真的挂载了该行」= 无法验证**：父会话日志（`session-adcad99b…`）里 **没有任何** `tool/call` 的 name 是 `mcp__quantwb__*`（14 次 `tool/call` 全是 bash/edit/subagent），本 subagent（子代理）会话的工具面里也没有这些工具；即无法把「配置已启用」升级为「运行时已挂载」。**outbound 搜索**：`grep -rl "serveStdio\|McpServer\|createMcpServer" ~/.dsh/profiles/node_modules/@deepseek-ai/ ~/.dsh/profiles/web/node_modules/@bstester/` → **0**；`grep -rn "serveStdio\|McpServer" plugins/ platform/js` → **0**；`ls ~/.dsh/profiles/node_modules/@helibeiqi` → No such file；`dsh-harness-mcp-server` 未安装 | **⚪不适用 → 🟡部分（推翻）**；outbound 是新发现的真缺口（§三 G2） |
| **FR-GATEWAY-002** | 「应部署 `@deepseek-ai/dsh-sdk-jsonrpc-server`…为每个 sessionId 打开一个会话、把用户提示词排入队列，并把每个会话事件与 Agent 状态转换流式发回平台」（spec:133）；「平台后端可使用 `deepseek-harness-sdk`（Python）…`DeepSeekHarness`」（spec:139-153） | ❌ **缺失** | `platform/server/v3_ops.py:130`（`SDK_REASON`）、`:135`（`SDK_PROTOCOL`）、`:1877` / `:1915` / `:2090` / `:2098`（四处硬编码 unavailable）。**新事实**：`~/.dsh/profiles/sdk/package.json:5-10` bundles=`["@deepseek-ai/dsh-base","@deepseek-ai/dsh-sdk-app"]`，但 `~/.dsh/profiles/sdk/cordis.patch.yml` = `[]`（空） | ①`/home/penn/.dsh/trading-venv/bin/python -c "import deepseek_harness"` → `ModuleNotFoundError: No module named 'deepseek_harness'`；②`pip list \| grep -i "harness\|sdk\|jsonrpc"` → 空；③实测 `GET /api/v3/gateway` → `channels.sdk={"status":"unavailable","reason":"本服务未挂载 SDK JSON-RPC 通道","protocol":"换行分帧 JSON-RPC / stdio（未挂载）"}`；④`grep -rn "jsonrpc\|session/prompt\|DeepSeekHarness" platform/server` → 仅 3 处常量文案；⑤**包已就位**：`~/.dsh/profiles/node_modules/@deepseek-ai/` 下有 `dsh-sdk-jsonrpc-server`、`dsh-sdk-app`、`dsh-sdk-minimal`、`dsh-sdk-protocol` | 与第一轮**同判**（缺失）；但**缺口显著变小**：三个官方包已安装、`sdk` profile 骨架已建，只差 1 行 patch + 装 Python SDK + 写客户端（第一轮估 3–5 人日偏大） |
| **FR-GATEWAY-003** | 「应实现 Headless Runner，由平台调度器调用 `dsh --profile headless`…**通道契约**：stdout：最后一次非空 assistant 文本…stderr：成功时为空；失败时输出…exit 0 / exit 1 / exit 130」（spec:158-172） | 🟡 **部分** | 平台服务内 **0 实现**：`platform/server/v3_ops.py:131-132`；`grep -rn "subprocess" platform/server/*.py` → `compute.py:34,149`（跑 python 计算脚本）与 `v3_report.py:15,295`（PDF 导出），**无 headless**。外置承担：`scripts/research_duty.sh:74`（`runner=("$dsh_bin" --profile "$profile" "$prompt")`）、`:82`（`"${runner[@]}" 2>&1 \| tee "$log"`）、`:83`（`status="${PIPESTATUS[0]}"`）、`:86-91`（退出码分流） | 实测 `GET /api/v3/gateway` → `channels.headless={"status":"unavailable","reason":"本服务未挂载 Headless CLI 子通道（无 dsh --profile headless 子进程调度）"}`；`systemctl --user list-timers` → `research-duty.timer` NEXT `Mon 2026-09-21 19:20:00`（active，LAST 2026-09-18 20:02）；`platform/install/quant-headless/README.md:121,155-156` 记录真跑退出码 0 | 与第一轮一致；**加深**：`:82` 的 `2>&1 \| tee` 把 **stdout 与 stderr 合并**，规格要求的二者分离契约**未实现**；退出码 130 只是被 `:89-90` 当普通非零码透传，无独立优雅关闭语义 |
| **FR-GATEWAY-004** | 「**定时触发**：开盘前扫描（08:30）、午间复盘（12:00）、收盘后分析（16:00）。**事件触发**：突发新闻、持仓异动、风控阈值突破、因子信号反转。**流水线节点触发**：调仓前决策确认、策略参数变更审核…**外部熔断保护**…超时、并发限制和费用预算」（spec:180-186）；「并发上限（默认 3 个并行）…token 预算（默认 200K）」（spec:555） | 🟡 **部分** | 现有定时链（**非 headless**）：`plugins/core/python/trading_core/daemon.py:69-105`；服务内调度器 `platform/server/scheduler.py:83`（`build_tick` → `daemon.tick` 包装，无 headless）。唯一 headless 唤醒：`install/research-duty.timer`（`OnCalendar=Mon..Fri 19:20`）+ `scripts/research_duty.sh:17`（`DSH_DUTY_TIMEOUT=1800`）、`:75-76`（`timeout`） | 逐项：08:30 开盘前扫描 **❌**（`grep -rn "08:30" platform plugins scripts install` → 仅 `platform/install/README.md:107`、`quant-v3-probe.timer:11` 的**探针**说明）；12:00 午间复盘 **❌**（12:00 命中全在交易时段/日历语义：`v3_market_calendar.py:85-86`、`plugins/core/python/trading_core/sessions.py`）；16:00 收盘后 headless **❌**（`daemon.py:69-70` 的 16:00 是 `sync_bars`/`sync_fundamentals` **数据同步**，不是大脑唤醒）；事件触发 **❌**（`grep -rni "event_driven\|事件驱动" platform/server plugins` → 0）；流水线节点触发 **❌**；并发上限 3 **❌**；token/费用预算 200K **❌**；超时 ✅（1800 s，仅外置脚本）。实测 `GET /api/v3/gateway` → `scheduler.rules=[]`、`headless={"today":{"total":0,…},"breaker":null,"last":[],"status":"unavailable"}` | 与第一轮一致；**新增排除**：规格点名的 16:00 在 `daemon.py` 里是数据作业，第一轮把它算作「已有时点」，本轮确认**不是** headless 触发 |
| **FR-GATEWAY-005** | 「应创建专属 Profile…Headless 模式使用 `dsh-base` + `dsh-headless` 组合…Profile 目录包含 `package.json`（…`dsh.profile` manifest）以及 `cordis.patch.yml`（用户 patch 层）」（spec:210-212）；示例 bundles（spec:217-224） | ✅ **对齐** | `platform/install/quant-headless/package.json:7-10`（**实际 bundles = `["@deepseek-ai/dsh-base","@deepseek-ai/dsh-headless"]`**，`patchReload:"startup"`）；`platform/install/quant-headless/cordis.yml:1`（空根，表明树由 patch 合成）；`platform/install/quant-headless/cordis.patch.yml:39`（`system-prompt` personaPrefix）/`:101-138`（16 行 disabled 白名单）/`:156-166`（唯一 insert 行）；`platform/install/quant-headless/pnpm-workspace.yaml`。线上：`~/.dsh/profiles/headless/package.json:5-11` + `~/.dsh/profiles/headless/cordis.patch.yml:24-31` | `platform/install/quant-headless/README.md:155-157`：临时 `DSH_HOME` 真跑 `dsh --profile quant-headless --dump-config` → **退出码 0 / 88 行**；`personaPrefix` 已是量化分析师文本；16 行 `disabled`；`quant-platform-mcp` 已 insert；`ls ~/.dsh/profiles/node_modules \| wc -l` = **279**、**无 `@helibeiqi`**、mtime 未变 | 与第一轮**同判**（对齐）。**差异逐项列全**：规格示例 4 条 bundles → 实际 **2 条**；缺 `dsh-cordis-universal-adapter`（**用户显式豁免**，且 `ls ~/.dsh/profiles/node_modules/@helibeiqi` → No such file）与 `dsh-quant-data-mcp`（未接，与 `FR-DATA-002` 同源）。spec:210 说 `dsh-base` 有 **78 个 entry**，`cordis.patch.yml:6` 自述现为 **87 行**（版本漂移，非缺口） |

#### B. 平台量化工具域（规格 §3.2）

| 编号 | 规格原句（摘录） | 状态 | 实现位置 file:line | 真实证据 | 与第一轮差异 |
|---|---|---|---|---|---|
| **FR-TOOLS-001** | 「应实现一个 MCP 服务器，暴露以下六大域的量化工具。参考 dsh-quant 的 59 工具·6 域架构：data / alpha / ML / risk / execution / ecosystem」（spec:233），表列 13 个代表工具与 `mcp:quant_data:query_quote` 等**方法名**（spec:235-249） | 🟡 **部分** | `platform/server/v3_ops.py:86`（`DOMAINS` 六域）、`:245`（`domain_of`）、`:93-104`（`V3_LOCAL_TOOLS` 5 个本地计算）、`:1939`（`GET /api/v3/tools`）；MCP 注册 `platform/server/mcp_tools.py`（77 直通）+ `platform/server/v3_mcp.py:630`（39 桥接） | 实测 `GET /api/v3/tools` → `{"ok":true,"total":82,"domains":{data:32,alpha:7,ml:2,risk:4,execution:18,ecosystem:19}}`；`/metrics` → `quantwb_build_info{version="3.0",tools="82",domains="6"} 1`、`quantwb_tools{scope="mcp"} 116`、`{scope="domain"} 82`；MCP `tools/list` 116 = 77 直通 + 39 `v3_*`。口径对账：`82 = 77 MCP 直通 + 5 V3 本地计算（v3_ops.py:93-104）`；**方法名逐字命中 0/13**（实得名为 `series` / `rt_quote` / `capital_flow` / `f10_detail` / `trade_place` … + `v3_*`） | **✅对齐 → 🟡部分（推翻）**：第一轮只数「域数一致（6）」就判对齐，漏核规格表里的方法名契约；工具面 116 ≫ 参考 59 |
| **FR-TOOLS-002** | 「- **工具 schema 注入系统提示词**…- **等长 null 对齐**：输出与输入等长，头部窗口位置为 `null`…- **规范 JSON + render 分离**…- **全部 isConcurrencySafe**…- **Skill 层**：`skill/quant-research` 让模型自行加载工作流」（spec:255-259）；附录 A 给出 `output: { schema, render }`（spec:866-869） | 🟡 **部分** | 工具契约：`platform/server/mcp_tools.py:265`（`ToolDefinition`）、`:1299`（`BoundTool`）、`:513-545`（`trade_place` 全字段契约，模型视角中文描述）；注解：`platform/server/v3_mcp.py:56`（`NON_READONLY_PATHS`）、`:646`（`readOnlyHint`）、`:647`（`destructiveHint`）、`:648`（`idempotentHint`） | 逐条：①schema 注入 —— 工具描述确为模型视角契约文本 ✅（**但注入动作在 Harness 侧的工具注册通道，平台无系统提示词写入实现**，`grep -rn "system-prompt\|systemPrompt" platform/server` → 0）；②**等长 null 对齐** ❌（`grep -rn "等长\|按索引对齐" platform/server/*.py` → **0**）；③**`render` 分离** ❌（真实 MCP 握手 `tools/list` 的 116 条里 `outputSchema` **0 条**、含 `render` 键 **0 条**；`mcp_tools.py`/`v3_mcp.py` 里 `render` 命中全部是 `observability._render_*` Prometheus 函数）；④`isConcurrencySafe` ❌（`grep -rn "isConcurrencySafe" platform/` → **0**）—— 但 **MCP 官方注解三件套已有**：实测 39/116 条带 `annotations`，其中 `readOnlyHint=true` 36 条；⑤`skill/quant-research` ❌（`find skills -name SKILL.md` → 11 个：`quant-trading`/`research-institute`/`trading-agents`/`last30days-bridge`/`futu-skills`×7，**无 `quant-research`**） | **❌缺失 → 🟡部分（推翻）**：第一轮 grep 只扫 `mcp_tools.py`，漏了 `v3_mcp.py` 的 MCP 注解 —— 证据不成立。五条子规范实际 **1 条部分达成 + 4 条未达** |
| **FR-TOOLS-003** | 「平台应向 Harness 暴露的 MCP 工具数量应控制在合理范围。参考 `dsh-quant-data-mcp` 的做法：提供 6 个 A 股数据工具…东方财富与腾讯数据源自动回退，所有数据源使用公开免密钥端点」（spec:263）；「平台应实现**工具发现代理**：MCP 服务器暴露一个 `list_tools` 入口和一个 `call_tool` 入口」（spec:265） | 🟡 **部分** | 域分组/过滤：`platform/server/v3_ops.py:1939`（`?domain=`）；桥接：`platform/server/v3_mcp.py:630`（把 39 条 `/api/v3/*` 路由镜像成 MCP 工具）、`platform/server/app.py:1032`（`v3_mcp.register(app.state.mcp, app)`） | 实测 MCP `tools/list`（21:27）= **116 条**，**无 `list_tools`、无 `call_tool`** 工具（`names` 里 `"list_tools" in names == False`、`"call_tool" in names == False`）。**上下文成本量化**：`tools/list` 响应 JSON = **79,624 字符 / 116 条 ≈ 686 字符/工具 ≈ 2 万 token**（按 4 字符≈1 token 粗估），一次性进入每次请求的 `tools` 参数 —— 与规格「避免上百个工具 schema 撑爆上下文窗口」正面冲突。`dsh-quant-data-mcp` 全仓库**非文档引用**：`platform/web-pro/src/pages/market.jsx:726-727`、`overview.jsx:536`（**仅作为卡片名称字面量**，见 §2.6 新发现 N1）；实现层 0 引用 | 与第一轮一致；**新增量化**：116 条 schema ≈ 2 万 token 的真实成本，以及「无 `list_tools`/`call_tool`」的握手级证据（第一轮未做握手） |

#### C. 数据层（规格 §3.3）

| 编号 | 规格原句（摘录） | 状态 | 实现位置 file:line | 真实证据 | 与第一轮差异 |
|---|---|---|---|---|---|
| **FR-DATA-001** | 「交易执行支持市价单、限价单、**条件单**、改单/撤单、订单查询、持仓查询」（spec:272） | 🟡 **部分** | **条件单（契约+闸门层）**：`platform/server/trading.py:257`（`PLACE_ORDER_TYPES = ("LIMIT","MARKET","AUCTION","AUCTION_LIMIT","STOP","STOP_LIMIT","MARKET_IF_TOUCHED","LIMIT_IF_TOUCHED")`）、`:261`（`AUX_PRICE_ORDER_TYPES = ("STOP","STOP_LIMIT","MARKET_IF_TOUCHED","LIMIT_IF_TOUCHED")`，规格要求触发价必填）、`:662`（枚举校验）、`:691-698`（aux_price 必填/互斥/3 位小数）、`:1595`（`trade.place_order(**kwargs)`）；工具面：`platform/server/mcp_tools.py:196-197`（`order_type` Literal 8 枚举）、`:519`（aux_price 必填说明）、`:637`（`order_type` 参数） | 测试 `tests/test_wp8_trading.py::test_place_order_official_constraints_rejected_locally`（`:377-378`：`{"order_type":"STOP_LIMIT"} → 需 aux_price`、`{"order_type":"MARKET_IF_TOUCHED"} → 需 aux_price`）、`::test_all_eight_order_types_and_four_sides`（`:327`）、`::test_aux_price_required_for_trigger_order_types`（`:1481`）、`::test_aux_price_mutually_exclusive_with_non_trigger_types`（`:1493`）、`::test_aux_price_format_is_official_three_decimals`（`:1498`）。**sim 通道边界**：`platform/server/trading.py:121`（`SIM_MAX_QTY_ORDER_TYPES={"LIMIT":1,"MARKET":3}`）、`:306`（`SIM_LIMIT_ONLY_FIELDS`）→ 模拟盘只支持限价/市价，触发类会被**如实拒绝**而非静默丢弃 | **第一轮「未见实现 / 未验证」→ 确定：条件单在契约与闸门层已实现**。第一轮的 `grep 条件单` 只搜中文词且只搜 `futu_data.py`，漏了 `trading.py:257` 的英文枚举。仍未验证的部分：**live 真实券商往返**（本轮禁写） |
| **FR-DATA-002** | 表列 5 源：Tushare Pro / AKShare / OpenBB / SEC EDGAR / `dsh-quant-data-mcp`（spec:276-282）；「`dsh-quant-data-mcp` 是零依赖 MCP stdio server…只用 Node 内置模块，无需 API key…公开免密钥端点（东方财富/腾讯）」（spec:284） | 🟡 **部分（4/5）** | `platform/server/v3_sources.py:1398`（Tushare 走 HTTP `http://api.tushare.pro`）、`:1648/1657/1670/1692/1705`（news/spot/financials/tushare/openbb）；降级链 `platform/server/v3_fallback.py:1`；凭据 `platform/server/v3_credentials.py:27`（`TUSHARE_TOKEN`） | 第一轮逐源实测记录（news=akshare、financials=sec/companyconcept、openbb=equity.fundamental.metrics、tushare=no-token）本轮未复跑（避免重复外部调用），**`/api/v3/sources/status` 仍可读**。**`dsh-quant-data-mcp` 实现层 0 引用**：`grep -rn "dsh-quant-data-mcp" --include=* .`（排除 node_modules/.git）= **16 处，分布**：`docs/v3-spec.md` 8（规格原文）、`docs/v3-capability-alignment.md` 3（本文档）、`platform/web-pro/src/pages/market.jsx` 2 + `overview.jsx` 1（**仅卡片名称字面量**）、`platform/web-pro/dist/assets/index-*.js` 2（构建产物）。**`platform/server` / `plugins` / `install` 命中 0** | 与第一轮同判；**新增**：这个规格点名的包被**前端当成数据源名称展示**（新发现 N1），容易被误读为已接入 |
| **FR-DATA-003** | 「系统应确保所有历史数据查询遵循 Point-in-Time 原则。平台侧的 **`data/cache.py` 作为唯一数据读取接口**，确保所有历史数据查询遵循 PIT 原则，防止前视偏差」（spec:288） | 🟡 **部分** | 规格点名的 `data/cache.py` **不存在**：`find . -name 'cache.py'` → 0；`ls platform/data/` → No such file。实际缓存层：`platform/server/caches.py:1`（391 行，TTL 两级 + 磁盘）。**PIT 约束分散在 4 处**：`platform/server/v3_analytics.py:978`（回测 `t` 日持仓只用 `≤ t-1` 收盘）、`:1045`（ML 特征只用 `≤ t`）、`platform/server/v3_ml.py:6-7` + `:222`（特征矩阵第 `i` 行只依赖 `closes[0..i]`）、`platform/server/v3_math.py:356-357`、`platform/server/trading.py:366`（基准价 PIT） | 测试：`platform/tests/test_v3_ml.py::PITTests`（含「打乱 `t` 之后数据」的反证）、`platform/tests/test_v3_analytics.py::test_pit_signal_uses_only_past_closes`（`:366`）、`::test_mutating_the_last_close_cannot_change_any_position`（`:388`）、`platform/tests/test_v3_ml.py::test_labels_are_forward_returns_not_contemporaneous` | 与第一轮同判。**缺口影响**：没有单一入口 → ①PIT 纪律**不可集中审计**（要读 5 个文件的注释才能拼出全貌）；②新增数据读取路径**不会自动继承** PIT 约束（`caches.py` 只是 TTL 缓存，不含任何 PIT 语义）；③规格点名的文件路径与实现不一致，验收时按字面找会「找不到」 |
| **FR-DATA-004** | 「应实现数据源健康检查机制。当主数据源不可用时自动降级至备用数据源」（spec:292） | ✅ **对齐** | `platform/server/v3_fallback.py:1`（`run_chain`）、`:646` 附近（`GET /api/v3/sources/status`）；探针素材 `platform/install/quant-v3-probe.{service,timer}` | 测试 `platform/tests/test_v3_fallback.py`（含 `test_all_failed_returns_none_none_and_full_timeline`）；`docs/e2e-and-data-gaps.md` 记录 `/api/v3/spot` 全链失败体含 `chain=[{source:"akshare/stock_zh_a_spot_em",ok:false,ms:22443,attempts:[…]}]`（真实来源 + 真实耗时 + 逐次尝试，不返回占位） | 与第一轮一致（对齐） |

#### D. 策略层（规格 §3.4）

| 编号 | 规格原句（摘录） | 状态 | 实现位置 file:line | 真实证据 | 与第一轮差异 |
|---|---|---|---|---|---|
| **FR-STRAT-001** | 「应支持多因子选股框架，涵盖**价值、成长、动量、质量、情绪、另类六类因子**。参考 dsh-quant 的 **PDAT→PAAT→PCPT→PRT→PET 五阶段**研究流水线」（spec:299） | 🟡 **部分** | 五阶段：`platform/server/v3_analytics.py:789`（`run_pipeline`）、路由 `:1169`（`GET /api/v3/strategy`）、`platform/server/v3_ops.py:98-99`（`strategy_run` 工具）。因子：`plugins/workbench/python/factors.py:27`（`FACTOR_SIGN`）、`:61-85`（`factor_values`）→ `:84` 返回 8 个价量因子；估值：`plugins/workbench/python/factors.py:88`（`valuation_values`）。**成长/质量因子**：`plugins/workbench/python/quality.py:58-60`（`roe` / `roa`）、`:110`（`gross_margin`）、`:115-116`（`revenue_yoy` / `net_profit_yoy`）。情绪：`platform/server/v3_nlp.py:936`（`GET /api/v3/sentiment`） | **实测 `/api/v3/factors/matrix`（as_of 2026-09-18，source `workbench/factors(z)`）**：`factors = ['liq_ratio','mdd_60','mom_20','mom_60','pb','pb_pct','pe_ttm','pe_ttm_pct','peg','ps','ps_pct','rsi_14','trend','vol_20']`（**14 个**）。逐类映射：**价值** ✅ `pe_ttm/pe_ttm_pct/pb/pb_pct/ps/ps_pct/peg`（7）；**动量** ✅ `mom_20/mom_60/trend`（3）；**质量** ❌ 矩阵内 0（`roe`/`roa`/`gross_margin` 只在 `quality.py`，**未进矩阵、未进流水线**）；**成长** ❌ 矩阵内 0（`revenue_yoy`/`net_profit_yoy` 同上）；**情绪** ❌ 矩阵内 0（`grep -rn "sentiment" platform/server/v3_analytics.py platform/server/v3_ml.py` → **0**）；**另类** ❌ 矩阵内 0（`capital_flow*`/`short_interest`/`option_chain` 是**独立 MCP 工具**，不进因子面）。另有 `rsi_14`/`trend`（技术）、`vol_20`/`mdd_60`/`liq_ratio`（波动/流动性）。**五阶段实测**：`GET /api/v3/brain` → `decision.stages = {"PDAT":{"bars":240,"universe":["US.NVDA","US.MSTR"]},"PAAT":{"analyzed":2,"withFactors":2,"scoreSource":"workbench/factors(z)"},"PCPT":{"longs":[…]}, "PRT":{"capped":true,"weightPctPerName":2.0},"PET":{"proposals":2}}` ✅ 五阶段**真实存在且在线** | 与第一轮同判（部分）；**本轮给出逐类清单**：六类里 **2 类达标（价值/动量）、4 类未进因子面（成长/质量/情绪/另类）**。第一轮说「情绪类由 v3_nlp 补（工作区）」——**不成立**：v3_nlp 只出独立评分，不进矩阵/流水线 |
| **FR-STRAT-002** | 「支持多因子选股策略、机器学习策略（Lasso/LightGBM/MLP）、**事件驱动策略**、**统计套利策略**。参数优化支持**数千次完整回测**和热力图可视化」（spec:303） | 🟡 **部分** | ML：`platform/server/v3_ml.py:50`（`FEATURE_NAMES`，全部由收盘价推出）、`platform/server/v3_analytics.py:1030` 附近（同口径评估）、`:1185`（`GET /api/v3/ml/sweep`）、`:1199`（`POST /api/v3/ml/backtest`）+ `:1099`（`GET /api/v3/ml/models`）；网格：`platform/server/v3_math.py:452`（`param_sweep`）；热力图：`platform/web-pro/src/components/charts.jsx:89`（`Heatmap`） | **事件驱动 0 实现**：`grep -rni "event_driven\|event-driven\|事件驱动" platform/server platform/web-pro/src plugins` → **0**。**统计套利 0 实现**：`grep -rni "统计套利\|cointegration\|协整\|stat_arb"` → **0**；`pairs` 27 处全部是 `(key,value)` 元组变量名（`v3_math.py:369` 等），无配对交易。**「数千次回测」实测量化**：`GET /api/v3/ml/sweep?ticker=SH.600519` → `grid` **12 格**（windows `[10,20,30,60]` × rebalance `[5,10,20]`），`best={"window":30,"rebalanceDays":20,"sharpe":0.236}`；代码上限 `platform/server/v3_analytics.py:940`（`_int_list(..., 8)` 两次）→ **上限 8×8 = 64 格/次**（`v3_math.py:452` 双层循环）。**与规格「数千次」差 1.5–2 个数量级** | 与第一轮同判；**新增硬数字**：网格上限 **64 格/次**（默认 12），第一轮只说「数千次不成立」没给上限 |
| **FR-STRAT-003** | 「集成实时情绪评分、**事件识别**、多源情绪融合、**情绪因子构建**」（spec:307） | 🟡 **部分** | `platform/server/v3_nlp.py:688`（打分主函数）、`:903`（同步实现）、`:936`（`@app.get("/api/v3/sentiment")`）、`:924`（`register`）、`:983`（`app.state.v3_nlp`）；装配 `platform/server/app.py:921` | **能力已在线（推翻第一轮）**：`GET /api/v3/sentiment?symbol=600519&days=7` → `HTTP 200 application/json`，`{"ok":true,"symbol":"600519","as_of":"2026-09-20T13:26:00.071850+00:00","source":"akshare/stock_news_em","score":-0.105574,"documents":7,"scored":4,"coverage":0.571429,"positive":1,"negative":2,"neutral":1,"top_terms":[{"term":"被执行","polarity":-0.7,"count":3,"weight":-2.1},…]}`；MCP `tools/list` 里也已有 `v3_sentiment`。测试 `platform/tests/test_v3_nlp.py`（**56 例**，含 `test_route_is_registered`、`test_no_news_returns_null_score_not_zero`、`test_lexicon_covers_spec_mandated_terms`）。**仍缺**：①事件识别（无独立抽取/分类器，只有极性词表）；②**情绪因子未进因子矩阵/策略流水线**（0 命中） | 证据级推翻（状态仍部分）：第一轮「运行进程未挂载、返回 SPA HTML」**已失效**（服务重启后生效）。真实缺口收窄为「事件识别 + 因子化接入」 |

#### E. 执行层（规格 §3.5）

| 编号 | 规格原句（摘录） | 状态 | 实现位置 file:line | 真实证据 | 与第一轮差异 |
|---|---|---|---|---|---|
| **FR-EXEC-001** | 「基于富途 OpenAPI 构建 OMS，负责将策略信号转化为交易订单并管理全生命周期」（spec:314） | 🟡 **部分** | `platform/server/v3_ops.py:911`（`class OmsLedger`）、`:1103`（`_upsert`，风控分级 + 状态历史）、`:2016`（`GET /api/v3/oms/orders`）、`:1586` 附近（`POST /api/v3/oms/sync`）；落库 `platform/server/v3_db.py:119`（`TABLE_SPECS`，`oms_orders`/`oms_sync`） | 实测 `GET /api/v3/oms/orders`（21:2x）→ `{"ok":true,"nav":999809.29,"nav_source":"sim-ledger(equity.current)","drawdown_pct":0.0,"drawdown_source":"sim-ledger(max_drawdown)","stages":{"manual":10},"orders":[…],"confirmation":{"pending":null,"ttl_ms":120000}}`；`/metrics` → `quantwb_oms_orders{stage="manual"} 10`，其余阶段 0 | 与第一轮同判；live 写仍**未经真实下单验证**（`docs/P4-live-trading.md` 自述，本轮禁写） |
| **FR-EXEC-002** | 「**自动执行**：预设风控阈值内的订单，平台直接调用富途交易接口；**人工确认**：超过阈值的订单，通过 Web/IM 通道请求人工审批；**强制阻断**：触发风控红线的订单，直接拒绝并记录」（spec:318-320） | 🟡 **部分** | `platform/server/v3_ops.py:107`（`LIMITS={"singlePct":2.0,"industryPct":20.0,"drawdownPct":15.0}`）、`:110`（`STAGES`）、`:494`（`check_order` 纯函数）、`:1037`（`industry_context` 封装）、`:1077`（取真实行业读数）、`:1113`（`check_order(...)` 传入 `context["industry_pct"]`）；行业读数 `platform/server/v3_risk_gate.py:338`（`industry_context`）；探针 `~/.dsh/v3-risk-probe.json` | **闸门已在线（推翻第一轮「no-data」）**：实测 `GET /api/v3/oms/orders` → `"industry_source":"cache/futu/info_owner_plate"`、`"industry_pct":50.0`、`"industry_top":"半导体"`、`"industry_as_of":"2026-09-20T13:01:39.096948+00:00"`、`"industry_missing":0`、`"industry_markets"` 三市场全 `breach:true`（SH 37.5% / HK 40.0% / US 50.0%，`limit_pct:20.0`）。`GET /api/v3/risk/industry?market=SH` → `as_of 2026-09-20T13:29:32Z`、`limitPct 20.0`、`breach true`、逐标的真实行业映射。测试 `platform/tests/test_v3_risk_gate.py`（**28 例**，含 `test_industry_red_line_beats_single_order_conclusion`、`test_industry_pct_equal_to_limit_is_not_blocked`）、`platform/tests/test_v3_ops.py::test_check_order_thresholds`（`:1223`） | 证据级推翻（状态仍部分）：第一轮「行业红线不下闸、待重启」**已失效**。仍部分：live 写未做真实下单验证 + 运行台账无 `blocked_industry` 实例 |
| **FR-EXEC-003** | 「**事前风控**：订单合规校验（持仓限制、单笔上限、行业集中度）、**资金检查**。**事中风控**：实时监控策略回撤、**杠杆率**、**流动性风险**。**事后风控**：**绩效归因分析**、最大回撤统计、VaR 计算。参考 dsh-quant 的 risk 域提供 **VaR/CVaR/Beta/Alpha/IR + Kupiec 检验**」（spec:324） | 🟡 **部分** | ✅ 有：`platform/server/v3_ops.py:494`（事前：单笔 2% / 行业 20% / 回撤 15%）；`platform/server/v3_math.py:188`（`kupiec_pof`）、`:171`（`max_drawdown`）、`:228`（`portfolio_risk`：VaR/CVaR/Beta/Alpha/IR）；`platform/server/v3_analytics.py:447`（`risk_analytics`）、`:456-458`（字段清单）、`:1134`（`GET /api/v3/risk/analytics`）。❌ 无：杠杆率 / 流动性 / 绩效归因 / 资金检查 | ✅ 实测 `GET /api/v3/risk/analytics?limit=60` → `analytics={"confidence":0.95,"observations":59,"varDailyPct":1.458,"cvarDailyPct":1.648,"varAmount":14580.96,"cvarAmount":16477.91,"annVolPct":14.84,"annReturnPct":19.5,"maxDrawdownPct":-5.26,"beta":-0.081,"alphaAnnPct":16.58,"ir":2.059,"benchmarkAnnReturnPct":-35.85,"kupiec":{"lr":1.8035,"pValue":0.1793,"breaches":1,"observations":59,"pass":true}}`，`benchmark=SH.000300`、`benchmarkSource=futu/quote_history_kline`、窗口 `2026-06-29 → 2026-09-18`。测试 `platform/tests/test_v3_analytics.py::test_var_uses_the_historical_quantile_and_cvar_the_tail_mean`（`:303`）、`::test_beta_estimator_is_population_cov_over_sample_variance`（`:264`）、`::test_breaches_matching_the_expected_rate_pass`（`:219`）。❌ **杠杆率**：`grep -rni "leverage\|杠杆" platform/server` → 5 命中，**全部**是窝轮 `leverage_direction`/`leverage_multiple` **请求参数**（`mcp_tools.py:1006-1007`、`futu_data.py:1181-1182`、`app.py:242`），**无组合杠杆读数**。❌ **流动性风险**：`grep -rni "liquidity\|流动性" platform/server` → **0**。❌ **绩效归因**：`grep -rni "attribution\|performance_attribution\|归因分析" platform/server` → **0**（`归因` 27 处均为「成交按市场归因」）。❌ **资金检查**：`grep -rni "buying_power\|available_funds\|资金检查" platform/server` → **0**（`check_order` 规则 1 只在「缺 NAV/金额」时退回 `manual`，不校验可用资金） | **✅对齐 → 🟡部分（推翻）**：第一轮把「杠杆/流动性未见实现」写进了说明却**没降级状态**。逐项后 6 项达标、**4 项 0 实现** |

#### F. 监控与可视化（规格 §3.6）

| 编号 | 规格原句（摘录） | 状态 | 实现位置 file:line | 真实证据 | 与第一轮差异 |
|---|---|---|---|---|---|
| **FR-MON-001** | 「每笔交易记录完整的决策链路：**信号溯源**（因子值、模型输出、情绪评分）、**决策快照**（市场状态和参数配置）、**执行链路**（订单全生命周期事件）」（spec:331） | 🟡 **部分** | stage 落盘 `platform/server/v3_analytics.py:789`；`platform/server/v3_ops.py:2071`（`GET /api/v3/brain`）、审计链 `platform/server/audit_chain.py:1`（354 行）+ `platform/server/v3_ops.py:1616` 附近（`/api/v3/audit`） | 实测 `GET /api/v3/brain` → `decision.stages`（PDAT…PET 全在）、`proposals[].basis="综合动量 z=0.7071（mom_20=0.36952）"`、`asOf` / `market` / `universe_source="futu/sim_trade_position_list#US"` → 信号溯源（**仅动量类**）✅ + 决策快照 ✅；实测 `GET /api/v3/audit?window=120` → `stats={"signals":3,"orders":1,"order_kinds":{"订单查询":1},"fills":1,"linked":0,"unlinked":2,"link_rule":"同标的、信号时间在前且间隔 ≤ 7 天"}`、`entries` 5 条（含 `kind/at/ticker/detail/source/source_label/origin`）→ 执行链路 ✅。**模型输出与情绪评分未进决策链**：`basis` 无 ML/情绪项（v3_nlp 虽已在线，但零接线） | 与第一轮同判；**新证据**：v3_nlp 上线**没有**改变「情绪评分未进决策链」这一事实 |
| **FR-MON-002** | 「系统概览…行情图表（**TradingView K线图**叠加策略信号）、策略分析（因子敏感性热力图）、决策面板、风险监控、交易记录、**Harness 会话状态（Agent Loop 运行状态、工具调用统计）**」（spec:335） | 🟡 **部分** | 前端 10 页 `platform/web-pro/src/pages/{overview,brain,market,strategy,risk,execution,gateway,tools,settings,research}.jsx`；图表 `platform/web-pro/src/components/charts.jsx:1-3`（`LineChart` `:17` / `CandleChart` `:46` / `Heatmap` `:89` / `BarList` `:131`）；Harness 状态 `platform/web-pro/src/pages/brain.jsx:162-198`、`:673-684`、`:942-990`、`gateway.jsx:209-320` | **TradingView 未使用（确定）**：`platform/web-pro/package.json` dependencies = `{"@ant-design/pro-components","antd","dayjs","react","react-dom"}`，devDependencies = `{"@vitejs/plugin-react","vite"}` —— **无任何图表库**（无 `lightweight-charts` / `tradingview` / `echarts` / `recharts` / `@antv/*` / `klinecharts`）；`charts.jsx:1-3` 自述「工作台共享图表组件（**纯 SVG，无额外依赖**）」，K 线由 `CandleChart`（`:46`）用 `<rect>` 手绘蜡烛 + 成交量柱。**Harness 会话状态**：页面**有真实字段** —— `brain.jsx:673`「Agent Loop 事件流（/api/v3/brain · sdk.events）」、`:680`（step/start · assistant/message · tool/call · tool/result · turn/end）、`:951`「落盘回合数（turns）」、`:942`（serverInfo）、`:947`（route）；**但数据源恒空** —— 实测 `GET /api/v3/brain` → `sdk={"status":"unavailable","serverInfo":null,"route":null,"lastTurn":null,"turns":[],"events":[]}`（21:2x）。**工具调用统计：真实存在** —— `/metrics` → `quantwb_mcp_calls_total 95`、`quantwb_mcp_errors_total 0`、`quantwb_tool_calls_total{tool="audit"} 9`（+confirmation 12 / equity 12 / orders_open 12 / positions 12 / schedule 21 / sources 11 / deals_today 4 / events 2） | 与第一轮同判；**新确定项**：TradingView 是规格点名的具体库，实测**未采用**，手绘 SVG 替代（第一轮未核依赖） |
| **FR-MON-003** | 「平台应记录每次 headless 调用的完整信息：**任务提示词、stdout 答案、stderr 诊断信息、退出码、执行耗时、token 消耗估算**。这些日志写入平台数据库，**不依赖 Harness 的 Session 存储**」（spec:339） | 🟡 **部分** | 表结构 `platform/server/v3_db.py:156-170`（`headless_log`：`id, started_at, success, exit_code, duration_ms, tokens_estimate` + payload JSON）、`:30`（设计注释同列）、`:114`（`HEADLESS_LOG_FILE`）、`:218`（JSONL 冷备映射 `{"file":"v3-headless-log.jsonl","table":"headless_log","format":"jsonl"}`） | **表/冷备/迁移齐备，但生产者与读取端都缺**：`grep -rn "headless_log\|HEADLESS_LOG_FILE" --include=*.py .`（排除 node_modules/.git）= **仅 5 处**：`v3_db.py:30/114/156/218` + `platform/tests/test_v3_db.py:175/180/187`（**只有测试用**）。`platform/server/v3_ops.py` 里 `headless.last` **硬编码 `[]`**（`:1932`、`:2088`），**无任何读取 `headless_log` 的端点**；实测 `GET /api/v3/brain` → `headless={"today":{"total":0,"success":0,"failed":0,"avgMs":0,"killed":0},"breaker":null,"last":[],"status":"unavailable"}` | 与第一轮同判（部分）；**加深**：第一轮只说「生产者缺失」，本轮补上「**读取端也缺失**」——`last` 是硬编码空数组，不是「表空所以查出来空」 |

#### G. 非功能需求（规格 §4）

| 编号 | 规格原句（摘录） | 状态 | 实现位置 file:line | 真实证据 | 与第一轮差异 |
|---|---|---|---|---|---|
| **NFR-PERF §4.1** | 六项：行情数据延迟 `< 100ms`；订单执行延迟 `< 500ms`；MCP 工具调用延迟 `< 2s`；Headless 调用延迟 `< 30s`；SDK 会话首次握手 `< 30s`；系统可用性 `99.9%`（spec:346-353） | ❓ **未验证** | `platform/server/observability.py:831`（`@app.get("/metrics")`） | **逐项标注**：①行情 `<100ms` —— **未验证**（无埋点；且 A 股实时权限缺失 `errcode=-9`，当前权限下不可测）；②订单 `<500ms` —— **未验证**（无埋点，且禁写）；③MCP `<2s` —— **部分可测且当前不达标**：`/metrics` → `quantwb_mcp_call_duration_seconds 2.384`（**进程内均值**，超 2s）；④Headless `<30s` —— **未验证**（通道未挂载，无样本）；⑤SDK 握手 `<30s` —— **未验证**（通道未挂载）；⑥可用性 `99.9%` —— **未验证**（无 SLI/SLO 采集，只有 `quantwb_up`）。**分位数口径**：`grep -in "p95\|p99\|percentile\|quantile\|histogram" /tmp/m.txt` → **0 命中**；`/metrics` 178 行里延迟只有**均值**（`quantwb_mcp_call_duration_seconds`）。前端亦自述（`gateway.jsx` 渲染文案）「metrics 只有全部调用的平均耗时…未采集单工具 P95」 | 与第一轮同判（未验证）；**新增**：MCP 平均延迟 **2.384 s > 规格 2 s** 是本轮唯一可对照的实测反例；并确认 `/metrics` **无任何分位数口径** |
| **NFR-SEC §4.2** | 「富途 OpenAPI 的**登录密码和交易解锁密码加密存储**。API Token 通过环境变量或密钥管理服务注入。所有交易操作记录审计日志。支持**多级权限控制**」（spec:357）；「`dsh-harness-mcp-server` 默认绑定到 127.0.0.1」（spec:359） | 🟡 **部分** | 凭据：`platform/server/v3_credentials.py:55`（`_write_file` 原子写 + `0o600`）、`platform/server/futu_openapi.py:29`（`write_private_key`：PEM 校验 + `fsync` + `0o600` 原子写）、`:41` / `:76`（`serialization.load_pem_private_key(..., password=None)` ⇒ **不加密**）；token 中间件 `platform/server/app.py:775` 附近；审计 `platform/server/audit_chain.py:1` + `platform/server/v3_ops.py:1616` 附近；绑定 `platform/server/config.py:13`（`host:"127.0.0.1"`） | **①密码加密存储**：实现**不存密码** —— 通道是 `appkey` 模式（RSA 私钥 PEM + OAuth bearer），**没有**「登录密码 / 交易解锁密码」字段（`grep -rni "解锁密码\|登录密码\|unlock_password\|trade_pwd" platform/server` → 0）。私钥以**明文 PKCS8 PEM**落 `~/.dsh/futu-openapi-key.pem`（`load_pem_private_key(pem, password=None)` ⇒ 无口令保护），文件权限 `-rw-------`（宿主实测）。→ **「加密存储」实为「文件权限保护」，不是加密**；规格该子项按当前通道架构是**被替代**而非实现。②Token：`TUSHARE_TOKEN` 走 env（`platform/server/v3_credentials.py:27` `"env":"TUSHARE_TOKEN"`，env 优先于文件）✅；富途 token 落 `~/.dsh/futu-token`（`-rw-------`）③审计日志：`GET /api/v3/audit?window=120` → `HTTP 200`，真实 entries（见 FR-MON-001）✅；`/api/v3/settings` 实测凭据只回「是否注入 + 掩码尾号」（无明文）✅。④**多级权限控制 ❌**：`grep -rni "\brole\b\|rbac\|多级权限" --include=*.py platform/server` → **0**；现只有「单一 Bearer token 网关 + 页面口令闸门」。⑤`dsh-harness-mcp-server` **未安装**（`ls ~/.dsh/profiles/node_modules/@deepseek-ai/dsh-harness-mcp-server` → No such file），该项不适用；平台自身 `config.py:13` 绑 `127.0.0.1` ✅ | 与第一轮同判（部分）；**新增确定结论**：规格点名的「登录/交易解锁密码」在当前 appkey 通道下**根本不存在**，「加密存储」的实际形态是 0600 明文 PEM |
| **NFR-COMPAT §4.3** | 「`@deepseek-ai/dsh-tools` 与 MCP 均处于 Developer Preview…集成层需做好版本适配和降级预案，锁定已验证的 API 形态」（spec:363） | 🟡 **部分** | 降级案例 `docs/v3-integration.md` §1.6；`docs/architecture.md` 有意差异清单；自动化门禁 `platform/tests/test_mcp_parity.py`；`platform/install/quant-headless/pnpm-workspace.yaml`（`autoInstallPeers:false` + `nodeLinker:hoisted` 的由来写在注释里，有**真实报错证据**） | 有**真实降级案例 + 文档 + 自动化 parity/schema 测试**；**无 CI 门禁、无 dsh 版本矩阵**（仓库无 `.github/workflows` 命中的 dsh 版本矩阵）。`dsh-tools` 与 MCP 的实际版本：`@deepseek-ai/dsh-mcp-client` `0.1.5-rc.2`（`package.json`），MCP SDK `^1.12.0` | 与第一轮同判 |
| **NFR-EXT §4.4** | 「数据源适配器采用**插件化设计**。**策略引擎支持热加载**。**执行算法可插拔**」（spec:367） | 🟡 **部分** | 数据源：`platform/server/v3_sources.py`（`Deps` 可注入）、`platform/server/v3_fallback.py:1`（链可编排）；策略注册表：`plugins/core/python/trading_core/strategies.py`（`REGISTRY`）、`platform/server/settings_api.py:213-220`（读 `strategies.REGISTRY`） | 测试 `platform/tests/test_v3_sources.py`（Deps 注入）、`platform/tests/test_v3_fallback.py`（16 例）证明**数据源可插拔** ✅。**策略热加载 ❌**：`grep -rni "热加载\|hot.reload\|hot reload" --include=*.py platform/server` → **0**；`REGISTRY` 是模块级静态字典（`strategies.py`），无运行期 reload 路径。**执行算法可插拔 ❌**：0 命中 | 与第一轮同判 |

#### H. 系统设计条目（规格 §5 / §6 / §8 / §10）

| 编号 | 规格原句（摘录） | 状态 | 实现位置 file:line | 真实证据 | 与第一轮差异 |
|---|---|---|---|---|---|
| **DES-5.1** | K8s 集群 + 7 微服务（scheduler/strategy/risk/data/exec/gateway/web）+ Kafka 消息总线 + Redis Cluster + PostgreSQL（spec:375-408） | ⚪ **不适用** | 单容器 + SQLite：`platform/server/v3_db.py:119`（`TABLE_SPECS`）、`platform/deploy/` | 用户显式改写（「不用集群，单容器就行」）；`docs/e2e-and-data-gaps.md` 第四轮 §7.4/§7.5 登记迁移与并发证据；`/metrics` 可见单进程模型（`quantwb_process_start_time_seconds`、`quantwb_process_uptime_seconds`） | 与第一轮一致 |
| **DES-5.2** | 「Gateway 服务是平台与 Harness 之间的唯一接口。它包含**三个子模块**：MCP Bridge / SDK Client / Headless Runner」（spec:412） | 🟡 **部分** | 同 FR-GATEWAY-001 / 002 / 003 | 实测 `GET /api/v3/gateway` → `mcp=running`（116 工具）、`sdk=unavailable`、`headless=unavailable` → **1/3 挂载** | 与第一轮一致（同源，不重复计分） |
| **DES-6** | 组合顺序（bundles patch → profile `cordis.patch.yml` → home patch → `--patch`）；量化 system prompt（单笔 2% / 行业 20% / 回撤 15%）（spec:562-618） | ✅ **对齐** | `platform/install/quant-headless/cordis.patch.yml:39-99`（`system-prompt` 行 `personaPrefix`）+ `:6`（组合顺序注释）；阈值同源 `platform/server/v3_ops.py:107` | `platform/install/quant-headless/README.md:155`：`--dump-config` 退出码 0，`personaPrefix` 为量化分析师文本；`LIMITS` 与 spec:600-602 的 2% / 20% / 15% **逐项一致** | 与第一轮一致 |
| **DES-8.2** | 环境变量表：DSH_HOME / DEEPSEEK_API_KEY / QUANT_MCP_NODE / QUANT_MCP_SERVER / QUANT_MCP_CWD / QUANT_MCP_LOG / FUTU_OPEND_HOST / FUTU_OPEND_PORT / TUSHARE_TOKEN（spec:775-785） | 🟡 **部分** | `platform/server/v3_ops.py:118-120`（`ENV_KEYS` 白名单，`:1470-1471` 只读 `os.environ.get(key)` 上报 `injected/source`）；`platform/server/v3_credentials.py:27`（TUSHARE_TOKEN env 优先）；`platform/server/v3_sources.py:1398`（Tushare HTTP） | **逐项 grep 证据（`platform/server scripts install plugins`，排除 node_modules/dist）**：`DSH_HOME` → 336 处，脚本/进程**真实读取**（`scripts/research_duty.sh:15`、`scripts/futu_auth.py:40`）✅；`TUSHARE_TOKEN` → 29 处，**平台真实读取**（`v3_credentials.resolve_tushare_token(home, os.environ.get)`，`v3_ops.py:1687`）✅；`QUANT_MCP_NODE` / `QUANT_MCP_SERVER` / `QUANT_MCP_CWD` / `QUANT_MCP_LOG` → **各仅 1 处命中，全在 `v3_ops.py:118-119` 的 `ENV_KEYS` 字符串常量里**，**零功能读取** ❌；`FUTU_OPEND_HOST` / `FUTU_OPEND_PORT` → **各仅 1 处命中，同在 `v3_ops.py:119`**，**零功能读取** ❌；`DEEPSEEK_API_KEY` → **仅 1 处命中（`v3_ops.py:118`）**，平台不承载 LLM 循环 → 零功能读取 ❌。实测 `GET /api/v3/settings`（21:2x）→ `env=[{"key":"DSH_HOME","injected":true},{"key":"DEEPSEEK_API_KEY","injected":false}×8]`，`futu.channel="openapi"`、`futu.openapi.mode="appkey"`、`config_keys=["algorithm","app_key","mode","private_key_path"]`。**实际传输 ≠ 规格**：走 **HTTP `/mcp`（streamable-http）**，不是规格 §8.2/§5.2.1 的 **stdio node 进程**（`QUANT_MCP_NODE`+`QUANT_MCP_SERVER`+`QUANT_MCP_CWD` 三件套）——`url` 见 `platform/install/quant-headless/cordis.patch.yml:162` | 与第一轮同判；**本轮把「读没读」查到每一处命中数**：9 个变量里只有 **2 个**被平台功能性读取（`DSH_HOME`、`TUSHARE_TOKEN`），其余 7 个只是「上报注入状态」的字符串 |
| **DES-8.3** | 「Prometheus + Grafana 监控以下指标」（spec:789）；9 类指标与阈值：Headless 成功率 `<95%`、Headless 平均耗时 `>60s`、SDK 会话活跃数 `>10`、MCP 延迟 `>5s`、MCP 失败率 `>5%`、数据源断连、数据延迟 `>5min`、订单执行延迟 `>1s`、风控阻断突增（spec:791-801） | 🟡 **部分** | `platform/deploy/monitoring/alerts.yml`（**23 条** `alert:` 规则，7 组）、`platform/deploy/monitoring/prometheus.yml`（scrape `127.0.0.1:8397` `/metrics` 15s）、`platform/server/observability.py:831`（`/metrics`） | **逐条映射（规则名或明确缺失）**：①Headless 成功率 `<95%` → **缺失**（`grep -in "headless" alerts.yml` → 0）；②Headless 耗时 `>60s` → **缺失**（0）；③SDK 会话活跃数 `>10` → **缺失**（`grep -in "sdk" alerts.yml` → 0）；④MCP 延迟 `>5s` → **缺失**（无延迟规则，只有 `QuantMcpToolCallFailureRateHigh`）；⑤MCP 失败率 `>5%` → ✅ `QuantMcpToolCallFailureRateHigh`（alerts.yml:90，`for: 10m`）；⑥数据源断连 → ✅ `DataSourceChainUnavailable`（:183）+ `DataSourceFellBackToSecondary`（:204）+ `QuantPushChannelDisconnected`（:430）；⑦数据延迟 `>5min` → **⚠️ 口径不符**：只有 `DataSourceProbeStale`（:222，`time() - quantwb_datasource_probe_timestamp_seconds > 86400`，**24h** 缓存过期，语义是「监控本身静默」而非「数据延迟 5 分钟」）；⑧订单执行延迟 `>1s` → **缺失**（`grep -in "order_exec\|latency" alerts.yml` → 0）；⑨风控阻断突增 → **⚠️ 口径不符**：`RiskBlockedOrderAppeared`（:272，`delta(quantwb_oms_orders{stage="blocked"}[1h]) > 0`，语义是「1 小时出现任何阻断」而非「突增」）。**Grafana 未部署**：`grep -rni grafana`（排除 node_modules/.git）= **8 处，全是文档**；`platform/deploy/monitoring/README.md:350-354` 自述「❌ 没有安装 Prometheus/Grafana/Alertmanager…❌ 没有建 Grafana 面板」 | 与第一轮同判；**本轮给出 9 类逐条映射**：**1 类覆盖、2 类口径不符、6 类缺失**（第一轮说「覆盖 7 类」，与文件实证不符，予以更正） |
| **DES-10** | 五项关键决策：三通道分工 / Headless 不依赖会话状态 / 工具粒度控制 / 外部熔断保护 / 版本锁定与降级预案（spec:840-848） | 🟡 **部分** | 同 FR-GATEWAY-001~004、FR-TOOLS-003、NFR-COMPAT | 决策 1 **部分**（三通道 1/3 挂载：MCP running、SDK/Headless unavailable）；决策 2 **不成立**（服务内无 Headless，只有外置 `research_duty.sh` 每次新建一次性会话）；决策 3 **未落地**（116 工具直暴露 ≈2 万 token；无 `list_tools`/`call_tool`）；决策 4 **部分**（超时 ✅ `research_duty.sh:17`；并发上限 3 ❌；token 预算 200K ❌；`breaker=null`）；决策 5 **部分**（有真实降级案例与 parity 测试，无 CI 门禁与版本矩阵） | 与第一轮一致 |

### 2.4 「无法验证」项（本轮收敛后仍然存在的）

| 条目 | 为什么仍然无法验证 | 第一轮 → 第二轮 |
|---|---|---|
| **NFR-PERF §4.1** 六项延迟/可用性 | 只读审计不做压测；行情/订单延迟**无埋点**；Headless 与 SDK 通道未挂载**无样本**；`/metrics` **无分位数**（只有均值）不足以判阈值。可对照的只有 MCP 均值 2.384 s（已超规格 2 s） | 仍「未验证」；但**新增实测反例** |
| **FR-EXEC-001 的 live 全生命周期** | live 写自述「未经真实 live 下单验证」（`docs/P4-live-trading.md`），本轮禁写 | 不变 |
| **FR-DATA-001 条件单的 live 券商往返** | 同上，禁写；只验证到「契约 + 闸门 + 单测」层 | **收敛**：从「未验证/未见实现」变为「契约层已实现、live 往返未验证」 |
| **Harness 是否真的挂载了 `quantwb` MCP 行**（FR-GATEWAY-001 的运行时事实） | 本轮无法做运行时观测：父会话日志里没有任何 `mcp__quantwb__*` 的 `tool/call`（14 次调用全是 bash/edit/subagent），本 subagent（子代理）会话的工具面也不含这些工具。只能给到**配置级推断**（`serverName: quantwb` + 客户端命名模板）与**文档旁证**（headless 真跑列出 77 个 MCP 工具）。可在 Harness 里跑一次 `research_tasks_list`/任一 `mcp__quantwb__*` 工具或看会话工具清单来直接判定 | 第一轮没有区分「平台 `/mcp` 有 116 条」与「Harness 是否挂了这一行」——本轮明确拆开 |
| **NFR-SEC §4.2 的「加密」强度** | 宿主文件权限是 0600，但**没有**任何加密实现；「加密存储」这条在 appkey 架构下**没有对应实体**，只能如实记为「不适用/被替代」，不是「验证通过」 | 第一轮只记「0600 非加密」，本轮补上「根因是通道不含密码字段」 |
| **/mcp 工具面规模对模型的实际影响** | 只能量化 schema 体积（79,624 字符 ≈2 万 token），真实「撑爆」与否取决于模型上下文窗口，本轮未做注入实验 | 第一轮「未做 MCP 握手」→ 本轮**已握手**，规模可量化 |

### 2.5 新发现的缺口（按影响排序）

| # | 新发现 | 影响 | 证据 |
|---|---|---|---|
| **N0** | **outbound 反向桥完全不存在**（FR-GATEWAY-001 第三子项）：没有任何机制把 DSH 的 `ctx.tools` 暴露成 MCP 服务器（规格要 `dsh:<toolName>`） | 规格的「双向」只实现了**单向**；外部 MCP 客户端**无法**调用 Harness 侧工具。`FR-GATEWAY-001`/`DES-5.2`/`DES-10` 决策 1 同时受影响 | `grep -rl "serveStdio\|McpServer\|createMcpServer" ~/.dsh/profiles/node_modules/@deepseek-ai/ ~/.dsh/profiles/web/node_modules/@bstester/` → **0**；`@helibeiqi` 未安装；`dsh-harness-mcp-server` 未安装 |
| **N1** | **前端把不存在的包当成数据源名称展示**：`market.jsx:726-727` / `overview.jsx:536` 的健康卡把平台自身的 workbench 工具面标成「**dsh-quant-data-mcp（workbench 工具面）**」 | 规格点名的 `dsh-quant-data-mcp` **从未接入**（实现层 0 引用），但页面把当前数据源冠以该名 → 验收人极易误判「第 5 个开源源已接入」。属诚实性问题（与第一轮 H 系列同族） | `grep -rn "dsh-quant-data-mcp" --include=* .`（排除 node_modules/.git）→ 16 处：spec 8 / 本文档 3 / **前端 3** / dist 2；`platform/server`+`plugins`+`install` = **0** |
| **N2** | **`FR-MON-003` 的读取端也缺失**：`headless.last` 在 `v3_ops.py:1932`/`:2088` **硬编码 `[]`**，即使将来有 headless 调用写进 `headless_log` 表，也没有任何端点会读出来 | 补了生产者也没用——读取端要一起补。这是第一轮「生产者缺失」之外的第二半 | `grep -rn "headless_log" --include=*.py .` → 5 处，全在 `v3_db.py` 与 `test_v3_db.py`；`v3_ops.py` 无读取 |
| **N3** | **`FR-EXEC-003` 的 4 个子项 0 实现**（杠杆率 / 流动性风险 / 绩效归因 / 资金检查），第一轮用「未见实现」一笔带过却维持了「对齐」状态 | 事中风控只覆盖回撤；事后只有统计量、没有归因；事前不校验可用资金。规格 §3.5 的「三段式风控」实际是「一段半」 | `grep -rni "liquidity\|流动性" platform/server` → 0；`grep -rni "attribution\|归因分析" platform/server` → 0；`grep -rni "buying_power\|资金检查" platform/server` → 0；`杠杆` 仅 5 处且全为窝轮请求参数 |
| **N4** | **MCP 工具面 schema 成本的硬数字**：`tools/list` = **79,624 字符 / 116 条 ≈ 2 万 token**，且**没有** `list_tools`/`call_tool` 发现代理 | 每次请求都要带上全量工具 schema；规格「决策 3：工具粒度控制」明确要避免这件事，当前是**反向** | MCP 握手实测（21:27）：`len(json(tools))` = 79,624；`"list_tools" in names == False`、`"call_tool" in names == False` |
| **N5** | **仓库工作区与线上预设不一致**：`quant-platform-mcp` 行在**仓库工作区** `agent.cordis.yml:184` 是 `disabled: true`，在**线上安装的预设** `~/.dsh/.agent-presets/dsh-trading-agents/agent.cordis.yml:184` 是 `disabled: false`；两份文件还有另一处注释差异。仓库 HEAD `fa9bc31`（预设）≠ 工作区 `48cd3d7` | 「仓库里看到的」不等于「Harness 实际跑的」：按仓库现状判断会得出「quantwb 工具面未挂载」的相反结论；本轮 FR-GATEWAY-001 的运行时结论因此只能标「无法验证」 | `diff /home/penn/workspace/dsh-trading-agents/agent.cordis.yml ~/.dsh/.agent-presets/dsh-trading-agents/agent.cordis.yml` → `184c184: < disabled: true --- > disabled: false`、`160c160` 注释差异；`git -C ~/.dsh/.agent-presets/dsh-trading-agents log -1` = `fa9bc31` |

### 2.6 本轮对第一轮的「判浅 / 判错」汇总

| 类型 | 条目 | 一句话 |
|---|---|---|
| **判错（证据找错文件）** | `FR-TOOLS-002` | 第一轮 grep 只扫 `mcp_tools.py`，`annotations` 实际在 `v3_mcp.py:646-648`；结论由「缺失」改「部分」 |
| **判宽（把用户豁免扩到整条）** | `FR-GATEWAY-001` | 用户豁免 adapter 载体 ≠ 豁免 outbound/命名契约；结论由「不适用」改「部分」，并暴露 N0 |
| **判浅（只数域不核名）** | `FR-TOOLS-001` | 域 6/6 ✅ 就当整条对齐，漏核规格表的 13 个方法名（命中 0） |
| **判浅（说明里写了缺失，状态不降级）** | `FR-EXEC-003` | 「杠杆/流动性未见实现」写进了备注却维持「对齐」 |
| **证据过期（服务已重启）** | `FR-STRAT-003` | 「`/api/v3/sentiment` 返回 SPA」已失效——现返回真实 JSON 评分 |
| **证据过期（服务已重启）** | `FR-EXEC-002` | 「行业读数仍是 `no-data`、待重启」已失效——现为 `cache/futu/info_owner_plate`、三市场 `breach:true` |
| **未验证项可收敛** | `FR-DATA-001` 条件单 | 从「未见实现」收敛为「契约 + 闸门 + 单测已实现，live 往返未验证」 |

## 一、汇总表（**第一轮归档值 —— 已被 §二轮取代，见上**）

> ⚠️ **本节是 2026-09-20 第一轮（快照 `8b75fcc`）的归档结论，保留用于对照，不再作为当前口径。**
> 修正后的计数与逐条结论见文首「**第二轮核对（基于 `docs/v3-spec.md` 原文）**」。
> 第一轮与第二轮的差异（4 条状态变化 + 2 条证据级更正）在该节 §2.2 与 §2.6。

规格条目共 **31 条**：功能需求 21（FR-\*）+ 非功能需求 4（NFR-\*）+ 设计条目 6（DES-\*）。

| 状态 | 条数 | 占 31 条比例 | 占可评条目（31−2 不适用−1 未验证 = 28） |
|---|---|---|---|
| ✅ 对齐 | **5** | 16.1% | 17.9% |
| 🟡 部分对齐 | **21** | 67.7% | 75.0% |
| ❌ 缺失 | **2** | 6.5% | 7.1% |
| ⚪ 不适用（用户显式改写/豁免） | **2** | 6.5% | — |
| ❓ 未验证 | **1** | 3.2% | — |
| 🚧 已知限制（非规格条目，单列） | **1** | — | — |

- **已对齐 + 部分对齐 = 26/28 = 92.9%**；**完全缺失 2 条**（`FR-GATEWAY-002` SDK JSON-RPC 会话通道、
  `FR-TOOLS-002` 工具注册规范）。
- **部分对齐的 21 条**里，多数是「能力在、口径或覆盖面不足」：14 条属**子项缺失**（如六类因子缺
  3 类、策略族缺事件驱动/统计套利、Headless 触发缺 08:30/12:00），7 条属**通道/接线未挂载**
  （SDK、Headless、NLP、行业闸门读数）。
- **已知限制 1 条**：A 股实时行情权限（`errcode=-9 realtime quote permission required`），
  按用户口径**不计入缺口计数**，单独归入「已知限制」。

### 与「用户本轮点名的三件事」对账

| 用户要求 | 规格条目 | 现状 |
|---|---|---|
| 接入红线闸门 | 派生自 FR-EXEC-002/003 | **工作区已接**（`v3_ops.py` 引入 `v3_risk_gate`，`check_order` 用真实行业读数）；**运行进程仍是 `no-data`**（实测 `/api/v3/oms/orders` → `industry_source="no-data…按 0% 不阻断"`）→ 需重启才生效 |
| 自研 NLP | FR-STRAT-003 | **已写**（`platform/server/v3_nlp.py`，自研中文金融词典 + 否定/程度修饰 + 时间半衰，路由 `GET /api/v3/sentiment`）；**未提交、运行进程未挂载**（实测 `/api/v3/sentiment` 返回 SPA 的 `index.html`）；工作区 `app.py:921` 已把它加进装配列表 |
| A 股权限可忽略 | FR-DATA-001 降级 | 已按「最近收盘 + `as_of`」降级，页面显式标注；**归为已知限制，不重复计缺口** |
| adapter 去掉、走平台 MCP、所有交互走 MCP | FR-GATEWAY-001 | **用户豁免**：规格点名 `@helibeiqi/dsh-cordis-universal-adapter`，实测在本部署 **0.1.5-rc.2 不可用**（三段真实错误见 `docs/v3-integration.md` §1.6）；替代路径（`dsh-mcp-client` 挂 `/mcp`）已在用，且工作区新增 `v3_mcp.py` 把 `/api/v3/*` 路由**反向暴露为 MCP 工具**（`app.py` 的 `v3_mcp.register(app.state.mcp, app)`） |

---

## 二、逐条明细表

> 证据口径：`实测` = 本次对 `http://127.0.0.1:8397` 的真实只读调用（时间 2026-09-20 18:44–18:52 +08:00）；
> `测试` = 仓库内测试用例名（`platform/tests/*.py`，未在本轮重跑，按题面「测试名」口径引用）。
> 未取得证据的一律写「未验证」。

### A. Harness 集成网关（规格 §3.1）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-GATEWAY-001** | 部署 `@helibeiqi/dsh-cordis-universal-adapter` 作双向 MCP 桥 | ⚪ **不适用** | 替代：`platform/install/quant-headless/cordis.patch.yml`（`quant-platform-mcp` 行，走 `@deepseek-ai/dsh-mcp-client`）；工作区新增 `platform/server/v3_mcp.py:1` + `app.py:1026` 反向暴露 | 平台自身 `/mcp`（MCP streamable-http） | 实测 `GET /api/v3/gateway` → `channels.mcp={"status":"running","protocol":"MCP streamable-http（/mcp…）","tools":82}`；`docs/v3-integration.md` §1.6 记录 adapter 在本部署**三处硬错误**（`pnpm-workspace.yaml` 缺失 / scope 缺失 / `CallId`→`ToolCallId` 改名）→ **用户显式「adapter 可以去掉」**；注意 `channels.mcp.tools=82` 是**六域工具目录**条目数，`tools/list` 实际条数是 77 + `v3_*` 桥接工具（口径见 `docs/v3-integration.md` §1.5） |
| **FR-GATEWAY-002** | 部署 `@deepseek-ai/dsh-sdk-jsonrpc-server`，平台可驱动 Harness 会话（`initialize` / `session/prompt` / 事件流） | ❌ **缺失** | 无 | 无 | 实测 `GET /api/v3/brain` → `sources.sdk="本服务未挂载 SDK JSON-RPC 通道"`；`GET /api/v3/gateway` → `channels.sdk={"status":"unavailable","protocol":"换行分帧 JSON-RPC / stdio（未挂载）"}`；`~/.dsh/trading-venv/bin/pip list` **无 `deepseek-harness-sdk`**、仓库无任何 SDK 客户端代码（`grep -rl "deepseek_harness\|jsonrpc" platform/server` 命中 0） |
| **FR-GATEWAY-003** | 实现 Headless Runner（`dsh --profile headless`，stdout/stderr/exit 0/1/130 契约） | 🟡 **部分** | **有**：`scripts/research_duty.sh:5,16,90`（外置 shell + systemd timer，真跑 `dsh --profile headless` 并按退出码 fail-loud）**缺**：平台服务内无 HeadlessRunner 子模块 | `dsh --profile headless "<task>"` | 实测 `GET /api/v3/gateway` → `channels.headless={"status":"unavailable","reason":"本服务未挂载 Headless CLI 子通道（无 dsh --profile headless 子进程调度）"}`；`platform/install/quant-headless/README.md` §四表格给出**真跑退出码 0**的两次实测记录（dump-config 88 行、一次性任务列出 9 本地工具 + 77 MCP 工具；当时 `/api/v3/*` 桥接尚未落地，现在同一行会多出 `v3_*` 工具） |
| **FR-GATEWAY-004** | Headless 调度器：定时（08:30/12:00/16:00）+ 事件 + 流水线节点 + **外部熔断**（超时/并发/费用预算） | 🟡 **部分** | 定时链：`plugins/core/python/trading_core/daemon.py:65-105`（SH 16:00/16:05/16:10/16:15/16:25/16:30，HK 16:30-16:50，US 05:30-05:50，GLOBAL 18:50/19:05）；外置唤醒 `install/research-duty.timer`（`Mon..Fri 19:20`）；超时预算 `scripts/research_duty.sh:16`（`DSH_DUTY_TIMEOUT=1800`） | `trading_core` 调度链 + systemd | `systemctl --user list-timers` 实取：仅 1 个 `research-duty.timer`（NEXT Mon 2026-09-21 19:20）；`daemon.py` 链中**无 08:30 开盘前扫描、无 12:00 午间复盘**；`GET /api/v3/gateway` → `scheduler.recent` 为真实作业历史（19:05 `enqueue_research`、19:00 `reconcile`、18:50 `sync_calendar`…），`headless={"today":{"total":0},"breaker":null,"status":"unavailable"}` → **无并发上限、无 token/费用熔断** |
| **FR-GATEWAY-005** | 专属 Profile（`dsh-base` + `dsh-headless`，`package.json` + `cordis.patch.yml`） | ✅ **对齐** | `platform/install/quant-headless/package.json:1`（bundles=`@deepseek-ai/dsh-base`,`@deepseek-ai/dsh-headless`）+ `cordis.patch.yml`（11,898 B）+ `cordis.yml` + `pnpm-workspace.yaml` | DSH 组合树 | `README.md` §四：临时 `DSH_HOME` 真跑，`--dump-config` **退出码 0 / 88 行**，`personaPrefix` 已是量化分析师文本、16 行 `disabled` 白名单生效、`quant-platform-mcp` 已 insert；偏差：规格里的 `dsh-cordis-universal-adapter`/`dsh-quant-data-mcp` 两行被 `dsh-mcp-client` 替代（同上，用户豁免 adapter） |

### B. 平台量化工具域（规格 §3.2）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-TOOLS-001** | 六大工具域 data/alpha/ML/risk/execution/ecosystem | ✅ **对齐** | `platform/server/v3_ops.py:73`（`DOMAINS`）+ `:151`（`domain_of`）+ `:1497`（`GET /api/v3/tools`） | 工作台工具面 82 工具 + 4 个 V3 本地计算工具 | 实测 `GET /api/v3/tools` → `{"ok":true,"total":82,"domains":{data:32,alpha:7,ml:2,risk:4,execution:18,ecosystem:19}}`；`/metrics` → `quantwb_build_info{version="3.0",tools="82",domains="6"} 1` |
| **FR-TOOLS-002** | 工具定义遵循 AI-native 规范：schema 注入系统提示词 / 等长 null 对齐 / 规范 JSON + render 分离 / 全部 `isConcurrencySafe` / Skill 层 | ❌ **缺失** | `platform/server/mcp_tools.py` 仅做端点代理（`BoundTool(definition=…, handle=…)`，`:1358`） | 无 | `grep -n "isConcurrencySafe\|render\|annotations\|readOnlyHint" platform/server/mcp_tools.py` **0 命中**；工具输出是端点原始信封 JSON（无 `render` 分离、无等长 null 对齐），无 `skill/quant-research` 层 |
| **FR-TOOLS-003** | 工具数量控制在合理范围 + **工具发现代理**（`list_tools` / `call_tool` 两个间接入口） | 🟡 **部分** | 域分组/过滤：`v3_ops.py:1497`（`?domain=`）；工作区新增 `v3_mcp.py`（路由→MCP 工具桥，**按路由造工具**） | 同 FR-TOOLS-001 | 实测 `/api/v3/tools` 与 `/metrics` 均报 **82 个工具直接暴露**（`/mcp` 一次 `tools/list` 即 82 条 schema）；仓库无「发现代理」工具（`grep "list_tools\|call_tool"` 命中的是 MCP SDK 协议方法，非规格所指代理工具） |

### C. 数据层（规格 §3.3）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-DATA-001** | 富途 OpenAPI：实时报价/K线/盘口/逐笔/快照/板块 + 交易（市价/限价/**条件单**/改撤/查询/持仓） | 🟡 **部分** | 行情：`v3_market.py:55/79/133/147`；交易：`trading.py`（`SIM_MAX_QTY_ORDER_TYPES={LIMIT,MARKET}` `:121`）；f10 财报：`v3_sources.py`（`GET /api/v3/financials`） | 富途 OpenAPI（appkey 模式）/ 富途远程 MCP；`channel=openapi`（`trading-platform.json(futu_channel)`） | 实测可用：`/api/v3/orderbook?ticker=HK.00700` ✅（HK 实时有权限）；`/api/v3/financials?ticker=600519` → `source="futu/f10_detail/statements"` ✅；实测不可用：`POST /api/wb/rt_quote{codes:[SH.600000]}` → `{"code":"trading/futu-error","message":"富途业务错误（errcode=-9）：realtime quote permission required"}` → **A 股实时归已知限制**；**条件单**在代码中无实现（`grep 条件单 → 0`） |
| **FR-DATA-002** | 补 5 个开源源：Tushare / AKShare / OpenBB / SEC EDGAR / `dsh-quant-data-mcp` | 🟡 **部分（4/5）** | `platform/server/v3_sources.py:1648/1657/1670/1692/1705`（news/spot/financials/tushare/openbb）；降级链 `v3_fallback.py:1` | 实测：`akshare/stock_news_em`、`sec/companyconcept(us-gaap XBRL)`、`openbb/equity.fundamental.metrics`、`futu/f10_detail`；Tushare 走 `http://api.tushare.pro`（`v3_sources.py:94`） | 逐源实测：`/api/v3/news?symbol=600519` → `source=akshare/stock_news_em` ✅；`/api/v3/financials?ticker=AAPL` → `source=sec/companyconcept(us-gaap XBRL)`，`chain=[{source:…,ok:true,ms:5793}]` ✅；`/api/v3/openbb?symbol=AAPL` → `source=openbb/equity.fundamental.metrics` ✅；`/api/v3/tushare` → `{"ok":false,"error":{"code":"tushare/no-token","message":"TUSHARE_TOKEN 未注入…"}}`（实现有、token 无）；**`dsh-quant-data-mcp` 全仓库 0 引用**（`grep -rn` 无命中）→ 该子项未实现 |
| **FR-DATA-003** | 所有历史查询遵循 PIT；平台侧 `data/cache.py` 为唯一读取接口 | 🟡 **部分** | PIT 纪律：`v3_analytics.py:978`（回测 `t` 日只用 `≤t-1`）、`:1045`（ML 特征只用 `≤t`）、`v3_math.py:356`；缓存层：`platform/server/caches.py:1`（TTL 两级缓存，**非** `data/cache.py`） | 富途日 K + 本地缓存 | PIT：测试 `test_v3_ml.py::test_labels_are_forward_returns_not_contemporaneous`、`test_v3_analytics.py`（`test_align_*`）；规格点名的 **`data/cache.py` 不存在**（`ls platform/data/cache.py` 无此文件）→ 路径与「唯一读取接口」定位未按规格落地（现为 `caches.py` + 各模块自取） |
| **FR-DATA-004** | 数据源健康检查；主源不可用自动降级备用源 | ✅ **对齐** | `platform/server/v3_fallback.py:1`（`run_chain`）+ `:646`（`GET /api/v3/sources/status`）；探测定时器素材 `platform/install/quant-v3-probe.{service,timer}` | 逐级真实上游 | 实测 `/api/v3/sources/status` → `{"ok":true,"as_of":"2026-09-20T10:49:04Z","note":"所有降级都带 source 标注；两源都失败时如实报错（不返回占位数据）"}`；`/api/v3/spot` 全链失败体含 `chain=[{source:"akshare/stock_zh_a_spot_em",ok:false,ms:22443,attempts:[…]}]` + `error.message` 列明「按顺序试过 5 个接口全部失败」；测试 `test_v3_fallback.py`（16 例，含 `test_all_failed_returns_none_none_and_full_timeline`） |

### D. 策略层（规格 §3.4）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-STRAT-001** | 多因子选股：价值/成长/动量/质量/情绪/另类 **六类**；PDAT→PAAT→PCPT→PRT→PET 五阶段 | 🟡 **部分** | 五阶段：`v3_analytics.py:789`（`run_pipeline`，`:850-880` 逐阶段）；因子：`plugins/workbench/python/factors.py:27`（`FACTOR_SIGN`：mom_20/mom_60/vol_20/trend/rsi_14 + 估值） | 富途日 K + `workbench/factors` | 实测 `/api/v3/brain` → `decision.stages` 真取到 `{"PDAT":{"bars":240,"universe":["US.NVDA","US.MSTR"]},"PAAT":{"analyzed":2,"scoreSource":"workbench/factors(z)"},"PCPT":{"longs":[…]},"PRT":{"capped":true,"weightPctPerName":2.0},"PET":{"proposals":2}}`；**六类因子只覆盖动量/波动/趋势/RSI + 估值（价值）**：成长、质量、另类**无实现**（`grep 成长\|质量\|另类` 在 server/ 无因子实现）；情绪类由 FR-STRAT-003 的 `v3_nlp` 补（工作区） |
| **FR-STRAT-002** | 策略生成与优化：多因子 / ML（Lasso/LightGBM/MLP）/ **事件驱动** / **统计套利**；数千次回测 + 热力图 | 🟡 **部分** | `v3_ml.py:1`（特征 + Lasso/GBDT/MLP 纯计算核）、`v3_analytics.py:1030`（同口径评估）、`:1188`（参数网格热力图）；端点 `v3_analytics.py:1185/1199/1213` | 富途日 K（`series`）；实测 ML 端点落在 `akshare/sina` | 实测 `/api/v3/ml/models?ticker=SH.600519` → `{"ok":true,"source":"akshare/sina","as_of":"2026-09-11","sources":{"SH.600519":"akshare/sina"}}`；`/api/v3/ml/sweep`、`POST /api/v3/ml/backtest` 均在探针表内且返回真实回测；**事件驱动、统计套利两类策略 0 实现**（`grep 统计套利\|event_driven\|pairs → 0`）；LightGBM 以 `numpy-gbdt-lite` 替代（`docs/e2e-and-data-gaps.md` 第五轮 §二如实标注 `impl`）；测试 `test_v3_ml.py`（48 例，含 PIT 反证） |
| **FR-STRAT-003** | NLP 情绪：实时情绪评分 / **事件识别** / 多源情绪融合 / 情绪因子构建 | 🟡 **部分**（工作区已写，未挂载） | `platform/server/v3_nlp.py:1`（词典 `:154`、否定/程度 `:162`、时间半衰 `:197`、路由 `:936 GET /api/v3/sentiment`）；工作区 `app.py:921` 已加入装配列表 | 资讯（`akshare/stock_news_em`，经 `retry_akshare`） | **运行进程未挂载**：实测 `GET /api/v3/sentiment` 与 `/api/v3/nlp` 均返回 SPA 兜底 HTML（`<!DOCTYPE html>…`），非 JSON → 该能力**当前不可用**；测试 `platform/tests/test_v3_nlp.py`（55 例，含 `test_route_is_registered`、`test_no_news_returns_null_score_not_zero`）为工作区新增、未提交；**「事件识别」无独立实现**（`)` 只有事件极性词表，无事件抽取/分类器） |

### E. 执行层（规格 §3.5）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-EXEC-001** | 基于富途 OpenAPI 的 OMS，信号→订单全生命周期 | 🟡 **部分** | `v3_ops.py`（`OmsLedger`）、`v3_db.py:119`（`orders`/`oms_orders` 落库）、`v3_ops.py:1574`（`GET /api/v3/oms/orders`）、`:1586`（`POST /api/v3/oms/sync`） | 台账（sim 模拟盘）+ 富途 `sim_trade_*`；`nav_source="sim-ledger(equity.current)"` | 实测 `/api/v3/oms/orders` → `{"ok":true,"nav":…,"nav_source":"sim-ledger(equity.current)","drawdown_source":"sim-ledger(max_drawdown)","stages":{"manual":10},"orders":[…]}`；**live 写协议未做真实下单验证**（`docs/P4-live-trading.md` 自述），故只能算部分 |
| **FR-EXEC-002** | 分级审批：阈值内自动执行 / 超阈值人工确认 / 红线强制阻断 | 🟡 **部分** | `v3_ops.py:318`（`check_order` 纯函数，`:94` `LIMITS={"singlePct":2.0,"industryPct":20.0,"drawdownPct":15.0}`，`:97` `STAGES`） | 台账 NAV + 回撤 + 行业读数 | 实测 `/api/v3/oms/orders` → `stages={"manual":10}`（当前无 auto/blocked 行）；测试 `test_v3_ops.py::test_check_order_thresholds`（10 行）；**强制阻断的行业读数在运行进程仍是 `no-data`**：实测 `industry_source="no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）"` → 行业红线**不下闸**；工作区已改（`v3_ops.py:66` `import v3_risk_gate`、`:861` `industry_context`、`:948` 传入 `check_order`），测试 `test_v3_risk_gate.py`（工作区新增）→ **待重启生效** |
| **FR-EXEC-003** | 风控：事前（持仓限制/单笔上限/行业集中度/资金）+ 事中（回撤/杠杆/流动性）+ 事后（归因/最大回撤/VaR）+ VaR/CVaR/Beta/Alpha/IR/Kupiec | ✅ **对齐** | `v3_math.py:188`（`kupiec_pof`）、`:300-340`（`beta`/`alphaAnnPct`/`ir`）、`v3_analytics.py:447`（`risk_analytics`）、`:1134`（`GET /api/v3/risk/analytics`）；行业：`v3_industry.py:508` | 富途日 K + `sim-ledger` 权益 + 富途板块 | 实测 `/api/v3/risk/analytics?limit=60` → `{"ok":true,"sources":{"kline":"futu/quote_history_kline","nav":"sim-ledger(equity.current)","errors":[]},"benchmarkSource":…}`；`/api/v3/risk/industry?market=SH` → `sources={"plate":"futu/info_owner_plate","weights":"platform/portfolio（自选池等权（8 只））","nav":"sim-ledger(equity.current)"}`；测试 `test_v3_analytics.py::test_var_uses_the_historical_quantile_and_cvar_the_tail_mean`、`::test_beta_estimator_is_population_cov_over_sample_variance`。**杠杆/流动性**两项事中口径未见实现（`grep` 无对应读数）→ 该两条子项并入本行的部分说明 |

### F. 监控与可视化（规格 §3.6）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-MON-001** | 每笔交易记录完整决策链：信号溯源（因子值/模型输出/情绪评分）+ 决策快照 + 执行链路 | 🟡 **部分** | `v3_analytics.py:789`（stage 落盘）+ `v3_ops.py:1629`（`GET /api/v3/brain`）；执行链：`v3_ops.py:1616`（`/api/v3/audit`）+ `:1591`（`/api/v3/events`）；落库 `v3_db.py:156` 等 | 台账 + 富途事件/公告 + 审计链 | 实测 `/api/v3/brain` → `decision.proposals[].basis="综合动量 z=0.7071（mom_20=0.36952）"` + `stages`（PDAT…PET）+ `asOf`/`market` → **信号溯源有（当前只有动量因子）、决策快照有**；**模型输出与情绪评分未进决策链**（`basis` 无 ML/情绪项；`v3_nlp` 未挂载）→ 部分 |
| **FR-MON-002** | 7 类仪表板：系统概览/行情图表/策略分析/决策面板/风险监控/交易记录/**Harness 会话状态（Agent Loop、工具调用统计）** | 🟡 **部分** | 前端 10 页：`platform/web-pro/src/pages/{overview,brain,market,strategy,risk,execution,gateway,tools,settings,research}.jsx`；后端 `app.py:943`（overview）+ 各页端点（见 §五） | 全部经 `GET /api/v3/*` / `POST /api/wb/*` | 前 6 类**对齐**（页面与端点逐页对应，见 §五表格）；**Harness 会话状态只有「不可用 + 原因」**：实测 `/api/v3/brain` → `sdk.status="unavailable"`、`headless.status="unavailable"`、`turns":[]`，Agent Loop 运行状态**取不到**；工具调用统计**有**（`/metrics` → `quantwb_mcp_calls_total 19`、`quantwb_tool_calls_total{tool="audit"} 1`…） |
| **FR-MON-003** | 记录每次 headless 调用（提示词/stdout/stderr/退出码/耗时/token 估算）并写平台数据库 | 🟡 **部分** | 存储层已建：`v3_db.py:156`（`headless_log` 表）+ `:218`（JSONL 冷备映射）；**生产者缺失**（FR-GATEWAY-003 未挂载） | 无（表空） | 实测 `/api/v3/brain` → `headless={"today":{"total":0,"success":0,"failed":0,"avgMs":0,"killed":0},"breaker":null,"status":"unavailable"}` → **无任何 headless 调用记录**；`docs/e2e-and-data-gaps.md` 第四轮 §7.3 登记该表 schema（`headless_log(id,started_at,success,exit_code,duration_ms,tokens_estimate,payload)`） |

### G. 非功能需求（规格 §4）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **NFR-PERF §4.1** | 行情延迟<100ms、下单<500ms、MCP 调用<2s、Headless<30s、SDK 握手<30s、可用性 99.9% | ❓ **未验证** | — | — | 本次为**只读审计**，未做压测；且 A 股实时行情权限缺失（`-9`）使「行情延迟」指标在当前权限下不可测；`/metrics` 只有 `quantwb_mcp_call_duration_seconds`（进程内均值，实测 `1.592`s，为**均值非 P95**，gateway 页自注「metrics 无 P95 口径」）。**故按「未验证」如实记账，不猜** |
| **NFR-SEC §4.2** | 密码/token 加密存储、API token 经环境变量或密钥管理注入、全交易操作审计、多级权限、Harness MCP 仅绑 127.0.0.1 | 🟡 **部分** | `platform/server/v3_credentials.py:55`（原子写 + 0600）、`app.py:775`（token 中间件：`/api/*` 与 `/mcp` 需 Bearer）、`config.py:13`（`host:"127.0.0.1"`）；凭据 `~/.dsh/v3-credentials.json` 0600 | 页面上报「是否注入 + 来源」，不回显值 | 实测 `/api/v3/credentials?action=status&key=tushare_token` → `{"ok":true,"keys":[…]"已配置/未配置…掩码尾号"}`（**无明文**）；`/api/v3/settings` → env 表逐项 `injected/source`；页面核查：`settings.jsx` 凭据/env/审计表**不回显密钥**（§五）。缺口：**多级权限控制未见实现**（无角色/权限模型），「加密存储」实为 0600 文件权限（非加密） |
| **NFR-COMPAT §4.3** | `@deepseek-ai/dsh-tools` 与 MCP 处 Developer Preview，需版本适配与降级预案 | 🟡 **部分** | 预案证据：`docs/v3-integration.md` §1.6（adapter 三段失败 → 替代方案）；`docs/architecture.md` 有意差异清单；`platform/tests/test_mcp_parity.py`（MCP 面覆盖率+封闭 schema 的**自动化**门禁） | — | 有**真实降级案例与文档**；MCP 工具面已有自动化 parity/schema 测试，但**无 CI 门禁、无 dsh 版本矩阵** → 部分 |
| **NFR-EXT §4.4** | 数据源适配器插件化、策略引擎热加载、执行算法可插拔 | 🟡 **部分** | 数据源：`v3_sources.py:Deps`（可注入 fake，`:348-360`）；降级链 `v3_fallback.run_chain` 可编排 | — | 测试 `test_v3_sources.py`（58 例，Deps 注入）+ `test_v3_fallback.py`（16 例）证明**数据源与降级链可插拔**；**策略热加载、执行算法可插拔未见实现/证据**（策略为进程内函数）→ 部分 |

### H. 系统设计条目（规格 §5/§6/§8/§10）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **DES-5.1** | K8s 多服务（scheduler/strategy/risk/data/exec/gateway/web）+ Kafka + Redis + PostgreSQL | ⚪ **不适用** | 单容器 + SQLite：`platform/server/v3_db.py:119`；`platform/deploy/` | SQLite `<DSH_HOME>/v3.db` + 文件冷备 | **用户显式改写**（「不用集群，单容器就行」）；`docs/e2e-and-data-gaps.md` 第四轮 §7.4/§7.5 登记迁移与并发证据（10 线程×10 / 4 进程×25 全 0 异常）；与规格的 K8s/Kafka/Redis/PG 架构**不一致但为用户授权** |
| **DES-5.2** | Gateway 三子模块（MCP Bridge / SDK Client / Headless Runner） | 🟡 **部分** | 同 FR-GATEWAY-001/002/003 | — | 实测 `/api/v3/gateway`：MCP `running`，SDK `unavailable`，Headless `unavailable` → **1/3 挂载**（本行与 FR-GATEWAY-002/003 同源，不重复计分） |
| **DES-6** | Profile 组装顺序（bundles patch → profile `cordis.patch.yml` → home patch → `--patch`）；量化 system prompt（单笔 2%/行业 20%/回撤 15%） | ✅ **对齐** | `platform/install/quant-headless/cordis.patch.yml`（`quant-system-prompt` 段）+ `package.json` | DSH 组合 | `README.md` §四实测 `--dump-config` 退出码 0，`personaPrefix` 为量化分析师文本；阈值 2%/20%/15% 与 `v3_ops.py:94` `LIMITS` **逐项一致**（规格 §6.2 同值） |
| **DES-8.2** | 环境变量表（DSH_HOME / DEEPSEEK_API_KEY / QUANT_MCP_NODE / QUANT_MCP_SERVER / QUANT_MCP_CWD / QUANT_MCP_LOG / FUTU_OPEND_HOST / FUTU_OPEND_PORT / TUSHARE_TOKEN） | 🟡 **部分** | `platform/server/app.py` 设置页数据面 + `v3_ops.py` 设置组装 | 环境变量实测探测 | 实测 `/api/v3/settings.env` → `DSH_HOME injected=true`；`DEEPSEEK_API_KEY / QUANT_MCP_NODE / QUANT_MCP_SERVER / QUANT_MCP_CWD / QUANT_MCP_LOG / FUTU_OPEND_HOST / FUTU_OPEND_PORT / TUSHARE_TOKEN` **全部 `injected=false`**。原因不同：MCP 侧改走 `@deepseek-ai/dsh-mcp-client`（不需要 QUANT_MCP_* 三件套）；富途改走 **appkey 模式**（实测 `futu.openapi.mode="appkey"`，不需要 OpenD host/port）；`DEEPSEEK_API_KEY` 与平台服务无关（服务不承载 LLM 循环）。**规格 §8.2 的变量表已过时，但替代路径都工作** → 部分 |
| **DES-8.3** | Prometheus + Grafana；9 类指标与告警阈值（成功率<95%、耗时>60s、SDK 会话>10、工具延迟>5s、失败率>5%、数据源断连、数据延迟>5min、订单延迟>1s、风控阻断突增） | 🟡 **部分** | `platform/server/observability.py:819`（`GET /metrics`）、`:830`（`POST /api/v3/metrics/probe/refresh`）；规则 `platform/deploy/monitoring/{prometheus.yml,alerts.yml,dump_metrics.py}` | 进程内计数 + 只读缓存（**抓取路径零外部请求**） | 实测 `GET /metrics` → `text/plain`（`quantwb_up 1`、`quantwb_build_info{version="3.0",tools="82",domains="6"} 1`、`quantwb_mcp_calls_total 19`、`quantwb_mcp_errors_total 0`、`quantwb_mcp_call_duration_seconds 1.592`、`quantwb_http_requests_total 15`…，共 124 行 HELP/样本）；`alerts.yml` 实测 **23 条 `alert:` 规则**（与提交信息一致，`promtool check rules SUCCESS`）；**Grafana 未部署**、**9 类里「SDK 会话活跃数」无法覆盖**（SDK 未挂载）、「Headless 成功率/耗时」恒空（Headless 未挂载）→ 部分 |
| **DES-10** | 五项关键决策（三通道分工 / Headless 不依赖会话状态 / 工具粒度控制 / 外部熔断保护 / 版本锁定与降级预案） | 🟡 **部分** | 同 FR-GATEWAY-002/003/004、FR-TOOLS-003、NFR-COMPAT | — | 决策 1 **部分**（通道 1/3 挂载）；决策 2 **不成立**（Headless 未挂载，但 `scripts/research_duty.sh` 确实每次新建一次性会话）；决策 3 **未落地**（82 工具直暴露）；决策 4 **部分**（超时有，并发/预算无）；决策 5 **部分**（有案例无门禁） |

---

## 三、缺口清单（**第二轮：按影响重排**）

> 排序口径：**影响 = 「规格点名的能力整条不可用」 > 「能力在但口径/覆盖面不足」 > 「命名或文档不一致」**；
> 同档内按「是否阻塞其它条目」排。第一轮的 G1–G12 编号已重排，旧编号在「前身」列标注。

### 3.1 规格要求但未实现（真缺口，按影响排序）

| # | 影响档 | 缺什么 | 规格依据 | 影响 | 建议补齐方式 | 工作量 |
|---|---|---|---|---|---|---|
| **G1** | 🔴 整条通道缺失 | **SDK JSON-RPC 会话通道**：服务内无 SDK 客户端、`sdk` profile 的 `cordis.patch.yml` 为空、venv 无 `deepseek_harness` | FR-GATEWAY-002（spec:133-153）、DES-5.2 | 「平台前端驱动 Harness 会话」的**人工研究交互通道整条缺**（§7.1 数据流不成立）；`brain` 页 SDK 区块恒「不可用」 | 包已就位（`@deepseek-ai/dsh-sdk-jsonrpc-server`/`dsh-sdk-app` 已装、`~/.dsh/profiles/sdk` 骨架已建）→ 1 行 patch + 装 `deepseek-harness-sdk` + 写 `HarnessSessionManager` + 事件流端点 | **中**（比第一轮估的 3–5 人日小） |
| **G2** | 🔴 整条通道缺失 | **outbound 反向 MCP 桥**（把 DSH `ctx.tools` 暴露成 MCP 服务器，命名 `dsh:<toolName>`） | FR-GATEWAY-001 第三段（spec:120）、DES-10 决策 1 | 规格的「双向」只做了单向：外部 MCP 客户端**无法**调用 Harness 工具 | 装回 `@helibeiqi/dsh-cordis-universal-adapter`（本部署实测三处硬错误，见 `docs/v3-integration.md` §1.6）或自研 `ctx.tools.schemas()` → `McpServer` 行 | **中–大** |
| **G3** | 🔴 整条通道缺失 | **Headless Runner + 调度器 + 外部熔断**：服务内 0 实现；无 08:30/12:00/16:00 headless 触发；无事件/流水线节点触发；无并发上限 3、无 token 预算 200K | FR-GATEWAY-003/004（spec:158-186, 555）、DES-10 决策 2/4 | 自动唤醒大脑只剩外置 `research-duty.timer`（周一–五 19:20 单点）；FR-MON-003 恒空；§7.2 自动触发数据流不成立 | 把 `scripts/research_duty.sh` 逻辑收进服务（`HeadlessRunner` + 信号量 + 超时 kill + token 预算 + 写 `headless_log`），**并补 `v3_ops.py` 的读取端**（见 G9）；调度链补 08:30/12:00 job | **中**（服务内 runner 约 300 行 + 测试） |
| **G4** | 🟠 能力在、覆盖面严重不足 | **`FR-EXEC-003` 的 4 个子项 0 实现**：杠杆率、流动性风险、绩效归因、资金检查 | FR-EXEC-003（spec:324） | 规格的「事前/事中/事后」三段式实际是「一段半」：事中只有回撤，事后只有统计量没有归因，事前不校验可用资金 | 杠杆率从 `positions`+`equity` 推（保证金/总资产）；流动性用 `capital_flow`+`deals_history` 换手；归因按行业/因子拆解收益；资金检查读 `account_funds` 的可用资金 | **中** |
| **G5** | 🟠 能力在、覆盖面严重不足 | **六类因子缺 4 类的因子面接入**：成长（`revenue_yoy`/`net_profit_yoy`）、质量（`roe`/`roa`/`gross_margin`）已有实现但未进因子矩阵/流水线；情绪（`v3_sentiment` 已在线）与另类（`capital_flow*`/`short_interest`）是独立工具、不进因子面 | FR-STRAT-001（spec:299） | 实测矩阵只有 14 个因子，覆盖价值（7）+ 动量（3）+ 技术（1）+ 波动/流动性（3）；`basis` 只有 `mom_20` | 把 `plugins/workbench/python/quality.py` 的 5 个基本面因子接进 `factors` 工具面；情绪分作为一列并入 `factors/matrix`；另类用 `capital_flow`/`short_interest` 造横截面因子 | **中** |
| **G6** | 🟠 能力在、覆盖面严重不足 | **事件驱动 / 统计套利策略 0 实现**（`grep -rni "event_driven"` → 0；`"统计套利\|cointegration\|协整"` → 0） | FR-STRAT-002（spec:303） | 策略族只有动量基线 + ML 三模型 + 参数扫描 | 事件驱动挂 `events`/`economic_calendar_hot`；统计套利需配对协整（`correlation` 可作基础） | **大**（各 2–4 人日，含 PIT 回测口径） |
| **G7** | 🟠 能力在、覆盖面严重不足 | **参数网格只有 64 格/次**（默认 12），规格要「数千次完整回测」 | FR-STRAT-002（spec:303） | 扫参覆盖不足，`best` 易过拟合单标的；热力图信息量低 | 扩 `v3_analytics.py:940` 的 `_int_list(..., 8)` 上限 + 多标的/多策略网格 + 分片执行；或如实把规格改成「数十–数百次」 | **中** |
| **G8** | 🟠 能力在、覆盖面严重不足 | **工具发现代理缺失**：无 `list_tools`/`call_tool` 两个间接入口；**116 条工具直暴露**（schema ≈ 79,624 字符 ≈ 2 万 token） | FR-TOOLS-003（spec:265）、DES-10 决策 3 | 与规格「避免上百个工具 schema 撑爆上下文窗口」正面冲突 | 增加 `quant_discover{list_tools, call_tool}` 两个元工具，其余按域**按需**注册（`v3_mcp.py` 已有按路由造工具的机制，可复用） | **小–中** |
| **G9** | 🟡 接线/读写端缺失 | **`headless_log` 无生产者、也无读取端**（`v3_ops.py:1932/2088` 硬编码 `last: []`） | FR-MON-003（spec:339） | 表结构齐备却永远空；补了生产者也不会被读出来 | 随 G3 一起补：写 `v3_db.append_event(home,"headless_log",…)` + 在 `brain`/`gateway` 读 `v3_db.list_events(home,"headless_log")` | **小**（与 G3 合并） |
| **G10** | 🟡 接线/读写端缺失 | **`data/cache.py` 唯一 PIT 入口不存在**（`find . -name cache.py` → 0），PIT 约束散在 5 个文件 | FR-DATA-003（spec:288） | ①PIT 不可集中审计；②新读取路径不会自动继承 PIT；③按规格字面找不到验收对象 | 把 `platform/server/caches.py` 升级为 `data/cache.py` 语义（PIT 读取唯一入口）并让各模块经它取数；或在规格层把路径改成实际位置 | **中**（回归面大） |
| **G11** | 🟡 接线/读写端缺失 | **`dsh-quant-data-mcp` 未接入**（实现层 0 引用），但前端把当前工具面**冠以该名展示** | FR-DATA-002 第 5 行（spec:282-284）、FR-TOOLS-003 | A 股免密钥数据面缺一路（现由富途/AKShare 覆盖，实际影响小）；**但页面命名会让人误判已接入**（新发现 N1） | 接入该 MCP stdio server 并加进 bundles；或在规格层注销该条 **+ 修改前端卡片命名** | **小**（接入） / **极小**（只改命名） |
| **G12** | 🟡 接线/读写端缺失 | **多级权限控制**（`grep -rni "role\|rbac\|多级权限" platform/server` → 0） | NFR-SEC §4.2（spec:357） | 只有「单一 Bearer token 网关 + 页面口令闸门」，无角色/权限分级 | 在 `/api/*` 中间件加角色（只读/研究/交易）与端点白名单 | **中** |
| **G13** | ⚪ 命名/文档不一致 | **MCP 方法名契约 0/13 命中**：规格给 `mcp:<域>:<工具>`，实得裸名 + `v3_*`；Harness 侧显示为 `mcp__quantwb__<toolName>` | FR-TOOLS-001（spec:235-249）、FR-GATEWAY-001（spec:118） | 按规格字面检索会找不到任何工具；两套命名并存增加对账成本 | 规格层修订成实际命名；或在桥层加一层别名 | **小**（文档） |
| **G14** | ⚪ 命名/文档不一致 | **`AI-native` 五条规范缺 4 条**：无 `isConcurrencySafe`、无 `render` 分离、无等长 null 对齐、无 `skill/quant-research` | FR-TOOLS-002（spec:255-259）、附录 A（spec:866-869） | 工具输出是原始信封 JSON，模型侧对齐成本高；与规格「AI-native」目标不符 | 在 `mcp_tools.BoundTool` 上加 `render` 与并发安全标注；或在工作区 `v3_mcp.py` 桥层为只读路由补 `outputSchema`/`render`；建 `skills/quant-research/` | **中** |
| **G15** | ⚪ 命名/文档不一致 | **监控 9 类里 6 类无规则、2 类口径不符**；Grafana 未部署 | DES-8.3 §8.3（spec:791-801） | 无法覆盖规格点名的告警面 | 补 `Headless 成功率/耗时`、`SDK 会话数`、`MCP 延迟 >5s`、`订单延迟 >1s` 四条规则（需先有对应指标）；把 `DataSourceProbeStale` 与数据延迟 `>5min` 拆开；把 `RiskBlockedOrderAppeared` 改成「突增」（如 `increase(...[1h]) > N`） | **小**（规则） / **中**（补指标） |
| **G16** | ⚪ 命名/文档不一致 | **NFR §4.1 六项延迟无埋点、`/metrics` 无分位数** | NFR-PERF §4.1（spec:346-353） | 无法判定任何一项延迟阈值（MCP 均值 2.384 s 已超规格 2 s，但没有 P95） | 在工具面埋直方图（`_bucket`）+ 加只读压测脚本写入 P95/P99 | **小–中** |

### 3.2 规格未要求、但比预期弱（不是缺口，是欠账）

| # | 弱在哪 | 影响 | 建议 | 工作量 |
|---|---|---|---|---|
| W1 | **前端硬编码阈值**（`overview.jsx:397/406/413`、`risk.jsx:835/837`、`settings.jsx:1290`、`execution.jsx:395/400`） | 后端改 `LIMITS` 后页面仍显示旧值；`overview.jsx:708` 甚至声称「回撤阈值取自台账」（**不实**） | 改为读 `/api/v3/oms/orders` 的 `industry_limit_pct` 等字段，或新增 `GET /api/v3/risk/limits` | **小** |
| W2 | **加载/失败态渲染成 0**（`gateway.jsx:93-94→107-109`、`tools.jsx:197-200/248/286`、`research.jsx:169-181`） | 请求未回来时显示「今日调用 0 次 / 失败 0 次 / 0 ms」，与真实 0 不可区分 | 统一 `fmt.dash` 或显式 `loading`/`error` 分支 | **小** |
| W3 | **`strategy.jsx:503-506` 渲染字面量 `null`** | 回测指标缺失时卡片显示 `null%` | 传 `"—"` 或用 `formatter` | **极小** |
| W4 | **固定文案当成事实**：`settings.jsx:1314`「LIVE 大额订单需双人复核」（**无实现**）、`execution.jsx:34/1042/1066` TTL 回退 120s、`brain.jsx:252`「0 条」、`risk.jsx:686`「满刻度 30%」 | 用户会把未实现的风控/参数当既有控制 | 加「未实现/未取到」标注，或删除该文案 | **小** |
| W5 | **`/api/v3/settings` 数据源状态自相矛盾**（`v3_ops.py:1323-1325` 写死 `SEC EDGAR available:false`；`:1328-1330` 要求 `tushare` **包**可导入） | 与事实相反（实测 `source=sec/companyconcept`；Tushare 走 HTTP 不需要包） | SEC 改为按 `v3_sources` 真实探测；Tushare 判据去掉 `_module_available('tushare')` | **小** |
| W6 | **新发现 N1：前端把不存在的包当数据源名**（`market.jsx:726-727`、`overview.jsx:536` 的「dsh-quant-data-mcp（workbench 工具面）」） | 验收人会误判第 5 个开源源已接入 | 改成真实名称（如「平台工作台工具面（quantwb MCP）」） | **极小** |
| W7 | **`docs/v3-integration.md` 已过时**（称页面在 `platform/web/public/v3/`、`/` 307 跳 `/v3/index.html`、`bash platform/tools/verify_pages.sh`） | 该目录已不存在（现为 `platform/web-pro`）→ 文档把人引到错路径 | 更新 §二·五/§三/§四 | **小** |
| W8 | **杠杆/流动性事中口径**（已升为真缺口 G4）与 **建仓至今无 `blocked_industry` 实例** | 闸门在线但从未实际阻断过一笔（只阻断在「下一个订单」上） | 用一个只读构造订单过 `check_order` 打印 reasons（`platform/deploy/monitoring/verify_industry_gate.py` 已有该脚本） | **极小** |

## 四、过度实现 / 偏离清单

| # | 项 | 与规格的关系 | 证据 |
|---|---|---|---|
| O1 | **82 工具目录 + 116 MCP 工具面 / 6 域** | 规格参考 dsh-quant「59 工具·6 域」，本实现六域目录 **82** 条；MCP 面 **116** 件 | `/api/v3/tools` → `total:82`；`/metrics` → `quantwb_tools{scope="domain"} 82` 与 `{scope="mcp"} 116`；MCP `tools/list` = 116（`v3_*` 39 件）。域数一致（6）✅ |
| O2 | **V3→MCP 反向桥**（`v3_mcp.py`，把 39 条 `/api/v3/*` 路由暴露成 MCP 工具） | 规格只要「一个平台 MCP server 暴露量化工具」；本实现是**把已存在的 HTTP 面镜像成 MCP**（同一函数对象，无第二份业务逻辑） | `app.py:1032` `v3_mcp.register(app.state.mcp, app)`；**已重启生效**：`tools/list` = 116（77 + 39），只读标注 36 件；对应用户指令「所有跟平台的交互都走 mcp 接口」。adapter 已从 `quant-headless` 全部安装材料中删除 |
| O3 | **SQLite 持久化替代 PostgreSQL/Redis** | 规格 §5.1 要 PG + Redis；本实现单文件 SQLite + 文件冷备 | 用户授权（「数据库可先用 sqllite」）；`v3_db.py:119` `TABLE_SPECS`；`docs/e2e-and-data-gaps.md` 第四轮 §7 |
| O4 | **富途限流治理**（令牌桶 + 单飞 + 退避 + 冷却 + 统一 `futu/rate-limited` 信封） | 规格未要求 | `platform/server/v3_ratelimit.py`（833 行）+ `test_v3_ratelimit.py`（40 例）；`app.py:906` `is_futu_endpoint` 分流 |
| O5 | **三市场过滤 + 分市场基准 + 交易日历/节假日自动获取** | 规格未要求（只泛提「交易日历」） | `?market=SH\|HK\|US`（`app.py:984-1022`）；`v3_calendar_source.py`（785 行，链：进程内 TTL → 磁盘 → 富途 `info_trading_days` → AKShare → 人工兜底）；实测 `/api/v3/markets/calendar` → `source="platform/market_calendar"`、`holidays_loaded`、`calendar_source` |
| O6 | **研报页 + PDF/Markdown 导出** | 规格 §3.6 未列研报页 | `v3_research.py:121/160/229`（含服务端 PDF 生成）；前端 `pages/research.jsx`（第 10 页，规格 7 类里没有） |
| O7 | **ML 用 numpy 实现替代 sklearn/LightGBM** | 规格点名 Lasso/LightGBM/MLP | `v3_ml.py:1`（实现名 `numpy-lasso`/`numpy-gbdt-lite`/`numpy-mlp`），`docs/e2e-and-data-gaps.md` 第五轮如实标注 `impl`；**口径偏离但声明诚实** |
| O8 | **`source` 字段不按规格写死** | 规格备注写死 `source=futu/quote_history_kline` | 实现上报**真实来源**（`_dominant_source`，逐标的 `sources` 另附）；文档已登记此有意偏离 |
| O9 | **外置 `research-duty.timer` 承担 Headless 唤醒** | 规格要平台调度器内实现 | `install/research-duty.timer`（`Mon..Fri 19:20`，实测已安装且 active）；服务内通道仍 `unavailable` → **能力达成但归属与规格不同** |
| O10 | **既有 WP6–WP26 能力原样保留**（工作台 82 端点、审计链、OMS、L3 队列…） | 规格是「完全重构」，实现选择**复用既有工具面同一 handle** | `app.py` 注释「与既有 `/api/wb/*` 共存…不新造第二事实源」——**偏离「重构」字面、保留能力**，另有 docs 登记 |
| O11 | **指标名与规格不同** | 规格点名 Prometheus+Grafana 与 9 类指标 | 实际指标族前缀 `quantwb_*`（自研 `observability.py`），Grafana 未部署；`alerts.yml` 23 条规则覆盖规格 9 类中的 7 类 |

---

## 五、诚实性核查（前端逐页「每个数字是否有真实来源」）

抽查 10 页（覆盖用户点名的 5–8 页要求）：`overview / brain / market / strategy / risk / execution /
gateway / tools / settings / research`。结论：**未发现「示例数据」字样，未发现编造的业务数字**
（如假的行情、假的收益），但发现 **12 处「固定文案/回退值被当作真实数据展示」**，逐条点名如下。

### 5.1 已点名问题（文件:行 + 为什么会被误读）

| # | 位置 | 内容 | 为什么会被误读 |
|---|---|---|---|
| H1 | `platform/web-pro/src/pages/gateway.jsx:93-94` → `:107-109` | `Number(metrics.mcp?.calls ?? 0)`、`errors ?? 0`、`Number(metrics.mcp?.avgMs ?? 0)`；MCP 卡在 `:215` **无条件渲染**（只判 `gateway.loading`） | `/api/v3/metrics` 加载中或失败时，卡片显示「今日调用 0 次 · 失败 0 次 · 平均延迟 0 ms」——与「真的 0」不可区分 |
| H2 | `pages/tools.jsx:197-200` | `value={… : undefined}` / `tools.loading ? undefined : localCount` | **实测 antd 行为**：`Statistic` 默认 `value = 0`（`node_modules/antd/es/statistic/Statistic.js:25`），`undefined` 渲染成 **`0`** → 加载中「工具域/一级/直通/今日调用」四项全 0 |
| H3 | `pages/tools.jsx:248`→`:259`、`:286` | `Number(callsByTool[tool?.name] ?? 0)` 渲染成「今日 0」 | metrics 失败时 `callsByTool={}`，每个域/工具都显示「今日 0」 |
| H4 | `pages/strategy.jsx:503-506` | `value={fin(metrics.X) ? Number(metrics.X) : null}` | **实测 antd 行为**：`Number.js` 走 `String(value)` → `null` 渲染成字面量 **`null`**（带 `suffix="%"` 即 `null%`） |
| H5 | `pages/overview.jsx:397 / :406 / :413`（渲染 `:727`「上限 {row.limit}%」） | 硬编码 `limit: 2 / 20 / 15` | 以「数据」形态展示风控上限；后端真值在 `v3_ops.py:94`，且 `/api/v3/oms/orders` 已返回 `industry_limit_pct`——页面**忽略接口值** |
| H6 | `pages/overview.jsx:708` | 文案「回撤阈值取自台账，为台账口径」 | **与代码不符**：15 是 `:413` 的常量，不是台账读数（不实声明） |
| H7 | `pages/overview.jsx:882` | `(order.risk?.reasons) \|\| ["阈值内"]` | `risk.reasons` 缺失时断言「阈值内」= 凭空给出风控结论 |
| H8 | `pages/risk.jsx:835 / :837` | `threshold: "≤ 权益 2%（OMS check_order 口径）"` + `singleRatio > 2` 决定「正常/超限」标签 | 判定用的 2% 是前端字面量，非接口阈值（同页 `:432` 反而诚实写「未从台账回读到阈值」） |
| H9 | `pages/risk.jsx:686` | 「满刻度 30%」 | `:706` 的 BarList **不传 max**（自动缩放）→ 图上满刻度不是 30% |
| H10 | `pages/execution.jsx:34` → `:1042` → `:1066` | `FALLBACK_TTL_SECONDS = 120` → 「TTL 120 秒」 | `confirmation.ttl_ms` 缺失时把前端默认值当**服务端 TTL** 展示 |
| H11 | `pages/settings.jsx:1314` | 「LIVE 大额订单需双人复核后执行（阈值由风控引擎统一下发）」 | `platform/server`、`scripts` 全域**无「双人复核」实现** → 用户会以为存在该控制 |
| H12 | `pages/settings.jsx:1290`；`pages/execution.jsx:395/400` | 「单笔 ≤ 2% · 行业 ≤ 20% · 回撤 ≤ 15%（v3_ops.LIMITS）」 | 同为前端硬编码阈值（H5 同族） |

补充（非数字、但属文本改写）：
- `pages/tools.jsx:73-81` 用**码位构造** `U+793A U+4F8B`（「示例」）并把上游工具说明里的该词替换成「**样例**」（`:359` 也作用于探测返回文本）。源码注释自述是「页面文本审计要求页面不出现该类字样」。
  - 事实核查：**页面确实不出现「示例数据」**；但页面会出现同义字「样例」，且**工具说明文本不再逐字等于上游返回**。这与 `docs/v3-integration.md` §五「不用占位数据」的精神接近、与「逐字保留」不一致，**建议改为对上游原文加引号并标注来源，而不是替换词**。
- 未发现 `mock`/`demo`/`假数据`/`TBD` 等字样；`dataDomain.jsx:50/77/120/145` 的 `600519`/`AAPL` 是**按需查询的真实标的默认值**（非展示指标），`settings.jsx:200` 的 `exec_window_minutes` 默认 `30` 会**回写**（`:585`）——属「默认配置」而非「编造数据」，但建议标注「默认值」。

### 5.2 后端诚实性问题（同属「数字来源」问题，按题面要求一并点名）

| # | 位置 | 内容 | 证据 |
|---|---|---|---|
| B1 | `platform/server/v3_ops.py:1323-1325` | 写死 `{"name":"SEC EDGAR…","available":False,"detail":"无数据源：本服务未实现 SEC EDGAR 客户端…"}` | 与事实矛盾：实测 `GET /api/v3/financials?ticker=AAPL&statement=income&periods=2` → `{"ok":true,"source":"sec/companyconcept(us-gaap XBRL)","chain":[{"source":"sec/companyconcept(us-gaap XBRL)","ok":true,"ms":5793}]}`；`docs/v3-integration.md` §一也写 SEC EDGAR ✅ 可用 → **文档/接口/实现三方矛盾** |
| B2 | `platform/server/v3_ops.py:1328-1330` | Tushare 可用性 = `token and _module_available("tushare")` | 实现在 `v3_sources.py:94` 走 **HTTP** `http://api.tushare.pro`，**不需要包**；若只注 token 而包不在，页面会误报「不可用」 |

### 5.3 诚实性做得好的地方（反向证据，避免只列缺点）

- `market.jsx` **干净**：缺失字段一律显式「无数据源」（`:538/:572/:584/:596/:963-965/:1213`），每块带 `source + as_of`；`plates` 接口自注「板块涨跌幅需富途实时行情权限；无权限时只有板块清单，不填占位」（实测 `/api/v3/plates` 返回体 `note` 原文）。
- `brain.jsx:989`、`overview.jsx:803` 明确写「不用设计稿里的占位 session / 轮次 / token 顶替」；`risk.jsx`/`execution.jsx`/`strategy.jsx` 各有「无数据源项（逐项写明原因，不填占位数字）」专卡。
- 所有图表序列均派生自接口状态（已逐一核对调用点：`strategy.jsx:441/508`、`gateway.jsx:442`、`tools.jsx:447`、`brain.jsx:705`、`risk.jsx:157/706/770`、`overview.jsx:220/818`、`market.jsx:870`），**无静态图表数组**。
- 写入口（`strategy/run`、`ml/backtest`、`oms/sync`、`plan-execute`、`confirm-decide`、`switch-mode`、`credentials`）**全部在点击处理器内**，加载/刷新路径只发读请求。

### 5.4 页面 ↔ 端点对应（本次实测覆盖）

| 页面 | 端点 |
|---|---|
| `overview` | `/api/v3/overview?market`、`/oms/orders`、`/strategy`、`/metrics`、`/brain`、`/settings`、`/audit?window=120` |
| `brain` | `/api/v3/brain`、`/strategy`、`/factors/matrix`、`/overview`、`/metrics`、`/audit`、`/news` |
| `market` | `/api/v3/market`、`/market/watchlist`、`/factors/matrix`、`/plates`、`/orderbook`、`/markets/calendar`、`/metrics`、`/brain`、`/overview`、`/news`、`/financials`、`/tushare`、`/openbb`、`/spot` |
| `strategy` | `/api/v3/strategy`、`/factors/matrix`、`/ml/sweep`；`POST /ml/backtest`、`POST /strategy/run`（点击） |
| `risk` | `/api/v3/risk/analytics`、`/risk`、`/risk/industry`、`/oms/orders`、`/execution`、`/audit`、`/events` |
| `execution` | `/api/v3/execution`、`/oms/orders`、`/metrics`、`/audit`、`/risk`、`/execution/quality`、`/risk/industry`；`POST /oms/sync`、`/api/wb/plan-execute`、`/api/wb/confirm-decide`（点击） |
| `gateway` | `/api/v3/gateway`、`/metrics` |
| `tools` | `/api/v3/tools`、`/metrics`、`/settings`、`/gateway`；`POST /api/wb/series`（点击探测） |
| `settings` | `/api/v3/settings`、`/metrics`、`/overview`、`/oms/orders`、`/audit`、`/credentials`、`/sources/status`；写：`POST /api/v3/credentials`、`/api/wb/{switch-mode,openapi_config,openapi_test,openapi_oauth,auto_pipeline}`（点击） |
| `research` | `/api/v3/research`、`/research/tasks`、`/research/report.pdf?id=` |

---

## 六、与既有文档的口径对齐与矛盾

**口径一致（不重复造结论）**：
- `docs/e2e-and-data-gaps.md`：A 股实时权限、Tushare 未注入、AKShare 全市场快照上游断连、`info_search` 载荷不完整 —— 本轮实测**复现一致**（`-9` / `tushare/no-token` / `RemoteDisconnected`）。
- 该文档第六轮「行业集中度红线已解决，但 `check_order` 的 `industry_pct` 仍恒 0.0（观测先行，闸门未接）」——本轮**逐字复现**（实测 `industry_source="no-data…按 0% 不阻断"`），并补充：**工作区已接闸门、待重启**（新事实）。
- `docs/v3-integration.md` §五「取不到就报错误信封、页面显示无数据源 + 原因」——前端抽查**基本成立**（market 页最佳），但有 §五.1 的 12 处回退值例外。

**矛盾（按题面要求点名）**：
1. `docs/v3-integration.md` §一 写 **SEC EDGAR ✅ 可用**；`platform/server/v3_ops.py:1323-1325`（并被 `/api/v3/settings` 直出到「接入与授权」页）写 **❌ 无数据源/本服务未实现**。**实测支持前者**（`source=sec/companyconcept(us-gaap XBRL)`）→ **接口状态字段与实现矛盾，需修 `v3_ops`**。
2. `docs/v3-integration.md` §三/§四 写前端在 `platform/web/public/v3/`、`/` 307 跳 `/v3/index.html`、逐页脚本 `verify_pages.sh`；**仓库中 `platform/web/` 不存在**（`ls` → No such file），现为 `platform/web-pro`（AntD Pro，根路径、hash 路由）。→ **文档过时**。
3. `docs/v3-integration.md` §一 表格写「工作台工具面（本服务 **56** 工具）」；`docs/architecture.md` 写 **82 端点 / 77 MCP 工具**；本轮 `/metrics` 与 `/api/v3/tools` 实测 **82**。→ 三处数字不一致，最新真值 **82**。
4. `AGENTS.md`「二、常驻纪律」写 `cd platform/web && npm test`；`platform/web` 不存在 → 测试命令应指向 `platform/web-pro`（现 `package.json` 在其下）。

---

## 七、本次未能验证的条目与原因（**第一轮归档；第二轮收敛见文首 §2.4**）

> 第二轮已收敛 1 条（`FR-DATA-001` 条件单 → 契约层已实现），新增 1 条实测反例（MCP 均值延迟 2.384 s > 规格 2 s）；
> 其余仍无法验证。

| 条目 | 为什么没验证 |
|---|---|
| **NFR-PERF §4.1**（6 项延迟/可用性） | 只读审计不做压测；且 A 股实时权限缺失使「行情延迟」在当前权限下不可测。`/metrics` 只有进程内均值（`quantwb_mcp_call_duration_seconds 1.592`），**无 P95**，不足以判定阈值 |
| **FR-DATA-001 的「条件单」** | 需要在真实通道下单才能验证；本轮**禁止调用任何写/交易端点**。代码侧 `grep 条件单` 0 命中 → 仅能说「未见实现」，不能说「一定没有」 |
| **FR-EXEC-001 的 live 全生命周期** | live 写协议自述「未经真实 live 下单验证」（`docs/P4-live-trading.md`），且本轮禁止交易调用 |
| **FR-GATEWAY-004 的事件触发 / 费用预算** | 无配置面可读（`GET /api/v3/gateway` 只给 `scheduler.rules=[]` 与心跳）；`headless.breaker=null` |
| **工作区未提交改动的最终形态**（`v3_nlp`/`v3_risk_gate`/`v3_mcp`、`app.py`/`v3_ops.py` 改动） | 审计期间并行 agent 仍在改（18:38→18:52 快照内多次变化）；且**运行中的 8397 进程早于这些改动**，故「工作区已实现」与「线上可用」必须分开陈述 |
| **`test_*` 是否当前全绿** | 未在本轮重跑测试套件（避免写临时文件与干扰并行 agent）；测试名按题面「证据 = 测试用例名」引用，**未断言其通过**（`docs/e2e-and-data-gaps.md` 第六轮 §七记录 `discover -s tests` = 527 OK，为历史基线） |
| **`/mcp` 的 82 工具 schema 细节** | 未做 MCP 握手（`initialize`+`tools/list`）以省配额；工具数以网关自报 + `/metrics` 为准 |

---

## 八、一页结论（**第一轮归档；当前口径见文首 §2.1 与 §2.6**）

> 第二轮修正：对齐 5→**3**、部分 21→**25**、缺失 2→**1**、不适用 2→**1**、未验证 1→1；
> 4 条状态变化（`FR-GATEWAY-001` 部分、`FR-TOOLS-001` 部分、`FR-TOOLS-002` 部分、`FR-EXEC-003` 部分）
> 与 2 条证据级更正（`FR-STRAT-003` 已上线、`FR-EXEC-002` 行业闸门已在线）。
> 下为第一轮原文。

1. **「其他能力」整体对齐度：可评 28 条中 26 条对齐或部分对齐（92.9%），完全缺失仅 2 条**——
   `FR-GATEWAY-002`（SDK JSON-RPC 会话通道）与 `FR-TOOLS-002`（AI-native 工具注册规范）。
2. **最大结构性缺口是「大脑三通道只通了 1 条」**：MCP 通（82 工具），SDK 与 Headless 均 `unavailable`
   （实测接口自报原因）。这直接连带 `FR-MON-002` 的 Harness 会话状态、`FR-MON-003` 的 headless 日志、
   `DES-5.2`、`DES-10` 决策 1/2/4 一起降为「部分」。
3. **本轮三件点名事项**：红线闸门**已接但未上线**（待重启）、自研 NLP**已写但未挂载**（待提交重启）、
   A 股实时权限**按已知限制处理**（页面如实标注、不填占位）。
4. **诚实性**：无「示例数据」字样、无编造业务数字；但有 **12 处回退值/固定文案会被读成真实数据**
   （H1–H12，重点：`gateway.jsx:93-94`、`tools.jsx:197-200`、`strategy.jsx:503-506`、
   `overview.jsx:397/406/413/+708`、`settings.jsx:1314`），以及 2 处**后端状态字段与事实相反**
   （`v3_ops.py:1323-1325` SEC、`:1328-1330` Tushare）。建议按 §三 3.2 的 W1–W5 一次性收敛（约 2 人日）。
