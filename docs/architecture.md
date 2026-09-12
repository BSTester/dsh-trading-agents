# 架构与交互边界

## 产品定位

本项目是 **DeepSeek Harness 的对话模式与插件组合**，不是独立交易终端。
Harness 是唯一 AI 对话、分析请求和交易指令入口。工作台嵌在 Harness 内，
用于研报结果、交易动态、量化信息预览，以及模拟盘/实盘模式切换。

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
          workbench Host：持久结果与模式
                    │ Harness Connection RPC
                    ▼
          workbench Client：结果卡片 + 展示面板 + 模式切换
```

**明确不做**：第二个聊天窗口、工作台下单/撤单表单、浏览器直接持有富途 token、
插件私建 LLM 对话循环、工作台 RPC 任意执行工具或 shell。

## 组件职责

| 组件 | 责任 |
|---|---|
| 根目录 preset | 新建「交易智囊模式」，组合 persona、原生工具、skill 和 MCP |
| `skills/trading-agents/SKILL.md` | Harness 主会话与子代理执行 12 角色、6 阶段研究，数据不足显式说明 |
| `plugins/engine/src/tools.js` | `run_trading_analysis` 启动记录、`research_publish` 发布有来源的报告、量化结果保存 |
| `plugins/engine/src/policy.js` | 拒绝跨模式账户工具；真实写操作经过 Harness 审批；记录最终工具响应 |
| `plugins/workbench/src/index.js` | 根级 Host 插件，提供 `tradingWorkbench` 服务与认证后的 Connection RPC |
| `plugins/workbench/src/store.js` | JSON 持久结果、原子替换、写锁、账户调用租约、模式隔离 |
| `plugins/workbench/src/client.js` | Harness 原生 module factory，使用宿主 React；`shell.overlay` 面板及 `tool.call.toolview` 卡片 |
| `plugins/engine/python` | 量化计算与本地模拟台账的权威实现 |

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

## 交易动态与实时性

账户工具的 `tools/result` 事件记录为 `broker_response`，包含模式、时间、
工具名、会话和结果/错误。敏感认证字段脱敏，超大响应不完整复制。
面板打开时每 3 秒通过 Connection RPC 拉取最新快照；关闭后停止轮询。

这里的“动态”是 **Harness 已观察到的工具响应**，不是已建立的券商成交推送。
`SUBMITTED`、超时、拒单不能转换成“已成交”。没有账户响应时显示未知，
不生成 100 万初始资产来冒充实盘账户。最新持仓和订单仍需在 Harness 查询富途。

## 账户模式与执行边界

- `DSH_HOME/trading-account-mode` 是模式真源，缺失时 sim，非法内容报错。
- 模式持久化，**重启不会自动切回 sim**。
- 用户在工作台输入「确认实盘」才能切 live；请求包含 `expected_mode`，
  防止基于旧页面误切换。切换不授权任何订单。
- `quant_switch` 只能切 sim；`scripts/trade_mode.py` 可查询或离线恢复 sim，
  不再通过脚本启用 live。
- 账户调用持有 `trading-call-*.active` 租约，期间拒绝切换；
  多个 Host 进程和恢复脚本共用文件边界。
- `mcp__futu__sim_trade_*` 仅 sim；`account_*`、`trading_*` 仅 live；
  真实写操作在会话摘要确认之外经过 Harness 原生审批。

这是插件管理的 MCP 工具边界，不是操作系统沙箱。拥有宿主 shell、文件权限或券商凭据
的人仍能绕过插件，因此禁止模型绕路，日常只授只读权限，不能宣称已经具备完整实盘安全保证。

## Host / Client 协议与存储

工作台通过原生 `ctx.connection.fetch.register` 注册两条精确 POST 路由，
Client 使用 `ctx.connection.rpc.call("/api", "trading-workbench/...", ...)`，
采用 Harness 的请求/响应 envelope，继承 Connection 的信任、认证和生命周期。
不占用 Gateway 的共享 interceptor，也不创建额外 Web 服务。
仅开放两个 endpoint：

| Endpoint | 输入 | 输出 |
|---|---|---|
| `snapshot` | `{}` | 当前 mode、研报、研究记录、量化预览、交易响应、快照时间 |
| `switch-mode` | `{mode, expected_mode, confirmation?}` | 实际 mode 与 `order_authorized: false` |

不开放 `execute`、下单、shell、LLM 或 token 读取接口。所有页面内容按文本呈现，不执行研报中的 HTML。

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
港美历史数据、多市场交易规则、券商实时推送、自动对账、自动调度和生产级熔断仍需后续实现；
面板可用不代表已达到实盘准入条件。
