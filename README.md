# dsh-trading-agents

> 📐 [专业量化工作台方案](docs/QUANT-WORKBENCH-PLAN.md) · 📋 [交接与复审文档](docs/HANDOVER.md)：已完成总结、验证结论、未验证项、下一份计划、复审清单。

DeepSeek Harness 对话模式与插件组合：把 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 多角色投研流水线装进 Harness，数据与交易能力来自[富途远程 MCP](https://github.com/FutunnOpen/futu-agent-plugin)（免 OpenD、OAuth 授权）。

**Harness 是唯一对话与指令入口。** 工作台嵌在 Harness 内，只用于查看研报、交易概要、
量化信息预览及切换模拟盘/实盘；没有独立聊天、下单或撤单入口。

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
> 安装方式就是放入 `~/.dsh/.agent-presets/` 目录。仅克隆得到 **skill 基础模式**；
> 要使用工作台、研报发布、量化工具与账户守卫，请运行方式 B 的完整安装器。

> ⚠️ **方式 A 只装对话模式（skill 基础模式），不装插件。**
> 工作台的 9 个脚本与量化引擎的 2 个脚本都 `import trading_datasource`（统一数据层），
> 而**统一数据层只有安装器会解出**——只跑 `dsh plugin add` 装不出它，
> 表现是一句 `ModuleNotFoundError: No module named 'trading_datasource'`。
> 装完可用自检验证：

```bash
python "$HOME/.dsh/.agent-presets/dsh-trading-agents/scripts/install_plugins.py" check \
  --repo "$HOME/.dsh/.agent-presets/dsh-trading-agents" --dsh-home "$HOME/.dsh"
```

**方式 A · 一行命令**

```bash
# Linux / macOS
git clone https://github.com/BSTester/dsh-trading-agents "$HOME/.dsh/.agent-presets/dsh-trading-agents"
```

```powershell
# Windows (PowerShell)
git clone https://github.com/BSTester/dsh-trading-agents "$env:USERPROFILE\.dsh\.agent-presets\dsh-trading-agents"
```

**方式 B · 完整安装脚本**（preset + Python 依赖 + 富途授权向导 + 四个插件）

```bash
# Linux / macOS
git clone https://github.com/BSTester/dsh-trading-agents && cd dsh-trading-agents && ./install.sh
```

```powershell
# Windows (PowerShell)
git clone https://github.com/BSTester/dsh-trading-agents; cd dsh-trading-agents; .\install.ps1
```

**方式 C · 让 AI 帮你装**——把下面这段话直接发给你正在使用的 DeepSeek Harness 会话即可：

```text
请帮我安装 dsh-trading-agents 对话模式：
1. 把 https://github.com/BSTester/dsh-trading-agents 克隆到本机的 Harness 用户 preset 目录
   （$HOME/.dsh/.agent-presets/dsh-trading-agents，Windows 为 %USERPROFILE%\.dsh\.agent-presets\...；已存在则 git pull 更新）
2. 检查该目录下是否有 agent.cordis.yml、preset.yml 和 skills/trading-agents/SKILL.md，逐一确认存在
3. 如需完整工作台，运行 install.sh（Windows：install.ps1），安装依赖及三个插件；
   富途 OAuth 由安装向导处理，也可暂时不授权，以公开数据降级使用
4. 完成后告诉我如何启动（dsh web → 新建会话 → 选「交易智囊模式」）
```

## 一键启动

```bash
dsh web
```

在 Web 界面新建会话时选择 **「交易智囊模式」** preset，然后直接说：

> 分析一下 00700.HK

Harness 会使用原生工具与子代理完成四位分析师报告、多空辩论和终审结论
（Buy/Overweight/Hold/Underweight/Sell 五档评级 + 参考入场价 + 止损 + 仓位建议）。
缺少可靠数据时必须注明，不能编造价格或承诺完成时间。

## 工作台与指令示例

完整安装并重启 Harness、刷新页面后，点击右下角 **「交易工作台」**：

| 页面内容 | 数据从哪里来 |
|---|---|
| 研报结果与来源 | Harness 完成研究后经 `research_publish` 发布 |
| 交易概要 | 由富途工具响应归纳出的订单与下单/改单/撤单动作（按 order_id 去重；只读查询仅计数）|
| 券商持仓 | 富途真实持仓（模拟盘读模拟账户、实盘读真实账户），按账户小计、不跨币种合并；本地缓存 5 分钟 |
| K 线图 | 富途 K 线（默认日线，可切 60m/15m/5m/1m）；按周期缓存，切页签不重复取数 |
| 量化预览 | 在 Harness 请求 `quant_signal`、`quant_backtest`、`quant_report` 的结果 |
| 模拟盘/实盘切换 | 面板内显式操作；实盘输入「确认实盘」，但不授权订单 |

仍然在 **Harness 对话中**下达指令，例如：

> 分析一下 00700.HK，并把完整报告发布到工作台。
>
> 回测 600519 的双均线策略，把结果放到量化预览。
>
> 查询当前模拟账户的持仓和订单状态。
>
> 展示这笔订单的完整摘要，等我确认后再提交。

面板打开时每 3 秒刷新本地快照，关闭后停止刷新；不会自动调用模型、券商或回测。
**工具响应不等于成交**，当前尚未接入券商实时成交推送。没有账户数据时显示未知，
本地量化模拟资金也不会冒充富途账户余额。

> 富途各工具的**已知限制与规避方式**（哪些工具会静默返回空、A 股缺哪些实时权限、
> 哪些工具是上下文炸弹）见 [docs/TOOL-LIMITS.md](docs/TOOL-LIMITS.md)，
> 由一次 99 个工具的全量实调体检得出。

## 数据渠道优先级

先富途 MCP，其余按需降级；社交舆情走 API，网页抓取仅作降级：

| 优先级 | 渠道 | 路径 | 说明 |
|---|---|---|---|
| 1 | **富途 MCP** | 远程 MCP | 行情/K 线/财务/研报/交易，能取到的一律走这里 |
| 2 | X（**必需**） | `api/graphql` | 社区 `x-client-transaction-id` 实现，热启动约 3s；DOM 抓取为降级 |
| 3 | Reddit | `api/json` | 同源 `/search.json` 走登录态，结构化返回 |
| 4 | AKShare | 本地库 | A 股日线/千股千评等补充 |
| 5 | Yahoo / 网页搜索 | HTTP | 兜底 |

X 与 Reddit 共用同一个专属浏览器登录态（`~/.dsh/x-profile`），**登录一次长期有效**：

```bash
python ~/.dsh/.agent-presets/dsh-trading-agents/scripts/x_search.py --login
```

脚本输出的 `path` 字段标明本次实际路径（`api/graphql` / `api/json` / `web/dom`）。
X 的前端 queryId 与混淆算法会随发版变化，`x_api` 任一环节失败即自动降级到 DOM 抓取，
渠道不会整体不可用。详见 [docs/QUANT-WORKBENCH-PLAN.md](docs/QUANT-WORKBENCH-PLAN.md)。

**研报与量化共用同一套取数实现。** 行情路由、富途 MCP 客户端与回测核心都只有一份，
放在 `plugins/datasource`（`@bstester/dsh-datasource`）；安装器会把它解到
`$DSH_HOME/trading-python/` 并向交易 venv 写入 `.pth`，所以任何插件脚本都能直接
`from trading_datasource.market import load_bars`，无需设置 `PYTHONPATH`。
量化侧可选地复用 fin-data 的情绪渠道（`quant_signal(include_sentiment=true)`），
但情绪**只是并列参考输入，不参与信号计算**——否则回测结论无法复现。
详见 [plugins/datasource/README.md](plugins/datasource/README.md)。

## 获取富途 token（可选，推荐）——一条命令，弹出授权页

没有 token 也能用——行情/新闻自动降级到 web 搜索；配了 token 才有富途的 K 线、财务、研报与交易工具。

```bash
# Linux / macOS（Windows 用 python 运行同一脚本即可）
python "$HOME/.dsh/.agent-presets/dsh-trading-agents/scripts/futu_auth.py"
```

脚本（纯 Python 标准库，跨平台）会自动完成整个 OAuth（Authorization Code + PKCE）：注册客户端 → **弹出/打印富途授权页链接** → 你登录富途账号点确认 → 自动换取 token 存入 `~/.dsh/futu-token`（权限 600）。默认只申请只读 scope；要开通交易功能加 `--write`。

会话内也可以：直接对 AI 说"帮我接通富途授权"，它会自己运行这个脚本并把授权链接展示给你。

> 技术细节：富途远程 MCP 是标准 OAuth2（Authorization Code + PKCE，支持 RFC7591
> 动态客户端注册），token 走 refresh_token 刷新、`/oauth2/revoke` 吊销。token 文件
> 只存本机，预设组合在加载时自动读取，不进任何代码或仓库。

### token 有效期（重要）

富途 OAuth 返回的 `expires_in` 是 **7200 秒（2 小时）**，续期不会换发新的 refresh_token。
这很短，所以要特别注意：**过期后服务端不对所有工具返回 401，而是统一返回 `internal error`**，
表现上像"富途服务挂了"。

现在的处理方式：

- **MCP 通道**：preset 里的 `futu-keepalive` 行每 10 分钟检查一次，剩余不足 30 分钟时
  自动续期（复用同一份续期实现）。**但它只能让新会话拿到新 token**——已挂载的会话
  仍持有旧的那份，需**新建会话**才会重新读取组合；
- **脚本通道**：任何调用遇到该特征时会**自动用 refresh_token 续期并重试一次**，无需人工干预；
- 渠道状态页显示**剩余有效期**，剩余不足 10 分钟时给出预警；
- 自动续期也失败才需要 `--refresh`；refresh_token 失效才需重新完整授权。

### 授权范围的实测行为（重要）

**请求的 `scope` 不构成权限上限。** 实测：脚本请求 `quote:read accid:* trade:read`（不含
`trade:write`），而富途授权服务端最终下发的 scope 是：

```
quote:read quote:write trade:read trade:write accid:...
```

即**多授了 `trade:write`**。所以不要指望通过收窄 `scope` 参数来限制权限——真正的执行边界是
本仓库自己的两层：**账户模式互斥（默认 sim）** + **Harness 原生实盘审批**。
这带来一个必须知道的推论：模式守卫是**插件级**的，凭据本身允许写操作；
若绕过插件直接以 HTTP 调券商接口（本仓库明令禁止），模式守卫不会拦你。请在授权页面上
按需选择权限，并始终经由 Harness 的工具体系下单。

**安全说明**：未设置模式时默认模拟盘；完整插件模式下有账户工具守卫与 Harness 原生实盘审批，
会话仍需逐笔复述并确认订单。仅安装 skill 不具备插件级守卫。日常建议只授权只读 scope。

## 模拟盘 / 实盘互斥开关

模式保存在 `~/.dsh/trading-account-mode`，**重启后保留，不会自动回到 sim**。
完整插件按该模式拒绝另一类账户工具；有在途账户调用时禁止切换。

```bash
python ~/.dsh/.agent-presets/dsh-trading-agents/scripts/trade_mode.py        # 查看模式
python ~/.dsh/.agent-presets/dsh-trading-agents/scripts/trade_mode.py sim    # 恢复模拟模式
```

实盘启用只能由用户在工作台确认；脚本不再支持写入 live，模型也不能用参数替用户确认。
切回模拟盘可在面板操作，或在 Harness 请求 `quant_switch(mode="sim")`。
配置 `DSH_HOME` 时，安装器和运行时共同使用该目录。


## 目录结构

仓库根目录即 preset 目录（放入 `~/.dsh/.agent-presets/` 即完成安装）：

```
├── agent.cordis.yml       # 对话模式组合：persona + 工具 + 富途 MCP 桥
├── preset.yml             # 模式元数据（名称/介绍）
├── skills/trading-agents/ # Harness 十二角色、六阶段工作流
├── plugins/datasource/   # 统一数据层（库，非插件）：唯一 MCP 客户端/行情路由/回测核心
├── plugins/engine/       # 研究记录/发布、量化工具、账户策略；含 Python 量化实现
├── plugins/fin-data/     # 统一新闻与舆情工具
├── plugins/workbench/    # Host 状态服务 + Harness Client 面板与卡片
├── plugins/quant/        # 旧量化脚本兼容入口
├── install.sh / install.ps1 # 跨平台完整安装
└── docs/architecture.md   # 架构与路线图
```

## 实盘交易（P4）

尚未达到无人值守实盘准入条件。模式开关和展示面板可用不代表策略、券商对账、
监控熔断都已完成。实盘前置条件与边界见 [docs/P4-live-trading.md](docs/P4-live-trading.md)。

## 免责声明

本项目输出均为 AI 研究性质内容，不构成任何投资建议。市场有风险，决策需独立。
