# WP2 研究层 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。
> **全局约定**：见 `2026-09-14-platform-plan-index.md`（UI 文案规范/数据可行性协议/工程约定）。本包无 UI 页面。

**目标：** 因子注册表、横截面打分、IC 检验、策略注册、多标的组合回测（含 A 股现实约束与基准对比）、walk-forward 与参数敏感性——产出可复现的 OOS 研究报告。

**架构：** 全部落 `plugins/core/python/trading_core/`，数据只读 WP1 的 PIT store；估值因子按日落 `valuations` 表（schema v2 迁移）；策略接口 `universe(as_of) → target_weights(as_of, data)`；回测引擎消费策略权重序列。

**技术栈：** Python 标准库 + pandas/numpy（venv 已有）+ 既有 trading_datasource。

**验收（规格 §十 WP2）：** 横截面策略 OOS 报告产出、IC 检验可用。

---

## 依赖锁定表（2026-09-14 核验）

| 依赖 | 锁定结论 | 证据 |
|---|---|---|
| 中证800 K 线 | `quote_history_kline(symbol="SH.000906", ktype=2)` 返回「CSI 800 Index」 | 本日实调 |
| 估值因子字段路径 | 复用 `plugins/workbench/python/factors.py::valuation_values`（三市场 48 通道实测可用），WP2 收敛进 core 后字段路径以该实现为准，不另猜 | TOOL-LIMITS + 既有实现 |
| 质量因子 | 富途 statements 4 键（WP1 已落库）+ ROE/ROA 经 `trading_datasource.fundamentals`（Yahoo/AKShare 既有实现） | WP1 任务 5 |
| 情绪因子 | 并列参考不进信号（规格 §5.1），不实现计算，仅透传展示 | 规格既定 |

### 任务 0：依赖核验固化为锁定测试

**文件：** 修改 `plugins/datasource/python/trading_datasource/market.py`（INDEX_SYMBOLS 加 csi800）；测试 `tests/test_core_wp2_locks.py`

- [x] 步骤 1：写失败测试 `tests/test_core_wp2_locks.py`：

```python
"""WP2 依赖锁定：上游 schema 漂移时这里的断言会先红（规格 §2.2 协议）。"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_datasource.market import INDEX_SYMBOLS, PERIOD_TO_FUTU_KTYPE


class Wp2Locks(unittest.TestCase):
    def test_csi800_index_available(self):
        self.assertEqual(INDEX_SYMBOLS["csi800"], "SH.000906")

    def test_valuation_source_converged(self):
        import importlib.util
        p = Path(__file__).resolve().parents[1] / "plugins" / "workbench" / "python" / "factors.py"
        self.assertTrue(p.is_file(), "估值因子唯一实现必须存在（收敛前）")


if __name__ == "__main__":
    unittest.main()
```

- [x] 步骤 2：运行确认 FAIL（csi800 不存在）：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_wp2_locks -v`
- [x] 步骤 3：`market.py` 的 `INDEX_SYMBOLS` 追加 `"csi800": "SH.000906"`
- [x] 步骤 4：重跑 PASS；`git commit -m "feat(core): WP2 依赖锁定（中证800/估值来源）"`

### 任务 1：store v2 —— valuations 表

**文件：** 修改 `store.py`（_SCHEMA、SCHEMA_VERSION=2、两个函数）；测试追加 `tests/test_core_store.py`

- [x] 步骤 1：追加失败测试：

```python
    def test_valuations_pit(self):
        store.upsert_valuations(self.conn, "SH.600519", "2026-09-13",
                                {"pe": 22.5, "pb": 8.1}, "futu/valuation")
        store.upsert_valuations(self.conn, "SH.600519", "2026-09-14",
                                {"pe": 22.1, "pb": 8.0}, "futu/valuation")
        rows = store.read_valuations(self.conn, "SH.600519", as_of="2026-09-13")
        self.assertEqual(rows["pe"], 22.5)
        with self.assertRaises(ValueError):
            store.read_valuations(self.conn, "SH.600519", as_of=None)
```

- [x] 步骤 2：确认 FAIL；步骤 3：`store.py` 实现——`_SCHEMA` 追加：

```sql
CREATE TABLE IF NOT EXISTS valuations(
  symbol TEXT NOT NULL, day TEXT NOT NULL, field TEXT NOT NULL,
  value REAL NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY(symbol, day, field)) WITHOUT ROWID;
```

`SCHEMA_VERSION = 2`；追加函数：

```python
def upsert_valuations(conn, symbol, day, fields, source):
    params = [(symbol, day, k, float(v), source) for k, v in fields.items() if v is not None]
    conn.executemany("INSERT OR REPLACE INTO valuations(symbol,day,field,value,source)"
                     " VALUES(?,?,?,?,?)", params)
    conn.commit()
    return len(params)


