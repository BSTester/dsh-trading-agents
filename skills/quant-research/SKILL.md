---
name: quant-research
description: 量化研究五阶段流水线（PDAT→PAAT→PCPT→PRT→PET）——把「数据准备→因子分析→候选池→组合风控→提案」跑成可验收的研究产出；当用户说「跑一轮研究/做因子研究/看候选池/组合风险量/事件研究/统计套利」时使用。产出只进研究页与规则候选池，永不下单、永不启用策略。
---

# 量化研究五阶段（PDAT → PAAT → PCPT → PRT → PET）

**定位：研究侧生产线。** 任一阶段的产出只有两类——**研究结论**（因子矩阵 / 候选池 /
风险量 / 研报）与**规则候选**（提案）；它们进**研究页**与**规则候选池**，
**绝不进订单、绝不启用策略**。启用只有一条通道：**人在 Web 点批准**。

## 何时用哪条路径（强规则）

| 用户诉求 | 走哪条 |
|---|---|
| "跑一轮研究"、"做因子研究"、"看候选池"、"组合风险量"、"事件研究/统计套利"、"五阶段流水线" | **本技能** |
| "挖个因子"、"提一条规则"、"巡检因子衰减"、"让研究院开工" | `research-institute`（研究院；**本技能是它内部那条流水线**） |
| "现在能不能买/卖"、"看下信号"、"跑个回测"、"短线怎么做" | `quant-trading`（快路径，秒级） |
| "帮我深度分析"、"出一份研报"、"多角度评估"、"辩论一下多空" | `trading-agents`（12 角色流程） |

分工一句话：快路径回答单点问题；12 角色产出单标的深度研报；研究院产出可回测的规则提案；
**本技能把研究跑成五阶段可验收的流水线**（阶段的字段名、排序键、上限都写死在平台实现里）。

## 第一件事：看清你在哪个工具面（决定你能调什么）

平台的 MCP 有两个端点，**同一个实现、同一份目录**，差别只在「转发闸门」：

| 面 | 谁在用 | 你能调什么 |
|---|---|---|
| `/mcp/ro`（只读面） | 决策 profile（quant-headless / quant-sdk） | **只有 `readOnlyHint=true` 的 `v3_*` 件 + 4 件直连保留**（`snapshot`/`admin_status`/`v3_gateway`/`v3_tools`） |
| `/mcp`（全局） | 值班链（`research_duty.sh`）、人 | `call_tool` 能转发**任意**工具——包括写类，**所以更需要你自己守边界** |

**推论（本技能的所有工具名都按这条选）**：基础面工作台工具（`factors` / `ic` / `series` /
`rules` / `risk` / `correlation`…）在只读面**一律被拒**（它们不发布 `readOnlyHint`，
只读面按 fail-closed 处理）。研究取数一律走带只读标注的 `v3_*` 桥接件：

```console
# 实测（2026-09-21，/mcp/ro 的 call_tool，真拒绝信封；handler 零调用）
$ call_tool(name="factors", arguments={"tickers":["US.NVDA"]})
{"ok":false,"error":{"code":"mcp/denied-by-policy",
 "message":"该工具不是只读，只读面 /mcp/ro 不放行；…（call_tool 名 'factors' 的注册表
 annotations 没有 readOnlyHint=true；只读面用 list_tools 卡片的 roCallable 字段区分…）"}}
```

先用 `mcp__quantwb__list_tools` 检索再调用（discovery 面下 `tools/list` 只有 6 件，
业务工具经 `mcp__quantwb__call_tool` 转发）。**卡片字段就是你的导航**：

* `roCallable`（只读面卡片上有）：`false` = 本面调用注定被拒，不要试；
* `required` / `optional`：必填与可选参数名（卡片不含完整 schema）；
* `concurrencySafe`：`false` 的工具**不要并发调用**（写类/状态机/全局限流相关的保守判定）；
* `renderable: true` + `formats: ["json","text"]`：该工具支持 `format:"text"`
  人类可读散文渲染（`format` 缺省 `"json"` = 规范 JSON 信封，机器读）。

