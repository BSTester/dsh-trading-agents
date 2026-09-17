# WP14 研究院闭环 实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md` §九。全局约定见索引（§2.6 三层边界）。
> 三套测试保持全绿；提交 `feat(core|platform): 摘要`。

**目标**：Harness 研究院产出声明式规则提案 → 机械验证门（t 检验/分层/半衰期/换手）→ Web 人工批准=启用 → auto_pipeline 消费执行；LLM 不写代码、不自批。

**架构**：rules 表（spec JSON + 状态机）+ `rule_engine.py`（spec→Strategy 协议实例，factors 白名单校验）+ 验证门统计补全（factors.py）+ 审批端点（Web-only）+ 研究页候选池区 + `skills/research-institute/SKILL.md` 编排。

---

### 任务 1：rules 表 + 规则协议校验器

**文件**：修改 `store.py`（rules 表）；新建 `plugins/core/python/trading_core/rule_engine.py`（先只放 `validate_spec`）；测试 `tests/test_wp14_rules.py`。

- [ ] **步骤 1：失败测试**

```python
def test_validate_spec_ok(self):
    spec = {"rule_id": "news_momentum_v1", "hypothesis": "…",
            "factors": ["momentum_20"], "combine": "zscore_equal_weight",
            "universe": "watchlist.SH", "top_n": 5, "rebalance": "weekly",
            "provenance": {"research_run_id": "R1", "created_by": "harness",
                           "created_at": "2026-09-16 10:00:00"}}
    ok, errors = rule_engine.validate_spec(spec, registry={"momentum_20": object()})
    self.assertTrue(ok); self.assertEqual(errors, [])

def test_validate_spec_rejections(self):
    # ① factor 未注册 → 错误文案含「未注册」
    # ② combine 不在白名单 {"zscore_equal_weight", "ic_weighted"} → 拒绝
    # ③ top_n 超界/缺 rule_id/未知字段/provenance.created_by=="human"（提案必须产自 harness，
    #    人只在批准环节出现）→ 各自拒绝
    # ④ 因子已注册但 immature（REGISTRY_MARKED_IMMATURE 集合：sentiment/f10/short 域）→ 拒绝，
    #    文案含「未满 250 交易日」（规格 §6.3/§9.2 演进条款的机械执行）
def test_rules_table_lifecycle(self):
    # upsert_rule/get_rules/decide_rule(conn, rule_id, decision, by)
    # status 流转断言：candidate→(validating)→passed/failed→enabled/disabled
    # 非法流转（candidate→enabled 直接跳）→ ValueError
    ...
```

- [ ] **步骤 2：验证失败** → **步骤 3：实现**：rules 表进 `_SCHEMA`（索引 §2.3 字段）；`validate_spec(spec, registry, immature=frozenset())`；`IMMATURE_FACTOR_PREFIXES = ("sentiment", "f10", "short")`（域前缀判定，攒数期因子一律 immature，转正由演进条款另行处理）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): rules 表+声明式规则协议校验器（白名单/未成熟因子拒绝）"`

### 任务 2：rule_engine 解释器

**文件**：修改 `rule_engine.py`；测试 `tests/test_wp14_engine.py`。

- [ ] **步骤 1：失败测试**：
  ① `load_rule(spec, registry) -> 实例`：`.universe(conn, as_of)` 转发 universe 键解析（watchlist.SH → 关注池 SH 分片）；`.target_weights(conn, as_of)`：factors 逐标的取值 → z-score 等权合成 → Top-N 等权 1/N、其余 0；缺值标的跳过（宁缺毋假）；
  ② `ic_weighted`：权重取各因子近 120 日 |RankIC| 归一（读 factor_snapshots；不足 30 日 → ValueError「IC 加权需 ≥30 日历史」）；
  ③ rebalance=weekly：非再平衡日 target_weights 返回「沿用上次目标」——读 kv `rule:<id>:last_weights`，无则当日视为再平衡日；
  ④ 解释器对 spec 二次校验（load 时复用 validate_spec——领取侧双端校验）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`RuleStrategy` 类实现 `strategies.REGISTRY` 同协议（可被 `plan-auto` 按名消费——REGISTRY 注册表加 `register_rule(spec)` 动态项）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): 规则解释器——spec→Strategy 实例（zscore/ic_weighted、周度再平衡沿用）"`

### 任务 3：验证门统计补全

