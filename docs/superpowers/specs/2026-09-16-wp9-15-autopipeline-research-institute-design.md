# 量化平台一次建成：sim 全自动流水线 + 流程可视化 + 富途数据面补全 + 研究院闭环 + 值班研究员（WP9–WP15）

> 状态：设计已与用户逐项确认（2026-09-16），待规格审查后进入实现计划。
> 前序：WP1–WP8（数据基座/研究层/执行闭环/调度/独立服务/独立平台/OpenAPI 统一）已交付。
> 缺口依据：§附录 A 的富途 OpenAPI 覆盖审计（2026-09-16，对照 `open.futunn.com/zh-cn/llms.txt`
> 与官方 API 参考逐一核对）。

## 一、背景与已确认的决策

现有平台的组件层已齐（PIT 数据、因子、回测、计划冻结、风控 8 规则、OMS、对账、TCA、
审计链、OpenAPI 交易通道），但存在三类缺口：

1. **日常闭环断成三截**：调度链只到数据同步/因子快照为止；计划生成靠手工内联权重；
   计划执行 sim/live 都要人工点击。
2. **流程不可见**：工作台各页是信息孤岛，看不出「今日闭环跑到哪一步」。
3. **数据面覆盖不全**：交易链路 13/13 全覆盖，但研究数据面（F10 深度数据、股票筛选、
   板块、做空、IPO）覆盖率仅约 15%，且复权/财报/估值/分红/经济日历仍绕道托管 MCP，
   通道统一没有闭环（详见附录 A）。

本规格把三类缺口一次补齐，并把 Harness 升级为「研究院大脑」。

设计前用户已确认以下决策：

| 决策点 | 选择 |
|---|---|
| sim 自动化范围 | **全链路**：策略驱动计划生成 + 自动执行，全程无人点击；live 计划同样自动生成，但执行等人工 |
| 自动执行时点 | **次日开盘后延迟执行**（每市场可配置，如 SH 09:35）；风控规则 3 决定了收盘后生成的计划当天无法成交 |
| 流程可视化 | **新增「流程」页签**：每市场一条端到端阶段链，可下钻现有页面 |
| 总开关 | `auto_pipeline.enabled` **默认 false**；不显式打开不改变任何现有行为 |
| 接入架构 | **作业链 + 指令文件**：auto_execute 到点后写与人工点击完全相同的 execute_plan 指令，复用既有窄门、processed/ 留痕与幂等 |
| 资讯/情绪数据 | **PIT 快照落库攒历史** + 演进条款（满 250 交易日启动检验，过门槛转正）；不上来就进信号 |
| 数据面缺口 | **全部补齐**（例外见 §1.1）：研究富矿端点接入 + sync 通道迁移 + 模拟交易 REST 化 |
| 研究院定位 | **Harness 研究院闭环**：子代理采集资讯 + 生成因子/规则假设 → 声明式规则提案 → 机械验证门 → 人工批准启用 → 既有流水线执行 |
| 信息源 | **三分采集面**：fin_news/fin_sentiment（富途常规）+ 富途 OpenAPI（公告/资金流/筹码/F10）+ last30days（社媒广度，可选组件，桥接纪律沿用） |
| 任务分层 | **三层模型**：L1 常驻机械调度（无 LLM）+ L2 Harness 即时任务（对话发起）+ L3 定时 Harness 研究任务（值班研究员，WP15） |
| L3 触发方式 | **外部定时器 + headless 单次运行为主（A），任务队列开机补跑兜底（C）**；不用会话内 `dsh-schedule`（投递需会话常驻、仅支持固定间隔无日历规则） |
| 交付方式 | **一份规格，WP9–WP15 顺序交付**，一次建成；每个 WP 独立验收，全程仓库全绿 |

核心架构原则（全规格的不变量）：**LLM 在研究侧自由，在信号侧匿名**——Harness 负责
研究什么、采集什么、如何解读，产出经人批准后成为确定性规则；规则一旦上岗即可回测、
可复现、可审计；LLM 永远不直接产生订单、不写代码进核心库、不能自批自己挖的因子。

### 1.1 缺口补全的例外（明确记录在案）

「全部补齐」指**本项目三市场（A 股/港股/美股）证券与期权范围内的官方功能端点**。以下两类
例外在附录 A 逐条登记，不在本规格实现范围：

| 例外 | 理由 |
|---|---|
| **加密货币全族**（`crypto_trading/*`：账户、下单、订单、成交、推送） | 独立资产类别与账户体系，超出本项目声明的市场范围（三市场证券/ETF/期权）；纳入会引入新的交易通道、风控口径与准入评估，属范围扩张而非缺口补全。**如用户要求，另立规格。** |
| **窝轮/牛熊证策略化使用** | `warrant-screen` 作为**数据端点**接入（保持 API 面完整），但平台策略、风控与执行不引入窝轮品类 |

## 二、非目标（明确不做）

1. **不改 live 任何确认环节**：点执行 + 口令「确认执行」+ Web 确认卡片，全部不变。
2. **不做盘中实时/高频**：自动执行是日频时点动作，频率上限沿用 WP7 边界。
3. **不做逐单自动改撤**：熔断撤余单沿用 execute.run 现状，不新增自动改单逻辑。
4. **不让 LLM 写代码进核心库**：研究产出只能是声明式规则 JSON；需要新算子时走人工
   代码审查的核心库 PR，不提供动态执行通道。
5. **不自动批准**：候选规则的批准按钮只在 Web，不进 MCP 工具面（同 confirm-decide
   与设置端点先例）——Harness 不能自批。
6. **不做通知推送新渠道**：沿用 alerts 表 + 可选桌面通知（配置默认关）。
7. **不引入加密货币**（§1.1）与**不策略化窝轮**（§1.1）。
8. **不用会话内 `dsh-schedule` 实现 L3**：其投递依赖会话常驻、且只支持固定间隔
   （无「每交易日 16:40」日历规则），无法承担无人值守的每日研究作业（§十）。
9. **不让服务进程触发或承载 LLM**：L3 唤醒一律由外部定时器拉起 DSH 自身
   （`dsh --profile headless`），服务只提供队列读写端点（§3.1 第三条）。

## 三、总体架构

```
┌────────────── 研究院（Harness + 子代理，LLM 自由区，WP14）──────────────┐
│ 采集代理群：公告/资金流/F10（OpenAPI）+ fin_news/fin_sentiment + last30days│
│ 假设代理：资讯 + PIT 数据 → 候选因子/交易规则（声明式 JSON 提案）           │
│ 检验代理：跑 ic/t检验/分层/walk-forward → 验证报告                        │
│ 研报代理：research_publish → 工作台研究页（人工随时对话介入）               │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼ 声明式规则提案
        ┌──── 验证门（机械，WP14 补全 t 检验/分层/半衰期/换手）────┐
        │ 未满 250 交易日的资讯因子不得引用；检验不过永留候选池       │
        └───────────────────────────┬─────────────────────────────┘
                                    ▼ passed
        候选池（工作台研究页）→ 人工一键批准 = 启用（sim 生效）
                                    ▼
┌── 日常流水线（服务内调度器，无 LLM，WP9）────────────────────────────┐
│ 收盘后链：sync → fundamentals → merge → quality → factors_snapshot    │
│         → sentiment_snapshot(WP11) → build_plan（策略→冻结，双模式）   │
│ 次日开盘+延迟：auto_execute（仅 sim；写 execute_plan 指令 → 既有窄门）  │
│ 晚间：reconcile → tca → digest（补齐规格 §8.1 未入链部分）             │
│ 每轮 tick：commands.poll 指令轮询并入（修复单进程断点）                  │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
        风控 8 规则（唯一提交入口）→ OMS → broker（sim/live 均 OpenAPI，WP13）
                            ▼
┌── 流程页签（WP10）───────────────────────────────────────────────────┐
│ 每市场一条阶段链：sync/quality/factors/plan/execute/reconcile/digest   │
│ 只读既有事实（ran 标记 + 表），不造状态；auto_pipeline 开关徽章         │
└─────────────────────────────────────────────────────────────────────┘
        ▲ 数据供给：WP12 数据面补全（F10/筛选/板块/做空/IPO/经济日历/自选）
        ▲ 通道统一：WP13（sync 迁 REST + 模拟交易 REST + 工具面治理）
```

### 3.1 三层任务模型（本规格的组织主线）

平台的任务按「谁触发、谁执行、有没有 LLM」严格分三层，**层间只经文件/API 交换数据，
不相互调用**：

| 层 | 触发源 | 执行体 | LLM | 产物 | 规格位置 |
|---|---|---|---|---|---|
| **L1 常驻机械调度** | 交易日历 + 60 秒 tick | 服务内调度器（无 LLM） | ❌ | 数据、信号、计划、订单、对账、digest | WP9 / WP11 / WP12 / WP13 |
| **L2 Harness 即时任务** | **人在对话里发起** | Harness 主会话 + 子代理 | ✅ | 研报、因子提案、规则候选 | WP14 |
| **L3 定时 Harness 任务**（值班研究员） | 外部定时器（headless 唤醒） | Harness headless 单次运行 | ✅ | 每日资讯简报、因子巡检、挖掘轮提案 | WP15 |

