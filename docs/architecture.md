# 架构与交互边界

> **状态标注（WP6 修订，2026-09-15）：** 本文涉及 daemon、`plan`/`plan-execute`/`schedule`/`reconcile`
> 四个 RPC 的描述按 WP4 计划规格撰写，**WP4/WP5 合并后已实测**（回归见 `tests/workbench-wp4.test.mjs`、
> `tests/test_core_wp4_e2e.py`、`tests/test_core_wp4_snapshots.py`）。WP6 起工作台前端由
> `platform/` **独立服务进程（FastAPI 单进程）**托管，Harness 内 legacy 面板过渡期并存；
> 验收记录见 `docs/superpowers/plans/2026-09-15-wp6-standalone-service.md` 末节。

## 结论性字段的中文标签

结论（信号、买卖方向、评级、持仓状态、数据源状态、回测口径）在产出侧一律附
`*_label` 中文标签，**机器码保留不动**（台账已落盘、逻辑在比较、旧记录要能读）。

唯一事实来源是 `plugins/datasource/python/trading_datasource/labels.py`；
`plugins/workbench/src/labels.js` 是 Host 侧镜像，`src/client.js` 里的 `ZH`
是浏览器侧回退表（client 是单文件、无法 import）。三份表由
`tests/test_labels.py` 解析比对，漂移即失败。

回退表存在的唯一理由是**渲染历史记录**：旧台账/旧预览/旧研报里只有 `BUY`、
`buy`、`open`、`Buy` 这类英文码，界面必须能翻成中文。

## 产品定位

本项目是 **DeepSeek Harness 的对话模式与插件组合**，不是独立交易终端。
Harness 是唯一 AI 对话、分析请求和交易指令入口。工作台自 WP6 起是
**独立 Web（FastAPI 单进程托管）+ Harness 内 legacy 面板过渡期并存**：
独立 Web 只用于研报结果、交易动态、量化信息预览与模拟盘/实盘模式切换；
Harness 内面板仍是 `tradingWorkbench` 服务的进程内锚（engine 账户策略依赖它），
面板移除另行提交。

```text
用户 ↔ Harness 对话 / 原生审批
          │
          ├─ trading-agents skill + Harness 子代理
          │    └─ 富途 MCP / fin_news / fin_sentiment / web 取数
          │
          ├─ engine：启动研究 → 发布研报；量化预览；账户工具守卫
          │    └─ Python 量化计算（不替代券商成交）
          │
          └─ 原生富途账户/交易工具 → 观察最终响应
                    │
                    ▼
          workbench Host：持久结果与模式（Harness 进程内）
                    │ Harness Connection RPC（legacy 面板，过渡期）
                    ▼
          workbench Client：结果卡片 + 展示面板 + 模式切换（legacy 通道，过渡期）
                            模式切换的正式入口是独立 Web 页头徽章（见下）

          独立服务进程（platform/，FastAPI + uvicorn 单进程）
            ├─ POST /api/wb/<endpoint>（envelope 契约）→ Ant Design Pro 前端
            ├─ /mcp（mcp SDK streamable-http，25 工具）→ Harness 的 mcp__quantwb__*
            └─ 静态托管 platform/web/dist；同一份 store/模式/指令文件
               （双进程无网络互通，靠原子写 + 文件锁互斥）
```

**明确不做**：第二个聊天窗口、浏览器直接持有富途 token、
插件私建 LLM 对话循环、工作台 RPC 任意执行工具或 shell。
独立 Web **无聊天、无逐单下单/撤单表单、无 shell/LLM/token 接口**。
工作台的下单边界已从「无任何下单入口」变更为「**恰好一个受约束执行入口——
执行已冻结计划**」：唯一写路径是指令文件（白名单 5 种指令），live 需口令复核，
对话侧保留等价 `plan_execute`，两条入口汇聚同一核心函数、同一套风控；
逐单下单/撤单表单仍然不做。

