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
  - **批准痕迹在哪**：`rules.approved_by`/`approved_at`（Web 端点写 `web`，CLI 可传
    `--by`）。`rules.validation` 存最近一次验证报告摘要（IC 均值/t 统计/分层单调/半衰期/
    换手率）；复核对齐一律跑 `rules-validate`（与验证门同一实现，不手写第二份阈值口径）。
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
