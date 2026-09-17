# dsh-trading-agents

> 📐 [专业量化工作台方案](docs/QUANT-WORKBENCH-PLAN.md) · 📋 [交接与复审文档](docs/HANDOVER.md)：已完成总结、验证结论、未验证项、下一份计划、复审清单。

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

**方式 B · 完整安装脚本**（preset + Python 依赖 + 富途授权向导 + 五个插件）

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
> （数据层 datasource + 量化核心 core）+ Python 依赖**，
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
   创建 venv 并安装 akshare 与 playwright。
   预期最后一行为「重启 dsh web → 新建会话 → 选择「交易智囊模式」…」。
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

## 可选：启动独立工作台服务（WP6）

不启动也不影响 Harness 的对话、投研与量化能力——`agent.cordis.yml` 的
`quant-platform-mcp` 行默认 `disabled: true`，服务未起时安静降级。要用独立 Web 工作台：

```bash
# 1. 依赖：Python 侧复用交易 venv；前端构建需要 Node（只需联网一次）
~/.dsh/trading-venv/bin/pip install -r platform/requirements.txt
npm --prefix platform/web install && npm --prefix platform/web run build

# 2. 启动：HTTP API + MCP + 静态前端同一个进程（默认 127.0.0.1:8397）
cd platform && ~/.dsh/trading-venv/bin/python -m server.run
```

浏览器打开 `http://127.0.0.1:8397` 即是工作台（`/healthz` 为存活探针）。
**不能在仓库根用 `python -m platform.server.run`**——标准库 `platform` 遮蔽同名包。

要让 Harness 会话直接调用工作台能力，把 `agent.cordis.yml` 里 `quant-platform-mcp` 行的
`disabled: true` 改成 `false`，然后**新建会话**（已挂载的会话不会重新读取组合）：会话内
出现 `mcp__quantwb__*` 共 59 个工具（WP8 任务 6 起），与 Web 同源（HTTP 与 MCP 调用同一批
处理函数；`confirm-decide` 有意不进工具面，模型不能自批实盘单）。
实盘切换只能在独立 Web 的模式切换入口（页头 SIM/LIVE 徽章 →「账户模式」对话框）
输入口令「确认实盘」完成，成功后提示带 `order_authorized: false`；`switch_mode` 工具
只接受切到 sim（live→sim 回模拟盘），sim→live 一律拒绝。启停/配置/systemd 见
[docs/RUNBOOK.md](docs/RUNBOOK.md) 的「平台服务」一节。

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
  账户模式 → 风控 8 规则（kill 文件=规则 1）→ **业务确认（Web 确认卡片作答，进程内）**
  → broker 适配；live 券商写协议未接入，live 下 `trade_*` 提交即拒（当前设计内行为）。
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
  }
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

**熔断（halt）不会自动恢复**：对账差异或日内熔断触发后，自动执行以 warn 级跳过并在
`daily:digest` 记录原因；恢复必须人工查明原因后清 halt（工作台调度页/`clear_halt`）。
零差异的一次对账**不会**清除已有 halt。

**手工与自动共用同一风控自洽定量口径**：`plan-build`（手工内联权重）与 `build_plan`
（自动策略权重）走同一个 `planner.build_and_freeze`——数量取 `min(权重定量, 风险预算
定量)`（风险预算 = 权益 × `risk_per_trade` ÷ 止损距离），加仓受单笔风险约束、减仓/清仓
不受限；算不出 ATR 的标的进结果里的 `skipped`（不生成「无止损全额定量」这种必被规则 4
拦下的徒劳计划）。手工路径**没有旁路**，这是刻意的统一。

排障与演练（假时钟、窗口超时、熔断拦截）见 [docs/RUNBOOK.md](docs/RUNBOOK.md)「自动流水线（WP9）」。


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

仓库根目录即 preset 目录（放入 `~/.dsh/.agent-presets/` 即完成安装）：

```
├── agent.cordis.yml       # 对话模式组合：persona + 工具 + 富途 MCP 桥
├── preset.yml             # 模式元数据（名称/介绍）
├── skills/trading-agents/ # Harness 十二角色、六阶段工作流
├── plugins/datasource/   # 统一数据层（库，非插件）：唯一 MCP 客户端/行情路由/回测核心
├── plugins/core/         # 量化平台核心库 trading_core（库，非插件）：PIT 存储/研究/执行/调度与运维
├── plugins/engine/       # 研究记录/发布、量化工具、账户策略；含 Python 量化实现
├── plugins/fin-data/     # 统一新闻与舆情工具
├── plugins/workbench/    # tradingWorkbench 服务锚（engine 工具与账户策略依赖；面板已退役，UI 在 platform/）
├── plugins/quant/        # 旧量化脚本兼容入口
├── platform/             # 独立工作台服务：server/（FastAPI 单进程：HTTP API + MCP + 静态托管）
│                         #   + web/（Vite + antd5 + ProComponents 前端，构建产物 dist/）
│                         #   + requirements.txt（fastapi/uvicorn/mcp/httpx 锁定）
├── install.sh / install.ps1 # 跨平台完整安装
└── docs/architecture.md   # 架构与路线图
```

## 实盘交易（P4）

尚未达到无人值守实盘准入条件。模式开关和工作台展示可用不代表策略、券商对账、
监控熔断都已完成。实盘前置条件与边界见 [docs/P4-live-trading.md](docs/P4-live-trading.md)。

## 免责声明

本项目输出均为 AI 研究性质内容，不构成任何投资建议。市场有风险，决策需独立。
