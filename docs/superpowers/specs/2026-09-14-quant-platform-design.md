# 专业量化交易平台设计（trading-core）

> 状态：设计已与用户分三批逐节确认（2026-09-14），待规格审查后进入实现计划。
> 位置：`docs/superpowers/specs/2026-09-14-quant-platform-design.md`。

## 一、背景与已确认的决策

在现有 dsh-trading-agents（对话模式 + 数据层 + 工作台）之上，建设一个专业量化交易平台。
设计前用户已确认三个方向性决策：

| 决策点 | 选择 |
|---|---|
| 自动化程度 | **半自动**：调度器自动完成数据→信号→组合→风控预检，生成次日计划；执行由人工在工作台一键确认整批订单 |
| 部署形态 | **轻量守护进程**：无 LLM 的 Python 调度守护进程（systemd/cron 拉起） |
| 市场范围 | **三市场全做**：A股 + 港股 + 美股，深度按数据现实分层并如实标注 |
| 总体架构 | **方案 A：统一核心包 + 无 LLM 守护进程 + 冻结计划执行**（备选 B 塞进 engine、C 双进程分仓被否，C 留作演进路径） |

## 二、非目标（明确不做）

1. **全自动交易**：不经人工确认的策略自动下单。执行必须经过冻结计划的人工确认窄门。
2. **多用户/多租户**：单操作者、单机。无用户体系、无权限分级。
3. **高频/秒级策略**：数据通道为拉取式（富途 MCP），A股无盘中实时权限（`-9`）。
   平台频率上限：日线与分钟级收盘数据策略；港美盘中实时仅用于执行时点参考。
4. **工作台逐单表单/聊天**：工作台仍无聊天、无逐单下单表单、无 shell/LLM/token 接口。
   唯一新增入口是"执行已冻结计划"（见 §8.3 边界变更）。
5. **绕过人工确认的对话内实盘**：对话路径的实盘写操作由工作台**业务确认**把守——`policy.js`
   pre-execute 发起确认、用户在 Harness 内 legacy 工作台面板作答（2026-09-15 main 修订后
   **不再走 Harness 原生审批**、`policy.js` 永不返回 `{kind:"ask"}`；模型不能代替用户自批；
   跨进程可见性边界见 WP6 规格 §5.1 末段）。

## 三、总体架构

### 3.1 组件图

```
富途 MCP / 新浪 / Yahoo / X / Reddit          券商账户（富途）
        │（唯一取数实现 trading_datasource）        │
        ▼                                         │
   sync 作业 ──► PIT SQLite ◄── fundamentals       │
        │            │                            │
        ▼            ▼                            │
   factors ──► strategies ──► portfolio ──► planner ──┐ 风控报告
                                                │     ▼
                                          计划冻结(hash) ──► 工作台一键确认
                                                           │ (live 需口令)
                                                           ▼
                                              risk.pre_trade_checks ◄─ kill switch
                                                           │
                                              oms.submit ──► 券商 ──► 状态机
                                                           │
                                              reconcile + tca ──► 审计链
```

### 3.2 新组件：plugins/core（库，非插件）

仓库位置 `plugins/core/`，与 `plugins/datasource` 同级；**没有 cordis 行，不出现在工具列表**。
安装方式与 datasource 完全一致：`install_plugins.py` 的 `LIBRARIES` 元组增加 `"core"`，
解包到 `$DSH_HOME/trading-python/core/`，`.pth` 从一行变两行，安装自检增加
`import trading_core` 一条。

```
plugins/core/python/trading_core/
├── store.py        # SQLite（WAL）：建表、迁移、连接管理
├── calendar.py     # 交易日历（富途 quote_trading_days 缓存落库）
├── sync.py         # 行情/复权/基本面增量同步 + 首次全量回填（断点续传）
├── factors.py      # 因子注册表（收敛 workbench/python/factors.py 实现）
├── strategies.py   # 策略注册表：universe(as_of) → target_weights(as_of)
├── portfolio.py    # 信号 → 目标权重（等权/波动率目标/风险平价）
├── backtest.py     # 多标的组合回测 + walk-forward（复用 datasource 单标的核心）
├── planner.py      # 目标持仓 vs 券商实际 → 订单计划（冻结、hash）
├── risk.py         # 风控硬拦截（唯一提交入口强制经过）
├── oms.py          # 订单状态机、幂等提交、成交同步
├── reconcile.py    # 与券商持仓/成交对账
├── tca.py          # 到达价 vs 成交价滑点分析
├── daemon.py       # 调度守护进程（python -m trading_core.daemon）
├── commands.py     # 指令目录协议（工作台 → daemon，白名单指令）
├── alerts.py       # 告警落库 + 可选桌面通知
└── config.py       # ~/.dsh/trading-platform.json（调度表/关注池/通知配置）
```