**不可越界的三条**：

1. **L1 永不调 LLM**——信号侧必须可回测、可复现；LLM 结论无法回测，故只能出现在
   研究侧（这也是「情绪/资讯不进因子库」的同一原则的另一面）。
2. **L2/L3 永不直接下单、永不直接启用策略**——研究产出必须过验证门（WP14 §9.3）+
   人工批准（WP14 §9.4）才成为规则；kill/熔断对研究侧无例外地一票否决执行。
3. **服务进程永不承载 LLM 循环**（architecture.md 既有「明确不做」）：L3 的唤醒由
   **外部定时器拉起 DSH 自身**（`dsh --profile headless "<任务>"`——回答一个任务、
   打印结果、退出）完成，服务进程只提供任务队列的读写端点，不持有模型调用。

## 四、WP9：sim 全自动流水线

### 4.1 配置（trading-platform.json 顶层新键）

```json
"auto_pipeline": {
  "enabled": false,
  "strategies": [{"market": "SH", "strategy": "watchlist_rsi", "watchlist": "watchlist"}],
  "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
  "exec_window_minutes": 30,
  "reconcile_at": "19:00"
}
```

- 缺省无此键 = 功能关闭，现有行为零变化（硬约束）；
- **每轮 tick 现读配置**（platform_config 已是每轮读文件的既有口径），改配置约 60 秒
  内生效，无需重启服务；
- `exec_at` 为北京时间；美股受夏令时影响，默认值按夏令时写，文档注明切换需人工调整
  （不做自动 DST 换算——诚实简单）；
- `exec_window_minutes`（默认 30）：**执行窗口**——见 §4.3 守卫 9（补齐 tick-first
  补跑语义的必然要求）；
- `strategies[].market` 决定**市场范围**（SH 链含 SZ/BJ），策略的分母与
  `max_positions` 截断都在市场过滤之后——跨市场合并计数会让先排序的市场吃光名额
  （2026-09-16 修订 K1）；
- `strategies[].watchlist` 是**池键名**（可省略，缺省 `watchlist` = 配置顶层既有的
  扁平列表）：它**不决定市场范围**，只选择关注池；显式指定的池键不存在 → 该策略当日
  软跳过 + warn 告警（fail-closed，不静默换池子）（2026-09-16 修订 I1）。

**作业分层（实现期补记，2026-09-16）**：调度链作业分两类，`enabled` 只控制后者——

| 层 | 作业 | 受 `enabled` 控制 | 理由 |
|---|---|---|---|
| **基础链**（常驻） | 既有 `sync_bars`/`fundamentals`/`merge_announcements`/`quality`/`factors_snapshot` + WP11 `sentiment_snapshot` + WP12 `research_snapshot` + WP15 `enqueue_research` | ❌ 不受控 | 数据与研究资产的**积累**与交易无关：资讯/F10/做空因子要攒满 250 交易日才有意义，不能等用户打开交易开关才开始计时；研究任务同样独立于交易开关（§十） |
| **交易链**（开关控制） | `build_plan`/`auto_execute`/`reconcile`（含 digest） | ✅ 受控 | 涉及计划生成与真实下单，属交易行为 |

「关闭时行为零变化」的准确含义因此是：**交易行为**零变化；基础链上的数据采集与研究
工作照常（它们不产生订单、不触达任何交易写路径）。

### 4.2 build_plan 作业（各市场链尾追加，factors_snapshot 之后）

**日期空间（实现期修订，2026-09-16 补记）：数据就绪门按「市场本地会话日」判定，
不用北京日。** 作业按北京时间触发，而 bars 与交易日历按**市场本地日期**落库——美股链
两者相差一天（ET 会话收盘 = 北京次日 04:00/05:00）。原实现把北京日直接当本地日用，
`expected`（应有最后交易日）因此永远超前一天，**美股链数据就绪门每天静默跳过、美股
自动计划永不生成**（港股与北京同时区，未受影响；实测复现：北京作业日跑 US 链恒返回
「数据未就绪」）。修法引入唯一实现 `planner.data_date_for(market, 北京时刻) -> 本地会话日`：

- **市场会话收盘的北京时刻表**：沪深 15:00（当日）、港股 16:00（当日）、**美股 05:00
  （北京次日）**——美股取 EST 最晚界（EDT 实为 04:00），两制下都已收盘，**DST 无关且
  偏保守**（宁可晚 1 小时判定就绪）；
- 时刻早于该会话收盘 → 作业**保守跳过**（本次会话未收盘：不拿上一场的收盘当本场，
  否则当日 ran 标记被消耗、真到收盘后不再补跑）；
- 候选本地日在日历里取 ≤ 的**最近交易日**（周末/节假日回落到上一场——北京周一早上的
  补跑因此落到上周五，正是美股链需要的语义）；
- 日历未同步 → 保持「宁可不跑」fail-closed；纯日期注入按「当日已过完」解释（``today``
  注入口径不变）；
- 就绪门与下游（过期语义、策略 `as_of`、PIT 取价、计划 `as_of`）一律使用该本地会话日；
  北京日只保留「休市不生成」判定与告警文案用途。沪深/港股与北京同日 → 行为逐字不变，
  由 `test_sh_hk_gate_unchanged_by_date_space_fix` 与 `data_date_for` 单元用例回归锁定。

1. **数据就绪门**：对关注池标的按日历校验 bars 新鲜度（复用 quality.freshness/gap_report
   口径）；不新鲜 → 跳过当日 build_plan + warn 告警（宁缺毋假，延续 §4.3 原则）；
2. **过期语义落地**：生成新计划前，将 `origin='auto'` 且 `status='frozen'` 且
   `as_of < 今日` 的旧计划置 `cancelled`（补齐原规格 §6.1「跨日计划 expired」从未实现
   的语义；**只动 auto 计划，手工计划不碰**）。**订单同步作废（实现期修订 I2，
   2026-09-16）**：计划冻结时订单已登记为 `draft`，只改计划状态会留下孤儿单（计划
   cancelled 而订单在途，OMS 台账自相矛盾、页面显示幽灵单）；故经 `oms.cancel_pending`
   把未提交订单一并置 cancelled（在途订单不动，留给对账）。该口径与工作台
   `cancel_plan`、熔断撤余单共用**同一实现**——三处各写一遍正是该缺口的成因；
3. **策略产出权重**：`strategy.target_weights(conn, as_of)`，as_of = 最近已收盘交易日；
4. **计划生成**：`planner.build_and_freeze(conn, mode, strategy_id, target,
   broker_positions, prices, as_of)`——价格用本地库 PIT 最近收盘（不盘中取数），持仓经
   broker 只读查询（planner 既有口径：current_positions 一律从券商查询）；
5. **定量口径（实现期补记，2026-09-16）：策略尊重风控上限 + 计划按风险预算定量。**
   实测发现「按权重全额定量」与风控规则 4/5 结构性冲突（`stop_dist=None` 时规则 4 按
   全额名义算风险 → 权重 1.0 必拦；权重 0.5 被规则 5 单票 25% 上限拦），即**自动流水线
   无法成交任何订单**。修法**不改动任何风控规则**（硬拦截语义不动），而是让定量与风控
   自洽：
   - **策略侧尊重上限**：`watchlist_rsi` 等组合策略的单标的权重上限取
     `risk_config` 的 `max_position_pct`（默认 0.25）、持仓数上限取 `max_positions`
     （默认 5），超出部分留现金——策略不再生成规则 5 注定拒绝的目标；
   - **计划侧按风险预算定量**：`qty = min(权重定量, floor(权益×risk_per_trade ÷ 止损
     距离 ÷ lot)×lot)`，止损距离 = `stop_atr_mult × ATR(14)`（本地 PIT bars 自算，
     与引擎快路径「1% 风险 ÷ 2×ATR」同一哲学）；执行侧 `stop_dist_of` 传入**同一
     止损距离**，使规则 4 按真实单笔风险判定（自洽：计划按该距离定量，规则按该距离
     校验）；
   - ATR 不可得（bars 不足）→ 该标的跳过并在结果标注（宁缺毋假），不做「无止损全额
     定量」这种必被拦的徒劳计划；
   - **权重降级为上限**：实际成交名义可能小于目标权重（风险预算约束所致），这是刻意
     的保守取向，界面/摘要如实呈现。
