# 交接与复审文档

> **WP4 状态标注（WP5 修订）：** 「七、接手操作注意」新增的 daemon 指令目录/kill 文件/
> 心跳文件运维操作按 WP4 计划规格撰写，**以 WP4 合并后实测为准**（逐步操作见
> [RUNBOOK.md](RUNBOOK.md)）。
> **WP7 状态标注（2026-09-16）：** 服务内调度器（吸收 daemon）、因子快照收集与
> `factors-history`、交易闸门 + 受约束交易工具、富途实时数据直通 8 工具（工具面 41）、富途写通道收窄、一键安装
> 已落地（提交清单与测试证据见
> `docs/superpowers/plans/2026-09-16-wp7-standalone-platform.md` 末节「WP7 验收记录」）。

## 一、目标与不可改变的边界

本项目是 **DeepSeek Harness 的新对话模式 + 原生插件**：

- Harness 负责全部 AI 对话、投研/量化指令、交易指令和逐笔人工确认。
- 工作台自 WP6 起是**独立 Web**（`platform/` FastAPI 单进程托管，默认
  `http://127.0.0.1:8397`），也是**唯一工作台界面**（Harness 内 legacy 面板已于 WP7
  退役，用户决策 2026-09-16）；工作台只展示研报、最近交易响应、量化预览，
  并提供 sim/live 切换。
- 不另建聊天入口，不在工作台提供下单、撤单或任意工具执行接口。
- 模式切换不授权交易；本地模拟台账不代表富途模拟账户或真实账户。

框架目标是投研与量化交易能力，但当前不能描述为“只差重启/用户确认就能实盘”。
详细组件、数据协议、权限边界见 [architecture.md](architecture.md)。

## 二、本轮框架调整

| 部分 | 当前实现 |
|---|---|
| preset / skill | 明确 Harness 单入口、12 角色 6 阶段、发布步骤与模式边界 |
| 研究引擎 | 不再私建无取数能力的 LLM 循环；`run_trading_analysis` 启动记录，Harness 完成研究，`research_publish` 发布 |
| 发布约束 | 校验 run、会话、标的、当前模式、五档评级、报告与带时间的来源；不猜测评级 |
| 工作台 Host | 根级 `tradingWorkbench` **服务锚**（engine 对话工具与账户策略的进程内依赖），持久报告/预览/响应；面板退役后不再注册任何 Connection RPC |
| 独立服务进程（WP6） | `platform/` FastAPI 单进程：`POST /api/wb/<endpoint>`（envelope，22 端点）+ `/mcp`（streamable-http，41 工具；`confirm-decide` 有意不进工具面）+ `platform/web/dist` 静态托管；HTTP 与 MCP 同源调用同一批处理函数；不启动时 preset 行安静降级 |
| 工作台 Client | ~~Host/Client 双入口、宿主 React module factory；结果卡片与面板~~ **已于 WP7 退役**：UI 由 `platform/web` 独立前端承接 |
| 交易动态 | 观察原生账户工具最终响应；工作台页面打开时轮询刷新快照；不伪装券商成交推送 |
| 模式切换 | 用户在独立 Web 的模式切换入口（页头 SIM/LIVE 徽章 →「账户模式」对话框）明确确认 live，携带预期旧模式；在途账户调用租约阻止跨进程切换；脚本只查询/恢复 sim |
| 执行策略 | 账户工具按 sim/live 拒绝不匹配调用；真实写工具经**工作台业务确认**（独立于权限审批，任何档位下都必须确认）；不允许模型走其他通道绕过 |
| 量化 | 修正风险/成本/成交时序；本地模拟与券商数据分离，失败不能回退为零价格或虚构资产 |
| 安装 | Bash/PowerShell 共用插件安装流程，包路径、失败退出、幂等启用与更新行为可重复核对 |
| 会话自动拉起 | preset 行 `platform-autostart`：会话启动自动 GET `/healthz` 检测平台服务，未启动则以分离进程（detached+unref）拉起，日志 `~/.dsh/trading-platform-service.log`；venv/仓库未安装时仅日志提示（指引 `install/HARNESS_SETUP.md`），不强行启动。仓库定位靠安装器写的标记文件 `~/.dsh/trading-platform-repo` |

工作台通过包内 `dsh.bundle.patch` 加载一次根级 Host；安装普通插件包不会自动启用 Host。
fin-data/engine 只在 preset 中启用。安装器保留内容寻址的 tarball 供 pnpm 后续更新，
不初始化、不重置账户模式，也不自动修复非法模式文件。

**行为变化**：`run_trading_analysis` 不再一次返回完整报告，它返回待执行的研究记录。
调用方必须继续 skill 流程并 `research_publish`；工作台不会自动运行 LLM。
`trade_mode.py live` 不再可用；实盘模式由工作台的用户确认入口启用。

## 三、版本与运行依赖

- 工具接口：`@deepseek-ai/dsh-tools@0.1.2-rc.1`。
- API 参考：官方 Harness 源码 `c291e7961a515f6d7af9304e7fd1d257929aef26`，
  使用 `ctx.tools.guard`、`tools/pre-execute`、`tools/execute`、`tools/result`、
  Connection RPC、客户端 `./client` 和 `window.__ModuleLoader__.load`。
- 数据根目录：`DSH_HOME`，默认 `~/.dsh`；前后端不传输 OAuth token。
- 模式文件持久化，**重启不会自动回到模拟盘**。
- 独立工作台服务（WP6）依赖装在 `~/.dsh/trading-venv`：`fastapi` / `uvicorn` / `mcp` /
  `httpx`（`platform/requirements.txt` 锁定，`pip install -r platform/requirements.txt`）；
  前端为构建产物 `platform/web/dist`（`npm --prefix platform/web install && npm --prefix platform/web run build`）。
- 服务启动：`cd platform && ~/.dsh/trading-venv/bin/python -m server.run`
  （或 `python platform/server/run.py`）。**不能**在仓库根执行
  `python -m platform.server.run`——标准库 `platform` 遮蔽同名包。
- `plugins/trading-agents` 为未启用旧脚手架；当前工具实现位于 `plugins/engine`。

### 代码解析路径（开发机 vs 生产）——2026-09-17 实机部署缺口修复

**问题**：量化侧代码有**两份**——仓库源码与安装器解出的**副本**
（`~/.dsh/trading-python/{datasource,fin-data,core}`，venv 的 `dsh-trading-python.pth` 指向它）。
父进程与子进程解析规则不同：

| 场景 | 谁启动 | 解析到 | 机制 |
|---|---|---|---|
| 服务进程（Web/MCP/调度器主体） | `server.run` | **仓库源码** | `run.py` 把仓库数据层插到 `sys.path` 最前 |
| 作业子进程（`python -m trading_core …`） | `daemon._subprocess_runner` | **安装副本** ← 缺口 | `subprocess` 是新解释器，不继承父进程 `sys.path`，只按 `.pth` 找 |
| 脚本子进程（fin-data 的 `fin_sentiment.py` 等） | `sentiment._default_runner` | 同上 | 同上 |

**后果**：开发机上「代码改了、修复不生效」——实测副本与仓库多份文件 md5 不一致、
新符号在副本里出现 0 次，作业链每条腿都在跑旧代码。

**修复**：`trading_datasource/repo_paths.py`（**唯一实现**，core 与平台侧共用）把
**存在的**仓库数据层经 `PYTHONPATH` 前置给子进程；仓库不在场时返回 `None`（继承环境
→ 回落副本，即生产的正确行为）。fin-data 脚本路径同样**优先仓库**
（`plugins/fin-data/python/<script>`），保证「脚本与数据层同版本」。

**运维要点（生产/无仓库环境）**：**改了 `core`/`datasource`/`fin-data` 后必须刷新副本**，
否则重启也只是重跑旧代码：

```bash
scripts/platform_service.sh refresh   # 只重解这三份副本 + 重写 .pth，不动 profile/pnpm/服务
scripts/platform_service.sh restart   # 服务进程自身换代码需要它（子进程下次拉起即生效）
```

判据：`~/.dsh/trading-python/core/trading_core/<file>.py` 的 md5 应与
`plugins/core/python/trading_core/<file>.py` 一致（`refresh` 会打印各目录模块数）。
**开发机（有仓库）不需要刷新**——子进程已优先仓库代码；但**跑真实 CLI 验证时要注意**：
直接 `python -m trading_core …` 仍走副本（没有我们的环境前置），这正是刷新后应复跑一次
真机命令的原因。


## 四、验证分层

仓库提供可重复的本地测试，而不是依赖上一会话的口头“已跑通”结论：

```bash
# 安装 engine 的开发/运行依赖后
npm install --prefix plugins/engine --ignore-scripts
node --test tests/*.test.mjs

# 使用已安装 pandas/numpy 的量化 Python 环境
python -B -m unittest discover -s tests -p 'test_*.py' -v
```

Node 用内置测试运行器，包括真实 Cordis + 钉版工具服务组合、账户守卫与结果事件、
工作台持久状态和模式切换（历史：客户端入口与 RPC 白名单用例已随面板退役删除）。
Python 用标准库 unittest，行情与账户使用隔离样本/临时目录，不发真实交易请求。

