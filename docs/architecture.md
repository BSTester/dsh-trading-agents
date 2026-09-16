# 架构与交互边界

> **状态标注（WP7 修订，2026-09-16）：** WP6 的独立服务（FastAPI 单进程）升级为**独立量化
> 平台**：服务内调度器（吸收 daemon 常驻循环）、因子快照定时收集与 `factors-history`、
> 交易闸门 + 受约束交易工具（工具面 27→33）、富途实时数据直通（33→41：资金流/衍生品/实时报价）、Harness 富途写通道收窄（policy 拒绝并指引）、
> 一键安装（`install/HARNESS_SETUP.md`）。规格/验收见
> `docs/superpowers/specs/2026-09-16-wp7-standalone-platform.md` 与
> `docs/superpowers/plans/2026-09-16-wp7-standalone-platform.md` 末节验收记录。
> **WP8（2026-09-16）：富途 OpenAPI 统一接入**——工作台权威富途通道升级为 OpenAPI
> （REST+WS，OAuth2.1+PKCE / AppKey 签名双认证）：交易链路（place/modify/cancel/
> order-confirm）、WS 推送（行情+交易事件）与全量行情端点统一到同一客户端；
> quantwb 工具面 41→**59**（54 端点工具 + 5 维护）、服务端点 **55**。规格/验收见
> `docs/superpowers/specs/2026-09-16-wp8-futu-openapi-unification.md` 与同名 plan
> 末节「WP8 验收记录」。
> **WP9–WP12（2026-09-16）：日常闭环自动化 + 数据面补全**——sim 全自动流水线
> （`auto_pipeline` 开关 + build_plan/auto_execute/reconcile 入链 + 指令轮询并入服务，
> 规格 WP9）、流程页签（`pipeline` 端点，WP10）、资讯 PIT 地基
> （`sentiment_snapshots` 三源采集，WP11）、**富途 OpenAPI 数据面补全**（F10 26 section /
> 筛选 / 板块 / 做空 / IPO / 经济日历 / 自选，16 端点；WP12）。
> **当前计数：quantwb 工具面 74（69 端点工具 + 5 维护）、服务端点 77**——工具面按
> **三档**治理（见「富途数据面」节）：直通 11 / 聚合 2 / HTTP-only 3（数据面内）。
> 规格见 `docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md`，
> 端点锁定表见 `docs/superpowers/plans/wp12-endpoint-lock.md`，逐端点口径见
> `docs/TOOL-LIMITS.md` §十。
> WP6 及更早的修订标注保留在后文相应位置。

## 结论性字段的中文标签

结论（信号、买卖方向、评级、持仓状态、数据源状态、回测口径）在产出侧一律附
`*_label` 中文标签，**机器码保留不动**（台账已落盘、逻辑在比较、旧记录要能读）。

唯一事实来源是 `plugins/datasource/python/trading_datasource/labels.py`；
`plugins/workbench/src/labels.js` 是 Host 侧镜像（浏览器侧回退表随 legacy 面板
client.js 于 WP7 退役删除）。两份表由 `tests/test_labels.py` 解析比对，漂移即失败。

回退表存在的唯一理由是**渲染历史记录**：旧台账/旧预览/旧研报里只有 `BUY`、
`buy`、`open`、`Buy` 这类英文码，界面必须能翻成中文。

## 产品定位

本项目是 **DeepSeek Harness 的对话模式与插件组合**，不是独立交易终端。
Harness 是唯一 AI 对话、分析请求和交易指令入口。工作台自 WP6 起是
**独立 Web（FastAPI 单进程托管）**，也是唯一工作台界面——Harness 内 legacy 面板
已于 WP7 退役（用户决策 2026-09-16，随独立服务承接全部工作台能力而移除）。
独立 Web 只用于研报结果、交易动态、量化信息预览与模拟盘/实盘模式切换；
Harness 内 `plugins/workbench` 保留 `tradingWorkbench` 服务锚
（engine 对话工具与账户策略的进程内依赖），不再有任何 UI。

