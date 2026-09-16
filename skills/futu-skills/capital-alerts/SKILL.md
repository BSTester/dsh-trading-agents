---
name: futu-capital-alerts
description: 资金面异动观察（主力资金流/历史资金流/资金分布/量价配合）——当用户问资金流向、主力进出或量价背离时使用；数据由工作台实时经富途通道获取并如实标注。
---

# 资金面异动（capital_analysis · 工作台实时直通版）

参考富途官方 SkillHub 资金面异动 skill 的分析框架，数据通道对接工作台：
资金面数据由服务端**实时经富途通道获取**（quantwb `capital_flow` / `capital_flow_history` /
`capital_distribution`），技能不再直连富途，也没有只读降级路径。

## 数据通道

| 数据点 | 工具 | 说明 |
|---|---|---|
| 分钟级资金流（首选） | `mcp__quantwb__capital_flow` | 日内分钟级资金流入/流出时间序列；A 股分钟级实测可用 |
| 历史资金流 | `mcp__quantwb__capital_flow_history` | 按日回看，days 默认 30（1..1000）；做趋势与拐点 |
| 日内资金分布 | `mcp__quantwb__capital_distribution` | 大/中/小单累计净流入流出，判断主力行为结构 |
| 量能配合 | `mcp__quantwb__series` | K 线成交量，做量价背离判断 |
| 通道健康 | `mcp__quantwb__sources` | 确认各数据渠道可用性与 as_of，再引用数据 |

- 直通纪律：调用失败会返回明确错误信封（`trading/futu-unavailable` 通道不可用、
  `trading/futu-error` 富途业务错误）——如实转述并停止该数据点的分析，不要编造主力净流入
  等数值，也不要反复重试同一调用。A 股实时报价/盘口无权限（统一 -9），资金面不受影响。
- 南向资金、大宗交易等 SkillHub 示例里的数据点：工作台暂无对应工具时，
  如实告知"暂无可用通道"，不得用无关数据凑数。

## 工作流程

1. `mcp__quantwb__sources` 确认通道健康；富途 token 未配置时资金面通道返回 unavailable
   并指向 `scripts/futu_auth.py`——先引导用户完成安装/授权，再取数。
2. `mcp__quantwb__capital_flow` 取日内分钟级资金流；需要更长历史时用
   `mcp__quantwb__capital_flow_history`（days 按分析窗口给）；结构归因
   （主力/大单占比）用 `mcp__quantwb__capital_distribution`。
3. `mcp__quantwb__series` 取同期 K 线，对照资金流向做量价关系判断
   （放量上涨/缩量下跌、量价背离等）。
4. **输出格式**：

   > **资金流表**：时段 | 净流入/流出 | 主力占比 | 量能 | 数据来源 | 数据时间
   > **摘要**：一段话概括资金面方向与强度
   > **关键信号**：异动条目（如"连续 N 个分钟段净流入""量价背离"）
   > 以上内容基于公开信息整理，不构成投资建议

## 安全

- 本技能**只读**，不下单、不改单、不撤单。
- 资金面数据滞后且口径各异（主力/大单定义随源不同），结论必须带数据时间与来源；
  实时直通返回富途原始口径，不与本地缓存口径混算。
- 用户据信号要求交易时：写操作唯一路径是工作台通道（见 `futu-trading` 技能：
  quantwb `trade_*` 工具 + 用户在独立 Web 确认卡片批准）；禁止经富途侧任何工具或
  绕过工作台的方式下单。live 写通道未接入（当前仅 sim 可交易），如实说明。
