# dsh-trading-workbench

DeepSeek Harness 内的 `tradingWorkbench` **服务锚**插件。**不提供聊天、面板或下单入口**。

WP7 起工作台 UI 由独立服务承接（`platform/`，FastAPI 单进程：HTTP + MCP + 静态前端，
见仓库根 `README.md`，功能细节见 `docs/FEATURES.md`「服务不是可选项」）；legacy 面板
（client.js）与 Host Connection RPC 已于 2026-09-16 退役删除。本插件只剩 Host 半边：
`tradingWorkbench` 服务保存研报、量化预览和 Harness 已观察到的富途账户响应，是 engine
对话工具（研报/预览/取消）与账户策略（模式互斥、调用租约、观察记录）的进程内依赖。

实盘业务确认只存在于服务侧（Web 确认卡片作答）；Harness 侧富途写类工具被
policy guard 一律拒绝并指引工作台。模式切换在独立 Web 完成，在途账户调用期间
禁止切换；这只是模式变更，不授权下单。

## 原生加载

- Host 入口：`src/index.js`，只 `provide("tradingWorkbench", store)`，不注册任何路由。
- `dsh.bundle.patch` 将 `trading-workbench` 注册为 profile 根级行；不要在每个会话重复注册。
- 服务侧数据访问与端点清单见 `platform/server/store_access.py`（清单已服务自有，
  不再解析本包内的 JS 文件）。

## 数据口径

没有账户数据时显示未知，不显示虚构资金；工具响应不等于成交。
本地量化台账与富途模拟账户分开，实盘不读取本地初始余额。
所有数据位于 `DSH_HOME`（默认 `~/.dsh`），模式重启后保留。
旧 `trading-memory.md` 不自动迁移；工作区复盘记忆仍由 Harness skill 维护。

详见 [架构与边界](../../docs/architecture.md)。
