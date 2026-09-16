# WP9 sim 全自动流水线 实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md` §四。全局约定见 `2026-09-16-wp9-15-plan-index.md`（表迁移模式、跨 WP 契约、UI 文案规范）。
> 三套测试保持全绿；提交 `feat(core|platform): 摘要`。

**目标**：auto_pipeline 开关打开后，sim 模式从收盘数据到次日执行全程零人工；开关关闭时行为与现状逐字节一致。

**架构**：daemon 作业链扩展（build_plan/auto_execute/reconcile 三类新作业，config 驱动装配）+ 指令轮询并入服务调度器 + plans 表 origin/market 来源隔离。auto_execute 与人工点击落同一张 execute_plan 指令文件，复用全部窄门。

---

### 任务 1：plans 表 origin/market 列 + store helpers

**文件**：修改 `plugins/core/python/trading_core/store.py`（`_SCHEMA` 后注释区、`migrate`、新查询函数）；测试 `tests/test_core_store.py` 追加。

- [ ] **步骤 1：失败测试**

```python
def test_plans_origin_market_columns(self):
    conn = store.connect(":memory:")
    store.migrate(conn)  # 重复 migrate 幂等
    store.migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
    self.assertIn("origin", cols); self.assertIn("market", cols)
    # 既有手工路径不传 origin/market → 默认 manual/NULL
    pid = _insert_plan(conn)  # 既有 INSERT 语句的辅助（直接 SQL）
    row = conn.execute("SELECT origin, market FROM plans WHERE plan_id=?", (pid,)).fetchone()
    self.assertEqual(row["origin"], "manual"); self.assertIsNone(row["market"])

def test_get_latest_auto_plan(self):
    # get_latest_auto_plan(conn, market, as_of, status="frozen")
    # → origin=auto 且 market=? 且 as_of=? 且 status=? 的最新一条；无则 None
    ...
def test_cancel_stale_auto_plans(self):
    # cancel_stale_auto_plans(conn, today) → as_of < today 的 frozen auto 计划置 cancelled
    # 返回被置 cancelled 的 plan_id 列表；手工计划（origin=manual）不动
    ...
```

- [ ] **步骤 2：运行验证失败**：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_store -v` → FAIL（列/函数不存在）。
- [ ] **步骤 3：实现**：按索引 §2.2 引入 `_add_columns`；`SCHEMA_VERSION = 4`；实现 `get_latest_auto_plan`、`cancel_stale_auto_plans`（沿用 `upsert_plan_status` 的 UPDATE 口径）。
- [ ] **步骤 4：测试通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): plans 表 origin/market 列与 auto 计划查询/过期 helpers"`

### 任务 2：watchlist 组合适配器

**文件**：修改 `plugins/core/python/trading_core/strategies.py`；测试 `tests/test_core_strategies.py` 追加。

- [ ] **步骤 1：失败测试**

```python
def test_watchlist_rsi_weights(self):
    # 注册表含 watchlist_rsi；universe(conn, as_of) 返回关注池该市场标的
    # target_weights：BUY 信号等权 1/N_buy、HOLD/SELL=0（卖出由 planner diff 处理减仓，
    # 策略层只决定目标持仓权重）；无 BUY → {}（全现金）
    # bars 不足 25 根的标的跳过（宁缺毋假）
    ...
```

- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`@strategy("watchlist_rsi") class WatchlistRsiStrategy`（复用 `SingleTicker.signal` 的 rsi 判定；`universe` 经 `daemon.platform_config(home)["watchlist"]` 的市场分片——签名带 `home`，从 `conn`/调用方注入，勿在模块级读文件）。
- [ ] **步骤 4：测试通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): watchlist_rsi 组合策略适配器"`

### 任务 3：auto_pipeline 配置读取与校验

**文件**：修改 `plugins/core/python/trading_core/daemon.py`（`platform_config` 旁）；测试 `tests/test_core_daemon.py` 追加。

- [ ] **步骤 1：失败测试**

