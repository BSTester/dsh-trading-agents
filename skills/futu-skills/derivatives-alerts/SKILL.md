---
name: futu-derivatives-alerts
description: 衍生品异动观察（期权到期/期权链/期权筛选）——当用户问期权链、衍生品异动或正股衍生品信号时使用；数据由工作台实时经富途通道获取并如实标注。
---

# 衍生品异动（derivatives_analysis · 工作台实时直通版）

参考富途官方 SkillHub 衍生品异动 skill 的分析框架，数据通道对接工作台：
期权数据由服务端**实时经富途通道获取**（quantwb `option_expiration` / `option_chain` /
`option_screen`），技能不再直连富途，也没有只读降级路径。

## 数据通道

| 数据点 | 工具 | 说明 |
|---|---|---|
| 期权到期日 | `mcp__quantwb__option_expiration` | 正股的到期日列表（HK/US/JP）；先定到期，再看链 |
| 期权链 | `mcp__quantwb__option_chain` | 按正股代码返回到期范围内的 CALL/PUT 合约；field_filter 可选透传（上游当前对该参数不生效） |
| 期权筛选 | `mcp__quantwb__option_screen` | 跨市场筛选合约；filter **必须带非空 field_filter 与非空 strategy**（见下方陷阱） |
| 正股背景 | `mcp__quantwb__series` | 正股 K 线：趋势位置、异动时段，与衍生品信号互相印证 |
| 标的解析 | `mcp__quantwb__instrument` | 正股与衍生品代码的市场/整手/到期要素解析 |

- **field_filter 陷阱（必读）**：`option_screen` 省略 field_filter 时，上游只返回 4 个
  默认字段、其余全 null——看起来像"数据缺失"，实际是请求不完整（见 docs/TOOL-LIMITS.md）。
  工作台参数层会直接拒绝缺 field_filter 的调用，所以每次筛选都要写明要哪些字段
  （如 `{"last_price": true, "volume": true}`）；strategy 也必填（类别码是整数，
  如 `{"market_category_list": [1]}`）。需要字段裁剪时用 option_screen，
  不要指望 option_chain 的 field_filter。
- 衍生品代码规则随市场差异大（港轮证、美期权），解析不了就问用户要准确代码，不要猜。
- 直通纪律：调用失败会返回明确错误信封（`trading/futu-unavailable` 通道不可用、
  `trading/futu-error` 富途业务错误）——如实转述并停止该数据点的分析，
  不得编造隐含波动率、街量等数值，也不要反复重试同一调用。

## 工作流程

1. `mcp__quantwb__instrument` 解析正股代码；与用户确认关注的衍生品要素
   （到期月份/行使价区间/认购认沽）。
2. `mcp__quantwb__option_expiration` 取到期日列表 → `mcp__quantwb__option_chain`
   按到期范围取链；跨市场批量找异动合约时用 `mcp__quantwb__option_screen`
   （记得带全 field_filter 所需字段列表）。
3. `mcp__quantwb__series` 取正股 K 线做背景：衍生品异动（成交放大、隐波抬升等）必须放在
   正股趋势里解读，不给孤立的衍生品信号。
4. **输出格式**：

   > **衍生品表**：代码 | 类型 | 到期 | 行使价 | 成交/持仓 | 异动特征 | 数据来源 | 数据时间
   > **摘要**：一段话概括衍生品市场对正股的定价倾向（如认购/认沽偏向）
   > **关键信号**：异动条目（含数值与时间）
   > 以上内容基于公开信息整理，不构成投资建议

## 安全

- 本技能**只读**，不下单、不改单、不撤单；不代用户构造期权组合订单。
- 衍生品杠杆与时间价值风险极高：输出必须带风险提示（到期归零、波动率回落、流动性风险）。
- 用户要求交易衍生品时：写操作唯一路径是工作台通道（见 `futu-trading` 技能：
  quantwb `trade_*` 工具 + 用户在独立 Web 确认卡片批准）；禁止经富途侧任何工具或
  绕过工作台的方式下单。live 写通道未接入（当前仅 sim 可交易），如实说明。
