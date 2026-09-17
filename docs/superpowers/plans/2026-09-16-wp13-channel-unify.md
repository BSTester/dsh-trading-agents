# WP13 通道统一收口 实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md` §八 + 附录 A.3.10。全局约定见索引。
> 三套测试保持全绿；提交 `feat(datasource|core|platform): 摘要`。

**目标**：`futu_channel: openapi` 成为完整通道——sync 五项（rehab/statements/估值/分红/经济日历）迁 REST、模拟交易 9 端点 REST 化；mcp 保留回退；双通道等价。

**架构**：只换取数实现、不改落库口径——同一批清洗/落库函数在两通道下产出同形结果；`core_broker` sim 分支在 openapi 通道下走 `OpenApiSimTrade`，能力边界（限价当日单）不变。

---

### 任务 0：`futu_openapi` 拆包 + 路径常量统一（WP12 质量审查遗留，紧跟的机械重构）

**触发**：WP12 后该文件约 2500 行（客户端核心 + 校验基类 + 9 个方法组）；且 **WP12 前的方法组
（OpenApiMarket 18 处、OpenApiTrade 15 处）仍内联字面量路径**，锁定测试只能核对命名常量——
内联路径绕过「禁止未核对路径落地」的不变式。WP13 再加 `OpenApiSimTrade` 会出现第三种写法。

**做法（对调用方零改动）**：改包 `trading_datasource/futu_openapi/`，`__init__.py` **原样再导出**
现有全部公开名（`OpenApiClient`/`CredentialStore`/`AppKeySigner`/`Pkce`/四个异常/
`parse_envelope*`/九个方法组类/`default_credential_path`），所有既有 `from ... import X` 不动。
模块切分建议：`errors.py`/`auth.py`/`envelope.py`/`client.py`/`validators.py`/`paths.py`（常量）/
`groups/{market,trade,dataplane,f10}.py`。同时把旧组 33 处内联路径提升为 `paths.py` 常量。

**护栏**：① 断言 `__init__` 再导出历史公开名集合的测试（防拆分静默破坏导入）；② 路径常量统一后，
把旧组也纳入锁定表绑定测试；③ 三套测试全绿 + `scripts/futu_openapi_check.py --dataplane` 复跑。
**零行为变化**是硬约束（纯移动 + 引用替换）。

### 任务 1：sync 五项迁 REST

**文件**：修改 `plugins/core/python/trading_core/sync.py`（取数函数加 channel 分派）、`plugins/datasource/python/trading_datasource/futu_openapi.py`（`OpenApiBasicData.rehab` 已在 WP12、补 statements/valuation/dividends 三个 F10 端点方法——WP12 任务 3 已含 statements/dividends/valuation-detail，本任务只做接线）；测试 `tests/test_wp13_sync_channel.py`。

- [x] **步骤 1：失败测试**（双通道假件等价）：

```python
def test_rehab_equal_shape_both_channels(self):
    # 同一假数据分别经 mcp call_tool 与 openapi client → sync.rehab 落库 adjustments 行完全一致
def test_channel_fallback(self):
    # futu_channel=openapi 且凭据缺失 → 取数回退 mcp 并在结果标 {"channel": "mcp(fallback)"}；
    # 两通道都失败 → 抛错（宁缺毋假），不写占位行
def test_statements_announced_at_preserved(self):
    # openapi 通道下 fundamentals 落库 announced_at 语义与 mcp 通道一致（None 如实落 None，双源合并不回归）
    ...
```

- [x] **步骤 2：验证失败** → **步骤 3：实现**：`sync.py` 取数入口统一 `_fetch(home, tool_name, openapi_method, **params)`（读 `futu_channel`；openapi 优先，凭据缺失回退 mcp）；经济日历（events 页的 `futu_economic_calendar`）、估值（`factors.valuation_values`）同样接 `_fetch`。
- [x] **步骤 4：通过**；三套绿。
- [x] **步骤 5：Commit**：`git commit -m "feat(core): sync 五项通道分派（openapi 优先 mcp 回退，落库口径不变）"`

