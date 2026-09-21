# dsh-trading-agents

把 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 的 12 角色投研流水线装进 **DeepSeek Harness**：
对话与指令入口在 Harness；数据与交易能力来自[富途远程 MCP](https://github.com/FutunnOpen/futu-agent-plugin)
（OAuth 授权，免 OpenD）；独立的 **Web 工作台**（FastAPI 单进程，默认 `http://127.0.0.1:8397`）用于查看研报、
交易概要、持仓风险、K 线与量化预览，并切换模拟盘/实盘；同一批能力另有 `mcp__quantwb__*` MCP 工具面（当前 128 件工具）。

装上后你会得到：**12 角色投研**（四位分析师 → 多空辩论 → 研究经理裁决 → 交易员提案 → 三方风控辩论 → 组合经理终审）、
**快路径量化**（信号 / 回测 / 短线判断）、**研究院规则提案**（过机械验证门后必须由人在 Web 批准才上岗）、
**值班研究员**、以及上述**独立 Web 工作台**。

**边界**：研究侧永不下单、永不启用策略；下单 / 改单 / 撤单 / 切模式只经工作台受约束入口（时段闸门 → 风控红线 → 人工确认）；
数据诚实——取不到就说取不到，标注来源与 `as_of`，不用估算值替代。

## 安装

**方式 A · 只装对话模式**

```bash
git clone https://github.com/BSTester/dsh-trading-agents "$HOME/.dsh/.agent-presets/dsh-trading-agents"
```

```powershell
git clone https://github.com/BSTester/dsh-trading-agents "$env:USERPROFILE\.dsh\.agent-presets\dsh-trading-agents"
```

**方式 C · 让 AI 帮你装**（把下面整段直接贴给 Harness 对话模式）：

> 请把 https://github.com/BSTester/dsh-trading-agents **完整安装**到本机（**不要只装 skill 基础模式**——
> 那只会有 skill 与 MCP 工具面，工作台与量化引擎都跑不起来）：
> ① 克隆到 `$HOME/.dsh/.agent-presets/dsh-trading-agents`；
> ② 在该目录执行 `./install.sh`（Windows 为 `.\install.ps1`）。它会准备统一 Python 依赖
> （含 `yfinance`、`akshare` 等数据层依赖与量化核心）、走富途授权向导、装五个 Harness 插件——
> `workbench`、`fin-data`、`trading-engine`、`futu-keepalive`、`platform-autostart`——并构建工作台前端；
> ③ 自检：`python scripts/install_plugins.py check --repo "$HOME/.dsh/.agent-presets/dsh-trading-agents" --dsh-home "$HOME/.dsh"`，
> 通过标准是打出「✅ 安装完整」；
> ④ 装完**重启 dsh web**（Host 插件需重启才加载）；
> ⑤ 启动工作台：`cd platform && ~/.dsh/trading-venv/bin/python -m server.run`，确认 `http://127.0.0.1:8397/healthz` 返回 200。

## 一键启动

```bash
cd "$HOME/.dsh/.agent-presets/dsh-trading-agents/platform" && ~/.dsh/trading-venv/bin/python -m server.run
```

## 免责声明

本项目仅供研究与技术演示，不构成投资建议。实盘交易风险由使用者自行承担。
