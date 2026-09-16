# 量化平台一次建成：sim 全自动流水线 + 流程可视化 + 研究院闭环（WP9–WP12）

> 状态：设计已与用户逐项确认（2026-09-16），待规格审查后进入实现计划。
> 前序：WP1–WP8（数据基座/研究层/执行闭环/调度/独立服务/独立平台/OpenAPI 统一）已交付。

## 一、背景与已确认的决策

现有平台的组件层已齐（PIT 数据、因子、回测、计划冻结、风控 8 规则、OMS、对账、TCA、
审计链、OpenAPI 交易通道），但日常闭环断成三截：调度链只到数据同步为止，计划生成靠
手工内联权重，计划执行 sim/live 都要人工点击；工作台各页是信息孤岛，看不出执行流程。
本规格把闭环补全，并把 Harness 升级为「研究院大脑」。

设计前用户已确认以下决策：

| 决策点 | 选择 |
|---|---|
| sim 自动化范围 | **全链路**：策略驱动计划生成 + 自动执行，全程无人点击；live 计划同样自动生成，但执行等人工 |
| 自动执行时点 | **次日开盘后延迟执行**（每市场可配置，如 SH 09:35）；风控规则 3 决定了收盘后生成的计划当天无法成交 |
| 流程可视化 | **新增「流程」页签**：每市场一条端到端阶段链，可下钻现有页面 |
| 总开关 | `auto_pipeline.enabled` **默认 false**；不显式打开不改变任何现有行为 |
| 接入架构 | **作业链 + 指令文件**：auto_execute 到点后写与人工点击完全相同的 execute_plan 指令，复用既有窄门、processed/ 留痕与幂等 |
| 资讯/情绪数据 | **PIT 快照落库攒历史** + 演进条款（满 250 交易日启动检验，过门槛转正）；不上来就进信号 |
| 研究院定位 | **Harness 研究院闭环**：子代理采集资讯 + 生成因子/规则假设 → 声明式规则提案 → 机械验证门 → 人工批准启用 → 既有流水线执行 |
| 信息源 | **三分采集面**：fin_news/fin_sentiment（富途常规）+ 富途 MCP（公告/资金流/筹码）+ last30days（社媒广度，可选组件，桥接纪律沿用） |
| 交付方式 | **一份规格，WP9–WP12 顺序交付**，一次建成；每个 WP 独立验收，全程仓库全绿 |

核心架构原则（全规格的不变量）：**LLM 在研究侧自由，在信号侧匿名**——Harness 负责
研究什么、采集什么、如何解读，产出经人批准后成为确定性规则；规则一旦上岗即可回测、
可复现、可审计；LLM 永远不直接产生订单、不写代码进核心库、不能自批自己挖的因子。

## 二、非目标（明确不做）

1. **不改 live 任何确认环节**：点执行 + 口令「确认执行」+ Web 确认卡片，全部不变。
2. **不做盘中实时/高频**：自动执行是日频时点动作，频率上限沿用 WP7 边界。
3. **不做逐单自动改撤**：熔断撤余单沿用 execute.run 现状，不新增自动改单逻辑。
4. **不让 LLM 写代码进核心库**：研究产出只能是声明式规则 JSON；需要新算子时走人工
   代码审查的核心库 PR，不提供动态执行通道。
5. **不自动批准**：候选规则的批准按钮只在 Web，不进 MCP 工具面（同 confirm-decide
   与设置端点先例）——Harness 不能自批。
6. **不做通知推送新渠道**：沿用 alerts 表 + 可选桌面通知（配置默认关）。

## 三、总体架构