def read_valuations(conn, symbol, as_of):
    _require_as_of(as_of)
    rows = conn.execute(
        "SELECT field, value FROM valuations WHERE symbol=? AND day="
        " (SELECT MAX(day) FROM valuations WHERE symbol=? AND day<=?)",
        (symbol, symbol, as_of)).fetchall()
    return {r["field"]: r["value"] for r in rows}
```

- [x] 步骤 4：PASS；`git commit -m "feat(core): valuations 表（schema v2，PIT 读取）"`

### 任务 2：因子注册表（价格域）

**文件：** 创建 `factors.py`；测试 `tests/test_core_factors.py`

- [x] 步骤 1：失败测试（合成 bars 验证动量/波动数值与注册表）：

```python
"""因子注册表单测：注册机制 + 价格域因子数值（合成序列，手算对照）。"""
import math, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import factors, store  # noqa: E402


class FactorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        bars = [{"t": f"2026-{m:02d}-{d:02d}", "o": 10, "h": 10.5, "l": 9.5, "c": 10 + i * 0.1, "v": 100}
                for i, (m, d) in enumerate([(m, d) for m in range(1, 9) for d in range(1, 29)])]
        store.upsert_bars(self.conn, "600519", "1d", bars, "test")

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_registry_contains_price_domain(self):
        for name in ("momentum_20", "momentum_60", "momentum_120", "volatility_20"):
            self.assertIn(name, factors.REGISTRY)

    def test_momentum_20_value(self):
        v = factors.REGISTRY["momentum_20"](self.conn, "SH.600519", "2026-07-18")
        self.assertIsInstance(v, float)
        self.assertFalse(math.isnan(v))

    def test_missing_history_returns_none(self):
        v = factors.REGISTRY["momentum_120"](self.conn, "SH.600519", "2026-02-05")
        self.assertIsNone(v)  # 历史不足 → None（宁缺毋假），上层记 missing
```

- [x] 步骤 2：FAIL；步骤 3：实现 `factors.py`：

```python
"""因子注册表：@factor 注册，统一签名 fn(conn, futu_symbol, as_of) -> float|None。

可复现性（规格 §5.1）：输入只有 PIT store 与 as_of；情绪永不入内。
"""
import math

from . import store

REGISTRY = {}


def factor(name):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


def _closes(conn, symbol, as_of, n):
    bars = store.read_bars(conn, symbol, "1d", as_of=as_of, limit=n)
    if len(bars) < n:
        return None
    return [b["c"] for b in bars]


def _momentum(n):
    def fn(conn, symbol, as_of):
        closes = _closes(conn, symbol, as_of, n + 1)
        if closes is None:
            return None
        return closes[-1] / closes[0] - 1.0
    return fn


def _volatility(n):
    def fn(conn, symbol, as_of):
        closes = _closes(conn, symbol, as_of, n + 1)
        if closes is None:
            return None
        rets = [math.log(closes[i + 1] / closes[i]) for i in range(n)]
        mean = sum(rets) / n
        return math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) * math.sqrt(250)
    return fn


factor("momentum_20")(_momentum(20))
factor("momentum_60")(_momentum(60))
factor("momentum_120")(_momentum(120))
factor("volatility_20")(_volatility(20))
```

- [x] 步骤 4：PASS；`git commit -m "feat(core): 因子注册表与价格域因子"`

### 任务 3：估值因子收敛 + 估值同步

**文件：** 修改 `factors.py`（valuation 因子，委托 `workbench/python/factors.py::valuation_values` 的收敛实现）、`sync.py`（`sync_valuations`）、测试 `tests/test_core_factors.py`

- [x] 步骤 1：失败测试（假 fetcher 返回样例估值 dict，验证落库与因子读取）：

```python
    def test_valuation_factor_reads_store(self):
        store.upsert_valuations(self.conn, "SH.600519", "2026-07-18", {"pe": 22.5}, "t")
        self.assertEqual(factors.REGISTRY["ep"](self.conn, "SH.600519", "2026-07-18"), 1 / 22.5)
        self.assertIsNone(factors.REGISTRY["ep"](self.conn, "SH.600519", "2026-07-01"))
```

- [x] 步骤 2：FAIL；步骤 3：实现——`factors.py` 追加（EP=市盈率倒数，口径统一"越大越看多"）：

```python
@factor("ep")
def _ep(conn, symbol, as_of):
    v = store.read_valuations(conn, symbol, as_of).get("pe")
    return (1.0 / v) if v and v > 0 else None
