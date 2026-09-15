# WP1 数据基座 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。
> **全局约定**（UI 文案规范/数据可行性协议/工程约定）见 `2026-09-14-platform-plan-index.md`，本计划同等受其约束。

**目标：** 建成 trading-core 的数据基座——PIT SQLite 存储、交易日历、三市场行情/复权/财务同步（含全量回填与断点续传）、财报公告日双源合并、数据质量报告；随安装器分发（LIBRARIES 机制）。

**架构：** 新建 `plugins/core/python/trading_core/`（库，非 Harness 插件），依赖既有 `trading_datasource`（唯一取数实现，规格 §3.3）。SQLite 单库 WAL 模式（规格 §4.1 六张表），三条硬规则（PIT 纪律/复权统一/宁缺毋假）落在 store 层。同步一律支持注入假取数器，测试全部离线（仓库约定：标准库 unittest）。

**技术栈：** Python 3（标准库 sqlite3/argparse/unittest）+ 既有 trading_datasource（futu_mcp/market）+ akshare 1.18.x（仅公告日期列）。

**规格依据：** `docs/superpowers/specs/2026-09-14-quant-platform-design.md` §四（数据基座）、§13.4/13.5（缺口①与 WP1 行）、§十（WP1 验收标准）。

**运行与测试：**
- 测试（离线，仓库根目录）：`PYTHONPATH=plugins/core/python:plugins/datasource/python ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests -p 'test_core_*.py' -v`
- 全量回归（不得破坏）：`~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v`

---

## 文件结构

| 文件 | 职责 |
|---|---|
| 创建 `plugins/core/package.json` | npm 包元数据（安装器 npm pack 用；非插件，无 cordis 行） |
| 创建 `plugins/core/python/trading_core/__init__.py` | 包标识与版本 |
| 创建 `plugins/core/python/trading_core/store.py` | SQLite 连接/迁移/六张表读写 + PIT 纪律强制 + kv 游标 |
| 创建 `plugins/core/python/trading_core/calendar.py` | 交易日历抓取（quote_trading_days）与落库 |
| 创建 `plugins/core/python/trading_core/sync.py` | bars 增量/回填（断点续传+节流）、复权因子、财务报表、公告日合并、成分股快照 |
| 创建 `plugins/core/python/trading_core/quality.py` | 缺口检测、新鲜度、announced_at 覆盖率、跨源交叉校验、汇总报告 |
| 创建 `plugins/core/python/trading_core/cli.py` | `python -m trading_core` 单入口（子命令 + JSON 输出） |
| 创建 `plugins/core/python/trading_core/__main__.py` | `python -m trading_core` 入口转发 |
| 修改 `scripts/install_plugins.py` | LIBRARIES/UNIFIED_PYTHON 加 core；.pth 多行；自检覆盖 core |
| 创建 `tests/test_core_store.py` | store 层单测 |
| 创建 `tests/test_core_calendar.py` | 日历单测（假取数器） |
| 创建 `tests/test_core_sync.py` | 同步单测（假取数器/假 akshare） |
| 创建 `tests/test_core_quality.py` | 质量报告单测 |
| 创建 `tests/test_core_install.py` | 安装器常量与 .pth 行单测 |
| 创建 `tests/test_core_cli.py` | CLI 冒烟单测 |
| 创建 `tests/test_core_integration.py` | 离线端到端（日历→回填→公告日→质量报告零缺口） |

**既有事实（写代码时直接引用，勿重新发明）：**
- `trading_datasource.market.load_bars(ticker, period, limit)` → `(bars, source, stale)`；bar 字典键为 `{"t","o","h","l","c","v"}`（t 为 `YYYY-MM-DD`）；内部已做三市场路由（富途≤370 根，长历史 A股→新浪、港美→Yahoo）、`to_futu_symbol`、`is_a_share` 唯一实现。
- `trading_datasource.futu_mcp.call_tool(name, arguments)` → 业务 dict；凭证过期自动续期重试一次。
- 富途参数口径（2026-09-14 实测，规格 §13.2）：`quote_history_kline` 的 `ktype` 是整数（日线=2）；`quote_trading_days` 的 `market` 必须大写且必带 `start/end`；`quote_corporate_actions_rehab` 参数 `symbol`（futu 格式），返回 `{"rehabs":[{ex_div_date, cum_forward_adj_factorA, cum_backward_adj_factorA, action_types, ...}]}`；`quote_valuation_index_component_stock_list` 参数 `symbol/limit(≤50)/next_key` 键集分页，返回 `stock_list`（含 `symbol` 字段）。
- `quote_financials_statements` 返回 `report_list[]`（`date_time` 毫秒报告期、`item_list[].display_name/data`），科目名因市场而异（alias 表见 `plugins/workbench/python/quality.py:38`，本计划只取其中 4 键）。
- akshare `stock_yjbb_em(date="YYYYMMDD")` 返回含「股票代码」「公告日期」列的 DataFrame（列名探测，不硬编码）。

---

### 任务 1：core 包骨架 + 安装器接线

**文件：**
- 创建：`plugins/core/package.json`
- 创建：`plugins/core/python/trading_core/__init__.py`
- 修改：`scripts/install_plugins.py`（L18、L22、L25-31、L167-182、L212-214、L225-241、L313-315）
- 测试：`tests/test_core_install.py`

- [ ] **步骤 1：创建包骨架**

`plugins/core/package.json` 完整内容：

```json
{
  "name": "@bstester/dsh-trading-core",
  "version": "0.1.0",
  "description": "量化平台核心库：PIT 存储、交易日历、同步、数据质量（库，非 Harness 插件）",
  "license": "SEE LICENSE IN LICENSE"
}
```

`plugins/core/python/trading_core/__init__.py` 完整内容：

```python
"""trading-core：量化平台核心库（规格 docs/superpowers/specs/2026-09-14-quant-platform-design.md）。

子模块：store（PIT 存储）/ calendar（交易日历）/ sync（同步）/ quality（质量）/ cli（单入口）。
本包不是 Harness 插件：安装器把 python/ 解到 $DSH_HOME/trading-python/core/ 并写入 .pth。
"""

__version__ = "0.1.0"
```

- [ ] **步骤 2：编写失败的安装器测试**

创建 `tests/test_core_install.py`：

```python
"""安装器接线测试：core 必须进入 LIBRARIES/UNIFIED_PYTHON，.pth 必须覆盖两个库。"""
import importlib.util
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "install_plugins_under_test", _REPO / "scripts" / "install_plugins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerCoreWiring(unittest.TestCase):
    def setUp(self):
        self.mod = _load_installer()

    def test_core_in_libraries_and_unified(self):
        self.assertIn("core", self.mod.LIBRARIES)
        self.assertIn("core", self.mod.UNIFIED_PYTHON)

    def test_core_package_name_registered(self):
        self.assertEqual(self.mod.PACKAGE_NAMES.get("core"), "@bstester/dsh-trading-core")

    def test_library_markers_cover_core(self):
        self.assertEqual(self.mod.LIBRARY_MARKERS["core"], "trading_core")
        self.assertEqual(self.mod.LIBRARY_MARKERS["datasource"], "trading_datasource")

    def test_pth_lines_cover_both_libraries(self):
        lines, missing = self.mod.data_layer_pth_lines(_REPO / "nonexistent-home")
        # home 不存在时两个库都缺，但函数必须同时列出两个候选目录
        self.assertEqual(len(lines), 0)
        self.assertEqual(sorted(missing), ["core", "datasource"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 3：运行测试验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_install -v`
预期：FAIL/ERROR —— `LIBRARIES` 无 `core`、`LIBRARY_MARKERS`/`data_layer_pth_lines` 属性不存在。

- [ ] **步骤 4：修改安装器（五处，逐字替换）**

`scripts/install_plugins.py` 修改 1 —— L18 与 L22：

```python
# 旧：
LIBRARIES = ("datasource",)
# 新：
LIBRARIES = ("datasource", "core")
```

```python
# 旧：
UNIFIED_PYTHON = ("datasource", "fin-data")
# 新：
UNIFIED_PYTHON = ("datasource", "fin-data", "core")
```

修改 2 —— L25-31 `PACKAGE_NAMES` 追加一行：

```python
PACKAGE_NAMES = {
    "workbench": "@bstester/dsh-trading-workbench",
    "fin-data": "@bstester/dsh-fin-data",
    "engine": "@bstester/dsh-trading-engine",
    "futu-keepalive": "@bstester/dsh-futu-keepalive",
    "datasource": "@bstester/dsh-datasource",
    "core": "@bstester/dsh-trading-core",
}
```

修改 3 —— 在 `DATA_LAYER_PTH_NAME = "dsh-trading-python.pth"`（L32）下一行新增库标记表与 .pth 行生成函数（供写入与自检共用，单一事实来源）：

```python
# 库目录 → 解包后必须存在的标记（extract 与自检共用）。
LIBRARY_MARKERS = {"datasource": "trading_datasource", "fin-data": "fin_sentiment.py",
                   "core": "trading_core"}


def data_layer_pth_lines(dsh_home):
    """返回应写入 .pth 的目录行与缺失的库。

    返回 (lines, missing)：lines 是已就绪库的绝对路径列表；missing 是未解出的库名。
    datasource 缺失时调用方维持旧行为（返回 None 提示先 install）。
    """
    root = unified_python_root(dsh_home)
    lines, missing = [], []
    for lib in LIBRARIES:  # 只遍历 LIBRARIES：fin-data 是子进程调用，不进 .pth
        marker = LIBRARY_MARKERS[lib]
        if (root / lib / marker).is_dir():
            lines.append(str(root / lib))
        else:
            missing.append(lib)
    return lines, missing
```