**文件**：修改 `plugins/core/python/trading_core/factors.py`（t 统计/分层/半衰期/换手）、`cli.py`（`ic` 输出扩展）；测试 `tests/test_wp14_stats.py`。

- [ ] **步骤 1：失败测试**（构造数据——已知分布的因子值+前向收益）：

```python
def test_ic_tstat_and_layering(self):
    # 因子=完美排序 + 前向收益=因子+噪声 → ic_report 返回
    # {"rank_ic_mean": ~0.9, "t_stat": >5, "p_value": <0.01,
    #  "layers": [{"q": 1, "ret": ...}, ...×5], "monotonic": True}
    # 随机因子 → t_stat 小、monotonic=False
def test_half_life_and_turnover(self):
    # 因子自相关按 lag 衰减（构造 AR(0.5)）→ half_life ≈ 1（lag 单位）
    # 换手率：Top-N 成员日变更比例的均值
def test_cli_ic_output(self):
    # ic 子命令输出含上述全部键；--json 机器可读
    ...
```

- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`factors.ic_report(factor_values_by_date, forward_returns_by_date, quantiles=5)`（RankIC 序列 t 检验用 `statistics.NormalDist` 近似，标注样本量；分层=分位组收益均值+单调性判定=Spearman(分位序,组收益)）；`half_life(ic_series_lag_autocorr)`、`turnover(top_sets)`；**不引入 numpy/pandas 新依赖**（标准库实现，样本量标注）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): 验证门统计——t检验/分层单调/半衰期/换手（零新依赖）"`

### 任务 4：rules-validate CLI + 审批端点 + 候选池 UI

**文件**：`cli.py`（`rules-validate --spec <file>`：跑 ic_report+walkforward 摘要 → rules.validation 落库 + status 流转）；`platform/server/app.py`/`compute.py`（`rules` 读端点 + `rules-decide` 动作端点，**均不进 MCP 工具面**）；`platform/web/src/pages/research.jsx`（候选池区）；测试 `tests/test_wp14_approve.py` + web 测试。

- [ ] **步骤 1：失败测试**：
  ① `rules` 端点 → `[{rule_id, hypothesis, factors, status, validation 摘要, approved_by}]`；
  ② `rules-decide {rule_id, decision: "enable"|"disable"}`：enable 仅对 status=passed 放行（candidate/failed → 拒绝）；落 approved_by="web"、approved_at；**TOOL_NAMES 不含两端点**（锁定测试）；
  ③ enable 后 `REGISTRY` 动态注册可被 `plan-auto --strategy <rule_id>` 消费（e2e 断言 build_plan 用了规则权重）；
  ④ UI 纯函数 `rulesTableRows(rules)` → 行渲染（状态徽章色：candidate 灰/passed 绿/enabled 蓝/failed 红）；研究页候选池卡：列表+批准/停用按钮（确认弹窗一句话）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**（审批语义与 confirm-decide 同构：动作端点、不进缓存、载荷白名单）。
- [ ] **步骤 4：通过**；三套绿；UI 文案 grep。
- [ ] **步骤 5：Commit**：`git commit -m "feat(platform): 规则验证 CLI+候选池审批端点与 UI（Web-only，批准=启用）"`

### 任务 5：研究院 skill

**文件**：新建 `skills/research-institute/SKILL.md`；`install/` 挂载（沿用 skills 目录既有安装方式，参照 `skills/quant-trading` 的 frontmatter 约定）。

- [ ] **步骤 1：编写**（内容要点，全部落在 SKILL.md）：
  - 触发词：挖因子/提规则/巡检衰减/研究某标的的基本面与资讯面；
  - 四子代理分工表（规格 §9.1 原文）+ 每代理的工具通道清单（quantwb 只读族 + fin_news/fin_sentiment + last30days + f10_detail/short_*）；
  - 硬规则：①产出只能是 rules JSON 提案（schema 样例照规格 §9.2）+ 研报（research_publish）；②提案先 `rules-validate` 自检再入库；③**不得**调用任何写端点（trade_*/rules-decide/auto_pipeline 在豁免表里点名禁止）；④社媒证据三要素；⑤未成熟因子（sentiment/f10/short 域）不得进 factors，只能在假设文字里讨论。