```text
用户 ↔ Harness 对话 / 工作台业务确认（服务进程内 Web 卡片作答，唯一确认通道）
      │
      ├─ trading-agents skill + Harness 子代理
      │    └─ 富途 MCP（只读研究）/ fin_news / fin_sentiment / web 取数
      │
      ├─ engine：启动研究 → 发布研报；量化预览；futu 写类 guard 拒绝并指引工作台
      │    └─ Python 量化计算（不替代券商成交）
      │
      └─ quantwb trade_*/account_* 工具 → 观察最终响应（写需服务进程内业务确认）
                │
                ▼
      独立服务进程（platform/，FastAPI + uvicorn 单进程；WP7 独立量化平台）
        ├─ 服务内调度器（吸收 daemon 作业链：sync→quality→factors_snapshot；
        │   心跳/告警协议不变，daemon CLI 保留为手动入口）
        ├─ POST /api/wb/<endpoint>（envelope 契约）→ Ant Design Pro 前端
        ├─ /mcp（mcp SDK streamable-http，74 工具）→ Harness 的 mcp__quantwb__*
        ├─ 交易闸门：mode → 风控 8 规则（kill=规则 1）→ 业务确认（Web 卡片，进程内）
        │   └ broker 适配（sim；live 经 OpenAPI place/modify/cancel/order-confirm，
        │     未经真实 live 下单验证——sim→live 冒烟属 P4 人工准入）
        ├─ WS 推送（富途行情/交易事件 → OMS+告警；对账兜底轮询保留）
        └─ 静态托管 platform/web/dist；同一份 store/模式/指令文件
           （与 Harness 进程无网络互通，靠原子写 + 文件锁互斥）
```

**明确不做**：第二个聊天窗口、浏览器直接持有富途 token、
插件私建 LLM 对话循环、工作台 RPC 任意执行工具或 shell。
独立 Web **无聊天、无逐单下单/撤单表单、无 shell/LLM/token 接口**。
工作台的下单边界自 WP6 的「唯一写路径=指令文件」演进为 WP7 的「**两条受约束写路径**」：
①执行已冻结计划（指令文件白名单 5 种指令，live 需口令复核，对话侧保留等价
`plan_execute`）；②WP7 新增的受约束 `trade_*` 交易工具（闸门链见 `platform/` 行：
模式→风控 8 规则→kill→业务确认，Web 卡片作答）。两条入口都有前置约束与留痕，
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

### 富途通道分级（WP7 决策）

**服务 = 账户/交易/行情的权威通道；Harness 直连富途 = 只读研究通道。**

| 通道 | 定位 | 写语义 |
|---|---|---|
| 服务进程（`trading_datasource.futu_mcp`，quantwb 工具面 + Web + 定时作业） | 账户/交易/行情/PIT 同步/因子数据的**权威通道**，token/续期/限速单一实现 | 唯一写路径：闸门链（模式→风控 8 规则→kill→业务确认）后的 `trade_*` + 计划执行 |
| Harness 直连富途（`mcp__futu__*`） | **只读研究**：新闻/板块/筹码/资讯等自由探索 | `trading_*`/`sim_trade_*` 写类被 policy 拒绝并指引工作台（`trading_*` 族未知动词按写拒绝=**fail-closed**；`sim_trade_*` 未知动词按读处理——sim 写伤害有界，已知 4 个写动词仍按写拒绝） |

理由（规格 §一）：①单一凭据/续期/限速；②写路径唯一——mode 文件、风控 8 规则、kill、
业务确认、审计只实现一次；③PIT 存储与因子计算在服务侧，数据就近；④Harness=大脑，
交易通道不属于它。

### 富途通道决策（WP8）

**权威通道 = 工作台富途 OpenAPI（REST+WS，OAuth2.1+PKCE / AppKey 签名双认证）。**
WP8 起服务进程的行情取数与交易执行统一经 `trading_datasource.futu_openapi` 客户端
（`trading-platform.json` 顶层 `futu_channel: openapi|mcp` 切换，默认 mcp 保持零变化；
openapi 已配置凭据时行情与交易都走 REST，交易事件走 WS 推送）。据此：

