# dsh-trading-agents

> 📐 [专业量化工作台方案](docs/QUANT-WORKBENCH-PLAN.md) · 📋 [交接与复审文档](docs/HANDOVER.md)：已完成总结、验证结论、未验证项、下一份计划、复审清单。
> ✅ [E2E 验收工具链](docs/E2E-ACCEPTANCE.md)：后端/前端两个 harness 的用法、退出码、缺陷清单与复验判据；服务启停见 [RUNBOOK](docs/RUNBOOK.md)。
> 🔄 [更新到最新版本](#更新到最新版本)：五层更新顺序与各层判据、`refresh` 为什么必须在、三个常见的坑。

DeepSeek Harness 对话模式与插件组合：把 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 多角色投研流水线装进 Harness，数据与交易能力来自[富途远程 MCP](https://github.com/FutunnOpen/futu-agent-plugin)（免 OpenD、OAuth 授权）。

**Harness 是唯一对话与指令入口。** 工作台自 WP6 起是**独立 Web**（FastAPI 单进程托管，
默认 `http://127.0.0.1:8397`；同一批能力另有 `mcp__quantwb__*` MCP 工具面），也是**唯一
工作台界面**（Harness 内 legacy 面板已于 WP7 退役）。工作台只用于查看研报、交易概要、
量化信息预览及切换模拟盘/实盘；没有独立聊天、下单或撤单入口。

```
市场分析师 ┐
舆情分析师 ├→ Bull/Bear 辩论 → 研究经理裁决 → 交易员提案 → 三方风控辩论 → 组合经理终审
新闻分析师 │
基本面分析 ┘
        ↓ 决策写入记忆，下次分析同标的自动注入历史教训
```


> **首启必做（否则平台在跑但什么都没发生）**：配置关注池——`~/.dsh/trading-venv/bin/python -m trading_core watchlist-init --from-index SH.000300`（需 universe 表已有该指数成分快照；缺它是全部数据作业静默跳过的原因，流程页会显示「关注池未配置」提示）。

## 一键安装

本仓库根目录就是一个 Harness preset（对话模式），放入 Harness 的 preset 目录即被自动发现，无需重启。

> 说明：`dsh plugin add` 命令用于安装 profile 级插件包，对话模式（preset）的官方
> 安装方式就是放入 `~/.dsh/.agent-presets/` 目录。仅克隆得到 **skill 基础模式**；
> 要使用工作台、研报发布、量化工具与账户守卫，请运行方式 B 的完整安装器。

> ⚠️ **方式 A 只装对话模式（skill 基础模式），不装插件。**
> 工作台与量化引擎的脚本依赖统一 Python 层的**两个库**——`trading_datasource`（统一数据层）
> 与 `trading_core`（量化核心：PIT 存储/研究/执行/调度），安装器以
> `LIBRARIES=("datasource", "core")` 解出这两个库——只跑 `dsh plugin add` 装不出它们，
> 表现是一句 `ModuleNotFoundError`。装完可用自检验证：

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
   创建 venv 并安装 akshare 与 playwright（含 Chromium 浏览器二进制）、装平台服务依赖
   （platform/requirements.txt：fastapi/uvicorn/mcp…）并构建前端 dist、最后跑一遍安装自检。
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

## 装完之后：服务启停与验收（WP9–WP17）

安装脚本会把 preset、统一 Python 层、**平台服务依赖与前端 dist** 都装好（方式 B 的第 3.6 步；
它用 `install_platform.py --skip-service`，只装依赖不拉起常驻进程），并在结尾跑一遍自检。
因此装完你只需要**决定何时拉起服务**——**日常运维用仓库里的一个脚本**，不要用前台
进程方式长期跑服务（服务是**常驻件**不是可选件——调度链、Web 工作台、规则批准与计划
执行都在它进程内；定位见下文「服务不是可选项」）：

```bash
scripts/platform_service.sh start     # 分离进程拉起（与 preset 的 platform-autostart 同一语义：
                                      # 会话结束仍存活）；已在跑则不重复拉起
scripts/platform_service.sh status    # 端口/健康/PID/日志尾部/scheduler 字段
scripts/platform_service.sh stop      # 优雅 TERM → 等端口释放 → 超时 KILL
scripts/platform_service.sh restart   # stop + start（改了服务侧代码后必须做）
scripts/platform_service.sh refresh   # 把仓库 python 层重新解到 ~/.dsh/trading-python/
```

> ⚠️ **改完代码要 `refresh`，服务侧改动还要 `restart`**。原因：作业子进程
> （`python -m trading_core …`，即调度链的每条腿）经 venv 的 `.pth` 解析到
> `~/.dsh/trading-python/` 下的**安装副本**；只改仓库文件不改副本，作业仍跑旧代码。
> 开发机上父进程（服务）走「仓库优先」（`trading_datasource.repo_paths`：存在仓库则前置到
> `PYTHONPATH`，否则回落副本），所以**服务内**改动重启即生效，而**手工/调度子进程**依赖副本。
> 前端改动另需重建：`npm --prefix platform/web run build`。
> 五层的完整更新顺序与各层判据见下文「[更新到最新版本](#更新到最新版本)」。

**首启必做：三步自举数据**（否则平台在跑但什么都没发生——流程页会给「关注池未配置」提示）。
2026-09-18 实测补正：此前这里只写了 `watchlist-init` 一条，但在**全新机器**上它必然失败，
因为它依赖 `universe` 表里的指数成分快照；而**交易日历**缺失时市场链会被判定为
「日历未同步 / 非交易日」而整天跳过：

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

> ③ 之后可以交给平台自己维护：服务内 GLOBAL 链的 `sync_calendar`（每天 18:50，自节流——
> 覆盖充足时零网络跳过）会自动补齐，日历快到期还会发「日历覆盖不足 / 已用尽」告警。
> 但它最早在**当天 18:50** 才生效，所以**首启当天请手工跑一次**，否则白天的市场作业仍会被跳过。
> ① ② 是人的选股决策，安装器不会替你决定，因此**没有**任何自动步骤会建关注池。

**验收工具**（交付即可跑，详见 [docs/E2E-ACCEPTANCE.md](docs/E2E-ACCEPTANCE.md)）：

```bash
cd <repo>

# 后端：82 个 HTTP 端点全扫 + 可逆写探针 + 一致性核对（只报告，发现缺陷即非零退出）
~/.dsh/trading-venv/bin/python scripts/e2e_workbench.py
#   刚重启过服务不用加参数：脚本自带冷启动暖机（跳过它就加 --no-warmup，
#   但脚本类端点会因此误报）；与别的任务共用同一服务、或只想只读巡检：加 --read-only

# 前端：16 个路由真浏览器（系统 Chromium + CDP，零新依赖）
node scripts/e2e_web.mjs
#   单页复验：--routes overview,plan,pipeline（未知键会报错并列出可用键）
#   默认把「已知在修」的软断言只记不判；修复落地后加 --fail-on-soft 判闭环
```

> 两个 harness 都**只报告、不修复**：退出码 `0` = 未发现缺陷（后端同时要求无真泄漏）。
> 冷启动暖机约 10 秒属正常；等待预算可用 `--route-budget`／`--health-timeout` 调。

**受约束的运维子命令**（都只碰本地库、**绝不触达券商**，成功与否都留审计痕；口径与排障见
[docs/RUNBOOK.md](docs/RUNBOOK.md)）：

| 命令 | 何时用 |
|---|---|
| `~/.dsh/trading-venv/bin/python -m trading_core oms-align --broker-order-id <单号> --status cancelled\|rejected --reason "<原因>" [--operator <标识>]` | **最后手段**：券商已撤/已拒，本地订单却仍停在「在途」。**先跑 `reconcile-daily`**（对账按官方状态码自动收敛，覆盖大多数）；只有对账覆盖不到的历史行才走这个人工入口。只允许 `cancelled`/`rejected`，必须命中本地订单行（不伪造），提交走合法状态机路径（**已成交的单不可能被降级**），`--reason` 必填，成功写一条 `warn` 告警留痕。用它而不是手改 SQLite——后者不可审计 |
| `~/.dsh/trading-venv/bin/python -m trading_core watchlist-init --from-index SH.000300 [--limit N] [--force]` | 首启配置关注池（见上）；已有非空关注池时默认拒绝覆盖，`--force` 才覆盖 |

**`AGENTS.md`（仓库根）是什么**：Harness 的**工程级常驻指令文件**——会话在工程根打开时
（以 `.git` 标记）由 `@deepseek-ai/dsh-agent-instructions` **自动加载**，因此它写的纪律对
每个新会话都生效，不依赖你每次提醒。本仓库的 `AGENTS.md` 只有两节：

- **一、值班研究员队列的兜底纪律（L3）**：会话首次交互先 `research_tasks_claim` 探测队列，
  有积压先按 `research-institute` 技能的**值班模式**消费，产出只进研究页与规则候选池
  （**不碰交易、不启用策略**）。主消费路径是 `install/research-duty.timer`
  （外部定时器 → `scripts/research_duty.sh` → `dsh --profile headless`），这条纪律是
  定时器没装、机器关机、headless 被杀时的兜底。**领取后必须回报**（做不了也要
  `ok=false` + 原因，不静默离开队列）；重复回报安全（终态任务原样返回、不改状态）。
- **二、常驻纪律**：直接提交 `main`（本仓库惯例）；每次提交保持三套测试全绿（命令见上）；
  下单/改单/撤单/切模式/执行计划只经工作台受约束入口，研究侧**永不下单、永不启用策略**
  （启用只能由人在 Web 点批准）；取不到数据就说取不到，不用估算值替代。

> ⚠️ 它的效力只覆盖**有 turn 的会话**：没有任何交互的会话不会消费队列——任务不丢，只延迟。
> 这不是后台自动动作，不要对外宣称「已自动处理」。

## 更新到最新版本

平台由**五层**组成，各自的更新方式与判据都不同。**五层按顺序做完**——跳层的典型症状是
「代码是新的，跑的还是旧的」（第 2 层没刷副本）或「页面白屏」（第 5 层没重建）。

| # | 层 | 是什么 | 更新方式 | 判据 |
|---|---|---|---|---|
| 1 | **preset（本仓库）** | `~/.dsh/.agent-presets/dsh-trading-agents`：对话模式组合、skills、`scripts/`、`platform/` 源码 | `git -C <preset> pull --ff-only` | `git -C <preset> log --oneline -1` 与目标版本一致 |
| 2 | **统一 Python 层** | 解到 `~/.dsh/trading-python/` 的 `datasource`／`core`／`fin-data`；venv 的 `.pth` 以它为准（`.pth` 只收 `datasource`+`core`，`fin-data` 供子进程调用） | `install_plugins.py install`（全量）或 `platform_service.sh refresh`（只刷这一层） | `install_plugins.py check` 打出版本与三层路径 |
| 3 | **Harness 插件包** | `workbench`／`fin-data`／`engine`／`futu-keepalive`／`platform-autostart` 五个 `@bstester/dsh-*` | `install_plugins.py install`（`dsh plugin add` + pnpm） | 同上自检逐行打出版本号 |
| 4 | **平台服务进程** | 常驻 FastAPI 单进程：HTTP 82 端点 + MCP 77 工具 + 静态前端 | `scripts/platform_service.sh restart` | `scripts/platform_service.sh status` 健康 + 「端点 82｜工具 77」 |
| 5 | **前端产物** | `platform/web/dist`（Vite 构建，由服务静态托管） | `npm --prefix platform/web install && npm --prefix platform/web run build` | 刷新 `http://127.0.0.1:8397` 页面正常（无白屏） |

**标准更新流程**（`PRESET` 按实际安装位置改；整段可复制粘贴，各步幂等，可安全重跑）：

```bash
PRESET="$HOME/.dsh/.agent-presets/dsh-trading-agents"

# 1) preset 取新代码（层 1）
git -C "$PRESET" pull --ff-only

# 2) 统一 Python 层 + 五个插件包（层 2、3；install 会一并刷新副本）
python "$PRESET/scripts/install_plugins.py" install --repo "$PRESET" --dsh-home "$HOME/.dsh"

# 3) 自检：必须看到「✅ 安装完整」+ 五个插件版本号 + 三层路径
python "$PRESET/scripts/install_plugins.py" check --repo "$PRESET" --dsh-home "$HOME/.dsh"

# 4) 重建前端（层 5）
npm --prefix "$PRESET/platform/web" install
npm --prefix "$PRESET/platform/web" run build

# 5) 重启服务（层 4），再看一眼健康与计数
"$PRESET/scripts/platform_service.sh" restart
"$PRESET/scripts/platform_service.sh" status

# 6) 验收：两个 harness 都不应报出缺陷（后端退出码非 0 即未通过；
#    前端默认只记软断言，要连软断言一起判闭环就加 --fail-on-soft）
~/.dsh/trading-venv/bin/python "$PRESET/scripts/e2e_workbench.py"
node "$PRESET/scripts/e2e_web.mjs"
```

**只改了量化侧代码**（`plugins/core`、`plugins/datasource`）时，第 2 步可以换成更轻的入口：

```bash
"$PRESET/scripts/platform_service.sh" refresh   # 只重解三份副本 + 重写 .pth
                                                # 不跑 dsh plugin add、不碰 web profile、不重启服务
```

### 更新时的三个坑

1. **`refresh` 是生产生效的唯一路径。** `~/.dsh/trading-python/*` 是**安装时**解出的副本，
   venv 的 `.pth` 指向它；作业子进程（`python -m trading_core …`，即调度链的每条腿）与手工
   CLI 都走副本。而开发机上**服务进程**会「仓库优先」（`trading_datasource.repo_paths`：
   存在仓库则前置到 `PYTHONPATH`）。于是会出现错位：**服务里改了立刻生效，子进程里没生效**。
   改完代码不 `refresh`，就是「代码改了、行为没变」。
2. **preset 是一个 git 克隆，`git pull` 只能拿到 origin 上已有的提交。** 若 origin 仍指向
   GitHub 而本机开发仓库领先远端（本项目的开发机当前即如此：本地领先远端上百个提交），
   从 GitHub 拉到的是**已发布版本**而非最新代码。此时让 preset 指向本地仓库，或先 push：
   ```bash
   git -C "$PRESET" remote set-url origin /path/to/local/dsh-trading-agents
   git -C "$PRESET" pull --ff-only
   ```
   服务脚本读哪个仓库，由解析顺序决定：`DSH_TRADING_REPO` 环境变量 > 标记文件
   `~/.dsh/trading-platform-repo`（由安装器写入，内容即 `--repo`）> 脚本上一级目录。
3. **preset 的内容在会话启动时读取。** 更新完 preset，**已挂载的会话仍持旧组合**——要
   **新建会话**。`mcp__quantwb__*` 工具面同理：把 `quant-platform-mcp` 行的 `disabled: true`
   改成 `false` 后也要新会话才出现。另外服务刚重启后，脚本类端点在冷启动的头十来秒会偏慢，
   harness 的暖机就是为它准备的。

## 维护：清掉"进行中"的研究记录

被中断的会话会留下停在 `running` 的研究 run，工作台「研究」页会一直显示"进行中"。

**首选方式：直接在对话里说**（工作台是只读展示，指令入口只有对话）：

> 看一下有哪些研报还卡在"进行中"，把那条不要的取消掉。

对应工具 `research_cancel`：

| 调用 | 作用 |
|---|---|
| `research_cancel(action="list")` | 列出进行中的记录 |
| `research_cancel(action="cancel", run_id=…)` | 取消指定一条（改状态、**保留记录**） |
| `research_cancel(action="cancel_stale", older_than_minutes=120)` | 批量取消超时的 |

**命令行方式**（AI 不在场时用）：

```bash
ADMIN="$HOME/.dsh/.agent-presets/dsh-trading-agents/scripts/workbench_admin.mjs"
node "$ADMIN" runs                                    # 列出全部 run 及其状态与年龄
node "$ADMIN" cancel-run <run_id>                     # 取消指定 run（保留记录，标记 cancelled）
node "$ADMIN" cancel-stale [--hours 2]                # 取消所有超时仍 running 的
node "$ADMIN" prune-runs [--hours 2]                  # 删除孤儿 run（有研报的保留）
```

- **取消**（`cancelled`）：用户主动决定不做，**保留记录**供追溯，并写一条 `research_cancelled` 活动日志；
- **自动判定**（`abandoned`）：超过 **2 小时**仍 `running` 的，工作台按派生状态显示为已中断
  （不改写磁盘数据）。阈值较宽是因为真实投研 run 可能跑较久；
- **删除**（`prune-runs`）：只删无研报的孤儿，**有研报的一律保留**。

## 一键启动

```bash
dsh web
```

在 Web 界面新建会话时选择 **「交易智囊模式」** preset，然后直接说：

> 分析一下 00700.HK

Harness 会使用原生工具与子代理完成四位分析师报告、多空辩论和终审结论
（Buy/Overweight/Hold/Underweight/Sell 五档评级 + 参考入场价 + 止损 + 仓位建议）。
缺少可靠数据时必须注明，不能编造价格或承诺完成时间。

## 服务不是可选项：独立工作台服务（WP6，WP7 起常驻）

**先说结论**：纯 Harness 对话、投研与量化计算，服务没起也能跑（`agent.cordis.yml` 的
`quant-platform-mcp` 行默认 `disabled: true`，服务未起时安静降级）。但**下面这些能力全都在
服务进程内**，服务不常驻就等于没有：

| 能力 | 为什么依赖服务进程 |
|---|---|
| 服务内调度器（WP7） | 按交易日历跑作业链（sync→质量→信号→计划、对账→TCA→摘要，收盘链尾追加因子快照），启动即补跑当日到期作业；**没有别的进程在跑它** |
| 自动流水线（WP9） | 计划生成与次日 `auto_execute` 都由调度器驱动——模拟盘「全自动」就是这个开关 |
| Web 工作台 | 唯一工作台界面（HTTP API + MCP + 静态前端同一个进程）；Harness 内 legacy 面板已于 WP7 退役 |
| **规则批准**（WP14） | `rules-decide` 是**服务进程内动作端点**：没有 CLI 子命令、不在 MCP 工具面——服务没起就无从批准，模型也永远不能自批 |
| 计划执行 / 实盘确认 | `plan_execute` 与业务确认卡片（TTL 120s）由服务托管 |
| 研报/因子/流程/调度/审计各页 | 发布本身写本地存储，但**要看到**得有服务在托管页面 |

所以：**把它当常驻件**。preset 的 `platform-autostart` 行会在**会话开始时**探活 `/healthz`，
不通才拉起（通了什么都不做）；显式启停用 `scripts/platform_service.sh`：

```bash
# 一次性依赖：Python 侧复用交易 venv；前端构建需要 Node（只需联网一次）
~/.dsh/trading-venv/bin/pip install -r platform/requirements.txt
npm --prefix platform/web install && npm --prefix platform/web run build

# 耐用拉起（分离进程，随会话结束仍存活）+ 看状态
scripts/platform_service.sh start
scripts/platform_service.sh status     # 仓库/解释器/健康/端点与工具计数/日志尾部
```

> `start|stop|status|restart|refresh` 的完整口径、退出码与排障见
> [docs/RUNBOOK.md](docs/RUNBOOK.md)「平台服务」一节。**前台跑法
> `cd platform && ~/.dsh/trading-venv/bin/python -m server.run` 只适合临时调试**——它随
> 当前 shell 结束而死（实测：会话结束时进程消失、端口释放）。

浏览器打开 `http://127.0.0.1:8397` 即是工作台（`/healthz` 为存活探针）。
**不能在仓库根用 `python -m platform.server.run`**——标准库 `platform` 遮蔽同名包。

要让 Harness 会话直接调用工作台能力，把 `agent.cordis.yml` 里 `quant-platform-mcp` 行的
`disabled: true` 改成 `false`，然后**新建会话**（已挂载的会话不会重新读取组合）：会话内
出现 `mcp__quantwb__*` 共 **77** 个工具（WP17 起；服务同时声明 **82** 个 HTTP 端点），
与 Web 同源（HTTP 与 MCP 调用同一批处理函数；`confirm-decide`、`rules-decide`、
`auto_pipeline`、`modify_user_security`、`research-tasks-list` 等**人类专属端点有意不进工具面**——
模型不能自批实盘单、不能自己批准规则、不能自拨自动执行开关）。
实盘切换只能在独立 Web 的模式切换入口（页头 SIM/LIVE 徽章 →「账户模式」对话框）
输入口令「确认实盘」完成，成功后提示带 `order_authorized: false`；`switch_mode` 工具
只接受切到 sim（live→sim 回模拟盘），sim→live 一律拒绝。服务配置项与 systemd 单元样例见
同一节。

实盘**写操作**的确认由**工作台业务确认**承载（不再走 Harness 原生审批）：WP7 起 Harness
会话内的富途写工具（`mcp__futu__sim_trade_*`/`trading_*` 的下单/改单/撤单动词）被工具
策略**一律拒绝并指引工作台**，不会产生待确认项；真实下单走 `mcp__quantwb__trade_*` 工具，
待确认项出现在**独立 Web 的确认卡片**（中文订单摘要，批准/拒绝各一次；TTL 120s 超时按
拒绝收尾）。历史上 Harness 会话侧发起、legacy 面板作答的确认路径已随收窄不可达，
该路径连同面板确认 UI 已于 WP7 退役删除——服务侧 Web 确认卡片是唯一确认通道。

## WP7：独立量化平台（服务内调度 / 因子收集 / 交易闸门）

WP6 把工作台装进了独立服务进程；WP7 让这个进程成为**独立量化平台**，Harness 只做大脑：

- **服务内调度器**：吸收 daemon 常驻循环——按交易日历自动跑作业链
  （sync→质量→信号→计划、对账→TCA→摘要，各市场收盘链末尾追加因子快照），
  心跳/告警协议不变；启动即补跑当日到期作业（与手动 daemon 共享去重标记，不重复执行）；
  daemon CLI 保留为手动入口。
- **因子快照定时收集**：每个交易日收盘后自动跑因子/信号落 `factors_history`，
  按日期回看走 `factors-history` 端点 / `mcp__quantwb__factors_history` 工具 / CLI。
- **交易闸门 + 受约束交易工具**：`trade_place/trade_modify/trade_cancel` 与
  `account_positions/account_orders/account_funds`（工具面 27→33）。写操作前置链 =
  账户模式 → **时段闸门（WP19：不在可委托时段即拒，撤单放行）** → 风控 8 规则
  （kill 文件=规则 1）→ **业务确认（Web 确认卡片作答，进程内）** → broker 适配；
  live 券商写协议未接入，live 下 `trade_*` 提交即拒（当前设计内行为）。
- **Harness 富途写通道收窄**：`mcp__futu__sim_trade_*`/`trading_*` 写类被工具策略
  一律拒绝并指引工作台（未知动词 fail-closed）；只读研究不受影响。
- **一键安装**：把 `install/HARNESS_SETUP.md` 的提示词整段发给一个 Harness 会话，
  即可完成「依赖 → Web 构建 → 服务启动 → preset 行启用 → 工具面验证」全流程
  （幂等，可分层跳过）。

## WP8：富途 OpenAPI 统一 + 交易能力完整暴露

- **行情/交易统一到富途 OpenAPI**：`trading-platform.json` 的 `futu_channel=openapi`
  且已配置凭据（`scripts/futu_auth.py --openapi`）时，行情与交易都走 REST；
  通道未配置时行为与 WP7 完全一致（默认零变化）。
- **交易字段完整暴露（任务 6）**：`trade_place` 支持官方 8 种 `order_type`、`GTC`、
  美股 `session`（市价单仅 RTH）、`aux_price` 触发价（证券 3 位小数）、港股 `lot_type`、
  `remark`（≤64 字节）、`order_class=MLEG` + `multi_leg_info`；`trade_modify` 补 `aux_price`。
  字段校验仍在闸门字段校验层（风控/确认之前），坏参数零券商往返、零确认消耗。
  **sim 通道只支持限价当日单**，扩展字段如实拒绝（消息说明），不静默丢弃。
- **推送订阅管理面（任务 6）**：`push_status`（与 `/healthz` 的 push 同形，前端页头 10 秒
  轮询显示「实时推送已连接 / 推送连接中 / 推送未启用」）、`push_subscribe`、
  `push_unsubscribe`（只改本地连接订阅意图，非交易；未启用如实返回
  `trading/push-unavailable`）。工具面 56 → **59**，端点 52 → **55**，锁定表同步。
  受限字段与通道边界见 [docs/TOOL-LIMITS.md](docs/TOOL-LIMITS.md) 第八节。
- **OpenAPI 统一接入全链路（WP8 收尾）**：8 种订单类型/交易时段/GTC/多腿/二次确认合一、
  WS 推送（行情+交易事件→OMS，对账兜底保留）、深度数据/资金流/衍生品/筛选/IPO/自选股/F10
  全部走同一 OpenAPI 客户端；一键安装（`install/HARNESS_SETUP.md`）后 quantwb 工具面
  33 → **59**（54 端点工具 + 5 维护）。
- **富途通道决策**：工作台 OpenAPI 是唯一权威通道（REST+WS）；Harness 直连富途只读研究；
  托管 MCP 降级为可选只读研究通道（preset 默认 disabled），写通道唯一在工作台。


## WP9：自动流水线（模拟盘全自动）

**默认关闭**：不配置 `auto_pipeline` 时，调度行为与 WP8 完全一致（零变化）。

打开方式（三步，全部在 `trading-platform.json` 里）：

```json
{
  "watchlist": ["SH.600519", "SH.000001"],
  "auto_pipeline": {
    "enabled": true,
    "strategies": [{"market": "SH", "strategy": "watchlist_rsi", "watchlist": "watchlist"}],
    "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
    "reconcile_at": "19:00",
    "exec_window_minutes": 30
  },
  "sentiment_budget_seconds": 600,
  "sentiment_symbol_timeout_seconds": 90
}
```

1. 写入上表配置（`enabled=false`/缺省即关闭）；
2. 关注池填要跑的市场标的（`watchlist_rsi` 扫关注池，BUY 等权、其余现金）；
3. 重启或等待下一轮调度 tick（约 60 秒）——配置每轮现读，改完即生效。

策略项两个键的分工：**`market` 决定市场范围**（`SH` 链含 SZ/BJ；三市场可各配一条，
各自独立计算权重与 `max_positions` 截断）；**`watchlist` 只选池子**（池键名，缺省
`watchlist` = 配置顶层列表，可省略；指定的池键不存在则当日跳过该策略并告警）。

打开后每个交易日自动完成的链路：

| 时机 | 自动动作 |
|---|---|
| 收盘后（各市场 `factors_snapshot` + 5 分钟） | `build_plan`：数据就绪门 → 策略算权重 → 冻结 auto 计划 |
| 晚间 `reconcile_at` | `reconcile` → TCA → digest（对账差异 → critical 告警 + 自动暂停执行） |
| 次一交易日 `exec_at`（窗口 `exec_window_minutes` 内） | `auto_execute`：九守卫 → 写 `execute_plan` 指令 → 同轮轮询消费 → 走风控 8 规则执行 |

**实盘永远等人工**：live 模式也会自动生成计划，但 `auto_execute` 只对 sim 生效——
实盘仍是在工作台点「执行」+ 口令「确认执行」。**关掉开关即回到全人工**。

**执行链的下单账户（WP16 实机修复，2026-09-17）**：执行侧（`execute.run`）过去用占位符
`acc_id="SIM"` 下单——MCP 通道下上游忽略它，切到 `futu_channel=openapi` 后 REST 把它拼进
URL（`/sim-trade/SIM/orders`），**每一张自动执行单都被券商 `-3 invalid parameter` 拒绝**。
现按市场从 `sim_trade_account_list` 解析真实模拟账户（与持仓查询同一份账户事实）。该修复
在 `trading_core`，生效需 `scripts/platform_service.sh refresh && restart`。实机证据与
未决项见 `docs/TOOL-LIMITS.md`「模拟盘读能力与自动执行下单账户」。

**熔断（halt）不会自动恢复**：对账差异或日内熔断触发后，自动执行以 warn 级跳过并在
`daily:digest` 记录原因；恢复必须人工查明原因后清 halt（工作台调度页/`clear_halt`）。
零差异的一次对账**不会**清除已有 halt。

**手工与自动共用同一风控自洽定量口径**：`plan-build`（手工内联权重）与 `build_plan`
（自动策略权重）走同一个 `planner.build_and_freeze`——数量取 `min(权重定量, 风险预算
定量)`（风险预算 = 权益 × `risk_per_trade` ÷ 止损距离），加仓受单笔风险约束、减仓/清仓
不受限；算不出 ATR 的标的进结果里的 `skipped`（不生成「无止损全额定量」这种必被规则 4
拦下的徒劳计划）。手工路径**没有旁路**，这是刻意的统一。

#### 交易日历自动维护（`calendar-sync`，基础链，与交易开关无关）

`calendar` 表是**交易日白名单**，`is_trading_day` 在日期**超出 `max(day)`** 时返回 False
而**不报错**——日历用尽会让市场链被静默跳过，流程页只显示「市场天天休市」。所以 GLOBAL
链（不查市场日历，假日/周末/日历用尽都照常跑）链首新增作业：

| 时机 | 作业 | 说明 |
|---|---|---|
| 每日 18:50（早于对账 19:00 与入队 19:05） | `calendar-sync --market SH,HK,US` | 逐市场自节流：`max(day) ≥ today+180 天` 就**跳过、零网络调用**；否则同步 `[today−30, today+400]` 天并幂等 upsert。一个市场通道失败只记 `failed`，不影响其余市场 |

18:50 的意义：日历必须在**当日**对账/计划之前就位——今日刚用尽也能在**今晚同一轮**补齐，
次日市场链照常跑（同一轮 tick 内 GLOBAL 链先于市场链处理）。

手工等价命令（运维/补跑；退出码 `0`=全成功或部分成功、`1`=零成功，与 `sync-bars` 同口径）：

```bash
~/.dsh/trading-venv/bin/python -m trading_core calendar-sync --market SH,HK,US \
  [--horizon-days 180] [--db "$DB"]
```

两条可见性告警（都是 **warn**，**每市场每日至多一条**，不随每轮 tick 刷屏）：

| 告警标题 | 触发 | 含义与处置 |
|---|---|---|
| `日历覆盖不足` | `max(day) < today + 60 天` | **链照常跑**（只是快到期）。查 `sync_calendar` 作业是否连续失败；可手工跑上面的命令 |
| `日历已用尽` | `today > max(day)` | 该市场链今日被**跳过**（此前完全静默）。同上处置——补齐后当晚即可恢复 |

`auto_execute` 另补了一道**纵深防御**守卫（守卫 4b）：作业体内也判交易日，非交易日
**info** 跳过并零指令（真实休市不是故障，与 `plan_auto` 同语义）。线上调度器本就挡住，
这道闸是给 CLI / MCP / E2E 直调用的。

排障与演练（假时钟、窗口超时、熔断拦截）见 [docs/RUNBOOK.md](docs/RUNBOOK.md)「自动流水线（WP9）」。

### 目标外持仓自动清出（`exit_outside_target`，默认关闭）

**问题**：受管集合（关注池 ∩ 策略 universe）之外的持仓刻意不进 diff（不清理用户手工持仓），
而 `max_positions`（默认 5）又拦住新建仓。实机 A 股模拟账户持 8 只、多在关注池之外时，
策略**既不买也不卖**，账户与策略组合永远不收敛。

**开关**：`trading-platform.json` **顶层**键 `exit_outside_target`（与 `watchlist` 同级），
**默认 `false`（键缺失即关闭，不改变任何既有行为）**；只接受真布尔，写 `"true"`/`1` 会
fail-closed：当日计划软跳过并发 warn（配置写错不静默降级）。

```json
{
  "watchlist": ["SH.600519", "..."],
  "auto_pipeline": {"enabled": true, "strategies": ["..."]},
  "exit_outside_target": true
}
```

打开后**只在自动计划路径**（`plan_auto` / `build_plan` 作业）生效，手工 `plan-build` 不受影响：

- **收敛集合** = 券商持仓 − 当日策略 `target` 的键（含 `managed` 之外的存量持仓）→ 目标
  权重按 0 处理、全额卖出；
- **硬守卫**：策略当日 `target` 为空（选不出标的）时**一律不清出**并发 warn
  「计划预警：目标为空未清出」——「选不出标的」不等于「清空全部持仓」；
- **价格**：本地最近收盘优先；本地无 K 线（实机那 8 只确实 0 行）时回退**券商持仓标记价**
  （sim `cur_price` / live `nominal_price`），来源在 `converge.prices` 与告警里标注
  `close`/`broker_mark`（**绝不把券商标记价说成本地收盘价**）；两处都拿不到 → 进
  `skipped`（`SYM(无价,无法清出)`）且**不生成订单**（不猜价）；
- **可卖数量**：券商给了可用数量（sim `qty_avbl` / live `can_sell_qty`）时卖
  `min(qty, available)`；`available=0`（T+N 当日买入未解禁）→ `skipped`
  （`SYM(T+N不可卖)`），**不生成注定被券商拒的单**；
- 每次清出都会发一条 warn「计划预警：目标外持仓清出」，detail 写明清出只数、价格来源
  各几只、哪些没清成（页面/摘要里同样可见）。

**两步生效（预期行为，不要当故障排查）**：执行侧 `ctx` 用的是**静态持仓快照**（不逐单
重查），所以**同一份计划里「卖出 8 只 + 买入新股」时买入仍会被风控规则 6 拒**。当日计划
先把账户清到策略组合，**下个交易日**快照回落（持仓数 ≤ `max_positions`）后，新计划才能
建仓。绝不为绕过规则 6 去伪造持仓数。

排障口径见 [docs/RUNBOOK.md](docs/RUNBOOK.md)「目标外持仓清出没生效 / 清了但买不进」。

### 情绪采集的两个预算键（最慢的基础链作业）

`sentiment_snapshot` 是**全仓库最慢的作业**（实测 20 标的 × 三源 >7 分钟：每源都要起
浏览器/网络重试）。两个顶层键控制它，缺省值即下面这两个，**通常不用改**：

| 键 | 默认 | 作用 |
|---|---|---|
| `sentiment_budget_seconds` | 600 | 一轮采集的**总预算，硬上界**：每次调用前按剩余预算裁剪单次超时，剩余 ≤ 0 即不再发起任何新调用（未开始的标的记 `skipped_symbols`）。**退出 0** + warn 告警「情绪快照预算耗尽」——「今天没采完」而不是「失败」 |
| `sentiment_symbol_timeout_seconds` | 90 | **单个标的**的超时。超时只判该标的失败并继续下一个（不会一个卡住拖死整轮） |

整轮耗时因此是**硬上界**：≤ 预算 + 已启动调用的允许时长（该时长本身也被剩余预算
裁剪，故实际 ≤ 预算 + 1s）。配置期另有一条**更保守的冗余校验**：`预算 + 3 × 单标的超时
< 900s`（作业上限）——它守的是裁剪逻辑退化时的兜底（默认 600 + 270 = 870 ✓），超了
fail-closed 报错并给出调整方向（刻意：预算被静默忽略就等于没修）。采集是**攒历史**，允许采不完，
但不允许因为超时被判失败而一行数据都留不下。分批人工跑与排障见
[docs/RUNBOOK.md](docs/RUNBOOK.md)「情绪采集」。


## WP19：交易时段事实源（半日市）与人工下单时段闸门

**唯一事实源 `trading_core/sessions.py`**（市场本地时区用 `zoneinfo`，不手算 DST）：

| 市场 | 全天秒数 | 全天收盘（本地） | 开盘（本地） | 可委托窗口（本地） |
|---|---|---|---|---|
| SH / SZ / BJ | 14400（4h） | 15:00 | 09:30 | 09:15–15:00（含开盘集合竞价与**午间报单**） |
| HK | 19800（5.5h） | 16:00 | 09:30 | 09:00–16:10（含开市前竞价与收市竞价） |
| US | 23400（6.5h） | 16:00 | 09:30 | 常规（`RTH`/缺省）09:30–16:00；请求扩展时段 04:00–20:00 |

**半日市（提前收盘）的算法**：`trade_second` 小于该市场全天秒数时，收盘 = **开盘 +
`trade_second`**，按**单段（不含午休）**算——港股半日只有上午（09:30–12:00 = 9000s），
美股半日 09:30–13:00（12600s）；用「全天收盘 − 缺口」会把港股算成 13:00（错，真实 12:00）。
全天行**仍走已知收盘表**（否则港股会被算成 15:00）。半日市同时**缩短可委托窗口**上界。
推理与逐市场口径写在模块 docstring 里，改动前先读它。

两处使用点：
1. **数据就绪门**（`planner.data_date_for`）：先解析会话本地日（回落最近交易日），再用
   **该日**的真实收盘判「本次会话是否已收盘」——半日市当天不再白等（旧口径要等到全天
   收盘才产出计划）；`trade_second` 缺失/全天 → 逐字沿用 `SESSION_CLOSE_BEIJING`（美股
   EST 05:00 的偏保守 DST 口径不变）；
2. **人工下单时段闸门**（`platform/server/trading.py`）：`trade_place`/`trade_modify` 与
   工作台 `plan-execute` 人工触发在**平台层**（与字段校验同层）先判时段，不在可委托时段
   即拒（零券商调用、不落 OMS/风控行、不写指令文件）；**撤单一律放行**（减少敞口不新增
   风险）。取向是「**宁可放过、不可错杀**」：只挡明确闭市，午休算在窗口内，未知
   市场/未知 `session` 才 fail-closed。

**不覆盖自动执行链**：`auto_execute` → 指令轮询 → `execute.run` 不经过上述收口，它的时段
约束是**守卫 9 的执行窗口**（`exec_window_minutes`）——两条路径的分工写在 `TradeGate`
docstring 里，别以为闸门覆盖了自动路径。

**风控规则 3 只查日粒度**（`is_trading_day`）：它没有时钟，文案是「非交易日：不提交订单」
——钟点判定不在 8 条规则里（那条不变量是「8 条规则是唯一提交入口」，不新增规则编号）。

排障见 [docs/RUNBOOK.md](docs/RUNBOOK.md)「人工下单时段闸门（WP19）」。


## WP14：研究院（因子/规则生产线）

**定位**：把「资讯 + 基本面 + PIT 数据」变成**可验证的规则提案**与研报。提案过机械
验证门后**必须由你在 Web 批准**才上岗。研究院不下单、不启用策略、不写代码进核心库。

### 怎么让研究院开工（在 Harness 对话里说）

| 你说 | 走哪条 |
|---|---|
| "挖个因子"/"提一条规则"/"巡检因子衰减"/"研究 XX 的基本面与资讯面"/"让研究院开工" | **`research-institute`**（四子代理：采集 → 假设 → 检验 → 研报） |
| "现在能不能买"/"看下信号"/"跑个回测" | `quant-trading` 快路径（秒级） |
| "帮我深度分析"/"出一份研报"/"辩论一下多空" | `trading-agents` 12 角色流程 |

### 一条规则的上岗路径（全链路可审计）

```
Harness 提案（rules JSON）→ 机械验证门 rules-validate（IC t 检验 + 5 分位分层单调）
  → passed 进候选池 → 你在 Web 研究页点「批准」→ enabled（记录批准人）
  → 把 rule_id 填进 auto_pipeline.strategies → 次日 build_plan 按该规则生成计划
```

命令行等价物（自动化与排查用；**批准没有命令行等价物**）：

```bash
cd <repo>
DB="${DSH_HOME:-$HOME/.dsh}/trading-data/trading.sqlite"
# 1) 提案 + 验证（协议非法=退出码 1，连库都不进）
~/.dsh/trading-venv/bin/python -m trading_core rules-validate --spec /tmp/rule.json --db "$DB"
# 2) 看候选池
~/.dsh/trading-venv/bin/python -m trading_core rules-list --db "$DB"
# 3) 批准 / 停用：**只能在独立 Web 研究页候选池点按钮**。没有 CLI 子命令、没有 MCP 工具、
#    批准来源（approved_by）由服务端固定——任何能被脚本跑出来的批准入口都是模型自批入口，
#    所以刻意不提供（`rules-decide` 子命令已删除）。
```

### 硬规则（不可绕过）

- **验证不过不许硬凑**：判 `failed` 就改假设、换新 `rule_id` 重走流程——调阈值重跑到
  通过是多重检验作弊。`factors` 只能引用已注册因子（当前 `momentum_20/60/120`、
  `volatility_20`、`ep`）；需要新算子 = 提核心库 PR 人工审查。
- **未成熟因子不得进规则**：情绪 / F10 / 做空域因子先按 PIT 攒历史，连续 **≥250 交易日**
  才可申请走同一套验证门；在那之前只能出现在假设的文字讨论里。
- **批准只在 Web（且无离线等价物）**：`rules-decide`（批准）是**服务进程内动作端点**——
  不进模型工具面、**也没有 CLI 子命令**，批准来源由服务端固定为 `web`，连"自报来源"的
  参数都不存在；`auto_pipeline`（开关）、`confirm-decide`（实盘确认）同样不进模型工具面。
  模型不能自批自己挖的因子、不能自拨流水线开关、不能自批实盘单。模型侧同样禁用
  `trade_place/trade_modify/trade_cancel/plan_execute/switch_mode`。
- **停用随时可停**：Web 停用后**同一进程内立即生效**（规则解析每次回查 DB 状态，失效实例
  即时摘除——这条是 WP14 端到端演练暴露并修掉的一个 fail-open 缺陷）；`disabled` 是终态。
- **研报照旧发布到研究页**：`research_publish` 必带 `sources`（名称/数据时间/引用）。

规则协议字段、样例与四子代理的工具通道清单见
[skills/research-institute/SKILL.md](skills/research-institute/SKILL.md)。

## WP15：值班研究员（L3 定时研究任务）

**定位**：研究院的**定时化**——没人开口时自己开工，消费 L1 机械层排好的任务队列。
它只产**简报 / 因子巡检 / 候选提案**，**不下单、不启用策略**（与 WP14 同一套边界）。

### 开起来（一次性）

```bash
mkdir -p ~/.config/systemd/user
cp install/research-duty.service install/research-duty.timer ~/.config/systemd/user/
# 单元内 %h/dsh-trading-agents 为示例路径，按实际安装位置替换
systemctl --user daemon-reload && systemctl --user enable --now research-duty.timer
systemctl --user list-timers research-duty.timer      # NEXT 即下一次唤醒
```

不想用 systemd 就用 cron（等价行写在 `install/research-duty.timer` 注释里）：

```
50 16 * * 1-5 /home/<user>/dsh-trading-agents/scripts/research_duty.sh >> ~/.dsh/logs/research-duty-cron.log 2>&1
```

### 它做什么

| 任务（白名单三种） | 何时入队 | 产出 |
|---|---|---|
| `daily_brief` | 每交易日数据就绪后 | 当日资讯影响简报 → 研究页 |
| `factor_patrol` | 每交易日 | 因子衰减巡检（RankIC/t 值/分层/半衰期/换手）→ 巡检报告 + 异常告警 |
| `mining_round` | 每周首次 | 因子/规则候选提案 → 候选池（**待你在 Web 批准**） |

入队是**机械层**动作（`enqueue_research`，基础链尾，零 LLM），与 `auto_pipeline` 开关**解耦**：
关掉自动交易，研究照常产出；反过来，研究产物在人工批准前也进不了交易链。

### 没跑成也不会丢

定时器没装、机器关机、headless 被杀，都只是**延后**：任务留在 `research_tasks`，
你下次打开会话时由 Harness 补跑（领取时顺手回收超时任务，`attempts` 达 3 次才判 failed）。
**该脚本不会自己拉起平台服务**——探活失败即非零退出并给指引；日志在
`~/.dsh/logs/research-duty-*.log`。安装/启停/排查表见
[docs/RUNBOOK.md](docs/RUNBOOK.md)「值班研究员（L3）」。

## 工作台与指令示例

按上节启动独立 Web 后在浏览器打开 `http://127.0.0.1:8397`（Harness 内没有工作台面板，
插件只提供 `tradingWorkbench` 服务锚）：

| 页面内容 | 数据从哪里来 |
|---|---|
| 研报结果与来源 | Harness 完成研究后经 `research_publish` 发布 |
| 交易概要 | 由富途工具响应归纳出的订单与下单/改单/撤单动作（按 order_id 去重；只读查询仅计数）|
| 券商持仓 | 富途真实持仓（模拟盘读模拟账户、实盘读真实账户），按账户小计、不跨币种合并；本地缓存 5 分钟 |
| 持仓风险 | 按账户分组的集中度（占持仓市值 / 占总资产两个口径）与浮盈亏分布；**不跨账户合计** |
| K 线图 | 富途 K 线（默认日线，可切 60m/15m/5m/1m）；按周期缓存，切页签不重复取数；**鼠标悬停显示该根 K 线的日期、开高低收、涨跌幅与成交量** |
| 量化预览 | 在 Harness 请求 `quant_signal`、`quant_backtest`、`quant_report` 的结果 |
| 模拟盘/实盘切换 | 独立 Web 的模式切换入口（页头 SIM/LIVE 徽章 →「账户模式」对话框）显式操作；实盘输入「确认实盘」，但不授权订单 |

仍然在 **Harness 对话中**下达指令，例如：

> 分析一下 00700.HK，并把完整报告发布到工作台。
>
> 回测 600519 的双均线策略，把结果放到量化预览。
>
> 查询当前模拟账户的持仓和订单状态。
>
> 展示这笔订单的完整摘要，等我确认后再提交。

工作台页面打开时每 3 秒刷新本地快照，关闭后停止刷新；不会自动调用模型、券商或回测。
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

可选扩展：last30days 社媒研究引擎提供近 30 天社媒/全网叙事的广度面（Reddit/HN/Polymarket/GitHub/YouTube 等免密钥来源开箱即用），`python3 scripts/install_last30days.py` 安装到 `~/.dsh` 后新建会话生效，组合方法论见 [skills/last30days-bridge/SKILL.md](skills/last30days-bridge/SKILL.md)——社媒信号只生成假设，验证与交易一律走工作台通道。

X 与 Reddit 共用同一个专属浏览器登录态（`~/.dsh/x-profile`），**登录一次长期有效**：

```bash
python ~/.dsh/.agent-presets/dsh-trading-agents/scripts/x_search.py --login
```

脚本输出的 `path` 字段标明本次实际路径（`api/graphql` / `api/json` / `web/dom`）。
X 的前端 queryId 与混淆算法会随发版变化，`x_api` 任一环节失败即自动降级到 DOM 抓取，
渠道不会整体不可用。详见 [docs/QUANT-WORKBENCH-PLAN.md](docs/QUANT-WORKBENCH-PLAN.md)。

**研报与量化共用同一套取数实现。** 行情路由、富途 MCP 客户端与回测核心都只有一份，
放在 `plugins/datasource`（`@bstester/dsh-datasource`）；量化平台核心（PIT 存储、
因子/策略/回测、风控/OMS/对账、调度运维）在 `plugins/core`（`trading_core`）。
两者同属安装器 `LIBRARIES`，都会被解到
`$DSH_HOME/trading-python/` 并向交易 venv 写入 `.pth`，所以任何插件脚本都能直接
`from trading_datasource.market import load_bars`、`from trading_core import store`，
无需设置 `PYTHONPATH`。
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
本仓库自己的两层：**账户模式互斥（默认 sim）** + **工作台业务确认（实盘写操作逐笔由用户在
独立 Web 的确认卡片作答，见 [docs/architecture.md](docs/architecture.md) 的「业务确认表述统一」）**。
这带来一个必须知道的推论：模式守卫是**插件级**的，凭据本身允许写操作；
若绕过插件直接以 HTTP 调券商接口（本仓库明令禁止），模式守卫不会拦你。请在授权页面上
按需选择权限，并始终经由 Harness 的工具体系下单。

**安全说明**：未设置模式时默认模拟盘；完整插件模式下有账户工具守卫与**工作台业务确认**
（实盘写操作在独立 Web 确认卡片逐笔作答，独立于权限档位），
会话仍需逐笔复述并确认订单。仅安装 skill 不具备插件级守卫。日常建议只授权只读 scope。

## 模拟盘 / 实盘互斥开关

模式保存在 `~/.dsh/trading-account-mode`，**重启后保留，不会自动回到 sim**。
完整插件按该模式拒绝另一类账户工具；有在途账户调用时禁止切换。

```bash
python ~/.dsh/.agent-presets/dsh-trading-agents/scripts/trade_mode.py        # 查看模式
python ~/.dsh/.agent-presets/dsh-trading-agents/scripts/trade_mode.py sim    # 恢复模拟模式
```

实盘启用只能由用户在工作台确认；脚本不再支持写入 live，模型也不能用参数替用户确认。
切回模拟盘可在工作台操作，或在 Harness 请求 `quant_switch(mode="sim")`。
配置 `DSH_HOME` 时，安装器和运行时共同使用该目录。


## 目录结构

**仓库根目录即 preset 目录**——放进 `~/.dsh/.agent-presets/` 就完成安装。`agent.cordis.yml`
（组合）与 `AGENTS.md`（常驻指令）都从这里被会话加载。

```
├── agent.cordis.yml         # 对话模式组合：persona + 工具 + 富途 MCP 桥 + 五个插件行
├── preset.yml               # 模式元数据（名称/介绍）
├── AGENTS.md                # 工程级常驻指令（会话自动加载）：值班队列兜底纪律 + 常驻纪律
├── skills/
│   ├── trading-agents/      # Harness 十二角色、六阶段投研工作流
│   ├── quant-trading/       # 快路径：信号/回测/短线量化（"现在能不能买"走这条）
│   ├── research-institute/  # 研究院：四子代理因子/规则生产线 + 值班模式手册
│   ├── futu-skills/         # 富途专项技能组（新闻检索/情绪/技术面/衍生品/资金异动…）
│   └── last30days-bridge/   # 近 30 天社媒叙事广度面（可选组件）
├── plugins/
│   ├── datasource/          # 统一数据层（库，非插件）：唯一 MCP 客户端/行情路由/回测核心
│   ├── core/                # 量化平台核心库 trading_core（库，非插件）：PIT 存储/研究/执行/调度与运维
│   ├── engine/              # 研究记录/发布、量化工具、账户策略
│   ├── fin-data/            # 统一新闻与舆情工具
│   ├── workbench/           # tradingWorkbench 服务锚 + 本地研究存储（UI 在 platform/）
│   ├── futu-keepalive/      # token 续期：每 10 分钟检查，余量 < 30 分钟时刷新
│   ├── platform-autostart/  # 会话开始时探活并按需拉起平台服务
│   ├── quant/               # 旧量化 CLI 兼容入口（唯一实现在 plugins/engine/python/）
│   └── trading-agents/      # v2 编排插件（已被 skills/trading-agents 取代，未进 preset）
├── platform/
│   ├── server/              # FastAPI 单进程：HTTP API + MCP + 静态托管（含服务内调度器）
│   ├── web/                 # Vite + antd5 + ProComponents 前端，构建产物 dist/
│   └── requirements.txt     # fastapi/uvicorn/mcp/httpx 锁定
├── scripts/
│   ├── install_plugins.py   # 插件 + 统一 Python 层安装与自检（install|update|link|check|refresh）
│   ├── platform_service.sh  # 平台服务耐用启停与副本刷新（start|stop|status|restart|refresh）
│   ├── e2e_workbench.py     # 后端 82 端点 E2E 扫描（只报告）
│   ├── e2e_web.mjs          # 前端 16 路由真浏览器 E2E（CDP，零新依赖）
│   ├── research_duty.sh     # L3 值班研究员入口（systemd timer / cron 调用）
│   ├── futu_auth.py         # 富途 OAuth 授权向导（纯标准库，跨平台）
│   └── workbench_admin.mjs  # 研究 run 命令行维护（list / cancel / prune）
├── install/                 # systemd 单元（research-duty.service|timer）与 HARNESS_SETUP.md
├── install.sh / install.ps1 # 跨平台完整安装（preset + 插件 + Python 依赖 + 授权向导）
├── tests/                   # Python / Node 两套测试（前端测试在 platform/web/tests/）
└── docs/                    # 架构 / 运维 Runbook / 交接 / E2E 验收 / 工具限额 / 实盘准入
```

## 实盘交易（P4）

尚未达到无人值守实盘准入条件。模式开关和工作台展示可用不代表策略、券商对账、
监控熔断都已完成。实盘前置条件与边界见 [docs/P4-live-trading.md](docs/P4-live-trading.md)。

## 免责声明

本项目输出均为 AI 研究性质内容，不构成任何投资建议。市场有风险，决策需独立。