修改 4 —— `write_data_layer_pth`（L167-182）整体替换为多行版本：

```python
def write_data_layer_pth(dsh_home):
    """向交易 venv 写入 .pth（每个已解出的库一行），让插件脚本直接 import。

    返回 (site_packages, lines) 或 None（datasource 未解出或 venv 不存在时）。
    """
    lines, missing = data_layer_pth_lines(dsh_home)
    if "datasource" in missing:
        return None
    site = site_packages_dir(dsh_home)
    if site is None:
        return None
    path = site / DATA_LAYER_PTH_NAME
    content = "".join(line + "\n" for line in lines)
    if not path.exists() or read_text(path) != content:
        path.write_text(content, encoding="utf-8")
    return site, lines
```

修改 5 —— `check_install` 三处：
L212-214 跳过库的插件清单检查：

```python
    for plugin, package in PACKAGE_NAMES.items():
        if plugin in ("datasource", "core"):  # 库不装进 profiles，走统一 python 目录
            continue
```

L226 统一数据层存在性检查加入 core：

```python
    for plugin in ("datasource", "fin-data", "core"):
        expect = LIBRARY_MARKERS[plugin]
        if (root / plugin / expect).exists():
            notes.append(f"统一数据层 {plugin}: {root / plugin}")
        else:
            problems.append(f"统一数据层缺少 {plugin}（{root / plugin / expect}）")
```

L236-241 `.pth` 内容比对改用同一助手（消灭两处口径漂移的可能）：

```python
        pth = site / DATA_LAYER_PTH_NAME
        lines, missing = data_layer_pth_lines(dsh_home)
        expected = "".join(line + "\n" for line in lines)
        if not pth.is_file():
            problems.append(f"未写入 .pth：{pth}（跑 `link` 动作）")
        elif read_text(pth) != expected:
            problems.append(f".pth 内容不对：期望 {expected.strip()}，实际 {read_text(pth).strip()}")
        elif missing:
            problems.append(f".pth 缺少库行：{', '.join(missing)}（重跑 `install` 解出）")
```

修改 6 —— `install_plugins()` L313-315 解包循环改用标记表：

```python
        # 统一数据层：解到 <DSH>/trading-python/，各插件在运行时按同一路径定位
        for plugin in UNIFIED_PYTHON:
            extract_python(packed_archives[plugin], plugin, dsh_home,
                           LIBRARY_MARKERS[plugin])
```

- [ ] **步骤 5：运行安装器测试与既有安装测试**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_install tests.test_install -v`
预期：PASS（test_install 是既有套件，若其断言了单行 .pth 内容，按同一 helper 口径修正该断言——期望值改为两行 `datasource\n+core`，断言代码同步更新）。

- [ ] **步骤 6：Commit**

```bash
git add plugins/core/package.json plugins/core/python/trading_core/__init__.py \
        scripts/install_plugins.py tests/test_core_install.py
git commit -m "feat(core): core 库骨架并接入安装器（LIBRARIES/.pth 多行/自检）"
```

---

### 任务 2：store.py —— PIT 存储层

**文件：**
- 创建：`plugins/core/python/trading_core/store.py`
- 测试：`tests/test_core_store.py`

- [ ] **步骤 1：编写失败的存储层测试**

创建 `tests/test_core_store.py`：

```python
"""store 层单测：六张表、PIT 纪律强制、幂等 upsert、kv 游标。全部离线。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import store  # noqa: E402

BARS = [{"t": "2026-09-10", "o": 10.0, "h": 11.0, "l": 9.5, "c": 10.5, "v": 1000.0},
        {"t": "2026-09-11", "o": 10.5, "h": 12.0, "l": 10.2, "c": 11.8, "v": 1200.0}]


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_wal_enabled(self):
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_upsert_bars_idempotent(self):
        self.assertEqual(store.upsert_bars(self.conn, "600519", "1d", BARS, "futu/x"), 2)
        self.assertEqual(store.upsert_bars(self.conn, "600519", "1d", BARS, "futu/x"), 2)
        rows = store.read_bars(self.conn, "600519", "1d", as_of="2026-09-11")
        self.assertEqual([r["t"] for r in rows], ["2026-09-10", "2026-09-11"])
        self.assertEqual(rows[-1]["c"], 11.8)

    def test_read_bars_requires_as_of(self):
        store.upsert_bars(self.conn, "600519", "1d", BARS, "futu/x")
        with self.assertRaises(ValueError):
            store.read_bars(self.conn, "600519", "1d", as_of=None)

    def test_read_bars_respects_as_of(self):
        store.upsert_bars(self.conn, "600519", "1d", BARS, "futu/x")
        rows = store.read_bars(self.conn, "600519", "1d", as_of="2026-09-10")
        self.assertEqual([r["t"] for r in rows], ["2026-09-10"])

    def test_fundamentals_pit_by_announced_at(self):
        store.upsert_fundamentals(self.conn, "600519",
                                  [{"field": "revenue", "period_end": "2026-06-30", "value": 1.0}],
                                  "futu/statements")
        # 未合并公告日：PIT 读取必须为空（宁缺毋假）
        self.assertEqual(store.read_fundamentals(self.conn, "600519", as_of="2026-09-14"), [])
        n = store.set_announced_at(self.conn, "600519", "2026-06-30", "2026-08-28", "akshare/yjbb")
        self.assertEqual(n, 1)
        rows = store.read_fundamentals(self.conn, "600519", as_of="2026-08-27")
        self.assertEqual(rows, [])  # 公告前不可见
        rows = store.read_fundamentals(self.conn, "600519", as_of="2026-08-28")
        self.assertEqual(rows[0]["field"], "revenue")
        self.assertEqual(rows[0]["announced_source"], "akshare/yjbb")

    def test_universe_latest_snapshot(self):
        store.store_universe(self.conn, "2026-08-29", "SH.000300", ["600519", "00700"], "futu/comp")
        store.store_universe(self.conn, "2026-09-12", "SH.000300", ["600519"], "futu/comp")
        snap = store.read_universe(self.conn, as_of="2026-09-13", index_name="SH.000300")
        self.assertEqual(snap["snapshot_date"], "2026-09-12")
        self.assertEqual(snap["symbols"], ["600519"])

    def test_calendar_and_trading_days(self):
        store.upsert_calendar(self.conn, "SH", [
            {"day": "2026-09-11", "trade_date_type": "WHOLE", "trade_second": 14400},
            {"day": "2026-09-12", "trade_date_type": "CLOSE", "trade_second": 0}])
        self.assertTrue(store.is_trading_day(self.conn, "SH", "2026-09-11"))
        self.assertFalse(store.is_trading_day(self.conn, "SH", "2026-09-12"))
        self.assertEqual(store.trading_days(self.conn, "SH", "2026-09-10", "2026-09-12"),
                         ["2026-09-11"])

    def test_is_trading_day_without_calendar_raises(self):
        with self.assertRaises(RuntimeError):
            store.is_trading_day(self.conn, "HK", "2026-09-11")

    def test_kv_roundtrip(self):
        store.kv_set(self.conn, "backfill:daily", {"done": ["600519"], "failed": {}})
        self.assertEqual(store.kv_get(self.conn, "backfill:daily")["done"], ["600519"])
        self.assertIsNone(store.kv_get(self.conn, "nope", default=None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_store -v`
预期：ERROR —— `No module named 'trading_core.store'`。

- [ ] **步骤 3：实现 store.py**

创建 `plugins/core/python/trading_core/store.py`：

```python
"""PIT SQLite 存储层（规格 §四）。三条硬规则在本层强制：

  1. PIT 纪律 —— read_bars/read_fundamentals/read_universe/read_adjustments 必须显式
     as_of，缺省直接拒绝（ValueError），不存在"全量读"口子；
  2. 复权统一 —— bars 一律存原始价，复权因子在 adjustments 表，口径查询时派生；
  3. 宁缺毋假 —— 本层不生成占位行； announced_at 未合并的基本面行对 PIT 读取不可见。
