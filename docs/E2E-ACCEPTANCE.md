# E2E 验收工具链与本轮结果

> 本文是**可重复运行的端到端验收**的唯一入口文档：两个 harness 的用途/覆盖/用法/退出码、
> 本轮实测结果与缺陷清单、以及「修复落地后怎么判闭环」。
> 相关：服务启停见 [RUNBOOK.md](RUNBOOK.md) 的「服务启停（推荐方式）」；
> 端点/工具面契约见 [architecture.md](architecture.md)；上游数据坑见 [TOOL-LIMITS.md](TOOL-LIMITS.md)。

| 工具 | 层次 | 入口 | 产物 |
|---|---|---|---|
| 后端端点全量扫描 | 服务（HTTP + SQLite + 文件系统） | `~/.dsh/trading-venv/bin/python scripts/e2e_workbench.py` | `~/.dsh/logs/e2e-workbench-<ts>.json` |
| 前端真浏览器 E2E | 浏览器（系统 Chromium + CDP，零依赖） | `node scripts/e2e_web.mjs` | `~/.dsh/logs/e2e-web-<ts>/{report.json,*.png}` |
| 页面字段级审计 | 端点载荷 × 浏览器渲染文本（零依赖） | `node scripts/audit_page_fields.mjs` | `~/.dsh/logs/page-fields-<ts>/report.json` |

三个 harness 都是**只报告、不修复**：发现缺陷后由人工/代理按 findings 修服务或页面，
再用同一命令复跑判闭环（判据见 §五）。

---

## 一、后端端点全量扫描 `scripts/e2e_workbench.py`

### 覆盖

| 阶段 | 内容 |
|---|---|
| 0 状态快照 | 库表行数 + **全部 kv 键** + 在途单分布 + kill 文件/pending 指令/配置文件指纹 + mode |
| 1 读端点 | 声明端点里的读类，逐项最小**合法**载荷（输入必填端点给真 tickers） |
| 1b section | `f10_detail` 26 项 + `derivative_detail` 4 项（含必填参数版与缺参版） |
| 1c 契约 | 输入必填端点的**空载荷**必须被如实拒绝（`invalid-operation`） |
| 2 写/动作 | kill→断言规则 1 拒单→unkill、`switch-mode` 无口令被拒 + sim→sim 幂等、`auto_pipeline` 写→读回→**按字节复原**、`plan-execute` 假 hash、**拒单契约探针**、**下单生命周期探针**、`confirm-decide`/`rules-decide`/`research-tasks-*` 拒绝路径、push 订阅往返、`modify_user_security` 非法 op |
| 3 一致性 | 声明端点 vs 实际可达、`pipeline` 阶段 vs kv ran 标记、空关注池时的阶段状态、plan/schedule/reconcile/audit 与库表 |
| 4 状态复原 | 三类判定（见下） |

**交易写端点的修后契约**（HEAD `f86783f` 起；`scripts/e2e_workbench.py` 按此断言）：

| 情形 | 契约 |
|---|---|
| 券商/上游**拒单** | `ok:false` + `error.code="trading/order-rejected"`，message 含上游原因与 `[errcode=…]` |
| 提交成功 | `ok:true` + `value.status="submitted"` |
| 撤单成功 | `ok:true` + `value.status="cancelled"`，且 **OMS 回写 `cancelled`**（S2） |
| 改单（撤旧重下） | 新单 `submitted`，**旧单 OMS 行落 `cancelled`**（M1），且不被自身在途查重阻塞 |
| 超时/传输不确定 | `unknown`（先查不重放，语义不变） |

**下单生命周期探针**（S2/M1 的 E2E 验收证据，按上述契约断言）：

1. `place` → 断言 `ok:true` + `submitted` + 有券商订单号；
2. `cancel` → 断言 `ok:true` + `cancelled`，并在 25s 内看到 OMS **不再在途**（回写生效）；
3. 同标的同方向**再次 `place`** → 断言不再被在途查重阻塞（S2 的直接证据）；
4. `place` → `modify`（改价）→ 断言新单 `submitted`、旧单 OMS 行落 `cancelled`（M1）；
5. 收尾：撤掉改单后的新单（**改单会换券商订单号**，用 modify 返回的新 id）。

**契约前置门（防止探针自己制造脏状态）**：生命周期探针**仅当拒单契约已生效**时才跑
（拒单探针先跑，须看到 `ok:false` + `order-rejected`）。理由是旧代码的撤单不回写 OMS
（S2）——在旧服务上跑生命周期探针会**每跑一次留一笔幽灵在途单**，让后续运行越来越不可
验证。契约未生效时脚本打印并跳过，同时给出「重启服务加载 HEAD 修复」的指引。

**陈旧服务信号**：若拒单探针仍看到 `ok:true`+`rejected`（旧语义）或 analytics 空载荷仍回
`analytics-unavailable`，脚本在汇总前打印
`⚠ 契约未生效：服务可能运行旧代码 → 请重启服务加载 HEAD 修复后复跑`，并在 JSON 里置
`stale_service_contract: true`。

**幽灵在途单（已知状态）**：闸门的 OMS 在途查重按 `(symbol, side)` 阻塞，因此**修复前**
（撤单不回写 OMS）留下的在途行会永久阻塞同标的同方向新单。脚本的处理是：
①阶段 2 开头检测并明确报告；②下单探针**自动换用未被阻塞的候选标的**
（`ORDER_CANDIDATES`：三只低价 A 股）；③收尾撤单已发出而 OMS 未回写时，把「新增在途单」
归因到 S2（条件性登记）而非判成 harness 缺陷。**不要手改库**：这两行是 S2 缺陷的历史后果，
S2 修复生效后新撤单都会回写；历史遗留行需由平台属主按 OMS 维护流程处理。

### 用法与退出码

```bash
# 只读巡检（并行任务占用同一服务、或 CI 里只跑读侧时用；不产生任何写动作）
~/.dsh/trading-venv/bin/python scripts/e2e_workbench.py --read-only

# 完整（含可逆写检查）：默认
~/.dsh/trading-venv/bin/python scripts/e2e_workbench.py

# 其它开关：--base URL（默认 http://127.0.0.1:8397）｜--timeout S｜--out DIR
```

- **冷启动暖机**：刚重启过服务**不用加参数**——脚本自带冷启动暖机（约 10 秒属正常）。
  跳过它就加 `--no-warmup`，但**脚本类端点会因此误报**。

- **只做 sim；绝不切 live**：启动时若 `mode != sim` 直接退出 2；`switch-mode` 只走
  「无口令被拒」与 sim→sim 幂等两条路径。
- **退出码**：`0` 无阻断发现且无真泄漏；`1` 有阻断发现（read/section/contract 的 error、
  或 blocking findings）或真泄漏；`2` 前置不满足（服务不可达 / 非 sim）。
- **可重复运行**：写动作全部可逆且结束复原（kill 删除、配置按字节还原、探针单撤净）。
  判定不依赖上一次运行。

### 状态变更三类判定（阶段 4）

首版把「kv 4→5」「sqlite_sequence 1→2」计成 critical，但那些主要是**服务自身活动**
（调度器每 tick 写 `daemon:state`、推送对账写 kv）与**探针审计行**——每轮都报 critical 的
判定等于没有判定。现按三类分流（判据写在脚本常量注释里）：

| 类 | 例子 | 处理 |
|---|---|---|
| ① 服务自身活动 | `daemon:*`/`push:*`/`reconcile:*`/`last_reject` 等 kv 键、`kv` 表行数、`sqlite_sequence` | **忽略，列出** |
| ② 审计/记录行 | `risk_checks`/`alerts`/`fills` 新增行、`orders`/`plans` 历史行 | **允许，计数并列出**（不影响退出码） |
| ③ 真泄漏 | `pending/` 指令残留、kill 文件残留、配置被改写、**本次新增**在途单、未知 kv 键/未知表行数变化 | **critical → 非零退出** |