```
┌────────────── 研究院（Harness + 子代理，LLM 自由区，WP12）──────────────┐
│ 采集代理群：公告/资金流（富途MCP）+ fin_news/fin_sentiment + last30days   │
│ 假设代理：资讯 + PIT 数据 → 候选因子/交易规则（声明式 JSON 提案）          │
│ 检验代理：跑 ic/t检验/分层/walk-forward → 验证报告                        │
│ 研报代理：research_publish → 工作台研究页（人工随时对话介入）              │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼ 声明式规则提案
        ┌──── 验证门（机械，WP12 补全 t 检验/分层/半衰期/换手）────┐
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
        风控 8 规则（唯一提交入口）→ OMS → broker（sim 自动 / live 人工）
                            ▼
┌── 流程页签（WP10）───────────────────────────────────────────────────┐
│ 每市场一条阶段链：sync/quality/factors/plan/execute/reconcile/digest   │
│ 只读既有事实（ran 标记 + 表），不造状态；auto_pipeline 开关徽章         │
└─────────────────────────────────────────────────────────────────────┘
```

## 四、WP9：sim 全自动流水线

### 4.1 配置（trading-platform.json 顶层新键）

```json
"auto_pipeline": {
  "enabled": false,
  "strategies": [{"market": "SH", "strategy": "watchlist_rsi", "watchlist": "SH"}],
  "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
  "reconcile_at": "19:00"
}
```

- 缺省无此键 = 功能关闭，现有行为零变化（硬约束）；
- **每轮 tick 现读配置**（platform_config 已是每轮读文件的既有口径），改配置约 60 秒
  内生效，无需重启服务；
- `exec_at` 为北京时间；美股受夏令时影响，默认值按夏令时写，文档注明切换需人工调整
  （不做自动 DST 换算——诚实简单）；
- `strategies[].watchlist` 引用 `watchlist` 配置的市场键，关注池为空则跳过并告警。

### 4.2 build_plan 作业（各市场链尾追加，factors_snapshot 之后）

1. **数据就绪门**：对关注池标的按日历校验 bars 新鲜度（复用 quality.freshness/gap_report
   口径）；不新鲜 → 跳过当日 build_plan + warn 告警（宁缺毋假，延续 §4.3 原则）；
2. **过期语义落地**：生成新计划前，将 `origin='auto'` 且 `status='frozen'` 且
   `as_of < 今日` 的旧计划置 `cancelled`（补齐原规格 §6.1「跨日计划 expired」从未实现
   的语义；**只动 auto 计划，手工计划不碰**）；
3. **策略产出权重**：`strategy.target_weights(conn, as_of)`，as_of = 最近已收盘交易日；
4. **计划生成**：`planner.build_and_freeze(conn, mode, strategy_id, target,
   broker_positions, prices, as_of)`——价格用本地库 PIT 最近收盘（不盘中取数），持仓经
   broker 只读查询（planner 既有口径：current_positions 一律从券商查询）；
5. **双模式都生成**：mode 取当前模式文件。sim 计划等待次日 auto_execute；live 计划
   冻结后等待人工执行（工作台计划页可见，流程与今日一致）。

### 4.3 auto_execute 作业（exec_at 时刻，交易日触发）

**八项守卫，全部满足才写指令；任一不满足 → 跳过 + info 告警（如实留痕）：**

1. `auto_pipeline.enabled == true`；
2. 模式文件 == sim（live 跳过并告警「计划等待人工执行」——自动生成≠自动执行）；
3. 无 kill 文件（`~/.dsh/trading-kill` 不存在）；
4. 无 halt（OMS 熔断状态为清）；
5. 存在 `origin='auto'` 且 `status='frozen'` 且 `as_of == 最近已收盘交易日` 的计划；
6. 该计划的市场 == 当前作业市场（per-market 计划，见 4.6）；
7. 该计划今日未被执行过（kv ran 标记幂等，同 daemon 既有口径）；
8. `expected_mode` 复核通过（写指令时携带，指令处理侧既有复核兜底）。

通过后 `commands.write_command(home, "execute_plan", {plan_hash, expected_mode})`——
**与人工点击落完全相同的指令文件**，由指令轮询处理，processed/ 留痕、nonce 幂等、
逐单风控 8 规则全部复用。自动执行 = 系统代替人点击，不是旁路。

### 4.4 reconcile / digest 入链

`reconcile_at`（默认 19:00）追加 `reconcile → tca → daily_digest` 作业——补齐原规格
§8.1 写明但一直未入链的「每日固定对账」；sim/live 通用，与执行动作解耦（晚间固定跑）。

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

