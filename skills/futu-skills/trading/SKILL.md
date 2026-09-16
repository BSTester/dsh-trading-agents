---
name: futu-trading
description: 富途交易/持仓/行情一站式对话技能——当用户要查持仓、资金、订单，看标的行情，或要求下单/改单/撤单时使用；写操作一律走工作台 trade_* 工具并由用户在 Web 确认卡片批准。
---

# Futu API Skill（交易/持仓/行情 · 工作台对接版）

参考富途官方 Futu API Skill 的能力面，但**数据与交易通道全部对接本仓库工作台**（quantwb）：
模型发起、用户批准，两层合起来才是完整闸门。

## 数据通道

| 数据点 | 工具 | 说明 |
|---|---|---|
| 账户模式与全量快照 | `mcp__quantwb__snapshot` | 工作台快照：账户模式、订单事实（trade_summary）、待确认列表；每次会话先读它 |
| 持仓 | `mcp__quantwb__account_positions` | 券商真实持仓（按账户列出，不跨币种合并）；mode 缺省读模式文件 |
| 订单历史 | `mcp__quantwb__account_orders` | 固定最近 30 天窗口；查不到成交时先确认时间窗，不要断言"没有订单" |
| 资金 | `mcp__quantwb__account_funds` | 可用资金/总资产等原始字段 |
| 行情 K 线 | `mcp__quantwb__series` | ticker + period + limit；A 股实时报价权限不可用，用日线/分钟 K 线即可 |
| 标的解析 | `mcp__quantwb__instrument` | 名称/市场/整手；用户只给中文名时先解析再操作 |
| 待确认列表 | `mcp__quantwb__confirmation` | **只读**：确认只能由用户在独立 Web 卡片作答，模型不能也不会自批 |
| 下单 | `mcp__quantwb__trade_place` | 限价单；提交后阻塞等待用户在独立 Web 确认卡片作答，TTL 120 秒超时按拒绝（fail-closed） |
| 改单 | `mcp__quantwb__trade_modify` | = 撤旧单 + 按新参数重下（券商改单接口不可靠），风控按新参数全额预检 |
| 撤单 | `mcp__quantwb__trade_cancel` | 撤单同样要过 Web 确认卡片 |
| 计划执行 | `mcp__quantwb__plan` / `mcp__quantwb__plan_execute` | 跟随已冻结研究计划时使用；live 执行需用户口令「确认执行」 |
| 切回模拟盘 | `mcp__quantwb__switch_mode` | 只接受 live→sim；sim→live 一律拒绝，只能由用户在独立 Web 输口令完成 |

## 工作流程

1. **读模式**：调 `mcp__quantwb__snapshot` 确认当前账户模式（sim/live）。两个账户类型永不并存
   操作；每次以快照为准，不依赖对话里上一次的模式。用户问持仓/订单/资金时按当前模式直查。
2. **只读查询**：持仓 `mcp__quantwb__account_positions`、订单 `mcp__quantwb__account_orders`、
   资金 `mcp__quantwb__account_funds`、行情 `mcp__quantwb__series`。标的先经
   `mcp__quantwb__instrument` 解析（`000001.HK` 是港股长和，不是 A 股平安银行）。
3. **写操作（下单/改单/撤单）**：
   - 先向用户展示完整订单摘要（账户模式、标的、方向、数量、价格），并明确提示：
     「请留意『交易工作台』的确认卡片并作答，我在等你批准。」
   - 调 `mcp__quantwb__trade_place`（或 `trade_modify`/`trade_cancel`）。工具会阻塞等待用户
     在独立 Web 确认卡片作答，TTL 120 秒，超时自动拒绝（fail-closed）。
   - 确认只能由用户在 Web 卡片上产生，与 Harness 权限审批无关：任何权限档位下都必须确认，
     模型不得代答，也不得用 shell/HTTP 绕过这道闸门。
   - 被拒/超时后**不要自动重试同一笔**；先用 `mcp__quantwb__account_orders` 查单确认到底
     有没有成交，再决定下一步。用户改主意时应由用户重新发起。
4. **输出格式**（对齐官方 Skill 示例的形态）：

   > **持仓/订单/资金表**：代码 | 名称 | 方向 | 数量 | 现价/成本 | 盈亏 | 数据时间
   > **摘要**：一两句话概括账户状态或订单结果
   > **关键信号**：成交/拒单/资金约束等待办要点
   > 以上内容基于公开信息整理，不构成投资建议

## 安全

- **写操作唯一路径 = quantwb `trade_*` 工具 + 用户在独立 Web 确认卡片批准**。
  禁止经 `mcp__futu__` 或任何绕过工作台的方式下单——富途写类工具（`sim_trade_*`/`trading_*`
  的下单/改单/撤单动词）在本 Harness 内被工具策略一律拒绝并指引工作台，不要重试。
- **live 写通道未接入**（默认适配器 supports_live_write=False）：`trade_*` 在 live 模式下
  提交即在确认之前被闸门拒绝（`trading/broker-unavailable`），**当前仅 sim 可交易**。
  如实告知用户这个状态，不要引导用户反复尝试注定被拒的提交。
- 模式切换：Harness 侧只接受 live→sim；sim→live 只能由用户在独立 Web 输口令完成。
  切换模式不授权任何订单。
- 本技能不宣称代替券商：查询结果标注数据时间；分析结论不构成投资建议。