### 3.3 与现有资产的关系

| 现有资产 | 处理方式 |
|---|---|
| `trading_datasource`（统一数据层） | **不动**，仍是唯一取数实现；core 依赖它 |
| `plugins/engine` 量化工具（quant_signal/quant_backtest） | WP2 后改为 core 策略注册表的薄封装，**工具 schema 不变**（对话快路径向后兼容） |
| `plugins/workbench/python/factors.py`、`analytics.py` 领域部分 | 收敛进 core，workbench 留薄适配层；旧测试改指向新包 |
| `risk_config.py`（5 参数） | 直接成为风控配置源，文件与校验逻辑不迁移以保持兼容 |
| 本地模拟台账（engine ledger） | **保留**，定位为"纸面推演"（quant 预览），明确不是成交事实 |
| 现有 52 Python + 17 Node 测试 | 全程保持全绿 |

**台账二分原则**（延续"本地台账不冒充账户状态"）：
- 本地模拟台账 = 纸面推演，用于快速预览；
- OMS 台账（core 新建）= 与券商往来的真实订单状态（sim 账户或 live 账户），**执行事实的唯一权威**；
- planner 的 `current_positions` 一律从券商查询，不读本地台账。

## 四、数据基座：PIT SQLite

### 4.1 存储与表结构

单库 `~/.dsh/trading-data/trading.sqlite`（WAL；`DSH_HOME` 覆盖规则与全仓库一致）：

| 表 | 主键 | 关键字段与用途 |
|---|---|---|
| `bars` | (symbol, period, ts) | 原始 OHLCV + `source` + `adj_factor`。**存原始价，复权口径查询时合成** |
| `adjustments` | (symbol, ex_date) | 复权因子版本化；来源富途 `quote_corporate_actions_rehab`（上下文炸弹，分市场增量拉取） |
| `fundamentals` | (symbol, field, period_end) | **`announced_at` 是 PIT 钥匙**：研究查询只允许 `announced_at <= as_of` |
| `universe` | (as_of, symbol, index_name) | 成分股快照，杜绝幸存者偏差 |
| `calendar` | (market, date) | 交易时段；同步与风控都依赖 |
| `kv` | key | 同步游标（断点续传进度） |

### 4.2 三条硬规则

1. **PIT 纪律**：任何研究/回测查询必须带 `as_of`；财报按公告日对齐（非报告期）、
   成分按当日快照、bar 按交易日。违反 PIT 的查询在 store 层直接拒绝。
2. **复权统一**：三个源口径不同（新浪 qfq / Yahoo auto_adjust / 富途原始）。
   落库一律原始价 + 因子表，前/后复权均从两者派生，跨源收益率才可比。
3. **宁缺毋假**：同步失败即报错并告警，不写占位 bar；缺口按日历检测并标记
   （延续 `load_bars` 抛错、界面宁空勿假的原则）。

### 4.3 规模边界与同步作业

- 全市场三市场日线（约 1.5 万标的 × 10 年 ≈ 3700 万行，SQLite 可承受）；
- **分钟级仅关注池**（≤ 200 只，配置于 `trading-platform.json`）；
- 首次运行全量回填：分块 ≤ 370 根（富途单次上限）、限速（复用 `futu_mcp` 现有
  串行节流与退避重试）、游标记进度、可断点续传；
- 增量同步：每市场收盘后 +30min 触发（按 calendar 判定交易日）；
- 数据质量作业：缺口检测（按日历）、跨源抽样交叉校验（日线 close 差异超阈值告警）、
  新鲜度标注。

## 五、研究层

### 5.1 因子注册表

```python
@factor("momentum_60", domain="price", universe="all")
def momentum_60(symbol, as_of, data): ...
# → {"value": 0.23, "source": "bars/futu", "missing": False}
```

| 因子域 | 首批因子 | 数据来源 |
|---|---|---|
| 价格 | 动量 20/60/120、波动率 20/60 | bars（PIT） |
| 估值 | PE/PB/股息率 | 富途 `quote_valuation_detail` → 同花顺兜底（A股） |
| 质量 | ROE/ROA | fundamentals 表（按 announced_at 对齐）+ Yahoo 备用 |
| 情绪 | X 声量/情感、千股千评 | fin-data 渠道；**标注"并列参考，不进信号"**（可复现性原则） |