| 通道 | WP8 后定位 |
|---|---|
| 工作台 OpenAPI（`mcp__quantwb__*` + Web） | **唯一权威通道**：行情/交易/推送/账户全链路，写路径唯一（闸门链 + 业务确认） |
| Harness 直连富途（`mcp__futu__*`） | **只读研究**（不变）：写类仍被 policy 拒绝并指引工作台 |
| 托管 MCP（`futu-mcp` preset 行，91 工具） | **可选只读研究通道**（preset 默认 disabled——用户可自行启用做自由研究）；写通道唯一在工作台 |
| SkillHub | 仅作能力对照，不作为集成通道（其 OpenD 形态与单进程服务冲突） |

## 组件职责

| 组件 | 责任 |
|---|---|
| 根目录 preset | 新建「交易智囊模式」，组合 persona、原生工具、skill 和 MCP |
| `skills/trading-agents/SKILL.md` | Harness 主会话与子代理执行 12 角色、6 阶段研究，数据不足显式说明 |
| `plugins/engine/src/tools.js` | `run_trading_analysis` 启动记录、`research_publish` 发布有来源的报告、量化结果保存 |
| `plugins/engine/src/policy.js` | 拒绝跨模式账户工具；WP7 收窄：futu 写类（`sim_trade_*`/`trading_*` 的下单/改单/撤单动词）guard **一律拒绝并指引工作台**（不返回 `ask`，与权限档位无关；`trading_*` 族未知动词按写拒绝=**fail-closed**，`sim_trade_*` 未知动词按读处理——sim 写伤害有界）；记录最终工具响应 |
| `plugins/workbench/src/index.js` | 根级 Host 插件，只提供 `tradingWorkbench` 服务锚（研报/预览/观察记录；WP7 面板退役后不再注册任何 Connection RPC） |
| `plugins/workbench/src/store.js` | JSON 持久结果、原子替换、写锁、账户调用租约、模式隔离 |
| `plugins/engine/python` | 量化计算与本地模拟台账的权威实现 |
| `plugins/datasource/python` | **统一数据层**：唯一的富途 MCP 客户端、行情路由与回测核心，被 engine/workbench 共同依赖（不是 Harness 插件） |
| `plugins/core/python/trading_core` | **量化平台核心库**（非 Harness 插件）：PIT 存储/日历/同步/质量（WP1）、因子/策略/组合回测/walk-forward（WP2）、风控八规则/计划冻结/OMS 状态机/券商适配/对账/TCA（WP3）、daemon 调度/指令目录/告警（WP4） |
| `trading_core` daemon | 无 LLM 单进程守护进程（`python -m trading_core daemon`）：按交易日历触发作业链（sync→质量→信号→计划、对账→TCA→摘要，WP7 起各市场链末尾追加 `factors_snapshot`）、心跳落 `~/.dsh/trading-daemon.json`（> 5 分钟未刷新工作台标红）、轮询指令目录 `~/.dsh/trading-commands/`、告警分级落 `alerts` 表（WP4）。**WP7 起常驻循环由平台服务内调度器承担（见 `platform/` 行）；daemon CLI 保留为手动/兼容入口，与调度器共享 kv `daemon:state` 的 ran 标记，同日作业不重复执行** |
| `platform/` 独立服务进程（WP6，WP7 独立量化平台） | FastAPI/uvicorn **单进程**，WP7 起承载四块新增职责：**①服务内调度器**（`server/scheduler.py`：daemon 作业链 sync→quality→factors_snapshot 原样复用，tick-first——启动即先跑一轮并补跑当日到期作业，与手动 daemon 共享 ran 标记不重复执行；心跳/告警协议不变；线程随 lifespan 启停，`/healthz` 附 `scheduler:{alive,last_error}`，`last_error` 保留最近一次异常、成功不清除、300 字符截断）；**②富途交易闸门**（`server/trading.py`：模式文件→风控 8 规则（kill 文件=规则 1）→业务确认（`store_access.request_confirmation` 进程内阻塞，**Web 确认卡片作答**，TTL 120s 超时=拒绝 fail-closed）→broker 适配（sim 下单/改单/撤单=撤旧重下/查询；WP8 起 live 经 OpenAPI 执行协议 place/modify/cancel/order-confirm，**未经真实 live 下单验证**，sim→live 冒烟属 P4 人工准入；无 OpenAPI 凭据 → `trading/openapi-unavailable`）；OMS 落 `orders`/`risk_checks` 表，超时→unknown 只查询不重放）；**③受约束交易工具**（`trade_place/trade_modify/trade_cancel` + `account_positions/account_orders/account_funds`，工具面 27→**33**）；**④`factors-history`**（定时快照按交易日落 `factor_snapshots` 表，HTTP/CLI/`mcp__quantwb__factors_history` 三路同源）。**WP8 起新增：⑤OpenAPI 统一通道**（`trading_datasource.futu_openapi`：OAuth2.1+PKCE/AppKey 双认证、限频/错误码映射、`futu_channel` 通道选择）；**⑥WS 推送**（`server/futu_push.py`：行情订阅+交易事件→OMS 状态迁移+告警，重连/refresh 保活，断线事件不补发→**对账兜底轮询保留**，事件仅加速不作唯一事实源）；**⑦推送订阅面**（`push_status/push_subscribe/push_unsubscribe`，与 `/healthz` 的 `push` 同一实现）；**⑧设置页凭据配置**（WP8 任务 7：`openapi_config`/`openapi_test` 读写在 `server/settings_api.py`；OAuth 集成增补 `openapi_oauth`：OAuth 2.1+PKCE 授权流程 start/status/cancel 在 `server/oauth_flow.py`（注册 client→PKCE→127.0.0.1 回调→换 token→凭据落盘 0600，与 AppKey 模式并存；code_verifier 只存内存，state 逐字校验，600s 超时自动停），Web 表单保存 AppKey 凭据（私钥 PEM 粘贴落盘 0600 或已有路径）、联动 `futu_channel`、连通性自检；**私钥原文绝不回显**，状态只给掩码 app_key 与公钥指纹）。既有职责不变：`POST /api/wb/<endpoint>` + `GET /api/wb/openapi_config`（envelope 契约，**77 端点**）+ `/mcp`（mcp SDK streamable-http，**74 工具**=69 端点工具+5 维护；**`confirm-decide` 与设置页两端点有意不进工具面**，防模型自批实盘单/自改凭据）+ `<DSH_HOME>/trading-workbench.json` store 访问层（只读快照/模式切换/业务确认/维护动作）+ `platform/web/dist` 静态托管（`GET /`，SPA fallback）；数据路径复用 `plugins/workbench/python/*` 脚本、`trading_core snapshot-*`/`factors-*` CLI 与指令目录协议，HTTP 与 MCP 同一批处理函数（同源，规格 §3.1） |

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