本轮本地基线：17 项 Node、52 项 Python 用例通过，覆盖包内根级 patch、模拟台账提交与
模式锁的串行化、安装失败及模式文件不变性。Bash 语法检查通过；当前环境未运行
PowerShell 安装器，也未启动完整 Harness Web。后续券商模拟订单实测见下节。

本地合约成立不等于完整部署验收。仍需在目标 Harness 版本、实际操作系统和新会话中
确认 preset 发现、插件加载、工作台业务确认（服务侧 Web 确认卡片）及真实数据接口行为。

### 2026-09-12：富途模拟账户实测

使用本机已安装的 Harness `0.1.2-rc.1`，通过 headless profile、原生
`dsh-mcp-client` 和 ToolRuntime 加载本项目 engine/workbench，额外限制为模拟账户及
明确列出的查询工具。没有绕过 Harness 直接调用交易 HTTP，也没有调用实盘工具。
本机 CLI 不在 PATH；实际入口为
`~/.dsh/profiles/node_modules/@deepseek-ai/dsh/lib/bin.js`。

| 项目 | 实测结果 |
|---|---|
| OAuth | 原凭据下账户和行情均报 `-32603 internal error`；用现有 refresh 凭据续期后，同一工具与参数恢复成功，没有扩大授权 |
| 账户与基线 | 获取模拟账户列表、港股模拟资金、持仓、历史订单及腾讯行情和整手数量 |
| 下单 | 两笔腾讯 `00700`、100 股、400 港元限价模拟买单均获得券商订单 ID，并查到 `status=2`、`cum_qty=0` |
| 改单 | 仅传 `new_price` 返回业务错误且原价不变；另一笔同时传 `new_price=399`、`new_qty=100` 成功，查询确认新价 |
| 撤单与恢复 | 两笔均查到 `status=5`、`cum_qty=0`；冻结资金为零，现金/购买力与持仓恢复到实测前状态 |
| 收尾 | 模式保持 sim、在途调用为零；未留下测试挂单，原有持仓未变 |

这证明了券商模拟账户的查询、订单受理、改单、撤单及资金释放链路，
**不证明成交、部分成交、卖出回补、自动对账或完整 Web 对话已完成验收**。
实测日为周六，未通过追价或市价单强行制造成交。原始响应保留在本次会话的本地证据中，
不将账户标识、资金明细或凭据写入仓库。

实测还发现：持仓接口虽将 `market` 标为可选，港股账户省略时返回 `ret_code=-5`，
显式传 `market=1` 后成功；市场编号必须取实际账户响应，不能照抄静态示例。
部分业务失败的 MCP 外层仍是 `isError=false`，必须检查文本 JSON 的 `ret_code`。

### 授权范围的实测行为（2026-09-13 重授权时发现）

脚本请求的 scope 是 `quote:read accid:* trade:read`，但富途授权服务端下发的实际 scope 为
`quote:read quote:write trade:read **trade:write** accid:...` —— **多授了交易写权限**。

结论：**请求的 `scope` 不是权限上限**，不能靠收窄参数来限制能力。真正的执行边界只有两层，
都在本仓库内：

1. 账户模式互斥（`~/.dsh/trading-account-mode`，默认 sim，由 `policy.js` 强制）；
2. 工作台业务确认（逐笔）+ 会话内摘要确认；两者独立于 Harness 权限档位。

必须交代的推论：模式守卫是**插件级**的。凭据本身允许下单，若有人绕过插件直接以
HTTP/shell 调券商接口，模式守卫不会拦截——仓库文档明令禁止这种行为，但它是**约定**，
不是技术强制。

## 四之二、工具与数据源体检（2026-09-13）

对 91 个富途工具 + 8 个非富途工具逐个实调，结论已落库到
[docs/TOOL-LIMITS.md](TOOL-LIMITS.md)。要点：

- **两个必修规避**：取新闻用 `fin_news`（`quote_news_search` 恒空）；
  改单失败改用"撤单 + 重新下单"（`sim_trade_modify_order` 间歇性 `-5`）；
- **A 股无实时行情权限**（`-9`），日线/资金流/选股可用，实时报价与盘口不可用；
- 3 个工具是**上下文炸弹**（单次 336KB 且无分页），另有 3 处易错字段名；
- **13 个实盘工具被安全策略拒绝属预期**，不是故障。

## 五、尚未完成的能力

| 缺口 | 必须如何描述 |
|---|---|
| 富途 K 线历史数据问题 | 之前的裸 HTTP internal error 是历史记录；不能认定重启或完整 SDK 必然修复 |
| 富途模拟盘订单生命周期 | 查询、受理、改单、撤单和资金释放已实测；成交/部分成交、断线后订单去重与自动对账仍需完善 |
| 业务错误归一化 | `ret_code != 0` 可能包在 `isError=false` 内；工作台当前错误标记只跟随工具外层状态，不能把无错误标记视为业务成功 |
| 授权续期 | **官方 `expires_in = 7200`（2 小时）**，续期不换发 refresh_token。实测 token 过期时 `initialize` 仍成功、而所有 `tools/call` 返回 `internal error`（不是 401）。两条通道处理不同：脚本通道遇该特征自动续期并重试一次；MCP 通道由 preset 里的 `futu-keepalive` 行按节奏提前续期（其 Authorization 头在行挂载时求值一次并被 Zod 压平，重连不重读，因此无法做到每请求刷新）。**实测 preset 目录无 watcher**，所以续期只对新建会话生效——已在运行的会话必须新建会话才会重新读取组合 |
| 实时交易推送 | 工作台刷新已有工具响应，不自动查询券商，也未建立成交订阅 |
| 多市场量化 | 首期为 A 股日线，港美历史数据、费用和交易规则需要独立完善 |
| 生产级风控 | 本地风险公式/止损意图不等于券商托管止损、日内熔断、组合风险与紧急停机 |
| 自动运行 | 定时调度、指令队列、监控告警由 WP4 daemon 交付（以 WP4 合并后实测为准）；合并前无生产级定时调度与无人值守服务 |
| 记忆复盘 | 旧工作区 memory 由 Harness skill 维护；工作台旧研报不自动变为复盘教训，不自动迁移旧全局 memory |
| 审计保留 | 工作台各列表保留最近 100 项，不是完整审计档案；完整过程留在 Harness 会话 |

### 社交渠道实测结论（X / Reddit）

X 的 GraphQL 搜索此前 404，根因是缺反爬头 `x-client-transaction-id`，不是鉴权。采用社区实现
`XClientTransaction`（PyPI 包，导入名 `x_client_transaction`）后打通：热启动约 3s、单次约 20 条，
DOM 抓取保留为降级路径（约 40-50s）。Reddit 走同源 `/search.json`。

两点必须在交接时说明：
1. **queryId 不在首页 HTML 里**，要从 `client-web/main.*.js` 提取，否则解析必然失败；
2. queryId 与混淆算法随 X 前端发版变化，属**已知脆弱点**；`x_api` 任一环节失败返回 `None`
   并由 `x_search.py` 自动降级，因此渠道不会因上游变更整体不可用，但延迟会退化到 DOM 水平。

脚本输出的 `path` 字段是判断本次走哪条路径的唯一依据，`sources_status` 会显示 `ok:api/graphql` 形式。

## 六、下一阶段优先级

1. 在目标 Harness 上完成完整安装与新会话端到端验收，明确版本与平台。
2. 核对真实行情来源、时间、交易日历和回测假设，修复富途 K 线入口。
3. 补齐业务错误归一化与授权续期，再于交易时段完成模拟成交、部分成交和自动对账。
4. 完成告警、审计、日内熔断、账户/订单级限额和恢复流程。
5. 策略做样本外/滚动评估，再由用户决定是否小额实盘。

工作台图表与体验可以逐步增强，但不能排在实盘之后才补交易状态与风控基础。

## 七、接手操作注意

- 不读出或复制本机 token、refresh token、浏览器登录态。
- 不以 token 文件存在断言授权有效；缺工具还可能是加载、网络或权限问题。
- 不重新执行真实下单来“验证链路”，除非用户明确授权具体订单。
- 有残留写锁/调用租约时，先确认进程已停止并核对券商状态，再清理具体文件；
  未知在途状态不得自动清除。
- 安装后的新会话必须检查能力，不能把旧会话缓存状态当当前源码表现。
- **daemon 指令目录**（`~/.dsh/trading-commands/{pending,processed}/`）是工作台 → daemon
  的唯一通道，白名单仅 5 种指令（execute_plan/cancel_plan/kill/unkill/run_job）；
  排查先看 `pending/` 积压与 `processed/` 是否按 nonce 去重，不要手工塞非白名单文件。
- **kill 文件**（`~/.dsh/trading-kill`）存在即风控规则 1 拒绝一切订单；
  演练用 `scripts/drills.sh`（建 kill → 断言拒单 → 清除，输出 JSON）；
  `unkill` = 人工确认后删除该文件，不做任何自动清除。
- **心跳文件**（`~/.dsh/trading-daemon.json`）：`heartbeat` 超过 5 分钟未刷新即 daemon
  失联（工作台标红）；`critical: true` 是告警常驻红点标志位，处置后随恢复流程清除。