"""
import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(
  symbol TEXT NOT NULL, period TEXT NOT NULL, ts TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL NOT NULL, source TEXT NOT NULL, adj_factor REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY(symbol, period, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS adjustments(
  symbol TEXT NOT NULL, ex_date TEXT NOT NULL,
  cum_forward REAL, cum_backward REAL, actions TEXT, source TEXT NOT NULL,
  PRIMARY KEY(symbol, ex_date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fundamentals(
  symbol TEXT NOT NULL, field TEXT NOT NULL, period_end TEXT NOT NULL,
  announced_at TEXT, announced_source TEXT, value REAL NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY(symbol, field, period_end)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS universe(
  snapshot_date TEXT NOT NULL, symbol TEXT NOT NULL, index_name TEXT NOT NULL,
  bias_note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
  PRIMARY KEY(snapshot_date, symbol, index_name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS calendar(
  market TEXT NOT NULL, day TEXT NOT NULL, trade_date_type TEXT, trade_second INTEGER,
  PRIMARY KEY(market, day)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def db_path(dsh_home=None):
    import os
    home = Path(dsh_home or os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    return home / "trading-data" / "trading.sqlite"


def connect(path=None):
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn):
    conn.executescript(_SCHEMA)
    row = conn.execute("PRAGMA user_version").fetchone()[0]
    if row < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def _require_as_of(as_of):
    if not as_of:
        raise ValueError("PIT 纪律：读取必须显式提供 as_of（规格 §4.2 规则 1）")
    return as_of


def upsert_bars(conn, symbol, period, bars, source):
    rows = [(symbol, period, b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v") or 0.0, source)
            for b in bars]
    conn.executemany(
        "INSERT OR REPLACE INTO bars(symbol,period,ts,open,high,low,close,volume,source)"
        " VALUES(?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def read_bars(conn, symbol, period, as_of, limit=None):
    _require_as_of(as_of)
    sql = "SELECT ts,open,high,low,close,volume,source FROM bars" \
          " WHERE symbol=? AND period=? AND ts<=? ORDER BY ts DESC"
    params = [symbol, period, as_of]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    rows.reverse()  # 升序返回，与 load_bars 口径一致
    return [{"t": r["ts"], "o": r["open"], "h": r["high"], "l": r["low"],
             "c": r["close"], "v": r["volume"], "source": r["source"]} for r in rows]


def last_bar_date(conn, symbol, period):
    row = conn.execute("SELECT MAX(ts) FROM bars WHERE symbol=? AND period=?",
                       (symbol, period)).fetchone()
    return row[0]


def upsert_adjustments(conn, symbol, rows, source):
    params = [(symbol, r["ex_date"], r.get("cum_forward"), r.get("cum_backward"),
               json.dumps(r.get("actions") or [], ensure_ascii=False), source) for r in rows]
    conn.executemany("INSERT OR REPLACE INTO adjustments"
                     "(symbol,ex_date,cum_forward,cum_backward,actions,source)"
                     " VALUES(?,?,?,?,?,?)", params)
    conn.commit()
    return len(params)


def read_adjustments(conn, symbol, as_of):
    _require_as_of(as_of)
    rows = conn.execute(
        "SELECT ex_date,cum_forward,cum_backward,actions FROM adjustments"
        " WHERE symbol=? AND ex_date<=? ORDER BY ex_date", (symbol, as_of)).fetchall()
    return [{"ex_date": r["ex_date"], "cum_forward": r["cum_forward"],
             "cum_backward": r["cum_backward"], "actions": json.loads(r["actions"] or "[]")}
            for r in rows]


def upsert_fundamentals(conn, symbol, rows, source):
    params = [(symbol, r["field"], r["period_end"], r.get("announced_at"),
               r.get("announced_source"), r["value"], source) for r in rows]
    conn.executemany("INSERT OR REPLACE INTO fundamentals"
                     "(symbol,field,period_end,announced_at,announced_source,value,source)"
                     " VALUES(?,?,?,?,?,?,?)", params)
    conn.commit()
    return len(params)


def set_announced_at(conn, symbol, period_end, announced_at, source):
    cur = conn.execute(
        "UPDATE fundamentals SET announced_at=?, announced_source=?"
        " WHERE symbol=? AND period_end=? AND announced_at IS NULL",
        (announced_at, source, symbol, period_end))
    conn.commit()
    return cur.rowcount


def read_fundamentals(conn, symbol, as_of, field=None):
    _require_as_of(as_of)
    sql = ("SELECT field,period_end,announced_at,value,source FROM fundamentals"
           " WHERE symbol=? AND announced_at IS NOT NULL AND announced_at<=?")
    params = [symbol, as_of]
    if field:
        sql += " AND field=?"
        params.append(field)
    rows = conn.execute(sql + " ORDER BY period_end", params).fetchall()
    return [{"field": r["field"], "period_end": r["period_end"],
             "announced_at": r["announced_at"], "value": r["value"],
             "source": r["source"]} for r in rows]


def announced_coverage(conn):
    """按市场前缀统计 announced_at 覆盖率（规格 §13.5 验收口径）。"""
    out = {}
    rows = conn.execute(
        "SELECT substr(symbol,1,instr(symbol,'.')-1) AS mkt, COUNT(*) AS total,"
        " SUM(announced_at IS NOT NULL) AS with_date FROM fundamentals GROUP BY mkt").fetchall()
    for r in rows:
        out[r["mkt"] or "?"] = {"total": r["total"], "with_date": r["with_date"] or 0}
    return out


def store_universe(conn, as_of, index_name, symbols, source, bias_note=""):
    params = [(as_of, sym, index_name, bias_note, source) for sym in symbols]
    conn.executemany("INSERT OR REPLACE INTO universe"
                     "(snapshot_date,symbol,index_name,bias_note,source)"
                     " VALUES(?,?,?,?,?)", params)
    conn.commit()
    return len(params)


def read_universe(conn, as_of, index_name):
    _require_as_of(as_of)
    row = conn.execute("SELECT MAX(snapshot_date) FROM universe WHERE index_name=? AND snapshot_date<=?",
                       (index_name, as_of)).fetchone()
    if not row or not row[0]:
        return None
    snap_date = row[0]
    rows = conn.execute("SELECT symbol,bias_note FROM universe WHERE index_name=? AND snapshot_date=?",
                        (index_name, snap_date)).fetchall()
    return {"snapshot_date": snap_date,
            "symbols": [r["symbol"] for r in rows],
            "bias_note": rows[0]["bias_note"] if rows else ""}


def upsert_calendar(conn, market, days):
    params = [(market.upper(), d["day"], d.get("trade_date_type"), d.get("trade_second"))
              for d in days]
    conn.executemany("INSERT OR REPLACE INTO calendar(market,day,trade_date_type,trade_second)"
                     " VALUES(?,?,?,?)", params)
    conn.commit()
    return len(params)


def _require_calendar(conn, market):
    n = conn.execute("SELECT COUNT(*) FROM calendar WHERE market=?", (market.upper(),)).fetchone()[0]
    if not n:
        raise RuntimeError(f"日历未同步：{market}（先跑 python -m trading_core calendar）")


def is_trading_day(conn, market, day):
    _require_calendar(conn, market)
    row = conn.execute("SELECT 1 FROM calendar WHERE market=? AND day=?",
                       (market.upper(), day)).fetchone()
    return row is not None


def trading_days(conn, market, start, end):
    _require_calendar(conn, market)
    rows = conn.execute("SELECT day FROM calendar WHERE market=? AND day>=? AND day<=?"
                        " ORDER BY day", (market.upper(), start, end)).fetchall()
    return [r["day"] for r in rows]


def kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_store -v`
预期：PASS（9 个测试）。

- [ ] **步骤 5：Commit**

```bash
git add plugins/core/python/trading_core/store.py tests/test_core_store.py
git commit -m "feat(core): PIT SQLite 存储层（六表+WAL+as_of 强制+kv 游标）"
```

---

### 任务 3：calendar.py —— 交易日历

**文件：**
- 创建：`plugins/core/python/trading_core/calendar.py`
- 测试：`tests/test_core_calendar.py`

- [ ] **步骤 1：编写失败的日历测试**

创建 `tests/test_core_calendar.py`：

```python
"""日历抓取单测：注入假 fetcher，锁定 market 大写与 start/end 必传的调用口径。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import calendar, store  # noqa: E402