> **A-1 补记（2026-09-17，阶段 A 审查修复）**：本任务当时只覆盖了 sync 五项，
> **三条腿仍硬编码 MCP**（与「openapi 为完整通道」的承诺不符）：K 线同步
> （`market.load_raw_bars`/`load_bars`）、交易日历（`calendar.sync_calendar`）、
> 指数成分股（`sync.sync_universe`）。现已补全，全部经 `trading_datasource.channel.fetch`
> 分派：
> - K 线 → `market.history_kline`（REST `autype` 枚举 int ↔ MCP 字符串的转换在调用点显式声明；
>   官方 `date` 为 int YYYYMMDD，`str()` 归一同时兼容）；
> - 交易日历 → `market.trading_days`（官方文档复核：响应与 MCP 逐字段同形，无需 adapter）；
> - 指数成分股 → `f10.valuation_index_stocks`（`GET /quote/valuation/index-stocks`，
>   **官方等价端点**，锁定表 §C.5 与本轮官方文档复核：`stock_list[].symbol` +
>   `pagination{has_more,next_key}`、limit≤50 同形）——**不存在「无等价端点」的腿**。
> 真机：三腿 `channel_used=openapi`（K 线 3 根最新 2026-09-17 / 日历 23 个交易日 /
> SH.000300 成分 300 只 6 页）；回归 `tests/test_wp13_sync_channel.py::SyncLegsChannelTests`
> 8 项 + 既有 `RawChunkTests` 钉死 `channel_name="mcp"`（开发机自身配置可能正是 openapi）。
> **范围边界（如实登记）**：本补记只覆盖这三条腿；workbench 脚本
> `instruments.py`（服务 `instrument` 端点）与 `quality.py`（服务 `quality` 端点）仍走
> MCP 容器，`positions.py` 非 sim 工具按翻译表边界保留 MCP——详见 TOOL-LIMITS §九之一
> 第 5 条与 architecture.md 通道表「未 REST 化的读取」。
> 详见 TOOL-LIMITS §九之一 第 5 条与 architecture.md 通道表。

### 任务 2：OpenApiSimTrade + core_broker sim 分支

> **前置阻断点（WP12 任务 1 锁定表 §D.5，2026-09-16 实测文档）**：官方模拟交易文档写明
> 「uid 由**登录态 header** 自动透传」，而仓库 OpenAPI 凭据是 AppKey 签名 / OAuth 服务端
> 凭据——**兼容性未验证**。本任务**第一步必须用真实凭据实测**：通过则按锁定表迁移；
> 不通过则**保留托管 MCP `sim_trade_*` 通道**，并在 TOOL-LIMITS 登记结论（锁定表不构成
> 迁移承诺）。另：模拟交易文档**无错误码表**，实现时不得编造错误码映射。

**文件**：修改 `futu_openapi.py`（`OpenApiSimTrade`：account_list/cash_info/position_list/input_order/modify_order/cancel_order/order_list/history_order_list/max_buy_sell 九方法）、`plugins/core/python/trading_core/broker.py`（sim 分支接通道分派）；测试 `tests/test_wp13_simtrade.py`。

- [x] **步骤 1：失败测试**（mock 传输）：
  ① 九方法路径/参数与锁定表（WP12 任务 1 已核对 sim-trade 族）一致；input_order 限价当日单参数锁定（order_type=1 对应口径照 TOOL-LIMITS）；
  ② `broker.place` 在 openapi 通道下走 `input_order`，mcp 通道零变化（既有 test_core_broker 回归）；
  ③ 改单策略：sim 下 modify → **撤旧重下**（沿用现状，注释注明官方模拟改单可靠性待实测，实测通过再切原生——登记 TOOL-LIMITS 待办）；
  ④ 超时/传输异常 → unknown（先查不重放，既有铁律在 sim REST 路径同样成立）。
- [x] **步骤 2：验证失败** → **步骤 3：实现**：`broker.py` 的 sim 调用统一经 `_sim_call(home, method, **params)` 通道分派（与 sync 的 `_fetch` 同构，放 `trading_core/channel.py` 单一实现，两处共用）。
- [x] **步骤 4：通过**；三套绿。
- [x] **步骤 5：Commit**：`git commit -m "feat(core): 模拟交易九端点 REST 化（channel 分派共用，先查不重放不变）"`

### 任务 3：双通道等价 + sim 全链路回归

**文件**：测试 `tests/test_wp13_e2e.py`；文档。