## 五阶段

### ① PDAT —— 数据准备（Data）

| 项 | 内容 |
|---|---|
| 输入 | `market`（`SH`/`HK`/`US`）、`universe`（标的列表或该市场宇宙）、`window`（默认 20）、`as_of` |
| 工具 | `v3_market`（逐标的真实日 K）、`v3_markets_calendar`（交易日历）、`v3_sources_status`（各数据链健康与尝试结果）、`v3_news`（资讯面） |
| 输出 | 每标的 bar 序列 + 缺口/错误清单（`errors`）+ `source`/`as_of` |
| 验收 | ① 每标的 bar 数 ≥ `max(120, window×4)`（流水线的真实下限），不达标的**进 `errors`**，不静默丢；② 每条数据带 `source` 与 `as_of`；③ 取不到就写「无数据源·原因」，**不用估算值替代** |

### ② PAAT —— 因子分析（Analysis）

| 项 | 内容 |
|---|---|
| 输入 | PDAT 的 bars + universe；`classes`（并入哪些因子类别）、`forward_days`（IC 前瞻窗） |
| 工具 | `v3_factors_matrix`（横截面 z 矩阵 + 因子 IC 序列 + 六类覆盖率）、`v3_factors_registry`（因子注册表：六类 + 真实数据源 + PIT 口径 + 逐因子覆盖率）、`v3_sentiment`（个股资讯情绪：`score`/`coverage`/`top_terms`/`per_day`）、`v3_financials`（质量/成长原始报表） |
| 输出 | 每标的因子 z、`scoreSource`、`withFactors`/`analyzed` 计数、覆盖率与 `missingTickers` |
| 验收 | ① 每个并入的因子都能在注册表里查到 `class` 与 `source`（不许有「来历不明的因子」）；② 覆盖率不足的**不填 0**——取不到的维度不参与综合分，`basis` 里注明；③ IC 的前瞻窗必须显式（`forward_days`），样本不足如实报 `ml/insufficient-sample` 一类的错误码 |

实测（2026-09-21，`v3_factors_registry`，`market=US`，只读面）：

```json
{"ok":true,"market":"US","asOf":"2026-09-21","classes":["growth","quality","sentiment"],
 "registry":[{"key":"mom_20","class":"momentum","direction":1,
   "source":"workbench/factors（日 K 动量，series→factors 工具链）",
   "pit":"只用 ≤t 的日 K（因子在 t 收盘后可得）"}, …],
 "coverage":{"available":true,"tickers":["US.MSTR","US.NVDA"],
   "factors":[{"key":"mom_20","class":"momentum","classLabel":"动量","covered":2,"total":2,
     "coveragePct":100.0,"missingTickers":[]}, …]}}
```

### ③ PCPT —— 候选池（Candidates）

| 项 | 内容 |
|---|---|
| 输入 | PAAT 的综合分（`extendedZ` 优先，退 `compositeZ`）；`topN`（2..50） |
| 工具 | 只读回看：`v3_strategy` / `v3_brain` 的 `stages.PCPT`；自算：`v3_factors_matrix` 的 z 排序 |
| 输出 | `longs`（前 `topN`）+ `reduces`（末位）+ **`rankBy`（排序键，平台明写）** |
| 验收 | ① `rankBy` 必须写清用的是哪个键：`extendedZ（价量动量+质量+成长+情绪同权）` 或 `compositeZ（纯价量动量 z；扩维因子无数据）`；② `reduces` 只在 `len(ranked) > topN` 时非空（标的数 ≤ `topN` 时为空，**不是**把刚判「增持」的又列进「减持」）；③ 每个入池标的都能回溯到 ② 的因子值 |

### ④ PRT —— 组合风控（Risk / Target）

