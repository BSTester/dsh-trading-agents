# E2E 验收工具链与本轮结果

> 本文是**可重复运行的端到端验收**的唯一入口文档：两个 harness 的用途/覆盖/用法/退出码、
> 本轮实测结果与缺陷清单、以及「修复落地后怎么判闭环」。
> 相关：服务启停见 [RUNBOOK.md](RUNBOOK.md) 的「服务启停（推荐方式）」；
> 端点/工具面契约见 [architecture.md](architecture.md)；上游数据坑见 [TOOL-LIMITS.md](TOOL-LIMITS.md)。

| 工具 | 层次 | 入口 | 产物 |
|---|---|---|---|
| 后端端点全量扫描 | 服务（HTTP + SQLite + 文件系统） | `~/.dsh/trading-venv/bin/python scripts/e2e_workbench.py` | `~/.dsh/logs/e2e-workbench-<ts>.json` |
| 前端真浏览器 E2E | 浏览器（系统 Chromium + CDP，零依赖） | `node scripts/e2e_web.mjs` | `~/.dsh/logs/e2e-web-<ts>/{report.json,*.png}` |

两个 harness 都是**只报告、不修复**：发现缺陷后由人工/代理按 findings 修服务或页面，
再用同一命令复跑判闭环（判据见 §四）。

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
  已按此口径收敛（详见 §三）。
- **锚点是状态感知的**：`required` 条目可以是字符串（必须出现）或数组（数据态/空态任一
  满足）。例：`plan` = `["当前计划","暂无计划"]`（空库时前者不会渲染，是**正确**状态）；
  `settings` 无页面级标题，以卡片标题为锚（`["富途 OpenAPI 凭据","自动流水线"]`）。
  报告里记录命中的是哪一支（`data`/`empty`）。
- **只读**：不点击任何提交类控件、不改服务状态、不装依赖。

---

## 三、本轮结果与缺陷清单

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

## 四、复验方法（判闭环）

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
| S2 / M1 | 写阶段「撤单后 OMS 回写(S2)」cleared=true；「撤单后同标的同方向仍被阻塞」消失；`place→modify` 后旧单落 `cancelled`（修复 `f86783f`，**待服务重启生效**）。**验证前提**：扫描前无幽灵单（见 §三「幽灵在途单」） |
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

## 五、已知未覆盖（诚实清单）

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
