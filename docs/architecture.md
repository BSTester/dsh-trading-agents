# 架构与路线图

## 总体形态

一个 DeepSeek Harness **agent preset（对话模式）** + 若干插件，全部通过 GitHub 仓库分发。

```
┌─ 对话模式 preset（本仓库 preset/ + skills/）───────────────────────┐
│  persona      投研助手人设与安全边界（模拟盘默认、双重确认）           │
│  tool-web     web 搜索/抓取（无 token 时的数据降级通道）             │
│  tool-fs      决策记忆读写（.tradingagents/memory.md）              │
│  skill-*      trading-agents 技能：六角色流水线方法论                │
│  futu-mcp     dsh-mcp-client → https://mcp.futunn.com/mcp          │
│               （K线/财务/研报/新闻/筛选/模拟与真实交易）               │
└──────────────────────────────────────────────────────────────────┘

┌─ v2 插件（plugins/trading-agents，npm 包）────────────────────────┐
│  确定性编排引擎：registerTool(run_trading_analysis)                │
│  六角色状态机用 llm 服务直调（quick/deep 双模型）                    │
│  数据经 ctx.tools.execute 调 mcp__futu__* 工具                     │
│  Client 半：tool.call.toolview 流水线进度卡片                       │
└──────────────────────────────────────────────────────────────────┘
```

## 为什么 v1 用 skill 驱动、v2 才插件化

- **v1（skill）**：模型按 SKILL.md 自己走流水线。半天可用，用于验证提示词质量、
  数据源覆盖与辩论效果。缺点：token 消耗高、流程非确定、无进度 UI。
- **v2（插件）**：同样的提示词固化进 Host 端状态机，直接调 llm 服务，流程可控、
  可重试、可观测，并加 Client UI。v1 验证过的提示词平移过去即可。

## 数据层

主通道：富途远程 MCP（Streamable HTTP + OAuth Bearer，官方维护，含真实/模拟交易，
免本地 OpenD）。降级通道：web 搜索/抓取 + 富途公开内容接口
`https://ai-news-search.moomoo.com/news_search`（免鉴权，新闻/公告/研报）。

## 路线图

- [x] v0.1 仓库脚手架：preset + skill + install.sh
- [ ] v0.2 端到端验证（无 token 降级模式 → 有 token 全量模式）
- [ ] v0.3 记忆复盘闭环打磨（pending → 真实涨跌幅 → reflection）
- [ ] v0.4 v2 插件：确定性编排引擎 + 双模型配置
- [ ] v0.5 Client UI：流水线进度卡片、辩论折叠视图
- [ ] v0.6 交易层：模拟盘一键下单 → 真实盘（双重确认）
- [ ] v0.7 npm 发布，install.sh 支持 `dsh plugin add` 直装