### 情绪/资讯 PIT 积累与演进条款（WP11）

情绪与资讯数据自 WP11 起**每日按关注池落 `sentiment_snapshots` 表**
（`date,symbol,source,payload,fetched_at`，主键三元组；**只存渠道原文事实，不打分**——
打分算法会漂移，原始数据不会）。采集作业 `sentiment_snapshot` 在**基础链**上常驻
（不受 `auto_pipeline.enabled` 交易开关控制，理由见规格 §4.1 作业分层）：数据积累
不应因为没开自动交易而停摆。

三源现状：`fin_sentiment`（X + 千股千评）、`fin_news`（fin-data 的新闻路由：
futu → AKShare → Yahoo）、`last30days`（可选组件，未安装即缺席）。**上游
`quote_news_search` 实测恒空**（TOOL-LIMITS/HANDOVER 记录），故不接该通道——不造一个
永远为空的源；单源失败对该源计缺席并告警，不阻塞其余源与作业链。

**演进条款（规格 §6.3）**：某资讯类因子候选**累计 ≥250 交易日**后，可由研究院提出
检验申请，走 WP14 验证门（IC t 检验显著 + 分层单调 + walk-forward OOS）；**转正前不得
出现在任何启用规则的 factors 列表**（规则解释器机械拒绝未注册/未通过检验的因子引用）。

## 交易动态与实时性