6. **受管集合与退出路径（实现期补记，2026-09-16）**：planner 只遍历 `target` 键，
   策略不再返回的已持仓标的永远不生成 SELL——**自动流水线只买不退**。
   修法：引入**受管集合**（managed set）= 该市场关注池 ∩ 策略 universe；计划对
   `managed ∪ target` 求 diff，`managed` 中缺席者视为目标权重 0（清仓），
   **受管集合之外的持仓一律不动**（不清理用户手工持有的标的）；
   **目标外清出修订（2026-09-17，实现期补记）**：上一条的「不动受管集合之外的持仓」与
   `max_positions` 合起来会形成**结构性死锁**——实机 A 股模拟账户持 8 只（多在关注池
   之外）、`max_positions=5`，策略既不买（新增建仓被风控规则 6 拦）也不卖（8 只全在
   `managed` 之外），账户与策略组合永远不收敛。本修订加一个**默认关闭**的顶层开关
   `exit_outside_target`（同文件顶层，与 `futu_channel`/`watchlist` 同级）：
   - **只在自动计划路径生效**（`planner.plan_auto`）；手工 `plan-build` 语义不变；
   - 关闭（键缺失=false）时逐字不改变任何既有行为；键存在必须是真布尔，字符串/数字/`null`
     一律 `ValueError`，`plan_auto` 按 fail-closed **软跳过当日计划 + warn**
     （作业契约「永不抛」，与 `risk_config` 非法的既有处理同分级）；
   - **收敛集合** = `{券商持仓} − {当日 target 的键}`，作为显式 `exit_symbols` 传给
     `build_and_freeze`（"这些标的目标权重按 0 处理"）——它是 `managed` 的**补集**：
     `managed` 管「策略负责范围内掉出 target 的」，`exit_symbols` 管「策略根本不负责、
     但券商账上有的」；
   - **硬守卫**：`target` 为空时一律不收敛 + warn「目标为空，未执行目标外清出」——
     「策略今日选不出标的」绝不能变成「清空全部持仓」；
   - **无本地 K 线时的价格回退**（实机：这 8 只在本地 `bars` 表 0 行，`_last_close`
     返回 None，而 `build_and_freeze` 遇无价会跳过 → 不补价就一张清仓单都没有）：优先本地
     最近收盘，其次券商持仓**标记价**（sim `cur_price` / live `nominal_price`，与
     `plugins/workbench/python/positions.py` 同一字段口径），来源在返回值
     `converge.prices[sym].source` 与告警 detail 里如实标注（`close` / `broker_mark`），
     **绝不把券商标记价伪装成本地收盘价**；两处都拿不到 → `skipped`（`SYM(无价,无法清出)`）
     且不生成订单；`cost_price`/`mv` 不作价格用（成本基准与市值都不是标记价）；
   - **可卖数量**：券商给了可用数量（sim `qty_avbl` / live `can_sell_qty`；设计初稿写的
     `available` 作为次级候选一并接受）时卖出量取 `min(qty, available)`；`available=0`
     记 `skipped`（`SYM(T+N不可卖)`），**不生成注定被拒的单**；字段缺失按既有口径用全部 qty；
   - **形状兼容**：`broker.positions_equity_cash(..., with_marks=False)` 缺省仍是
     `{symbol: {"qty": n}}`（既有调用方与 153 处断言逐字不变），`with_marks=True` 才附带
     `price`/`mark_field`/`available`；`build_and_freeze(exit_symbols=None)` 缺省时返回体
     **无** `exits` 键、diff 符号集与顺序不变；
   - **两步生效的现实**：执行侧 `ctx` 是**静态持仓快照**（`execute.run` 不逐单重查），
     所以「同一份计划里卖出 8 只 + 买入新股」时买入仍会被规则 6 拒——这是**预期行为**
     （不为绕过规则 6 伪造持仓数）。当日计划把账户清到策略组合，**下个交易日**快照回落
     后新计划自然能建仓。
7. **执行侧 ctx 用真实持仓（实现期补记，2026-09-16）**：`_execute_plan` 的 ctx 原为
   离线保守默认（`positions_value={}`、`positions_count=0`），导致规则 5 对 SELL 把
   `after` 算成 `0 + qty×price` → **退出单被判成新建大仓位而拒**（自动流水线能加仓、
   不能减仓）。修法：ctx 的 `positions_value`/`positions_count` 从券商持仓查询填充
   （台账二分原则：一律券商事实，不读本地台账）；查询不可得时保留原离线保守默认并
   在结果中如实标注（既有行为不回退）。
   **实现期补记（2026-09-17，实测算术修正）**：仅「把真实持仓市值填进 ctx」**不足以**
   达成上述目标——规则 5 的判定式 `持仓市值 + 本单名义` 只在**买入**方向成立，填真实
   市值后卖出反而更严。实测（权益 100 万 / 单票上限 25% / 持仓 1700 股 × 200 元 = 34 万
   全卖）：空表 `after = 34 万`（拒）、真实市值 `after = 68 万`（更严地拒）、成交后口径
   `after = 0`（放行）。因此修法是**两层**：
   - `daemon._positions_ctx`：持仓市值/持仓数取券商事实；**任一市场查询失败即整体退回
     离线保守默认**并标 `ctx_source="offline_default"`（半个持仓表比没有更危险——缺失的
     存量会被规则 5 当成 0，从而放行叠加超限的单）；
   - `execute._order_ctx`：**仅当** `ctx_source=="broker"` 且该标的持仓已知时，对 SELL
     把 ctx 持仓市值折算为「成交后剩余市值 − 本单名义」，使规则 5 的结果恒等于真实
     成交后市值（清仓为 0、减仓为剩余）。持仓未知时不折算：离线口径保持「按本单全额
     名义」判定，退出大仓位仍被拒（保守不回退）。
   折算只作用于卖方向——买入叠加超限、以及**减仓后仍超上限**两种情形照旧被拦
   （三条反证见 `tests/test_wp9_exec_ctx.py`）。`risk.py` 一行未改；把规则 5 本身改为
   方向感知是更干净的长期修法，但需先解冻该文件（登记为后续项）。
8. **双模式都生成**：mode 取当前模式文件。sim 计划等待次日 auto_execute；live 计划
   冻结后等待人工执行（工作台计划页可见，流程与今日一致）。

### 4.3 auto_execute 作业（exec_at 时刻，交易日触发）

**九项守卫，全部满足才写指令；任一不满足 → 跳过 + info 告警（如实留痕）：**

1. `auto_pipeline.enabled == true`；
2. 模式文件 == sim（live 跳过并告警「计划等待人工执行」——自动生成≠自动执行）；
3. 无 kill 文件（`~/.dsh/trading-kill` 不存在）；
4. 无 halt（OMS 熔断状态为清）。**可见性与恢复口径（实现期修订 I3，2026-09-16）**：
   halt 生效时守卫**以 warn 级跳过并带原因与设置时间**（原先 info 级会让自动链路
   静默停摆、页面上看不到任何异常），且 `daily:digest` 记录 `halted`/`halt_reason`；
   **不自动恢复**——清除 halt 永远由人工 `clear_halt` 决定（先查明原因；对账零差异
   也不清除既有 halt）；
5. 存在 `origin='auto'` 且 `status='frozen'` 且 `as_of == 最近已收盘交易日` 的计划；
6. 该计划的市场 == 当前作业市场（per-market 计划，见 4.6）；
7. 该计划今日未被执行过（kv ran 标记幂等，同 daemon 既有口径）；
8. `expected_mode` 复核通过（写指令时携带，指令处理侧既有复核兜底）；
9. **执行窗口**：当前时刻在 `exec_at` 起 `exec_window_minutes`（默认 30）之内。

守卫 9 的必要性（实现期发现，2026-09-16 补记）：调度器是 **tick-first**——启动时补跑
当日已到期作业。若服务在 16:20 启动，SH 的 09:35 `auto_execute` 会被补跑：收盘后执行
导致风控规则 3 逐单拒单、当日计划被消耗；盘中晚补则按错误时点价格成交。窗口外的计划
**不执行、留待人工**（info 告警「已超执行窗口，计划等待人工执行」），符合 §11.1
「宁可跳过 + 告警，不冒险执行」的保守取向。

守卫 5 的 `window=2` 容差（实现期补记，2026-09-16）：守卫 5 的实现取**最近两个**交易日
`[D, D−1]` 作为 `as_of` 候选（`planner.recent_trading_days(..., window=2)`），而不是规格
字面的「最近已收盘交易日」单值。原因是**跨市场日期空间**：`auto_execute` 与 build_plan
的日期空间相差一天（美股计划 `as_of = ET D−1`，而北京 D 的「最近交易日」按日历算是
ET D）。逐候选回退保证「按本地会话日冻结的计划」在两种解释下都能被找到，**否则美股链
在守卫 5 处会二次跳过**（就绪门修好、门后仍找不到计划）。影响有界：正常路径（`exec_at`
当日 09:35–10:05）不存在 `as_of = D` 的 auto 计划（当日计划要收盘后才生成），因此第一个
候选恒为空、实际命中的仍是 `D−1`；`D−2` 及更早的计划已被守卫 5 的窗口**拒绝**
（不会拿过期计划执行）。日期空间修正（§4.2）后 `as_of` 语义统一为本地会话日，本容差
继续作为跨日期空间的兼容层保留，**不改守卫语义**。

通过后 `commands.write_command(home, "execute_plan", {plan_hash, expected_mode})`——
**与人工点击落完全相同的指令文件**，由指令轮询处理，processed/ 留痕、nonce 幂等、
逐单风控 8 规则全部复用。自动执行 = 系统代替人点击，不是旁路。

