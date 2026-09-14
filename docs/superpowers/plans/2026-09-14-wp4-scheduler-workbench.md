# WP4 调度与工作台 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。
> **全局约定**：见 `2026-09-14-platform-plan-index.md`。**本包含 UI 任务，2.1 UI 文案规范在此强制执行**。

**目标：** 无 LLM 守护进程（调度/心跳/作业链/指令队列/告警）+ 工作台 4 个受约束 RPC + 「计划」「调度」两个新页签 + 审计链升级 + 既有页面文案清理（规格 §八、§十 WP4）。

**架构：** daemon = `python -m trading_core.daemon`（单进程轮询，无 LLM）；工作台→daemon 唯一通道 = 指令目录 `~/.dsh/trading-commands/`（原子写 JSON，5 种白名单指令）；Host RPC 新增 4 端点，读 SQLite 与 daemon 状态文件，写只经指令文件。Host 调 Python 复用既有 venv + `python -m trading_core` 模式（同 `plugins/engine/src/tools.js:16-27` 的 runQuant 模式）。

**插入点锚点（2026-09-14 定位）：**
- `plugins/workbench/src/client.js:995-999` 页签数组（`{ id: "audit", label: "审计" }` 前插入两行）；
- `plugins/workbench/src/rpc.js` `createRpcFetchHandler` 内的 endpoint 分派（`switch-mode` 分支旁加 4 个分支）；
- `plugins/workbench/src/endpoints.js` `ENDPOINTS` 数组与 `ENDPOINT_SHAPE`；
- `plugins/workbench/src/index.js:18-19` 循环注册（ENDPOINTS 加入即自动挂载）；
- Client 调用形状（architecture.md 既定）：`ctx.connection.rpc.call("/api", "trading-workbench/<endpoint>", payload)`。

---

### 任务 0：依赖核验与运行环境探测

**文件：** 创建 `tests/test_core_wp4_locks.py`；创建 `plugins/core/python/trading_core/platform_notify.py`

- [ ] 步骤 1：失败测试：

```python
"""WP4 依赖锁定：指令白名单、RPC 端点名、桌面通知命令探测。全部离线。"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import commands, daemon  # noqa: E402


class Wp4Locks(unittest.TestCase):
    def test_command_whitelist_exactly_five(self):
        self.assertEqual(set(commands.COMMANDS),
                         {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"})

    def test_daemon_paths(self):
        import os
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
        self.assertTrue(daemon.heartbeat_path(home).endswith("trading-daemon.json"))
        self.assertTrue(daemon.commands_dir(home).endswith("trading-commands"))

    def test_notify_command_probe(self):
        # 平台无桌面通知命令时必须返回 None（可选能力，缺失不报错）
        cmd = __import__("trading_core.platform_notify", fromlist=["x"]).detect()
        self.assertTrue(cmd is None or isinstance(cmd, str))
```

- [ ] 步骤 2：FAIL；步骤 3：实现——`commands.py` 先放 `COMMANDS = {...}`（任务 2 补行为）；`daemon.py` 先放路径助手（任务 1 补循环）；`platform_notify.py`：

```python
"""可选桌面通知：探测平台命令，缺失返回 None（YAGNI：不发邮件/短信）。"""
import shutil


def detect():
    for cmd, arg in (("osascript", "-e"), ("notify-send",), ("kdialog",)):
        if shutil.which(cmd):
            return cmd
    return None
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): WP4 依赖锁定（指令白名单/路径/通知探测）"`

### 任务 1：daemon.py 调度循环

**文件：** 修改 `daemon.py`；测试 `tests/test_core_daemon.py`

- [ ] 步骤 1：失败测试（假时钟 + 假作业注册表：到点触发作业链、写心跳、市场休市跳过）：