**双进程数据约定（WP6）**：服务进程与 Harness 进程共享同一份
`<DSH_HOME>/trading-workbench.json`、模式文件 `trading-account-mode` 与指令目录
`trading-commands/`；互斥依赖既有的**原子写 + 独占锁**，两处同时切换模式的竞态由
`expected_mode` 复核兜底（后到者拒绝）。Python 侧快照读**不合并** pending observations
（合并留给 Harness Host），见 `docs/HANDOVER.md`。

**MCP 通道分级（WP6）**：`mcp__quantwb__switch_mode` **只接受切到 sim
（live→sim 回模拟盘）；sim→live 一律拒绝**（`trading/live-switch-web-only`）——
实盘切换只能由用户在独立 Web 的模式切换入口（页头 SIM/LIVE 徽章 →「账户模式」对话框）
输入口令「确认实盘」完成：`/api/wb/switch-mode` 保留口令流程，请求带 `expected_mode`，
成功后显示 `order_authorized: false`；与 `quant_switch`「模型不能代替用户确认实盘」
同一条不变量。

## 组件职责

| 组件 | 责任 |
|---|---|
| 根目录 preset | 新建「交易智囊模式」，组合 persona、原生工具、skill 和 MCP |
| `skills/trading-agents/SKILL.md` | Harness 主会话与子代理执行 12 角色、6 阶段研究，数据不足显式说明 |
| `plugins/engine/src/tools.js` | `run_trading_analysis` 启动记录、`research_publish` 发布有来源的报告、量化结果保存 |
| `plugins/engine/src/policy.js` | 拒绝跨模式账户工具；真实写操作走**工作台业务确认**（不返回 `ask`，与权限档位无关）；记录最终工具响应 |
| `plugins/workbench/src/index.js` | 根级 Host 插件，提供 `tradingWorkbench` 服务与认证后的 Connection RPC |
| `plugins/workbench/src/store.js` | JSON 持久结果、原子替换、写锁、账户调用租约、模式隔离 |
| `plugins/workbench/src/client.js` | Harness 原生 module factory，使用宿主 React；`shell.overlay` 面板及 `tool.call.toolview` 卡片 |
| `plugins/engine/python` | 量化计算与本地模拟台账的权威实现 |
| `plugins/datasource/python` | **统一数据层**：唯一的富途 MCP 客户端、行情路由与回测核心，被 engine/workbench 共同依赖（不是 Harness 插件） |
| `plugins/core/python/trading_core` | **量化平台核心库**（非 Harness 插件）：PIT 存储/日历/同步/质量（WP1）、因子/策略/组合回测/walk-forward（WP2）、风控八规则/计划冻结/OMS 状态机/券商适配/对账/TCA（WP3）、daemon 调度/指令目录/告警（WP4） |
| `trading_core` daemon | 无 LLM 单进程守护进程（`python -m trading_core daemon`）：按交易日历触发作业链（sync→质量→信号→计划、对账→TCA→摘要）、心跳落 `~/.dsh/trading-daemon.json`（> 5 分钟未刷新工作台标红）、轮询指令目录 `~/.dsh/trading-commands/`、告警分级落 `alerts` 表（WP4） |
| `platform/` 独立服务进程（WP6） | FastAPI/uvicorn **单进程**：`POST /api/wb/<endpoint>`（envelope 契约，20 端点）+ `/mcp`（mcp SDK streamable-http，**25 工具**）+ `<DSH_HOME>/trading-workbench.json` store 访问层（只读快照/模式切换/5 个 `admin_*` 维护动作）+ `platform/web/dist` 静态托管（`GET /`，SPA fallback）；数据路径复用 `plugins/workbench/python/*` 脚本、`trading_core snapshot-*` CLI 与指令目录协议，HTTP 与 MCP 同一批处理函数（同源，规格 §3.1） |

`plugins/trading-agents` 是旧的未启用脚手架，不是当前执行引擎。
workbench 包通过 `dsh.bundle.patch` 插入根级 Host 行；fin-data/engine 是普通插件包，
安装器只启用它们对应的 preset 行，不将账户策略重复装进全局与会话。

## 研究与量化数据流

`run_trading_analysis(ticker)` 仅建立研究记录，返回 `id`、模式、历史报告及后续步骤。
它不直接调用 LLM，也不声称已经完成报告。Harness 随后加载 skill、使用原生工具取数，
通过主会话/子代理研究与辩论，并调用：

