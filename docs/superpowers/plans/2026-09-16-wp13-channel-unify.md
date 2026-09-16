# WP13 通道统一收口 实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md` §八 + 附录 A.3.10。全局约定见索引。
> 三套测试保持全绿；提交 `feat(datasource|core|platform): 摘要`。

**目标**：`futu_channel: openapi` 成为完整通道——sync 五项（rehab/statements/估值/分红/经济日历）迁 REST、模拟交易 9 端点 REST 化；mcp 保留回退；双通道等价。

**架构**：只换取数实现、不改落库口径——同一批清洗/落库函数在两通道下产出同形结果；`core_broker` sim 分支在 openapi 通道下走 `OpenApiSimTrade`，能力边界（限价当日单）不变。

---

### 任务 1：sync 五项迁 REST

**文件**：修改 `plugins/core/python/trading_core/sync.py`（取数函数加 channel 分派）、`plugins/datasource/python/trading_datasource/futu_openapi.py`（`OpenApiBasicData.rehab` 已在 WP12、补 statements/valuation/dividends 三个 F10 端点方法——WP12 任务 3 已含 statements/dividends/valuation-detail，本任务只做接线）；测试 `tests/test_wp13_sync_channel.py`。

- [ ] **步骤 1：失败测试**（双通道假件等价）：

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

- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`sync.py` 取数入口统一 `_fetch(home, tool_name, openapi_method, **params)`（读 `futu_channel`；openapi 优先，凭据缺失回退 mcp）；经济日历（events 页的 `futu_economic_calendar`）、估值（`factors.valuation_values`）同样接 `_fetch`。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): sync 五项通道分派（openapi 优先 mcp 回退，落库口径不变）"`

### 任务 2：OpenApiSimTrade + core_broker sim 分支

> **前置阻断点（WP12 任务 1 锁定表 §D.5，2026-09-16 实测文档）**：官方模拟交易文档写明
> 「uid 由**登录态 header** 自动透传」，而仓库 OpenAPI 凭据是 AppKey 签名 / OAuth 服务端
> 凭据——**兼容性未验证**。本任务**第一步必须用真实凭据实测**：通过则按锁定表迁移；
> 不通过则**保留托管 MCP `sim_trade_*` 通道**，并在 TOOL-LIMITS 登记结论（锁定表不构成
> 迁移承诺）。另：模拟交易文档**无错误码表**，实现时不得编造错误码映射。

**文件**：修改 `futu_openapi.py`（`OpenApiSimTrade`：account_list/cash_info/position_list/input_order/modify_order/cancel_order/order_list/history_order_list/max_buy_sell 九方法）、`plugins/core/python/trading_core/broker.py`（sim 分支接通道分派）；测试 `tests/test_wp13_simtrade.py`。

- [ ] **步骤 1：失败测试**（mock 传输）：
  ① 九方法路径/参数与锁定表（WP12 任务 1 已核对 sim-trade 族）一致；input_order 限价当日单参数锁定（order_type=1 对应口径照 TOOL-LIMITS）；
  ② `broker.place` 在 openapi 通道下走 `input_order`，mcp 通道零变化（既有 test_core_broker 回归）；
  ③ 改单策略：sim 下 modify → **撤旧重下**（沿用现状，注释注明官方模拟改单可靠性待实测，实测通过再切原生——登记 TOOL-LIMITS 待办）；
  ④ 超时/传输异常 → unknown（先查不重放，既有铁律在 sim REST 路径同样成立）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`broker.py` 的 sim 调用统一经 `_sim_call(home, method, **params)` 通道分派（与 sync 的 `_fetch` 同构，放 `trading_core/channel.py` 单一实现，两处共用）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): 模拟交易九端点 REST 化（channel 分派共用，先查不重放不变）"`

### 任务 3：双通道等价 + sim 全链路回归

**文件**：测试 `tests/test_wp13_e2e.py`；文档。

- [ ] **步骤 1：等价性测试**：rehab/statements/估值/分红/经济日历五项 × 两通道 = 10 个用例（假件喂同源数据 → 落库行逐字段相等）；sim 链路：`futu_channel=openapi`（假 client）下复用 `tests/test_wp9_e2e.py` 的两日时间线跑通（计划→执行→成交→台账一致）。
- [ ] **步骤 2：回退语义测试**：openapi 凭据缺失 → 五项与 sim 全部回退 mcp 且结果标 `channel` 字段；两通道皆不可用 → 如实报错零占位。
- [ ] **步骤 3：三套绿 + 文档**：`docs/architecture.md`「富途通道决策」表更新（openapi=完整通道，mcp=回退）；`docs/TOOL-LIMITS.md` sim REST 实测口径与改单待办登记。
- [ ] **步骤 4：Commit**：`git commit -m "test(platform): 双通道等价与 sim 全链路回归（WP13 收口）"`

### WP13 验收（对照规格 §8.3）

- [ ] `futu_channel: openapi` 下行情/交易/同步/推送全 REST；mcp 回退完整；
- [ ] sim 计划→执行→成交→台账一致（openapi 通道）；三套全绿。