### 本轮加固的自身弱点（都是首版的假阳性来源）

| # | 首版问题 | 修法 |
|---|---|---|
| 1 | 状态泄漏误报（kv/sqlite_sequence 计 critical） | 三类判定 + kv 键级快照 + 在途单「与扫描前对比」 |
| 2 | `correlation`/`factors`/`ic` 空载荷探针被计成「读端点未预期失败」 | happy path 给**合法最小 tickers**；空载荷另作**契约断言**（期望 `invalid-operation`，过渡期容忍 `analytics-unavailable` 但记「过渡码」） |
| 3 | 期望拒绝的写探针被计成 error（`error=7` 全假） | `record(..., expected_code=…)`：命中预期拒绝码记 ok |
| 4 | `option_screen` 探针缺 `field_filter`（上游只回 4 个默认字段） | 补足非空 `field_filter` + `strategy`（TOOL-LIMITS 实测陷阱） |
| 5 | 下单探针价写死 `3.29`（约为收盘价一半 → 越出 ±10% 价格带 → 上游拒单） | 锚定价 ±10% **价格网格**逐档试；全部被拒记「探针不可判定」并提示更新锚定价（**不误报成平台缺陷**） |
| 6 | `derivative_detail.future_info`/`modify_user_security` 的错分类被当阻断缺陷 | 按修后语义判定；服务端仍在过渡期时记**条件性/过渡**发现（不阻断） |
| 7 | 汇总的 `read_*` 把写阶段也算进去 | 改为**阶段感知**统计（read/section/contract/write/consistency 各自计数） |
| 8 | 扫描前遗留的幽灵在途单会污染 kill 与下单探针 | 阶段 2 开头检测 `in-flight`：存在则**明确指出并跳过**下单/撤单探针（根因指向 S2），不产出「合法单被拒」的假缺陷 |
| 9 | `pipeline` 阶段 vs kv ran 标记比对把**表驱动派生阶段**（`plan`/`execute`，由 plans/orders 表事实得出、没有同名作业）也算进去 | 比对只覆盖**作业阶段**；派生阶段排除（2026-09-17 WP16 实机：手工 `plan-auto` 生成计划后，旧口径误报「没跑却显示完成」并计阻断） |
| 10 | `trade_modify` 探针缺 `side` → 被闸门以 `invalid-operation` 拒（write 恒 error=1） | 探针补 `side=BUY`；改单真正走「撤旧重下」，收尾按**新券商单号**撤单 |

---

## 二、前端真浏览器 E2E `scripts/e2e_web.mjs`

### 覆盖

16 条路由（overview/market/capital/options/signal/portfolio/risk/factors/execution/
research/events/plan/pipeline/schedule/audit/settings），每条做一次**完整加载**
（路由间先 `about:blank`，否则同文档导航不触发 `loadEventFired`），采两次 DOM：

- `domEarly`：`loadEventFired` 之后立刻采（**加载窗口**，观察加载态）；
- `domFinal`：**网络空闲 + DOM 稳定**后采（**稳态**，锚点断言以此为准）。
  「网络空闲」按 `Network.requestWillBeSent/loadingFinished/loadingFailed` 的**在途计数**
  判定（不是 performance 资源条目——后者只在请求完成时产生，会把「仍在飞」误判为安静）；
  稳态还需 DOM 连续 3 次采样不变（SPA 有依赖式二次请求波）。

采集项：console error/warning、未捕获异常、HTTP 4xx/5xx、请求加载失败、白屏、
React 崩溃文案、内容区必需锚点、API 请求清单、截图（部分路由）。

### 用法与退出码

```bash
node scripts/e2e_web.mjs                        # 全量 16 路由
node scripts/e2e_web.mjs --routes plan,pipeline # 单页复验（未知键报错并列出可用键）
node scripts/e2e_web.mjs --no-screenshot        # CI 友好（只留 report.json）
node scripts/e2e_web.mjs --fail-on-soft         # 让软断言也计入退出码（判闭环用）
node scripts/e2e_web.mjs --help
```

- **等待预算**：`--route-budget S` 单路由等待预算（秒，默认 90；超预算记缺陷但继续跑其余
  路由）、`--health-timeout S` 健康门等待上限（秒，默认 60；服务未就绪则不跑页面扫描）。

- **退出码**：`0` 无缺陷（阻断/严重/重要）且（未开 `--fail-on-soft` 或无软断言失败）；
  `1` 有缺陷、运行中断、或开了 `--fail-on-soft` 且软断言失败。
- **软断言**（`report.softAssertions`）：**报告但不计退出码**——因为对应缺陷**已知在修**，
  让 CI 常年变红会让门失效。当前一条：`loading-window-factual-claims`
  （加载窗口内不得出现**事实口吻的空态**文案，如「暂无持仓」「无心跳记录」「今日无该链阶段」）。
  修复落地后用 `--fail-on-soft` 复跑，**本条不再出现**即闭环（本轮已达成 0，修复 `bf36057`）。
- **空态断言 vs 失败断言**（2026-09-17 收窄）：失败类文案（`读取失败：<详情>`）**必须**与失败
  请求关联——有失败请求（4xx/5xx/加载失败）时如实展示失败属**诚实**行为，只记观察项；
  **没有**失败请求却显示失败才算说谎（记软断言）。裸串 `读取失败` 曾把 portfolio 页的
  **说明文案**（「实时读取失败时展示缓存并标注 stale」）误判成加载态说谎——那是探针过松，
  已按此口径收敛（详见 §四）。
- **锚点是状态感知的**：`required` 条目可以是字符串（必须出现）或数组（数据态/空态任一
  满足）。例：`plan` = `["当前计划","暂无计划"]`（空库时前者不会渲染，是**正确**状态）；
  `settings` 无页面级标题，以卡片标题为锚（`["富途 OpenAPI 凭据","自动流水线"]`）。
  报告里记录命中的是哪一支（`data`/`empty`）。
- **只读**：不点击任何提交类控件、不改服务状态、不装依赖。

---

## 三、页面字段级审计 `scripts/audit_page_fields.mjs`

### 与 §二 的分工

`e2e_web.mjs` 问「**页面有没有渲染出来**」（白屏/锚点/console 错误/加载态说谎）；
本节这个工具问「**每个字段显示得对不对**」——同一个端点值在页面上的呈现是否与格式契约一致。
两者互补，都要跑。

### 覆盖与判定

16 条路由全覆盖（overview/market/capital/options/signal/portfolio/risk/factors/execution/
research/events/plan/pipeline/schedule/audit/settings）。每页做两件事：

1. **后端事实**：直接 `POST /api/wb/<endpoint>`（与浏览器同一入口）拿到真实载荷；
2. **页面事实**：真 Chromium（CDP）加载 `http://127.0.0.1:8397/#/<route>`，抓 antd
   Statistic / Descriptions / 表格单元格的**标签 → 渲染文本**（含 `—`），以及内容区全文
   （**排除**「原始返回」折叠块——那是 JSON 逃生口，不该按展示文本判）。

然后按格式契约逐字段比对，判定码：

| 判定 | 含义 | 计入缺陷 |
|---|---|---|
| `MISSING_WHEN_DATA` | 后端有非空值，渲染全是 `—` | ✅ 严重 |
| `FORMAT` | 两端都有值但对不上（无千分位 / 比例没 ×100 / 时间戳带 `T` / 原始毫秒时间戳） | ✅ 重要 |
| `FABRICATED` | 后端缺失/为空，渲染出 `0`/`0.00`/`NaN`/`[object Object]` | ✅ 重要 |
| `T_TIMESTAMP` | 内容区出现带 `T` 的 ISO 时间戳（全文扫雷） | ✅ 重要 |
| `EMPTY` | 两端都空 | ❌ **数据确实没有时显示 `—` 是正确的** |
| `UNJUDGED` | 端点取数失败（无权限/无凭据/参数不被接受） | ❌ 但要点名端点错误原文 |
| `NOT_RENDERED` | 标签在 DOM 里没出现（条件渲染/需先输入标的） | ❌ 观察项 |