FUTU_PAYLOAD = {"trading_days": [
    {"time": "2026-09-11", "trade_date_type": "WHOLE", "trade_second": 14400},
    {"time": "2026-09-12", "trade_date_type": "CLOSE", "trade_second": 0}]}


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        self.seen = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fake_fetch(self, market, start, end):
        self.seen.append((market, start, end))
        return FUTU_PAYLOAD

    def test_sync_uses_uppercase_market_and_range(self):
        n = calendar.sync_calendar(self.conn, "sh", "2026-09-11", "2026-09-12",
                                   fetcher=self.fake_fetch)
        self.assertEqual(n, 2)
        self.assertEqual(self.seen, [("SH", "2026-09-11", "2026-09-12")])
        self.assertTrue(store.is_trading_day(self.conn, "SH", "2026-09-11"))
        self.assertFalse(store.is_trading_day(self.conn, "SH", "2026-09-12"))

    def test_trading_days_range(self):
        calendar.sync_calendar(self.conn, "SH", "2026-09-11", "2026-09-12",
                               fetcher=self.fake_fetch)
        self.assertEqual(store.trading_days(self.conn, "SH", "2026-09-01", "2026-09-30"),
                         ["2026-09-11"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_calendar -v`
预期：ERROR —— `No module named 'trading_core.calendar'`。

- [ ] **步骤 3：实现 calendar.py**

创建 `plugins/core/python/trading_core/calendar.py`：

```python
"""交易日历：抓取 quote_trading_days 并落库（规格 §4.1 calendar 表）。

口径（2026-09-14 实测）：market 必须大写（SH/HK/US/...），start/end 必传。
"""
from trading_datasource.futu_mcp import call_tool


def _default_fetcher(market, start, end):
    data = call_tool("quote_trading_days",
                     {"market": market.upper(), "start": start, "end": end},
                     client_name="trading-core/calendar")
    return data or {}


def sync_calendar(conn, market, start, end, fetcher=None):
    fetcher = fetcher or _default_fetcher
    payload = fetcher(market, start, end)
    days = [{"day": d["time"], "trade_date_type": d.get("trade_date_type"),
             "trade_second": d.get("trade_second")}
            for d in (payload.get("trading_days") or [])]
    return store_upsert(conn, market, days)


def store_upsert(conn, market, days):
    from . import store
    return store.upsert_calendar(conn, market, days)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_calendar -v`
预期：PASS（2 个测试）。

- [ ] **步骤 5：探测口径沉淀（规格 §13.5 第 4 条）——编写失败的常量锁定测试**

在 `tests/test_core_calendar.py` 追加：

```python
class MarketConstantsTest(unittest.TestCase):
    """2026-09-14 实测口径锁死（规格 §13.2/§13.5）：ktype 整数、指数代码映射。"""

    def test_ktype_values_are_integers(self):
        from trading_datasource.market import PERIOD_TO_FUTU_KTYPE
        self.assertTrue(all(isinstance(v, int) for v in PERIOD_TO_FUTU_KTYPE.values()))
        self.assertEqual(PERIOD_TO_FUTU_KTYPE["1d"], 2)

    def test_index_symbol_mapping(self):
        from trading_datasource.market import INDEX_SYMBOLS
        self.assertEqual(INDEX_SYMBOLS["csi300"], "SH.000300")
        self.assertEqual(INDEX_SYMBOLS["hsi"], "HK.800000")
        self.assertEqual(INDEX_SYMBOLS["ixic"], "US..IXIC")
        self.assertEqual(INDEX_SYMBOLS["dji"], "US..DJI")
        self.assertEqual(INDEX_SYMBOLS["sp500_proxy"], "US.SPY")  # 标普指数代码无效，SPY 替代
```

- [ ] **步骤 6：运行验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_calendar -v`
预期：`test_index_symbol_mapping` FAIL —— market 无 `INDEX_SYMBOLS` 属性（ktype 断言应已通过）。

- [ ] **步骤 7：在 `plugins/datasource/python/trading_datasource/market.py` 末尾追加常量**

```python
# 指数/基准代码映射（2026-09-14 实测，规格 §13.2）：标普指数代码无效，用 SPY ETF 代理。
INDEX_SYMBOLS = {"csi300": "SH.000300", "hsi": "HK.800000", "ixic": "US..IXIC",
                 "dji": "US..DJI", "sp500_proxy": "US.SPY"}
```

- [ ] **步骤 8：运行验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_calendar -v`
预期：PASS（3 个测试类合计 5 个测试）。

- [ ] **步骤 9：Commit**

```bash
git add plugins/core/python/trading_core/calendar.py plugins/datasource/python/trading_datasource/market.py tests/test_core_calendar.py
git commit -m "feat(core): 交易日历抓取与落库；探测口径沉淀为 market 常量"
```

---

### 任务 4：sync.py（上）—— bars 增量同步与全量回填

**文件：**
- 创建：`plugins/core/python/trading_core/sync.py`
- 测试：`tests/test_core_sync.py`（本任务先写 bars 部分）

- [ ] **步骤 1：编写失败的 bars 同步测试**

创建 `tests/test_core_sync.py`：

```python
"""同步层单测：增量过滤、回填断点续传、复权/财务/公告日/成分股映射。全部离线（注入假取数器）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import store, sync  # noqa: E402


def _bars(*dates, base=10.0):
    return [{"t": d, "o": base, "h": base + 1, "l": base - 0.5, "c": base + 0.5, "v": 100.0}
            for d in dates]


class BarsSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_incremental_skips_existing(self):
        calls = []

        def loader(ticker, period, limit):
            calls.append((ticker, period, limit))
            return _bars("2026-09-10", "2026-09-11"), "futu/x", False

        first = sync.sync_bars_incremental(self.conn, "600519", "1d", loader=loader)
        self.assertEqual(first["rows"], 2)
        second = sync.sync_bars_incremental(
            self.conn, "600519", "1d",
            loader=lambda t, p, l: (_bars("2026-09-11", "2026-09-14"), "futu/x", False))
        self.assertEqual(second["rows"], 1)  # 只补 09-14
        self.assertEqual(store.last_bar_date(self.conn, "600519", "1d"), "2026-09-14")
        self.assertEqual(len(calls), 1)  # 第二次用内联 lambda，不再记 calls

    def test_backfill_resume_after_failure(self):
        flaky = {"600519": _bars("2026-09-10", "2026-09-11"), "00700": None, "AAPL": _bars("2026-09-10")}

        def bad_loader(ticker, period, limit):
            bars = flaky[ticker]
            if bars is None:
                raise RuntimeError("取数失败：模拟断点")
            return bars, "fake", False

        summary = sync.backfill_bars(self.conn, ["600519", "00700", "AAPL"],
                                     loader=bad_loader, sleep_seconds=0)
        self.assertEqual(summary["ok"], ["600519", "AAPL"])
        self.assertIn("00700", summary["failed"])

        # 第二次运行：600519/AAPL 已在游标 done 里，只重试 00700
        fixed = {"600519": _bars("2026-09-10"), "00700": _bars("2026-09-11"), "AAPL": _bars("2026-09-10")}

        def good_loader(ticker, period, limit):
            return fixed[ticker], "fake", False

        summary2 = sync.backfill_bars(self.conn, ["600519", "00700", "AAPL"],
                                      loader=good_loader, sleep_seconds=0,
                                      progress_key=sync.PROGRESS_KEY_BACKFILL)
        self.assertEqual(summary2["ok"], ["00700"])
        self.assertEqual(store.last_bar_date(self.conn, "00700", "1d"), "2026-09-11")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_sync -v`
预期：ERROR —— `No module named 'trading_core.sync'`。

- [ ] **步骤 3：实现 bars 同步**

创建 `plugins/core/python/trading_core/sync.py`：

```python
"""同步层：bars 增量/回填、复权因子、财务报表、公告日合并、成分股快照。

原则（规格 §四）：取数一律走 trading_datasource（唯一实现）；失败如实上报，
不写占位行；回填进度落 kv 游标，可断点续传；富途调用间节流（复用 futu_mcp 退避，
此处只控制调用频率）。
"""
import datetime as _dt
import time

from trading_datasource.futu_mcp import call_tool
from trading_datasource.market import load_bars, to_futu_symbol

from . import store

SLEEP_SECONDS = 0.5            # 富途通道调用间隔；测试注入 0
PROGRESS_KEY_BACKFILL = "backfill:bars:1d"
BACKFILL_LIMIT = 2000          # market.MAX_BARS 上限内（路由层自动换源满足长历史）
_TZ8 = _dt.timezone(_dt.timedelta(hours=8))  # 富途毫秒时间戳按 UTC+8 零点对齐


def _sleep(seconds):
    if seconds:
        time.sleep(seconds)


def sync_bars_incremental(conn, ticker, period="1d", loader=None):
    loader = loader or load_bars
    last = store.last_bar_date(conn, ticker, period)
    if last is None:
        needed = 370                      # 首次增量 = 富途单次上限；更长历史交给 backfill
    else:
        since = (_dt.date.today() - _dt.date.fromisoformat(last)).days + 7
        needed = min(370, max(since, 5))
    bars, source, stale = loader(ticker, period, needed)
    fresh = [b for b in bars if last is None or b["t"] > last]
    rows = store.upsert_bars(conn, ticker, period, fresh, source)
    return {"ticker": ticker, "rows": rows, "source": source, "stale": stale,
            "last": store.last_bar_date(conn, ticker, period)}


def backfill_bars(conn, tickers, period="1d", limit=BACKFILL_LIMIT,
                  loader=None, sleep_seconds=None, progress_key=PROGRESS_KEY_BACKFILL):
    """全量回填：每标的一次 load_bars（路由层自动满足长历史）；失败记录不中断；
    kv 游标（done/failed）支持断点续传——重复调用只处理未完成标的。"""
    loader = loader or load_bars
    sleep_seconds = SLEEP_SECONDS if sleep_seconds is None else sleep_seconds
    progress = store.kv_get(conn, progress_key, default={"done": [], "failed": {}})
    done = list(progress.get("done", []))
    failed = dict(progress.get("failed", {}))
    ok = []
    for ticker in tickers:
        if ticker in done:
            continue
        try:
            bars, source, stale = loader(ticker, period, limit)
            store.upsert_bars(conn, ticker, period, bars, source)
            failed.pop(ticker, None)
            done.append(ticker)
            ok.append(ticker)
        except Exception as error:  # noqa: BLE001 —— 单标的失败不中断回填
            failed[ticker] = str(error)[:160]
        store.kv_set(conn, progress_key, {"done": done, "failed": failed})
        _sleep(sleep_seconds)
    return {"ok": ok, "failed": failed,
            "done_total": len(done), "pending": [t for t in tickers if t not in done]}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_sync -v`
预期：PASS（2 个测试）。

- [ ] **步骤 5：Commit**

```bash
git add plugins/core/python/trading_core/sync.py tests/test_core_sync.py
git commit -m "feat(core): bars 增量同步与回填（kv 游标断点续传+节流）"
```

---

### 任务 5：sync.py（中）—— 复权因子与财务报表

**文件：**
- 修改：`plugins/core/python/trading_core/sync.py`（追加函数）
- 修改：`tests/test_core_sync.py`（追加测试类）

- [ ] **步骤 1：追加失败的测试**

在 `tests/test_core_sync.py` 追加：

```python
REHAB_SAMPLE = {"rehabs": [
    {"ex_div_date": "2026-06-20", "cum_forward_adj_factorA": 12.34,
     "cum_backward_adj_factorA": 0.98, "action_types": ["DIVIDEND"], "per_cash_div": 2.0},
    {"ex_div_date": "2025-06-20", "cum_forward_adj_factorA": 12.10,
     "cum_backward_adj_factorA": 0.97, "action_types": ["DIVIDEND"], "per_cash_div": 1.8}]}

STATEMENTS_SAMPLE = {"report_list": [
    {"date_time": 1782748800000, "financial_type": 2, "fiscal_year": 2026,
     "item_list": [{"display_name": "Total Operating Revenue", "data": 37575159697.98},
                   {"display_name": "Net Profit", "data": 1890123456.78},
                   {"display_name": "Gross Profit", "data": 3012345678.9},
                   {"display_name": "Diluted EPS", "data": 15.06}]}]}


class AdjustmentsFundamentalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_adjustments_mapping(self):
        seen = {}

        def fetcher(name, args):
            seen["args"] = args
            return REHAB_SAMPLE

        n = sync.sync_adjustments(self.conn, "600519", fetcher=fetcher)
        self.assertEqual(n, 2)
        self.assertEqual(seen["args"]["symbol"], "SH.600519")   # futu 格式
        rows = store.read_adjustments(self.conn, "600519", as_of="2026-09-14")
        self.assertEqual(rows[-1]["cum_forward"], 12.34)
        self.assertEqual(rows[-1]["actions"], ["DIVIDEND"])

    def test_fundamentals_aliases_and_pit_invisible(self):
        seen = {}

        def fetcher(name, args):
            seen["args"] = args
            return STATEMENTS_SAMPLE

        n = sync.sync_fundamentals(self.conn, "600519", fetcher=fetcher)
        self.assertEqual(n, 4)  # revenue/net_profit/gross_profit/diluted_eps 各一期
        self.assertEqual(seen["args"]["symbol"], "SH.600519")
        # 富途无公告日：PIT 读取必须不可见，直到任务 6 的合并作业补上
        self.assertEqual(store.read_fundamentals(self.conn, "600519", as_of="2026-09-14"), [])
```

- [ ] **步骤 2：运行测试验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_sync -v`
预期：ERROR —— `sync` 无 `sync_adjustments` 属性。

- [ ] **步骤 3：实现（追加到 sync.py）**

在 `plugins/core/python/trading_core/sync.py` 末尾追加：

```python
# 富途科目名按市场不同（实测口径沿用 workbench/python/quality.py 的 alias 表，只取 4 键）。
FIELD_ALIASES = {
    "revenue": ["Total Revenue as Reported", "Total Revenue", "Total Operating Revenue",
                "Operating Revenue"],
    "net_profit": ["Net Profit", "Net Income to Parent Company",
                   "Net Profit of Parent Company Owners"],
    "gross_profit": ["Gross Profit"],
    "diluted_eps": ["Diluted EPS"],
}


def ms_to_date(ms):
    """富途报告期毫秒时间戳 → YYYY-MM-DD（按 UTC+8 零点对齐，实测口径）。"""
    return _dt.datetime.fromtimestamp(ms / 1000, _TZ8).strftime("%Y-%m-%d")


def sync_adjustments(conn, ticker, divi_mode="include_divi", fetcher=None):
    """复权因子：quote_corporate_actions_rehab（炸弹工具，单标的调用；divi_mode
    默认 include_divi = A股/富途口径，schema 实测确认）。"""
    fetcher = fetcher or call_tool
    data = fetcher("quote_corporate_actions_rehab",
                   {"symbol": to_futu_symbol(ticker), "divi_mode": divi_mode}) or {}
    rows = [{"ex_date": r["ex_div_date"],
             "cum_forward": r.get("cum_forward_adj_factorA"),
             "cum_backward": r.get("cum_backward_adj_factorA"),
             "actions": r.get("action_types") or []}
            for r in (data.get("rehabs") or []) if r.get("ex_div_date")]
    return store.upsert_adjustments(conn, to_futu_symbol(ticker), rows, "futu/rehab")


def sync_fundamentals(conn, ticker, fetcher=None):
    """财务报表：quote_financials_statements → 4 个核心字段。
    富途不含公告日，announced_at 落 NULL，由 merge_announcements_akshare 补齐。"""
    fetcher = fetcher or call_tool
    futu_symbol = to_futu_symbol(ticker)
    data = fetcher("quote_financials_statements", {"symbol": futu_symbol}) or {}
    rows = []
    for report in data.get("report_list") or []:
        period_end = ms_to_date(report["date_time"])
        items = {i.get("display_name"): i.get("data") for i in report.get("item_list") or []}
        for field, aliases in FIELD_ALIASES.items():
            value = next((items[a] for a in aliases if items.get(a) is not None), None)
            if value is not None:
                rows.append({"field": field, "period_end": period_end, "value": float(value)})
    return store.upsert_fundamentals(conn, futu_symbol, rows, "futu/statements")
```

- [ ] **步骤 4：运行测试验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_sync -v`
预期：PASS（4 个测试）。

- [ ] **步骤 5：Commit**

```bash
git add plugins/core/python/trading_core/sync.py tests/test_core_sync.py
git commit -m "feat(core): 复权因子与财务报表同步（科目别名映射，公告日留空）"
```

---

### 任务 6：sync.py（下）—— 公告日双源合并 + 成分股快照

**文件：**
- 修改：`plugins/core/python/trading_core/sync.py`（追加函数）
- 修改：`tests/test_core_sync.py`（追加测试类）

- [ ] **步骤 1：追加失败的测试**

在 `tests/test_core_sync.py` 追加：

```python
class AnnouncementsUniverseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        # 先落一期的富途基本面（announced_at 为空）
        sync.sync_fundamentals(self.conn, "600519", fetcher=lambda n, a: STATEMENTS_SAMPLE)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_merge_announcements(self):
        import pandas as pd

        class FakeAk:
            @staticmethod
            def stock_yjbb_em(date):
                assert date == "20260630"
                return pd.DataFrame([
                    {"股票代码": "600519", "公告日期": "2026-08-28 00:00:00", "净利润": 1.0},
                    {"股票代码": "000001", "公告日期": "2026-08-29", "净利润": 2.0}])

        result = sync.merge_announcements_akshare(self.conn, "20260630", akshare_module=FakeAk)
        self.assertEqual(result["matched"], 4)  # 600519 一期 × 4 字段；000001 无台账行，rowcount=0
        rows = store.read_fundamentals(self.conn, "600519", as_of="2026-09-14")
        self.assertEqual([r["field"] for r in rows],
                         ["diluted_eps", "gross_profit", "net_profit", "revenue"])
        self.assertTrue(all(r["announced_at"] == "2026-08-28" for r in rows))

    def test_universe_pagination(self):
        pages = {"": {"stock_list": [{"symbol": f"SZ.3008{i:02d}"} for i in range(50)],
                      "next_key": "NEXT1"},
                 "NEXT1": {"stock_list": [{"symbol": "SH.600519"}]}}

        def fetcher(name, args):
            return pages[args.get("next_key", "")]

        n = sync.sync_universe(self.conn, "SH.000300", "2026-09-14", fetcher=fetcher)
        self.assertEqual(n, 51)
        snap = store.read_universe(self.conn, as_of="2026-09-14", index_name="SH.000300")
        self.assertEqual(len(snap["symbols"]), 51)
        self.assertIn("幸存者偏差", snap["bias_note"])  # 缺口②的降级标注（规格 §13.4）
```

- [ ] **步骤 2：运行测试验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_sync -v`
预期：ERROR —— `sync` 无 `merge_announcements_akshare` 属性。

- [ ] **步骤 3：实现（追加到 sync.py）**

在 `plugins/core/python/trading_core/sync.py` 末尾追加：

```python
UNIVERSE_BIAS_NOTE = "当前成分快照，未含历史成分，含幸存者偏差（规格 §13.4 缺口②降级）"


def merge_announcements_akshare(conn, period, akshare_module=None):
    """缺口①（规格 §13.4）：A股公告日双源合并。

    period 形如 "20260630"（报告期）。一次调用覆盖全市场当期业绩表，
    逐行把「股票代码+报告期」匹配到的 fundamentals 行补上公告日。
    只补 announced_at IS NULL 的行，不覆盖已合并的来源。
    """
    ak = akshare_module
    if ak is None:
        import akshare as ak
    df = ak.stock_yjbb_em(date=period)
    if df is None or len(df) == 0:
        return {"matched": 0, "rows": 0}
    period_end = f"{period[:4]}-{period[4:6]}-{period[6:8]}"
    code_col = next(c for c in df.columns if "股票代码" in c)
    date_col = next(c for c in df.columns if "公告" in c)
    matched = 0
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        symbol = to_futu_symbol(code)
        raw = str(row[date_col]).strip()
        announced = raw[:10] if len(raw) >= 10 else raw
        matched += store.set_announced_at(conn, symbol, period_end, announced, "akshare/yjbb")
    return {"matched": matched, "rows": int(len(df))}


def sync_universe(conn, index_symbol, as_of, fetcher=None, bias_note=UNIVERSE_BIAS_NOTE,
                  limit=50):
    """指数成分快照：quote_valuation_index_component_stock_list 键集分页（limit≤50，
    next_key 透传；schema 实测确认）。bias_note 承载缺口②的幸存者偏差标注。"""
    fetcher = fetcher or call_tool
    symbols, next_key = [], None
    while True:
        args = {"symbol": index_symbol, "limit": limit}
        if next_key:
            args["next_key"] = next_key
        data = fetcher("quote_valuation_index_component_stock_list", args) or {}
        page = data.get("stock_list") or []
        symbols.extend(r["symbol"] for r in page if r.get("symbol"))
        next_key = data.get("next_key")
        if not next_key or not page:
            break
    # 成分返回的是 futu 格式（SH.600519）；universe 统一存 6 位裸码（is_a_share 约定）
    bare = sorted({s.split(".")[-1] if "." in s else s for s in symbols})
    store.store_universe(conn, as_of, index_symbol, bare, "futu/component_stock_list", bias_note)
    return len(bare)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_sync -v`
预期：PASS（6 个测试）。

- [ ] **步骤 5：真实调用核验分页（一次性，非测试套件）**

运行：`PYTHONPATH=plugins/datasource/python ~/.dsh/trading-venv/bin/python -c "from trading_datasource import futu_mcp; total, nk = 0, None
while True:
    args = {'symbol': 'SH.000300', 'limit': 50}
    if nk: args['next_key'] = nk
    d = futu_mcp.call_tool('quote_valuation_index_component_stock_list', args) or {}
    total += len(d.get('stock_list') or [])
    nk = d.get('next_key')
    if not nk: break
print('component total:', total)"`
预期：`component total:` ≥ 300（沪深300 全量）。若响应里的翻页字段名不是 `next_key`（例如返回 `has_more`+游标别名），以实测字段为准修正 `sync_universe` 的两处 `next_key` 引用后重跑本步与测试——这是唯一允许按运行时 schema 微调的字段名。

- [ ] **步骤 6：Commit**

```bash
git add plugins/core/python/trading_core/sync.py tests/test_core_sync.py
git commit -m "feat(core): 公告日双源合并（akshare/yjbb）与指数成分快照（键集分页+偏差标注）"
```

---

### 任务 7：quality.py —— 缺口检测与覆盖率

**文件：**
- 创建：`plugins/core/python/trading_core/quality.py`
- 测试：`tests/test_core_quality.py`

- [ ] **步骤 1：编写失败的质量测试**

创建 `tests/test_core_quality.py`：

```python
"""质量层单测：缺口检测、新鲜度、announced_at 覆盖率、跨源交叉校验。全部离线。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import quality, store, sync  # noqa: E402


class QualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        store.upsert_calendar(self.conn, "SH", [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400}
            for d in ("2026-09-10", "2026-09-11", "2026-09-14")])
        store.upsert_bars(self.conn, "600519", "1d",
                          _bars("2026-09-10", "2026-09-14"), "futu/x")  # 缺 09-11

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_gap_report_finds_missing_day(self):
        gaps = quality.gap_report(self.conn, "SH", "600519", "2026-09-10", "2026-09-14")
        self.assertEqual(gaps, ["2026-09-11"])

    def test_freshness_counts_calendar_days(self):
        info = quality.freshness(self.conn, "600519", "1d", today="2026-09-14")
        self.assertEqual(info["last"], "2026-09-14")
        self.assertEqual(info["days_behind"], 0)

    def test_cross_source_mismatch_detection(self):
        fresh = _bars("2026-09-14", base=99.0)  # 与库内 close 10.5 差异巨大

        def loader(ticker, period, limit):
            return fresh, "sina/test", False

        bad = quality.cross_source_check(self.conn, "600519", "1d", sample=1, loader=loader)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["date"], "2026-09-14")

    def test_announced_coverage(self):
        sync.sync_fundamentals(self.conn, "600519", fetcher=lambda n, a: STATEMENTS_SAMPLE)
        store.set_announced_at(self.conn, "SH.600519", "2026-06-30", "2026-08-28", "akshare/yjbb")
        cov = quality.announced_coverage(self.conn)
        self.assertEqual(cov["SH"], {"total": 4, "with_date": 4})