```python
"""daemon 单测：假时钟驱动一轮调度；心跳与作业历史落盘；休市跳过。"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import daemon, store  # noqa: E402


class DaemonTest(unittest.TestCase):
    def test_tick_runs_due_jobs_and_writes_heartbeat(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = store.connect(str(home / "t.sqlite"))
        store.upsert_calendar(conn, "SH", [{"day": "2026-09-14", "trade_date_type": "WHOLE",
                                            "trade_second": 14400}])
        ran = []
        jobs = {"SH": [{"name": "sync_bars", "at": "16:00",
                        "fn": lambda ctx: ran.append("sync") or {"ok": True}}]}
        clock = {"now": "2026-09-14 16:00:30"}
        daemon.tick(conn, home=str(home), jobs=jobs, now=lambda: clock["now"])
        self.assertEqual(ran, ["sync"])
        hb = json.loads((home / "trading-daemon.json").read_text())
        self.assertEqual(hb["last_job"], "sync_bars")
        self.assertIn("heartbeat", hb)

    def test_closed_market_skips(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = store.connect(str(home / "t.sqlite"))
        store.upsert_calendar(conn, "SH", [{"day": "2026-09-14", "trade_date_type": "CLOSE",
                                            "trade_second": 0}])
        ran = []
        daemon.tick(conn, home=str(home),
                    jobs={"SH": [{"name": "sync_bars", "at": "16:00",
                                  "fn": lambda ctx: ran.append("x") or {}}]},
                    now=lambda: "2026-09-14 16:00:30")
        self.assertEqual(ran, [])  # 休市不跑
```

- [ ] 步骤 2：FAIL；步骤 3：实现 `daemon.py`：

```python
"""调度守护进程（规格 §8.1）：无 LLM 单进程；按交易日历触发作业链；
心跳落 ~/.dsh/trading-daemon.json；指令目录轮询在 commands 模块。"""
import json
from pathlib import Path

from . import store

JOBS_DEFAULT = {
    "SH": [{"name": "sync_bars", "at": "16:00", "cmd": ["sync-bars", "--tickers", "@watchlist"]},
            {"name": "sync_fundamentals", "at": "16:00", "cmd": ["fundamentals", "--tickers", "@watchlist"]},
            {"name": "merge_announcements", "at": "16:05", "cmd": ["merge-announcements", "--period", "@latest-quarter"]},
            {"name": "quality", "at": "16:10", "cmd": ["quality", "--market", "SH"]}],
    "HK": [{"name": "sync_bars", "at": "16:30", "cmd": ["sync-bars", "--tickers", "@watchlist"]}],
    "US": [{"name": "sync_bars", "at": "05:30", "cmd": ["sync-bars", "--tickers", "@watchlist"]}],
}


def heartbeat_path(home): return Path(home) / "trading-daemon.json"
def commands_dir(home): return Path(home) / "trading-commands"


def write_heartbeat(home, payload):
    p = heartbeat_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def tick(conn, home, jobs=None, now=None):
    """一轮调度：对每个市场判断「今日为交易日 且 当前时间 ≥ at 且 今日未跑」，
    满足则执行并记录。now 注入便于假时钟测试。"""
    now = now or _real_now
    jobs = jobs or JOBS_DEFAULT
    state = store.kv_get(conn, "daemon:state", default={"ran": {}})
    stamp = now()
    for market, chain in jobs.items():
        if not store.is_trading_day(conn, market, stamp[:10]):
            continue
        for job in chain:
            key = f"{market}:{job['name']}:{stamp[:10]}"
            if state["ran"].get(key) or stamp[11:16] < job["at"]:
                continue
            _run_job(conn, job, home)
            state["ran"][key] = stamp
    store.kv_set(conn, "daemon:state", state)
    write_heartbeat(home, {"heartbeat": stamp,
                           "last_job": store.kv_get(conn, "daemon:last_job", ""),
                           "next": "见 trading-platform.json"})
    return state


def _run_job(conn, job, home, runner=None):
    """fn 形式直接调用（测试注入）；cmd 形式经 runner 跑 CLI 子进程（任务 4 接线）。"""
    if "fn" in job:
        job["fn"]({"conn": conn, "home": home})
    elif runner:
        runner(job["cmd"])
    store.kv_set(conn, "daemon:last_job", job["name"])


def _real_now():
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): daemon 调度循环（假时钟可测/休市跳过/心跳）"`

### 任务 2：commands.py 指令目录

**文件：** 修改 `commands.py`；测试 `tests/test_core_commands.py`

- [ ] 步骤 1：失败测试：

```python
"""指令目录：白名单校验、nonce 幂等、原子写、轮询消费、processed 去重。"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import commands, daemon  # noqa: E402


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_poll(self):
        nonce = commands.write_command(self.home, "kill", {"nonce": "n1"})
        self.assertTrue(nonce)
        handled = commands.poll(self.home, handler=lambda cmd: {"ok": True})
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["type"], "kill")
        self.assertEqual(commands.poll(self.home, handler=lambda c: c), [])  # processed 去重

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError):
            commands.write_command(self.home, "rm_rf", {})
```

