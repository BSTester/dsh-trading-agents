# dsh-trading-workbench（Client UI，P1/P5）

Client 半插件，通过 `dsh.client` 被 harness 客户端扫描加载（无需组合行）。

## 已实现

- `tool.call.toolview` key=`run_trading_analysis`：投研流水线结果卡片（纯展示报告文本）

## 待实现（P5 Dashboard）

- `shell.overlay` 交易工作台面板：持仓/信号/绩效一屏（需 Host 半经 `harness.handle`
  暴露 `quant_report`/`quant_signal`，shell 到 engine.py 取数）

## 构建与验证

- 依赖客户端打包链路（`dsh.client` 扫描 + apps/web Vite 构建）；改源码后需
  `pnpm run dev:web` 重建，重启 harness 后生效。
- 本机未能在当前会话内验证（进程内 loader/客户端缓存 + 需重启），列为重启后验收项。