```python
def test_auto_pipeline_config(self):
    # auto_pipeline_config(home) → {"enabled": bool, "strategies": [...],
    #   "exec_at": {"SH": "09:35", ...}, "reconcile_at": "19:00"}
    # 缺省（无键/无文件）→ enabled=False + 默认时刻表（SH 09:35/HK 09:45/US 22:35/19:00）
    # 非法（enabled 非 bool/strategies 非列表/exec_at 非 HH:MM）→ ValueError（fail-closed，
    # 调用方捕获后跳过当日 auto 作业并 warn 告警）
    ...
```

- [ ] **步骤 2：验证失败** → **步骤 3：实现** `auto_pipeline_config(home)`（独立函数，校验枚举/时刻格式 `re` 匹配 `^\d{2}:\d{2}$`）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): auto_pipeline 配置读取与校验"`

### 任务 4：build_plan 作业（CLI `plan-auto`）

**文件**：修改 `plugins/core/python/trading_core/planner.py`（签名扩展）、新建作业实现（`daemon.py` 内函数 or `cli.py` 直连）；`cli.py` 加子命令；测试 `tests/test_wp9_plan_auto.py`。

- [ ] **步骤 1：失败测试**（假库 + 注入 broker_positions/prices）：
  ① 数据就绪门：关注池某标的最后 bar 落后日历上一交易日 → 跳过 + warn 告警 + 不产生计划；
  ② 就绪 → 策略权重 → `build_and_freeze(origin="auto", market="SH")` 落库，plans 行含 origin/market；
  ③ 过期语义：先插一条昨日 frozen auto 计划 → 被 `cancel_stale_auto_plans` 置 cancelled；手工 frozen 计划不动；
  ④ planner 既有调用（无 origin/market）回归通过。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：
  - `planner.build_and_freeze` 增加 `origin`/`market` keyword 参数（INSERT 列集扩展）；
  - CLI `plan-auto --market SH [--home H]`：读 auto_pipeline 配置（disabled→exit 0 静默）→ 数据就绪门（`quality.freshness` 对关注池逐标的 vs `store.last_trading_day`）→ 过期 → `strategies.REGISTRY[cfg.strategy].target_weights` → `prices = {s: store.last_close(conn, s)}`（PIT 最近收盘，取不到该标的跳过并在结果标注）→ `broker_positions` 用 `core_broker.positions`（sim/live 模式文件决定；查询失败→跳过+告警，不用本地台账）→ freeze。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): plan-auto 作业——策略驱动计划生成+数据就绪门+过期语义"`

### 任务 5：auto_execute 作业（CLI `auto-execute`，九守卫）

**文件**：`cli.py`、`daemon.py`；测试 `tests/test_wp9_auto_execute.py`。

- [ ] **步骤 1：失败测试**（假时钟 + 假库 + 临时 home）：
  ① 九守卫逐项跳过：enabled=False / mode=live（告警文案含「等待人工执行」）/ kill 文件存在 / halt 置位 / 无 auto frozen 计划 / 计划 market≠作业 market / 当日已执行（kv `auto_exec:<market>:<date>` 已存在）/ expected_mode 不匹配 / **超出执行窗口**（`exec_window_minutes` 默认 30，规格 §4.3 守卫 9——tick-first 补跑不得在错误时点执行）——各产 info 告警、**零指令文件**；
  ② 全部通过 → `~/.dsh/trading-commands/pending/` 出现 `execute_plan` 指令，payload 含 plan_hash 与 expected_mode=sim；kv ran 标记写入；
  ③ 与人工路径同构：指令文件被 `handle_command` 消费后计划 executing（复用 test_core_commands 假件）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现** `cli auto-execute --market SH`：守卫顺序按规格 §4.3 编号，任一失败 `alerts.emit(info)` 后 exit 0（**不是错误**，是当日不执行）；通过后 `commands.write_command(home, "execute_plan", {...})`。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): auto-execute 作业——九守卫（含执行窗口）+指令文件复用人工窄门"`

### 任务 6：reconcile/tca/digest 入链 + build_jobs 装配

**文件**：`daemon.py`（`build_jobs`）、`cli.py`；测试 `tests/test_wp9_build_jobs.py`。

