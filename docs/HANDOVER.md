# 交接与复审文档

## 一、目标与不可改变的边界

本项目是 **DeepSeek Harness 的新对话模式 + 原生插件**：

- Harness 负责全部 AI 对话、投研/量化指令、交易指令和逐笔人工确认。
- 工作台嵌在 Harness 中，展示研报、最近交易响应、量化预览，并提供 sim/live 切换。
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
| 工作台 Host | 根级 `tradingWorkbench` 服务，持久报告/预览/响应；认证后的 Harness Connection RPC |
| 工作台 Client | 正确的 Host/Client 双入口、宿主 React module factory；结果卡片与面板，不需重建 Harness Web |
| 交易动态 | 观察原生账户工具最终响应；打开面板时每 3 秒刷新快照；不伪装券商成交推送 |
| 模式切换 | 用户在面板明确确认 live，携带预期旧模式；在途账户调用租约阻止跨进程切换；脚本只查询/恢复 sim |
| 执行策略 | 账户工具按 sim/live 拒绝不匹配调用；真实写工具经 Harness 原生审批；不允许模型走其他通道绕过 |
| 量化 | 修正风险/成本/成交时序；本地模拟与券商数据分离，失败不能回退为零价格或虚构资产 |
| 安装 | Bash/PowerShell 共用插件安装流程，包路径、失败退出、幂等启用与更新行为可重复核对 |

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
工作台持久状态和模式切换、客户端入口及 RPC 白名单。
Python 用标准库 unittest，行情与账户使用隔离样本/临时目录，不发真实交易请求。

本轮本地基线：17 项 Node、52 项 Python 用例通过，覆盖包内根级 patch、模拟台账提交与
模式锁的串行化、安装失败及模式文件不变性。Bash 语法检查通过；当前环境未运行
PowerShell 安装器，也未启动完整 Harness Web。后续券商模拟订单实测见下节。

本地合约成立不等于完整部署验收。仍需在目标 Harness 版本、实际操作系统和新会话中
确认 preset 发现、插件加载、原生审批、客户端加载及真实数据接口行为。

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

## 五、尚未完成的能力

| 缺口 | 必须如何描述 |
|---|---|
| 富途 K 线历史数据问题 | 之前的裸 HTTP internal error 是历史记录；不能认定重启或完整 SDK 必然修复 |
| 富途模拟盘订单生命周期 | 查询、受理、改单、撤单和资金释放已实测；成交/部分成交、断线后订单去重与自动对账仍需完善 |
| 业务错误归一化 | `ret_code != 0` 可能包在 `isError=false` 内；工作台当前错误标记只跟随工具外层状态，不能把无错误标记视为业务成功 |
| 授权续期 | **官方 `expires_in = 7200`（2 小时）**，续期不换发 refresh_token。实测 token 过期时 `initialize` 仍成功、而所有 `tools/call` 返回 `internal error`（不是 401）。两条通道处理不同：脚本通道遇该特征自动续期并重试一次；MCP 通道由 preset 里的 `futu-keepalive` 行按节奏提前续期（其 Authorization 头在行挂载时求值一次并被 Zod 压平，重连不重读，因此无法做到每请求刷新）。**实测 preset 目录无 watcher**，所以续期只对新建会话生效——已在运行的会话必须新建会话才会重新读取组合 |
| 实时交易推送 | 面板刷新已有工具响应，不自动查询券商，也未建立成交订阅 |
| 多市场量化 | 首期为 A 股日线，港美历史数据、费用和交易规则需要独立完善 |
| 生产级风控 | 本地风险公式/止损意图不等于券商托管止损、日内熔断、组合风险与紧急停机 |
| 自动运行 | 没有生产级定时调度、监控告警、无人值守交易服务 |
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