```

`sync.py` 追加 `sync_valuations(conn, symbols, fetcher=None)`：对每个 futu symbol 调用收敛后的估值实现（把 `workbench/python/factors.py::valuation_values` 的实现体迁入 `trading_core.factors.valuation_values`，workbench 侧改为薄委托），返回 `{field: value}` 后 `store.upsert_valuations(conn, symbol, today, fields, "futu/valuation")`。迁移时**旧测试改指向新位置**（`tests/` 里引用 valuation_values 的用例同步更新 import）。
- [x] 步骤 4：PASS；步骤 5：`git commit -m "feat(core): 估值因子收敛进 core 并按日落库"`

### 任务 4：横截面标准化与复合打分

**文件：** 修改 `factors.py`；测试追加

- [x] 步骤 1：失败测试：

```python
    def test_zscore_mad_winsorize(self):
        vals = {"A": 1.0, "B": 1.1, "C": 0.9, "D": 1.05, "E": 100.0}  # E 为离群
        z = factors.cross_sectional_zscore(vals)
        self.assertLess(abs(z["E"]), abs(z["B"]))  # MAD 去极值后离群点不再统治
        score = factors.composite_score({"A": {"f1": 1.0}, "B": {"f1": 2.0}}, weights={"f1": 1.0})
        self.assertGreater(score["B"], score["A"])