**IC 检验**：收敛现有 `ic_series`，补 RankIC、5 分位分层收益、因子衰减半衰期。
因子上线门槛：IC 均值 t 检验显著 + 分层单调。

### 5.2 策略接口

```python
class Strategy(Protocol):
    id: str
    def universe(self, as_of) -> list[str]
    def target_weights(self, as_of, data) -> dict[str, float]  # ∑|w| ≤ 1，可含现金
```

- 现有 `rsi` / `ma_cross` 移植为单标的策略；
- 新增横截面组合策略作验证载体：动量+估值复合 z-score → Top-N 等权，月度再平衡。

### 5.3 组合回测与 walk-forward

- 成本模型沿用现有常数：佣金 0.03% 双边 + A股印花税卖出 0.1% + 滑点 0.1%；
- **A 股现实约束**：T+1（当日买入不可卖）、涨停不买/跌停不卖（由 bars 判定）、
  停牌跳过、整手取整；
- 基准对比：指数 bar 入库（沪深300/恒指/标普500）→ 超额收益、信息比率、跟踪误差；
- **walk-forward**：滚动 训练 504 / 测试 63 / 步长 63 交易日，输出 OOS 拼接曲线；
  参数敏感性网格复用现有 `sensitivity.py`；报告强制列出测试过的参数组数（多重检验自曝）。

## 六、执行层

### 6.1 计划（Plan）对象

```
plan_id, as_of, mode, strategy_id
target_weights    # 策略目标
current_positions # 券商实际（查询所得，非本地台账）
orders[]          # diff 生成：标的/方向/数量/限价/预计金额
risk_report       # 逐单预检结果 + 组合影响
content_hash      # 冻结指纹
status: draft → frozen → approved → executing → done | cancelled | expired
```

冻结后内容不可变。执行请求必须携带 `plan_hash + expected_mode`
（复用 switch-mode 防呆模式，防止拿旧页面批新计划）。

状态语义补充：`approved` 是持久化状态——记录确认时间、确认来源（指令文件 id）与
口令校验结果，随后才进入 `executing`；审计链据此可回答"谁在何时批准了哪个hash的计划"。
`expired` 的判定：下一交易日新计划生成时，前一交易日仍未执行的计划自动置为 expired
（跨日计划不可执行，防止拿昨天的目标持仓今天下单）。

### 6.2 订单状态机

```
draft → frozen → submitting → submitted → partial → filled
                                   │
                                   ├→ cancelled | rejected
                                   └→ unknown   ← 铁律：只查询，绝不重放
```

**幂等三件套**：
1. `client_order_id` 本地 UUID，唯一索引；
2. 同 (账户, 标的, 方向, 计划) 仅允许一支在途单；
3. 提交超时/异常 → 转 `unknown` → 查询券商实际状态后再迁移，**永不自动重发下单**
   （P4 实测教训的代码化）。

### 6.3 对账与 TCA

- **对账**：每次执行后 + 每日固定作业。拉券商 positions + 当日 orders/fills，
  与 OMS 台账比对数量/成本/状态。差异判定：**持仓数量不一致即差异**；成本/市值口径
  差异 > `reconcile_value_diff_pct`（默认 0.5%，trading-platform.json 可配）为差异。
  差异 → critical 告警 + **自动暂停后续计划执行**（只暂停，不自动平仓）。
  差异与处理记录落表。
- **TCA**：记录到达价（计划冻结时 close 与提交时 quote 双口径）vs 实际成交价 →
  滑点 bps，按标的/日/策略聚合，工作台展示。

## 七、风控层：硬拦截

`risk.py` 提供**唯一**提交入口 `pre_trade_checks(order, ctx) -> Allow | Deny(reason)`，
`oms.submit` 强制经过，无旁路：