- 假时钟单测：build_plan 触发/数据不就绪跳过/旧 auto 计划过期；auto_execute 八守卫
  逐项跳过路径 + 正常路径落指令；live 模式绝不自动执行；kill 存在绝不执行；
- 端到端：sim 模式下（enabled）假时钟连跑 2 个模拟交易日——收盘链自动生成冻结计划，
  次日 exec_at 自动写出 execute_plan 指令，指令轮询处理，订单经风控落 OMS，晚间
  reconcile/digest 完成，全程零人工；
- 开关关闭时行为与现状逐字节一致（回归）。

## 五、WP10：流程页签

### 5.1 pipeline 端点（GET /api/wb/pipeline，只读）

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
- 开关按钮走 settings 扩展（5.3），页面上明确提示「关闭时流水线不自动运行」。

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
  - **富途 MCP 公告/资讯**：公告类沿用既有 announcements 通道（WP1 merge-announcements
    已落 `announced_at`），本作业补资讯面快照；
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
走 WP12 验证门——与价格因子同一套门槛（IC t 检验显著 + 分层单调 + walk-forward OOS），
**过门槛转正为正式因子，不过就继续攒或放弃，不搞特殊通道**。转正前，资讯因子不得
出现在任何启用规则的 factors 列表里（规则解释器机械拒绝未注册/未通过检验的因子引用）。

### 6.4 WP11 验收标志

- 落库/降级/查询单测；last30days 缺席时的降级语义测试；
- 假时钟：作业入链后 sentiment_snapshots 按交易日累积、流程页阶段联动。

## 七、WP12：研究院闭环

### 7.1 研究院编排 skill（skills/research-institute/）

Harness 会话内的编排技能，定义四类子代理分工（复用 Harness 原生子代理能力）：

| 子代理 | 职责 | 主要通道 |
|---|---|---|
| 采集代理 | 标的/事件的资讯盘点：公告、资金流、新闻、社媒叙事 | 富途 MCP（只读）+ fin_news/fin_sentiment + last30days |
| 假设代理 | 从资讯 + PIT 数据提出候选因子/交易规则，落声明式提案 | 工作台数据（quantwb 只读工具）+ 采集代理产出 |
| 检验代理 | 对提案跑验证门（ic/分层/walk-forward），产出验证报告 | rules CLI + 因子检验工具 |
| 研报代理 | 综合产出研报并发布 | research_publish（研究页可见） |

- **人工随时介入**：对话即介入——问资讯影响、要求深挖、否决假设，都是研究院的
  正常输入；12 角色深度流程（trading-agents）仍是单标的深度研究的重型入口，
  研究院 skill 是持续性的因子/规则生产线；
- 产出落 `rules/` 候选目录 + rules 表（7.3），全程带 research_run_id 溯源。

### 7.2 声明式规则协议（rules JSON）

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
  检验（如攒数中的资讯因子）的引用直接拒绝提案（fail-closed）。

### 7.3 rules 表 + 规则解释器

- 新表 `rules(rule_id PRIMARY KEY, spec TEXT, status TEXT, validation TEXT,
  created_at, approved_at, approved_by)`——status: candidate/validating/passed/failed/
  enabled/disabled；validation 存最近一次验证报告摘要；
- 规则解释器（core 新模块 `rule_engine.py`）：rules JSON → Strategy 协议适配实例
  （universe(as_of) → target_weights(as_of)），机械执行声明；通过的规则批准后即成为
  `auto_pipeline.strategies` 的合法取值（strategy 填 rule_id）；
- 验证门补全（ic CLI / factors 扩展）：IC 均值 **t 统计与 p 值**、**5 分位分层收益
  及单调性判定**、**因子衰减半衰期**、**换手率**；walk-forward OOS 复用既有实现；
  验证报告落 rules.validation。

### 7.4 候选池与审批（工作台研究页扩展）

- 研究页新增「规则候选池」区：候选规则列表（假设/因子/验证报告/状态）；
- **人工一键批准 = 启用**（批准按钮只在 Web，不进 MCP 工具面）：批准后 status=enabled，
  该规则即可被 `auto_pipeline.strategies` 引用，sim 下次 build_plan 即生效；
  否决 → disabled 留档；
