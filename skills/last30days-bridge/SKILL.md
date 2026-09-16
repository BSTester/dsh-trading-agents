---
name: futu-last30days-bridge
description: 近 30 天社媒/全网研究桥接——当 12 角色研究进入「市场情绪/叙事」阶段、需要个股/公司/事件的近 30 天社媒信号盘点或趋势发现时使用；与 fin_news/fin_sentiment 分工互补，社媒信号只生成假设，验证一律走 quantwb 工作台数据。
---

# last30days 桥接：近 30 天社媒研究 × 工作台验证

上游 last30days-skill（AI 智能体驱动的社媒/全网近 30 天研究引擎，MIT）是**可选组件**，
不随本仓库分发：由 `scripts/install_last30days.py` clone 到 `<home>/last30days-skill`
（`<home>` = `$DSH_HOME`，缺省 `~/.dsh`），经 agent.cordis.yml 的 customSkillDirs 挂载。
本技能是我方所有的方法论说明：教你在 12 角色研究里正确调用该引擎，并把它的输出接回
工作台数据通道完成验证。

## 何时使用

- TradingAgents 12 角色研究的**社交舆情分析师**阶段：为标的收集近 30 天社媒叙事与情绪；
- 个股/公司/事件的近 30 天全网信号盘点（Reddit、Hacker News、Polymarket、GitHub、
  StockTwits 等免密钥来源开箱即用）；
- 趋势发现（`--discover`）：先看什么主题在升温，再决定研究对象。

**与 fin_news / fin_sentiment 的分工**（互补，不互相替代）：

| 能力 | 覆盖面 | 定位 |
|---|---|---|
| last30days | 广度：YouTube 字幕、TikTok、Polymarket 真金白银概率、GitHub、HN、Reddit 等 | 近 30 天社媒/全网叙事与情绪假设 |
| fin_news / fin_sentiment | 富途源新闻与千股千评（fin-data 插件原生工具） | 个股资讯面与 A 股情绪的常规通道 |

## 运行方式

引擎路径：`<home>/last30days-skill/skills/last30days/scripts/last30days.py`
（Python ≥ 3.12，本机 trading-venv 的 3.13 兼容）。

```bash
# 0) 自检（不读 Cookie、不写文件；安装后先跑这个）
~/.dsh/trading-venv/bin/python ~/.dsh/last30days-skill/skills/last30days/scripts/last30days.py --preflight
# 1) 单主题近 30 天研究，输出 Markdown 简报
~/.dsh/trading-venv/bin/python ~/.dsh/last30days-skill/skills/last30days/scripts/last30days.py "比亚迪 海外扩张"
# 2) 需要程序化读取结构化结果时加 --emit=json
~/.dsh/trading-venv/bin/python ~/.dsh/last30days-skill/skills/last30days/scripts/last30days.py "特斯拉 Robotaxi" --emit=json
# 3) 趋势发现
~/.dsh/trading-venv/bin/python ~/.dsh/last30days-skill/skills/last30days/scripts/last30days.py --discover
```

- **未安装时**：先运行 `python3 scripts/install_last30days.py`（幂等：目录已存在则
  `git pull --ff-only` 更新；`--remove` 删除）。安装或更新后需**新建会话**才会挂载生效。
- 引擎依赖按需自理：yt-dlp、ScrapeCreators 密钥、arXiv/Techmeme 免费 CLI 等由上游首次
  配置时安装（见其 README.zh-CN.md）。免密钥来源开箱即用；密钥来源未配置时对应来源
  缺席（降级而非报错），简报里会如实标注。

## 与 quantwb 的组合研究流（核心）

**社媒信号不直接触发交易；它生成假设，quantwb 数据做验证。**

1. **假设**：last30days 简报给出近 30 天叙事/情绪假设（如「零售情绪一边倒看多」「某事件
   驱动的关注度跳升」），逐条摘出并标注平台、时间、互动数。
2. **验证**：对每条假设调工作台数据核对——`mcp__quantwb__rt_quote`（实时报价）、
   `mcp__quantwb__series`（K 线量价）、`mcp__quantwb__factors`（因子敞口）、
   `mcp__quantwb__capital_flow`（资金流）、`mcp__quantwb__events`（公告与事件）。
   叙事与量价背离时，把背离本身如实写进结论。
3. **结论**：区分「社媒叙事」与「工作台数据」两类证据，每条结论标注来源与数据时间；
   深度研究场景按 `trading-agents` 技能发布研报（sources 必填）。
4. **交易**：用户据结论要求交易时，写操作唯一路径是工作台通道——quantwb 的
   trade_place/trade_modify/trade_cancel 加用户在独立 Web **确认卡片**批准；
   社媒情绪永远不是下单依据，也绝不绕过工作台。

## 数据通道纪律

- last30days 是**公开社媒数据**：不经富途、不经交易闸门，与工作台读写完全隔离；
- 引用任何社媒证据必须带**平台 / 时间 / 互动数**三要素，取不到的如实标注「未核实」；
- 免密钥来源（Reddit、HN、Polymarket、GitHub、StockTwits）开箱即用；X、YouTube、
  TikTok、Instagram、Threads、Pinterest、LinkedIn、小红书、Perplexity、Brave 需按上游
  配置密钥或会话，未配置时这些来源缺席而不是报错；
- 第三方引擎代码在本机运行：只运行官方仓库 clone 的代码，更新只走 `git pull --ff-only`，
  不把本机凭据（浏览器 Cookie、富途 token 等）传给引擎。

以上内容基于公开信息整理，不构成投资建议
