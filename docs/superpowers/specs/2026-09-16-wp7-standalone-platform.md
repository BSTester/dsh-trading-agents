# WP7 独立量化平台规格（服务为中心的重构）

> 状态：按用户指令（2026-09-16）设计——「合并后的代码重构为完整独立版本；工作台独立定时收集量化因子而不依赖 Harness；Harness 只做大脑」。
> 前置：WP6 已合并主干（`7ef5049`）：FastAPI 单进程服务（HTTP 22 端点 + MCP 26 工具 + Web 托管）、AntD Pro 前端、审批回归矩阵。
> 全局约定见 `2026-09-14-platform-plan-index.md`。

## 一、富途通道拓扑决策（用户委托权衡）

**结论：富途集成进工作台——服务是账户/交易/行情数据的权威通道。Harness 侧富途直连降级为只读研究通道。**

| 通道 | 用途 | 工具面 |
|---|---|---|
| 工作台服务（`trading_datasource.futu_mcp`） | 行情/账户/交易/PIT 同步/因子数据；**唯一写路径** | quantwb MCP 的数据与交易工具 + Web UI + 定时作业 |
| Harness 直连富途（`mcp__futu__*`） | **只读研究**：新闻/板块/筹码/资讯等自由探索 | 保留只读；`sim_trade_*`/`trading_*` 写类被 policy 拒绝并指引工作台 |

理由：①单一凭据/续期/限速；②写路径唯一——mode 文件、风控 8 规则、kill、业务确认、审计只实现一次；③PIT 存储与因子计算在服务侧，数据就近；④Harness=大脑，交易通道不属于它。

## 二、目标架构

```text
┌─ 独立工作台（FastAPI 单进程：platform/server）──────────────┐
│ Web UI（AntD Pro，dist 托管）+ /mcp（quantwb，Harness 交互面）│
│ 富途通道 trading_datasource.futu_mcp（token/续期/限速）       │
│ 调度器（吸收 daemon：作业链/心跳/告警协议不变）                │
│   └ 因子快照定时收集（factors_history，按交易日落库）           │
│ 交易闸门：mode 文件 → 风控 8 规则 → kill → 业务确认（Web 作答） │
│   └ broker 适配（futu 下单/改单/撤单/查询）                    │
└───────────────────────────────────────────────────────────┘
      ▲ MCP（数据/因子/计划/受约束交易）        ▲ HTTP（本机浏览器）
┌─ Harness（大脑）───────────────────────────┐
│ 研究/决策/分析；quantwb 工具面；              │
│ mcp__futu__* 只读研究；写类被拒并指引工作台     │
└────────────────────────────────────────────┘
```

关键变化（相对 WP6）：
1. **调度进服务**：`trading_core.daemon` 的作业链改为服务内线程驱动（`daemon.tick`/JOBS/心跳/告警协议原样复用，`daemon` CLI 保留为手动/兼容入口）；「固定信息收集由服务定时跑」就此落地。
2. **因子快照定时收集**：每个交易日收盘后自动跑因子/信号并落 `factors_history`，可按日期回看（新端点 `factors-history` + MCP 工具）。
3. **交易集成进服务**：新增受约束交易工具（`trade_place/trade_modify/trade_cancel` + `account_positions/account_orders/account_funds`），写操作前置链 = 模式 → 风控 8 规则 → kill → **业务确认（Web UI 作答，进程内）** → broker。WP6 的「确认跨进程不可见」问题就此消解（写路径与 Web 同进程）。
4. **Harness 写通道收窄**：`policy.js` 对 futu 写类工具改为拒绝并指引工作台（消息含工作台通道指引）；研究只读不受影响。
5. **一键安装**：`install/HARNESS_SETUP.md` 提示词 + 安装器扩展，粘贴进 Harness 新会话即可完成「对话模式 preset + 服务依赖 + Web 构建 + 服务启动 + MCP 行启用 + 工具面验证」。

## 三、边界（不做）

1. 不把 91 个富途工具搬进 quantwb 工具面（研究自由度靠 Harness 只读直连）；
2. 不做逐单下单 Web 表单（Web 的交易入口=确认卡片+计划执行；会话内临时交易走 MCP 交易工具+确认卡片）；
3. 不做多用户/远程访问（仍 loopback + 可选 token）；
4. daemon CLI 与既有指令目录协议保持兼容，不破坏 WP4 验收口径。

## 四、验收标准

1. 服务启动后：调度器按交易日历自动跑作业链并写心跳；收盘后 `factors_history` 有当日快照；`factors-history` 端点/工具可按日期查询；
2. live 模式下 `trade_place`（经 MCP）→ Web 出现待确认卡片 → 批准后订单进入 broker 适配（sim 冒烟）；拒绝/超时 → 拒单且留痕；kill 文件 → 一切订单拒绝；
3. Harness 内 `mcp__futu__trading_*` 被拒且消息指引工作台；`mcp__futu__*` 只读研究不受影响；
4. 全新环境按 `install/HARNESS_SETUP.md` 提示词操作可完成安装并验证工具面；
5. 全量测试（Python/Node/web）全绿；文案规范 grep 零命中。
