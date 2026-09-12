# 交接与复审文档

> 本文档供接手人或复审者快速建立全局认知：做了什么、验证到什么程度、哪些未验证、
> 有哪些坑、下一步怎么走。所有结论以仓库代码与实测记录为准。

## 一、项目概述

在 DeepSeek Harness 上构建「金融分析交易工作台」，目标 = **投研出建议**（TradingAgents
十二角色流水线）+ **短期量化交易能力**（信号→回测→模拟盘闭环→实盘），并以
**模拟盘/实盘互斥开关**隔离账户。

- 仓库：https://github.com/BSTester/dsh-trading-agents （public）
- 形态：一个 agent preset（对话模式）+ 多个 DSH 插件包 + 一组 Python 脚本
- 交付原则：DSH 原生机制（插件包 + preset 组合 + MCP 桥 + 子代理），不硬编码不绕路

## 二、已实现内容

### 2.1 对话模式（preset，仓库根目录）

| 文件 | 内容 |
|---|---|
| `agent.cordis.yml` | 组合：persona + 持久shell双栈 + fs/web/技能 + 子代理 + 富途MCP桥 + fin-data行 + trading-engine行（两行默认 disabled，安装器装包后启用） |
| `preset.yml` | 模式元数据 |
| `skills/trading-agents/SKILL.md` | v1 工作流：12角色6阶段、各角色官方提示词核心、五类数据渠道、持仓/交易频率感知、交易员七组因素清单、模拟/实盘开关、记忆闭环 |

### 2.2 插件包（plugins/）

| 包 | 工具/能力 | 验证状态 |
|---|---|---|
| `@bstester/dsh-fin-data` | `fin_news`（富途快讯→AKShare→Yahoo RSS 路由降级）、`fin_sentiment`（X CDP + A股千股千评） | ✅ 脚本三市场实测；插件注册形状经沙箱验证 |
| `@bstester/dsh-trading-engine` | `run_trading_analysis`（12角色直调llm+记忆）、`quant_signal`/`quant_backtest`/`quant_report`/`quant_switch` | ✅ 回测/信号/台账脚本实测；llm 直调待重启验证 |
| `@bstester/dsh-trading-workbench` | Client 投研结果卡片（`tool.call.toolview`） | ⚠️ 待 dev:web 构建+重启 |
| `plugins/quant` | `backtest.py`（策略+成本+绩效）、`engine.py`（信号→风控→下单意图→台账） | ✅ 真实A股数据全流程实测 |

### 2.3 脚本（scripts/）

| 文件 | 功能 | 验证 |
|---|---|---|
| `futu_auth.py` | 富途 OAuth 全自动（动态注册+PKCE+本地回调+自动关窗+`--refresh`续期） | ✅ 真实授权成功、token 落盘 |
| `trade_mode.py` | 模拟盘/实盘互斥开关（`~/.dsh/trading-account-mode`） | ✅ 切换/隔离实测 |
| `install.sh` / `install.ps1` | 一键安装：克隆+venv+依赖+授权+插件安装+启用行 | ✅ install.sh 端到端实测 |

### 2.4 文档

- `README.md`：安装/授权/开关/实盘入口
- `docs/architecture.md`：架构与路线图
- `docs/P4-live-trading.md`：实盘操作手册（小额/风控/逐字确认流）

## 三、关键实测结论（数据源）

| 源 | 结论 |
|---|---|
| 富途 MCP 91 工具 | ✅ 鉴权/快照/财务/交易工具正常；**K线类工具**（history/cur_kline）裸 HTTP 返回 internal error（所有符号格式+完整握手均失败），需用 dsh-mcp-client 完整 SDK 复核是否我端问题 |
| 富途公开快讯 `ai-news-search.moomoo.com` | ✅ 免鉴权，三市场新闻可用 |
| AKShare（sina 源） | ✅ A股真实日线（回测已用）；东财源日线限流 |
| AKShare 新闻/千股千评 | ✅ 可用 |
| X（CDP 复用登录态） | ✅ 登录一次持久复用，真实推文实测 |
| Yahoo RSS | ✅ 港美股新闻可用；**yfinance 后端本机不可达** |
| Stooq | ❌ 已上 JS 验证 |

## 四、关键技术决策与坑（复审重点）

1. **工具注册配方**：`defineTool` + `dsh-tools` 钉版 `0.1.2-rc.1`（`*` 会拉不兼容新版）；parameters 用短形式、output.schema 必须显式 `additionalProperties`。
2. **tarball 安装**：pnpm 对 file: 依赖软链导致依赖解析失败，且按版本号缓存包内容——改代码必须升版本号 + `npm pack` + `dsh plugin --profile web add <tgz>`。
3. **loader 模块缓存**：进程内按包名缓存首版模块，改代码后**必须重启 harness** 才能生效（本会话内无法自证插件的最终状态，这是最大未验证点）。
4. **`!!js` 表达式**：loader 的 YAML 方言要求反引号表达式加引号（`!!js '...'`）。
5. **preset 行默认 disabled**：插件未安装时 disabled 行不解析不报错；安装器装包后用 sed 启用。

## 五、未验证项（诚实清单）

- [ ] 三个插件（fin-data/engine/workbench）在**重启后的真实会话**里的工具可见性；
- [ ] Client UI 卡片经 `dev:web` 构建后的渲染；
- [ ] `run_trading_analysis` 的 llm 直调端到端（依赖上述重启）；
- [ ] 富途 K线工具在 dsh-mcp-client 完整 SDK 下的真实行为（可能修好回测的数据源）；
- [ ] 实盘真实下单（需用户决策，非代码问题）。

## 六、下一份计划（供复审选择优先级）

| 方向 | 内容 | 依赖 |
|---|---|---|
| **A. 收尾验收**（建议先做） | 重启 harness → `./install.sh` → 逐一验证三插件工具 + 投研端到端 | 重启 |
| **B. 富途 K线排障** | 用会话内 MCP 工具验证 history_kline；若可用则回测接入富途源（港/美历史数据） | A |
| **C. 回测深化** | 多策略、参数优化、样本外验证、HK/US 数据、防过拟合 | B |
| **D. P5 可视化面板** | workbench 加 host RPC（`quant_report` shell）+ overlay Dashboard | A |
| **E. 生产化** | timer 定时调度信号、监控告警、订单/日志审计、指标持久化 | A |
| **F. P4 实盘启动** | 按 `docs/P4-live-trading.md`，用户决策小额试水 | A |

**建议顺序**：A → B → C（量化可信度）→ F（实盘）→ D/E（体验与运维）。

## 七、复审清单（reviewer 逐项）

1. `agent.cordis.yml`：两个 disabled 行的启用逻辑是否安全（安装器 sed 是否会误伤）；
2. `futu_auth.py`：OAuth 流程与 token 文件权限（600）是否合规；
3. `engine/src/index.js`：llm 调用参数（provider/model/messages 形状）是否正确；
4. `backtest.py`：成本建模、T+1/整手、未来函数是否有漏洞；
5. 风控参数默认值（单笔 1%、ATR 2×、最大持仓 5）是否合理；
6. 实盘手册的确认流是否有可绕过的路径。

## 八、交接事项

- 本机已授权富途 token（`~/.dsh/futu-token`），refresh_token 可续期；
- 本机 trading-venv 已装 akshare/playwright/yfinance；X 登录态在 `~/.dsh/x-profile`；
- 当前账户模式 = sim（安全默认）；
- 所有未验证项集中在「重启 harness 后一次性验收」。
