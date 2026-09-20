# dsh-trading-agents

DeepSeek Harness 对话模式与插件组合：把 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 多角色投研流水线装进 Harness，数据与交易能力来自[富途远程 MCP](https://github.com/FutunnOpen/futu-agent-plugin)（免 OpenD、OAuth 授权）。

**Harness 是唯一对话与指令入口。** 工作台自 WP6 起是**独立 Web**（FastAPI 单进程托管，默认 `http://127.0.0.1:8397`；同一批能力另有 `mcp__quantwb__*` MCP 工具面），也是**唯一工作台界面**（Harness 内 legacy 面板已于 WP7 退役）。工作台只用于查看研报、交易概要、量化信息预览及切换模拟盘/实盘；没有独立聊天、下单或撤单入口。

```
市场/舆情/新闻/基本面四位分析师 → 多空辩论 → 研究经理裁决 → 交易员提案 → 三方风控辩论 → 组合经理终审
        ↓ 决策写入记忆，下次分析同标的自动注入历史教训
```

装上之后你会得到：**12 角色投研**（`trading-agents`：四位分析师 → 多空辩论 → 研究经理裁决 → 交易员提案 → 三方风控辩论 → 组合经理终审，五档评级 + 参考入场价 + 止损 + 仓位建议）、**快路径量化**（`quant-trading`：信号 / 回测 / 短线量化，秒级回答"现在能不能买"）、**研究院**（`research-institute`：四子代理把资讯与基本面变成可验证的**规则提案**，过机械验证门后**必须由你在 Web 批准**才上岗）、**值班研究员（L3）**（消费研究任务队列，只产简报 / 因子巡检 / 候选提案）、**独立 Web 工作台**（研报、交易概要、持仓风险、K 线、量化预览、模拟盘/实盘切换）。

**关键边界**：纯 Harness 对话、投研与量化计算，**服务没起也能跑**；但调度链、规则批准、计划执行、Web 页面**全在服务进程内**，服务不常驻就等于没有（见 [docs/FEATURES.md](docs/FEATURES.md)「服务不是可选项」）。研究侧**永不下单、永不启用策略**——启用只能由人在独立 Web 点批准；下单/改单/撤单/切模式只经工作台受约束入口（时段闸门 → 风控 8 规则 → 业务确认）。**live 券商写协议未接入**，实盘尚未达到无人值守准入条件（见 [docs/P4-live-trading.md](docs/P4-live-trading.md)）。数据诚实：取不到数据就说取不到，标注来源与 `as_of`，不用估算值替代。功能与操作细节（WP7–WP26 实现、工作台页面、数据渠道、授权范围实测）见 [docs/FEATURES.md](docs/FEATURES.md)，架构见 [docs/architecture.md](docs/architecture.md)，运维排障见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。

## 一键安装

本仓库根目录就是一个 Harness preset（对话模式），放入 Harness 的 preset 目录即被自动发现，无需重启。

> ⚠️ **方式 A 只装对话模式（skill 基础模式），不装插件。** 工作台与量化引擎的脚本依赖统一 Python 层的**两个库**——`trading_datasource`（数据层）与 `trading_core`（量化核心），只跑 `dsh plugin add` 装不出它们，表现是一句 `ModuleNotFoundError`。装完先自检：`python "$HOME/.dsh/.agent-presets/dsh-trading-agents/scripts/install_plugins.py" check --repo "$HOME/.dsh/.agent-presets/dsh-trading-agents" --dsh-home "$HOME/.dsh"`

**方式 A · 一行命令**

```bash
# Linux / macOS
git clone https://github.com/BSTester/dsh-trading-agents "$HOME/.dsh/.agent-presets/dsh-trading-agents"
```

```powershell
# Windows (PowerShell)
git clone https://github.com/BSTester/dsh-trading-agents "$env:USERPROFILE\.dsh\.agent-presets\dsh-trading-agents"
```

**方式 B · 完整安装脚本**（preset + Python 依赖 + 富途授权向导 + 五个插件 + 平台服务依赖与前端 + 自检）

```bash
# Linux / macOS
git clone https://github.com/BSTester/dsh-trading-agents && cd dsh-trading-agents && ./install.sh
```

```powershell
# Windows (PowerShell)
git clone https://github.com/BSTester/dsh-trading-agents; cd dsh-trading-agents; .\install.ps1
```

**方式 C · 让 AI 帮你装**——把下面这段话直接发给你正在使用的 DeepSeek Harness 会话。

> 实测：按下面的步骤走完，会装出 **2 个 skill + 5 个插件 + 统一 Python 层
> （数据层 datasource + 量化核心 core）+ Python 依赖 + 平台服务依赖与前端构建**，
> 并以自检「✅ 安装完整」为准。`install.sh` 会自行把 preset 克隆到用户 preset 目录，
> 因此**不需要你手动克隆**。装完后的对话模式会话会**自动检测并拉起工作台服务**
> （preset 行 `platform-autostart`；未安装平台时仅日志提示，不影响会话）。

```text
请帮我完整安装 dsh-trading-agents。这是"对话模式 + 全部插件"的完整安装，不要只装 skill 基础模式。
按下面四步做，每一步都要看到预期结果再继续：

1) 克隆仓库并运行完整安装器：
     git clone https://github.com/BSTester/dsh-trading-agents /tmp/dsh-trading-agents
     bash /tmp/dsh-trading-agents/install.sh
   Windows 用 powershell -File /tmp/dsh-trading-agents/install.ps1
   它会：把 preset 装到 $HOME/.dsh/.agent-presets/dsh-trading-agents（Windows 为
   %USERPROFILE%\.dsh\...）、安装 workbench / fin-data / trading-engine / futu-keepalive /
   platform-autostart 五个插件、解出统一 Python 层（datasource + core 两个库）并注入交易 venv、
   创建 venv 并安装 akshare、playwright（含 Chromium 浏览器二进制）与 yfinance
   （Yahoo 财报备用源 + 港美股长历史通道；缺它不报错、只静默降级，故安装器显式声明）、
   装平台服务依赖（platform/requirements.txt：fastapi/uvicorn/mcp…/yfinance）并构建前端 dist、
   最后跑一遍安装自检。
   预期能看到「自检通过（✅ 安装完整）」与「重启 dsh web → 新建会话 → 选择「交易智囊模式」…」。
   ⚠️ 过程中会弹出富途授权页（OAuth）等你确认。此刻不想授权就让它跳过，
     之后随时可以补：python ~/.dsh/.agent-presets/dsh-trading-agents/scripts/futu_auth.py

2) 运行安装自检，必须看到「✅ 安装完整」：
     python "$HOME/.dsh/.agent-presets/dsh-trading-agents/scripts/install_plugins.py" check \
       --repo "$HOME/.dsh/.agent-presets/dsh-trading-agents" --dsh-home "$HOME/.dsh"
   自检会逐项核对 preset 各行的启用状态、五个插件、统一 Python 层
   （datasource/core 两库的标记文件）与 .pth 注入。
   若报出问题，按它给出的修复命令处理后重跑，直到通过为止。

3) 确认 preset 目录下有 agent.cordis.yml、preset.yml、skills/trading-agents/SKILL.md。

4) 把这三件事告诉我：安装结果、第 2 步的自检输出原文、以及怎么启动
   （重启 dsh web → 新建会话 → 选「交易智囊模式」）。
```

**更短的说法**（把细节交给 AI 按仓库文档执行）：

```text
请按 https://github.com/BSTester/dsh-trading-agents 的 README「方式 C」完整安装
（要装全部插件，不要只装 skill 基础模式），完成后运行安装器自检并把输出发我。
```

## 装完之后

安装脚本会把 preset、统一 Python 层、**平台服务依赖与前端 dist** 都装好（只装依赖、**不拉起常驻进程**），并在结尾跑一遍自检；装完你只需要**决定何时拉起服务**，再补一次首启数据自举。

**① 首启必做：三步自举数据**（否则平台在跑但什么都没发生——流程页会给「关注池未配置」提示）。2026-09-18 实测补正：只跑 `watchlist-init` 在**全新机器**上必然失败，因为它依赖 `universe` 表里的指数成分快照；而**交易日历**缺失时，市场链会被判定为「日历未同步 / 非交易日」而整天跳过。

```bash
# ① 指数成分快照（watchlist-init 的数据来源；缺它 → 「universe 表没有 SH.000300 的成分快照」）
~/.dsh/trading-venv/bin/python -m trading_core universe --index SH.000300
# ② 关注池（取成分股前 20 只；`--limit` 可按需调整）
~/.dsh/trading-venv/bin/python -m trading_core watchlist-init --from-index SH.000300 --limit 20
# ③ 交易日历（三市场都要，覆盖到明年年底；含法定假日与半日市）
for m in SH HK US; do
  ~/.dsh/trading-venv/bin/python -m trading_core calendar --market "$m" --start 2026-01-01 --end 2027-12-31
done
```

> ③ 之后交给平台自己维护：GLOBAL 链的 `sync_calendar`（每天 18:50，自节流——覆盖充足时零网络跳过）会自动补齐，日历快到期还会发「日历覆盖不足 / 已用尽」告警。但它最早在**当天 18:50** 才生效，所以**首启当天请手工跑一次**，否则白天的市场作业仍会被跳过。① ② 是人的选股决策，安装器不会替你决定，因此**没有**任何自动步骤会建关注池。

**② 日常运维用仓库里的一个脚本**（服务是**常驻件**不是可选件——调度链、Web 工作台、规则批准与计划执行都在它进程内；不要用前台进程方式长期跑）。浏览器打开 `http://127.0.0.1:8397` 即是工作台（`/healthz` 为存活探针）：

```bash
scripts/platform_service.sh start     # 分离进程拉起（与 preset 的 platform-autostart 同一语义：会话结束仍存活）；已在跑则不重复拉起
scripts/platform_service.sh status    # 端口/健康/PID/日志尾部/scheduler 字段
scripts/platform_service.sh stop      # 优雅 TERM → 等端口释放 → 超时 KILL
scripts/platform_service.sh restart   # stop + start（改了服务侧代码后必须做）
scripts/platform_service.sh refresh   # 把仓库 python 层重新解到 ~/.dsh/trading-python/
```

> ⚠️ **改完代码要 `refresh`，服务侧改动还要 `restart`**。原因：作业子进程（`python -m trading_core …`，即调度链的每条腿）经 venv 的 `.pth` 解析到 `~/.dsh/trading-python/` 下的**安装副本**；只改仓库文件不改副本，作业仍跑旧代码（开发机上的服务父进程走「仓库优先」，所以服务内改动重启即生效）。前端改动另需重建：`npm --prefix platform/web run build`。

> **③ 验收**：交付即可跑的端到端验收（后端 82 端点全扫 + 前端 16 路由真浏览器）——两个 harness 的参数、退出码与复验判据见 [docs/E2E-ACCEPTANCE.md](docs/E2E-ACCEPTANCE.md)。受约束的运维子命令（`oms-align` 等，只碰本地库、**绝不触达券商**）见 [docs/FEATURES.md](docs/FEATURES.md)。

## 富途授权（可选，推荐）

没有 token 也能用——行情/新闻自动降级到 web 搜索；配了 token 才有富途的 K 线、财务、研报与交易工具。一条命令弹出授权页（标准 OAuth2 Authorization Code + PKCE，默认只申请只读 scope；要开通交易功能加 `--write`）：

```bash
# Linux / macOS（Windows 用 python 运行同一脚本即可）
python "$HOME/.dsh/.agent-presets/dsh-trading-agents/scripts/futu_auth.py"
```

**有效期要点**：`expires_in` 只有 **7200 秒（2 小时）**，续期不换发新的 refresh_token；过期后富途**不返回 401，而是统一返回 `internal error`**（看起来像"富途服务挂了"）。`futu-keepalive` 行每 10 分钟检查、余量不足 30 分钟自动续期，但**只会让新会话拿到新 token**（已挂载的会话要新建会话）；脚本通道遇到该特征会自动续期并重试一次。续期失败处置（`--refresh` → 完整重新授权）、授权范围实测行为与安全边界见 [docs/RUNBOOK.md](docs/RUNBOOK.md)「场景 4：富途 token 过期」与 [docs/FEATURES.md](docs/FEATURES.md)「获取富途 token 的细节」；工具的已知限制见 [docs/TOOL-LIMITS.md](docs/TOOL-LIMITS.md)。日常建议只授权只读 scope；仅安装 skill 不具备插件级守卫。

## 更新到最新版本

平台由**五层**组成，各自的更新方式与判据都不同。**五层按顺序做完**——跳层的典型症状是「代码是新的，跑的还是旧的」（第 2 层没刷副本）或「页面白屏」（第 5 层没重建）。

| # | 层 | 是什么 | 更新方式 | 判据 |
|---|---|---|---|---|
| 1 | **preset（本仓库）** | `~/.dsh/.agent-presets/dsh-trading-agents`：对话模式组合、skills、`scripts/`、`platform/` 源码 | `git -C <preset> pull --ff-only` | `git -C <preset> log --oneline -1` 与目标版本一致 |
| 2 | **统一 Python 层** | 解到 `~/.dsh/trading-python/` 的 `datasource`／`core`／`fin-data`；venv 的 `.pth` 以它为准（`.pth` 只收 `datasource`+`core`，`fin-data` 供子进程调用） | `install_plugins.py install`（全量）或 `platform_service.sh refresh`（只刷这一层） | `install_plugins.py check` 打出版本与三层路径 |
| 3 | **Harness 插件包** | `workbench`／`fin-data`／`engine`／`futu-keepalive`／`platform-autostart` 五个 `@bstester/dsh-*` | `install_plugins.py install`（`dsh plugin add` + pnpm） | 同上自检逐行打出版本号 |
| 4 | **平台服务进程** | 常驻 FastAPI 单进程：HTTP 82 端点 + MCP 77 工具 + 静态前端 | `scripts/platform_service.sh restart` | `scripts/platform_service.sh status` 健康 + 「端点 82｜工具 77」 |
| 5 | **前端产物** | `platform/web/dist`（Vite 构建，由服务静态托管） | `npm --prefix platform/web install && npm --prefix platform/web run build` | 刷新 `http://127.0.0.1:8397` 页面正常（无白屏） |

**标准更新流程**（`PRESET` 按实际安装位置改；各步幂等，可安全重跑；只改了量化侧代码时第 2 步可换成更轻的 `platform_service.sh refresh`——只重解三份副本 + 重写 `.pth`，不跑 `dsh plugin add`、不碰 web profile、不重启服务；跑完用 [docs/E2E-ACCEPTANCE.md](docs/E2E-ACCEPTANCE.md) 的两个 harness 验收，前端要连软断言一起判闭环就加 `--fail-on-soft`）：

```bash
PRESET="$HOME/.dsh/.agent-presets/dsh-trading-agents"
git -C "$PRESET" pull --ff-only                                                                # 1) preset 取新代码（层 1）
python "$PRESET/scripts/install_plugins.py" install --repo "$PRESET" --dsh-home "$HOME/.dsh"   # 2) 统一 Python 层 + 五个插件包（层 2、3）
python "$PRESET/scripts/install_plugins.py" check   --repo "$PRESET" --dsh-home "$HOME/.dsh"   # 3) 必须看到「✅ 安装完整」
npm --prefix "$PRESET/platform/web" install && npm --prefix "$PRESET/platform/web" run build   # 4) 重建前端（层 5）
"$PRESET/scripts/platform_service.sh" restart && "$PRESET/scripts/platform_service.sh" status # 5) 重启服务（层 4）
```

### 更新时的三个坑

1. **`refresh` 是生产生效的唯一路径。** `~/.dsh/trading-python/*` 是**安装时**解出的副本，venv 的 `.pth` 指向它；作业子进程（`python -m trading_core …`，即调度链的每条腿）与手工 CLI 都走副本。而开发机上**服务进程**会「仓库优先」（`trading_datasource.repo_paths`：存在仓库则前置到 `PYTHONPATH`）。于是会出现错位：**服务里改了立刻生效，子进程里没生效**。改完代码不 `refresh`，就是「代码改了、行为没变」。
2. **preset 是一个 git 克隆，`git pull` 只能拿到 origin 上已有的提交。** 若 origin 仍指向 GitHub 而本机开发仓库领先远端（本项目的开发机当前即如此：本地领先远端上百个提交），从 GitHub 拉到的是**已发布版本**而非最新代码。此时让 preset 指向本地仓库，或先 push：`git -C "$PRESET" remote set-url origin /path/to/local/dsh-trading-agents && git -C "$PRESET" pull --ff-only`。服务脚本读哪个仓库，由解析顺序决定：`DSH_TRADING_REPO` 环境变量 > 标记文件 `~/.dsh/trading-platform-repo`（由安装器写入，内容即 `--repo`）> 脚本上一级目录。
3. **preset 的内容在会话启动时读取。** 更新完 preset，**已挂载的会话仍持旧组合**——要**新建会话**。`mcp__quantwb__*` 工具面同理：把 `quant-platform-mcp` 行的 `disabled: true` 改成 `false` 后也要新会话才出现。另外服务刚重启后，脚本类端点在冷启动的头十来秒会偏慢，harness 的暖机就是为它准备的。

## 一键启动

```bash
dsh web
```

在 Web 界面新建会话时选择 **「交易智囊模式」** preset，然后直接说：

> 分析一下 00700.HK

Harness 会用原生工具与子代理完成四位分析师报告、多空辩论与终审结论（五档评级 + 参考入场价 + 止损 + 仓位建议），缺少可靠数据时必须注明。工作台服务的拉起见上文「装完之后」②；preset 的 `platform-autostart` 行也会在会话开始时探活 `/healthz`，不通才拉起，通了什么都不做。

## 目录结构

**仓库根目录即 preset 目录**——放进 `~/.dsh/.agent-presets/` 就完成安装，`agent.cordis.yml`（组合）与 `AGENTS.md`（常驻指令）都从这里被会话加载。

```
├── agent.cordis.yml / preset.yml / AGENTS.md   # 对话模式组合 / 模式元数据 / 工程级常驻指令
├── skills/     # trading-agents、quant-trading、research-institute、futu-skills（+ 可选 last30days-bridge）
├── plugins/    # 库 datasource、core；插件 engine、fin-data、workbench、futu-keepalive、platform-autostart
├── platform/   # server/（FastAPI 单进程）+ web-pro/（V3 工作台：Ant Design Pro，产物挂根路径）
├── scripts/    # install_plugins.py（安装自检）、platform_service.sh（启停）、e2e_*（验收）、futu_auth.py
├── install/    # systemd 单元（research-duty.*）、HARNESS_SETUP.md 与 install.sh / install.ps1
├── tests/      # Python / Node 两套测试（前端测试在 platform/web/tests/）
└── docs/       # 下表各份文档
```

## 文档索引

| 文档 | 用途 |
|---|---|
| [docs/FEATURES.md](docs/FEATURES.md) | **功能说明与操作细节**：WP7–WP26 各项实现、工作台页面与指令示例、数据渠道优先级、富途 token 细节、模拟盘/实盘开关（2026-09-18 从本 README 拆出） |
| [docs/architecture.md](docs/architecture.md) | 架构与交互边界：三层模型、组件职责、HTTP 端点 / MCP 工具面契约 |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | 运维 Runbook：服务启停与配置、排障场景（token 过期、日历、情绪采集、时段闸门…）、演练 |
| [docs/HANDOVER.md](docs/HANDOVER.md) | 交接与复审：已完成总结、验证结论、未验证项、下一份计划 |
| [docs/TOOL-LIMITS.md](docs/TOOL-LIMITS.md) | 富途工具与数据源已知限制（全量实调体检结论：静默空返回、A 股权限、上下文炸弹） |
| [docs/E2E-ACCEPTANCE.md](docs/E2E-ACCEPTANCE.md) | E2E 验收工具链：两个 harness 的覆盖、用法、退出码与复验判据 |
| [docs/P4-live-trading.md](docs/P4-live-trading.md) | 实盘边界与准入清单（无人值守实盘的前置条件） |
| [docs/QUANT-WORKBENCH-PLAN.md](docs/QUANT-WORKBENCH-PLAN.md) | 专业量化工作台方案（现状评估 → 信息体系 → UI/图表 → 分阶段实施） |
| [docs/OPENAPI-FEASIBILITY.md](docs/OPENAPI-FEASIBILITY.md) | 富途 OpenAPI 接入可行性备忘 |
| [docs/REVIEW-AND-TEST-PLAN.md](docs/REVIEW-AND-TEST-PLAN.md) | 复盘 · 工作流 · 重启后全面测试清单（历史快照） |
| [docs/superpowers/](docs/superpowers/) | 历史计划与规格存档（`plans/` + `specs/`） |
| [install/HARNESS_SETUP.md](install/HARNESS_SETUP.md) | 一键安装提示词（Harness 安装协调员手册：依赖 → 构建 → 启服务 → 启用工具面 → 验证） |

## 免责声明

本项目输出均为 AI 研究性质内容，不构成任何投资建议。市场有风险，决策需独立。
