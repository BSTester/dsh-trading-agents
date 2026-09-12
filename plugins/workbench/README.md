# dsh-trading-workbench

DeepSeek Harness 内的结果展示插件。**不提供聊天或下单入口**。

Host 提供 `tradingWorkbench` 服务，保存研报、量化预览和 Harness 已观察到的富途账户响应。
Client 提供右下角工作台按钮、展示面板和投研/量化工具结果卡片。面板打开时每 3 秒刷新缓存，
不会自动发起研究、查询券商或重跑回测。

用户可在面板切换 sim/live；切 live 需输入「确认实盘」，在途账户调用期间禁止切换。
这只是模式变更，不授权下单。分析、下单、撤单和逐笔确认都在 Harness 完成。

## 原生加载

- Host 入口：`src/index.js`，根级状态服务；`connection` 就绪时注册两条精确 API 路由。
- `dsh.bundle.patch` 将 `trading-workbench` 注册为 profile 根级行；不能只声明 `dsh.client`，也不要在每个会话重复注册 Host。
- Client 入口：`exports["./client"]` → `src/client.js`，
  原生 `window.__ModuleLoader__.load` factory，从宿主模块表使用 React。
- 插槽：`shell.overlay` 与 `tool.call.toolview`。
- 通道：Harness Connection `/api/trading-workbench/{snapshot,switch-mode}`，仅 POST；无任意执行接口。
- 安装包已包含客户端入口，**不需要运行 Harness 的 `dev:web` 构建**；升级插件后重启宿主并刷新页面。

## 数据口径

没有账户数据时显示未知，不显示虚构资金；工具响应不等于成交。
本地量化台账与富途模拟账户分开，实盘不读取本地初始余额。
所有数据位于 `DSH_HOME`（默认 `~/.dsh`），模式重启后保留。
旧 `trading-memory.md` 不自动迁移；工作区复盘记忆仍由 Harness skill 维护。

详见 [架构与边界](../../docs/architecture.md)。