### 4.4 reconcile / digest 入链

`reconcile_at`（默认 19:00）追加 `reconcile → tca → daily_digest` 作业——补齐原规格
§8.1 写明但一直未入链的「每日固定对账」；与执行动作解耦（晚间固定跑）。

**对账口径（实现期修订，2026-09-16 补记）：先同步券商事实，再判差异。**

- **为什么**：sim 通道成交不经 WS 交易事件（WS 只覆盖 live OpenAPI 路径），本地 OMS
  台账不会自己知道成交与状态；若对账只做纯比对，会把「本地尚未学习」系统性误报为差异
  → 每日 critical + halt → **自动执行被自己的对账噪声永久熔断**（实现期实测，非推测）。
- **同步两步**（只读券商、只写本地，绝不向券商写、绝不重放）：
  1. **成交回填**：从券商订单历史的累计成交量与均价回填 `fills`（差额口径、确定性
     `fill_id` 指纹幂等；均价缺失则**不回填**、缺口如实暴露——宁缺毋假）；
  2. **状态推进**：按累计成交量推进 OMS 订单状态（足额→`filled`、部分→`partial`），
     **数量口径优先于状态文本**（延续 WP8 P1 遗留项的保守口径），只前进不回退。
- **判定**：同步后仍存在的差异才是真差异 → critical 告警 + `set_halt`（**只暂停后续
  执行，绝不自动平仓**）；券商有持仓但本地无任何足迹的记 `untracked`，不计差异
  （历史存量持仓的自然结果）。
- **通道范围**：本期 sim（对齐 sim 全自动的目标）；live 对账经工作台端点，WP13 通道
  统一时升级——live 跳过并 info 告警，不猜不编。

### 4.5 指令轮询并入服务内调度器（断点修复）

现状：`commands.poll` 只在独立 daemon CLI 常驻模式里跑；服务内调度器只跑作业链。
**若独立 daemon 未运行，plan-execute 写下的指令永远无人处理**——这是实测确认的集成
断点，也是全自动的前置条件。

修复：服务内调度器每轮 tick 末尾调用 `commands.poll`（handler 与 CLI daemon 同一
分派函数）。双进程并发安全沿用既有协议：文件原子写 + processed/ nonce 去重 + 同名
文件 replace 原子性；CLI daemon 保留为手动/兼容入口，同日作业靠 kv ran 标记不重复。

### 4.6 计划模型小扩展

- `plans` 表幂等追加两列（沿用 store.py 既有幂等 ALTER 模式，SCHEMA_VERSION 不动）：
  - `origin TEXT NOT NULL DEFAULT 'manual'`——auto 计划与手工计划的来源隔离依据；
  - `market TEXT`——auto 计划一计划一市场（匹配 per-market 作业链与 exec_at）；
    手工计划留空，完全兼容现状。
- `strategies.py` 补 watchlist 组合适配器（`watchlist_rsi` 等）：扫指定市场关注池，
  信号 BUY 等权、其余现金；现有 rsi/ma_cross 单标的策略不动。

### 4.7 WP9 验收标志

- 假时钟单测：build_plan 触发/数据不就绪跳过/旧 auto 计划过期；auto_execute 九守卫
  逐项跳过路径（含执行窗口外不执行）+ 正常路径落指令；live 模式绝不自动执行；
  kill 存在绝不执行；
- 端到端：sim 模式下（enabled）假时钟连跑 2 个模拟交易日——收盘链自动生成冻结计划，
  次日 exec_at 自动写出 execute_plan 指令，指令轮询处理，订单经风控落 OMS，晚间
  reconcile/digest 完成，全程零人工；
- 开关关闭时行为与现状逐字节一致（回归）。

## 五、WP10：流程页签

### 5.1 pipeline 端点（POST /api/wb/pipeline 空载荷，只读）

> 措辞修订（实现期，2026-09-16）：规格初稿写作 `GET /api/wb/pipeline`；实现与仓库既有
> 约定一致——只读快照类端点（`plan`/`schedule`/`reconcile`）一律走
> `POST /api/wb/<endpoint>` 空载荷 + envelope 契约，故 pipeline 同此口径（`GET` 专用
> 路由仅 `openapi_config`/`auto_pipeline` 两个设置读端点使用）。

每市场返回今日各阶段状态，来源全部是既有事实，**只读不造**：

| 阶段 | 事实来源 |
|---|---|
| sync / fundamentals / quality / factors / sentiment / build_plan / auto_execute / reconcile / digest | kv `daemon:state` ran 标记（作业名+时间）+ 当日 alerts（失败/跳过原因） |
| plan | plans 表最新计划（status/as_of/origin/market/订单数） |
| execute | orders 表按计划聚合的状态分布 |

每阶段 `{status: pending|ok|skipped|failed, at, summary}`；响应含 `auto_pipeline`
当前配置摘要（enabled/strategies/exec_at）。缓存 TTL 30s。

### 5.2 Web 流程页（pages/pipeline.jsx）

- 每市场一条 antd Steps 横向链，状态着色（完成/跳过/失败/待运行），
  点击阶段下钻到计划/调度/审计页；
- 页头：模式徽章（SIM/LIVE）+ auto_pipeline 开关徽章；
- 开关**在设置页**（settings 扩展，见 5.3），流程页只显示开关状态徽章；设置页开关区
  明确提示「关闭时流水线不自动运行」（措辞修订，实现期 2026-09-16：初稿「页面上」指代
  不清，实现按计划把开关落在设置页）。

### 5.3 auto_pipeline 开关（settings_api 扩展）

- `GET/POST auto_pipeline`（载荷白名单 + 结构校验 + 原子写，模式与 openapi_config
  完全一致）；**两端点不进 MCP 工具面**（同 openapi_config/openapi_test/confirm-decide
  先例）——模型不能自拨开关。

### 5.4 WP10 验收标志

- 端点形状测试（envelope 契约 + 阶段状态推导的假库用例）；
- 前端渲染 + 端点声明自检清单更新（endpoints 数组加 pipeline/auto_pipeline）；
- 开关闭环：页面切换 → 配置文件变化 → 下轮 tick 生效。

## 六、WP11：资讯 PIT 地基

### 6.1 sentiment_snapshots 表 + 每日作业

- 新表 `sentiment_snapshots(date, symbol, source, payload, fetched_at)`——**只存原始
  事实（原文摘要/声量计数/渠道原文），不打分**（打分算法会漂移，原始数据不会；
  PIT 一致性优先）；
- 每日作业 `sentiment_snapshot`（链尾，factors_snapshot 之后）：对关注池标的并行采集，
  逐源落库、逐源标注：
  - **fin_sentiment / 千股千评**（fin-data）：A 股常规情绪通道；
  - **fin_news**（fin-data 的新闻路由：futu → AKShare → Yahoo）：资讯面快照。
    **修订（实现期 2026-09-16）**：初稿写 `find-news`/`find-community`（上游
    `quote_news_search`/社区端点），但该通道**实测恒空**（`docs/TOOL-LIMITS.md` 三种参数
    均 `data: []`；`docs/HANDOVER.md` 明示「取新闻用 fin_news」）——接恒空通道等于造一个
    永远为空的源，违背不编造纪律。故改用 fin_news，并保留 `news_call` 注入缝：WP12 若
    实测官方 find-news 有数据，经该缝接回。公告类沿用既有 announcements 通道
    （WP1 merge-announcements 已落 `announced_at`），本作业不重复采；
  - **last30days**（可选组件）：`--emit=json` 结构化输出落库；未安装/未配密钥的来源
    **缺席标注而非报错**（上游降级语义），桥接纪律原样沿用：证据必须带平台/时间/
    互动数三要素，不经富途、不经交易闸门；
- 失败单源跳过并告警，不阻塞其他源；全部失败 → warn 告警（不阻塞后续 build_plan——
  情绪数据目前不进信号，采集是攒历史，不是当日依赖）。

### 6.2 查询与展示

- `sentiment-history` 查询端点（按标的/日期倒序，同 factors-history 三路同源模式）；
- 流程页情绪阶段展示「已连续积累 N 天」；因子页增加情绪快照查看入口。

### 6.3 演进条款（写死在规格里）

`sentiment_snapshots` 某因子候选**连续积累 ≥250 交易日**后，可由研究院提出检验申请，
走 WP14 验证门——与价格因子同一套门槛（IC t 检验显著 + 分层单调 + walk-forward OOS），
**过门槛转正为正式因子，不过就继续攒或放弃，不搞特殊通道**。转正前，资讯因子不得
出现在任何启用规则的 factors 列表里（规则解释器机械拒绝未注册/未通过检验的因子引用）。

### 6.4 WP11 验收标志

- 落库/降级/查询单测；last30days 缺席时的降级语义测试；
- 假时钟：作业入链后 sentiment_snapshots 按交易日累积、流程页阶段联动。

## 七、WP12：富途 OpenAPI 数据面补全