- [ ] 步骤 2：FAIL；步骤 3：实现 `commands.py`：

```python
"""指令目录（规格 §8.2）：工作台 → daemon 的唯一通道。白名单外一律拒绝；
nonce + processed/ 目录保证幂等；原子写防半文件。"""
import json, uuid
from pathlib import Path

COMMANDS = {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"}


def _dir(home): return daemon.commands_dir(home)


def write_command(home, type_, payload):
    if type_ not in COMMANDS:
        raise ValueError(f"指令不在白名单：{type_}")
    d = _dir(home)
    (d / "pending").mkdir(parents=True, exist_ok=True)
    nonce = payload.get("nonce") or uuid.uuid4().hex
    body = json.dumps({"type": type_, "nonce": nonce, **payload},
                      ensure_ascii=False, sort_keys=True)
    target = d / "pending" / f"{nonce}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return nonce


def poll(home, handler):
    """轮询 pending/，逐个 handler 处理；成功移入 processed/（以 nonce 去重）。"""
    pending = _dir(home) / "pending"
    processed = _dir(home) / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(pending.glob("*.json")):
        try:
            cmd = json.loads(f.read_text(encoding="utf-8"))
            if (processed / f.name).exists():
                f.unlink()
                continue
            result = handler(cmd)
            out.append({**cmd, "result": result})
            f.replace(processed / f.name)
        except Exception as error:  # noqa: BLE001 —— 单条指令失败不阻塞其余
            out.append({"file": f.name, "error": str(error)[:160]})
    return out
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): 指令目录（5 白名单/nonce 幂等/processed 去重）"`

### 任务 3：alerts.py

**文件：** 创建 `alerts.py`；测试 `tests/test_core_alerts.py`

- [ ] 步骤 1：失败测试：

```python
"""告警：分级落表、critical 置心跳标志、可选桌面通知（探测为 None 时不发）。"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import alerts, store  # noqa: E402


class AlertsTest(unittest.TestCase):
    def test_level_store_and_critical_flag(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = store.connect(str(home / "t.sqlite"))
        alerts.emit(conn, home=str(home), level="warn", title="缺口", detail="600519 缺 2 日")
        alerts.emit(conn, home=str(home), level="critical", title="对账差异", detail="AAPL −20")
        rows = alerts.list_recent(conn, limit=10)
        self.assertEqual([r["level"] for r in rows], ["critical", "warn"])  # 倒序
        hb = json.loads((home / "trading-daemon.json").read_text())
        self.assertTrue(hb["critical"])  # critical 置心跳标志位（工作台红点依据）
```

- [ ] 步骤 2：FAIL；步骤 3：实现——store v3 `_SCHEMA` 再追加：

```sql
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, title TEXT NOT NULL,
  detail TEXT, created_at TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0);
```

`alerts.py`：

```python
"""告警（规格 §8.4）：info/warn/critical 落表；critical 置心跳标志位；
桌面通知为可选能力（platform_notify.detect() 为 None 时不发）。"""
import datetime as dt
import json
import subprocess

from . import platform_notify


def emit(conn, home, level, title, detail=""):
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO alerts(level,title,detail,created_at) VALUES(?,?,?,?)",
                 (level, title, detail, now))
    conn.commit()
    hb_path = Path(home) / "trading-daemon.json"
    hb = {}
    if hb_path.exists():
        hb = json.loads(hb_path.read_text(encoding="utf-8"))
    if level == "critical":
        hb["critical"] = True
        hb["critical_title"] = title
    hb.setdefault("heartbeat", now)
    tmp = hb_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
    tmp.replace(hb_path)
    _maybe_desktop_notify(level, title, detail)


def list_recent(conn, limit=50):
    rows = conn.execute("SELECT level,title,detail,created_at FROM alerts"
                        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"level": r["level"], "title": r["title"], "detail": r["detail"],
             "created_at": r["created_at"]} for r in rows]


def _maybe_desktop_notify(level, title, detail):
    if level != "critical":
        return
    cmd = platform_notify.detect()
    if not cmd:
        return
    text = f"[量化] {title}"
    try:
        if cmd == "osascript":
            subprocess.run(["osascript", "-e", f'display notification "{text}"'], timeout=5)
        else:
            subprocess.run([cmd, text], timeout=5)
    except Exception:
        pass  # 通知失败不影响告警落库
```