账户工具的 `tools/result` 事件记录为 `broker_response`，包含模式、时间、
工具名、会话和结果/错误。敏感认证字段脱敏，超大响应不完整复制。
独立 Web 的工作台页面打开时轮询 `/api/wb/snapshot`（默认 60 秒兜底轮询），
关闭后停止轮询（legacy 面板的 Connection RPC 通道已随面板退役）。

### 本地缓存与"不追求实时"

工作台是查看用途，不需要实时取数。这些接口背后是 python 子进程与富途调用，
每次切页签都重跑既慢又浪费额度，因此有两层缓存（legacy 面板的双层结构在独立 Web
中原样保留，只是宿主从 Harness Client/Host 换成了浏览器/服务进程）：

| 层 | 位置 | 作用 |
|---|---|---|
| 客户端内存缓存 | `platform/web/src/services/api.js`（按接口 TTL，对齐旧 client.js） | 切页签、重开页面不再重复请求 |
| 服务 TTL 缓存 | `platform/server/caches.py` 的 `CACHE_TTL_MS`（按接口粒度，1–30 分钟） | 即使客户端重新请求也不会重跑取数 |

`_refresh: true` 是显式旁路标记（不参与各接口的字段校验）；页面的「刷新」按钮会清空
客户端缓存，并让随后 5 秒内的请求穿透服务缓存。切换模拟/实盘会清缓存，
避免展示上一个模式的数据。界面上明确写出"数据按 TTL 本地缓存，不追求实时行情"。

### 接口自检：让"版本陈旧"可诊断

服务在 `snapshot` 响应里**声明**它实际提供的接口（`endpoints`，与端点清单同源）；
前端据此**不发起**已知缺失的请求，直接给出可读原因（「服务版本陈旧，请重启服务后
刷新」），声明集合取不到时一律放行，不把旧服务拦死。业务失败（如台账不可读）
不会被误判成路由缺失。点击「刷新」会清掉缺失记忆，让重启后的服务有机会恢复。
（历史：这一自检机制原为诊断 legacy 面板的 Host/Client 半边加载时差而生，该形态已随
面板退役；独立 Web 是服务端单进程，不存在半边版本差，前端预检保留为版本陈旧提示，
并继续以 404 分支兜底。）

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
因此分钟级并不比日线贵；配合两层缓存（浏览器与服务各 5 分钟），切页签为 0 次调用。

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
- 缓存分三层：浏览器内存 → 服务端 TTL（2 分钟）→ python 磁盘缓存（5 分钟）。
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
- `mcp__futu__sim_trade_*` 仅 sim；`account_*`、`trading_*` 仅 live（WP7 收窄后：两族
  **写类动词不分模式一律拒绝**并指引工作台，见「富途通道分级」）；真实写操作唯一路径是
  工作台 `trade_*` 工具——服务进程内逐笔业务确认（Web 卡片作答，独立于 Harness 权限审批）
  或执行已冻结计划（口令复核）。

这是插件管理的 MCP 工具边界，不是操作系统沙箱。拥有宿主 shell、文件权限或券商凭据
的人仍能绕过插件，因此禁止模型绕路，日常只授只读权限，不能宣称已经具备完整实盘安全保证。

## 服务协议与存储

（历史：legacy 面板时代工作台经原生 `ctx.connection.fetch.register` 注册 POST 路由、
Client 用 `ctx.connection.rpc.call` 调用并继承 Connection 信任——该 Host RPC 面已随
面板于 WP7 退役。）

**WP6 起同一批端点在 `platform/` 独立服务进程内以 `POST /api/wb/<endpoint>` 暴露**
（envelope 契约不变：`{ok, value?, cached?, cached_at?, error?{code,message,details}}`）；
`/mcp` 的 33 个 MCP 工具与 HTTP 路由在该进程内调用**同一批 Python 处理函数**
（同一 `(endpoint, payload) -> envelope`），行为对等由代码结构 + 审批回归矩阵共同保障。
`snapshot` / `switch-mode` 之外，WP4 新增 4 个受约束端点
（读侧一律经只读子命令取数，Python 侧经 `trading_core snapshot-*`
读 SQLite，不直接改库；写侧只落指令目录，不直接操作业务状态）：