- **不自动清除未知在途租约/订单**：执行中断留下的 `unknown` 订单与调用租约，
  一律先查券商核对真实状态，再按 RUNBOOK 场景 2/3 迁移状态机或清 halt；禁止重放。
- **值班研究员（L3）定时研究任务（WP15）**：`scripts/research_duty.sh` + `install/research-duty.{service,timer}`
  是**研究侧**定时执行体——只消费 `research_tasks` 队列产简报/巡检/提案，**永不直接下单、
  永不直接启用策略**，与交易侧自动流水线（WP9）互不调用。定时器没装/机器关机/headless 被杀
  都只是**延后**：任务留在队列，等**会话开始/恢复后的首次交互**由 Harness 按仓库根
  `AGENTS.md` 的启动纪律补跑（`claim` 会顺手回收超时任务）——**这不是后台自动动作**，
  没有任何 turn 的会话不会消费队列（任务不丢、只延迟）。
  时刻链：`reconcile` 19:00 → 当日 `digest` → `enqueue_research` 19:05（基础 GLOBAL 链）→
  定时器 19:20 唤醒；服务需在 19:05 前已运行，否则任务留待下次唤醒或会话兜底。
  该脚本**不会**自己拉起平台服务（探活失败即非零退出并给指引；拉起是 platform-autostart 的职责）；
  日志在 `~/.dsh/logs/research-duty-*.log`，排查表见 `docs/RUNBOOK.md`「值班研究员（L3）」。
  队列载荷是白名单结构化引用（队列即攻击面）：直接改库塞自由文本会被领取侧拒领并留 critical 告警，
  队列**暂停在队首等人处置**——这是刻意的 fail-closed，不要为「让它跑起来」而绕过校验。
- **双进程数据约定（WP6）**：独立服务进程与 Harness 进程共享同一份
  `<DSH_HOME>/trading-workbench.json`、模式文件 `trading-account-mode` 与指令目录
  `trading-commands/`；写路径靠既有**原子写 + 独占锁**互斥，两处同时切换模式的竞态由
  `expected_mode` 复核兜底（后到者拒绝）。排查数据不一致时先确认没有两个进程同时在写。
- **实盘写确认 = 工作台业务确认（唯一路径在服务侧；WP7 起现行，2026-09-16 面板退役后无第二条）**：
  确认由**服务进程**发起（工作台 `trade_*` 工具的交易闸门，`store_access.request_confirmation`），
  用户在**独立 Web 确认卡片**作答（`confirmation`/`confirm-decide`，同进程，中文订单摘要，
  TTL 120s 超时按拒绝收尾 fail-closed）。确认是**进程内存态**（刻意不落盘），服务重启即清空。
  Harness 侧富途写类被 policy guard 一律拒绝并指引工作台，**不产生任何待确认**。
  （历史：main `624ccd0`~WP6 期间曾有「插件 pre-execute 发起、Harness 内 legacy 面板作答」的
  路径与「两进程确认表互不可见」的跨进程限制；该路径连同面板、Host RPC 与 Node 侧确认三方法
  已于 WP7 随面板退役删除，详见 `docs/architecture.md` 的「业务确认表述统一」。）
- **Python 侧快照的诚实边界（WP6）**：服务进程的 store 访问层只读
  `trading-workbench.json`，**不合并** pending observations（合并仍由 Harness Host 完成），
  因此合并前的账户响应不会出现在独立 Web 快照里；`trade_summary`/`audit` 链是 Node
  原实现的 Python 移植版，等价性由差分测试钉死（见 `tests/test_wp6_summary_audit.py`）。
- **面板已退役；Host 仅作服务锚（WP7，用户决策 2026-09-16）**：legacy 面板（client.js）、
  Host Connection RPC 面与 Node 侧确认三方法已删除；`plugins/workbench` 的 Host 行保留仅为
  `tradingWorkbench` 服务锚（engine 对话工具与账户策略——模式互斥/租约/观察记录——的
  进程内依赖），删除该行即破坏 engine。工作台 UI 与数据访问只在 `platform/` 独立服务。
- **服务依赖与启动（WP6）**：依赖 `platform/requirements.txt`（FastAPI/uvicorn/mcp/httpx，
  装在 `~/.dsh/trading-venv`）；启动 `cd platform && ~/.dsh/trading-venv/bin/python -m server.run`；
  未构建 `platform/web/dist` 时 `GET /` 404，取数类工具全报 `trading/*-unavailable`
  多为服务未用 venv 解释器启动。
- **调度线程随服务 lifespan 启停（WP7）**：`Scheduler` 是服务进程内的 daemon 线程，
  服务停=调度停（systemd 只需管服务进程）；tick-first——启动即补跑当日到期作业，
  与手动 daemon CLI 共享 `daemon:state` 的 ran 标记（同日作业不重复执行）。
  `/healthz` 的 `scheduler.last_error` 保留最近一次 tick 异常、**成功不自动清除**——
  它是「最近一次出错记录」不是「当前是否故障」；确认恢复看 `alive: true` 与后续作业留痕，
  抹掉旧记录只能重启服务。
- **交易闸门的 fail-open 有界口径（WP7）**：闸门在业务确认通过前对 broker 保持零调用，
  风控 ctx 的敞口字段取**保守默认**（equity=1_000_000、持仓市值空表、持仓数 0、日亏 0），
  由此有两个**窄口**：①真实权益远小于默认值的**微小账户**——规则 4 的单笔名义封顶按默认
  权益计（1_000_000×risk_per_trade 1% = **1 万名义**），对小账户偏松；②**叠加场景**——
  存量持仓不可见，规则 5/6 只对本单全额名义生效，拦不住与已有持仓叠加的超限。
  两口都受规则 4 的 1 万名义封顶**限界**（宁可错拒也不放大敞口）；接真实盯市数据是后续任务，
  接入前不要把这层风控当完整敞口约束。
- **futu 写动词 fail-closed 收紧（WP7）**：`trading_*` 族**未知动词按写拒绝**（实盘不能赌），
  `sim_trade_*` 族未知动词按读处理（sim 写伤害有界，已知 4 个写动词仍按写拒绝）；
  上游新增写动词不会被当读放行。只读研究不受影响。
- **确认 TTL 与 `toolCallTimeoutMs` 的关系（WP7）**：服务端确认 TTL=120s
  （`store_access.CONFIRM_TTL_MS`），到期按拒绝收尾（fail-closed）；preset 的
  `quant-platform-mcp` 行 `toolCallTimeoutMs: 180000` = TTL 120s + 子进程取数余量——
  模型侧即使超时，服务端闸门仍把订单按未批准拒绝收尾，**不存在「模型超时导致订单悬空」**。
  改 TTL 时两边要一起评估。
- **安装器幂等要点（WP7）**：`scripts/install_platform.py` 五步（venv/deps/web/service/verify）
  重复运行安全——venv 已存在报 `already-exists`、dist 新于 src 跳过重建、8397 已有服务报
  `already-running`，都是正常行为不是错误；`--skip-venv/--skip-deps/--skip-web/--skip-all`
  分层跳过，任一步 `ok=false` 退出码 1；`--dry-run` 零副作用可预览。`install_plugins.py link`
  在每次合并 WP 分支后要重跑一次，否则 venv 里的 core/datasource 是旧副本。
- **服务优先仓库数据层（WP8）**：服务进程经 `server/run.py` 的 sys.path 处理**优先加载
  仓库内** `plugins/datasource`/`plugins/core` 的 Python 模块，已修复「venv 副本滞后导致
  服务缺新模块/AttributeError」一类问题；但 **Harness 内 fin-data/engine 插件仍用 venv 副本**，
  datasource/core 新增模块后仍要跑 `scripts/install_plugins.py install/update`（见 RUNBOOK）。
- **OpenAPI 凭据与通道（WP8）**：凭据在 `~/.dsh/futu-openapi.json`（appkey 模式或 OAuth，
  权限 0600；授权跑 `scripts/futu_auth.py --openapi`）；`trading-platform.json` 顶层
  `futu_channel: openapi|mcp` 切通道（默认 mcp=WP7 行为零变化）；openapi 无凭据 →
  `trading/openapi-unavailable`（如实拒绝，不回落）。连通性自检
  `python3 scripts/futu_openapi_check.py --app-key <ID>`。
- **通道分派与回退（WP13 任务 3）**：`futu_channel=openapi` 自 WP13 是**完整通道**——
  sim 全链路（计划生成取持仓/权益、执行下单、对账读券商事实）与 sync 五项统一经
  `trading_datasource.channel`（`sim_call` / `fetch`）分派，实现唯一；此前
  `planner.plan_auto`/`reconcile.daily` 硬编码 MCP 造成的通道分裂已修复。分层语义要分清：
  **服务取数端点**（`server/futu_data.py`）在 openapi 无凭据时**如实拒绝**
  （`trading/openapi-unavailable`）；**core/datasource 的通道分派**在 openapi 已配置但凭据
  缺失时**回退 MCP**（`fetch` 返回第二元素 `"mcp(fallback)"` 供调用方告警）。
  排查顺序：①`futu_channel` 是什么；②凭据是否就绪（`channel.openapi_ready()`）；
  ③REST 调用是否报错——**REST 失败不会静默换 MCP**（换通道会把限频/权限错误伪装成 MCP
  行为），失败原因原样出现在作业告警与 `scheduler.last_error` 里。
  **遗留（如实登记）**：`sim_call` 出来的 callable 只返回数据，**sim 路径丢弃回退标记**
  （只有 `fetch` 携带），因此无法从返回值判断「本该 REST 却回退」；当前只能靠「凭据是否
  配置」判断，若要更强可观测性需改 `sim_call` 的返回契约（涉及全部调用点，未做）。