def _bars(*dates, base=10.0):
    return [{"t": d, "o": base, "h": base + 1, "l": base - 0.5, "c": base + 0.5, "v": 100.0}
            for d in dates]


STATEMENTS_SAMPLE = {"report_list": [
    {"date_time": 1782748800000, "financial_type": 2, "fiscal_year": 2026,
     "item_list": [{"display_name": "Total Operating Revenue", "data": 1.0},
                   {"display_name": "Net Profit", "data": 2.0},
                   {"display_name": "Gross Profit", "data": 3.0},
                   {"display_name": "Diluted EPS", "data": 4.0}]}]}


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_quality -v`
预期：ERROR —— `No module named 'trading_core.quality'`。

- [ ] **步骤 3：实现 quality.py**

创建 `plugins/core/python/trading_core/quality.py`：

```python
"""数据质量：缺口检测（按日历）、新鲜度、announced_at 覆盖率、跨源交叉校验。

宁缺毋假（规格 §4.2 规则 3）：缺口如实列出，不自动补假数据。
"""
import datetime as _dt

from trading_datasource.market import load_bars

from . import store

CROSS_SOURCE_TOLERANCE = 0.005  # 收盘价交叉校验容差 0.5%（规格 §4.3）


def gap_report(conn, market, symbol, start, end):
    expected = store.trading_days(conn, market, start, end)
    have = {b["t"] for b in store.read_bars(conn, symbol, "1d", as_of=end)}
    return [d for d in expected if d not in have]