```text
research_publish(
  run_id, ticker, rating, report,
  sources=[{name, as_of, reference}]
)
```

Host 校验会话归属、标的、模式、五档评级及来源。报告不会通过正则猜评级，
也不以默认 Hold 掩盖格式错误。工作台保留完整文本和来源，旧工作区
`.tradingagents/memory.md` 继续用于 Harness 复盘；旧研报不自动等同于已验证教训。

`quant_signal`、`quant_backtest`、`quant_report` 仅由 Harness 指令触发。
结果保存到量化预览；工作台刷新只读取缓存，不自动重跑回测、不调 LLM、不下单。
本地模拟台账与富途模拟账户、真实账户必须明确区分。

### 统一数据层（研报与量化共用）

研报路径与量化路径的**数据来源**本来就是同一套优先级（富途 MCP 优先，其余降级），
但重构前**代码路径并不统一**：行情路由有 2 份实现、富途 MCP 客户端有 4 份、回测核心有 2 份。
现在这些都收敛到 `plugins/datasource`：

```text
研报路径：Harness 会话 → mcp__futu__* 原生工具 ─┐
                                            ├─→ 同一个富途远程 MCP 服务端
量化路径：engine/workbench → trading_datasource.futu_mcp ─┘
```

- 行情（`market.load_bars`）与回测（`backtest`）在 engine 与 workbench 之间**只有一份实现**；
- 新闻/情绪仍由 `fin-data` 独家提供，量化侧通过 `locate` 复用它，
  因此两个路径的 `sources_status` 完全可比（如 `x: ok:api/graphql`）；
- 量化侧的情绪是**可选并列输入**（`quant_signal(include_sentiment=true)`），
  **不参与信号计算**——信号必须能由历史数据复现，否则回测结论不可验证；
- 安装器把该包解到 `$DSH_HOME/trading-python/` 并写入 venv 的 `.pth`，
  因此安装后无需 `PYTHONPATH`。

渠道不可用时一律明确报「不可用」：`load_bars` 抛错、情绪返回 `available: false`、
`locate` 返回 `None`。工作台因此宁可为空，也不显示占位数据。

## 交易动态与实时性

账户工具的 `tools/result` 事件记录为 `broker_response`，包含模式、时间、
工具名、会话和结果/错误。敏感认证字段脱敏，超大响应不完整复制。
面板打开时通过 Connection RPC 拉取快照（默认 60 秒兜底轮询），关闭后停止轮询。

### 本地缓存与"不追求实时"

面板是查看用途，不需要实时取数。这些接口背后是 python 子进程与富途调用，
每次切页签都重跑既慢又浪费额度，因此有两层缓存：

| 层 | 位置 | 作用 |
|---|---|---|
| 客户端内存缓存 | `client.js`（按接口 TTL） | 切页签、重开面板不再重复请求 |
| Host TTL 缓存 | `rpc.js` 的 `CACHE_TTL_MS`（按接口粒度，1–30 分钟） | 即使客户端重新请求也不会重跑取数 |

`_refresh: true` 是显式旁路标记（不参与各接口的字段校验）；面板的「刷新」按钮会清空
客户端缓存，并让随后 5 秒内的请求穿透 Host 缓存。切换模拟/实盘会清缓存，
避免展示上一个模式的数据。界面上明确写出"数据按 TTL 本地缓存，不追求实时行情"。

### 接口自检：让"进程陈旧"可诊断

Client 半边每次请求都从磁盘读取，而 Host 半边只在进程启动时加载一次。
插件升级后若没重启，客户端会调用一批 Host 根本没有的路由，表现为满屏
`transport failure ... HTTP 404`，完全看不出原因。现在：

- Host 在 `snapshot` 响应里**声明**它实际提供的接口（`endpoints`，与路由注册同源）；
- 客户端据此**不发起**已知缺失的请求，直接给出可读原因；
- 对不声明清单的旧 Host，客户端从 404 反推并记住，同样停止重试；
- 面板顶部显示缺失接口清单与修复动作（重启 `dsh web`）。