（`alerts.py` 顶部需 `from pathlib import Path`。）

- [ ] 步骤 4：PASS；`git commit -m "feat(core): 告警分级落库与心跳标志"`

### 任务 4：daemon 执行体接线 + `__main__` 常驻

**文件：** 修改 `daemon.py`、新增 `trading_core/daemon_main.py`（或并入 `cli.py` 的 `daemon` 子命令）；测试追加

- [ ] 步骤 1：失败测试（`_run_job` 对 `cmd` 形式的作业调子进程——注入 runner；对 `fn` 形式直接调）：

```python
    def test_run_job_via_runner(self):
        seen = []
        job = {"name": "sync", "cmd": ["sync-bars", "--tickers", "600519"]}
        daemon._run_job(None, job, "/tmp", runner=lambda cmd: seen.append(cmd))
        self.assertEqual(seen[0][0:2], ["sync-bars", "--tickers"])
```

- [ ] 步骤 2：FAIL；步骤 3：实现：`_run_job(conn, job, home, runner=None)` —— `runner = runner or _subprocess_runner`；`_subprocess_runner(cmd)` 用 `subprocess.run([sys.executable, "-m", "trading_core", *cmd], timeout=900)`（替 `@watchlist` 占位为配置关注池）；`cli.py` 加 `daemon` 子命令（`--once` 跑一轮 / 默认常驻 60s 轮询 + `commands.poll` 分派到对应 CLI 动作：`execute_plan`→按 nonce 读计划执行、`kill`→建 kill 文件、`unkill`→删、`cancel_plan`→撤余单、`run_job`→立即跑指定作业）。
- [ ] 步骤 4：PASS；`git commit -m "feat(core): daemon 执行体接线与常驻入口"`

### 任务 5：Host RPC —— 4 个受约束端点

**文件：** 修改 `plugins/workbench/src/endpoints.js`、`rpc.js`；创建 `plugins/workbench/src/pycore.js`；测试 `plugins/workbench/tests` 既有测试风格（`tests/*.test.mjs`）新增 `tests/workbench-wp4.test.mjs`

- [ ] 步骤 1：失败测试（mock pythonCall，断言 4 端点分派、plan-execute 校验 payload、ENDPOINT_SHAPE 扩展）：

```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { ENDPOINTS, matchesShape } from "../src/endpoints.js";

test("wp4 endpoints registered with shape", () => {
  for (const e of ["plan", "plan-execute", "schedule", "reconcile"]) {
    assert(ENDPOINTS.includes(e), e);
  }
  assert(matchesShape("plan", { plans: [], alerts: [] }));
  assert(matchesShape("schedule", { heartbeat: {}, jobs: [] }));
  assert(matchesShape("reconcile", { diffs: [], tca: {} }));
});
```

- [ ] 步骤 2：FAIL；步骤 3：实现：
  - `endpoints.js`：`ENDPOINTS` 追加 4 项；`ENDPOINT_SHAPE` 追加 `plan: ["plans","alerts"]`、`schedule: ["heartbeat","jobs"]`、`reconcile: ["diffs","tca"]`（`plan-execute` 非缓存端点不进 SHAPE）；
  - `pycore.js`：完整文件——

```javascript
// Host → trading-core 子进程桥（模式同 engine/src/tools.js 的 runQuant）。
import { execFile } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);

export function pythonHome() {
  return process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
}

export function pythonBin() {
  return path.join(pythonHome(), "trading-venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
}

export async function pycore(args, { timeout = 60_000 } = {}) {
  const { stdout } = await run(pythonBin(), ["-m", "trading_core", ...args],
    { timeout, maxBuffer: 8 * 1024 * 1024 });
  const start = stdout.indexOf("{");
  if (start < 0) throw new Error(`trading_core ${args[0]} 无 JSON 输出`);
  const parsed = JSON.parse(stdout.slice(start));
  if (parsed.error) throw new Error(`trading_core: ${parsed.error}`);
  return parsed;
}
```

  - `rpc.js` 分派分支（`switch-mode` 分支旁）：