```

- [x] 步骤 2：FAIL；步骤 3：实现 `factors.py`：

```python
def cross_sectional_zscore(values, mad_bound=3.0):
    """横截面 z-score，MAD 去极值（规格 §5.1）。返回 {key: z}。"""
    if len(values) < 3:
        return {k: None for k in values}
    xs = sorted(values.values())
    med = xs[len(xs) // 2]
    mad = sorted(abs(x - med) for x in xs)[len(xs) // 2] or 1e-12
    clipped = {k: med + max(-mad_bound, min(mad_bound, (v - med) / (1.4826 * mad))) * (1.4826 * mad)
               for k, v in values.items()}
    mean = sum(clipped.values()) / len(clipped)
    var = sum((v - mean) ** 2 for v in clipped.values()) / (len(clipped) - 1)
    std = math.sqrt(var) or 1e-12
    return {k: (v - mean) / std for k, v in clipped.items()}


def composite_score(per_symbol_factors, weights):
    """{symbol: {factor: raw}} × {factor: weight} → {symbol: score}。
    因子方向在注册时已统一（越大越看多），此处不再翻方向。"""
    out = {}
    for symbol, fv in per_symbol_factors.items():
        num = den = 0.0
        for name, w in weights.items():
            v = fv.get(name)
            if v is not None:
                num += w * v
                den += abs(w)
        out[symbol] = num / den if den else None
    return out
```

- [x] 步骤 4：PASS；`git commit -m "feat(core): 横截面 z-score(MAD) 与复合打分"`

### 任务 5：IC / RankIC 与五分位分层

**文件：** 修改 `factors.py`；测试追加

- [x] 步骤 1：失败测试（已知单调序列 → RankIC=1.0；分层 Q1>Q5）：

```python
    def test_rank_ic_perfect_monotonic(self):
        vals = {f"S{i}": float(i) for i in range(6)}
        fwd = {f"S{i}": float(i) * 0.01 for i in range(6)}
        self.assertAlmostEqual(factors.rank_ic(vals, fwd), 1.0, places=6)

    def test_quintile_spread_monotonic_universe(self):
        # 12 标的、因子值与未来收益完全同序 → Q5（高因子）收益 > Q1
        vals = {f"S{i}": float(i) for i in range(12)}
        fwd = {f"S{i}": float(i) * 0.01 for i in range(12)}
        q = factors.quintile_returns(vals, fwd)
        self.assertGreater(q["Q5"], q["Q1"])
```

- [x] 步骤 2：FAIL；步骤 3：实现（追加）：

```python
def _rank(values):
    order = sorted(values, key=values.get)
    return {k: i for i, k in enumerate(order)}


def rank_ic(factor_values, forward_returns):
    """Spearman 秩相关；样本 < 3 或零方差返回 None。"""
    common = [k for k in factor_values if k in forward_returns]
    if len(common) < 3:
        return None
    rf, rr = _rank({k: factor_values[k] for k in common}), _rank({k: forward_returns[k] for k in common})
    n = len(common)
    d2 = sum((rf[k] - rr[k]) ** 2 for k in common)
    return 1 - 6 * d2 / (n * (n * n - 1))


def quintile_returns(factor_values, forward_returns, buckets=5):
    common = sorted((k for k in factor_values if k in forward_returns), key=factor_values.get)
    if len(common) < buckets:
        return {}
    size = len(common) // buckets
    out = {}
    for q in range(buckets):
        part = common[q * size:(q + 1) * size] if q < buckets - 1 else common[(buckets - 1) * size:]
        out[f"Q{q + 1}"] = sum(forward_returns[k] for k in part) / len(part)
    return out
```

- [x] 步骤 4：PASS；`git commit -m "feat(core): RankIC 与五分位分层"`

### 任务 6：策略注册表

**文件：** 创建 `strategies.py`；测试 `tests/test_core_strategies.py`

- [x] 步骤 1：失败测试：

```python
"""策略注册表：单标的策略移植 + 横截面策略 target_weights。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import store, strategies  # noqa: E402


class StrategiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        bars = [{"t": f"2026-{m:02d}-{d:02d}", "o": 10, "h": 10.5, "l": 9.5,
                 "c": 10 + (i % 40) * 0.05, "v": 100}
                for i, (m, d) in enumerate([(m, d) for m in range(1, 10) for d in range(1, 29)])]
        for s in ("SH.600519", "SH.000858", "SZ.300750", "SH.601899", "SH.600036",
                  "SZ.000333", "SH.601318", "SZ.002415"):
            store.upsert_bars(self.conn, s, "1d", bars, "test")
        store.store_universe(self.conn, "2026-09-13", "SH.000300",
                             [s.split(".")[1] for s in ("SH.600519", "SH.000858", "SZ.300750",
                                                        "SH.601899", "SH.600036",
                                                        "SZ.000333", "SH.601318", "SZ.002415")], "t")
        for day in ("2026-09-12", "2026-09-13"):
            store.upsert_valuations(self.conn, "SH.600519", day, {"pe": 20.0}, "t")
            store.upsert_valuations(self.conn, "SH.000858", day, {"pe": 12.0}, "t")
            store.upsert_valuations(self.conn, "SZ.300750", day, {"pe": 30.0}, "t")
            store.upsert_valuations(self.conn, "SH.601899", day, {"pe": 10.0}, "t")
            store.upsert_valuations(self.conn, "SH.600036", day, {"pe": 6.0}, "t")

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_cross_section_strategy_topn_equal_weight(self):
        strat = strategies.REGISTRY["momentum_value_top5"]
        w = strat.target_weights(self.conn, "2026-09-13")
        self.assertLessEqual(len(w), 5)
        self.assertLessEqual(sum(w.values()), 1.0 + 1e-9)
        self.assertTrue(all(v > 0 for v in w.values()))

    def test_single_ticker_strategy_signal(self):
        strat = strategies.REGISTRY["rsi"]
        self.assertIn(strat.signal(self.conn, "SH.600519", "2026-09-13"), ("BUY", "SELL", "HOLD"))
```

- [x] 步骤 2：FAIL；步骤 3：实现 `strategies.py`：

```python
"""策略注册表（规格 §5.2）：universe(as_of) → target_weights(as_of)。

信号可复现铁律：输入只有 PIT store + as_of。
"""
from trading_datasource.backtest import ma_cross_signal, rsi_signal  # 唯一回测实现内的信号函数

from . import factors, store

REGISTRY = {}


def strategy(sid):
    def deco(cls):
        REGISTRY[sid] = cls()  # 注册实例：调用方直接 strat.target_weights(conn, as_of)
        return cls
    return deco


class SingleTicker:
    id = "base"
    # 子类提供 _fn：df → 1（买）/ -1（卖）/ 0（持有），参数口径与
    # trading_datasource.backtest 既有信号函数一致（rsi 25/75，ma_cross 5/20）。
    _fn = staticmethod(lambda df: 0)

    def universe(self, conn, as_of):
        return []

    def signal(self, conn, symbol, as_of):
        bars = store.read_bars(conn, symbol, "1d", as_of=as_of, limit=60)
        if len(bars) < 25:
            return "HOLD"
        import pandas as pd
        df = pd.DataFrame(bars).rename(columns={"t": "date", "c": "close"})
        v = self._fn(df)
        return "BUY" if v == 1 else ("SELL" if v == -1 else "HOLD")


@strategy("rsi")
class RsiStrategy(SingleTicker):
    id = "rsi"
    _fn = staticmethod(lambda df: rsi_signal(df, 25, 75))


@strategy("ma_cross")
class MaCrossStrategy(SingleTicker):
    id = "ma_cross"
    _fn = staticmethod(lambda df: ma_cross_signal(df, 5, 20))


@strategy("momentum_value_top5")
class MomentumValueTop5:
    id = "momentum_value_top5"
    top_n = 5
    weights = {"momentum_60": 0.5, "momentum_20": 0.2, "ep": 0.3}

    def universe(self, conn, as_of):
        snap = store.read_universe(conn, as_of=as_of, index_name="SH.000300")
        return [f"{'SH.' if c.startswith(('6', '9')) else 'SZ.' if c.startswith(('0', '3')) else 'BJ.'}{c}"
                for c in (snap["symbols"] if snap else [])]

    def target_weights(self, conn, as_of):
        universe = self.universe(conn, as_of)
        per = {}
        for sym in universe:
            fv = {}
            for name in self.weights:
                try:
                    fv[name] = factors.REGISTRY[name](conn, sym, as_of)
                except Exception:
                    fv[name] = None
            if any(v is not None for v in fv.values()):
                per[sym] = fv
        if not per:
            return {}
        # 逐因子横截面 z 后按权重合成
        zs = {}
        for name, w in self.weights.items():
            raw = {s: per[s][name] for s in per if per[s][name] is not None}
            for s, z in factors.cross_sectional_zscore(raw).items():
                zs.setdefault(s, {})[name] = z
        scored = {s: (sum(w * z[name] for name, w in self.weights.items()
                          if z.get(name) is not None)
                      / sum(w for name, w in self.weights.items() if z.get(name) is not None))
                  if z else None for s, z in zs.items()}
        ranked = sorted((s for s in scored if scored[s] is not None),
                        key=scored.get, reverse=True)[:self.top_n]
        if not ranked:
            return {}
        w = round(1.0 / len(ranked), 4)
        return {s: w for s in ranked}
```

- [x] 步骤 4：PASS；`git commit -m "feat(core): 策略注册表（rsi/ma_cross 移植 + momentum_value_top5）"`

### 任务 7：组合回测引擎

**文件：** 创建 `portfolio.py`；测试 `tests/test_core_portfolio.py`

- [x] 步骤 1：失败测试（合成 6 标的 × 300 日：引擎跑通、指标齐全、约束生效）：

```python
"""组合回测：月度再平衡、成本、A股约束、基准对比。全部合成数据离线。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import portfolio, store  # noqa: E402


class BacktestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        syms = ["600519", "000858", "300750", "601899", "600036", "000333"]
        for k, s in enumerate(syms):
            bars = [{"t": f"2025-{m:02d}-{d:02d}",
                     "o": 10 + k, "h": 11 + k, "l": 9 + k,
                     "c": 10 + k + ((i * 7 + k * 3) % 11 - 5) * 0.1, "v": 1000}
                    for i, (m, d) in enumerate([(m, d) for m in range(1, 13) for d in range(1, 26)])]
            store.upsert_bars(self.conn, f"{'SH.' if s[0] in '69' else 'SZ.'}{s}", "1d", bars, "t")
        store.upsert_bars(self.conn, "SH.000300", "1d",
                          [{"t": f"2025-{m:02d}-{d:02d}", "o": 4000, "h": 4010, "l": 3990,
                            "c": 4000 + i, "v": 1} for i, (m, d) in enumerate(
                              [(m, d) for m in range(1, 13) for d in range(1, 26)])], "t")

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_run_produces_metrics_and_respects_constraints(self):
        class Static:
            id = "static5"
            def target_weights(self, conn, as_of):
                syms = ["SH.600519", "SH.000858", "SZ.300750", "SH.601899", "SH.600036"]
                return {s: 0.2 for s in syms}

        r = portfolio.run(conn, Static(), start="2025-01-01", end="2025-12-24",
                          benchmark="SH.000300")
        for k in ("total_return", "annual", "sharpe", "max_drawdown", "excess_annual",
                  "information_ratio", "turnover", "param_groups"):
            self.assertIn(k, r["summary"])
        self.assertIn("equity", r)
        self.assertEqual(r["summary"]["param_groups"], 1)  # 静态策略也如实报组数
```

- [x] 步骤 2：FAIL；步骤 3：实现 `portfolio.py`：

```python
"""多标的组合回测（规格 §5.3）：月度再平衡 + 成本 + A股现实约束 + 基准对比。

成本：佣金 0.03% 双边 + A股卖出印花税 0.1% + 滑点 0.1% 双边（与
trading_datasource.backtest 常数一致）。约束：T+1（当日买入不可卖）、
涨停不买/跌停不卖（±10% 判定）、停牌跳过、整手 100。
"""
import math

from . import store

COMMISSION, STAMP, SLIPPAGE = 0.0003, 0.001, 0.001
LOT = 100


def _is_a(code):
    return code.startswith(("SH.", "SZ.", "BJ."))


def run(conn, strategy, start, end, benchmark=None, rebalance="monthly"):
    days = store.trading_days(conn, "SH", start, end) if benchmark else []
    if benchmark and days:
        universe_dates = days
    else:
        row = conn.execute("SELECT MIN(ts),MAX(ts) FROM bars WHERE period='1d'").fetchone()
        if not row or not row[0]:
            raise RuntimeError("库内无 bars，先回填")
        universe_dates = store.trading_days(conn, "SH", row[0], row[1]) or []
    cash, shares, equity, last_buy_day = 1_000_000.0, {}, [], {}
    curve, turnover_sum, rebal_months = [], 0.0, set()
    target = {}
    for day in universe_dates:
        # 月度再平衡：每月第一个交易日重算目标权重
        if day[5:7] not in rebal_months:
            rebal_months.add(day[5:7])
            target = strategy.target_weights(conn, _prev_as_of(conn, day))
        day_equity = cash + sum(shares.get(s, 0) * _close(conn, s, day) for s in shares)
        # 卖出（先卖后买，T+1 约束：当日买入不卖）
        for s in list(shares):
            px, lim = _close(conn, s, day), _limit_ok(conn, s, day)
            if px is None or not lim:
                continue
            want = target.get(s, 0.0) * day_equity
            have = shares[s] * px
            if have > want * 1.02 and day not in last_buy_day.get(s, set()):
                lots = int(min((have - want), shares[s] * px) / px / LOT) * LOT
                if lots >= LOT:
                    shares[s] -= lots
                    cash += lots * px * (1 - COMMISSION - STAMP - SLIPPAGE)
                    turnover_sum += lots * px
        # 买入
        for s, w in target.items():
            px, lim = _close(conn, s, day), _limit_ok(conn, s, day)
            if px is None or not lim:
                continue
            have = shares.get(s, 0) * px
            want = w * day_equity
            if want > have * 1.02:
                lots = int(min(want - have, cash) / px / LOT) * LOT
                if lots >= LOT:
                    cost = lots * px * (1 + COMMISSION + SLIPPAGE)
                    if cost <= cash:
                        shares[s] = shares.get(s, 0) + lots
                        cash -= cost
                        turnover_sum += lots * px
                        last_buy_day.setdefault(s, set()).add(day)
        equity.append(day_equity)
        curve.append({"t": day, "equity": day_equity})
    summary = _metrics(curve, turnover_sum,
                       _benchmark_curve(conn, benchmark, universe_dates))
    return {"summary": summary, "equity": curve, "target_last": target}


def _close(conn, symbol, day):
    rows = store.read_bars(conn, symbol, "1d", as_of=day, limit=1)
    return rows[-1]["c"] if rows and rows[-1]["t"] == day else None  # 停牌：当日无 bar


def _limit_ok(conn, symbol, day):
    rows = store.read_bars(conn, symbol, "1d", as_of=day, limit=2)
    if len(rows) < 2 or rows[-1]["t"] != day:
        return False
    chg = rows[-1]["c"] / rows[0]["c"] - 1.0
    if _is_a(symbol) and abs(chg) >= 0.0995:  # 涨停不买/跌停不卖（近似判定）
        return False
    return True


def _prev_as_of(conn, day):
    days = store.trading_days(conn, "SH", "2000-01-01", day)
    idx = days.index(day)
    return days[idx - 1] if idx > 0 else day


def _benchmark_curve(conn, benchmark, dates):
    if not benchmark:
        return []
    return [(_close(conn, benchmark, d) or 0.0) for d in dates]


def _metrics(curve, turnover, bench):
    vals = [p["equity"] for p in curve]
    if len(vals) < 2:
        return {"param_groups": 1}
    rets = [vals[i + 1] / vals[i] - 1 for i in range(len(vals) - 1)]
    total = vals[-1] / vals[0] - 1
    years = len(vals) / 250
    annual = (1 + total) ** (1 / years) - 1
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(250)
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    br = [b for b in bench if b]
    b_total = (br[-1] / br[0] - 1) if len(br) > 1 else 0.0
    b_annual = (1 + b_total) ** (1 / years) - 1 if br else 0.0
    excess = [r - (b_annual / 250) for r in rets] if br else rets
    ir = (sum(excess) / len(excess)) / (math.sqrt(sum((e - sum(excess) / len(excess)) ** 2
         for e in excess) / (len(excess) - 1)) * math.sqrt(250)) if len(excess) > 2 else 0.0
    return {"total_return": round(total, 4), "annual": round(annual, 4),
            "volatility": round(vol, 4),
            "sharpe": round(annual / vol, 3) if vol else 0.0,
            "max_drawdown": round(mdd, 4),
            "excess_annual": round(annual - b_annual, 4) if br else None,
            "information_ratio": round(ir, 3) if br else None,
            "turnover": round(turnover / (sum(vals) / len(vals)) / years, 2),
            "param_groups": 1}
```

- [x] 步骤 4：PASS；`git commit -m "feat(core): 组合回测引擎（月度再平衡+约束+基准）"`

### 任务 8：walk-forward 与参数敏感性

**文件：** 创建 `walkforward.py`；测试 `tests/test_core_walkforward.py`

- [x] **步骤 1：失败测试**（`tests/test_core_walkforward.py`）——合成数据上跑 3 折：OOS 拼接曲线长度 = 测试窗 × 折数；`param_groups` 字段如实报告网格大小；敏感性网格形状 5×5：

```python
"""walk-forward：训练 504/测试 63/步长 63 可配置为小窗口做离线测试（90/20/20）。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import store, walkforward  # noqa: E402


class WfTest(unittest.TestCase):
    def test_oos_concat_and_param_groups(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        days = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 26)]
        for s in ("SH.600519", "SH.000858", "SZ.300750", "SH.601899", "SH.600036"):
            store.upsert_bars(conn, s, "1d",
                              [{"t": d, "o": 1, "h": 2, "l": 0.5, "c": 1 + (int(d) + len(s)) % 7 * 0.1,
                                "v": 1} for d in days], "t")
        result = walkforward.run(conn, strategy_id="momentum_value_top5",
                                 train=90, test=20, step=20,
                                 grid={"top_n": [3, 5]}, start=days[0], end=days[-1])
        self.assertEqual(result["summary"]["param_groups"], 2)
        self.assertTrue(result["oos_curve"])
        self.assertEqual(len(result["folds"]), 3)
```

- [x] **步骤 2：FAIL**；**步骤 3：实现 `walkforward.py`**：

```python
"""walk-forward（规格 §5.3）：每折仅用训练窗选参，测试窗拼接为 OOS 曲线；
报告强制自曝测试过的参数组数（多重检验）。"""
from . import portfolio, store


def _instance(base):
    """REGISTRY 存的是实例；策略无状态，按参数组合浅拷贝即可。"""
    import copy
    return copy.copy(base)


def run(conn, strategy_id, train, test, step, grid, start, end, benchmark="SH.000300"):
    from . import strategies
    base = strategies.REGISTRY[strategy_id]
    all_days = store.trading_days(conn, "SH", start, end)
    folds, oos_curve = [], []
    combos = _expand(grid)
    i = 0
    while i + train + test <= len(all_days):
        tr_end = all_days[i + train - 1]
        te_end = all_days[min(i + train + test - 1, len(all_days) - 1)]
        best, best_metric = None, -1e9
        for combo in combos:
            inst = _instance(base)
            _apply(inst, combo)
            r = _eval_window(conn, inst, all_days[i], tr_end, benchmark)
            if r["summary"].get("sharpe", -1e9) > best_metric:
                best, best_metric = combo, r["summary"].get("sharpe", -1e9)
        best_inst = _instance(base)
        _apply(best_inst, best)
        r = _eval_window(conn, best_inst, all_days[i], te_end, benchmark)
        seg = r["equity"][-test:]
        for p in seg:
            oos_curve.append(p)
        folds.append({"train_end": tr_end, "test_end": te_end, "params": best,
                      "oos_sharpe": r["summary"].get("sharpe")})
        i += step
    return {"folds": folds, "oos_curve": oos_curve,
            "summary": {"param_groups": len(combos),
                        "folds": len(folds),
                        "train": train, "test": test, "step": step}}


def _expand(grid):
    keys = list(grid)
    out = [{}]
    for k in keys:
        out = [dict(c, **{k: v}) for c in out for v in grid[k]]
    return out or [{}]


def _apply(inst, combo):
    for k, v in combo.items():
        setattr(inst, k, v)


def _eval_window(conn, inst, start, end, benchmark):
    """窗口内直接按月调 target_weights 生成持仓并估值（复用 portfolio.run 的
    约束引擎，但策略实例已带参）。"""
    return portfolio.run(conn, inst, start=start, end=end, benchmark=benchmark)
```

- [x] **步骤 4：PASS**；`git commit -m "feat(core): walk-forward 与参数敏感性（多重检验自曝）"`

### 任务 9：CLI 子命令 + 离线端到端

**文件：** 修改 `cli.py`（`ic`/`backtest` 子命令）；创建 `tests/test_core_wp2_e2e.py`

- [x] 步骤 1：失败测试（合成库上 `backtest` 子命令输出含 `summary.oos_sharpe` 与 `param_groups`）；步骤 2：FAIL；步骤 3：`cli.py` 追加两个子命令：

```python
    s = sub.add_parser("ic", help="因子 RankIC 序列")
    s.add_argument("--factor", default="momentum_60")
    s.add_argument("--symbols", required=True)
    s.add_argument("--as-of", required=True)
    s.add_argument("--horizon", type=int, default=20)
    _add_db(s)

    s = sub.add_parser("backtest", help="walk-forward 组合回测")
    s.add_argument("--strategy", default="momentum_value_top5")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--train", type=int, default=504)
    s.add_argument("--test", type=int, default=63)
    s.add_argument("--step", type=int, default=63)
    _add_db(s)
```

分派分支：

```python
        elif args.cmd == "ic":
            from . import factors
            vals = {s.strip(): factors.REGISTRY[args.factor](conn, s.strip(), args.as_of)
                    for s in args.symbols.split(",")}
            vals = {k: v for k, v in vals.items() if v is not None}
            # 前向收益：取 as_of 之后的窗口（近似口径——交易日对齐由 bars 本身保证）
            as_of2 = (_dt.date.fromisoformat(args.as_of)
                      + _dt.timedelta(days=args.horizon * 2)).isoformat()
            fwd = {}
            for s in vals:
                bars = store.read_bars(conn, s, "1d", as_of=as_of2, limit=args.horizon)
                if len(bars) >= 2:
                    fwd[s] = bars[-1]["c"] / bars[0]["c"] - 1
            result = {"factor": args.factor, "rank_ic": factors.rank_ic(vals, fwd),
                      "samples": len(vals)}
        elif args.cmd == "backtest":
            from . import walkforward
            result = walkforward.run(conn, strategy_id=args.strategy, train=args.train,
                                     test=args.test, step=args.step,
                                     grid={"top_n": [3, 5]},
                                     start=args.start, end=args.end)
```

- [x] 步骤 4：PASS；步骤 5：`git commit -m "feat(core): ic/backtest CLI 子命令与 WP2 端到端测试"`

### WP2 验收记录（执行时填写）

> 2026-09-14 执行（分支 feat/wp2，worktree 隔离）。取证方式：离线合成库（8 标的 × 150 交易日 + 基准 + 估值）走 `trading_core` CLI 真实子命令，与 `tests/test_core_wp2_e2e.py` 同一口径。全量回归 `unittest discover -s tests -p 'test_*.py'`：**293 tests OK**（执行两次确认稳定）。

- 横截面策略 OOS 报告 JSON 原文：

```json
{
 "folds": [
  {"train_end": "2025-04-15", "test_end": "2025-05-10", "params": {"top_n": 3}, "oos_sharpe": 0.506},
  {"train_end": "2025-05-10", "test_end": "2025-06-05", "params": {"top_n": 5}, "oos_sharpe": 0.406},
  {"train_end": "2025-06-05", "test_end": "2025-06-25", "params": {"top_n": 5}, "oos_sharpe": 1.09}
 ],
 "oos_curve": [
  {"t": "2025-04-16", "equity": 1081616.266},
  {"t": "2025-04-17", "equity": 1081102.486},
  {"t": "2025-04-18", "equity": 1049277.248},
  "...共 60 点（测试窗 20 × 3 折）"
 ],
 "summary": {"param_groups": 2, "folds": 3, "oos_sharpe": 0.667, "train": 90, "test": 20, "step": 20}
}
```

- IC 检验输出 JSON 原文：

```json
{"factor": "momentum_60", "rank_ic": 1.0, "samples": 5}
```

- 多重检验自曝字段 `param_groups` 值：**2**（grid `top_n∈[3,5]`，与 walk-forward summary 如实上报一致）

#### 执行偏差披露（计划与实际矛盾，修正确的一方）

1. **共享工作目录冲突**：并行 WP3 代理与执行代理共用同一 checkout 且中途切换分支（reflog 证据），WP2 全部工作隔离到独立 worktree `/home/penn/workspace/dsh-wp2`、提交在 `feat/wp2` 分支。
2. **任务 2/3 测试符号口径**：因子统一收 futu 符号（`SH.600519`），计划测试骨架按裸码 `600519` 落库与其自身的因子调用矛盾 → 测试落库改 futu 符号；`ep` 因子读 `pe_ttm`（依赖锁定表"字段路径以 valuation_values 实测实现为准"，计划 ep 草稿读 `pe` 与之矛盾）。
3. **任务 3 迁移连带**：`tests/test_data_layer.py::test_valuation_fallback_is_a_share_only` 改指向 `trading_core/factors.py`（计划既定"旧测试改指向新位置"）；另加断言锁定 workbench 薄委托不得复活第二份实现。`valuation_values` 增加 `fetcher`/`akshare_module` 注入口（离线测试，仓库既有模式），online 缺省行为逐字不变。
4. **任务 4 断言修正**：计划断言 `|z_E|<|z_B|` 与其自身实现数学矛盾（MAD 截断后离群点仍是截面最大值，E=1.51>B=0.26），改为等价可判定性质：截断后离群点与正常点 z 差距有界（<2.0，不裁剪≈2.23）+ 正常点保持分散（Spread>1.0，不裁剪≈0.007）。
5. **任务 6**：`rsi_signal`/`ma_cross_signal` 实际返回逐 bar 信号 Series 而非标量，`signal()` 统一取 as_of（末根 bar）状态；`trading_datasource.backtest` 导入改调用时惰性解析（库模块不强依赖 sys.path）；测试文件补 datasource 路径。
6. **任务 7/8/9 测试 fixture**：引擎交易日取自 `store.trading_days`（WP1 设计：日历硬依赖），计划测试骨架漏日历 → setUp 补 `upsert_calendar`。任务 8 数据 300 日会产出 10 折与计划断言 3 折矛盾 → 数据裁为 150 日恰好 3 折，保住断言与意图；另修 `int(d)` → `int(d[8:])` 笔误。
7. **引擎健壮性**：`_metrics` 的 IR 在 excess 零方差（空仓等合法状态）除零 → 如实报 0；持仓估值改用最近可用收盘（停牌日不中断）。walkforward summary 增加 `oos_sharpe`（各折均值，OOS 报告头牌指标，任务 9 e2e 依赖）。