> 目标：把附录 A 列出的**研究数据缺口全部接入权威 OpenAPI 通道**，让 WP14 研究院
> 开工时数据已就位。所有端点逐条对照官方文档实现；**实施时以官方文档参数为准**
> （附录 A 给出每项的官方文档 URL 与已验证/待核对标记）。

### 7.1 传输层扩展（trading_datasource.futu_openapi）

新增方法组（与既有 `OpenApiQuote`/`OpenApiTrade` 同构：参数白名单 = 方法签名、
枚举/区间本地校验、错误码映射复用 `parse_envelope`）：

| 方法组 | 覆盖端点 | 方法与路径（已验证 / 待核对） |
|---|---|---|
| `OpenApiScreen` | 股票筛选、窝轮筛选 | `POST /api/v1.0/quote/stock-screen` ✅（`screen_queries`/`retrieve_queries`/`sort`/`sorts`/`next_key`/`limit`≤300/`watchlist_stock_ids`/`holding_stock_ids`/`user_stock_list_mode`）；`POST /api/v1.0/quote/warrant-screen`（待核对） |
| `OpenApiPlate` | 板块列表、板块成份股 | `GET /api/v1.0/quote/plate-list` ✅（`market`+`plate_class`∈{ALL,INDUSTRY,REGION,CONCEPT,OTHER}；REGION 仅 SH/SZ，否则 `-8 unsupported`）；`GET /api/v1.0/quote/plate-stock`（待核对） |
| `OpenApiShort` | 每日卖空成交、空头持仓 | `GET /api/v1.0/quote/{symbol}/short/daily-volume` ✅（`count`≤90；仅 HK/US 可卖空证券；无数据 `-10 no_data` 视为空而非错）；`.../short/interest`（待核对） |
| `OpenApiF10` | 个股深度数据 23 项 | `/api/quote/financials/*` 族（官方 api-reference 归类为「个股深度数据」；**llms.txt 的 `/f10/*.md` 链接已 404，实施时按 api-reference 的真实路径逐一核对**） |
| `OpenApiBasicData` | 经济日历热门/搜索、证券搜索、所属板块、复权因子 | `economic-calendar/hot`、`economic-calendar/search`、`search`、`{symbol}/owner-plate`、`{symbol}/rehab`（路径待核对；后两项现经托管 MCP） |
| `OpenApiDerivatives` | 期货信息、期权波动率、行权概率、相关期货 | 期货/期权族补充端点（路径待核对） |
| `OpenApiIpo` | IPO 列表 | `GET /api/v1.0/quote/ipo-list`（待核对；支持 HK/US/CN/MY/SG） |
| `OpenApiWatchlist` | 自选列表、自选分组 | `GET` 自选族（待核对） |
| `OpenApiSimTrade` | 模拟交易 9 端点 | 见 §8（WP13） |

**约束**：传输层不做业务聚合；每个方法只做「参数校验 + 一次 REST 调用 + envelope 解析」，
与既有 OpenAPI 客户端纪律一致。**待核对路径不得靠猜落地**——实施时先取官方文档确认，
再写方法与测试；文档与实现不一致时以官方文档为准并登记到 `docs/TOOL-LIMITS.md`。

### 7.2 服务端点面与服务/工具面分层

**新增端点全部进服务 HTTP 端点面（1:1，Web/CLI/测试可逐端点验证）**；
MCP 工具面按研究价值分档，避免工具数无节制膨胀：

| 档 | 策略 | 端点 |
|---|---|---|
| **直通工具**（逐端点 1:1） | 研究高频、语义独立 | `stock_screen`、`plate_list`、`plate_stock`、`short_daily_volume`、`short_interest`、`ipo_list`、`economic_calendar_hot`、`economic_calendar_search`、`info_owner_plate`、`watchlist_list`、`watchlist_groups`（**实现期修订 2026-09-16**：删去 `info_search_stock`——锁定表 §D.1 实测官方「搜索」页只有 find-news/find-community，**不存在证券搜索端点**；故直通 11 项） |
| **聚合读工具**（一个工具、白名单 section 枚举） | 结构同质、逐项暴露收益低 | `f10_detail(code, section)`：section 枚举映射锁定表 §C.5 的 26 个 F10 端点；`derivative_detail(code, section)` 同理（4 项）。（实现期修订：字段名是 **`code`** 而非 `symbol`，与既有行情端点载荷口径一致，服务端内部再转 futu symbol） |
| **HTTP-only**（不进工具面） | 低频/写类/自选修改 | `modify_user_security`（写用户富途侧自选，仅 Web 用户操作）、`warrant_screen`（窝轮数据保持 API 面完整但不策略化）、`info_rehab`（同步作业内部用）（实现期清单） |

- 工具面预算不变量：**直通工具 + 聚合工具合计 ≤ 80**（基线链路：WP8 末 59 → WP10
  `pipeline` 后 60 → WP11 `sentiment_history` 后 61 → **WP12 后 74**（+直通 11 +聚合 2）
  → **WP15 后 76**（+`research_tasks_claim/report`）；实测 `TOOL_COUNT = 74`），
  写入测试断言；新增工具必须显式登记档位与理由；
- **写端点纪律不变**：只有只读研究端点可批量进工具面；`modify_user_security` 触及
  用户富途侧数据，仅 Web 端点可达且不复用交易闸门（非交易写）；
- 端点声明自检（`store_access.endpoints()`）与 `caches.CACHE_TTL_MS` 同步登记：
  F10/板块/筛选＝静态或低频（TTL 30m–6h，按官方口径与实测确定）；做空数据 TTL 1h；
  实时族不进缓存（既有纪律）。

### 7.3 数据落库与因子化衔接

- **F10 与做空数据落 PIT 表**（新表，只存原始事实 + 抓取时间 + 来源）：
  `f10_snapshots(symbol, section, period_end, announced_at, payload, fetched_at)`、
  `short_snapshots(symbol, date, payload, fetched_at)`——**PIT 钥匙按 section 语义取值，
  不得统一硬套**：财报/公告类用 `announced_at`（沿用 WP1 财报公告日双源合并口径，与
  `fundamentals` 表同纪律）；持仓/评级/共识等快照类用数据自身日期字段（如
  `period_end`/数据日期），该字段缺失时以 `fetched_at` 为观测时点并**显式标注
  「观测时点非数据时点」**——研究查询只允许 `as_of` 之前已入库的观测，防前视；
- 每日作业 `research_snapshot`（与 sentiment_snapshot 同链）：对关注池标的抓取
  F10 关键 section（分析师共识、评级汇总、机构持仓、内部交易、持股变动）与做空数据
  落库，攒 PIT 历史；**先攒数、后因子化**，与 §6.3 同一套 250 交易日演进条款；
- 板块归属（`owner-plate`）落 `universe`/板块表用于**行业中性化**（补齐原规格 §5.1
  缺失的行业中性化能力）——板块列表与成份股按交易日快照落库，防幸存者偏差。

### 7.4 WP12 验收标志

- 传输层：每个新方法真机连通性自检（有凭据环境）+ 无线环境下的参数校验单测；
- 端点面：逐端点 envelope 形状测试 + 声明自检清单一致；
- 工具面：预算断言 ≤80、档位登记完整、写端点白名单回归（`modify_user_security`
  不在 MCP 工具面）；
- 落库：F10/做空/板块快照按交易日累积，`announced_at` 覆盖率可统计；
- 降级：`no_data`/`unsupported` 按官方语义如实呈现（空而非错），失败单端点不阻塞链。

## 八、WP13：通道统一收口

### 8.1 sync 作业迁移到 OpenAPI

现状：复权因子（rehab）、财务报表（statements）、估值明细、分红、经济日历仍经托管
MCP（`trading_datasource.futu_mcp`），`futu_channel: openapi` 未闭环。

迁移：上述 5 项取数在 openapi 通道下走 REST（方法见 §7.1），mcp 通道保留为回退；
**行为等价由同一批清洗/落库函数保证**（通道只换取数实现，不改落库口径）。迁移后
`futu_channel: openapi` 为完整通道（行情/交易/同步/推送全 REST）。