| 项 | 内容 |
|---|---|
| 输入 | PCPT 的 `longs`、组合权重（缺省等权）、`market`、行业红线 `limit_pct`（缺省 20） |
| 工具 | `v3_risk`（风控配置，**阈值以它为准**）、`v3_risk_analytics`（历史模拟法 VaR/CVaR、Beta/Alpha/IR、Kupiec POF、净值曲线）、`v3_risk_industry`（行业暴露与红线）、`v3_risk_funding_check`（事前资金检查：订单金额对真实可用购买力，**只回答够不够，不下单**） |
| 输出 | `weightPctPerName`、`capped`；组合风险量；行业暴露 |
| 验收 | ① 单笔权重 = `min(2.0, 100/topN)`（平台单笔上限 2.0%），触及上限时 `capped=true` **如实标注**；② 行业集中度按 `limit_pct` 核，超限要报出来；③ 风险量必须带 `benchmark` 与 `confidence`——基准不可用时为 `null` 并说明，**不许编一个基准**；④ 本阶段是**只读**的：没有任何订单、没有执行计划 |

### ⑤ PET —— 提案（Proposal / Target）

| 项 | 内容 |
|---|---|
| 输入 | 前四阶段的结论 |
| 工具 | 读：`v3_strategy`（最近一轮 run）、`v3_brain`（决策大脑只读视图）、`v3_research`（研报/研究 run/量化预览）、`v3_research_report_pdf`（研报出 PDF）；提案与研报落到**研究页**，规则候选进**候选池** |
| 输出 | `proposals[] = {ticker, action, targetWeightPct, basis, riskLevel, action_hint}`（落 `<DSH_HOME>/v3-strategy-runs.jsonl`） |
| 验收 | ① `action_hint` 恒为「经审批后由工作台受约束入口执行」；② PET 里**没有订单号/成交回报**（那是执行面的事，出现即越界）；③ 每条 `basis` 能追到因子值；④ 交付给用户的话术是「**提案已进候选池，等你到 Web 研究页点批准**」——不是「已下单」「已启用」 |

实测（2026-09-21 读最近一轮 run，`v3_strategy`，只读面，`market=US`）：

```json
{"ok":true,"run":{"asOf":"2026-09-20T06:48:52.933Z","market":"US",
 "stages":{"PDAT":{"bars":240,"universe":["US.NVDA","US.MSTR"],"errors":[]},
           "PAAT":{"analyzed":2,"withFactors":2,"scoreSource":"workbench/factors(z)","factorsError":null},
           "PCPT":{"longs":["US.MSTR","US.NVDA"],"reduces":[]},
           "PRT":{"capped":true,"weightPctPerName":2.0},
           "PET":{"proposals":2}},
 "proposals":[{"ticker":"US.MSTR","action":"增持","targetWeightPct":2.0,
   "basis":"综合动量 z=0.7071（mom_20=0.36952）","riskLevel":"低",
   "action_hint":"经审批后由工作台受约束入口执行"}]}}
```

> 跑**新的一轮**流水线（`v3_strategy_run`：一次跑完 PDAT→PET 并落盘研究轮）是**写类**，
> 只读面拒；工作台策略页的那个按钮**只由人工点击触发**（按钮文案自己就写着「产物是调仓建议
> 提案（不下单）」）。研究侧做的是**只读回看 + 自己按上面的口径复算**，**不要为「让结果好看」
> 而反复重跑**（那是多重检验作弊的同族动作）。

## 禁写边界（逐条执行，不可协商）

研究侧**永不下单、永不启用策略**。点名清单——**一个都不许调**：

| 类别 | 工具/动作 | 为什么 |
|---|---|---|
| 下单/改单/撤单 | `trade_place`、`trade_modify`、`trade_cancel`、`trade_max_qty` | 交易链路；只在工作台受约束入口由人执行 |
| 执行计划/切模式 | `plan_execute`、`switch_mode` | 改账户模式、执行/取消计划（风控总开关） |
| 本地台账/凭据 | `v3_oms_sync`、`v3_credentials`（`save`/`clear`） | 重写本地 OMS 台账 / 凭据写入（桥内封死） |
| 订阅意图 | `push_subscribe`、`push_unsubscribe` | 共享可变连接状态 |
| 本地运行台账维护 | `admin_cancel_run`、`admin_cancel_stale`、`admin_prune_runs` | 取消/清理 run 是维护动作；研究侧只读 `admin_status` / `admin_runs` |
| **只属于人的三个动作** | `rules-decide`（规则批准）、`auto_pipeline`（流水线开关）、`confirm-decide`（实盘确认） | **不在工具面里**，也不许经 shell/HTTP 绕过；批准只能由用户在独立 Web 完成 |