- [ ] **步骤 2：测试**：`tests/test_wp14_skill.py`（解析 SKILL.md：frontmatter 合法、含四子代理名、含禁用端点清单、schema 样例可被 validate_spec 解析通过）。
- [ ] **步骤 3：三套绿**。
- [ ] **步骤 4：Commit**：`git commit -m "feat(skills): 研究院编排技能——四子代理分工与产出纪律"`

### 任务 6：端到端演练 + 文档

**文件**：测试 `tests/test_wp14_e2e.py`；文档。

- [ ] **步骤 1：e2e（假时钟+sim 全假件）**：构造 spec（momentum_20 单因子）→ validate（t 检验过）→ rules-validate CLI（passed）→ `rules-decide enable` → 次日 build_plan 按 rule_id 生成计划 → auto_execute → 流程页 plan 阶段 summary 含 rule_id。断言：未 enable 的第二条规则**零消费**。
- [ ] **步骤 2：三套绿 + 文档**：architecture.md（rule_engine/研究院 skill/端点/三层模型 §3.1 落文档）、README（研究院用法一节）、HANDOVER（审批运维：批准/停用/回滚=disable）。
- [ ] **步骤 3：Commit**：`git commit -m "test(core): 规则提案→检验→批准→执行端到端+研究院文档"`

### WP14 验收（对照规格 §9.6）

- [x] 解释器/统计/审批流/工具面回归/端到端全过；LLM 零代码通道、零自批路径；三套全绿。

### WP14 任务 5+6 验收记录（2026-09-16）

- **任务 5（研究院 skill）**：`skills/research-institute/SKILL.md` 交付；persona 由两条
  路径扩为三条（INSTITUTE PATH）。`tests/test_wp14_skill.py` 22 项：frontmatter 合法、
  四子代理与工具通道在场、禁用端点清单（5 个在环写工具 + `rules-decide`/`auto_pipeline`/
  `confirm-decide` 三个人专属端点）、三要素/未成熟因子/不调阈值条款、**协议样例锁**
  （SKILL.md 里那段 rules JSON 必须通过真实 `validate_spec` 且能构造策略实例）、
  **工具引用真实性锁**（SKILL.md 引用的 `mcp__quantwb__*` 必须真在工具面；
  `fin_news`/`fin_sentiment` 在 fin-data 源码、`research_publish`/`run_trading_analysis`
  在 engine 源码可查）。
- **任务 6（端到端 + 文档）**：`tests/test_wp14_e2e.py` 12 项，链路全真实（`momentum_20`
  真实注册因子 + 构造价格路径 → 真实验证门 → Web 审批端点 → `plan-auto` → `auto-execute`
  → 真实指令轮询 → `execute.run` → 券商假件收到下单）。文档三处：architecture.md
  （三层任务模型章节 + `rule_engine`/研究院 skill 组件行 + `rules`/`rules-decide` 端点行）、
  README（WP14 研究院用法一节）、HANDOVER（审批运维条目）。
- **e2e 暴露并修掉一个 fail-open 缺陷（`e36f5a9`）**：`planner._resolve_strategy` 曾把
  `strategies.REGISTRY` 当一级事实来源，规则被解析一次即常驻进程内——此后用户在 Web
  停用该规则，长驻服务进程的下一次 `plan_auto` 仍会命中陈旧实例继续下单。`disabled` 是
  终态（`rule_engine.RULE_TRANSITIONS`），等于「停用随时可停」在进程内失效。
  修复：规则名一律回查 DB 状态（内置策略仍注册表优先）；非 `enabled` 或库内无记录时
  立即 `strategies.unregister_rule` 摘除实例（fail-closed），加载失败同样摘除。
  回归门：`test_disable_after_enable_stops_consumption`（**先解析一次**再停用才测得出，
  仅「启用→停用→解析」的写法不经过注册表、测不出该缺陷）。
- **三套测试**：Python 1887 项 OK（skipped=5）、Node 66 项 pass、Web 240 项 pass。
- **规格 §9.6 逐条**：规则解释器单测 ✅（任务 2）、验证门统计单测 ✅（任务 3）、
  审批流闭环 ✅（任务 4 + 本任务 e2e）、工具面回归 ✅（`rules` 进面、`rules-decide` 不进）、
  端到端（提案→检验→批准→次日计划→执行）✅。
- **未尽事项**：第三层（L3 值班研究员队列/定时器）属 WP15，本任务在 architecture.md
  三层模型里以「设计而非现状」显式标注，落地时同步更新该节。