```javascript
      if (endpoint === "plan") {
        const value = await cached(endpoint, payload, force,
          () => deps.pycore(["quality", "--market", "SH", "--symbols", "@watchlist-js",
                             "--start", recentStart(), "--end", today()])
            .then(() => deps.readPlanSnapshot()), 4001);
        return { ok: true, value };
      }
```

**说明（写进计划，执行者照做）**：`plan/schedule/reconcile` 三个读端点由 `deps.pycore` + `deps.readPlanSnapshot()/deps.readScheduleSnapshot()/deps.readReconcileSnapshot()` 提供——新增 `plugins/workbench/src/corebridge.js`，内部 `pycore(["quality", ...])` 等命令 + 直接读 `~/.dsh/trading-data/trading.sqlite`？**否——Node 不直接读 SQLite**。统一约定：**在 `trading_core/cli.py` 增加三个只读子命令 `snapshot-plan / snapshot-schedule / snapshot-reconcile`（JSON 输出，读 store 只读连接），Host 一律经 `pycore` 取数**。`plan-execute` 特殊分支（同 `switch-mode` 不走缓存）：校验 `payload.plan_hash/expected_mode/confirmation` → live 且 confirmation !== "确认执行" 拒绝 → `commands.write_command(home, "execute_plan", {plan_hash, expected_mode, nonce})` → 返回 `{queued: true, nonce}`（不等待执行结果，状态由 plan 端点轮询）。
- [ ] 步骤 4：PASS（含既有 `workbench.test.mjs` 不回归）；`git commit -m "feat(workbench): 4 个受约束 RPC（plan/plan-execute/schedule/reconcile）"`

### 任务 6：Client「计划」页签（文案规范应用）

**文件：** 修改 `plugins/workbench/src/client.js`（L995-999 页签数组 + 新组件 + 主 switch 分支）

- [ ] 步骤 1：页签数组 L997 后插入：`{ id: "plan", label: "计划" }, { id: "schedule", label: "调度" },`
- [ ] 步骤 2：新组件（完整代码，文案规范已应用——页面只出现数据事实/操作反馈/模式徽章/口令，无任何规格引用与设计注释；设计意图写在注释里）：

```javascript
    // 计划页：目标 vs 实际 diff、逐单预检、执行窄门。
    // 设计依据见 docs/superpowers/plans/2026-09-14-wp4-scheduler-workbench.md（注释，不上屏）。
    function PlanTab() {
      const [plan, setPlan] = React.useState(null);
      const [error, setError] = React.useState("");
      const [confirmText, setConfirmText] = React.useState("");
      const [busy, setBusy] = React.useState(false);
      const load = React.useCallback(() => {
        callApi("plan", {}).then(setPlan).catch((e) => setError(String(e.message || e)));
      }, []);
      React.useEffect(() => { load(); }, [load]);
      if (error) return h("div", { className: "wb-note" }, "计划读取失败：", error);
      if (!plan) return h("div", { className: "wb-muted" }, "加载中…");
      const current = plan.plans && plan.plans[0];
      const live = plan.mode === "LIVE";
      const canExecute = current && current.status === "frozen" && !busy;
      const execute = () => {
        if (live && confirmText !== "确认执行") return;
        setBusy(true);
        callApi("plan-execute", { plan_hash: current.content_hash,
                                  expected_mode: plan.mode,
                                  confirmation: live ? confirmText : undefined })
          .then(() => load())
          .catch((e) => setError(String(e.message || e)))
          .finally(() => setBusy(false));
      };
      return h("div", null,
        current && h("div", { className: "wb-row" },
          h("span", { className: "wb-badge " + (live ? "wb-live" : "wb-sim") }, plan.mode),
          h("span", { className: "wb-mono" }, current.plan_id),
          h("span", { className: "wb-mono" }, current.content_hash),
          h("span", null, current.status),
          h("span", { className: "wb-muted" }, "冻结 ", current.created_at)),
        h("table", { className: "wb-table" },
          h("thead", null, h("tr", null,
            ["标的", "方向", "数量", "限价", "预检"].map((c) => h("th", { key: c }, c)))),
          h("tbody", null, (current.orders || []).map((o) =>
            h("tr", { key: o.symbol + o.side },
              h("td", null, o.symbol), h("td", null, o.side),
              h("td", { className: "wb-num" }, o.qty),
              h("td", { className: "wb-num" }, o.price),
              h("td", null, o.risk_verdict || "—"))))),
        current && current.status === "frozen" && h("div", { className: "wb-row" },
          live && h("input", { value: confirmText, placeholder: "输入：确认执行",
                               onChange: (e) => setConfirmText(e.target.value) }),
          h("button", { disabled: !canExecute || (live && confirmText !== "确认执行"),
                        onClick: execute }, live ? "执行（实时账户）" : "执行计划"),
          h("button", { onClick: () => callApi("plan-execute", {
              plan_hash: current.content_hash, expected_mode: plan.mode,
              action: "cancel" }).then(load) }, "取消计划")),
        busy && h("div", { className: "wb-muted" }, "已提交，等待 daemon 回写状态…"));
    }
```