> **实现期补记（2026-09-17，阶段 A 审查）**：本节的「5 项」只覆盖 sync 的显式取数，
> 实际闭环还必须包含**三条同步腿**——K 线同步（`market.load_raw_bars`/`load_bars`
> 富途腿）、交易日历（`calendar.sync_calendar`）、指数成分股（`sync.sync_universe`）；
> 三者当时仍硬编码 MCP，「完整通道」的承诺并未成立。现三腿统一经
> `trading_datasource.channel.fetch` 分派：
> - K 线 → `market.history_kline`（入参 `autype`：MCP 字符串 ↔ REST 枚举 int，调用点显式
>   转换；官方 `date` 为 int YYYYMMDD，`str()` 归一兼容）；
> - 交易日历 → `market.trading_days`（官方文档复核：响应与 MCP 逐字段同形）；
> - 指数成分股 → `f10.valuation_index_stocks`（`GET /quote/valuation/index-stocks`）——
>   **这是官方等价端点**（指数成分股估值，锁定表 §C.5 + 2026-09-17 官方文档复核）；
>   **不存在「官方无等价端点」而必须保留 MCP 的腿**。
>
> 真机抽样三腿 `channel_used=openapi`（K 线 3 根 / 日历 23 个交易日 / SH.000300 成分
> 300 只 6 页）；`load_bars` 的新浪/Yahoo 降级源不属富途通道（设计如此）。
> **范围边界（如实登记）**：本补记只覆盖这三条腿；workbench 脚本 `instruments.py`
> （服务 `instrument` 端点）与 `quality.py`（服务 `quality` 端点）仍走 MCP 容器，
> `positions.py` 非 sim 工具按 `channel.sim_call` 翻译表边界保留 MCP——均已在
> `docs/architecture.md` 通道表「未 REST 化的读取」逐条登记。完整清单与
> 回归见 `docs/architecture.md` 通道表与 `docs/TOOL-LIMITS.md` §九之一 第 5 条。

### 8.2 模拟交易 REST 化（9 端点）

现状：sim 下单/查单经托管 MCP `sim_trade_*` 工具（TOOL-LIMITS 记录
`sim_trade_modify_order` 间歇性 `-5`）。

迁移：`OpenApiSimTrade` 接入官方模拟交易 9 端点（账户列表/资金/持仓/下单/改单/撤单/
订单列表/历史订单/最大买卖量）；`futu_channel=openapi` 时 `core_broker` 的 sim
实现走 REST，mcp 通道保留回退。**sim 通道能力边界不变**（仅限价当日单，扩展字段
如实拒绝），改单策略沿用「撤旧重下」（官方模拟改单可靠性待实测，实测通过后可改原生改单，
登记到 TOOL-LIMITS）。

### 8.3 WP13 验收标志

- 双通道等价性测试：同一请求在 mcp 与 openapi 通道下产出同形落库结果（假件注入）；
- sim 全链路回归：`futu_channel=openapi` 下 sim 计划→执行→成交→台账一致；
- 通道回退测试：凭据缺失/通道不可用时按既有语义如实报错，不静默降级。

## 九、WP14：研究院闭环

### 9.1 研究院编排 skill（skills/research-institute/）

Harness 会话内的编排技能，定义四类子代理分工（复用 Harness 原生子代理能力）：

| 子代理 | 职责 | 主要通道 |
|---|---|---|
| 采集代理 | 标的/事件的资讯与基本面盘点：公告、资金流、F10、新闻、社媒叙事 | 富途 OpenAPI（只读工具）+ fin_news/fin_sentiment + last30days |
| 假设代理 | 从资讯 + PIT 数据提出候选因子/交易规则，落声明式提案 | 工作台数据（quantwb 只读工具）+ 采集代理产出 |
| 检验代理 | 对提案跑验证门（ic/分层/walk-forward），产出验证报告 | rules CLI + 因子检验工具 |
| 研报代理 | 综合产出研报并发布 | research_publish（研究页可见） |

- **人工随时介入**：对话即介入——问资讯影响、要求深挖、否决假设，都是研究院的
  正常输入；12 角色深度流程（trading-agents）仍是单标的深度研究的重型入口，
  研究院 skill 是持续性的因子/规则生产线；
- 产出落 `rules/` 候选目录 + rules 表（9.3），全程带 research_run_id 溯源。

### 9.2 声明式规则协议（rules JSON）

```json
{
  "rule_id": "news_momentum_v1",
  "hypothesis": "公告超预期 + 资金流入 → 短期动量（研究假设，人话写清）",
  "factors": ["momentum_20", "capital_flow_3d"],
  "combine": "zscore_equal_weight",
  "universe": "watchlist.SH",
  "top_n": 5,
  "rebalance": "weekly",
  "provenance": {
    "research_run_id": "…", "created_by": "harness",
    "created_at": "…", "approved_by": null, "approved_at": null
  }
}
```

- **LLM 不写代码**：factors 只能引用已注册因子（REGISTRY 现有 + 核心库 PR 增补），
  combine 只能用解释器支持的有限算子集（首版：zscore 等权/IC 加权）；需要新算子 =
  核心库 PR（人工代码审查），不提供 eval/动态执行通道；
- **未成熟因子机械拒绝**：规则解释器校验 factors 引用——未注册、或注册但未通过
  检验（如攒数中的资讯/AI 因子）的引用直接拒绝提案（fail-closed）。

### 9.3 rules 表 + 规则解释器

- 新表 `rules(rule_id PRIMARY KEY, spec TEXT, status TEXT, validation TEXT,
  created_at, approved_at, approved_by)`——status: candidate/validating/passed/failed/
  enabled/disabled；validation 存最近一次验证报告摘要；
- 规则解释器（core 新模块 `rule_engine.py`）：rules JSON → Strategy 协议适配实例
  （universe(as_of) → target_weights(as_of)），机械执行声明；通过的规则批准后即成为
  `auto_pipeline.strategies` 的合法取值（strategy 填 rule_id）；
- 验证门补全（ic CLI / factors 扩展）：IC 均值 **t 统计与 p 值**、**5 分位分层收益
  及单调性判定**、**因子衰减半衰期**、**换手率**；walk-forward OOS 复用既有实现；
  验证报告落 rules.validation。

### 9.4 候选池与审批（工作台研究页扩展）

- 研究页新增「规则候选池」区：候选规则列表（假设/因子/验证报告/状态）；
- **人工一键批准 = 启用**（批准按钮只在 Web，不进 MCP 工具面）：批准后 status=enabled，
  该规则即可被 `auto_pipeline.strategies` 引用，sim 下次 build_plan 即生效；
  否决 → disabled 留档；
- 启用随时可停（研究页/流程页开关）；live 模式的执行确认环节不因规则来源有任何变化；
- 批准是策略上岗的唯一通道：Harness 可以提交一百条提案，没有批准，一条也进不了
  auto_pipeline。

### 9.5 边界纪律（汇总）

1. LLM 不写代码进核心库（9.2）；
2. Harness 不能自批（批准只在 Web 端点，不进模型工具面）；
3. 社媒证据三要素（平台/时间/互动数），取不到标注「未核实」；
4. 社媒情绪永不直接触发交易（last30days 桥接纪律原样沿用）；
5. 资讯/F10/做空因子未满 250 交易日不得进规则 factors（§6.3、§7.3 演进条款）；
6. 一切执行仍过风控 8 规则唯一入口，kill/熔断对自动链同样一票否决。

### 9.6 WP14 验收标志

- 规则解释器单测：声明 → 权重、非法因子引用拒绝、combine 算子白名单；
- 验证门统计单测：t 统计/分层单调/半衰期在构造数据上的正确性；
- 审批流闭环：candidate → validating → passed → 人工批准 → enabled → build_plan
  消费；未批准规则绝不进 auto_pipeline；
- 工具面回归：MCP 工具清单不含 rules 批准端点；
- 端到端（假时钟 + sim）：研究院产出一条规则 → 检验通过 → Web 批准 → 次日
  build_plan 按新规则生成计划 → auto_execute 执行 → 流程页全程可见。

## 十、WP15：值班研究员（L3 定时 Harness 研究任务）

> 依赖：WP14（研究院 skill 与规则协议）。L3 是 L2 的定时化——同一套研究院能力，
> 多一个「没人开口时自己开工」的触发器。

### 10.1 组件与数据流

```
外部定时器（systemd timer / cron）
   └─► dsh --profile headless "<唤醒提示>"        ← DSH 自己跑，服务进程不承载 LLM
         └─► 读任务队列（SQLite research_tasks）→ 循环领取至清空
               └─► 研究院 skill 子代理分工执行（采集/假设/检验/研报）
                     └─► 产出落研究页 / 规则候选池（待人工批准）→ 任务置 done
```

兜底路径：**会话开始/恢复后的首次交互**先消费队列积压（由仓库根 `AGENTS.md` 的启动纪律
驱动：首个 turn 用 `research_tasks_claim` 探测，有任务先按研究院技能值班模式处理再回应用户）
——与定时器路径共用同一状态机，靠状态迁移互斥，不会重复执行。**没有任何 turn 的会话不会
消费队列**（任务不丢、只延迟），这是刻意的限度而非缺陷。

### 10.2 任务队列（SQLite 新表 `research_tasks`）

```
task_id, kind, payload, status, attempts,
created_at, started_at, finished_at, result_ref, err
```

- `status`：pending / running / done / failed；
- **领取 = 状态迁移**（pending→running）；`running` 超时 N 分钟回收为 pending 并
  `attempts+1`；`attempts ≥ 3` → failed + warn 告警（不无限重试）；
- **当日幂等**：唯一索引 `(kind, as_of, market)`——同一天同一任务只入队一次，
  定时器重复唤醒、双路径并发都不会重复研究；