业务失败（如台账不可读）不会被误判成路由缺失。点击「刷新」会清掉缺失记忆，
让重启后的 Host 有机会恢复。

这里的“动态”是 **Harness 已观察到的工具响应**，不是已建立的券商成交推送。
`SUBMITTED`、超时、拒单不能转换成“已成交”。没有账户响应时显示未知，
不生成 100 万初始资产来冒充实盘账户。最新持仓和订单仍需在 Harness 查询富途。

### 交易概要（由工具响应归纳，不是工具调用清单）

原始 `activity` 记录的是**工具调用**，直接铺开有个明显问题：同一个工具被调用 7 次
就有 7 条重复行，读起来像发生了很多笔交易，而真正的事实埋在 JSON 里。
例如一次实测的 23 条响应，展开是 7 条重复的 `sim_trade_history_order_list`
和 10 次只读查询，归纳后才是 **12 笔去重订单 + 6 个下单/改单/撤单动作**。

`src/broker_trades.js` 负责归纳，派生视图放在 `snapshot.trade_summary`（读时计算，不写盘）。
它的原则是**只归纳、不推测**：

| 事实 | 来源 |
|---|---|
| 订单字段（标的/名称/数量/委托价/成交价/成交数量） | 券商返回原文，不重算、不补默认值 |
| 方向「买入/卖出」 | 工具 schema 明文：`order_side: 1=Buy 2=Sell` |
| 成交情况「全部/部分/未成交」 | 由 `cum_qty` 与 `qty` 推导（可自证） |
| 原始 `status` 码 | 原样附上。**服务端未在响应或 schema 中给出该枚举的含义，因此不猜标签** |
| 「已撤单」「改单 N 次」 | 我们自己记录到的、券商返回 success 的动作，按 `order_id` 关联 |
| 只读查询 | 只计数并如实列出工具名与次数，不逐条铺开，也不隐藏 |

同一订单在多次查询中出现时按 `order_id` 去重并**保留最后一次观测**（状态会演进）。
无法解析的时间戳保持 `null`，不用当前时间顶替；缺少 `order_id` 的行直接丢弃，
不生成占位 id。原始响应仍保留在可展开的「审计核对」区域，证据不丢。

### K 线图（按需 + 缓存）

行情页在富途标的卡之外提供 K 线蜡烛图，周期预设默认**日线**，可切 60m/15m/5m/1m。

实测过四种周期 × 两个市场，结论是**开销与周期无关**：

| 周期 | 300 根耗时 | JSON 大小 | 覆盖时长 |
|---|---|---|---|
| 1m | 3.5–5.1 s | 28.9 KB | 5 小时（A 股一天 4 小时）|
| 5m | 2.8 s | 29.0 KB | 25 小时 |
| 15m | 3.3–3.9 s | 29.2 KB | 75 小时 |
| 1d | 3.0–4.6 s | 29.0 KB | 1.2 年 |

300 根就是 300 根——payload 与往返成本基本相同，成本变量是**调用频率**而非周期。
因此分钟级并不比日线贵；配合两层缓存（客户端与 Host 各 5 分钟），切页签为 0 次调用。

根数上限受富途单次 370 根约束（预设 200–250）。图表仅在标的解析后挂载，
未解析时不发请求；canvas 自绘，不引入依赖。

**悬停查看**：鼠标移到某根 K 线上显示竖准星、右侧价格标签与信息框
（日期、开/高/低/收、相对前一根的涨跌幅、成交量），移出即清除。两处实现要点：

- 命中判定与绘制**共用同一套几何量**（绘制时把 `padL/plotW/count` 记进 ref），
  两处各写一份迟早会错位；
- 只在**跨到另一根 K 线**时才 `setState`，不是每次 `mousemove` 都重绘。

可测的纯逻辑抽成了函数并有断言：`barIndexAt`（绘图区左右留白必须返回 null，
不能误命中首尾）、`tooltipLeft`（贴近右边缘时翻到左侧，窄画布下不越界）、
`compactNumber`（成交量紧凑写法；缺失显示 `—` 而不是 `0`）。

