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

本仓库根目录就是一个 Harness preset（对话模式），放入 Harness 的 preset 目录即被自动发现，无需重启。

> 说明：`dsh plugin add` 命令用于安装 profile 级插件包，对话模式（preset）的官方
> 安装方式就是放入 `~/.dsh/.agent-presets/` 目录。以下三种方式任选其一。

**方式 A · 一行命令**

```bash
git clone https://github.com/BSTester/dsh-trading-agents "$HOME/.dsh/.agent-presets/dsh-trading-agents"
```

**方式 B · 安装脚本**（方式 A + 富途 token 录入向导）

```bash
git clone https://github.com/BSTester/dsh-trading-agents && cd dsh-trading-agents && ./install.sh
```

**方式 C · 让 AI 帮你装**——把下面这段话直接发给你正在使用的 DeepSeek Harness 会话即可：

```text
请帮我安装 dsh-trading-agents 对话模式：
1. git clone https://github.com/BSTester/dsh-trading-agents "$HOME/.dsh/.agent-presets/dsh-trading-agents"（已存在则 git pull 更新）
2. 检查 $HOME/.dsh/.agent-presets/dsh-trading-agents 下是否有 agent.cordis.yml、preset.yml 和 skills/trading-agents/SKILL.md，逐一确认存在
3. 检查环境变量 FUTU_MCP_TOKEN 是否已设置，没设置的话告诉我如何完成富途 OAuth 授权（见仓库 README「获取富途 token」），并说明可以先跳过、以降级模式使用
4. 完成后告诉我如何启动（dsh web → 新建会话 → 选「交易智囊模式」）
```

## 一键启动

```bash
dsh web
```

在 Web 界面新建会话时选择 **「交易智囊模式」** preset，然后直接说：

> 分析一下 00700.HK

几分钟后你会得到：四位分析师报告、多空辩论实录、结构化终审结论（Buy/Overweight/Hold/Underweight/Sell 五档评级 + 参考入场价 + 止损 + 仓位建议）。

## 获取富途 token（可选，推荐）——一条命令，弹出授权页

没有 token 也能用——行情/新闻自动降级到 web 搜索；配了 token 才有富途的 K 线、财务、研报与交易工具。

```bash
bash "$HOME/.dsh/.agent-presets/dsh-trading-agents/scripts/futu-auth.sh"
```

脚本会自动完成整个 OAuth（Authorization Code + PKCE）：注册客户端 → **弹出/打印富途授权页链接** → 你登录富途账号点确认 → 自动换取 token 存入 `~/.dsh/futu-token`（权限 600）。默认只申请只读 scope；要开通交易功能加 `--write`。

会话内也可以：直接对 AI 说"帮我接通富途授权"，它会自己运行这个脚本并把授权链接展示给你。

> 技术细节：富途远程 MCP 是标准 OAuth2（Authorization Code + PKCE，支持 RFC7591
> 动态客户端注册），token 走 refresh_token 刷新、`/oauth2/revoke` 吊销。token 文件
> 只存本机 `~/.dsh/futu-token`，预设组合在加载时自动读取，不进任何代码或仓库。

**安全说明**：交易默认模拟盘；真实下单强制双重人工确认；日常建议只授权只读 scope。

## 目录结构

仓库根目录即 preset 目录（放入 `~/.dsh/.agent-presets/` 即完成安装）：

```
├── agent.cordis.yml       # 对话模式组合：persona + 工具 + 富途 MCP 桥
├── preset.yml             # 模式元数据（名称/介绍）
├── skills/trading-agents/ # TradingAgents 六角色工作流技能（v1 引擎，含首次授权提示）
├── plugins/trading-agents/ # v2 确定性编排插件（脚手架，见 docs/architecture.md）
├── install.sh             # 更新 + 富途 token 录入向导
└── docs/architecture.md   # 架构与路线图
```

## 免责声明

本项目输出均为 AI 研究性质内容，不构成任何投资建议。市场有风险，决策需独立。