| Endpoint | 输入 | 输出 |
|---|---|---|
| `snapshot` | `{}` | 当前 mode、研报、研究记录、量化预览、交易响应、快照时间 |
| `switch-mode` | `{mode, expected_mode, confirmation?}` | 实际 mode 与 `order_authorized: false` |
| `plan` | `{}` | 当前/历史计划：目标 vs 实际 diff、逐单风控报告、状态时间线 |
| `plan-execute` | `{plan_hash, expected_mode, confirmation?}` | live 需口令「确认执行」；服务端复核冻结状态/hash/mode；成功只返回 `{queued, nonce}`，状态经 `plan` 轮询 |
| `schedule` | `{}` | daemon 心跳、作业历史、下次运行 |
| `reconcile` | `{}` | 最近对账差异、TCA 摘要、告警列表 |
| `confirmation` | `{}`（空载荷，**不进缓存**） | `{pending, ttl_ms}`；`pending` 为待用户确认的实盘写操作（编号、工具、中文订单摘要、创建/到期时间）或 `null` |
| `confirm-decide` | `{id, decision: "approved"\|"rejected"}` | 提交用户的决定；**唯一能批准实盘操作的通道**，只由独立 Web 确认卡片的用户点击触发（不进 MCP 工具面） |
| `factors-history`（WP7） | `{limit?}`（1..120，TTL 5m 缓存） | 定时收集的因子快照历史（按交易日倒序） |
| `sentiment-history`（WP11） | `{symbol?, limit?}`（1..120，TTL 5m 缓存） | 给了 `symbol` → 该标的情绪快照倒序记录；未给 → 最近有记录日摘要 `{date, symbols, sources, days}`。**只作研究参考，不参与信号计算**（`factors.py` 铁律：情绪永不入因子计算） |
| `trade_place` / `trade_modify` / `trade_cancel`（WP7；WP8 任务 6 扩面） | `trade_place`: `{symbol*, side*, qty*, order_type?, price?, time_in_force?, session?, aux_price?, lot_type?, remark?, order_class?, multi_leg_info?, client_order_id?}`（官方 place-order 全字段：8 种 order_type / GTC / 美股时段（市价单仅 RTH）/ 触发价 / 港股手数 / 备注 ≤64B / 多腿 MLEG）；`trade_modify`: 另加 `aux_price?`（官方改单请求体无 order_type）；`trade_cancel`: `{order_id*, symbol*, client_order_id?}` | 经交易闸门链的写操作；字段校验（条件必填/枚举/互斥/结构，**早于风控与确认**）失败 → `trading/invalid-operation`（消息带官方允许值），风控/确认/券商失败 → `trading/order-rejected` / `trading/broker-unavailable`，绝不 500；sim 通道仅支持限价当日单，扩展字段如实拒绝、不静默丢弃 |
| `account_positions` / `account_orders` / `account_funds`（WP7） | `{mode?}` | 券商账户查询直通（模式文件约束；不进任何缓存；失败账户列入 `errors` 不掩盖） |
| `push_status`（WP8 任务 6） | `{}`（空载荷，TTL 0） | 富途 WS 推送状态：`{enabled, started, reason, last_error, quote, trade}`——与 `/healthz` 的 `push` **同一实现、同一事实**（quote/trade 各含 connected/authenticated/最后消息时间/重连次数/订阅意图） |
| `push_subscribe` / `push_unsubscribe`（WP8 任务 6） | `{quote?, order_book?, ticker?, kline?: [{symbol, period, adjust}]}`（**非交易**载荷） | 追加/精确移除本地连接订阅意图（不改模式、不过风控、不产生订单）；成功回意图快照；推送未启用 → `trading/push-unavailable`（如实拒绝），载荷非法 → `trading/invalid-operation`；幂等（重复订阅不重发订阅帧） |
| `pipeline`（WP10） | `{}`（空载荷，TTL 30s） | 流程页数据源：每市场当日各阶段（真实作业名 + `plan`/`execute`/`digest`）`{label, status: pending\|ok\|skipped\|failed, at, scheduled, summary}` + `auto_pipeline` 生效配置摘要；**只读既有事实**（kv ran 标记、plans/orders、当日告警归因），不造状态、不写库、不调券商 |
| `auto_pipeline`（WP10） | GET 空载荷读；POST `{enabled, strategies, exec_at, exec_window_minutes, reconcile_at}`（白名单） | 自动流水线开关读写：写入经 core `autopipeline.apply_overlay` **同一校验实现**，原子写 `trading-platform.json` 的 `auto_pipeline` 键（保留其他键），校验失败文件零改动；**两端点不进 MCP 工具面**（模型不得自拨开关，同 `openapi_config`/`confirm-decide` 先例） |
| 富途数据面 16 端点（WP12） | 直通 11（`stock_screen`/`plate_list`/`plate_stock`/`short_daily_volume`/`short_interest`/`ipo_list`/`economic_calendar_hot`/`economic_calendar_search`/`info_owner_plate`/`watchlist_list`/`watchlist_groups`）+ 聚合 2（`f10_detail`/`derivative_detail`，载荷 `{code*, section*, params?}`）+ HTTP-only 3（`warrant_screen`/`modify_user_security`/`info_rehab`） | 逐端点路径/参数/错误码见 `docs/superpowers/plans/wp12-endpoint-lock.md`（2026-09-16 官方文档核对），实测口径见 `docs/TOOL-LIMITS.md` §十 |