def freshness(conn, symbol, period, today=None):
    last = store.last_bar_date(conn, symbol, period)
    if last is None:
        return {"last": None, "days_behind": None}
    today = today or _dt.date.today().isoformat()
    days = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(last)).days
    return {"last": last, "days_behind": days}


def announced_coverage(conn):
    return store.announced_coverage(conn)


def cross_source_check(conn, ticker, period="1d", sample=5,
                       tolerance=CROSS_SOURCE_TOLERANCE, loader=None):
    """抽样比对：库内最后 N 根收盘 vs 现取同源收盘，超容差记为不一致。"""
    loader = loader or load_bars
    today = _dt.date.today().isoformat()
    stored = store.read_bars(conn, ticker, period, as_of=today, limit=sample)
    if not stored:
        return []
    fresh, _, _ = loader(ticker, period, sample)
    by_date = {b["t"]: b["c"] for b in fresh}
    out = []
    for b in stored:
        other = by_date.get(b["t"])
        if other is None:
            continue
        if abs(other - b["c"]) > tolerance * max(abs(other), abs(b["c"]), 1e-9):
            out.append({"date": b["t"], "stored": b["c"], "fresh": other})
    return out


def full_report(conn, market, symbols, start, end, today=None):
    return {
        "market": market,
        "range": [start, end],
        "freshness": {s: freshness(conn, s, "1d", today=today) for s in symbols},
        "gaps": {s: gap_report(conn, market, s, start, end) for s in symbols},
        "announced_coverage": announced_coverage(conn),
    }
```

- [ ] **步骤 4：运行测试验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_quality -v`
预期：PASS（4 个测试）。

- [ ] **步骤 5：Commit**

```bash
git add plugins/core/python/trading_core/quality.py tests/test_core_quality.py
git commit -m "feat(core): 质量层（缺口/新鲜度/公告日覆盖率/跨源校验）"
```

---

### 任务 8：CLI 单入口

**文件：**
- 创建：`plugins/core/python/trading_core/cli.py`
- 创建：`plugins/core/python/trading_core/__main__.py`
- 测试：`tests/test_core_cli.py`

- [ ] **步骤 1：编写失败的 CLI 测试**

创建 `tests/test_core_cli.py`：

```python
"""CLI 冒烟：子命令解析与 JSON 输出（临时库，不碰真实 DSH_HOME，不发网络请求）。

只测离线安全的 quality 子命令；网络类子命令（calendar/sync/backfill...）的真实
行为由任务 10 步骤 2 的手动冒烟覆盖，不打进自动化套件。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import cli, store  # noqa: E402


class CliTest(unittest.TestCase):
    def test_quality_roundtrip_on_seeded_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.sqlite")
            conn = store.connect(db)
            try:
                store.upsert_calendar(conn, "SH", [
                    {"day": "2026-09-11", "trade_date_type": "WHOLE", "trade_second": 14400}])
                store.upsert_bars(conn, "600519", "1d", [], "futu/x")  # 无 bar → 全缺口
            finally:
                conn.close()
            out = self._run(["quality", "--db", db, "--market", "SH",
                             "--symbols", "600519", "--start", "2026-09-11",
                             "--end", "2026-09-11"])
            self.assertEqual(out["gaps"]["600519"], ["2026-09-11"])
            self.assertIn("announced_coverage", out)

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_cli -v`
预期：ERROR —— `No module named 'trading_core.cli'`。

- [ ] **步骤 3：实现 cli.py 与 __main__.py**

创建 `plugins/core/python/trading_core/cli.py`：