### 券商持仓（真实数据 + 缓存）

「组合」页显示的是**券商真实持仓**，不是本地台账。两种模式走不同账户体系，字段名也不同：

| 模式 | 账户来源 | 持仓接口 |
|---|---|---|
| 模拟盘 | `sim_trade_account_list`（每个市场一个账户） | `sim_trade_position_list(acc_id, market)` —— **缺 `market` 会报 `ret=-5`** |
| 实盘 | `account_authorized_trd_accs` | `account_positions(acc_id)` |

诚实性约束：

- **不跨账户/币种合并**。模拟盘响应里根本没有币种字段，且每个模拟账户本身就是单一市场，
  因此只做「按账户小计」；实盘响应自带 `currency`，才按币种小计。
  页面上不会出现把港币和美元加在一起的"总市值"。
- 单个账户读取失败**不掩盖**：计入 `errors` 并在界面上列出，同时继续展示其他账户。
- 缓存分三层：客户端内存 → Host RPC（2 分钟）→ python 磁盘缓存（5 分钟）。
  命中时界面标注 `as_of` 与"本地缓存"；实时读取失败时展示上次缓存并标 `stale`，
  而不是显示空白或编造数字。
- 真实账户在界面上只显示末四位，不泄露完整账户号。

并发：串行读 9 个模拟账户约 36 秒（每次往返约 3.5 秒）。共享 MCP 客户端已做线程安全
（会话建立加锁），持仓批量读取用线程池并发，降到约 8 秒；缓存命中约 0.3 秒。

## 账户模式与执行边界

- `DSH_HOME/trading-account-mode` 是模式真源，缺失时 sim，非法内容报错。
- 模式持久化，**重启不会自动切回 sim**。
- 用户在工作台输入「确认实盘」才能切 live：独立 Web 的入口是页头 SIM/LIVE 徽章
  （点击展开「账户模式」对话框，sim→live 显示口令输入框，`services/mode.js` 构造
  `{mode, expected_mode, confirmation}`）；请求包含 `expected_mode`，
  防止基于旧页面误切换。切换不授权任何订单（成功提示固定带 `order_authorized: false`）。
- `quant_switch` 只能切 sim；`scripts/trade_mode.py` 可查询或离线恢复 sim，
  不再通过脚本启用 live。
- 账户调用持有 `trading-call-*.active` 租约，期间拒绝切换；
  多个 Host 进程和恢复脚本共用文件边界。
- `mcp__futu__sim_trade_*` 仅 sim；`account_*`、`trading_*` 仅 live；
  真实写操作在会话摘要确认之外，还必须在工作台逐笔点确认（业务确认，独立于 Harness 权限审批）。

这是插件管理的 MCP 工具边界，不是操作系统沙箱。拥有宿主 shell、文件权限或券商凭据
的人仍能绕过插件，因此禁止模型绕路，日常只授只读权限，不能宣称已经具备完整实盘安全保证。

## Host / Client 协议与存储

工作台通过原生 `ctx.connection.fetch.register` 注册精确 POST 路由，
Client 使用 `ctx.connection.rpc.call("/api", "trading-workbench/...", ...)`，
采用 Harness 的请求/响应 envelope，继承 Connection 的信任、认证和生命周期。
不占用 Gateway 的共享 interceptor。

**WP6 起同一批端点在 `platform/` 独立服务进程内以 `POST /api/wb/<endpoint>` 暴露**
（envelope 契约不变：`{ok, value?, cached?, cached_at?, error?{code,message,details}}`）；
`/mcp` 的 25 个 MCP 工具与 HTTP 路由在该进程内调用**同一批 Python 处理函数**
（同一 `(endpoint, payload) -> envelope`），行为对等由代码结构 + 审批回归矩阵共同保障。
`snapshot` / `switch-mode` 之外，WP4 新增 4 个受约束端点
（读侧一律经只读子命令取数，Node 侧经 `pycore`、Python 侧经 `trading_core snapshot-*`
读 SQLite，不直接改库；写侧只落指令目录，不直接操作业务状态）：