* 只读面已经替你挡了一层：以上工具在 `/mcp/ro` 的 `call_tool` 一律 `mcp/denied-by-policy`，
  **先于实参校验、handler 零调用**。但**全局 `/mcp` 面没有这层**（值班链要用它调
  `research_tasks_claim/report`）——在全局面上，边界靠你自己守。
* 允许的写只有一条：**研究产物落盘与任务回报**（`research_tasks_report` 回报任务终态；
  流水线轮次由平台侧落盘）。它们不改账户、不改策略状态。
* **领取的值班任务必须回报**（`research_tasks_report`，做不了也要 `ok=false` 写清原因）——
  领了不回报会卡在 `running` 直到 30 分钟超时回收，连续三次超时判 failed。

## 数据诚实（研究结论的可信度全靠这条）

1. 每条结论标 `source` 与 `as_of`；平台信封自带就照抄，不自造。
2. 取不到就说「取不到」（错误码 + 原因），**不用估算值/上次的值/0 代替**。
3. 覆盖率不足的因子**不参与**综合分，并在 `basis`/`notes` 里写明缺哪一维。
4. 自算的中间量（自己排序、自己算收益）必须能由工具返回的原始字段复现——写清用的是哪个键。

## 这个技能怎么被加载（挂载事实，供排障）

* 技能目录由 DSH 的 `skill-filesystem` 插件扫描：**根目录下一层**的 `<name>/SKILL.md`
  或根目录下的 `<name>.md` 视为一个技能。
* 本仓库的两个决策 profile（`platform/install/quant-headless/cordis.patch.yml`、
  `platform/install/quant-sdk/cordis.patch.yml`）都配了：

  ```yaml
  - id: skill-filesystem
    config:
      customSkillDirs:
        - /home/penn/workspace/dsh-trading-agents/skills/
        - /home/penn/workspace/dsh-trading-agents/skills/futu-skills/
  ```

* 因此 `skills/quant-research/SKILL.md` 与本仓库既有的 `quant-trading` / `research-institute`
  **同形同根**（都在 `skills/` 下、都是一层目录 + `SKILL.md`），随 profile 挂载自动进技能目录，
  无需改任何配置。`skills/futu-skills/` 单独列出来是因为它的子目录（`sentiment/` 等）
  还要再下一层——那属于另一个根，不是本技能的位置。
* front-matter 只有 `name` + `description` 两个必填字段（`name` 即技能名，目录名需与之一致）。
  改完技能文件**不需要重启平台服务**：技能在会话开场按目录现扫。

## 一页速查

| 阶段 | 主要只读工具 | 关键验收 |
|---|---|---|
| PDAT 数据准备 | `v3_market`、`v3_markets_calendar`、`v3_sources_status`、`v3_news` | bar 数 ≥ `max(120, window×4)`；缺口进 `errors`；带 source/as_of |
| PAAT 因子分析 | `v3_factors_matrix`、`v3_factors_registry`、`v3_sentiment`、`v3_financials` | 因子有 class/source/PIT；覆盖不足不填 0；IC 前瞻窗显式 |
| PCPT 候选池 | `v3_strategy`、`v3_brain`（回看）+ `v3_factors_matrix`（自算） | `rankBy` 明写；`reduces` 只在 `len>topN` 时非空 |
| PRT 组合风控 | `v3_risk`、`v3_risk_analytics`、`v3_risk_industry`、`v3_risk_funding_check` | 单笔 ≤2%；`capped` 如实；benchmark/confidence 不编 |
| PET 提案 | `v3_research`、`v3_research_report_pdf`（读） | `action_hint` 恒为「经审批后由工作台受约束入口执行」；无订单号 |

**收尾话术**：研究产出 = 研报 + 候选池里的提案；启用与否是**人的动作**。
