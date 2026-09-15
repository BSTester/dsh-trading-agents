# WP7 独立量化平台 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp7-standalone-platform.md`（富途拓扑决策=§一；验收=§四）。
> **全局约定**：见 `2026-09-14-platform-plan-index.md`。测试三套：Python `~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'`、`node --test tests/*.test.mjs`、`npm --prefix platform/web test`。提交格式 `feat(scope): 摘要`。

**目标**：工作台成为独立的量化平台服务（自带调度与因子收集、自带富途交易闸门），Harness 只做大脑。

---

### 任务 1：服务内调度器（吸收 daemon）

**文件**：创建 `platform/server/scheduler.py`；测试 `tests/test_wp7_scheduler.py`

- [ ] **步骤 1**：失败测试（`tests/test_wp7_scheduler.py`）：①`Scheduler(tick=fn, interval=0.05)` 线程启动后按间隔调用 tick；②`stop()` join 且不再调用；③`build_tick(home, conn_factory, jobs=None, now=None)` 组装 `daemon.tick(conn, home, jobs, now)`（复用 `trading_core.daemon.tick`/`JOBS_DEFAULT`/`heartbeat_path`，导入方式参考 `tests/test_core_wp4_e2e.py`）；④心跳文件在 tick 后存在。
- [ ] **步骤 2**：运行确认 FAIL → 实现 `platform/server/scheduler.py`：
  - `class Scheduler`：`__init__(tick, interval=60.0)`，`start()` 起 daemon 线程（循环 `tick(); event.wait(interval)`），`stop()` 置位+join(timeout=5)；异常捕获进 `last_error`（不杀线程）。
  - `build_tick(home, conn_factory, jobs=None, now=None)`：返回无参 callable，内部开 store 连接→`daemon.tick(...)`→关闭连接；`now` 透传便于假时钟。
- [ ] **步骤 3**：`app.py` 的 lifespan：启动时 `Scheduler(build_tick(home), interval=60).start()` 存 `app.state.scheduler`，退出 `stop()`；`/healthz` 增 `scheduler: {"alive": bool, "last_error": str|None}`。
- [ ] **步骤 4**：三套全绿；`git commit -m "feat(platform): 服务内调度器（吸收 daemon 作业链）"`

### 任务 2：因子快照定时收集 + factors-history

**文件**：改 `trading_core/cli.py`（或新增 `trading_core/factors_history.py`）；`platform/server/compute.py`；`app.py`；`mcp_tools.py`；测试 `tests/test_core_wp7_factors_history.py`

- [ ] **步骤 1**：失败测试：①`save_factor_snapshot(conn, date, payload)` 落 `factor_snapshots` 表（store v3 追加 `CREATE TABLE IF NOT EXISTS factor_snapshots(date TEXT PRIMARY KEY, payload TEXT, created_at TEXT)`，不改 SCHEMA_VERSION，对齐 WP4 先例）；②`list_factor_snapshots(conn, limit)` 倒序；③CLI `factors-snapshot --tickers ... --date today` 跑 factors 并落库（factors 计算函数用注入替身）。
- [ ] **步骤 2**：实现 CLI 子命令 `factors-snapshot` / `factors-history`（JSON 输出，对齐既有 snapshot-* 风格）。
- [ ] **步骤 3**：调度接线：`daemon` 作业链（`JOBS_DEFAULT` 或服务侧 jobs 覆盖）每个市场收盘链末尾加 `factors_snapshot` 作业（cmd 形式 `[ "factors-snapshot", "--tickers", "@watchlist" ]`）。
- [ ] **步骤 4**：`compute.py` 加 `factors_history(limit)`（`python -m trading_core factors-history`）；`app.py` 路由 `factors-history`（白名单/shape `["snapshots"]`/TTL 5m）；`mcp_tools.py` 加工具 `factors_history`（**工具面 26 → 27**，TOOL_COUNT/`ENDPOINT_TOOL_ENDPOINTS`/R5 断言同步）。
- [ ] **步骤 5**：三套全绿；`git commit -m "feat(core,platform): 因子快照定时收集与 factors-history 查询"`

### 任务 3：交易闸门 + 受约束交易工具 + Web 确认卡片

**文件**：创建 `platform/server/trading.py`；改 `app.py`、`mcp_tools.py`、`platform/web/src/pages/execution.jsx`（或新 `confirmations` 区块）；测试 `tests/test_wp7_trading.py`

- [ ] **步骤 1**：失败测试（注入假 broker/假确认）：
  - 闸门链：sim/live 模式检查 → `risk.pre_trade_checks`（8 规则，参数对齐 WP3 用法）→ kill 文件 → 业务确认（approved 才继续）→ broker 调用；任一前置失败 = 拒单 + 原因，**不触达 broker**。
  - `trade_place(home, {symbol, side, qty, price, mode...})` → 确认流：`confirmation_view()` 出现待确认（operation=下单）→ `decide_confirmation(approved)` → broker 收到订单；`rejected` → 拒单。
  - `account_positions/account_orders/account_funds` 直通 broker 查询（mode 文件约束同 WP6）。