| # | 规则 | 依据 |
|---|---|---|
| 1 | **kill switch**：`~/.dsh/trading-kill` 存在 → 拒绝一切订单 | 工作台一键创建/清除，删文件即恢复 |
| 2 | 模式一致：订单 mode == 模式文件 + 账户族匹配 | sim/live 互斥的执行侧延伸 |
| 3 | **交易日**：`is_trading_day`（按 calendar 的交易日白名单，**日粒度**） | 非交易日拒单——「非交易日：不提交订单」。**钟点层不在本规则**（`ctx` 没有时钟/日历）：人工下单的钟点由平台层**时段闸门**判（WP19 `trading_core.sessions` + `server/trading.py`），自动执行由守卫 9 的执行窗口判；原表「按 calendar + market_state / 非连续竞价拒单」的措辞与实际实现不符，2026-09-18 对齐 |
| 4 | 单笔风险 ≤ 权益 × `risk_per_trade` | risk_config.py 现有 5 参数 |
| 5 | 成交后单票市值 ≤ 权益 × `max_position_pct` | 同上 |
| 6 | 成交后持仓数 ≤ `max_positions` | 同上 |
| 7 | **日内熔断**：当日已实现+浮动亏损 ≥ `daily_loss_limit_pct` → 拒单 + 撤计划内剩余订单 + 置 halt | 次日自动清或手动清 |
| 8 | **计划一致性**：订单必须属于 frozen 且 hash 匹配的计划 | **拒绝任何计划外订单——窄门的技术保证** |

规则 8 是"工作台只有一个受约束执行入口"的技术落地：哪怕指令被伪造，
不在冻结计划里的订单也无法通过提交函数。

## 八、调度与运维

### 8.1 守护进程

`python -m trading_core.daemon`，单进程、无 LLM，systemd/cron 拉起。
状态文件 `~/.dsh/trading-daemon.json`：心跳时间、当前作业、下次运行队列
（心跳 > 5 分钟工作台标红）。

声明式调度表（`trading-platform.json`），按各市场交易日历触发：

| 时机 | 作业链 |
|---|---|
| 每市场收盘后 +30min | sync_bars → sync_fundamentals → data_quality |
| 数据就绪后 | run_signals → build_plan（仅启用的策略） |
| 每次执行后 + 每日固定 | reconcile → tca → daily_digest（摘要落盘供对话读取） |

「数据就绪」的判定：该市场 sync 作业成功完成 **且** data_quality 无 critical 缺口，
由 daemon 按作业链顺序推进；sync 失败或质量检查亮红灯则跳过当日信号与计划并告警。

### 8.2 指令目录（工作台 → daemon 唯一通道）

`~/.dsh/trading-commands/`：原子写 JSON 指令文件（含 nonce + plan_hash + 口令），
daemon 5 秒轮询，处理完移入 `processed/`。
**指令白名单仅 5 种**：`execute_plan / cancel_plan / kill / unkill / run_job`——
无任意代码、无任意参数。崩溃恢复靠 processed 目录 + 幂等键去重。

### 8.3 工作台变更与边界声明

新增 4 个受约束 RPC（白名单模式同现有）：

| RPC | 输入 | 输出 |
|---|---|---|
| `plan` | `{}` | 当前/历史计划：目标 vs 实际 diff、逐单风控报告、状态时间线 |
| `plan-execute` | `{plan_hash, expected_mode, confirmation?}` | live 需口令「确认执行」；服务端复核冻结状态/hash/mode |
| `schedule` | `{}` | daemon 心跳、作业历史、下次运行 |
| `reconcile` | `{}` | 最近对账差异、TCA 摘要、告警列表 |

页签新增「**计划**」（今日 diff 表 + 执行按钮 + 状态机时间线 + 审计链下钻）与
「**调度**」（daemon 健康 + 作业表 + 告警）。「审计」页升级为 `计划→订单→成交`
三级链路视图。

**边界变更声明**：工作台从"无下单入口"变为"**恰好一个**受约束入口——执行已冻结计划"；
对话侧保留等价工具 `plan_execute`，两条入口汇聚同一个核心函数、同一套风控；
其余不变（无聊天、无逐单表单、无 shell/LLM/token 接口）。

### 8.4 告警与恢复

- `alerts` 表分级（info/warn/critical）：数据缺口、对账差异、订单 unknown、熔断触发、
  daemon 心跳丢失；
- critical 同时置心跳文件标志位（工作台常驻红点）+ 可选桌面通知（配置项默认关）；
- 恢复原则（P4 延续）：daemon 崩溃 systemd 重启；执行中断订单留 `unknown` 由对账兜底；
  **先查券商再动手，不自动清除未知在途状态**。

## 九、测试与验收

- **离线单测**（延续现有套件全绿）：PIT 语义（announced_at 过滤）、复权合成、
  状态机全迁移、风控 8 规则逐条拒绝、幂等（超时→查询不重放）、对账差异检测、
  walk-forward 窗口正确性、指令队列端到端；