### 富途数据面：三档治理与通道边界（WP12，2026-09-16）

**为什么分三档**：数据面一次补入 16 个端点，若全部 1:1 进模型工具面会从 59 涨到 75+，
模型选择成本与误用面同步膨胀。治理规则（**新增端点必须显式登记档位与理由**）：

| 档 | 端点 | 理由 |
|---|---|---|
| **直通**（逐端点 1:1 进工具面，11） | 研究高频、语义独立 | 每个都有独立语义与参数形状，聚合反而增加一层映射 |
| **聚合**（一个工具 + section 白名单枚举，2） | `f10_detail`（26 section）/ `derivative_detail`（4 section） | 结构同质、逐项暴露收益低；section 白名单以传输层 `OpenApiF10.SECTIONS` 为单一事实源 |
| **HTTP-only**（服务端点面可达、不进工具面，3） | `warrant_screen`（不策略化窝轮）/ `modify_user_security`（写用户富途自选，非交易写）/ `info_rehab`（同步作业内部取数） | 无需模型自由调用；写类限 Web 用户操作 |

**通道边界（fail-closed）**：数据面 16 端点**只在 openapi 通道登记**——托管 MCP 的上游
工具名与参数形状未逐项核对（禁止猜名），`futu_data._fetch_mcp` 对它们如实拒绝并指引
「配置凭据 + 切 `futu_channel=openapi`」。因此**默认配置（mcp 且无凭据）下研究页/期权页
的数据卡会显示「不可用 + 下一步」**——这是如实呈现而非故障。

**前端 TTL 镜像**：客户端 TTL 与服务端 `caches.CACHE_TTL_MS` 逐项一致（6h/30m/5m/1h 四档，
写端点 0），跨语言漂移由 `tests/test_wp12_locks.py::WebTtlMirrorTests` 解析两侧比对，
单边改值即失败。

除 `plan-execute`（执行已冻结计划，白名单指令落盘）、`confirm-decide`（人工批准）与
WP7 的 `trade_*` 写端点（必须通过交易闸门链：模式→风控 8 规则→kill→业务确认）外，
不开放 shell、LLM 或 token 读取接口。所有页面内容按文本呈现，不执行研报中的 HTML。

**业务确认表述统一（WP7 修订，2026-09-16）**：全仓库的「工作台业务确认」自 WP7 起只有
两条路径，表述以此为准——

1. **工作台 `trade_*` 工具 → 服务进程内确认**：`trade_place/trade_modify/trade_cancel`
   触发的确认在**服务进程内**发起（`store_access.request_confirmation`），用户在**独立 Web
   的确认卡片**作答（`confirmation`/`confirm-decide` 端点，同进程，无跨进程问题）。
   这是当前**唯一可达**的实盘写确认路径（TTL 120s 超时=拒绝，fail-closed）。