- **`trade_*` 新字段与确认卡片（WP8 任务 6）**：`trade_place/trade_modify` 暴露官方全字段
  （8 种 order_type/GTC/时段/触发价/港股手数/多腿），字段校验在风控与确认**之前**；
  live 下单服务端确认 TTL 120s，确认卡片披露「风控基准价」（市价类订单取本地日线最近收盘，
  取不到 fail-closed 拒绝）；`side` 暂限 BUY/SELL。sim 仍只支持限价当日单（扩展字段如实拒绝）。
- **WS 推送语义（WP8）**：推送仅作**加速**，不是唯一事实源——断线期间事件不补发、
  不保证顺序，重连（自动重鉴权+按订阅意图重订阅+refresh 5/10 分钟保活）后靠
  60s 对账兜底轮询收敛；状态看 `/healthz` 的 `push` 字段（reconnects/last_error）。
  排障详见 RUNBOOK「OpenAPI 凭据与通道（WP8）」。
- **自动流水线开关与窗口（WP9）**：`trading-platform.json` 顶层 `auto_pipeline`
  （`enabled` 默认 false）。`exec_at` 为**北京时间**；美股默认 22:35 按夏令时写死，
  冬令时需人工调早一小时（不做自动 DST 换算）。`exec_window_minutes`（默认 30）是执行
  窗口：**超出窗口的计划不自动执行、留待人工**——调度器 tick-first（启动即补跑当日到期
  作业）时，靠它防止收盘后才去执行早盘计划；窗口外会在告警里看到「已超执行窗口」。
  对账差异会置 halt，此时 `auto_execute` 由守卫 4（熔断）拦下并要求人工核对
  （差异只暂停、不自动平仓）；恢复走 RUNBOOK 场景 3。
  **halt 的可见性与恢复口径（I3）**：守卫 4 现在以 **warn** 级跳过并附
  `原因 + 设置时间`（原先是 info，自动链路停摆时页面上看不到异常），`daily:digest`
  同时带 `halted`/`halt_reason` 字段供流程页与运维读取；**halt 永不自动清除**——
  一次零差异的对账不会解除熔断，必须人工查明原因后 `clear_halt`。
  关注池/策略配置（I1/ K1）：`strategies[].market` 决定市场范围（SH 链含 SZ/BJ，
  各市场独立计算权重与 `max_positions` 截断），`strategies[].watchlist` 只选**池键名**
  （缺省 `watchlist`，可省略）；显式指定的池键不存在 → 当日跳过该策略 + warn 告警
  （fail-closed，不会静默改用别的池子）。
- **假时钟 `DSH_FAKE_NOW`（WP9 演练/测试专用）**：格式 `YYYY-MM-DD HH:MM:SS`，
  设置后 daemon 调度与 `plan-auto`/`auto-execute`/`reconcile-daily` 全部按它取时刻
  （子进程作业靠它对齐时间线）。生效时工作台告警列表会出现一条 warn「假时钟生效」——
  **看到这条告警说明环境变量还挂着，演练结束务必 `unset DSH_FAKE_NOW`**；格式写错会
  fail-closed 报错（不静默回落真实时间）。演练示例见 RUNBOOK「自动流水线（WP9）」。
- **常驻入口二选一（WP9）**：服务内调度器（`platform/`）已包含指令轮询；独立
  `python -m trading_core daemon` 仍可用但**建议只留一个常驻**——两者靠 processed/nonce
  与 kv ran 标记互斥，同跑不会重复执行，但会产生重复的告警噪音。
- **规则候选池与人工批准（WP14）**：规则 = 声明式 JSON spec（`rules` 表），状态
  `candidate → validating → passed/failed → enabled/disabled`。
  - **批准/停用只能人工**：独立 Web 研究页候选池按钮 → 端点 `rules-decide`
    （`enable` 仅对 `passed` 放行；`disable` 对 candidate/failed/passed/enabled 放行 →
    终态留档）。该端点与 `auto_pipeline` 一样**有意不进 MCP 工具面**——模型不得自批自己
    挖的因子；模型侧同时禁用 `trade_place/trade_modify/trade_cancel/plan_execute/switch_mode`
    （清单见 `skills/research-institute/SKILL.md`，由 `tests/test_wp14_skill.py` 锁定）。
    该端点还是**进程内动作**（服务直调 `rule_engine.decide_rule`），**没有 CLI 子命令**：
    批准一旦有可脚本化的等价入口，「批准只在 Web」就形同虚设（历史缺陷：CLI 与 Web 同
    路径、来源靠 `--by` 自报），因此批准来源由服务端固定、不接受任何传入。
  - **批准痕迹在哪**：`rules.approved_by`/`approved_at`（只可能由服务端写成 `web`——
    没有 CLI 批准入口，也没有可传的来源参数）。`rules.validation` 存最近一次验证报告
    摘要（IC 均值/t 统计/分层单调/半衰期/换手率）；复核对齐一律跑 `rules-validate`
    （与验证门同一实现，不手写第二份阈值口径）。
  - **启用的机械落地**：`auto_pipeline.strategies[].strategy` 填 `rule_id`；`plan_auto`
    解析时**每次回查 DB 状态**，非 `enabled` 一律跳过并在告警里给出当前状态。
    **停用即时生效**：失效规则实例会从 `strategies.REGISTRY` 摘除——这是 WP14 端到端演练
    暴露并修掉的 fail-open 缺陷（旧实现把进程内注册表当一级事实来源，长驻服务里停用后
    仍会被消费），回归门
    `tests/test_wp14_e2e.py::UnapprovedRuleNeverConsumedTest::test_disable_after_enable_stops_consumption`。
  - **回滚 = 停用**：`disabled` 是终态（不可再启用）；要复跑同一假设就**换新 `rule_id`**
    重新提案——`rules-validate` 拒绝重复验证既有 rule_id，防静默重置已通过/已启用的规则。
  - **验证不过不许硬凑**：`failed` 的规则既不能批准也不能被 `plan_auto` 消费；调阈值重跑
    到通过属多重检验作弊，正确做法是改假设、换 id、重走流程。协议非法（未注册因子等）
    在 `rules-validate` 阶段就以非零退出拦下，**连库都不进**。
  - **未成熟因子**：情绪/F10/做空域（`rule_engine.IMMATURE_FACTOR_PREFIXES`）一写进
    `factors` 即被协议层拒绝；需连续 ≥250 交易日攒数后按演进条款申请放行。

## 八、已知限制与遗留登记（WP9–WP15 审查，2026-09-16）

本节登记**已确认、有意不修**的限制与遗留项（对应审查编号 R1–R11）。修掉的三项
（R1 终态回报幂等、R2 超时不计 attempts、R3 全链时间序）见提交历史与下列说明的
「已修」标注；**其余项按现状接受**，接手时不要把它们当故障排查。

### 8.1 队列失败模式的两种事实（R2 已修，模式本身要记住）

`research_tasks` 的失败有**两种不可混同的事实**，实施时曾混为一谈（已修）：

| 事实 | 谁累加 | 上限 | err 口径 |
|---|---|---|---|
| 执行体**真的试过并失败**（回报 `ok=false`） | `attempts` | `TASK_MAX_ATTEMPTS=3` | 执行体自报原因 |
| 执行体**领取后没回来**（会话打断/headless 被杀） | `timeouts` | `TASK_MAX_TIMEOUTS=3` | 「执行体未回报（连续 N 次超时…）」 |

历史缺陷：旧实现把超时算进 `attempts`，于是**三次打断就把一条从未被尝试过的任务判
failed，err 还误报「已达重试上限」**——运维据此排查会被带偏。配套纪律已写入
`AGENTS.md` 与 `skills/research-institute/SKILL.md`：**领取必须回报，放弃也要
`ok=false` 说明原因**（被打断可以，静默离开不行）。终态重复回报**幂等**（原样返回、
不重复告警），因此重试回报不必先查状态。

### 8.2 情绪采集被 SIGKILL 时的孤儿（R11，2026-09-17 实机实证）

作业在 **900s 上限被外部 SIGKILL** 时，`sentiment_snapshot` 内的全部清理代码都不执行
（SIGKILL 不可捕获）→ 其子进程（`fin_sentiment.py` 及其拉起的浏览器）变孤儿。已实测：
`job_kill`（SIGKILL）我的一次真机跑后，`fin_sentiment.py --ticker SH.600009` 存活。

- **已做的**：超时分支 / `finally` / **SIGTERM 守卫**三条路径整组回收（SIGTERM 的默认
  动作会跳过 `finally`，这是最常见的孤儿成因，已封堵）；