```python
"""python -m trading_core 单入口：全部子命令输出 JSON，便于 Harness 对话读取。

网络类子命令（calendar/sync/backfill/adjustments/fundamentals/merge/universe）
直连真实渠道；quality 只读本地库。默认库路径 $DSH_HOME/trading-data/trading.sqlite。
"""
import argparse
import datetime as _dt
import json

from . import calendar as cal
from . import quality, store, sync


def _add_db(parser):
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 $DSH_HOME/trading-data/trading.sqlite）")


def build_parser():
    p = argparse.ArgumentParser(prog="trading_core", description="量化平台数据基座 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("calendar", help="同步交易日历")
    s.add_argument("--market", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    _add_db(s)

    s = sub.add_parser("sync-bars", help="增量同步日线")
    s.add_argument("--tickers", required=True, help="逗号分隔")
    _add_db(s)

    s = sub.add_parser("backfill", help="全量回填日线（断点续传）")
    s.add_argument("--tickers", required=True, help="逗号分隔")
    s.add_argument("--limit", type=int, default=sync.BACKFILL_LIMIT)
    _add_db(s)

    s = sub.add_parser("adjustments", help="同步复权因子")
    s.add_argument("--tickers", required=True)
    _add_db(s)

    s = sub.add_parser("fundamentals", help="同步财务报表（公告日留空）")
    s.add_argument("--tickers", required=True)
    _add_db(s)

    s = sub.add_parser("merge-announcements", help="合并 A 股公告日（akshare/yjbb）")
    s.add_argument("--period", required=True, help="报告期，如 20260630")
    _add_db(s)

    s = sub.add_parser("universe", help="指数成分快照")
    s.add_argument("--index", default="SH.000300")
    s.add_argument("--as-of", default=_dt.date.today().isoformat())
    _add_db(s)

    s = sub.add_parser("quality", help="质量报告（只读本地库）")
    s.add_argument("--market", default="SH")
    s.add_argument("--symbols", required=True, help="逗号分隔")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    _add_db(s)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = store.connect(args.db) if getattr(args, "db", None) else store.connect()
    try:
        if args.cmd == "calendar":
            result = {"days": cal.sync_calendar(conn, args.market, args.start, args.end)}
        elif args.cmd == "sync-bars":
            result = {"symbols": [sync.sync_bars_incremental(conn, t.strip())
                                  for t in args.tickers.split(",") if t.strip()]}
        elif args.cmd == "backfill":
            result = sync.backfill_bars(conn, [t.strip() for t in args.tickers.split(",") if t.strip()],
                                        limit=args.limit)
        elif args.cmd == "adjustments":
            result = {t: sync.sync_adjustments(conn, t.strip())
                      for t in args.tickers.split(",") if t.strip()}
        elif args.cmd == "fundamentals":
            result = {t: sync.sync_fundamentals(conn, t.strip())
                      for t in args.tickers.split(",") if t.strip()}
        elif args.cmd == "merge-announcements":
            result = sync.merge_announcements_akshare(conn, args.period)
        elif args.cmd == "universe":
            result = {"symbols": sync.sync_universe(conn, args.index, args.as_of),
                      "as_of": args.as_of,
                      "bias_note": sync.UNIVERSE_BIAS_NOTE}
        elif args.cmd == "quality":
            result = quality.full_report(conn, args.market,
                                         [t.strip() for t in args.symbols.split(",") if t.strip()],
                                         args.start, args.end)
        else:  # pragma: no cover - argparse 已约束
            raise ValueError(f"未知子命令 {args.cmd}")
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0
```

创建 `plugins/core/python/trading_core/__main__.py`：

```python
from .cli import main

raise SystemExit(main())
```