2. ~~**legacy 面板的确认 UI**~~（**已于 WP7 退役，用户决策 2026-09-16**）：futu 写通道收窄后
   （engine guard 对 futu 写一律拒绝），Harness 会话侧不再产生新的 futu 写确认；该确认 UI
   连同 Host Connection RPC 的 `confirmation`/`confirm-decide` 路由与 store 确认三方法
   已随面板整体删除。**实盘写确认自 WP7 起只有路径①。**

下文的「跨进程边界」清单按 WP6 时点撰写，仅作历史依据与设计参考保留（其中「Harness
会话发起确认、面板作答」的路径已不存在）。

**业务确认的跨进程边界（诚实清单，2026-09-15 业务确认修订）**：实盘写操作的确认是
“此刻等人回答”的**内存态**，刻意不落盘（进程重启后无人回答，落盘会让陈旧请求复活；
持久化的只有 `activity` 里的事件留痕）。

> **已于 WP7 退役/闭环（用户决策 2026-09-16）**：下述「两进程各持一份待确认表、互不可见」
> 与「过渡期在 Harness 内 legacy 面板作答」均成历史——Node 侧确认三方法与 Connection RPC
> 的 `confirmation`/`confirm-decide` 路由已随面板删除，Harness 进程不再有确认实现；WP7 的
> 交易闸门让服务进程自身发起确认（工作台 `trade_*` 工具），独立 Web 确认卡片就是作答界面，
> **确认在服务单进程内闭环**，跨进程问题不复存在。以下清单保留作 WP6 时点的历史依据。

- ~~Harness 会话里实盘写操作触发的确认，独立 Web 的 `confirmation` 端点读不到；该笔确认
  只能在 Harness 内的工作台面板作答（同进程，Connection RPC）~~（该路径已随面板退役删除）。
- 服务进程的 `confirmation`/`confirm-decide` 只反映**服务自身处理函数发起**的确认
  （WP7 起即交易闸门的 `trade_*` 写路径——这是唯一的确认发起方）。
- ~~独立 Web 本期不建确认界面~~（WP6 时点的有意不做；WP7 已落成确认卡片，本条失效）。

`confirmation` 与 `confirm-decide` 因此是**同一进程内**的读/答两端，不是跨进程确认总线；
确认是本进程内存态，服务重启即清空（超时语义等价于 fail-closed）。

**端点声明自检（前端）**：`snapshot` 响应带 `endpoints` 数组（服务端 `store_access.endpoints()`，
29 项 = 22 项基础清单 + WP7 服务自有端点；WP7 起清单为服务自有常量，不再解析面板源文件），
前端 `services/endpoints.js` 用纯函数比对本次要调的端点；未声明即
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
工作台显示等待合并数量。这里只重试保存响应，绝不重试券商操作。

异常退出可能留下写锁或调用租约。恢复时先停掉相关 Harness 进程、核对券商订单状态，
再检查锁/租约中的 PID 和时间，仅清理确认无持有者的具体文件；不要自动过期删除未知在途订单的租约。

## 兼容性与后续准入

工具依赖钉在 `@deepseek-ai/dsh-tools@0.1.2-rc.1`。Host API 对照官方源码
`c291e7961a515f6d7af9304e7fd1d257929aef26` 的工具守卫/结果事件
（历史：Connection RPC 与客户端 `./client` 导出、module factory 的对照已随 legacy 面板
于 WP7 退役失效；插件不再使用 `ctx.connection`）。

优先级：安装/宿主集成验收 → 数据源与量化正确性 → 富途模拟盘成交/对账 →
告警、审计、熔断及恢复 → 样本外策略评估 → 人工决定是否小额实盘。
券商实时推送、港美历史数据与多市场交易规则仍需后续实现；本地对账/调度/熔断告警
由 WP3–WP4 交付（已实测，恢复演练见 [RUNBOOK.md](RUNBOOK.md)）；
工作台可用不代表已达到实盘准入条件。