- **未做的（本项）**：SIGKILL 路径。长期解法二选一——① 子进程侧 `PR_SET_PDEATHSIG`
  （Linux `ctypes`，父死即被内核杀）；② 外层按**进程组**杀（`daemon._subprocess_runner`
  目前对 CLI 子进程用 `subprocess.run(timeout=)`，其超时只 SIGKILL 直接子进程，不涉进程组）。
- **运维处置**：中断/超时作业后跑一次 RUNBOOK「情绪采集 → 孤儿检查与手工清理」的两条命令。

### 8.3 WP17：模拟盘全自动闭环（2026-09-17 实现 / 2026-09-18 真机复验）

**背景**：本机 A 股模拟账户「权益 812,231.816 / 可用现金 55,257.816 / 已持 8 只」而
默认 `max_positions=5` → 自动流水线**结构性跑不起来**；券商侧还有一张本地从未登记的探针单
（`7147945`/`SH.603993`）→ 每日对账 `missing_in_oms` → critical + `set_halt`。两项互相放大：
计划买不动，链还被熔断锁死。本轮修复与**新不变量**（改动前必读）：

| 修复 | 不变量 | 锁定测试 |
|---|---|---|
| 买入按**可用现金**封顶 | 定量 = `min(权重定量, 风险预算, 现金可买整手数)`；**多买单共享一笔现金**、按计划顺序逐单扣减；**卖单不受限**、**卖出所得不计入可买现金**；现金不可得 → 一笔买单都不生成（**绝不用权益冒充现金**）；调用方不给现金事实（`broker_cash=None`）时返回体**无** `cash` 键、语义逐字不变 | `tests/test_wp17_cash_cap.py::CashCapTest`、`BrokerCashSnapshotTest` |
| 计划期**结构性预警** | 现金封顶/现金不可得/持仓数超限/零订单都发 warn 并进 `plan_auto` 的 `warnings`；**零订单计划在流程页按 `skipped` 呈现**（「没做成」不得显示「已完成」）；预警**不改作业状态**（作业确实跑完） | 同文件 `PlanAutoStructuralWarningTest`（含 `_ALERT_STATUS`/`_CONTENT_OUTCOMES` 字面量锁） |
| `missing_in_oms` **收编**（收敛路径） | 券商独有订单在差异判定前导入台账（`plan_id=reconcile-import`、`err` 带券商单号与原始状态码）后**重新匹配**；只认已发布枚举，表外码/无单号**不导入**、保留为差异；幂等；**只读券商** | 同文件 `ReconcileImportTest` |
| 收编的两条配套口径 | ① 收编行按**对账日期**落 `created_at`（不是墙钟）——订单匹配按对账日取窗，否则重放历史日期时刚收编的行落在窗口外、critical + halt 原样复现；② 收编行**不建立持仓知识**（`footprint_symbols` 排除 `IMPORT_PLAN_ID`）——零成交收编单不会把历史存量持仓从 `untracked` 升级成 `missing_side`，有成交的经 fills 回填照常进足迹 | 同文件两个对偶用例 |

**真机证据（2026-09-18 00:19–00:22，sim）**：

- `plan-auto --market SH --today "2026-09-17 16:20:00"`（临时开 `auto_pipeline`，跑完**字节
  还原**配置，md5 `776a71d4…` 比对一致）：订单 `SH.600010 BUY 26100 @2.11` =
  `55,071 ≤ 55,257.816`（封顶后剩 186.816），`plan.cash.capped=["SH.600010"]`，warnings =
  持仓 8 ≥ 5 + 现金封顶。验证计划 `PLN-20260917-sim-2B02` 随后按**既有过期语义**
  （`cancel_stale_auto_plans`，与次日调度同一实现）作废 → 计划与订单一并 `cancelled`：
  计划行与告警 140/141 留库作证，OMS 在途单 0（`auto_pipeline` 已还原为关闭态，从未提交
  任何订单到券商）。
- `reconcile-daily --today 2026-09-17`：`orders_imported=1` → `diffs=[]`、`halted=false`，
  `SH.603993` 仍如实列在 `untracked`（券商持 2100 股、本地无成交足迹）；随后 `clear_halt`
  （`halt:active=false`，`set_at` 2026-09-18 00:22:25）。
- **诚实登记**：告警 139（critical 对账差异，00:15:02）是**本次复验过程**产生的——第一次
  重放跑在「收编已实现、持仓足迹口径尚未修正」的中间代码上，`SH.603993` 因收编行进了足迹
  而报 `missing_side`。修正后同一重放 `diffs=[]`（告警 142 = 对账无差异）。该 critical 是
  历史记录（平台无 ack 入口），随收敛而不再重现；它同时是「为什么必须有上面第 ② 条配套
  口径」的实证。
- 未由本任务决定：是否调 `max_positions`/`risk_per_trade`、还是减仓——属交易决策。现金
  封顶只保证「计划不再下出买不动的量」，不等于策略在本机会盈利。
- R9②（`_ALERT_STATUS` 无锁）**部分收敛**：WP17 的四条标题已加字面量锁；其余标题与 emit
  点的一一对应仍靠人工核对。

### 8.4 目标外持仓自动清出（2026-09-17 实现 / 2026-09-17 真机只读复验）

**背景**：WP17 的现金封顶让「计划不再下出买不动的量」，但**账户与策略组合仍不收敛**——
受管集合（关注池 ∩ 策略 universe）之外的存量持仓刻意不进 diff，而 `max_positions=5` 拦住
新建仓：实机 A 股模拟账户持 8 只（`SH.601179 SZ.002475 SH.601899 SH.600089 SH.601138
SZ.002716 SH.603993 SZ.002131`，全在 20 只 `SH.600xxx` 关注池之外，且本地 `bars` 表
**0 行**）时，策略既不买也不卖 = **结构性死锁**。

| 修订 | 不变量 | 锁定测试 |
|---|---|---|
| 顶层开关 `exit_outside_target` | `~/.dsh/trading-platform.json` **顶层**键（与 `watchlist`/`futu_channel` 同级）；**默认缺失=false**；非真布尔一律 `ValueError`，`plan_auto` fail-closed **软跳过当日计划 + warn**（作业契约「永不抛」）；**只在自动计划路径生效**，`plan-build` 不变 | `tests/test_exit_outside_target.py::ExitSwitchConfigTest`、`PlanAutoExitTest` |
| 收敛集合 | = 券商持仓 − 当日 `target` 的键；**硬守卫：`target` 为空一律不收敛** + warn「目标为空，未执行目标外清出」；无价/不可卖的标的进 `skipped` 且**不产单** | 同上（`test_empty_target_does_not_clear_anything` 等） |
| 无本地 K 线的价格回退 | 本地最近收盘优先 → 券商标记价（sim `cur_price` / live `nominal_price`，与 `workbench/python/positions.py` 同口径）；来源如实标注 `close`/`broker_mark`（**绝不伪装成本地收盘**）；两处都无 → `SYM(无价,无法清出)`；`cost_price`/`mv` **不作价格用** | 同上；`BrokerMarksSnapshotTest` |
| 可卖数量封顶 | sim `qty_avbl` / live `can_sell_qty`（设计初稿写的 `available` 作次级候选，**实测无此键**）→ 卖 `min(qty, available)`；`available=0` → `SYM(T+N不可卖)` | 同上；`BuildAndFreezeExitSymbolsTest` |
| 形状与返回体兼容 | `positions_equity_cash(..., with_marks=False)` 缺省仍是 `{symbol:{"qty":n}}`；`build_and_freeze(exit_symbols=None)` 缺省返回体**无** `exits` 键、diff 逐字不变 | 同上（逐字断言）+ 既有持仓形状断言 |

**真机只读复验（2026-09-18，不改 `~/.dsh`、不下单）**：真实配置**无**该键
（`exit_outside_target_enabled(platform_config(真实 home)) → False`，配置 md5 仍为 WP17 记录的
`776a71d4…`）；把真实 DB **副本** + 真实配置 + 真实券商只读通道按 WP17 同一重放参数
（`today="2026-09-17 16:20:00"`）跑 `plan_auto`，产出与 WP17 记录**逐字一致**：
`SH.600010 BUY 26100 @2.11`、warnings = 持仓 8≥5 + 现金封顶、**0 张 SELL**、
`converge={"enabled":false,…}`、无新标题告警。同一副本把开关打开 → 8 张 SELL
（`SH.600089 5200@17.91 … SZ.002716 7000@9.54`，**全部 `broker_mark`/`cur_price`**，
8 只本地 K 线均为 0 行）+ 原买入单，warn「计划预警：目标外持仓清出」——
证明缺口确实由此闭合（真实库未被写入：新标题告警 0 条、最新计划与 draft 单数不变）。

**诚实登记（未决/边界）**：

- **两步生效**：执行侧 `ctx` 是**静态持仓快照**（`execute.run` 不逐单重查），所以同一份
  计划里「卖出 8 只 + 买入新股」时买入仍被规则 6 拒——**预期行为，不得为绕过规则 6 伪造
  持仓数**；下个交易日快照回落后新计划才能建仓；