- [x] **步骤 1：等价性测试**：rehab/statements/估值/分红/经济日历五项 × 两通道 = 10 个用例（假件喂同源数据 → 落库行逐字段相等）；sim 链路：`futu_channel=openapi`（假 client）下复用 `tests/test_wp9_e2e.py` 的两日时间线跑通（计划→执行→成交→台账一致）。
- [x] **步骤 2：回退语义测试**：openapi 凭据缺失 → 五项与 sim 全部回退 mcp 且结果标 `channel` 字段；两通道皆不可用 → 如实报错零占位。
- [x] **步骤 3：三套绿 + 文档**：`docs/architecture.md`「富途通道决策」表更新（openapi=完整通道，mcp=回退）；`docs/TOOL-LIMITS.md` sim REST 实测口径与改单待办登记。
- [x] **步骤 4：Commit**：`git commit -m "test(platform): 双通道等价与 sim 全链路回归（WP13 收口）"`

### WP13 验收（对照规格 §8.3）

- [x] `futu_channel: openapi` 下行情/交易/同步/推送全 REST；mcp 回退完整；
- [x] sim 计划→执行→成交→台账一致（openapi 通道）；三套全绿。

### WP13 任务 3 验收记录（2026-09-17）

- **等价性**：`tests/test_wp13_e2e.py` 6 项全绿——同一份种子数据在两通道跑完两交易日时间线，
  `plans`/`orders`/`risk_checks` 逐字段相等（uuid/时间戳等非确定性字段排除），且断言基线
  非空（避免空真）。
- **REST 全链路**：openapi 通道下五类腿（accounts/positions/cash/history/place）全部走 REST，
  `sim_trade_*` 的 MCP 调用为 **0**；反向 mcp 通道 REST 零调用（回退不是「两边都发」）。
- **回退**：openapi 配置 + 凭据缺失 → **整链**回退 MCP 且与基线等价（REST 零尝试）。
- **不换通道**：REST 失败 → 如实跳过/失败 + 告警 + `tick_errors` 可见，MCP 零调用、零占位；
  两通道皆不可用 → 零计划/零订单/零预检行。
- **真机（2026-09-17）**：`scripts/futu_openapi_check.py --dataplane` **41/41 ok**；sim 只读
  四路经生产分派抽样可用（9 账户 / 8 持仓 / `total_asset` 权益 / 历史 1 单）；
  `f10.statements`→`report_list`、`f10.dividends`→`dividend_list` 与 MCP **同名**——
  双兜底第一支即真实契约，实现无需修改（TOOL-LIMITS §九之一）。
- **修复（本任务实质产出）**：`planner.plan_auto` 与 `reconcile.daily` 的缺省券商通道由硬编码
  `trading_datasource.futu_mcp.call_tool` 改为 `core_broker.sim_call(home)`（与
  `daemon._default_executor` 同口径）——消除「下单执行走 REST、取持仓与对账走 MCP」的通道分裂
  以及 REST 失败**静默换通道**（会把限频/权限错误伪装成 MCP 行为）。
  回归钉已验证有效：仅回退该修复，`test_rest_failure_does_not_switch_to_mcp` 与
  `test_openapi_snapshot_equals_mcp_snapshot` 转红（MCP 收到了 `sim_trade_*` 调用）。
- **遗留（如实登记）**：①sim 调用路径丢弃 `mcp(fallback)` 回退标记（HANDOVER §七）；
  ②sim REST 原生改单待更多样本（TOOL-LIMITS §九）。
- 三套测试全绿：Python `OK (skipped=5)` / Node `66 pass` / Web `233 pass`。

### WP13 阶段 A 审查修复（2026-09-17）

- **A-1（核心承诺未闭环）**：三条同步腿（K 线 / 交易日历 / 指数成分股）的硬编码 MCP 残留已修复
  （见任务 1 的「A-1 补记」）。验收口径自此成立：`futu_channel=openapi` 下**取数全 REST**，
  唯一非富途通道的取数是 `load_bars` 的新浪/Yahoo 降级源（设计如此，非残留）。
- **A-2**：本计划任务 1 四步勾选补齐；任务 0（`a7eecd3` 拆包 + 路径常量统一）、任务 2（`444e035`）、
  任务 3（`8f2a14d`）勾选状态与实际提交一致。
- **A-3**：`docs/architecture.md` 通道表 openapi 行改为**同一份可核对清单**（逐腿列出），
  不再用「完整通道」一词带过；未走富途通道的取数单列一行说明。
- 三套测试全绿：Python `Ran 1742 tests … OK (skipped=5)` / Node `66 pass` / Web `233 pass`。