- [ ] **步骤 2**：实现 `platform/server/trading.py`：broker 适配经 `trading_core` 既有 broker 模块（读 `plugins/core/python/trading_core/broker.py` 定接口；子进程或进程内导入，风控需进程内 → 进程内导入 trading_core，broker 的富途调用经 `trading_datasource`）。**业务确认在本进程内**（`store_access.request_confirmation`），Web UI 同进程作答——WP6 的跨进程限制对交易路径消解。
- [ ] **步骤 3**：路由+工具：`trade_place/trade_modify/trade_cancel/account_positions/account_orders/account_funds`（写三个走闸门链 + 确认；读三个 mode 约束直通）。MCP 工具面 27 → **33**（TOOL_COUNT 等同步；写工具描述注明「提交后需在 Web 确认卡片批准」）。
- [ ] **步骤 4**：Web 确认卡片：`execution.jsx` 顶部新增「待确认实盘操作」卡（`useEndpoint("confirmation")` + 10s 轮询；显示 operation/tool/摘要/过期时间；批准/拒绝按钮 → `callApi("confirm-decide", {id, decision})`；显示 `pending:null` 时不渲染）。纯逻辑（轮询间隔/按钮状态）可抽函数测试。
- [ ] **步骤 5**：三套全绿 + sim 冒烟（mock broker）；`git commit -m "feat(platform): 受约束交易工具与交易闸门（风控/kill/业务确认）+ Web 确认卡片"`

### 任务 4：Harness 写通道收窄

**文件**：改 `plugins/engine/src/policy.js`；`agent.cordis.yml`（persona/注释）；`skills/trading-agents/SKILL.md`；测试 `tests/wp7-policy.test.mjs`（或并入 wp6-approval-regression）

- [ ] **步骤 1**：失败测试：live/sim 下 `mcp__futu__trading_*`/`sim_trade_*` → guard 返回拒绝文案（含「请通过工作台交易：quantwb 的 trade_* 工具或计划执行」）；`mcp__futu__*` 只读放行；`tools/pre-execute` 不再对 futu 写发起业务确认（main 的业务确认逻辑对 futu 写退役——交易确认移到服务侧）。
- [ ] **步骤 2**：实现 `policy.js`：`accountTool` 写类 → guard 拒绝 + 指引；删除 pre-execute 的 requestConfirmation 分支（或保留给非 futu 写工具——当前无此类，则整段移除并注明）。
- [ ] **步骤 3**：persona/skill 更新：「交易走工作台通道（trade_* 工具需 Web 确认 / 计划执行）」；`agent.cordis.yml` futu-mcp 行注释标注「只读研究通道；写类被 policy 拒绝」。
- [ ] **步骤 4**：三套全绿；`git commit -m "feat(engine): 富途写通道收窄至工作台（policy 拒绝并指引）"`

### 任务 5：一键安装（提示词 + 安装器扩展）

**文件**：创建 `install/HARNESS_SETUP.md`；改 `scripts/install_plugins.py`（或新增 `scripts/install_platform.py`）；`install.sh`/`install.ps1`；测试 `tests/test_core_wp7_install.py`

- [ ] **步骤 1**：失败测试：`scripts/install_platform.py --home <tmp> --skip-service`（幂等）：创建/复用 venv → `pip install -r platform/requirements.txt`（--skip-deps 可跳）→ `npm install --prefix platform/web` + `run build`（--skip-web 可跳）→ 提示启动命令与 preset 行启用方法；重复运行零变更退出 0。离线可测部分：参数解析、幂等标记、路径拼接。
- [ ] **步骤 2**：实现脚本（真实网络步骤用 `--skip-*` 分层，便于测试与 CI）。
- [ ] **步骤 3**：`install/HARNESS_SETUP.md`：一段可直接粘贴给 Harness 的提示词——引导 Harness 依次：①拉取/更新仓库；②运行 `scripts/install_plugins.py`（既有）+ `scripts/install_platform.py`；③启用 preset 行（fin-data/engine/quant-platform-mcp）；④后台启动服务并 `/healthz` 验证；⑤新建会话并验证 `mcp__quantwb__*` 工具面；⑥把结果汇报给用户。提示词含幂等与失败排查指引（指向 RUNBOOK）。
- [ ] **步骤 4**：`install.sh`/`install.ps1` 尾部追加「platform 服务」可选段（调用 install_platform.py）。
- [ ] **步骤 5**：三套全绿；`git commit -m "feat(install): 一键安装提示词与平台安装器"`

### 任务 6：文档与验收

- [ ] `docs/architecture.md`（新拓扑/调度/交易闸门/通道收窄）、`docs/RUNBOOK.md`（服务=调度器；factors-history 查询）、`README.md`、`docs/HANDOVER.md`、`agent.cordis.yml` 注释。
- [ ] 验收记录（本文件末节）：对照规格 §四 逐条，粘证据；§5.3 人工清单遗留项标注。
- [ ] `git commit -m "docs: WP7 文档修订与验收记录"`

### 执行顺序

1 → 2 → 3 → 4 → 5 → 6 严格串行（同仓库）。每任务一个子代理 + 两阶段审查（先规格后质量）。