**毒值优先于「部分命中」**：同一列里既有对得上的值、又有 `[object Object]`/`NaN` 时，先判 ok
会把真缺陷盖掉（2026-09-19 实测：审计页「券商」列同时是 `200, 1,000, [object Object]`），
所以 `FABRICATED` 的毒值判据在 `hits` 短路上**之前**执行。期望文本比对前按渲染口径做空白归一
（HTML 折叠连续空白），这是对齐渲染事实，不是放宽数值/千分位/百分号判据。

### 交互卡片取证（phases，2026-09-19 新增）

有些字段「数据存在但默认视图里没有」——必须先在页面上**操作**才出来。这类区块用页面规格里的
`phases` 二次取证：每个 phase 自带端点表、动作与字段，动作**只点只读查询控件**（筛选按钮、
查询按钮、标的输入框），不碰任何提交/下单/执行/模式类控件。已覆盖：

| 页面 | phase | 动作 | 新增字段 |
|---|---|---|---|
| options | `screen` | 在「期权筛选（option_screen）」卡片里按下「筛选」 | 结果表代码 / 成交量 / 持仓量 |
| options | `derivative` | 往「期权合约代码」输入框填 `--option-code` 并按下「查询」 | 平均隐含波动率 / IV 状态 / 标的价格 |
| factors | `quality` | 重新导航后只填**1 只**标的（ic 需要 3..8 只，同一输入框喂不出两种标的数） | 财报卡 12 项（报告期/财年/币种/会计准则/期间数/营收/毛利/净利/毛利率/净利率/营收同比/净利同比） |

动作失败（按钮没命中、合约代码挑不到）时该 phase 的字段一律 `UNJUDGED` 并在日志里带出原因——
**「点了但没反应」不会被当成通过**（衍生品 phase 还会校验两张子卡真的出现了键值行）。

非表格结构也纳入了探针：事件页的时间线按 `.ant-timeline-item` 抓「整项全文 + Tag 文本」，
期权衍生品卡的键值行按「次要色标签 + `.ant-space-item` 兄弟值」配对（`key=` 那种行内片段排除）。

判定的期望文本由工具**独立实现**（`CONTRACTS`），再由 `tests/audit-page-fields.test.mjs`
与 `services/formatCore.js` 的真实实现逐值对齐——既不共用一份代码（否则实现有 bug 会被
工具原样祝福），也不允许两处漂移。

### 用法与退出码

```bash
node scripts/audit_page_fields.mjs                          # 全量 16 路由
node scripts/audit_page_fields.mjs --pages capital,market    # 单页/多页复验（未知键报错并列可用键）
node scripts/audit_page_fields.mjs --pages options --symbol HK.09961   # 标的类页面指定标的
node scripts/audit_page_fields.mjs --pages options --option-code US.SPY260918C760000
node scripts/audit_page_fields.mjs --json --verbose          # 额外打印报告路径 / 逐字段明细
node scripts/audit_page_fields.mjs --help
```

- **标的**：默认取关注池第一只（与页面候选同源）。期权页与行情页都声明了 `preferMarket: "HK"`
  ——实测 `option_chain` 对 A 股标的**直接拒绝**（`option chain only supports HK / US / JP
  markets`）、A 股 `rt_quote` 恒 `errcode=-9 realtime quote permission required`，用 A 股标的
  跑这两页只会得到 UNJUDGED（`--symbol` 仍可覆盖）。事件页另有 `symbolResolver`：从关注池里
  挑第一只**真有事件**的标的（默认标的 SH.600000 只有 1 条，覆盖不到多条时间线的形态）。
- **`--option-code`**：衍生品卡要的是**期权合约代码**（关注池与持仓都不产出它）。缺省时工具
  自己调 `option_screen` 取真机结果首条的 `code`；取不到就把该 phase 的字段判 `UNJUDGED`
  并写明原因，**不会编一个看起来对的合约代码**。
- **退出码**：`0` 无缺陷；`1` 发现缺陷；`2` 服务未就绪或运行中断。
- **等待预算**：`AUDIT_ROUTE_BUDGET_MS`（单路由预算，默认 90s）、`AUDIT_MAX_WAIT_MS`
  （网络空闲上限）、`AUDIT_BASE` / `AUDIT_CHROME` 覆盖地址与浏览器。
- **产物**：`~/.dsh/logs/page-fields-<ts>/report.json`——逐字段明细
  （`page/label/where/endpoint/path/kind/backend/rendered/judge/note`）、每个 phase 的
  动作结论与探针计数、全文扫雷结果、每页抓到的表格清单（诊断「这个值取自哪张表」）与端点错误原文。
- **只读**：在输入框里输入并回车/点只读查询按钮（都是页面的正常查询动作），
  不点击任何提交类控件、不改服务状态、不装依赖。

---

### 空态字段怎么变成可判定字段（数据生成，2026-09-19）

上一轮有 30 项因**没数据**只判到 `EMPTY`/`NOT_RENDERED`（等于没验证格式）。本轮先按平台
自己的写入路径把数据造出来，再复验。**全部只用平台自己的工具/存储 API，不手写 JSON、不切模式、
不改 `trading-platform.json`、不清库。**

| 数据 | 生成命令（DSH_HOME 默认 ~/.dsh） | 生成前 | 生成后 |
|---|---|---|---|
| 工作台信号预览 | `node scripts/workbench_admin.mjs seed-preview --ticker SH.600000 --strategy rsi`（×3：SH.600000 / HK.09961 / US.NVDA） | `snapshot.previews = 0` | `= 3` |
| 研究运行 + 已发布研报 | `node scripts/workbench_admin.mjs seed-research --ticker SH.600000 --rating Hold --report-file <md> --sources-file <json>` | runs/reports 均 0 | runs 1 / reports 1（来源 2 条） |
| 券商成交回填（fills） | `PYTHONPATH=plugins/core/python ~/.dsh/trading-venv/bin/python -B -m trading_core reconcile-daily --today 2026-09-14` | `fills = 0` | `fills = 3`（HK.00981 1000@61.4、HK.00100 200@253、HK.02513 100@729；15 条券商单收编进 OMS） |
| 对账差异（diffs） | 同上，末尾再跑一次当天：`… reconcile-daily --today 2026-09-19` | `diffs = 0` | `diffs = 11`（2 条数量不一致 + 9 条单边缺失） |
| 本地模拟台账（组合权益 + 执行页本地台账） | `DSH_HOME=~/.dsh ~/.dsh/trading-venv/bin/python -B plugins/engine/python/engine.py decide --ticker SH.600031 --strategy rsi --apply` | `quant-ledger.json` 不存在、`equity.points = 0`、`trades = 0` | 1 笔模拟买入（8200 股 @17.89，含费 190.71）、`equity` 出数、`trades = 1` |
| 第二个计划（计划列表卡） | `PYTHONPATH=plugins/core/python ~/.dsh/trading-venv/bin/python -B -m trading_core plan-build --mode SIM --strategy momentum_value_top5 --target '{"SH.600000": 0.03}' --prices '{"SH.600000": 9.07}' --as-of 2026-09-18` | `plan.plans = 1`（`plans.length > 1` 才渲染列表卡） | `= 2`（新增 `PLN-20260918-SIM-12FB`，`origin=manual`，不会被自动执行） |
| 财报卡（factors quality） | 数据本来就在（`fundamentals` 578 行）；缺的是**取证方式** → 见上面 factors 的 `quality` phase | `NOT_RENDERED`（工具只填 3 只标的，而卡片要恰好 1 只） | 12 项全部 ok |
| 期权筛选 / 衍生品 | 无需写库；用 phase 在页面上点「筛选」/填合约代码点「查询」 | 未覆盖 | 6 项全部 ok |
| 行情页实时报价/盘口 | 无需写库；行情页改为默认挑 HK 标的 | A 股标的恒 `-9` → UNJUDGED | 13 项（12 ok + 1 空态） |
| 事件页时间线 | 无需写库；`symbolResolver` 自动挑有事件的标的 | 只审计到 1 个字段 | 5 项全部 ok |