- 由 L1 机械层入队（新作业 `enqueue_research`，**基础 GLOBAL 链**，时刻 = 对账
  `reconcile_at` + 5 分钟，**严格晚于当日 digest 落库**——审查 A-2 修订；关闭态用固定
  基线 19:05），**入队是机械动作、零 LLM**；一条作业覆盖三市场（各市场按自己的会话
  收盘与日历折算观测日，未收盘软跳过），`daily_brief` 的引用里带 `daily_digest` 行的
  `as_of`/`fresh_enough`——摘要比研究日旧时发 info 告警并在简报里如实标注。

### 10.3 任务种类（白名单三种，不接受自由文本）

| kind | 触发时机 | payload（机械层预填，仅结构化引用） | 产出 |
|---|---|---|---|
| `daily_brief` | 每交易日（digest 落库后） | as_of、market、digest 引用、symbols、refs（含 `daily_digest` 的 as_of/fresh_enough） | 资讯影响简报 → 研究页 |
| `factor_patrol` | 每交易日 | as_of、factor_list（注册表基线）、IC 历史窗口 | 因子衰减巡检报告 + 异常告警 |
| `mining_round` | 每周一次 | as_of、**factor_list（候选因子域/已注册基线）**、数据引用 | 因子/规则候选提案 → 候选池（待人工批准） |

**payload 纪律**：只含标的、日期、数据 ID 等结构化引用，**不含自由文本指令**——
队列不得成为任意 prompt 注入口（与指令目录白名单同一纪律；载荷校验在入队侧与
领取侧各做一次，fail-closed）。

### 10.4 唤醒与兜底（A + C）

- **A（主）**：外部定时器每交易日唤醒一次，时刻**排在入队之后**（默认 19:20 =
  对账 19:00 → digest → 入队 19:05 → 唤醒 19:20；审查 A-2 修订）；唤醒后 headless 会话
  循环领取至队列清空，单次运行设总时长上限（防失控），超限即退出并把未完成任务留在队列；
- **C（兜底）**：定时器没跑成（机器关机、异常退出、未装定时器）时，**会话开始/恢复后的
  首次交互**按仓库根 `AGENTS.md` 的启动纪律消费积压——**非后台自动动作**（没有任何 turn
  的会话不会消费队列），**任务不丢、只延迟**；
- 安装与运维（定时器单元文件/命令行、日志位置、失败排查）随 WP15 一并交付到 RUNBOOK。

### 10.5 边界（写死）

1. L3 只产出研究产物，**永不直接下单、永不直接启用策略**；
2. 不写核心库代码，规则提案走 WP14 声明式协议；
3. 白名单三种 kind + 结构化 payload，拒绝自由文本（§10.3）；
4. L3 失败或积压**不阻塞 L1**（研究时效问题，不是交易安全问题）；
5. **服务进程不调 LLM**：唤醒由外部定时器拉起 `dsh --profile headless` 完成，
   服务只提供队列读写端点。

### 10.6 WP15 验收标志

- 队列状态机单测：入队/领取/完成/失败/超时回收/attempts 上限/当日幂等；
- 白名单回归：未知 kind、自由文本 payload、结构非法一律拒绝；
- 假时钟端到端：L1 数据就绪 → 入队 → 模拟 headless 执行 → 产出落研究页 → 队列清空；
- 兜底测试：定时器不跑时，会话恢复补跑积压且不重复执行已完成任务；
- 边界回归：L3 全程无交易写通道调用；候选提案在人工批准前不进 `auto_pipeline`；
- 定时器安装/启停/日志/排查文档齐全。

## 十一、测试总则

- 既有 52 Python + 17 Node 测试全程保持全绿；每个 WP 附带离线单测（假时钟注入，
  无网络依赖；last30days/富途通道全部注入假件）；
- 集成验收：WP9 末做 sim 两交易日无人干预全链路；WP12/WP13 末做双通道等价与
  数据面逐端点形状；WP14 末做规则从挖掘到执行的端到端演练；WP15 末做
  「定时唤醒 → 队列执行 → 产出落研究页 → 兜底补跑」全链路演练；
- live 准入维持 P4 清单人工评估不变，本规格不改变任何 live 准入条件。

## 十二、风险与诚实清单

1. **自动执行的可靠性依赖数据就绪门**：门太松会在坏数据上生成计划（风控与质量检查
   兜底），太紧会频繁跳过——保守取向：宁可跳过 + 告警，不冒险执行。
1.1 **调度器 tick-first 补跑语义**：服务启动即补跑当日到期作业，因此「到点即执行」
   会在错误时点执行交易（收盘后执行被规则 3 拒单并消耗计划；盘中晚补按错误价格成交）。
   已由守卫 9（执行窗口，默认 30 分钟）封堵——超窗一律不执行、留待人工。**这是保守
   但会漏执行的设计**：机器关机跨过窗口 → 当日计划不自动执行，需人工或次日新计划。
2. **美股 exec_at 与夏令时**：默认值按夏令时写死，冬令时需人工调配置（文档注明）；
   不做自动 DST 换算。
3. **last30days 渠道脆弱**：上游发版/密钥失效 → 来源缺席（降级不报错）；情绪采集
   不进当日信号，链路韧性无影响。
4. **官方文档链接漂移**（已实测）：`llms.txt` 的 `/f10/*.md` 链接 404，真实路径在
   `/api/quote/financials/*`。**实施时必须逐端点核对官方文档**，§7.1 标「待核对」的
   路径不得直接落地；实现与文档不一致登记 TOOL-LIMITS。
5. **声明式规则表达能力有边界**：复杂假设（条件触发、事件驱动）首版算子集表达不了，
   需要核心库 PR——这是刻意的摩擦，不是缺陷。
6. **资讯/F10/做空因子转正周期长**：250 交易日 ≈ 一年，本规格交付时这些因子仍在攒数期；
   规格保证的是「路径存在」，不承诺「当下可用」。
7. **工具面膨胀**：直通 + 聚合合计 ≤80 是硬预算；超预算必须走聚合档或 HTTP-only，
   不允许「顺手加一个工具」。
8. **指令双进程并发**：CLI daemon 与服务同跑时靠既有原子协议互斥，极端交叉仍可能
   告警噪音（不产生重复执行——nonce 主键兜底）；文档建议二选一常驻。
9. **build_plan 持仓查询依赖 broker 可用**：晚间查询失败 → 跳过当日计划 + 告警，
   次日重试；不做本地台账替代（台账二分原则不变）。
10. **数据面补全的额度成本**：F10/做空按关注池每日抓取会消耗富途调用额度（限频见
    官方 rate-limit）；实施时按「关注池规模 × 端点族」估算并给出限速参数，默认只抓
    关键 section（分析师共识/评级/机构持仓/内部交易/持股变动 + 做空 2 项）。
11. **L3 的时效依赖外部条件**：定时器未安装、机器关机、headless 运行失败时，研究任务
    只能等你下次开会话补跑（C 兜底）——**任务不丢但会延迟**。这是刻意的取舍：
    为了不让服务进程承载 LLM 循环，放弃了「永远准点」。研究产物不进信号路径，
    延迟不影响交易安全。
12. **L3 的运行成本与失控防护**：headless 会话会真实消耗模型额度；单次运行设总时长
    上限、任务 attempts 上限、每日 kind 幂等三重防护，防止定时器故障导致循环烧额度。
13. **队列即攻击面**：`research_tasks` 若接受自由文本就成了 prompt 注入口；本规格用
    白名单 kind + 结构化 payload + 双端校验（入队侧与领取侧）封堵，实施时必须有
    拒绝用例覆盖。

## 十三、需同步修订的现有文档

| 文档 | 修订内容 |
|---|---|
| `docs/architecture.md` | 组件职责表加 rule_engine/研究院 skill/新数据端点族/research_tasks 队列；**三层任务模型（L1/L2/L3）写入**；闭环链路图更新；端点表加 pipeline/auto_pipeline/sentiment-history/rules/research-tasks；「两条受约束写路径」表述更新为含自动链；工具面计数与档位说明 |
| `docs/TOOL-LIMITS.md` | 新增各端点的实测口径（经济日历、rehab、F10 族、做空、板块、筛选、模拟交易 REST），官方文档链接漂移登记 |
| `docs/OPENAPI-FEASIBILITY.md` | 覆盖率审计结论并入（附录 A 摘要） |
| `README.md` | auto_pipeline 配置样例；研究院用法；目录结构；端点/工具面计数 |
| `docs/HANDOVER.md` | 开关运维、exec_at 时区注意、规则审批运维、数据面抓取额度、**值班研究员定时器运维（安装/启停/失败排查/漏跑补跑）** |
| `docs/RUNBOOK.md` | 全自动演练：kill 中断恢复、开关启停、规则否决回滚、通道回退演练、**L3 定时器演练（定时唤醒、队列积压补跑、超时回收）** |
| `docs/P4-live-trading.md` | live 准入清单不变，补记「live 计划已自动生成，执行仍全人工」事实 |

---

# 附录 A：富途 OpenAPI 覆盖审计（2026-09-16）