- 启用随时可停（研究页/流程页开关）；live 模式的执行确认环节不因规则来源有任何变化；
- 批准是策略上岗的唯一通道：Harness 可以提交一百条提案，没有批准，一条也进不了
  auto_pipeline。

### 7.5 边界纪律（汇总）

1. LLM 不写代码进核心库（7.2）；
2. Harness 不能自批（批准只在 Web 端点，不进模型工具面）；
3. 社媒证据三要素（平台/时间/互动数），取不到标注「未核实」；
4. 社媒情绪永不直接触发交易（last30days 桥接纪律原样沿用）；
5. 资讯因子未满 250 交易日不得进规则 factors（6.3 演进条款）；
6. 一切执行仍过风控 8 规则唯一入口，kill/熔断对自动链同样一票否决。

### 7.6 WP12 验收标志

- 规则解释器单测：声明 → 权重、非法因子引用拒绝、combine 算子白名单；
- 验证门统计单测：t 统计/分层单调/半衰期在构造数据上的正确性；
- 审批流闭环：candidate → validating → passed → 人工批准 → enabled → build_plan
  消费；未批准规则绝不进 auto_pipeline；
- 工具面回归：MCP 工具清单不含 rules 批准端点；
- 端到端（假时钟 + sim）：研究院产出一条规则 → 检验通过 → Web 批准 → 次日
  build_plan 按新规则生成计划 → auto_execute 执行 → 流程页全程可见。

## 八、测试总则

- 既有 52 Python + 17 Node 测试全程保持全绿；每个 WP 附带离线单测（假时钟注入，
  无网络依赖；last30days/富途通道全部注入假件）；
- 集成验收：WP9 末做 sim 两交易日无人干预全链路；WP12 末做规则从挖掘到执行的
  端到端演练；
- live 准入维持 P4 清单人工评估不变，本规格不改变任何 live 准入条件。

## 九、风险与诚实清单

1. **自动执行的可靠性依赖数据就绪门**：门太松会在坏数据上生成计划（风控与质量检查
   兜底），太紧会频繁跳过——保守取向：宁可跳过 + 告警，不冒险执行。
2. **美股 exec_at 与夏令时**：默认值按夏令时写死，冬令时需人工调配置（文档注明）；
   不做自动 DST 换算。
3. **last30days 渠道脆弱**：上游发版/密钥失效 → 来源缺席（降级不报错）；情绪采集
   不进当日信号，链路韧性无影响。
4. **声明式规则表达能力有边界**：复杂假设（条件触发、事件驱动）首版算子集表达不了，
   需要核心库 PR——这是刻意的摩擦，不是缺陷。
5. **资讯因子转正周期长**：250 交易日 ≈ 一年，本规格交付时资讯因子仍在攒数期；
   规格保证的是「路径存在」，不承诺「当下可用」。
6. **指令双进程并发**：CLI daemon 与服务同跑时靠既有原子协议互斥，极端交叉仍可能
   告警噪音（不产生重复执行——nonce 主键兜底）；文档建议二选一常驻。
7. **build_plan 持仓查询依赖 broker 可用**：晚间查询失败 → 跳过当日计划 + 告警，
   次日重试；不做本地台账替代（台账二分原则不变）。

## 十、需同步修订的现有文档

| 文档 | 修订内容 |
|---|---|
| `docs/architecture.md` | 组件职责表加 rule_engine/研究院 skill；闭环链路图更新；端点表加 pipeline/auto_pipeline/sentiment-history/rules；「两条受约束写路径」表述更新为含自动链 |
| `README.md` | auto_pipeline 配置样例；研究院用法；目录结构 |
| `docs/HANDOVER.md` | 开关运维、exec_at 时区注意、规则审批运维 |
| `docs/RUNBOOK.md` | 全自动演练：kill 中断恢复、开关启停、规则否决回滚 |
| `docs/P4-live-trading.md` | live 准入清单不变，补记「live 计划已自动生成，执行仍全人工」事实 |