> **`reconcile-daily` 的两个前提与副作用（必须知道）**：
> ① `fills` 的回填**只认券商订单历史**（`_backfill_fills`，幂等、只补差额）。本机券商侧
> 30 天窗口里**只有 2026-09-14 那 3 张单真成交**（`cum_qty > 0`），且当天本地台账没有对应
> 订单，所以必须先跑 `--today 2026-09-14` 让对账把券商单收编进 OMS 再回填；不带 `--today`
> 跑当天只会拿到空订单窗口，`fills` 依然是 0。
> ② 有差异就会 `set_halt(True, reason="reconcile_diff")`——本轮真实造出了 11 条差异，
> **自动执行因此被熔断**。这是平台的设计行为（差异不平就暂停），恢复要人工查明后
> `store.clear_halt`（见 `docs/RUNBOOK.md` 场景 3）。不想要这个副作用就别跑当天的对账。

---

### 本轮逐字段审计结果（2026-09-19）

| 阶段 | 字段数 | ok | 空态正确 | 判定缺陷 | 其中**真页面缺陷** | 工具侧假阳性 | 观察项 | 退出码 |
|---|---|---|---|---|---|---|---|---|
| **基线**（修复前） | 122 | 38 | 18 | 20 | **13** | 6（+1 条重复计数） | 53 | **1** |
| **修复后**（全量 16 页） | 124 | 90 | 20 | **0** | 0 | 0 | 16 | **0** |
| 补充：`--pages market --symbol HK.09961`（实时报价/盘口数据面） | 13 | 12 | 1 | **0** | 0 | 0 | 0 | **0** |
| **本轮**（造数据 + 交互取证 + 修 2 处新缺陷，全量 16 页） | **154** | **150** | **4** | **0** | 0 | 0 | **0** | **0** |

产物：`~/.dsh/logs/page-fields-<ts>/report.json`（基线 `page-fields-2026-09-18T18-34-59-801Z`、
修复后 `page-fields-2026-09-18T19-02-24-094Z`、HK 补充轮 `page-fields-2026-09-18T19-05-43-827Z`、
本轮 `page-fields-2026-09-19T02-59-10-910Z`）。

**本轮相对上一轮的三个变化**（口径没动，判定码与退出码语义不变）：

1. **字段 124 → 154**：新增的 30 条全部来自「原来取不到证的区块」——期权筛选 3 条、
   期权衍生品 3 条、因子财报卡 12 条（上一轮 16 条里 3 条在旧规格下是 NOT_RENDERED，
   本轮换成 12 条的完整卡）、研究运行/研报 6 条、信号页 3 条（信号/数据日期/历史预览标的）、
   对账差异 2 条（差异类型/数量差）、事件时间线 4 条（减去原来的 1 条日期正则）；
   同时删掉了两个**规格写错**的字段（signal 的「策略」按原始 `strategy` 比对，
   而页面渲染的是 `strategy_label`；factors 的财报路径写在 base 而非单标的 phase）。
2. **ok 90 → 150、空态 20 → 4**：剩下的 4 条空态都是**真的没有数据**（见下表），
   且这 4 条本轮已用真实载荷核实过「两端确实都空」。
3. **新发现并修掉的 2 处真页面缺陷 + 3 处工具侧误判**（见后两节）。

**逐页结论（本轮）**

| 页面 | 审计字段数 | ok | 空态正确 | 缺陷 | 备注 |
|---|---|---|---|---|---|
| overview | 10 | 10 | 0 | 0 | 因子数/覆盖标的在 `payload.tickers` 对象上取，全部命中 |
| market | 13 | **13** | 0 | 0 | 兜底 `preferMarket: "HK"` 后**默认就覆盖**实时报价/盘口（上一轮要手动 `--symbol`）；同名标签按卡片限定 |
| capital | 10 | 10 | 0 | 0 | 四档键名/毫秒时间戳/分布表全部命中 |
| options | 11 | 9 | 2 | 0 | 新增 `screen`/`derivative` 两个 phase 共 6 条：筛选结果 3 条 + 衍生品 3 条全部命中 |
| signal | **9** | **9** | 0 | 0 | 造出 3 条 signal 预览后全覆盖；**发现并修掉 ATR 截断缺陷** |
| portfolio | 7 | 7 | 0 | 0 | 造出本地台账后 `current/total_return/max_drawdown/trades` 全部命中 |
| risk | 14 | 14 | 0 | 0 | 无变化 |
| factors | **25** | **25** | 0 | 0 | base 3 只标的（13 条）+ `quality` phase 单标的（12 条） |
| execution | 14 | 12 | 2 | 0 | 本地台账有 1 笔模拟买入；OpenAPI 四表有 51 条订单/3 条成交；`收益`/`胜率` 仍空（无卖出） |
| research | **9** | **9** | 0 | 0 | 造出 1 个 run + 1 篇已发布研报；运行表 + 研报表全部命中 |
| events | **5** | **5** | 0 | 0 | `symbolResolver` 自动挑到 US.NVDA（5 条事件）；时间线 4 条字段全部命中 |
| plan | 6 | **6** | 0 | 0 | 造出第 2 个手工计划 → 「计划列表」卡渲染，`创建时间`/`订单数` 命中 |
| pipeline | 2 | 2 | 0 | 0 | 无变化 |
| schedule | 4 | 4 | 0 | 0 | 无变化 |
| audit | 11 | 11 | 0 | 0 | 造出 11 条对账差异后差异表渲染；**发现并修掉 `[object Object]` 缺陷** |
| settings | 4 | 4 | 0 | 0 | 无变化 |

**仍然空态的 4 条**（不是「已验证」，是「确实没有数据」）：

| 页面 / 字段 | 后端 | 渲染 | 为什么空 |
|---|---|---|---|
| options 成交量（期权链） | 全 null | `—` | HK 期权链（HK.00100）这些行的 `volume` 上游就是 null；US 那边有值（筛选 phase 的成交量 815,132 就命中） |
| options 标的价格（行权概率卡） | 顶层无 `security_price` | `—` | **上游把 `security_price`/`strike_probability`/`timestamp` 放在 `item_list[]` 里**，卡片按顶层键读 → 全 `—`。见「未修」清单 |
| execution 收益（本地台账） | 无卖出记录 | `—` | T+1 约束下当天买入当天不可卖，本地台账只有 1 笔 BUY，`return` 字段本来就不存在 |
| execution 胜率（卖出计） | `win_rate = null` | `—` | 同上：没有 SELL 行时 `win_rate` 恒为 null |

### 本轮修掉的真页面缺陷（2 条）