- **集成**：假时钟跑 3 个模拟交易日（同步→计划→执行→对账全链路，sim 账户）；
- **每阶段验收**：数据标注来源与时间、sim/live 不混、审计链完整可查；
- **live 准入仍按 P4 清单人工评估**，平台不自动升级。

## 十、实施路线图

| WP | 交付 | 验收标志 | 依赖 |
|---|---|---|---|
| **WP1** 数据基座 | core 包骨架 + store + calendar + sync（含回填）+ 质量检查 + **财报公告日双源合并**（富途报告期 + AKShare 披露日期） | 三市场日线回填完成、缺口报告可查、`announced_at` 覆盖率可统计 | 无 |
| **WP2** 研究层 | 因子/策略注册 + 组合回测 + walk-forward + 基准 | 横截面策略 OOS 报告产出、IC 检验可用 | WP1 |
| **WP3** 执行闭环 | planner + 风控硬拦截 + OMS 状态机 | **sim 全链路**：计划→执行→成交→台账一致 | WP1（WP2 可并行） |
| **WP4** 调度与面板 | daemon + 指令队列 + 工作台两页签 + 对账 + TCA | 无人干预跑 3 交易日（sim）、对账零差异 | WP3 |
| **WP5** 运维与文档 | 告警/恢复演练 + P4/架构文档修订 + 端到端验收 | kill switch/熔断演练通过、文档与实现一致 | WP4 |

## 十一、风险与已知限制（诚实清单）

> 数据类缺口的具体渠道、严重度与 2026-09-14 实测证据见 §十三 数据可得性审计。

1. **A股盘中实时不可用**（富途 `-9`）：分钟级策略只能用收盘/历史数据回测，
   盘中执行时点价格对 A 股用最近收盘降级标注。
2. **富途 rehab 是上下文炸弹**（单次 336KB 无分页）：复权因子同步必须分批增量，
   首次回填耗时较长。
3. **SQLite 规模上限**：3700 万行日线在个人规模可承受；若未来扩展全市场分钟级，
   需迁移 parquet/duckdb（列为演进项，不在本期）。
4. **daemon 持有券商凭证的边界**：daemon 经脚本通道调用富途（token 文件本机已有），
   其守卫是模式文件 + 风控硬拦截 + 冻结计划 + 口令，**不是** Harness 审批。
   这是半自动模式的固有边界变更，已在上文明示；日常仍建议只读授权 + 需要时 `--write`。
5. **X 渠道脆弱性**：queryId 与混淆算法随前端发版变化，降级路径 DOM 抓取（约 40-50s）。
   情绪因子因此永不进入信号计算，只作并列参考。
6. **对账差异的处置**：平台只告警 + 暂停，**不自动平仓**——差异处置永远由人决定。

## 十二、需同步修订的现有文档

| 文档 | 修订内容 |
|---|---|
| `docs/architecture.md` | 组件职责表加 trading-core 与 daemon；"明确不做"清单更新（工作台新增唯一执行入口的表述）；Host/Client 协议表加 4 个 RPC |
| `docs/P4-live-trading.md` | 实盘准入清单增加：daemon 执行链路演练、kill switch 演练、对账零差异连续 N 日 |
| `README.md` | 目录结构加 plugins/core；一键安装说明覆盖新库 |
| `docs/HANDOVER.md` | 交接注意增加：daemon 指令目录与 kill switch 的运维操作 |

## 十三、数据可得性审计（2026-09-14 实测）

> 结论：工作台设计所需数据项约 **90% 已实测可得或可本地派生**；真实缺口 4 项——
> 2 项有替代渠道、1 项有付费渠道、1 项已在 UI 设计中画了降级态。设计本身无需修改。

### 13.1 审计方法与证据基础

1. 全量工具体检（91 富途 + 8 非富途工具逐个实调，结论落库 `docs/TOOL-LIMITS.md`）；
2. 三市场通道覆盖实测（16 通道 × 3 市场 = 48 次，2026-09-14）；
3. 2026-09-14 针对本设计的关键待验证项，经 `trading_datasource.futu_mcp.call_tool`
   现场探测（探测脚本口径见 13.2 的参数注意事项）。

### 13.2 本轮新实测确认（此前未验证）