- [ ] **步骤 4：运行测试验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_cli -v`
预期：PASS（1 个测试）。

- [ ] **步骤 5：Commit**

```bash
git add plugins/core/python/trading_core/cli.py plugins/core/python/trading_core/__main__.py tests/test_core_cli.py
git commit -m "feat(core): python -m trading_core 单入口（9 个子命令，JSON 输出）"
```

---

### 任务 9：离线端到端集成

**文件：**
- 创建：`tests/test_core_integration.py`

- [ ] **步骤 1：编写端到端测试（日历→回填→财务→公告日→质量报告）**

创建 `tests/test_core_integration.py`：

```python
"""离线端到端：假渠道驱动 full pipeline，验收口径对齐规格 §十 WP1。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import calendar, quality, store, sync  # noqa: E402

TICKERS = ["600519", "000858"]
DAYS = ["2026-09-10", "2026-09-11", "2026-09-14"]


def _bars(dates):
    return [{"t": d, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 9.0} for d in dates]


class PipelineTest(unittest.TestCase):
    def test_full_pipeline_zero_gaps_and_coverage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        self.addCleanup(conn.close)

        # 1) 日历（假 fetcher）
        n_days = calendar.sync_calendar(
            conn, "SH", DAYS[0], DAYS[-1],
            fetcher=lambda m, s, e: {"trading_days": [
                {"time": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in DAYS]})
        self.assertEqual(n_days, 3)

        # 2) 回填（假 loader，模拟一次失败后续传）
        state = {"calls": 0}

        def loader(ticker, period, limit):
            state["calls"] += 1
            if ticker == "000858" and state["calls"] == 2:
                raise RuntimeError("模拟瞬时失败")
            return _bars(DAYS), "fake/src", False

        summary = sync.backfill_bars(conn, TICKERS, loader=loader, sleep_seconds=0)
        summary = sync.backfill_bars(conn, TICKERS, loader=loader, sleep_seconds=0,
                                     progress_key=sync.PROGRESS_KEY_BACKFILL)
        self.assertEqual(set(summary["failed"]), set())

        # 3) 财务 + 公告日合并
        statements = {"report_list": [
            {"date_time": 1782748800000, "item_list": [
                {"display_name": "Total Operating Revenue", "data": 5.0},
                {"display_name": "Net Profit", "data": 1.0},
                {"display_name": "Gross Profit", "data": 2.0},
                {"display_name": "Diluted EPS", "data": 0.5}]}]}

        import pandas as pd

        class FakeAk:
            @staticmethod
            def stock_yjbb_em(date):
                return pd.DataFrame([{"股票代码": t, "公告日期": "2026-08-28"} for t in TICKERS])

        for t in TICKERS:
            sync.sync_fundamentals(conn, t, fetcher=lambda n, a: statements)
        merge = sync.merge_announcements_akshare(conn, "20260630", akshare_module=FakeAk)
        self.assertEqual(merge["matched"], 2 * 4)

        # 4) 质量报告：零缺口 + 覆盖率 100%
        report = quality.full_report(conn, "SH", TICKERS, DAYS[0], DAYS[-1])
        self.assertEqual(report["gaps"], {t: [] for t in TICKERS})
        self.assertEqual(report["announced_coverage"],
                         {"SH": {"total": 4, "with_date": 4},
                          "SZ": {"total": 4, "with_date": 4}})

        # 5) PIT 纪律抽查：公告日之前读不到财务
        self.assertEqual(store.read_fundamentals(conn, "SH.600519", as_of="2026-08-27"), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行验证通过**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_integration -v`
预期：PASS（1 个测试）。

- [ ] **步骤 3：Commit**

```bash
git add tests/test_core_integration.py
git commit -m "test(core): 离线端到端（日历→回填→财务→公告日→质量零缺口）"
```

> 勘误（2026-09-15 执行期发现）：覆盖率断言按市场分桶、PIT 断言须用 futu 格式符号——原计划文本两处笔误已随实现修正。

---

### 任务 10：全量回归 + 真实冒烟（手动）+ 安装验收

- [ ] **步骤 1：全量离线回归（既有套件不得破坏）**

运行：`~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v`
预期：全部 PASS（含既有 52 例 Python 套件 + 新增 core 套件）。若 `tests/test_install.py` 因 .pth 单行断言失败，按任务 1 步骤 5 的口径修正其期望值后重跑。

- [ ] **步骤 2：真实冒烟（手动，网络可用时执行；不属于 unittest 套件）**

```bash
# 日历（富途真实调用；market 大写、start/end 必传）
PYTHONPATH=plugins/core/python ~/.dsh/trading-venv/bin/python -m trading_core calendar \
  --market SH --start 2026-01-01 --end 2026-09-14
# 三样本回填（A股新浪长历史 + 港股/美股富途/Yahoo 路由）
PYTHONPATH=plugins/core/python ~/.dsh/trading-venv/bin/python -m trading_core backfill \
  --tickers 600519,00700,AAPL
# 公告日合并（最近一期中报）
PYTHONPATH=plugins/core/python ~/.dsh/trading-venv/bin/python -m trading_core merge-announcements \
  --period 20260630
# 质量报告：预期 600519/00700/AAPL 缺口为空或如实列出
PYTHONPATH=plugins/core/python ~/.dsh/trading-venv/bin/python -m trading_core quality \
  --market SH --symbols 600519 --start 2026-01-01 --end 2026-09-14
```

预期：各命令输出 JSON；`backfill` 的 `failed` 为空；`quality.gaps` 无异常缺口（个股停牌除外，如实列出）。

- [ ] **步骤 3：安装验收（自检通过）**

```bash
~/.dsh/trading-venv/bin/python scripts/install_plugins.py install \
  --repo "$PWD" --dsh-home "${DSH_HOME:-$HOME/.dsh}"
~/.dsh/trading-venv/bin/python scripts/install_plugins.py check \
  --repo "$PWD" --dsh-home "${DSH_HOME:-$HOME/.dsh}"
```

预期：check 输出「✅ 安装完整」，且 notes 里出现统一数据层 `core`；`$DSH_HOME/trading-python/core/trading_core/` 存在；venv 的 `dsh-trading-python.pth` 含 `datasource` 与 `core` 两行。

- [ ] **步骤 4：WP1 验收对照（规格 §十）逐项确认并记录**

在 `docs/superpowers/plans/2026-09-14-wp1-data-foundation.md` 末尾追加「验收记录」小节：三市场日线回填的标的数与起止日期、`announced_at` 覆盖率 JSON 原文、缺口报告 JSON 原文。缺口非零时逐条给出原因（停牌/新股/渠道失败），不允许静默留空。

- [ ] **步骤 5：Commit 收尾**

```bash
git add docs/superpowers/plans/2026-09-14-wp1-data-foundation.md
git commit -m "docs(plans): WP1 验收记录（回填/覆盖率/缺口报告原文）"
```

---

## 明确不在本计划范围（YAGNI，见规格）

- 分钟级同步、关注池（WP2 按策略需要再开）；
- 港美公告日（Yahoo earnings dates）——`announced_coverage` 会如实显示 HK/US 为 0%，属规格 §13.4① 的既定边界；
- daemon/调度（WP4）、组合回测（WP2）、OMS（WP3）。

---

## 验收记录（2026-09-15 执行）

**任务 1-9**：10 个实现子代理 + 每任务两阶段审查（规格合规 → 代码质量），全部通过。
审查驱动的修复共 7 轮（T1 package.json private/files、T2 补覆盖与不可覆写锁、
T3 卫生门禁 main 块、T4 仅 1d 守卫与 needed 锁定、T5 两步式匹配+类型守卫、
T6 公告日格式校验与分页上限、T7 去跨测试 import 与跨源口径说明、T9 PIT 断言
改 futu 符号）。执行期发现并勘误计划文本笔误 4 处（read_fundamentals 漏列、
CLOSE 日口径、覆盖率分桶、PIT 裸码断言）。

**步骤 1 全量回归**：`Ran 265 tests in ~14s — OK`（0 失败；含既有 52+ 与新增 core 套件）。

**步骤 2 真实冒烟**（2026-09-15，真实富途 MCP + akshare，库 `~/.dsh/trading-data/trading.sqlite`）：
- calendar SH 2026-01-01..09-14 → 170 交易日
- backfill 600519,00700,AAPL → ok×3、failed {}，12.1s（新浪长历史/富途/Yahoo 路由各自动生效）
- fundamentals ×3 → 35/40/40 行（多报告期 × 4 字段，PK 去重后 SH 29/HK 32/US 32）
- merge-announcements 20260630 → matched 3、rows 11449、skipped 0（全市场表一次拉取）
- quality（600519）：gaps []、freshness last=09-14、coverage：SH 3/29、**HK 0/32、US 0/32**
  ——HK/US 公告日 0% 为规格 §13.4① 既定边界，如实呈现（诚实性验收点）

**步骤 3 安装验收（安全变体）**：手动解出 core 至 `~/.dsh/trading-python/core/` +
`link`（.pth 两行：datasource、core）+ 纯 venv 导入 `trading_core 0.1.0` 成功。
`check` 除一项**存量状态**外全部通过：仓库 preset 的 futu-keepalive 行本就
`disabled: true`（部署流程「方式 B」才会启用，与 WP1 无关）。完整 `install` 的
preset 翻动留待用户部署时执行，本验收不做。

**步骤 4 WP1 验收标准对照（规格 §十）**：
- 三市场日线回填完成 ✅（冒烟三市场各 1 标的端到端；全市场回填属运维作业，由
  daemon/调度（WP4）按关注池与节奏执行）
- 缺口报告可查 ✅（`python -m trading_core quality`）
- `announced_at` 覆盖率可统计 ✅（同上，SH/HK/US 分桶如实）

**遗留**：HK/US 公告日（规格 §13.4① 后半，随 WP2 港美基本面接入）；全市场回填
调度（WP4 daemon 作业链）；`test_data_layer` 的真实富途探测测试依赖 MCP 在线
（环境性，已在本轮全量中自然通过）。

### 合并前最终审查结论（2026-09-15）：需修复后合并

- **K1（关键阻塞）**：回填经 `load_bars` 长历史路由落库的是**复权价**（新浪 qfq / Yahoo
  auto_adjusted），违反规格 §4.2 规则 2「落库一律原始价 + 因子表」——真实库实证
  600519 库内 1184.08=前复权（不复权 1212.10），且当时 adjustments 为空（现已补
  30 行真实因子）。修复方案待用户定夺：A) 回填改走富途 ≤370 根分块原始价
  （推荐，口径单一）；B) sina/yahoo 增加不复权变体。存量 ~6000 行需清洗重灌。
- W1/W2 已修复（`44af543`：fundamentals 重跑保留公告日；bars 符号归一 futu 格式）。
- W3 已补真实通道证据：adjustments 30 行（2002 起累计因子）、universe 300 行
  （分页信封实测 6 页，`has_more/next_key` 与实现一致）。
- 环境备注：venv `.pth` 解析的是安装快照——datasource 改动后必须刷新快照
  （重跑 install 或 rsync），否则 CLI 跑在旧代码上（本次 universe=50 即此因）。

### K1 修复（方案 A）执行记录（2026-09-15）

**改动说明**（TDD，先红后绿）：

- `market.py`：新增 `load_raw_bars(ticker, period="1d", limit=2000)`——富途
  ≤370 根/页、`end` 游标向后翻页（cursor 初始今天，其后取本批最早日期的前一天）、
  按 t 合并去重；凑满 limit / 历史翻尽（空页）/ 游标不再前移（防死循环）即停；
  首批即失败按 load_bars 风格抛 `RuntimeError`；返回
  `(bars, "futu/raw_chunk", False)`。`fetch_futu` 增加可选参 `end=None`（缺省
  今天，向后兼容）。
- **根因再发现（本轮关键实证）**：富途 MCP `quote_history_kline` 带
  `autype` 参数（tools/list schema 实测）：0=不复权 / **1=前复权（服务端默认）**
  / 2=后复权。**不传 autype 拿到的是前复权**——「走富途」不等于「拿到原始价」，
  这才是 K1 的真正根因。因此 `fetch_futu` 再增 `autype=None` 可选参，
  `load_raw_bars` 必须显式 `autype="0"`；实测 SH.600519 end=2026-06-25：
  autype=0 → close 1212.10（原始价），autype=1/缺省 → 1184.0758（前复权）。
- `sync.py`：`backfill_bars` 默认 loader 改 `load_raw_bars`，docstring 更新
  「回填走富途原始价分块——复权口径由 adjustments 表派生（规格 §4.2 规则 2）」；
  `sync_bars_incremental` 未动（见下方告警②）。
- 测试：`test_data_layer.py` 增 `RawChunkTests` 6 条（分页合并/跨页去重/游标
  逐块前移/ktype=2 且 autype="0"/三种终止路径/首批空页与通道故障抛
  RuntimeError/`fetch_futu` 缺省参数向后兼容）；`test_core_sync.py` 增 1 条
  （backfill 默认路径经 patch 验证确实调用 `load_raw_bars` 并落库）。
  TDD 红灯实证：6 条 `AttributeError: no attribute 'load_raw_bars'`、
  autype 断言 `None != '0'`，随后转绿。

**测试**：全量 `unittest discover` **Ran 273 tests — OK**（修复前基线 266 OK，
净增 7 条全绿；含既有 sync 回填断点续传测试注入 loader 不受影响）。

**存量清洗与重灌**（真实富途 MCP，库 `~/.dsh/trading-data/trading.sqlite`）：

- 第一轮清洗 6000 行（3 标的 × 2000；注意：存量行符号是 W2 修复前灌入的
  **裸码** 600519/00700/AAPL，DELETE 需同时覆盖裸码与 futu 格式，任务给的
  只含 futu 格式的 DELETE 实际匹配 0 行）+ 游标 kv `backfill:bars:1d` 复位
  （否则断点续传会静默跳过全部标的）。
- 第一轮重灌暴露 autype 问题（source 已是 futu/raw_chunk 但 600519
  仍 1184.0758）→ 实现 `autype="0"` 后**二次清洗 6660 行 + 重灌**。
- 终态：`SELECT source, COUNT(*) FROM bars GROUP BY source` →
  **`futu/raw_chunk | 6660`**（唯一来源）；每标的 2220 行（370×6 整页，
  ≥2000 即停不截断），起止 SH.600519 2017-07-26..2026-09-14 /
  HK.00700 2017-09-06..2026-09-15 / US.AAPL 2017-11-10..2026-09-14，
  符号均为 futu 格式。

**600519 抽查对照（2026-06-25 close）**：

| 来源 | close | 口径 |
|---|---|---|
| 修复前库内（akshare/sina qfq） | 1184.08 | 前复权 ❌ |
| 富途 autype 缺省（=1 前复权） | 1184.0758 | 前复权 ❌ |
| 富途 autype=0（本轮落库） | **1212.10** | **不复权 ✅**（抽查行 1184.08 已不存在） |

00700 421.40 / AAPL 275.15 同日抽查：00700 与原 Yahoo 值一致（06-25 后无除权，
前复权=原始，交叉印证）；AAPL 274.9129→275.15（复权差异肉眼可见）。

**告警与如实记录**：

1. **600519 未触发空页终止**：三标的均在凑满 limit（2220≥2000）时截止，
   没有翻到真实历史起点，空页终止路径由离线测试覆盖、真实通道未走到——
   如实记录，不宣称验证过。
2. **增量口径遗留（需用户定夺，本轮未动）**：实测证明 `sync_bars_incremental`
   经 `load_bars`→`fetch_futu`（不带 autype）落库的同样是**前复权**——任务
   前提「增量 ≤370 本就走富途原始价」不成立。若维持现状，下个交易日的增量
   同步会把前复权行写入全原始价的表（混源，且前复权值随分红漂移）。建议
   后续把增量默认 loader 也钉为 `load_raw_bars`（needed≤370 时恰为单页
   autype=0 取数，行为等价、口径统一，约一行改动 + 测试）。本轮严格按任务
   范围未动 `sync_bars_incremental`，在此之前应暂停增量同步作业。
3. 执行过程环境注：改动后按「环境备注」rsync 刷新了 `~/.dsh/trading-python/`
   快照并验证 venv 解析到新代码后才跑真实冒烟（否则 CLI 跑旧代码）。