| # | 页面 / 字段 | 后端值 | 渲染值（修复前） | 为什么是缺陷 | 修法 |
|---|---|---|---|---|---|
| 1 | signal 最新信号 · ATR(14)（同因也影响 `收盘价` 与 portfolio `最新权益`） | `5.915678571428567` | `5.91` | 正确值是 `5.92`。根因是 **antd `Statistic` 带 `precision` 时截断而非四舍五入**（`node_modules/antd/es/statistic/Number.js`：`decimal.padEnd(precision,'0').slice(0,precision)`）。这是全站口径不一致：其他数值展示（`num`/`pctOf`/`f10.fmtNum`）都四舍五入 | 新增 `formatCore.roundTo(value, digits)`（`Number(v.toFixed(digits))`）并在 3 个 `precision={2}` 的 Statistic 上传入，先四舍五入再交给 antd；`platform/web/tests/formatCore.test.mjs` 与审计工具 `numFixed2` 契约各自锁住 |
| 2 | audit 对账差异 · 本地/券商列 | `local=null, broker={"qty":5200}`（`missing_side`）、`local={"qty":-100}` | `[object Object]` | `reconcile.compare` 的两类差异**值形状不同**：`qty` 类给整数、`missing_side` 类给**整份持仓字典**。页面 `num()` 对非有限值回落 `String(value)`，把字典渲染成 `[object Object]`——页面出现这种东西等于「拿未知当已知」 | `audit.jsx` 新增 `diffQty()`：字典取它的 `qty`（唯一的数量事实），`null`/取不到 qty 显示 `—`；`qty` 类整数行为不变 |

### 本轮修掉的工具侧误判（3 条，不是页面缺陷）

| 判定 | 页面对照 | 为什么是工具写错 |
|---|---|---|
| events 「事件详情」`FORMAT`（后端 `每股派息 0  · …` 双空格 vs 渲染单空格） | HTML 折叠连续空白 | 期望文本未按渲染口径做空白归一 → 比对前统一 `replace(/\s+/g," ")` |
| market 涨跌幅（实时报价卡）`EMPTY`，同列却渲染 `+18.92%` | 页面 `changePctOf`：上游 `change_rate/change_pct` 都没有时，用（最新价−昨收）/昨收 换算 | 规格只读了上游字段 → 改成与页面同序取值（`quoteChangePct`） |
| audit 「券商」列判 `ok`（同列已有 `[object Object]`） | 该列确实渲染了 `[object Object]`（真缺陷） | 判定顺序问题：`hits` 短路在毒值判据之前 → **毒值判定提前**（这条误判掩盖了上面那条第 2 号真缺陷） |

### 本轮**未修**（需要产品决策或超出「显示缺陷」范围，如实列出）

| 项 | 事实 | 为什么不修 |
|---|---|---|
| options 「行权概率」卡整卡显示 `—` | 上游 `option_exercise_probability` 的响应只有 `item_list[]`，`security_price`/`timestamp`/`strike_probability` 都在**数组元素**里；`f10.exerciseProbabilitySummary` 按顶层键读 → 三项全 `—`（`docs/TOOL-LIMITS.md` 的锁定表把这三个字段记成 section 级字段） | 「取哪一条 item、怎么和 `strike_probability` 配对」锁定表没写（实测 item[0] 只有 price/timestamp，item[1] 才有 strike_probability）→ 映射等于**猜语义**。**本轮不猜，如实暴露**：字段判定记 `EMPTY` 并在本节点名，修它要先确定 item_list 的排序与配对规则 |
| audit 对账差异「本地/券商」列 | 已修渲染（取 `qty`），但 `missing_side` 的语义仍是「一侧没有持仓」 | 显示层已不撒谎；`local=-100`（只有卖出成交、没有对应买入）这类**负持仓**是数据层事实（券商 30 天订单窗口缺历史买单），要不要在数据层修是另一个决策 |
| execution OpenAPI 四表用 `rawCell` 原样展示 | 数量 `"1000"`、委托价 `"61"` 不带千分位，与全站 `num()` 口径不一致 | 该函数有明文理由（原文数值原样展示；订单号/状态原码必须保持原样）。需按列决定 |
| capital 资金分布「≤1 就 ×100」的单位猜测 | 上游当前不给 `*_ratio` 键（走自算分支） | **无法用真实数据判定**该猜测对不对 |
| 「最大可买可卖」模拟盘必失败 | 载荷缺 `price`（`trading.py:1175` 明确必填），且列读 `max.max_cash_buy` 而模拟盘实际键是 `max_cash_buy_qty_round_lot` | 属**功能性**缺陷（端点失败 → 整卡无数据），修它要先定「用哪个价格算最大可买」 |
| execution 实盘（live）下四张 OpenAPI 表缺 `market` | `OpenApiBroker._per_market` 强制要求 market | 模式相关功能性缺陷；本环境是 sim，无法复现验证 |
| 研究/执行页时间列的时区语义 | 服务端用 `toISOString()`（UTC），页面截断显示且不标时区 | 显示格式符合契约（无 T、可读），但「UTC 值当本地时间读」是语义问题，需产品决策 |
| **portfolio 权益曲线本身**（canvas） | 需要**≥2 个交易日**的本地台账成交才画；同一天只能造出 1 个点 | 不是缺陷、也不是工具限制：页面已如实显示「权益序列仅 1 个点，不足以绘制」。要验证曲线得等到第二个交易日再跑一次 `engine.py decide --apply` |
| 本地模拟台账没有**定时写入方** | `~/.dsh/quant-ledger.json` 只被 `engine.py decide --apply`（CLI/对话工具）写，`auto_pipeline`/调度链**不写**它 | 结构性事实（不是显示缺陷）：不人工跑 `decide`，组合页权益与执行页本地台账就长期是空态。要不要给它一个调度写入方是产品决策 |

### 本轮**未覆盖**（工具的诚实清单）

- **图表内部像素**：canvas 图表只判「有没有数据可画」（空态文案/跳过计数），不核像素与刻度。
- **端点恒失败的字段**：A 股 `rt_quote`/`rt_order_book`（`-9 无权限`）→ UNJUDGED；
  行情页已自动改用 HK 标的覆盖，但**A 股那一路的失败形态**仍未被「有数据」的用例覆盖。
- **需要点提交类控件的区块**：确认卡片（`confirmation`）、规则审批（`rules-decide`）、
  计划执行（`plan-execute`）、模式切换——这些会改状态，按工具的只读纪律**不点**，
  由 `e2e_web.mjs` 与后端测试覆盖。
- **仍为空态的 4 条字段**：见上表，它们**不是**「已验证」，是「确实没有数据」。

---

以下三节保留**上一轮（2026-09-19 上午，提交 `63118d2`）**的原始记录，作为历史对照；
本轮的口径补充见上文。

> **口径说明（上一轮）**：基线那 20 条判定里，6 条是**工具自己的规格写错**（把有意的展示映射
> 当成后端原值比对：overview 账户模式「模拟 SIM」、research 规则状态「未通过」、plan/pipeline
> 的取值正则把时间戳截断、audit 时间列的精度种类选错、settings 通道带后缀），另有 1 条是同一
> 根因（audit 自检时间带 `T`）的字段判定与全文扫雷各记一次。这 7 条已在**工具侧**修掉（正则/
> 种类/包含匹配），不是页面缺陷。**真页面缺陷 = 13 条**。

### 上一轮修掉的真页面缺陷（17 条）

其中 13 条由基线审计直接指出，另外 4 条由**代码 + 真实载荷**判定（基线那一轮因前置条件不满足
或端点无权限没能在浏览器里取到前后对比，逐条注明证据）。