- [ ] **步骤 1：失败测试**：
  ① `build_jobs(home)`：enabled=False → 仅 JOBS_DEFAULT（与现 JOBS_DEFAULT 逐键相等）；
  ② enabled=True → 各市场链尾追加 `{"name": "build_plan", "at": <factors+5min>}`、`sentiment_snapshot`（占位名，WP11 接管 fn）、各市场 `{"name": "auto_execute", "at": cfg.exec_at[market]}`；全局 `{"GLOBAL": [reconcile_job]}`（reconcile_at，tick 对非市场键跳过日历判定——需在 tick 里支持 `"GLOBAL"` 链不查交易日历，只有 at 判定）；
  ③ `tick` 缺省 jobs 改调 build_jobs（注入 now 的既有测试回归）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`build_jobs` 深拷贝 JOBS_DEFAULT 后按配置合并；GLOBAL 链在 `tick` 的 `is_trading_day` 分支豁免（`market == "GLOBAL"` 直接过）；reconcile 作业 cmd `["reconcile-daily"]`（新 CLI：`reconcile-diff` 既有逻辑的定时包装 + `tca` + digest 摘要落 kv `daily:digest`，格式 `{"as_of":…, "orders":…, "diffs":…, "tca":…}` 供 pipeline 端点与对话读取）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): build_jobs 配置驱动装配+reconcile/tca/digest 入链"`

### 任务 7：指令轮询并入服务调度器

**文件**：`daemon.py`（`poll_commands`）、`platform/server/scheduler.py`（`build_tick`）；测试 `tests/test_wp9_scheduler_poll.py`。

- [ ] **步骤 1：失败测试**：
  ① `poll_commands(conn, home)`：pending 一条 execute_plan → handle_command 消费 → 文件移 processed/、返回 1；坏指令（白名单外 type）→ 告警 + 移 processed（不阻塞）；
  ② `build_tick` 的 tick()：先 `daemon.tick` 再 `poll_commands`（假件注入断言调用顺序）；tick 抛异常不阻断 poll（各自 try/except + last_error 记录）；
  ③ 双进程幂等：同 nonce 文件被 poll 两次只执行一次（processed/ 同名跳过——commands.poll 既有语义回归）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**；scheduler tick 包装两段 try/except，异常写 `kv daemon:last_error`（300 字符截断，沿用 healthz 口径）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(platform): 指令轮询并入服务内调度器（修复单进程断点）"`

### 任务 8：端到端假时钟两交易日

**文件**：测试 `tests/test_wp9_e2e.py`。

- [ ] **步骤 1：编写测试**（全程假件：broker=内存账本、bars=构造数据、时钟=注入）：
  - 日 D1 16:20：tick → build_plan 产出 frozen auto 计划（origin/market 正确）；
  - 日 D1 19:00：tick → reconcile/digest ran；
  - 日 D2 09:35：tick → auto_execute 落指令 → poll_commands 消费 → execute.run → 订单落 OMS submitted → kv ran；
  - 断言全程无人工输入；`enabled=False` 时同一时间线 tick 后无计划、无指令（回归）。
- [ ] **步骤 2：运行**：预期先 FAIL（若任务 1–7 正确则直接 PASS，允许跳过红步但必须真实跑出绿）。
- [ ] **步骤 3：三套绿 + 手册快照**：`README.md` 追加 auto_pipeline 配置样例（§4.1 原文 JSON）与「打开步骤」三行；`docs/HANDOVER.md` 追加开关与 exec_at 时区注意。
- [ ] **步骤 4：Commit**：`git commit -m "feat(core): WP9 端到端假时钟两交易日零人工闭环+文档"`

### WP9 验收（对照规格 §4.7）

- [ ] 九守卫（含执行窗口半开区间边界）/数据门/过期/plans 列/轮询并入全部有测试；
- [ ] `enabled=false` 全平台行为回归一致；
- [ ] 三套测试全绿；边界检查：auto 链路零 LLM、零绕过风控（订单全部经 `execute.run` 的 pre_trade_checks）。