- live 通道的 `nominal_price`/`can_sell_qty` 口径来自 `workbench/python/positions.py` 的
  既有归一，**本机无 live 账户，未真机核对**（sim 侧已实测：行字段含 `qty_avbl`/`cur_price`/
  `mv`，且**无** `available` 键）；
- 是否真的开启开关属**交易决策**，未由本任务决定（默认关闭，账户不会自己清仓）。

### 8.5 遗留项（按现状接受，未实现）

| # | 遗留 | 现状与影响 |
|---|---|---|
| **R4** | **被拒载荷永久堵队首** | 领取侧校验失败的载荷（直改库塞自由文本）留在队首 pending，每次 `claim` 都重新拒绝并发一条 critical 告警——**无 quarantine 区、无告警去重**。刻意的 fail-closed：队列停在那等人处置；代价是告警会重复刷（数量=领取次数）。处置方式：人工清理该行或修正载荷后重试。 |
| **R5** | **定时器 `Mon..Fri` 与美股周五会话的时间错位** | `install/research-duty.timer` 按周一至周五本地时刻唤醒；美股周五交易日的研究任务在**北京时间周六 19:05** 入队，落在定时器窗口之外 → 顺延到下周一 19:20 消费（**不丢，只延迟**）。已记时刻，未记后果——即：周五美股任务的实际产出晚约两天。 |
| **R6** | **`store.py` 已 1175+ 行** | 单文件承载 PIT 行情 / plans-orders-OMS / 研究快照 / rules / research_tasks 五个域。建议下一轮按域拆分（接缝清晰：上述五域各有独立段落注释与函数簇），**本轮不动**以避免大规模移动与审查噪声。 |
| **R7** | **手工 `plan-build --mode live` 用离线假权益** | 与交易闸门同一保守口径（`({}, 1_000_000.0)`），WP4 起预置、代码内已披露。含义：手工 live 计划的名义上限按默认权益计算，小账户偏松——**实盘下单仍受闸门 8 规则与人工确认约束**，此处只是计划生成侧的估算口径。 |
| **R8** | **索引 §2.3 契约表漏记 planner 的两个 keyword** | `planner.build_and_freeze` 还有 `risk_config`/`managed` 两个 keyword（代码内有文档）。已补进计划索引 §2.3，避免后续任务按不完整签名调用。 |
| **R9** | **`research-duty.service` 的 `After=` 引用未交付单元；`pipeline._ALERT_STATUS` 标题映射无锁** | ①service 单元 `After=` 指向本仓库未交付的单元名——未安装该单元时该依赖项不生效（systemd 静默忽略），脚本自身探活兜底（探活失败非零退出并给指引）；②流程页告警→阶段映射靠标题字符串匹配，**没有锁定测试**：改告警标题会静默改变阶段归因，改动 `alerts.emit(title=...)` 时需人工核对 `pipeline._ALERT_STATUS`。 |
| **R11** | **`rt_quote` 曾一次返回 `ok:true, value:null`（可疑瞬态）** | 后端 E2E 取证：多标的 A 股、**服务刚重启**时出现一次；同载荷随后稳定返回 `ok:false -9`（无 A 股实时权限）。**未能稳定复现**，疑似推送缓存竞态下产生的瞬时 null 成功信封。按「不猜测性修复」原则**仅登记**：界面遇到 `value:null` 时按「无数据」渲染（既有 `?? "—"` 口径），不当作权限结论；若再现，先记下时间与载荷再排查 `futu_push` 的行情缓存写入路径。 |
| **R12** | **`option_screen` 最小合法载荷不可发现** | 服务端要求 `filter.field_filter` 为非空 dict；按提示给 `{"option_type": []}` 会被上游 `-3 invalid parameter` 拒绝，省略/空数组也被本地拒绝 → 契约对调用方不可发现。**待办**：确定一个真实可用的最小示例（需对官方文档 + 真机确认）后写进工具描述与本文档；在确定之前**不编造示例**。 |
| **R10** | **WP8 的 P1/P2 只在 WP8 计划文件里** | 现 surface 到本文档：**P1** 部分成交后撤单的对账永不收敛到终态（数量口径优先于状态文本）→ 需人工核对入口（记 diff 提示，不猜 `cancelled`）；**P2** A 股 live 订单不在推送对账覆盖内（`PUSH_RECONCILE_MARKETS=HK,US`）→ `unknown→终态` 兜底链对 A 股有缺口。两者均为**保守方向的已知缺口**（不误判、不自动改状态），实盘前按 P4 清单人工评估。 |

### 8.6 WP18：交易日历自动维护 + 交易日纵深防御（2026-09-18 实现 / 只读复验）

**背景（两条实测事实）**：

1. `calendar` 表是「交易日白名单」，`store.is_trading_day` 用白名单查；`store._require_calendar`
   只在**该市场零行**时抛错。因此**日历用尽**（日期超出 `max(day)`）时 `is_trading_day`
   返回 **False 而不报错** → 市场链被静默跳过（实测：日历只到 2026-09-30 时跑 2026-10-12，
   只有 2 条全局作业、0 条市场作业、0 条 warn，页面表现为「市场天天休市」）；
   而此前**没有任何作业会同步日历**——`daemon.JOBS_DEFAULT` 的 GLOBAL 链只有
   `enqueue_research`，同步只能人工跑 `trading_core calendar`（`--market` 单值、start/end 必传）。
2. `autopilot.auto_execute` 的九守卫（开关 / sim-only / kill / halt / 计划存在且
   mode+market 匹配 / 当日幂等 / 执行窗口 / 写指令）**不含交易日判定**——实测在法定假日
   2026-05-01 09:35 直调照样写出 `execute_plan` 指令，只有下游 `risk.pre_trade_checks`
   规则 3 逐单拒单兜底（线上调度器挡住市场链，故属**纵深防御**缺口）。

| 修订 | 不变量 | 锁定测试 |
|---|---|---|
| `calendar.calendar-sync` / `ensure_coverage` | 逐市场自节流：`max(day) ≥ today + 180` → **跳过、零网络调用**；否则幂等同步 `[today−30, today+400]`；单市场失败只记 `failed` 不中断其余市场；退出码与 `sync-bars` 同口径（0=全成功或部分成功、1=零成功）。取数可注入（离线可测） | `tests/test_wp18_calendar_sync.py` |
| GLOBAL 链新增 `sync_calendar`（18:50） | **基础链**作业（与交易开关解耦；GLOBAL 链不查市场日历 → 假日/周末/用尽都照常跑 = 自愈来源）。时刻早于对账 19:00 与入队 19:05：日历必须在当日对账/计划之前就位 | `tests/test_wp18_calendar_coverage.py::CalendarJobChainTest`、`tests/test_wp9_build_jobs.py`、`tests/test_wp15_enqueue.py` |
| 同一轮 tick 内 **GLOBAL 链先于市场链** | `daemon._chain_order` **显式定序**，不靠 dict 插入顺序（`JOBS_DEFAULT` 字面量顺序 + `deepcopy` 保序只是巧合）——否则「本轮刚补齐的日历」对本轮市场链不可见 | 同上（`test_global_chain_is_processed_before_market_chains`、`test_calendar_synced_this_tick_unblocks_market_chain_in_same_tick`） |
| 告警 `日历覆盖不足`（warn） | `max(day) < today + 60 天`（`daemon.CALENDAR_COVERAGE_WARN_DAYS`；比同步阈值 180 窄 3 倍）。**链照常跑** → 登记在 `pipeline._CHAIN_NOTICE_ALERT_TITLES`（**惰性**，不改阶段状态）。每市场每日至多一条（kv 去重） | 同上（`CoverageWarningTest`、`CalendarAlertTitleLockTest`） |
| 告警 `日历已用尽`（warn） | `today > max(day)`；与「覆盖不足」**互斥**（用尽时只发前者，避免同义双告警 + 负天数文案）。**真实休市保持静默**（在覆盖范围内但不在白名单——刻意的语义，`plan_auto` 同样不告警） | 同上（`CalendarExhaustedTest`、`test_real_holiday_stays_silent`） |
| `store.calendar_last_day` | 只读助手（唯一实现）：`tick` 不写裸 SQL；零行市场 → `None`（此时走既有「日历未同步」，不叠加覆盖告警） | 同上 |
| `auto_execute` 守卫 **4b：交易日** | 纵深防御（线上调度器已挡，CLI/MCP/E2E 直调没有）：`RuntimeError`（日历未同步）→ warn；False（真实休市）→ **info** 跳过 + 稳定标题 `非交易日` + **零指令**。编号用 4b 以免打乱规格 §4.3 已登记编号 | `tests/test_wp18_auto_execute_trading_day.py` |

**字面量契约（标题必须稳定，`pipeline` 按标题精确匹配；变量只进 detail）**：
`日历覆盖不足`、`日历已用尽`、`非交易日`。三者均已登记（前两者在链层表，`非交易日` 在
`_ALERT_STATUS` 的 `auto_execute` 行）并纳入 N3 标题锁（`tests/test_wp10_locks.py`）。