| # | 页面 / 字段 | 后端值 | 渲染值（修复前） | 为什么是缺陷 | 证据来源 |
|---|---|---|---|---|---|
| 1 | overview 因子数 | `payload.tickers` 的 5 个因子键 | `—` | 落库 payload 实测是 `{date,tickers:{标的:{因子:值}}}`——**没有 `factors` 键**，`payload.factors.length` 恒 undefined | 基线审计 |
| 2 | overview 覆盖标的 | `payload.tickers` 的 20 个标的 | `—` | `tickers` 是**对象**不是数组，`.length` 恒 undefined | 基线审计 |
| 3 | capital 时间列（分时） | `1789695000000`（毫秒） | `1789695000000` | 时间列显示 13 位整数，读不出任何时刻 | 基线审计 |
| 4-7 | capital 超大单/大单/中单/小单 | `super_in_flow` / `big_in_flow` / `mid_in_flow` / `sml_in_flow` | 四列**全是 `—`**，四线折线全空 | 实测字段名是 `*_in_flow`，页面候选键只登记了 `*_inflow` | 基线审计 |
| 8 | capital 日期列（历史） | `1786291200000` | `1786291200`（10 位假日期） | 原实现 `String(v).slice(0,10)` 把毫秒时间戳截成 10 位数字 | 基线审计 |
| 9 | capital 资金分布 净流入 | `capital_in_*` / `capital_out_*` | 4 格 `—` | 该端点给的是「流入/流出」两套键，页面只找「净流入」单键 | 基线审计 |
| 10 | capital 资金分布 占比 | 同上 | 4 格 `—` | 同上（净流入取不到 → 占比也取不到） | 基线审计 |
| 11 | portfolio 数据时间 | `2026-09-19T02:31:16` | 原样（带 `T`） | 全站时间戳契约是 `stampOf`（T→空格） | 基线审计（全文扫雷） |
| 12 | risk 持仓数据时间 | 同上 | 原样（带 `T`） | 同上 | 基线审计（全文扫雷） |
| 13 | audit 自检时间 | `2026-09-19T02:37:42+08:00` | 原样（带 `T`） | 同页「差异时间」已走 `stampOf`，此处漏了 | 基线审计（全文扫雷） |
| 14 | factors 正 IC 占比 | `positive_ratio = 0.472`（比例） | `0.472` | 标签是「占比」，同页比例型字段都走 `pctOf` → 应显示 `47.20%` | 代码 + 载荷（基线那轮 IC 卡未渲染：多标的输入框没填） |
| 15 | options 到期日列 + 本地到期日过滤 | 行内 `strike_time = "2026-09-18"` | 列全是 `—`；一旦选中某个到期日，**整表被筛空** | 实测链行到期日键是 `strike_time`，而列与过滤用的候选键是 `expiration_date/expire_date/date`（同文件 `expirationCandidates` 却有 `strike_time`——三处不一致） | 代码 + 载荷（基线那轮用 A 股标的，被上游 `-8` 拒绝） |
| 16 | execution 更新时间列 | `create_time = "1789363901000000"`（微秒） | 16 位数字 | 模拟盘/部分通道给微秒整数，`String(v).slice(0,16)` 把它当文本截断 | 代码 + 载荷（需先有 OpenAPI 订单行） |
| 17 | overview 持仓数 / 今日成交 / 活跃告警 | 端点读取失败（无后端值） | `0` | 把「没读到」渲染成「确实没有」（旁边还同时显示「读取失败」）；工具只在真的失败时才判得出，本轮端点正常 → 未进基线 | 代码（`positions.error ? … : 0` 的兜底恒不为 null） |

**修法**（一句话一条）：新增 `services/formatCore.js`（`clockText` 毫秒→`HH:mm`、`dayText`
毫秒→`YYYY-MM-DD`、`minuteText` 毫秒/微秒→`YYYY-MM-DD HH:mm`，`format.jsx` 原样再导出）；
新增 `services/factorSnapshot.js`（从 `payload.tickers` 推因子数与覆盖标的）；
capital 补实测候选键（`*_in_flow`）与「流入−流出」净流入换算；options 三处候选键统一；
factors「正 IC 占比」与四处时间戳改走 `pctOf`/`stampOf`；overview 三个 Statistic 在读取失败时
显示 `—` 而不是 `0`。

### 上一轮工具侧修掉的 6 条假阳性（不是页面缺陷，如实列出）

| 判定 | 页面对照 | 为什么是工具写错 |
|---|---|---|
| overview 账户模式 `sim` vs `模拟 SIM` | `services/mode.js` 的徽章译文 | 有意的展示映射；已从规格中移除 |
| research 状态 `failed` vs `未通过` | 规则候选池状态中文映射 | 同上 |
| plan 冻结时间「2026-09-18」 | 页面写的是完整 `2026-09-18 20:50:24` | 取值正则 `(\S+)` 在空格处截断 → 改成吃完整时间戳 |
| pipeline 日期「2026-09-19A股」 | 页面写的是 `日期 2026-09-19` | 同上（正则贪婪吃到下一个标签） |
| audit 时间 `2026-09-18T11:46:28.281Z` | 页面 `timeOf` 输出 `2026-09-18 11:46` | 种类选错（应分钟精度）→ 新增 `stampMinute` |
| settings 通道 `openapi` vs `openapi（本页凭据）` | 页面有意带后缀 | 改成包含匹配 |

### 上一轮**未修**（需要产品决策或超出「显示缺陷」范围，如实列出）

| 项 | 事实 | 为什么不修 |
|---|---|---|
| audit 对账差异「本地/券商」列可能渲染 `[object Object]` | `reconcile.compare` 的 `missing_side` 行把**整个持仓字典**放进 `local`/`broker`，页面 `num()` 对非有限值回落 `String(value)` | 上一轮 `diffs` 为**空**，无法用真实数据判定该列应显示什么 → **本轮造出 11 条真实差异后已确认是缺陷并修掉**（见上文「本轮修掉的真页面缺陷」第 2 条） |
| execution OpenAPI 四表用 `rawCell` 原样展示 | 数量 `"1000"`、委托价 `"61"` 都不带千分位，与全站 `num()` 口径不一致 | 该函数有明文理由（「原文数值原样展示，num 会四舍五入」），且**订单号/状态原码必须保持原样**——改成 `num` 会把订单号变成 `7,138,921`。需按列决定 |
| capital 资金分布「≤1 就 ×100」的单位猜测 | `Number(ratio) <= 1 ? ratio * 100 : ratio` | 上游当前不给 `*_ratio` 键（走自算分支），**无法用真实数据判定**该猜测对不对 |
| 「最大可买可卖」模拟盘必失败 | 载荷缺 `price`（`trading.py:1175` 明确 price 必填），且列读 `max.max_cash_buy` 而模拟盘实际键是 `max_cash_buy_qty_round_lot` | 属**功能性**缺陷（端点失败 → 整卡无数据）；修它要先定「用哪个价格算最大可买」，超出显示层 |
| execution 实盘（live）下四张 OpenAPI 表缺 `market` | `OpenApiBroker._per_market` 强制要求 market | 同上：模式相关功能性缺陷；本环境是 sim，无法复现验证 |
| 研究/执行页时间列的时区语义 | 服务端用 `toISOString()`（UTC），页面截断显示且不标时区 | 显示格式符合契约（无 T、可读），但「UTC 值当本地时间读」是语义问题，需产品决策 |
| schedule 心跳原文可能带 `T` | `alerts.emit` 用 ISO 写心跳（当前值恰为空格分隔） | 当前数据不带 T → 本轮**不构成缺陷**；工具已覆盖，一旦出现即报 `T_TIMESTAMP` |

### 上一轮**未覆盖**（工具的诚实清单）

- **需要表单提交/手输才能出数据的区块**：期权筛选（`option_screen` 要按下「查询」）、
  期权波动率/行权概率（要手输**期权合约代码**，不是标的）。工具是只读的，不点提交类控件。
- **需要特定前置状态的区块**：factors 的单标的财报质量卡（恰好 1 只时）、plan 的「计划列表」
  （>1 个计划时）、signal 最新信号卡与历史预览（`previews` 非空时）——本轮前置条件不满足，
  对应字段记 `NOT_RENDERED`/`EMPTY`，**不等于通过**。