| 数据项 | 设计依赖处 | 实测结果与调用口径 |
|---|---|---|
| 指数 K 线（基准） | 研究/信号/组合 | ✅ `quote_history_kline`：`SH.000300` 沪深300、`HK.800000` 恒指、`US..IXIC` 纳指、`US..DJI` 道指均返回日线。标普指数代码（`US..INX`/`US.SPX`/`US.SP500`）无效，**用 `US.SPY` ETF 替代**。注意 `ktype` 必须传**整数**（日线=2），字符串报 `ret=-3` |
| 交易日历 | 调度/风控/事件 | ✅ `quote_trading_days(market="SH", start, end)`；`market` 必须**大写**；返回 `trade_date_type`/`trade_second`（休市、半日市可判） |
| 当前指数成分 | 信号 universe | ✅ `quote_valuation_index_component_stock_list(SH.000300)` 返回成分 + 市值 + 估值分位 |
| 行业/板块归属 | 组合行业分布 | ✅ `quote_owner_plate(SH.600519)` 返回板块列表；需按 `plate_type` 过滤行业类、剔除概念板块 |
| 基础信息 | 组合/计划 | ✅ `quote_stock_basicinfo`：`lot_size`（整手）、`listing_date`（次新过滤）、`suspension`（停牌）齐全；无行业字段（行业走 owner_plate） |
| 财报报表结构 | 质量因子/PIT | ✅ `quote_financials_statements` 可取 10 期利润表科目；**无公告日字段**（仅报告期 `date_time`）→ 见缺口 ① |

### 13.3 逐页可得性矩阵

| 页面 | 数据项 | 状态 |
|---|---|---|
| 行情 | K线/盘口/逐笔/资金流 | ✅ 港美全量；A股实时 ❌（缺口③，UI 已画降级态） |
| 信号 | 动量/波动/估值因子、ROE、情绪 | ✅ 全部（bars 派生 + 富途估值 + Yahoo ROE + 已接情绪渠道）；RankIC/分层本地计算 |
| 组合 | 持仓/权益/相关性/行业 | ✅ 券商查询已实测；相关性本地；行业归属本轮确认 |
| 风险 | 全部 | ✅ 本地（risk_config + OMS + 券商权益） |
| 执行 | 订单/成交/动作 | ✅ 已实测（历史订单必须传时间范围等坑已有规避） |
| 计划 | 目标权重/持仓/限价 | ✅ 本地 + 券商；A股下单限价用最近收盘（降级口径） |
| 调度 | 全部 | ✅ 本地 daemon + 本轮确认的交易日历 |
| 研究 | 回测/基准/walk-forward | ✅ bars 本地计算；基准指数本轮确认 |
| 事件 | 财报预约/分红/宏观/IPO | ✅ A股全量；港美股披露预约 ❌（缺口④） |
| 审计 | 链路/对账/TCA | ✅ 本地 OMS + 券商查询 |

### 13.4 缺口清单与出路

| # | 缺口 | 严重度 | 出路与决策 |
|---|---|---|---|
| ① | **财报公告日**（PIT 的 `announced_at`）：富途只给报告期 | 高（回测严谨性地基） | 双源合并：富途报表 + **AKShare 东财披露日期**（A股）+ Yahoo earnings dates（港美）。**WP1 必做** |
| ② | **历史指数成分**（PIT universe，防幸存者偏差） | 中 | 部分渠道：中证指数官网历史成分、AKShare 部分；港美难。降级：当前成分 + 界面标注「含幸存者偏差」（UI 已预留标注位） |
| ③ | **A股盘中实时**（报价/盘口/逐笔，`-9` 权限） | 中（只影响日内执行时点） | 付费渠道：富途 A 股实时行情订阅；或执行时点用日线收盘限价（已定）。免费无解，本期不做 |
| ④ | **港美股披露预约/财报日历** | 低（事件页覆盖面） | 渠道为港交所披露易/SEC EDGAR 网页抓取（重工程，本期不做）；UI 如实标注「不可用」 |

### 13.5 对实现的影响

1. **WP1 交付新增**：财报公告日双源合并（已在 §十 路线图 WP1 行更新），验收含
   `announced_at` 覆盖率统计；
2. 缺口②按降级方案实现，`universe` 表加 `bias_note` 字段承载标注；
3. 缺口③④维持设计内降级，不阻塞任何工作包；
4. 探测口径沉淀：`ktype` 整数、`market` 大写、指数代码映射
   （`SH.000300`/`HK.800000`/`US..IXIC`/`US..DJI`/`US.SPY`）写入
   `trading_datasource.market` 的常量与测试，避免实现期重新踩坑。