**只读复验（2026-09-18，不改 `~/.dsh`）**：真实库 `calendar` 三市场
SH 503 行 / HK 507 / US 511，边界全部 `2027-12-31`；`2026-10-12`（周一）在 SH 白名单内
= 交易日、`2026-05-01`（周五）不在 = 假日。当日（2026-09-18）距边界 469 天，
远大于同步阈值 180 与告警阈值 60 → `calendar-sync` 三市场全部 **skipped（零网络）**、
两条日历告警都不会发。**未**对真实 `~/.dsh` 写任何文件、未重启服务、未下单。

**诚实登记（未决/边界）**：

- 「覆盖不足」与「已用尽」按上文**互斥**实现（用尽时只发后者）。需求原文对 A3 的描述
  （`max(day) < today + 60`）在数学上**也覆盖**用尽态，本实现刻意不重复发——若后续要求
  「两条都发」，改 `_warn_calendar_shortage` 的 `date > last` 早退即可（有一条测试锁住当前口径）；
- `calendar-sync` 的「今天」取**北京日**（`date.today()`），日历按市场本地日期落库——
  对 SH/HK 无差异，美股链在极端时点可能差一天；影响被 30 天回看窗口吸收（不构成缺口）；
- 上游信封异常导致 0 行时 `ensure_coverage` 记 `days=0`、边界不变，**不**判失败
  （沿用 `sync_calendar` 既有口径）；可见性由覆盖告警兜底，不在同步层硬判；
- 真实库已同步到 2027-12-31 是**手工**完成的（本轮之前），本轮只是把它接进调度链；
  实机**未**触发过真实网络同步（`calendar-sync` 取数腿的端到端联网路径本轮未实机验证）。

### 8.7 WP19：交易时段事实源（半日市）+ 人工下单时段闸门（2026-09-18 实现 / 只读复验）

**背景（两条实测事实）**：

1. `calendar` 表有 `trade_second` 列（实测取值 SH=14400 / HK=19800 / US=23400，
   `trade_date_type` 一律 `WHOLE`），但它在**生产代码里从未被使用**——半日市/提前收盘
   因此没有建模。数据就绪门 `planner.data_date_for` 只认常量全天收盘
   `SESSION_CLOSE_BEIJING`（SH/HK 当日 15:00/16:00、US 北京次日 05:00），于是港股半日市
   （真实收盘本地 12:00）在北京 13:00 仍被判「本次会话尚未收盘」→ **当天不产出任何计划**；
   美股半日（13:00 ET）同理要等到北京次日 05:00。