- **图表内部像素**：canvas 图表只判「有没有数据可画」（空态文案/跳过计数），不核像素与刻度。
- **端点恒失败的字段**：A 股 `rt_quote`/`rt_order_book`（`-9 无权限`）→ UNJUDGED；
  这类字段**必须在 HK/US 标的下复跑**才算覆盖（本轮已补跑一轮）。
- **执行页 10 项/信号页 6 项之类的空数据字段**：数据本身为空时工具判 `EMPTY`——
  那是「显示 `—` 正确」，但**没有验证过有数据时的格式**，需要等真实数据到位后复跑。

---

## 四、本轮结果与缺陷清单

### 结果（2026-09-17）

| 面 | 结果 |
|---|---|
| **后端**（`--read-only`，加固后） | 声明端点 **82/82 可达**；读 **70 = ok 63 / 降级 7 / 0 error**；section **30 = ok 28 / 降级 1 / 1**（该 1 条＝缺陷 1 现场证据）；契约 5/5；真泄漏 **0**；`stale_service_contract: true` |
| **后端**（完整含写，加固后） | 写 23 = ok 21 / error 2（拒单契约 + 非法 op 各 1，均属「契约未生效」）；三类状态：服务活动 1 / 审计行 4 / **真泄漏 0**；kill→规则 1 拒单→unkill 与 `auto_pipeline` 写回按字节复原均通过 |
| **前端**（16 路由，`--no-screenshot`） | **16/16 通过**，阻断/严重/重要 **0**，软断言失败 **0**，观察项 6，退出码 0 |

**一个关键区别（排障必读）**：前端修复**已生效**（静态产物 `platform/web/dist` 重建即可，无需重启进程）；
后端修复**未生效**——服务进程仍是修补前的那个（启动于 17:38），所以拒单/撤单回写/载荷错误码
等新契约在运行中的服务上**看不到**。脚本汇总会显式打印
「⚠ 契约未生效：服务可能运行旧代码 → 请重启服务加载 HEAD 修复后复跑」并在 JSON 置
`stale_service_contract: true`。**重启后**下单生命周期探针（place→cancel→replace、place→modify）
会自动启用并把结果写进下一次报告。

### 缺陷 → 修复提交对照

| 编号 | 现象 | 根因 | 修复提交 |
|---|---|---|---|
| **缺陷 1** | `derivative_detail {section:"future_info"}` → `invalid-operation`「code_list 必须是 1..400 的列表」；该 section 永远失败 | 适配层把**单标的 code** 传给需要**列表**的传输层方法 | **`cc2239c`**（聚合端点 section 参数装配：批量方法包 `code_list`） |
| **I1** | 缺 `days_before` / `leader_name` → `futu-unavailable`「missing 1 required positional argument」 | 缺必填被归成**通道故障**（用户被引去配凭据） | **`cc2239c`**（必填参数前置校验 → `invalid-operation`） |
| **缺陷 2** | 拒单后 OMS `err` 只剩「backend business error」，无上游码 | 券商/上游业务错误丢错误码 → 无法判断价格越界/权限/限频 | **`f86783f`**（错误码随错误走；现形如 `[errcode=-2000] 资金不足`） |
| **缺陷 3** | 被券商拒的单仍返回 `ok:true`（`status: rejected`） | 拒单被当成「请求成功」 | **`f86783f`**（拒单 → `ok:false` + `order-rejected`） |
| **S2** | 撤单成功不回写 OMS → 幽灵在途单永久阻塞同标的同方向新单（连 kill 验证都被在途查重抢先） | 缺 OMS 回写 | **`f86783f`**（撤单/撤旧回写 OMS） |
| **M1** | 改单（撤旧重下）后旧单 OMS 行未落 `cancelled` | 同 S2 的幽灵单 | **`f86783f`** |
| **缺陷 4** | `correlation`/`factors`/`ic` 空载荷 → `analytics-unavailable` | **载荷错误**被归成「引擎不可用」 | **`6c4cd7d`**（载荷错误 → `invalid-operation`；引擎真不可用仍是 `analytics-unavailable`） |
| **缺陷 5** | 只读设置类端点 POST 无 body → 415，读都读不到 | 空 body POST 被当坏请求 | **`6c4cd7d`** |
| **（E2E 状态类）** | 关注池为空时基础链作业跳过，但流程页阶段仍显示 `ok`（把「什么都没做」报成完成） | 阶段状态只看 ran 标记，不看内容结局 | **`2b823f0`**（流程页不再把「什么都没做」报成已完成；现为 `skipped` + 原因摘要） |
| **option_screen** | 探针缺 `field_filter` 时上游只回 4 个默认字段（会被误判成数据缺失） | 契约不可发现 | **`7e42075`**（契约可发现：真机示例 + 值形状前置校验；harness 侧同时补足探针载荷） |
| **F1（前端）** | 加载窗口以**事实口吻**渲染空态（「暂无持仓」「无心跳记录」「今日无该链阶段」） | 「尚未取到」被渲染成「确实没有」 | **`bf36057`**（加载态与空态分离 + 流程页首启配置提示） |
| **F2（前端，本轮新发现）** | `overview` 页整页崩溃：`ReferenceError: fieldState is not defined`（「something went wrong」、锚点「运行状态」缺失、白屏） | `overview.jsx` 用了 `fieldState` 却**未导入**（`services/fieldState.js` 是新增文件） | **`9c136c7`**（补缺失导入 + 页面静态导入锁） |
| **I2–I5 / S1 / S3 / M2–M4** | 作者原始编号，**未在本轮产物中复核** | — | 未复核（如需要请提供原始清单） |

**F1 的当前状态：已绿。** `node scripts/e2e_web.mjs --no-screenshot` → **16/16 通过、软断言失败 0**；
`--fail-on-soft` 下同样为 0（可用它作为长期回归门）。判定依据＝本轮 16 路由的 `domEarly`
（加载窗口）里不再出现空态事实文案。

**关于 portfolio「读取失败」这条（曾报红，已确认为 harness 期望过松，非产品缺陷）**：首版把裸串
`"读取失败"` 列入「加载窗口事实断言」，命中的其实是该页的**说明文案**
「实时读取失败时展示缓存并标注 stale」——那是策略描述，且该页 4 个 API 全部 **200**
（`push_status/positions/equity/snapshot`），并非在说谎。已修正期望：空态断言只认
`暂无…`/`无心跳记录`/`数据源 —`/`今日无该链阶段` 等**absence** 文案；
失败断言（`读取失败：<详情>`）**必须**与失败请求关联——有失败请求时如实展示失败属**诚实**
行为（记观察项），**没有**失败请求却显示失败才算说谎（记软断言）。

### WP16 复验（2026-09-17 晚：模拟盘读能力 + 自动执行下单账户）

| 面 | 结果 |
|---|---|
| **后端完整扫**（`scripts/e2e_workbench.py`） | 读 **70 = ok 63 / 降级 7 / 0 error**；section **30 = ok 29 / 降级 1 / 0**；契约 5/5；写 **32 = ok 32 / 0 error**；一致性 **4/4**；**发现 0、真泄漏 0、阻断 0**，退出码 **0** |
| **前端**（16 路由，`--fail-on-soft`） | **16/16 通过**，阻断/严重/重要 **0**，软断言失败 **0**，观察项 6（均为数据依赖，非缺陷），退出码 0 |
| **三套测试** | python **2167 OK**（skipped 5）｜node **66**｜web **251** |
| **真机闭环（模拟盘全自动）** | 调度器自动生成冻结计划 → 九守卫 → `execute_plan` 指令 → 轮询消费 → 风控 8 规则 → **券商接单**（修复后 `7149712`/`7149716` `submitted`，随后撤单净空）；详见 TOOL-LIMITS「模拟盘读能力与自动执行下单账户」 |

**本轮新发现并修复的产品缺陷**（测试 + 反证已锁）：