（`callApi(endpoint, payload)` 为本任务新增的 8 行助手：包装 `ctx.connection.rpc.call("/api", "trading-workbench/" + endpoint, payload)`，提取 `value`；若 client.js 已有等价助手则复用并删除新助手。）
- [ ] 步骤 3：主 switch 加 `plan` 分支渲染 `PlanTab`；步骤 4：手动 `node --test plugins/workbench/tests/workbench-wp4.test.mjs` + 现有 `client.test.mjs` PASS；`git commit -m "feat(workbench): 计划页签（diff 表/预检/执行窄门，live 口令）"`

### 任务 7：Client「调度」页签

**文件：** 修改 `client.js`

- [ ] 步骤 1：组件（完整代码）：心跳状态点（`heartbeat` 距今 >5 分钟标红——数据事实提示，允许）、作业历史表、告警列表（critical 红/warn 黄/info 蓝）、kill switch 与 halt 状态行、两个功能按钮「激活 kill switch」「解除」调 `plan-execute` 的 `action: "kill"/"unkill"`；无任何规范解说文案。
- [ ] 步骤 2：switch 分支 + `node --test` PASS；`git commit -m "feat(workbench): 调度页签（心跳/作业/告警/kill）"`

### 任务 8：审计链升级 + 既有页面文案清理

**文件：** 修改 `client.js`（audit 页）、`rpc.js`/`cli.py`（`snapshot-reconcile` 提供 链路数据）

- [ ] 步骤 1：`snapshot-reconcile` 子命令输出 `{diffs, tca, chain:[{plan_id, orders:[{client_order_id,status,broker_order_id,fills:[...]}]}]}`（读 plans/orders/fills 三表）；audit 分支透出。
- [ ] 步骤 2：审计页在既有内容前插入链路区（表格式三级展开，无设计稿解说文案）。
- [ ] 步骤 3：**文案清理（全局约定 2.1）**——对 `client.js` 全量 grep：`示例|设计稿|规格|宁可|窄门|token 说明`，逐一处理：
  - L1573 附近「原始券商响应（N 条，审计核对用）」→ 保留（功能性：说明该区用途）；
  - 「台账回放」「回测」等指标 label 保留（数据事实）；
  - 所有含「设计/规格/原则」字样的字符串 → 删除，语义写入同文件顶部注释块。
- [ ] 步骤 4：`node --test plugins/workbench/tests/` 全 PASS；`git commit -m "feat(workbench): 审计三级链路；按文案规范清理页面字符串"`

### 任务 9：端到端 + 3 交易日 runbook

- [ ] 步骤 1：离线端到端测试 `tests/test_core_wp4_e2e.py`：写指令文件 kill → `poll` → 断言 kill 文件存在 + 心跳标志 → 写 execute_plan（假 runner）→ 断言状态机推进 → `snapshot-plan` JSON 含最新状态。
- [ ] 步骤 2：手动 runbook（sim，无人干预）：`python -m trading_core daemon` 常驻 → 三个交易日不开对话 → 第三日检查：作业链 ran 记录、alerts 为空、对账零差异、工作台计划页显示最新冻结计划。结果记入本文件验收记录。
- [ ] 步骤 3：`git commit -m "test(core): WP4 指令端到端；验收记录"`

### WP4 验收记录（执行时填写）

- 3 交易日无人干预运行结果：（执行时粘贴心跳/作业/对账 JSON）
- 对账差异：目标零差异，非零须逐条归因