2. 人工/Web 下单**没有任何钟点判定**：全仓唯一的钟点守卫是自动路径的
   `autopilot._within_exec_window`（守卫 9、执行窗口），而 `risk.pre_trade_checks` 的规则 3
   只查日粒度 `is_trading_day`——但它的文案写着「非交易日/**非连续竞价时段**」，超出了它
   实际检查的范围（调用方会以为盘中/收盘后的单在风控里挡得住）。

| 修订 | 不变量 / 口径 | 锁定测试 |
|---|---|---|
| 新增 `trading_core/sessions.py` | **时段唯一事实源**（两组共用）：时区一律 `zoneinfo`（SH/SZ/BJ=Asia/Shanghai、HK=Asia/Hong_Kong、US=America/New_York），不手算 DST | `tests/test_core_sessions.py` |
| **半日市收盘 = 开盘 + `trade_second`**（单段、不含午休） | 港股半日 9000s → 本地 **12:00**、美股半日 12600s → **13:00**；用「全天收盘 − 缺口秒数」会把港股算成 13:00（错）。**全天行仍走已知收盘表**（SH 15:00/HK 16:00/US 16:00），否则港股全天会被算成 15:00。半日市在两地实践里都是单段（只有上午）——这就是本算法成立的理由 | `test_core_sessions.HalfDayCloseTest`、`test_hk_half_day_is_single_segment_not_gap_subtraction` |
| `planner.data_date_for` 改为「**先解析会话日 → 再用该日真实收盘判是否已收盘**」 | 语义不变：未收盘 → `None`（调用方软跳过）、跨市场日期空间（US +1 天）不变、日历未同步 → `RuntimeError`。`trade_second` **缺失/全天 → 逐字沿用** `SESSION_CLOSE_BEIJING`（美股 EST 05:00 的偏保守 DST 口径**刻意不动**） | `tests/test_core_planner.py::HalfDayCloseTest`（含 `test_full_day_rows_keep_legacy_behaviour`、`test_missing_trade_second_keeps_legacy_behaviour`） |
| `store.calendar_row(conn, market, day)` | 单日只读助手（唯一实现）：planner 不写裸 SQL；该列为 NULL → 键存在值为 `None`（按全天回落，不编造）；无该行 → `None` | `test_core_store.test_calendar_row_reads_one_day` |
| **人工写入时段闸门**（平台层，与字段校验同层） | `trade_place`/`trade_modify` 在**风控之前**判时段：不在窗口 → `trading/order-rejected` + 「时段闸门拒绝：…」（含市场/当前市场本地时间/当日窗口/「平台前置校验，券商同样会拒」）；**零券商调用、不落 OMS/风控行、不消耗确认**。**撤单一律放行**（减少敞口不新增风险——与规则 7「熔断即撤余单」同口径）。排在既有本地前置拒绝（2.5 live 写能力 / 2.6 sim 能力边界）**之后**：不改写它们的错误码契约；排在幂等回放**之前**（闭市时连「重复提交回放」也不放行） | `tests/test_wp19_session_gate.py::OrderSessionGateTest` |
| 可委托窗口（取向「**宁可放过、不可错杀**」） | SH/SZ/BJ **09:15–15:00**（含开盘集合竞价与**午间报单**，午休刻意算在内——用「非连续竞价」当理由挡掉午间报单是错杀）；HK **09:00–16:10**（含开市前竞价与收市竞价）；US 常规（`RTH`/缺省）**09:30–16:00**，请求扩展时段（官方 `PLACE_SESSIONS` 里的 `RTH+Pre/Post-Mkt`/`OVERNIGHT`/`ALL_DAY`）→ **04:00–20:00** 并集；**半日缩短上界**（HK 半日 → 12:00 而非 16:10，US 半日 → 13:00），但**只在缩短日**取 `min`（全天日的 HK 16:10 收市竞价、US 盘前盘后必须保留） | 同上 `HalfDayGateTest`/`UsSessionGateTest`/`InWindowTest` |
| fail-closed 边界 | 未知市场前缀 / 未知 `session` 取值 → 记不允许 + 明确原因；窗口下界晚于上界（极端提前收盘）→ 「无可委托时段」而非负长度窗口；日历缺当日行 → 按**全天**窗口判时刻（不 fail-closed：非交易日由规则 3 日粒度兜底） | 同上 `SessionRefusalUnitTest`、`test_unknown_us_session_fails_closed` |
| `plan-execute`（**人工触发**）入队前判时段 | 工作台端点写指令文件**之前**判：计划涉及的市场**全部闭市**才拒（有一个开着就放行——为闭市市场连坐开市市场的订单属错杀）；撤销计划（`action=cancel`）不放闸门；计划/订单解析不到 → 放行（计划合法性由 `execute.run` 把关，本闸门只判时段，不做存在性校验） | `tests/test_wp19_session_gate.py::PlanExecuteGateTest` |
| **不覆盖自动执行链**（刻意） | `autopilot.auto_execute` → 指令轮询 → `daemon.handle_command` → `execute.run` 不经 `TradeGate`，其时段约束是**守卫 9 的执行窗口**；`TradeGate` 与 `plan_session_refusal` 的 docstring 写清两条路径分工，避免以后有人以为闸门覆盖了自动路径 | 同上（`test_all_markets_closed_is_refused_without_writing_a_command` 只覆盖人工端点） |
| 规则 3 文案纠正 | 由「非交易日/非连续竞价时段」→「**非交易日：不提交订单**」（它只查 `is_trading_day`）。**不给规则 3 加时钟逻辑**：`ctx` 里没有时钟/日历，且「8 条规则是唯一提交入口 + 编号不许改」是平台不变量——钟点判定属平台层前置闸门 | `test_wp19_session_gate.Rule3WordingTest` |

**字面量契约**：拒绝前缀 `时段闸门拒绝：`（`trading.SESSION_GATE_PREFIX`）、错误码
`trading/order-rejected`（`trading.SESSION_GATE_CODE`）。本轮**未新增告警标题**（闸门是同步
拒绝路径，不写 `alerts`），因此 `pipeline._ALERT_STATUS` / `_CONTENT_OUTCOMES` 与标题锁不变。

**夹具修正（属于本轮的连锁影响）**：`tests/test_core_planner.py` 与
`tests/test_wp9_plan_auto.py` 的日历种子原先给**所有市场**写 `trade_second=14400`——写进
美股日历就是一行「提前收盘 13:30 ET」的**假数据**，新口径如实读出来了（
`test_us_chain_before_session_close_skips` 因此红过）。夹具改为按市场写各自的全天秒数；
`tests/test_wp7_trading.py`/`test_wp8_trading.py`/`test_e2e_defect_fixes.py` 的闸门用例改为
**注入固定时钟**（否则用例会随真实运行时刻飘）。

**只读复验（2026-09-18，不改 `~/.dsh`、不重启服务、不下单）**：见 `docs/RUNBOOK.md`
「人工下单时段闸门（WP19）」；本轮以真实日历行（`calendar` 表）与 `sessions` 逐市场算
当日窗口与「当前是否在窗口内」，结果记在交付回报里。

**诚实登记（未决/边界）**：

- `session` 字段在 **sim 模式**下会被既有的 sim 能力边界先拒（`sim` 只支持限价当日单），
  因此「美股盘前/盘后窗口」实际只在 **live** 通道可达；这是既有契约，本轮未改；
- US 的 `OVERNIGHT` / `ALL_DAY` 在券商侧有独立夜盘时刻表，本仓库**没有权威来源**：按
  「宁可放过」取扩展时段并集 04:00–20:00，**不猜**一张可能把真实夜盘挡掉的窄表（如实登记
  为近似口径，不是精确时段表）；
- 半日**缩短窗口只在缩短日取 `min(全天窗口上界, 真实收盘)`**：需求文字给的是无条件
  `min`，但无条件取 min 会把港股全天日的 16:00–16:10 收市竞价与美股 16:00–20:00 盘后
  挡掉（与「宁可放过」相反），故按缩短日实现并在此登记该读法；
- `plan-execute` 的人工闸门判「计划涉及市场**全部**闭市」而非逐单：`execute.run` 是整份
  计划一次执行，逐单拦截会改执行链语义（本轮明确不动执行链）；
- 撤单**不判时段**也不判交易日（既有语义：规则 3 对撤单同样生效——`default_ctx_builder`
  的撤单分支只中和敞口字段，`is_trading_day` 仍生效）；本轮未改动该口径。

### 8.8 WP20：全新安装演练暴露的三个真实缺陷（2026-09-18 实测 / 已修）

**背景（三条实测事实，均由 2026-09-18 的全新安装演练暴露）**：

1. **`quality` 作业必然失败**：`daemon.JOBS_DEFAULT["SH"]` 只给 `["quality","--market","SH"]`，
   而 CLI 的 `quality` **必填** `--symbols/--start/--end` → 实测
   `trading_core quality: error: the following arguments are required: --symbols, --start, --end`，
   退出码 2，告警「作业失败 job=quality market=SH exit=2」。
2. **`@latest-quarter` 从未实现**：作业把**字面量** `"@latest-quarter"` 当报告期传给
   `merge_announcements` → `akshare.stock_yjbb_em(date="@latest-quarter")` 内部对
   `data_json["result"]["pages"]`（None）取下标 → `TypeError: 'NoneType' object is not
   subscriptable`。全仓 `resolve_command` 只实现了 `@watchlist`——**我们自己的占位符漏实现，
   却表现为看不懂的上游故障**。
3. **`yfinance` 没进任何安装步骤**：它是数据层生产依赖（`fundamentals` 的 Yahoo 财报备用源
   `SOURCE_YAHOO` + `market.fetch_yahoo` 港美股长历史通道，富途单次只有 370 根），而
   `install.sh`/`install.ps1` 只 `pip install akshare playwright`、`platform/requirements.txt`
   里也没有。全新 venv 上 Python 套件报 `ModuleNotFoundError: No module named 'yfinance'`。

**失败可见性的时间线（本轮最重要的证据）**：旧库 `daemon:state` 里
`SH:quality:2026-09-16` / `2026-09-17` **都有 ran 标记**（说明它跑过），但旧库 152 条告警里
**一条「作业失败」都没有**——因为失败可见性是 **2026-09-17 19:57 的 `ce2b47b`** 才加的，
而两次失败分别发生在 16:10 与 19:11，**都在修复之前**，所以当时是**静默失败**。
即：**缺陷一直都在，只是旧库的告警面看不见它**——这类缺陷只能靠「全新环境 + 看退出码」发现。

| 修订 | 不变量 / 口径 | 锁定测试 |
|---|---|---|
| `daemon.resolve_command(cmd, home, now=None)` 实现 `@latest-quarter` | = **今天之前最近一个已结束季度末**，格式 `YYYYMMDD`（2026-09-18 → `20260630`）。季度末 = 03-31/06-30/09-30/12-31；判据**严格早于今天**——正好落在季度末当天（09-30）取**上一季**，因为业绩报表只认已结束报告期，当日那份还没出 | `tests/test_wp20_install_drill.py::LatestQuarterPlaceholderTest`（13 个边界日） |
| 新增 `@today` / `@today-<N>d` | 当天 / N 个自然日前，格式 `YYYY-MM-DD`（CLI 的 `--start/--end` 口径）；**时钟口径不另立一套**：`now=` 注入 > `DSH_FAKE_NOW` > 真实时间，日期取 `stamp[:10]`（与 `tick` 逐字一致）。`now` 与 `tick(now=…)` 同形（零参 callable 或完整时间戳）；**无日期占位符时不读时钟**，既有两参调用逐字不变 | `TodayPlaceholderTest`、`LegacyCallContractTest` |
| **未知占位符显式报错** | 任何以 `@` 开头但不是已知占位符的参数 → `ValueError("未知占位符：…（已知：@watchlist/@latest-quarter/@today/@today-Nd）")`，经 `_run_job` 转成 **`作业异常`** 告警（可见）。判定口径 = 「参数以 `@` 开头」，值里的 `@`（如 `a@b.com`）不受影响 | `UnknownPlaceholderTest`（含 `test_job_runner_turns_it_into_a_visible_alert`） |
| 修 `JOBS_DEFAULT["SH"]` 的 quality | `["quality","--market","SH","--symbols","@watchlist","--start","@today-180d","--end","@today"]`。窗口 180 自然日的理由（写进 `daemon.py` 注释）：`quality.gap_report` 的 start/end 是**日历区间**（`store.trading_days(market,start,end)`），内部**没有**回看常量；180 自然日 ≈123 交易日 ≥ `rule_engine.IC_WINDOW_DAYS`=120（运行时 IC 加权窗口，§9.3）。`--market` 只决定按哪张日历取交易日、**不筛标的** | `JobDefinitionTest`（含**全作业表**的 CLI 可解析不变量 + 按作业定义真跑一次 `cli.main`） |
| **只有 SH 链挂 quality**（刻意） | `announced_at` 覆盖率是 A 股 PIT 缺口①的口径（只由本链 16:05 的 `merge_announcements` 从东财 yjbb 补）；港美股 fundamentals 走 `futu/statements`，本就没有公告日——挂上去只会每天报一条结构性恒为 0 的覆盖率。本轮**不凭感觉扩链**；给 HK/US 加之前还需确认各自日历已同步 | `JobDefinitionTest.test_quality_is_declared_for_sh_only` |
| `sync.merge_announcements_akshare` 取数异常转可读 | 包住 `stock_yjbb_em` 调用 → `RuntimeError("上游 akshare/eastmoney 接口异常（stock_yjbb_em date=…）：<原始类型: 摘要>")`，原始异常挂 `__cause__`。**不软跳过**：`announced_at` 是 PIT 关键字段（§13.4 缺口①），缺数据必须可见（宁缺毋假，§4.2 规则 3）；`df is None/空` 的软返回 `{"matched":0,"rows":0}` 保持不变 | `MergeAnnouncementsFailureTest` |
| `merge-announcements` CLI 给**失败信封** | `{"ok": false, "error": "公告合并失败：…"}` + 退出 1（与 `sync-bars` 的 F-b 同一手法）。理由：失败告警的 detail **只引用 stdout 尾部**，裸 traceback 走 stderr → 告警只剩 `exit=1`、没有原因 | `test_cli_failure_envelope_names_the_upstream`（含 runner tail/summary → `作业失败` detail 的整链断言） |
| `yfinance` 显式声明 | `install.sh` / `install.ps1` 的数据渠道依赖行加 `yfinance`（注释写明 Yahoo 财报备用源 + 港美股长历史通道，以及「缺它不报错、只静默降级」）；`platform/requirements.txt` 加 `yfinance==1.7.0`（本机已装版本精确锁定，注释说明它属**数据层**、放这里是为只跑 `install_platform.py` 的人）；README 的安装说明、平台一次性依赖与「数据渠道优先级」表同步 | `DependencyDeclarationTest`（脚本/requirements/README 三处） |

**本轮未修 / 未决（如实登记）**：

- **失败告警的 detail 仍只引用 stdout**（`_subprocess_runner` 的 `tail` 只取 stdout 行）：
  本轮只给 `merge-announcements` 补了失败信封；**其它作业**若抛未捕获异常（如 quality 遇
  日历未同步的 `RuntimeError`），告警仍只有 `exit=1`、没有原因。彻底解法是让 `tail`
  在 stdout 为空时回落 stderr，属跨作业的调度层改动，**本轮明确未动**（避免改动
  `_subprocess_runner` 的既有返回契约）。
- **`@latest-quarter` 的「当日未出报」是日历判据，不是披露日判据**：口径只看季度末是否已过，
  不看该公司是否已披露；作业仍会把当期表拉下来（上游没有的期次返回空表 → 软返回 0 行）。
- **HK/US 没有 quality 作业**（理由见上表）；`quality` 的缺口检查对它们同样成立，
  但需要先确认各自日历已同步，登记为遗留。