| Endpoint | 输入 | 输出 |
|---|---|---|
| `snapshot` | `{}` | 当前 mode、研报、研究记录、量化预览、交易响应、快照时间 |
| `switch-mode` | `{mode, expected_mode, confirmation?}` | 实际 mode 与 `order_authorized: false` |
| `plan` | `{}` | 当前/历史计划：目标 vs 实际 diff、逐单风控报告、状态时间线 |
| `plan-execute` | `{plan_hash, expected_mode, confirmation?}` | live 需口令「确认执行」；服务端复核冻结状态/hash/mode；成功只返回 `{queued, nonce}`，状态经 `plan` 轮询 |
| `schedule` | `{}` | daemon 心跳、作业历史、下次运行 |
| `reconcile` | `{}` | 最近对账差异、TCA 摘要、告警列表 |

除 `plan-execute`（执行已冻结计划，白名单指令落盘）外，不开放下单、shell、LLM 或
token 读取接口。所有页面内容按文本呈现，不执行研报中的 HTML。

**端点声明自检（前端）**：`snapshot` 响应带 `endpoints` 数组（服务端 `store_access.endpoints()`，
恰 20 项），前端 `services/endpoints.js` 用纯函数比对本次要调的端点；未声明即
「服务未提供 X（服务版本陈旧，请重启服务后刷新）」并**不发起请求**
（`services/api.js` 接线；服务端 404 分支保留作兜底；声明集合取不到时一律放行，
不把旧服务拦死）。

**独立 Web 的安全边界（诚实清单，规格 §4.7）**：脱离 Harness Connection 后，
认证边界变为 **loopback 绑定（默认 127.0.0.1:8397）+ 可选静态 Bearer token**
（`~/.dsh/trading-platform.json` 的 `service.token` 非空时 `/api/*` 与 `/mcp` 均要求
`Authorization`，`/healthz` 与静态前端豁免）。相对 Connection 这是明示的能力降级；
loopback 不防同机其他进程，token 是可选加固而非强认证。浏览器不持有任何券商凭据，
服务进程只能读写既有数据文件（store JSON、模式文件、指令目录、SQLite 只读快照），
`switch-mode`/`plan-execute` 的口令校验在服务端执行，浏览器绕不过。
敏感字段沿用 `store.sanitize` 脱敏（token/secret/password/authorization/cookie →
`[redacted]`，64KB 截断），HTTP 与 MCP 输出同源。

默认数据根目录 `~/.dsh`，可用 `DSH_HOME` 覆盖。`trading-workbench.json` 保存版本化快照，
各列表最多保留最近 100 项；这不是完整审计档案，完整过程仍在 Harness 会话记录中。
写入采用独占锁和同目录原子替换，读取失败不能退回伪造的空成功状态。
已完成账户响应先写入独立持久暂存文件，写锁冲突时保留，后续快照读取/重启再合并；
面板显示等待合并数量。这里只重试保存响应，绝不重试券商操作。

异常退出可能留下写锁或调用租约。恢复时先停掉相关 Harness 进程、核对券商订单状态，
再检查锁/租约中的 PID 和时间，仅清理确认无持有者的具体文件；不要自动过期删除未知在途订单的租约。

## 兼容性与后续准入

工具依赖钉在 `@deepseek-ai/dsh-tools@0.1.2-rc.1`。Host/Client API 对照官方源码
`c291e7961a515f6d7af9304e7fd1d257929aef26`：
Connection RPC、工具守卫/结果事件、客户端 `./client` 导出和 module factory。
客户端已经是可分发的原生 factory，无需用户重新构建 Harness Web。

优先级：安装/宿主集成验收 → 数据源与量化正确性 → 富途模拟盘成交/对账 →
告警、审计、熔断及恢复 → 样本外策略评估 → 人工决定是否小额实盘。
券商实时推送、港美历史数据与多市场交易规则仍需后续实现；本地对账/调度/熔断告警
由 WP3–WP4 交付（已实测，恢复演练见 [RUNBOOK.md](RUNBOOK.md)）；
面板可用不代表已达到实盘准入条件。