> 方法：对照 `https://open.futunn.com/zh-cn/llms.txt` 与官方 API 参考
> （`/api/overview/api-reference.md`）逐端点核对仓库实现（`platform/server/*.py`、
> `plugins/datasource/python/trading_datasource/futu_openapi.py`、`mcp_tools.py` 工具面），
> 对关键新端点现场取官方文档核对参数。
> **图例**：✅ 已覆盖（OpenAPI REST 权威通道）｜⚠️ 功能可用但走托管 MCP（待迁移）｜
> ❌ 未覆盖（本规格补齐）｜🚫 明确不做／范围外

## A.1 交易与账户：13/13 全覆盖 ✅

| 端点 | 状态 | 实现 |
|---|---|---|
| 下单 place-order | ✅ | `OpenApiTrade.place_order`（8 种 order_type/GTC/时段/触发价/多腿/备注） |
| 改单 modify-order | ✅ | `modify_order`（官方不支持改 A 股 → 如实拒绝并指引撤+下） |
| 撤单 cancel-order | ✅ | `cancel_order` |
| 下单确认 order-confirm | ✅ | `order_confirm`（人工批准后自动调用，失败落 unknown 不重放） |
| 最大可交易数量 get-max-qty | ✅ | `max_trade_qty` |
| 账户资金 get-funds | ✅ | `account_funds` |
| 授权交易账户 get-accounts | ✅ | `authorized_accounts` |
| 持仓列表 get-positions | ✅ | `positions` |
| 未完成订单 get-open-orders | ✅ | `open_orders` |
| 历史订单 get-history-orders | ✅ | `history_orders` |
| 订单详情 get-order-details | ✅ | `order_details` |
| 当日成交 get-today-deals | ✅ | `today_deals` |
| 历史成交 get-history-deals | ✅ | `history_deals` |

**行情/交易事件 WS 推送**：✅ 双通道（`server/futu_push.py`，重连/refresh/事件→OMS，
断线不补发、对账兜底）。

## A.2 行情：已覆盖（19 REST 端点）✅

实时行情 6/6：股票报价、买卖盘、逐笔成交、分时数据、当前 K 线、市场快照。
资金流向 3/3：资金流向、资金流向历史、资金分布。
基础数据 5/9：历史 K 线、市场状态、股票基本信息、交易日、新闻搜索（`find-news`/
`find-community`）。
衍生品 2/6：期权链、期权到期日。
筛选 1/3：期权筛选。

## A.3 缺口清单（WP12 补齐）

### A.3.1 基础数据（4）

| 端点 | 状态 | 官方文档 | 备注 |
|---|---|---|---|
| 经济日历热门 | ⚠️→✅ | `/api/quote/basic-data/economic-calendar-hot.md` | 现经托管 MCP（events 页在用） |
| 经济日历搜索 | ❌ | `/api/quote/basic-data/economic-calendar-search.md` | 宏观事件因子 |
| 搜索（证券） | ❌ | `/api/quote/basic-data/search.md` | 现 `info_search` 实为新闻搜索 |
| 所属板块 | ⚠️→✅ | `/api/quote/basic-data/owner-plate.md` | 行业中性化前提；现经 MCP |
| 复权因子 | ⚠️→✅ | `/api/quote/basic-data/rehab.md` | sync 作业现经 MCP |

### A.3.2 板块（2）

| 端点 | 状态 | 官方文档 | 已验证参数 |
|---|---|---|---|
| 板块列表 | ❌ | `/api/quote/plate/plate-list.md` | ✅ `GET /api/v1.0/quote/plate-list?market&plate_class`（ALL/INDUSTRY/REGION/CONCEPT/OTHER；REGION 仅 SH/SZ） |
| 板块成份股 | ❌ | `/api/quote/plate/plate-stock.md` | 路径待核对 |

### A.3.3 衍生品（4）

期货信息、相关期货、期权波动率、期权行权概率 ❌ —— 官方文档
`/api/quote/derivatives/{future-info,reference-future,option-volatility,option-exercise-probability}.md`。

### A.3.4 筛选（2）

| 端点 | 状态 | 官方文档 | 已验证参数 |
|---|---|---|---|
| 股票筛选 | ❌ | `/api/quote/screening/stock-screen.md` | ✅ `POST /api/v1.0/quote/stock-screen`：`screen_queries`（11 选 1 查询类型）/`retrieve_queries`（9 选 1 取值）/`sort`/`sorts`/`next_key`/`limit`≤300/自选持仓范围；8 市场支持 |
| 窝轮筛选 | ❌（数据接入，不策略化） | `/api/quote/screening/warrant-screen.md` | 路径待核对 |

### A.3.5 IPO（1）

IPO 列表 ❌ —— `/api/quote/ipo/ipo-list.md`（HK/US/CN/MY/SG）。

### A.3.6 个股深度数据 F10（20 缺失 / 23 总数）

| 端点 | 状态 |
|---|---|
| 财务报表 statements | ⚠️→✅（sync 现经 MCP） |
| 估值明细 valuation-detail | ⚠️→✅（估值因子现经 MCP） |
| 分红派息 dividends | ⚠️→✅（events 页现经 MCP） |
| 分析师共识 analyst-consensus | ❌ |
| 评级汇总 rating-summary | ❌ |
| 机构持仓 institutional | ❌ |
| 持股明细 holder-detail | ❌ |
| 持仓变动 holding-changes | ❌ |
| 内部持股人 insider-holders | ❌ |
| 内部交易 insider-trades | ❌ |
| 股东概况 shareholders-overview | ❌ |
| 公司高管 company-executives | ❌ |
| 高管背景 executive-background | ❌ |
| 公司简介 company-profile | ❌ |
| 回购 buybacks | ❌ |
| 拆合股 stock-splits | ❌ |
| 业绩价格历史 earnings-price-history | ❌ |
| 业绩价格变动 earnings-price-move | ❌ |
| 运营效率 operational-efficiency | ❌ |
| 营收拆分 revenue-breakdown | ❌ |
| 晨星评级 morningstar | ❌ |
| 券商席位 top-brokers | ❌ |
| 板块估值成份股 valuation-plate-stocks | ❌ |

> **文档链接漂移（实测）**：llms.txt 给出的 `/api/quote/f10/*.md` 全部 404；官方 API
> 参考将本族归入「个股深度数据」，真实路径在 `/api/quote/financials/*`。
> **实施时逐端点核对真实路径与参数。**

### A.3.7 做空（2）

| 端点 | 状态 | 已验证参数 |
|---|---|---|
| 每日做空量 | ❌ | ✅ `GET /api/v1.0/quote/{symbol}/short/daily-volume?count`（≤90；HK 成交维度 / US 持仓维度；仅 HK/US 可卖空证券；`-10 no_data` 视为空；返回累计空头持仓 `aggregated_short`/`aggregated_short_ratio`） |
| 做空利息 | ❌ | `/api/quote/short/short-interest.md`（路径待核对） |

### A.3.8 自选（3）

自选列表、自选分组 ❌（读取用于关注池导入：把用户富途自选同步为平台关注池候选）；
修改自选 ❌（**仅 Web 用户操作端点，不进 MCP 工具面**——触及用户富途侧数据，非交易写）。

### A.3.9 模拟交易（9）

模拟账户列表、模拟资金、模拟持仓列表、模拟下单、模拟改单、模拟撤单、模拟订单列表、
模拟历史订单、模拟最大买卖量：⚠️→✅（WP13 迁 REST；现经托管 MCP `sim_trade_*`）。

### A.3.10 通道不统一残留（5，WP13 收口）

复权因子、财务报表、估值明细、分红派息、经济日历热门——功能可用但走托管 MCP，
`futu_channel: openapi` 未闭环。

## A.4 范围外（明确不做）

| 类别 | 端点族 | 理由 |
|---|---|---|
| 加密货币 | `crypto_trading/*`（账户总余额/授权账户、下单/改单/撤单/最大买卖量、活跃订单/历史订单/订单详情、成交明细/历史成交、WS 推送） | 独立资产类别与账户体系，超出三市场（A/H/US 证券·ETF·期权）范围；纳入需另立规格并重做风控/准入 |
| 窝轮/牛熊证策略化 | warrant-screen 之外的窝轮交易与策略 | 数据端点接入以保持 API 面完整，但策略/风控/执行不引入该品类 |

## A.5 审计结论

| 层 | 覆盖率 | 结论 |
|---|---|---|
| 交易与账户 | **13/13（100%）** | 交易链路完整，无需补 |
| 行情核心（实时/K线/资金流/期权基础） | 19/约 25（~76%） | 核心可用，衍生品 4 项待补 |
| 研究数据面（F10/筛选/板块/做空/IPO/经济日历） | 约 3/32（~9%） | **主要缺口，WP12 补齐** |
| 通道统一 | 5 项残留 | WP13 收口 |
| 模拟交易 | 功能可用，通道未统一 | WP13 迁 REST |

补齐后：**三市场证券与期权范围内的官方功能端点全部接入权威 OpenAPI 通道**，
唯一例外为 §1.1 登记的加密货币与窝轮策略化使用。
