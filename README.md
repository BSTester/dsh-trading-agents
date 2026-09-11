# dsh-trading-agents

DeepSeek Harness 对话模式：把 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 多角色投研流水线装进你的 AI 助手，数据与交易能力来自[富途远程 MCP](https://github.com/FutunnOpen/futu-agent-plugin)（免 OpenD、一次 OAuth 授权）。

```
市场分析师 ┐
舆情分析师 ├→ Bull/Bear 辩论 → 研究经理裁决 → 交易员提案 → 三方风控辩论 → 组合经理终审
新闻分析师 │
基本面分析 ┘
        ↓ 决策写入记忆，下次分析同标的自动注入历史教训
```

## 一键安装

```bash
git clone https://github.com/BSTester/dsh-trading-agents.git
cd dsh-trading-agents
./install.sh
```

脚本做三件事：安装对话模式（preset + 技能）到 `~/.dsh/.agent-presets/dsh-trading-agents` → 可选录入富途 token → 打印启动命令。

## 一键启动

```bash
dsh web
```

在 Web 界面新建会话时选择 **「交易智囊模式」** preset，然后直接说：

> 分析一下 00700.HK

几分钟后你会得到：四位分析师报告、多空辩论实录、结构化终审结论（Buy/Overweight/Hold/Underweight/Sell 五档评级 + 参考入场价 + 止损 + 仓位建议）。

## 获取富途 token（可选，推荐）

没有 token 也能用——行情/新闻自动降级到 web 搜索；配了 token 才有富途的 K 线、财务、研报与交易工具。

1. 确认你有富途/牛牛账号；
2. 按富途官方 Agent 插件的授权流程完成 OAuth（入口见 [futu-agent-plugin](https://github.com/FutunnOpen/futu-agent-plugin)），取得 Bearer token；
3. `export FUTU_MCP_TOKEN=<token>` 后再启动 dsh（install.sh 可代写入 shell rc）。

**安全说明**：token 只存你自己的环境变量；交易默认模拟盘；真实下单强制双重人工确认。

## 目录结构

```
├── preset/
│   ├── agent.cordis.yml   # 对话模式组合：persona + 工具 + 富途 MCP 桥
│   └── preset.yml
├── skills/trading-agents/ # TradingAgents 六角色工作流技能（v1 引擎）
├── plugins/trading-agents/ # v2 确定性编排插件（脚手架，见 docs/architecture.md）
├── install.sh             # 一键安装
└── docs/architecture.md   # 架构与路线图
```

## 免责声明

本项目输出均为 AI 研究性质内容，不构成任何投资建议。市场有风险，决策需独立。