| 编号 | 现象 | 根因 | 落点 |
|---|---|---|---|
| **A1** | 模拟盘自动执行的**每一张单**都被券商 `-3 invalid parameter` 拒 | `execute.run` 用占位符 `acc_id="SIM"` 下单；REST 把它拼进 URL（`/sim-trade/SIM/orders`）。MCP 通道下上游忽略它 → 切到 openapi 才暴露 | `broker.sim_account_for_market` + `execute.run` 逐市场解析（`tests/test_wp16_auto_order_account.py` 8 项） |
| **A2** | sim 的 `trade_max_qty` 传字符串枚举必被拒；缺 `price` 时回全 0；带前缀标的报 `-5` | 工具面契约（OpenAPI 字符串枚举）与模拟盘枚举（1/3）、裸代码、必填 `price` 三处未收口 | `trading.py::_sim_max_qty_order_type` + 裸代码 + price 必填（`tests/test_wp16_sim_reads.py` 对应 5 项） |
| **A3** | `orders_detail` 同一订单在当日与历史各出现一次 → 详情重复行 | 两套接口都覆盖当日，未按订单号去重 | `orders_detail` 并集去重 + 用例 |

**遗留未决（如实登记，不在本轮擅自处置）**：① A 股模拟账户实持 8 只 > 默认 `max_positions=5`，
可用资金 `55,257.816`（权益 `812,231.816`）——按权益权重定量的自动计划在本机结构性买不动，
是否调风控/减仓属交易决策；② `missing_in_oms` 对账差异（`SH.603993` / `7147945`，已撤无成交）
没有收敛入口（`oms-align` 要求本地已有该单），已按 RUNBOOK 场景 3 人工 `clear_halt`，
critical 告警保留未 ack；③ A1 修复在 `trading_core`，**需重启服务**才在生产进程生效
（本轮按约定未重启，故运行中服务的下单生命周期探针仍走旧代码）。

### 工具侧自身缺陷（本轮修掉）

见 §一 表格（三类状态判定、tickers 探针、期望拒绝分类、`option_screen` 载荷、下单价格网格、
过渡/条件性分类、阶段感知统计、幽灵单处理、契约前置门）。修掉后，后端 harness 从
「每轮必报 2 条 critical 假阳性」变成可长期复用的门；前端 harness 从「说明文案误判」变成
「空态/失败断言分类判定」。**WP16 晚补两条**：派生阶段（`plan`/`execute`）不再参与 ran 标记
比对；`trade_modify` 探针补 `side`（否则 write 恒 error=1）。修后后端 harness 首次
**写 32/32 ok、发现 0、退出码 0**。

### 幽灵在途单（诚实披露）

`orders` 表现存 **2 条 `submitted`**（`SH.601988 BUY`、`SH.601398 BUY`，sim）：这是修复前
（撤单不回写 OMS = S2）做 place→cancel 留下的——券商侧已撤单成功，OMS 未回写。它们会按
`(symbol, side)` 阻塞同标的同方向新单。
**处理**：① 本 harness 不手改库、不重启服务；② 自动换用未被阻塞的候选标的
（`ORDER_CANDIDATES` 三只），并在报告里明确列出被阻塞的标的；③ 历史遗留行由平台属主按
OMS 维护流程清理（修复后新增的撤单都会正常回写）。

## 五、复验方法（判闭环）

```bash
# 1) 后端：先只读扫一遍（快、无副作用）
~/.dsh/trading-venv/bin/python scripts/e2e_workbench.py --read-only

# 2) 后端：完整扫一遍（写检查可逆，结束会复原；契约未生效时会自动跳过下单生命周期探针）
~/.dsh/trading-venv/bin/python scripts/e2e_workbench.py

# 3) 前端：全量 + 严格软断言
node scripts/e2e_web.mjs --fail-on-soft
#    单页复验：node scripts/e2e_web.mjs --routes overview --no-screenshot

# 4) 三套单元/集成测试（harness 之外的正交证据）
~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests
node --test tests/*.test.mjs
cd platform/web && npm test
```

**闭环判据（逐条）**

| 缺陷 | 闭环判据（重跑后） |
|---|---|
| 缺陷 1 / I1 | 1b 阶段：`future_info` 成功或如实降级；缺 `days_before` / `leader_name` → `invalid-operation` 且 message **点名参数**（不再是 `futu-unavailable`）；「`future_info` 出站装配错误」与「过渡码」发现消失（修复 `cc2239c`，**待服务重启生效**） |
| 缺陷 2 / 3 | 写阶段「带外价 → 拒单契约」一行：`ok:false` + `order-rejected` + message 含 `[errcode=` → 相应发现消失（修复 `f86783f`，**待服务重启生效**） |
| S2 / M1 | 写阶段「撤单后 OMS 回写(S2)」cleared=true；「撤单后同标的同方向仍被阻塞」消失；`place→modify` 后旧单落 `cancelled`（修复 `f86783f`，**待服务重启生效**）。**验证前提**：扫描前无幽灵单（见 §四「幽灵在途单」） |
| 缺陷 4 | 1c 契约三行的码从 `analytics-unavailable` 变为 `invalid-operation`，「过渡码」发现消失（修复 `6c4cd7d`） |
| 缺陷 5 | 1 阶段 `openapi_config`/`auto_pipeline` 用 GET 读成功（不再是 415）（修复 `6c4cd7d`） |
| 状态类（pipeline） | `pipeline vs kv ran 标记不一致 = 0`，且关注池为空的阶段显示 `skipped` + 原因（不是 `ok`）（修复 `2b823f0`） |
| F1（前端加载态） | `node scripts/e2e_web.mjs --fail-on-soft` 退出码 0 且 `softAssertions` 为空（**已达成**，修复 `bf36057`；本轮 16/16、软断言 0） |
| F2（前端 overview 崩溃） | `node scripts/e2e_web.mjs --routes overview --no-screenshot` 通过且锚点「运行状态」在位（**已达成**，修复 `9c136c7`） |
| 状态泄漏（工具侧） | 阶段 4 ③ 真泄漏 = 0（服务活动与审计行不计） |

> **服务重启是后端闭环的最后一步**：以上后端判据都要求运行中的服务已加载 HEAD 修复
> （`scripts/platform_service.sh restart`，见 [RUNBOOK](RUNBOOK.md)）。重启前，harness 的
> `stale_service_contract: true` 与「⚠ 契约未生效」提示就是这个事实的**如实表达**，
> 不是脚本故障；重启后下单生命周期探针会自动启用。

## 六、已知未覆盖（诚实清单）

- **TTL 缓存陈旧**：只验证端点可达与形状，不验证「缓存过期后是否刷新」；
  `_refresh: true` 旁路的时序未测。
- **浏览器交互/提交类路径**：前端 harness 是只读的（不点击提交控件），因此
  「点批准 → 端点 → 刷新」这类链路只有端点级/单元级证据（`tests/test_wp14_approve.py`）。
- **WebSocket 实时更新**：不验证推送到达后页面是否自动更新（只验 `/healthz` 的 push 状态
  与 `push_status` 端点形状）。
- **systemd 定时器实装**：`install/research-duty.timer` 的装载与 `Persistent=true` 的
  错过补跑需在真实 systemd 上验证（本环境未安装）。
- **真实 headless 值班**：`scripts/research_duty.sh` 只做了假 dsh 的契约测试，
  未真实跑一次 `dsh --profile headless`（消耗模型额度且需服务在线）。
- **live 链路**：全部验收在 sim；实盘下单/撤单/对账属 P4 人工准入，不在 E2E 范围。
- **跨语言/跨进程一致性**：服务与作业子进程的代码版本一致性靠「仓库优先 + 安装副本刷新」
  保证，harness 不校验副本新鲜度（`--read-only` 也不检）。
