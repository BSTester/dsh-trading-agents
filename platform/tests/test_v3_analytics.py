"""V3 分析类接口的确定性单测：纯函数 + 可注入依赖，**不打网络、不起服务**。

运行::

    cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_analytics -v

覆盖策略：
  * ``server/v3_math.py`` 的每个口径用**构造数据**断言（Kupiec LR/卡方尾概率、Beta 的估计量
    约定、交易日对齐、回测 PIT、最大回撤、z 矩阵/IC 统计）；
  * ``server/v3_analytics.py`` 的六个 handler 逻辑用**假 v3_run**（回放预置信封）驱动，
    断言响应信封的字段/错误码、落盘与自选池读取；
  * ``register()`` 用假 app 断言注册的路径集合，并直接 ``asyncio.run`` 调用 handler
    验证「永远 200 + 信封，绝不抛 500」。

为什么不用 TestClient：真 app 需要完整的数据层/调度器/富途通道，单测要的是**可注入**——
本文件的 ``FakeRun``/``FakeApp`` 让每条分支（含各失败分支）都能确定性复现。
"""

import asyncio
import datetime as dt
import json
import math
import tempfile
import unittest
from pathlib import Path

from server import v3_analytics, v3_math

TRADING_DAYS = 252

ROUTES = {
    ("GET", "/api/v3/risk/analytics"),
    ("GET", "/api/v3/factors/matrix"),
    ("GET", "/api/v3/strategy"),
    ("POST", "/api/v3/strategy/run"),
    ("GET", "/api/v3/ml/sweep"),
    ("POST", "/api/v3/ml/backtest"),
}


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
def make_bars(closes, start="2026-01-01"):
    """把收盘价序列变成日 K 列表（日期逐日递增，跨月/跨年由 ``timedelta`` 负责）。"""
    base = dt.date.fromisoformat(start)
    return [{"t": (base + dt.timedelta(days=index)).isoformat(), "c": float(close)}
            for index, close in enumerate(closes)]


def series_envelope(bars, ticker="X", source="futu/quote_history_kline"):
    return {"ok": True, "value": {"ticker": ticker, "period": "1d", "source": source,
                                  "as_of": bars[-1]["t"] if bars else None,
                                  "count": len(bars), "bars": list(bars)}}


def closes_from(returns, start=100.0):
    """由收益率序列生成累乘收盘价（用于构造 Beta/对齐的确定性样本）。"""
    out = [float(start)]
    for value in returns:
        out.append(out[-1] * (1 + value))
    return out


class FakeRun:
    """``v3_run`` 替身：按工具名回放预置信封（callable 或 dict），并记录每次调用。

    未登记的工具按工具面口径返回 ``v3/unknown-tool``——测试因此也能断言「只调了该调的」。
    """

    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, payload))
        handler = self.handlers.get(name)
        if handler is None:
            return {"ok": False,
                    "error": {"code": "v3/unknown-tool", "message": f"未知工具 {name}"}}
        return handler(payload) if callable(handler) else handler

    def payloads(self, name):
        return [payload for called, payload in self.calls if called == name]


class FakeApp:
    """记录路由的假 app（只需 ``.get``/``.post`` 装饰器协议）。"""

    def __init__(self):
        self.routes = {}

    def _register(self, method, path):
        def decorator(function):
            self.routes[(method, path)] = function
            return function
        return decorator

    def get(self, path):
        return self._register("GET", path)

    def post(self, path):
        return self._register("POST", path)


class FakeRequest:
    """只需 ``await request.body()`` 的最小请求替身。"""

    def __init__(self, body=b""):
        self._body = body

    async def body(self):
        return self._body


def envelope_of(response):
    assert response.status_code == 200, response.status_code
    return json.loads(response.body)


# ---------------------------------------------------------------------------
# 数值工具
# ---------------------------------------------------------------------------
class TestNumericHelpers(unittest.TestCase):
    def test_to_float_rejects_bool_nan_and_garbage(self):
        self.assertEqual(v3_math.to_float("3.5"), 3.5)
        self.assertEqual(v3_math.to_float(7), 7.0)
        for bad in (True, False, None, "abc", float("nan"), float("inf"), [], {}):
            self.assertIsNone(v3_math.to_float(bad), bad)
        self.assertEqual(v3_math.to_float(" 2.5 "), 2.5)

    def test_round_half_up_follows_tofixed_on_the_binary_value(self):
        # toFixed 看的是内存里的二进制值：1.005 实际是 1.00499…，0.125 精确可表示。
        self.assertEqual(v3_math.round_half_up(1.005, 2), 1.0)
        self.assertEqual(v3_math.round_half_up(0.125, 2), 0.13)
        self.assertEqual(v3_math.round_half_up(2.675, 2), 2.67)
        self.assertEqual(v3_math.round_half_up(0.1 + 0.2, 2), 0.3)
        self.assertEqual(v3_math.round_half_up(2.5, 0), 3.0)
        self.assertEqual(v3_math.round_half_up(-2.5, 0), -3.0)
        self.assertIsNone(v3_math.round_half_up(float("nan"), 2))
        self.assertIsNone(v3_math.round_half_up("x", 2))

    def test_mean_and_sample_stdev(self):
        self.assertEqual(v3_math.mean([]), 0.0)
        self.assertEqual(v3_math.mean([1, 2, 3]), 2.0)
        self.assertEqual(v3_math.stdev([5]), 0.0)
        # 样本标准差（n-1）：[1,2,3,4] → sqrt(5/3)
        self.assertAlmostEqual(v3_math.stdev([1, 2, 3, 4]), math.sqrt(5 / 3), places=12)

    def test_max_drawdown_uses_running_peak(self):
        self.assertEqual(v3_math.max_drawdown([]), 0.0)
        self.assertAlmostEqual(v3_math.max_drawdown([1.0, 1.2, 0.9, 1.5]), 0.9 / 1.2 - 1, places=12)
        self.assertEqual(v3_math.max_drawdown([1.0, 1.1, 1.2]), 0.0)


# ---------------------------------------------------------------------------
# 交易日对齐 / 收益
# ---------------------------------------------------------------------------
class TestAlignment(unittest.TestCase):
    def test_align_takes_sorted_intersection(self):
        first = [{"t": "2026-01-03", "c": 3}, {"t": "2026-01-01", "c": 1},
                 {"t": "2026-01-02", "c": 2}]
        second = [{"t": "2026-01-02", "c": 20}, {"t": "2026-01-04", "c": 40},
                  {"t": "2026-01-01", "c": 10}]
        dates, closes = v3_math.align_series({"A": first, "B": second})
        self.assertEqual(dates, ["2026-01-01", "2026-01-02"])
        self.assertEqual(closes["A"], [1.0, 2.0])
        self.assertEqual(closes["B"], [10.0, 20.0])

    def test_align_drops_tickers_without_bars(self):
        dates, closes = v3_math.align_series({
            "A": [{"t": "2026-01-01", "c": 1}, {"t": "2026-01-02", "c": 2}],
            "B": [],
        })
        self.assertEqual(dates, ["2026-01-01", "2026-01-02"])
        self.assertEqual(list(closes), ["A"])

    def test_align_drops_bars_with_bad_close_instead_of_infecting_with_nan(self):
        dates, closes = v3_math.align_series({
            "A": [{"t": "2026-01-01", "c": 1}, {"t": "2026-01-02", "c": "oops"},
                  {"t": "2026-01-03", "c": 3}],
            "B": [{"t": "2026-01-01", "c": 10}, {"t": "2026-01-02", "c": 20},
                  {"t": "2026-01-03", "c": 30}],
        })
        self.assertEqual(dates, ["2026-01-01", "2026-01-03"])
        self.assertEqual(closes["A"], [1.0, 3.0])

    def test_align_empty_input(self):
        self.assertEqual(v3_math.align_series({}), ([], {}))
        self.assertEqual(v3_math.align_series({"A": []}), ([], {}))

    def test_returns_of_and_zero_close(self):
        self.assertEqual(v3_math.returns_of([100.0]), [])
        self.assertAlmostEqual(v3_math.returns_of([100.0, 110.0])[0], 0.1, places=12)
        with self.assertRaises(ValueError):
            v3_math.returns_of([0.0, 10.0])


# ---------------------------------------------------------------------------
# Kupiec POF
# ---------------------------------------------------------------------------
class TestKupiecPof(unittest.TestCase):
    """LR 用闭式手算常数钉住公式（不是把实现再抄一遍）。"""

    def test_no_breach_is_a_rejection(self):
        result = v3_math.kupiec_pof(0, 250, 0.05)
        # LR = -2 * 250 * ln(0.95) = 25.6466
        self.assertAlmostEqual(result["lr"], 25.6466, places=4)
        self.assertFalse(result["pass"])
        self.assertEqual(result["breaches"], 0)
        self.assertEqual(result["observations"], 250)
        self.assertLess(result["pValue"], 0.05)

    def test_breaches_matching_the_expected_rate_pass(self):
        result = v3_math.kupiec_pof(12, 250, 0.05)  # 期望 breaches = 12.5
        self.assertAlmostEqual(result["lr"], 0.0213, places=4)
        self.assertAlmostEqual(result["pValue"], 0.8839, places=4)
        self.assertTrue(result["pass"])

    def test_too_many_breaches_is_a_rejection(self):
        result = v3_math.kupiec_pof(25, 250, 0.05)
        self.assertAlmostEqual(result["lr"], 10.3271, places=4)
        self.assertAlmostEqual(result["pValue"], 0.0013, places=4)
        self.assertFalse(result["pass"])

    def test_all_breaches(self):
        result = v3_math.kupiec_pof(250, 250, 0.05)
        self.assertAlmostEqual(result["lr"], 1497.8661, places=4)
        self.assertFalse(result["pass"])

    def test_lr_is_monotone_in_distance_from_expected_rate(self):
        self.assertLess(v3_math.kupiec_pof(12, 250, 0.05)["lr"],
                        v3_math.kupiec_pof(0, 250, 0.05)["lr"])
        self.assertLess(v3_math.kupiec_pof(12, 250, 0.05)["lr"],
                        v3_math.kupiec_pof(25, 250, 0.05)["lr"])

    def test_degenerate_inputs(self):
        for bad_p in (0.0, 1.0, 1.5):
            result = v3_math.kupiec_pof(3, 100, bad_p)
            self.assertIsNone(result["lr"])
            self.assertIsNone(result["pValue"])
            self.assertIsNone(result["pass"])
        result = v3_math.kupiec_pof(0, 0, 0.05)
        self.assertEqual(result, {"lr": None, "pValue": None, "breaches": 0,
                                  "observations": 0, "pass": None})


# ---------------------------------------------------------------------------
# 组合风险量
# ---------------------------------------------------------------------------
class TestPortfolioRisk(unittest.TestCase):
    def setUp(self):
        # 交替 ±1% 的基准收益率 → 均值≈0，让 Alpha/IR 有闭式期望
        self.returns = [0.01 if index % 2 == 0 else -0.01 for index in range(60)]
        self.bench_closes = closes_from(self.returns)
        self.bench_bars = make_bars(self.bench_closes)
        self.doubled_bars = make_bars(closes_from([2 * value for value in self.returns]))

    def test_beta_estimator_is_population_cov_over_sample_variance(self):
        result = v3_math.portfolio_risk(
            {"A": self.doubled_bars, "B": self.doubled_bars}, self.bench_bars,
            {"A": 0.5, "B": 0.5}, 0.95, None)
        self.assertNotIn("error", result)
        count = len(self.bench_closes) - 1
        # 组合收益 = 2 × 基准收益 ⇒ cov = 2·总体方差、varB = 样本方差
        # ⇒ beta = 2·(n-1)/n（协方差用 1/n，stdev 用 1/(n-1)，参考实现即此约定）
        expected_beta = 2 * (count - 1) / count
        self.assertAlmostEqual(result["beta"], round(expected_beta, 3), places=3)
        self.assertTrue(1.9 < result["beta"] < 2.0)
        self.assertEqual(result["observations"], count)
        self.assertEqual(len(result["equityCurve"]), count)

    def test_alpha_and_ir_are_near_zero_when_benchmark_mean_is_zero(self):
        result = v3_math.portfolio_risk(
            {"A": self.doubled_bars, "B": self.doubled_bars}, self.bench_bars,
            {"A": 0.5, "B": 0.5}, 0.95, None)
        # mp = 2·mb ⇒ alpha = (2mb − beta·mb)·252 ≈ 0；基准均值 ≈ 0 ⇒ 基准年化收益 ≈ 0
        self.assertLess(abs(result["alphaAnnPct"]), 0.01)
        self.assertLess(abs(result["benchmarkAnnReturnPct"]), 0.01)
        self.assertLess(abs(result["ir"]), 0.01)

    def test_weights_are_normalized(self):
        result = v3_math.portfolio_risk(
            {"A": self.doubled_bars, "B": self.doubled_bars}, self.bench_bars,
            {"A": 3, "B": 1}, 0.95, None)
        self.assertAlmostEqual(result["tickers"]["A"], 0.75, places=12)
        self.assertAlmostEqual(result["tickers"]["B"], 0.25, places=12)

    def test_nav_scales_var_and_cvar_amounts_only(self):
        plain = v3_math.portfolio_risk({"A": self.doubled_bars}, None, {"A": 1}, 0.95, None)
        funded = v3_math.portfolio_risk({"A": self.doubled_bars}, None, {"A": 1}, 0.95, 1000000.0)
        self.assertIsNone(plain["varAmount"])
        self.assertIsNone(plain["cvarAmount"])
        self.assertAlmostEqual(funded["varAmount"],
                               round(1000000.0 * funded["varDailyPct"] / 100, 2), places=2)
        self.assertAlmostEqual(funded["varDailyPct"], plain["varDailyPct"], places=9)

    def test_var_uses_the_historical_quantile_and_cvar_the_tail_mean(self):
        # 单标的、权重 1 ⇒ 组合日收益 ≡ returns_of(closes)，期望值可在测试里独立重算
        closes = closes_from([0.01] * 20 + [-0.01] * 20 + [-0.02] * 20)
        bars = make_bars(closes)
        actual = sorted(v3_math.returns_of(closes))
        count = len(actual)
        for confidence in (0.5, 0.95):
            with self.subTest(confidence=confidence):
                result = v3_math.portfolio_risk({"A": bars}, None, {"A": 1}, confidence, None)
                index = max(0, math.floor((1 - confidence) * count) - 1)
                tail = actual[:index + 1]
                self.assertEqual(result["observations"], count)
                self.assertAlmostEqual(result["varDailyPct"],
                                       round(-actual[index] * 100, 3), places=3)
                self.assertAlmostEqual(result["cvarDailyPct"],
                                       round(-sum(tail) / len(tail) * 100, 3), places=3)
                self.assertEqual(result["kupiec"]["breaches"],
                                 sum(1 for value in actual if value < actual[index]))

    def test_lowest_quantile_yields_zero_breaches(self):
        # confidence 足够高时 varIndex 归 0 ⇒ 分位就是最小值，「严格小于」的样本数为 0
        # （参考实现同此口径，不是把 1 个尾部样本算成 breach）
        closes = closes_from([0.01] * 20 + [-0.02] * 20)
        actual = sorted(v3_math.returns_of(closes))
        result = v3_math.portfolio_risk({"A": make_bars(closes)}, None, {"A": 1}, 0.99, None)
        self.assertEqual(max(0, math.floor(0.01 * len(actual)) - 1), 0)
        self.assertAlmostEqual(result["varDailyPct"], round(-actual[0] * 100, 3), places=3)
        self.assertEqual(result["kupiec"]["breaches"], 0)
        self.assertEqual(result["kupiec"]["observations"], len(actual))

    def test_insufficient_alignment_returns_error_not_estimates(self):
        bars = make_bars(closes_from([0.01] * 29))       # 30 根 → 30 个交易日
        result = v3_math.portfolio_risk({"A": bars, "B": bars}, None, {"A": 0.5, "B": 0.5})
        self.assertIn("error", result)
        self.assertIn("30", result["error"])

    def test_benchmark_length_mismatch_disables_beta_only(self):
        short_bench = make_bars(closes_from([0.01] * 20), start="2026-03-01")
        result = v3_math.portfolio_risk(
            {"A": self.doubled_bars, "B": self.doubled_bars}, short_bench,
            {"A": 0.5, "B": 0.5}, 0.95, None)
        self.assertNotIn("error", result)
        self.assertIsNone(result["beta"])
        self.assertIsNone(result["alphaAnnPct"])
        self.assertIsNone(result["ir"])
        self.assertIsNotNone(result["varDailyPct"])

    def test_zero_weight_sum_and_no_series(self):
        self.assertEqual(v3_math.portfolio_risk({}, None, {"A": 1})["error"], "组合无可用的成分序列")
        bars = make_bars(self.bench_closes)
        self.assertEqual(v3_math.portfolio_risk({"A": bars}, None, {"A": 0})["error"],
                         "组合权重之和为 0")

    def test_zero_close_is_reported_not_computed(self):
        bars = [{"t": "2026-01-01", "c": 0.0}, {"t": "2026-01-02", "c": 1.0}]
        result = v3_math.portfolio_risk({"A": bars}, None, {"A": 1})
        self.assertIn("error", result)


# ---------------------------------------------------------------------------
# 回测（PIT）
# ---------------------------------------------------------------------------
class TestBacktest(unittest.TestCase):
    def test_pit_signal_uses_only_past_closes(self):
        # d05 从 100 跳到 200（+100%）：这一天必须**空仓**吃到 0，信号只在次日生效。
        closes = [100, 100, 100, 100, 100, 200, 150, 160, 140, 170]
        bars = make_bars(closes, start="2026-01-05")
        result = v3_math.backtest_momentum(bars, window=5, rebalance_days=1)
        self.assertNotIn("error", result)
        self.assertEqual(result["positions"], [0, 0, 0, 0, 0, 1, 1, 1, 1])
        self.assertEqual(result["strategyReturns"][4], 0.0)          # 跳涨当日空仓
        self.assertAlmostEqual(result["strategyReturns"][5], 150 / 200 - 1, places=12)
        self.assertAlmostEqual(result["equity"][5]["value"], 0.75, places=6)
        self.assertEqual(result["metrics"]["heldDays"], 4)
        self.assertEqual(result["metrics"]["flatDays"], 5)
        self.assertEqual(result["metrics"]["days"], 9)
        self.assertEqual(result["metrics"]["signalFlips"], 0)
        self.assertEqual(result["equity"][0]["t"], "2026-01-06")

    def test_first_window_days_are_flat(self):
        closes = [100 + index for index in range(30)]
        result = v3_math.backtest_momentum(make_bars(closes), window=7, rebalance_days=3)
        self.assertEqual(result["positions"][:7], [0] * 7)
        self.assertTrue(all(value >= 0 for value in result["positions"]))

    def test_mutating_the_last_close_cannot_change_any_position(self):
        closes = [100 * (1.001 ** index) for index in range(40)]
        bars = make_bars(closes)
        base = v3_math.backtest_momentum(bars, window=5, rebalance_days=1)
        mutated = [dict(bar) for bar in bars]
        mutated[-1]["c"] = mutated[-1]["c"] * 1.5
        after = v3_math.backtest_momentum(mutated, window=5, rebalance_days=1)
        self.assertEqual(base["positions"], after["positions"])
        self.assertEqual(base["equity"][:-1], after["equity"][:-1])
        self.assertNotEqual(base["equity"][-1], after["equity"][-1])
        self.assertEqual(base["positions"][-1], 1)

    def test_win_rate_counts_held_days_only(self):
        closes = [100, 100, 100, 100, 100, 200, 150, 160, 140, 170]
        result = v3_math.backtest_momentum(make_bars(closes), window=5, rebalance_days=1)
        metrics = result["metrics"]
        self.assertEqual(metrics["winRatePct"], 50.0)  # 4 个持仓日里 2 天为正
        self.assertEqual(metrics["flatDays"], metrics["days"] - metrics["heldDays"])

    def test_insufficient_bars_and_bad_parameters(self):
        bars = make_bars([100, 101, 102])
        result = v3_math.backtest_momentum(bars, window=5, rebalance_days=5)
        self.assertIn("need >= 7, got 3", result["error"])
        self.assertIn("必须为正整数", v3_math.backtest_momentum(bars, window=0)["error"])
        self.assertIn("必须为正整数", v3_math.backtest_momentum(bars, rebalance_days=0)["error"])

    def test_non_finite_closes_are_skipped(self):
        bars = [{"t": "2026-01-01", "c": 100}, {"t": "2026-01-02", "c": None},
                {"t": "2026-01-03", "c": 110}, {"t": "2026-01-04", "c": 120},
                {"t": "2026-01-05", "c": 130}, {"t": "2026-01-06", "c": 125},
                {"t": "2026-01-07", "c": 135}]
        result = v3_math.backtest_momentum(bars, window=2, rebalance_days=1)
        self.assertNotIn("error", result)
        self.assertEqual(result["metrics"]["days"], 5)
        self.assertEqual(result["equity"][0]["t"], "2026-01-03")

    def test_param_sweep_grid_and_best(self):
        closes = [100 * (1.002 ** index) + 3 * math.sin(index / 2.0) for index in range(120)]
        sweep = v3_math.param_sweep(make_bars(closes), [5, 10, 500], [5, 10])
        self.assertEqual(len(sweep["grid"]), 6)
        failed = [row for row in sweep["grid"] if "error" in row]
        self.assertEqual(len(failed), 2)
        for row in failed:
            self.assertIsNone(row["sharpe"])
            self.assertIsNone(row["annReturnPct"])
            self.assertIsNone(row["maxDrawdownPct"])
        valid = [row for row in sweep["grid"] if row["sharpe"] is not None]
        self.assertEqual(sweep["best"]["sharpe"], max(row["sharpe"] for row in valid))
        self.assertIn(sweep["best"], valid)

    def test_param_sweep_all_failed_gives_null_best(self):
        sweep = v3_math.param_sweep(make_bars([100, 101, 102]), [50], [5])
        self.assertIsNone(sweep["best"])
        self.assertEqual(len(sweep["grid"]), 1)


# ---------------------------------------------------------------------------
# 因子矩阵 / IC / 组合定义
# ---------------------------------------------------------------------------
class TestFactorMath(unittest.TestCase):
    ROWS = [
        {"ticker": "A", "as_of": "2026-09-17", "factors": {"mom_20": 0.04},
         "z": {"mom_20": 1.23456, "mom_60": 0.5, "trend": -1.0}},
        {"ticker": "B", "as_of": "2026-09-18", "factors": {"mom_20": -0.01},
         "z": {"mom_20": -0.5, "trend": 2.0}},
        {"ticker": "C", "as_of": "2026-09-18", "factors": None,
         "z": {"mom_20": None, "peg": "oops"}},
    ]

    def test_matrix_columns_are_sorted_union_and_missing_cells_are_null(self):
        matrix = v3_math.factor_matrix(self.ROWS)
        self.assertEqual(matrix["tickers"], ["A", "B", "C"])
        self.assertEqual(matrix["factors"], ["mom_20", "mom_60", "peg", "trend"])
        self.assertEqual(matrix["as_of"], "2026-09-18")
        self.assertEqual(matrix["matrix"][0], [1.2346, 0.5, None, -1.0])
        self.assertEqual(matrix["matrix"][1], [-0.5, None, None, 2.0])
        self.assertEqual(matrix["matrix"][2], [None, None, None, None])
        self.assertEqual(matrix["raw"][2], {"ticker": "C", "factors": None})

    def test_matrix_handles_empty_rows(self):
        matrix = v3_math.factor_matrix([])
        self.assertEqual(matrix["matrix"], [])
        self.assertIsNone(matrix["as_of"])

    def test_ic_stats_are_icir_not_annualized(self):
        value = {"tickers": ["A", "B", "C"],
                 "points": [{"t": "2026-09-01", "ic": 0.5}, {"t": "2026-09-02", "ic": -0.1},
                            {"t": "2026-09-03", "ic": "bad"}, {"t": "2026-09-04", "ic": 0.2}]}
        stats = v3_math.ic_stats(value, "mom_20", 5, fallback_tickers=["A", "B", "C"])
        self.assertEqual(stats["observations"], 3)
        self.assertEqual(stats["meanIc"], 0.2)
        expected_std = math.sqrt(((0.5 - 0.2) ** 2 + (-0.1 - 0.2) ** 2 + (0.2 - 0.2) ** 2) / 2)
        self.assertAlmostEqual(stats["stdIc"], round(expected_std, 4), places=4)
        self.assertAlmostEqual(stats["ir"], round(0.2 / expected_std, 3), places=3)
        self.assertEqual(stats["latestIc"], 0.2)
        self.assertEqual([point["t"] for point in stats["points"]],
                         ["2026-09-01", "2026-09-02", "2026-09-04"])

    def test_ic_stats_keeps_only_the_last_40_points(self):
        points = [{"t": f"2026-01-{index:02d}", "ic": index / 100.0} for index in range(1, 46)]
        stats = v3_math.ic_stats({"points": points}, "mom_20", 5)
        self.assertEqual(stats["observations"], 45)
        self.assertEqual(len(stats["points"]), 40)
        self.assertEqual(stats["points"][0]["t"], "2026-01-06")

    def test_ic_stats_with_single_point_has_no_std_or_ir(self):
        stats = v3_math.ic_stats({"points": [{"t": "t", "ic": 0.3}]}, "mom_20", 5)
        self.assertEqual(stats["observations"], 1)
        self.assertIsNone(stats["stdIc"])
        self.assertIsNone(stats["ir"])
        self.assertEqual(stats["latestIc"], 0.3)

    def test_composite_z_averages_only_momentum_and_trend(self):
        row = {"z": {"mom_20": 1.0, "mom_60": 0.5, "trend": 1.5, "rsi_14": 99.0, "pe_ttm": -9.0}}
        self.assertAlmostEqual(v3_math.composite_z(row), 1.0, places=12)
        self.assertAlmostEqual(v3_math.composite_z({"z": {"trend": 2.0}}), 2.0, places=12)
        self.assertIsNone(v3_math.composite_z({"z": {"rsi_14": 1.0}}))
        self.assertIsNone(v3_math.composite_z(None))

    def test_cross_sectional_z(self):
        values = [1.0, 2.0, 3.0]
        zs = v3_math.cross_sectional_z(values)
        self.assertAlmostEqual(sum(zs), 0.0, places=12)
        self.assertAlmostEqual(zs[1], 0.0, places=12)
        self.assertAlmostEqual(zs[0], -1.0, places=12)
        self.assertEqual(v3_math.cross_sectional_z([1.0, None]), [None, None])
        self.assertEqual(v3_math.cross_sectional_z([2.0, 2.0]), [0.0, 0.0])
        self.assertEqual(v3_math.cross_sectional_z([None, 5.0, None]), [None, None, None])


class TestPortfolioResolution(unittest.TestCase):
    def test_first_frozen_plan_with_two_targets_wins(self):
        plans = [
            {"plan_id": "P1", "status": "frozen", "target": {"A": 0.5}},
            {"plan_id": "P2", "status": "executed", "target": {"A": 0.5, "B": 0.5}},
            {"plan_id": "P3", "status": "frozen", "target": {"A": 0.3, "B": 0.7}},
        ]
        weights, source = v3_math.pick_frozen_plan_weights(plans)
        self.assertEqual(weights, {"A": 0.3, "B": 0.7})
        self.assertEqual(source, "工作台 frozen 计划 P3")

    def test_non_positive_and_invalid_weights_are_dropped(self):
        plans = [{"plan_id": "P1", "status": "FROZEN",
                  "target": {"A": 0.5, "B": 0, "C": "oops", "D": 0.5}}]
        weights, source = v3_math.pick_frozen_plan_weights(plans)
        self.assertEqual(weights, {"A": 0.5, "D": 0.5})
        self.assertEqual(source, "工作台 frozen 计划 P1")
        self.assertEqual(v3_math.pick_frozen_plan_weights([{"target": {"A": 1, "B": 1}}]),
                         (None, None))
        self.assertEqual(v3_math.pick_frozen_plan_weights(None), (None, None))

    def test_watchlist_equal_weights_and_empty_pool(self):
        weights, source = v3_math.watchlist_equal_weights(["A", "B", "C", "D"])
        self.assertEqual(weights, {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25})
        self.assertEqual(source, "自选池等权（4 只）")
        self.assertEqual(v3_math.watchlist_equal_weights([]),
                         (None, "无可用组合定义（既无多标的计划，自选池也为空）"))
        weights, _ = v3_math.watchlist_equal_weights([f"T{index}" for index in range(12)], limit=8)
        self.assertEqual(len(weights), 8)


# ---------------------------------------------------------------------------
# 路由级：假 v3_run + 临时 home
# ---------------------------------------------------------------------------
class RouteCase(unittest.TestCase):
    """所有路由级用例共用：临时 home + 便捷断言。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def write_watchlist(self, names):
        path = Path(self.home) / "trading-platform.json"
        path.write_text(json.dumps({"watchlist": names}, ensure_ascii=False), encoding="utf-8")
        return path

    def assertError(self, result, code):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], code, result["error"])
        self.assertTrue(result["error"]["message"], result["error"])


class TestRiskAnalyticsRoute(RouteCase):
    def _risk_run(self, ticker_count=2, days=60, plan=None, equity=1000000.0,
                  bench_ok=True, failing=()):
        """构造假取数：``days`` 个交易日（= ``days`` 根日 K、``days-1`` 个收益观测）。"""
        closes = closes_from([0.01 if index % 2 == 0 else -0.008
                              for index in range(max(1, days - 1))])
        tickers = [f"T{index}" for index in range(ticker_count)]

        def series(payload):
            ticker = payload["ticker"]
            if ticker in failing:
                return {"ok": False,
                        "error": {"code": "trading/futu-unavailable", "message": "上游不可用"}}
            if ticker == "SH.000300" and not bench_ok:
                return {"ok": False,
                        "error": {"code": "trading/futu-unavailable", "message": "基准取不到"}}
            return series_envelope(make_bars(closes), ticker=ticker)

        return FakeRun({"series": series,
                        "plan": plan if plan is not None else {"ok": True, "value": {"plans": []}},
                        "equity": {"ok": True, "value": {"current": equity}}}), tickers

    def test_explicit_weights_drive_the_portfolio(self):
        run, _ = self._risk_run()
        result = v3_analytics.risk_analytics(run, self.home, limit=250, confidence=0.95,
                                             benchmark="SH.000300",
                                             weights_raw='{"T0": 3, "T1": 1}')
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["portfolioSource"], "请求显式权重")
        self.assertAlmostEqual(result["analytics"]["tickers"]["T0"], 0.75, places=12)
        self.assertEqual(result["analytics"]["observations"], 59)
        self.assertEqual(result["nav"], 1000000.0)
        self.assertEqual(result["sources"]["kline"], "futu/quote_history_kline")
        self.assertEqual(result["sources"]["nav"], "sim-ledger(equity.current)")
        for key in ("confidence", "observations", "tickers", "window", "varDailyPct",
                    "cvarDailyPct", "varAmount", "cvarAmount", "annVolPct", "annReturnPct",
                    "maxDrawdownPct", "beta", "alphaAnnPct", "ir", "benchmarkAnnReturnPct",
                    "kupiec", "equityCurve", "method"):
            self.assertIn(key, result["analytics"], key)
        for key in ("lr", "pValue", "breaches", "observations", "pass"):
            self.assertIn(key, result["analytics"]["kupiec"], key)
        self.assertEqual(set(result["analytics"]["kupiec"]),
                         {"lr", "pValue", "breaches", "observations", "pass"})
        first = result["analytics"]["equityCurve"][0]
        self.assertEqual(set(first), {"t", "v"})
        self.assertEqual(result["analytics"]["window"]["from"], "2026-01-01")

    def test_frozen_plan_is_used_before_the_watchlist(self):
        plan = {"ok": True, "value": {"plans": [
            {"plan_id": "PLN-1", "status": "frozen", "target": {"T0": 1, "T1": 1}}]}}
        run, _ = self._risk_run(plan=plan)
        self.write_watchlist(["OTHER"])
        result = v3_analytics.risk_analytics(run, self.home)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["portfolioSource"], "工作台 frozen 计划 PLN-1")

    def test_watchlist_equal_weight_is_the_last_resort(self):
        run, tickers = self._risk_run()
        self.write_watchlist(tickers)
        result = v3_analytics.risk_analytics(run, self.home)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["portfolioSource"], "自选池等权（2 只）")
        self.assertAlmostEqual(result["analytics"]["tickers"]["T0"], 0.5, places=12)

    def test_no_portfolio_source(self):
        run, _ = self._risk_run()
        result = v3_analytics.risk_analytics(run, self.home)
        self.assertError(result, "risk/no-portfolio")

    def test_single_name_is_rejected(self):
        run, _ = self._risk_run()
        result = v3_analytics.risk_analytics(run, self.home, weights_raw='{"T0": 1}')
        self.assertError(result, "risk/single-name")

    def test_bad_weights_json_and_bad_confidence(self):
        run, _ = self._risk_run()
        self.assertError(v3_analytics.risk_analytics(run, self.home, weights_raw="{oops"),
                         "bad-request")
        self.assertError(v3_analytics.risk_analytics(run, self.home, weights_raw="[]"),
                         "bad-request")
        self.assertError(v3_analytics.risk_analytics(run, self.home, weights_raw='{"T0":1,"T1":1}',
                                                     confidence=1.5), "risk/bad-confidence")
        self.assertError(v3_analytics.risk_analytics(run, self.home, weights_raw='{"T0":1,"T1":1}',
                                                     confidence="abc"), "risk/bad-confidence")

    def test_insufficient_alignment_is_an_error_envelope(self):
        run, _ = self._risk_run(days=30)
        result = v3_analytics.risk_analytics(run, self.home, weights_raw='{"T0":1,"T1":1}')
        self.assertError(result, "risk/insufficient")
        self.assertIn("30", result["error"]["message"])

    def test_benchmark_failure_keeps_the_risk_numbers(self):
        run, _ = self._risk_run(bench_ok=False)
        result = v3_analytics.risk_analytics(run, self.home, weights_raw='{"T0":1,"T1":1}')
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["benchmarkTicker"])
        self.assertIsNone(result["analytics"]["beta"])
        self.assertIsNone(result["analytics"]["benchmarkAnnReturnPct"])
        self.assertIsNotNone(result["analytics"]["varDailyPct"])

    def test_failed_component_is_reported_in_sources_errors(self):
        run, _ = self._risk_run(ticker_count=3, failing=("T2",))
        result = v3_analytics.risk_analytics(
            run, self.home, weights_raw='{"T0":1,"T1":1,"T2":1}')
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sources"]["errors"],
                         [{"ticker": "T2", "error": "上游不可用"}])
        self.assertNotIn("T2", result["analytics"]["tickers"])

    def test_nav_missing_is_null_with_an_explanation(self):
        run, _ = self._risk_run(equity=0)
        result = v3_analytics.risk_analytics(run, self.home, weights_raw='{"T0":1,"T1":1}')
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["nav"])
        self.assertIsNone(result["analytics"]["varAmount"])
        self.assertIsNone(result["sources"]["nav"])
        self.assertIn("nav 取不到", result["navNote"])

    def test_limit_is_clamped(self):
        run, _ = self._risk_run()
        # 每轮 = 2 个成分 + 1 个基准 = 3 次 series 调用（同轮 limit 相同）
        v3_analytics.risk_analytics(run, self.home, limit=5, weights_raw='{"T0":1,"T1":1}')
        v3_analytics.risk_analytics(run, self.home, limit=99999, weights_raw='{"T0":1,"T1":1}')
        v3_analytics.risk_analytics(run, self.home, limit="abc", weights_raw='{"T0":1,"T1":1}')
        limits = [payload["limit"] for payload in run.payloads("series")]
        self.assertEqual(limits, [60, 60, 60, 2000, 2000, 2000, 250, 250, 250])

    def test_tool_exception_becomes_an_envelope(self):
        def boom(payload):
            raise RuntimeError("子进程挂了")

        run = FakeRun({"series": boom})
        result = v3_analytics.risk_analytics(run, self.home, weights_raw='{"T0":1,"T1":1}')
        self.assertError(result, "risk/insufficient")


class TestFactorsMatrixRoute(RouteCase):
    ROWS = [
        {"ticker": "A", "as_of": "2026-09-18",
         "factors": {"mom_20": 0.04, "mom_60": 0.1, "rsi_14": 55.0, "mdd_60": -0.05},
         "z": {"mom_20": 1.0, "mom_60": 0.5, "trend": 0.25}},
        {"ticker": "B", "as_of": "2026-09-18",
         "factors": {"mom_20": -0.02, "mom_60": -0.1, "rsi_14": 41.0, "mdd_60": -0.3},
         "z": {"mom_20": -1.0, "mom_60": -0.5, "trend": -0.25}},
        {"ticker": "C", "as_of": "2026-09-18", "factors": {"mom_20": 0.0},
         "z": {"mom_20": 0.0, "mom_60": None, "trend": None}},
    ]
    IC = {"ok": True, "value": {"factor": "mom_20", "tickers": ["A", "B", "C"],
                                "forward_days": 5,
                                "points": [{"t": "2026-09-01", "ic": 0.5},
                                           {"t": "2026-09-08", "ic": -0.2}]}}

    def test_matrix_and_ic_payloads(self):
        run = FakeRun({"factors": {"ok": True, "value": {"rows": self.ROWS,
                                                         "failures": {"D": "取不到"}}},
                       "ic": self.IC})
        result = v3_analytics.factors_matrix_data(run, self.home, tickers_raw="A,B,C")
        self.assertTrue(result["ok"], result)
        matrix = result["matrix"]
        self.assertEqual(matrix["tickers"], ["A", "B", "C"])
        self.assertEqual(matrix["factors"], ["mom_20", "mom_60", "trend"])
        self.assertEqual(matrix["matrix"], [[1.0, 0.5, 0.25], [-1.0, -0.5, -0.25],
                                            [0.0, None, None]])
        self.assertEqual(matrix["as_of"], "2026-09-18")
        self.assertEqual(matrix["raw"][0]["factors"]["rsi_14"], 55.0)
        self.assertEqual(matrix["failures"], {"D": "取不到"})
        self.assertEqual(result["ic"]["observations"], 2)
        self.assertEqual(result["ic"]["meanIc"], 0.15)
        self.assertEqual(result["ic"]["factor"], "mom_20")
        self.assertEqual(result["ic"]["points"],
                         [{"t": "2026-09-01", "ic": 0.5}, {"t": "2026-09-08", "ic": -0.2}])

    def test_ic_tool_field_is_named_forward(self):
        run = FakeRun({"factors": {"ok": True, "value": {"rows": self.ROWS}},
                       "ic": self.IC})
        v3_analytics.factors_matrix_data(run, self.home, tickers_raw="A,B,C", forward_days=7)
        payload = run.payloads("ic")[0]
        self.assertEqual(payload["forward"], 7)
        self.assertNotIn("forward_days", payload)
        self.assertEqual(payload["factor"], "mom_20")

    def test_ic_failure_does_not_break_the_matrix(self):
        run = FakeRun({"factors": {"ok": True, "value": {"rows": self.ROWS}},
                       "ic": {"ok": False, "error": {"code": "trading/unavailable",
                                                     "message": "IC 不可用"}}})
        result = v3_analytics.factors_matrix_data(run, self.home, tickers_raw="A,B,C")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["matrix"]["matrix"])
        self.assertFalse(result["ic"]["ok"])
        self.assertEqual(result["ic"]["error"]["code"], "trading/unavailable")
        self.assertEqual(result["ic"]["factor"], "mom_20")

    def test_ic_requires_three_names_but_matrix_does_not(self):
        run = FakeRun({"factors": {"ok": True, "value": {"rows": self.ROWS[:2]}},
                       "ic": self.IC})
        result = v3_analytics.factors_matrix_data(run, self.home, tickers_raw="A,B")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["ic"]["ok"])
        self.assertEqual(result["ic"]["error"]["code"], "factors/too-few")
        self.assertFalse(run.payloads("ic"))

    def test_watchlist_default_and_truncation_to_eight(self):
        names = [f"T{index}" for index in range(10)]
        self.write_watchlist(names)
        run = FakeRun({"factors": {"ok": True, "value": {"rows": []}},
                       "ic": {"ok": True, "value": {"points": []}}})
        v3_analytics.factors_matrix_data(run, self.home)
        self.assertEqual(run.payloads("factors")[0]["tickers"], names[:6])
        run.calls.clear()
        v3_analytics.factors_matrix_data(run, self.home, tickers_raw=",".join(names))
        self.assertEqual(run.payloads("factors")[0]["tickers"], names[:8])

    def test_too_few_names_is_an_error(self):
        run = FakeRun()
        self.assertError(v3_analytics.factors_matrix_data(run, self.home, tickers_raw="A"),
                         "factors/too-few")

    def test_factors_failure_is_propagated(self):
        run = FakeRun({"factors": {"ok": False, "error": {"code": "trading/unavailable",
                                                          "message": "因子不可用"}}})
        result = v3_analytics.factors_matrix_data(run, self.home, tickers_raw="A,B,C")
        self.assertError(result, "trading/unavailable")


class TestStrategyRoute(RouteCase):
    def _run(self, tickers=("A", "B", "C", "D"), factors_ok=True, bars=200):
        closes = closes_from([0.002 * math.sin(index / 4.0) + 0.001
                              for index in range(max(1, bars - 1))])

        def series(payload):
            return series_envelope(make_bars(closes), ticker=payload["ticker"])

        def factors(payload):
            if not factors_ok:
                return {"ok": False, "error": {"code": "trading/unavailable",
                                               "message": "workbench factors 不可用"}}
            rows = []
            for index, ticker in enumerate(payload["tickers"]):
                rows.append({"ticker": ticker, "as_of": "2026-09-18",
                             "factors": {"mom_20": 0.04, "mom_60": 0.1, "rsi_14": 55.0,
                                         "mdd_60": -0.05},
                             "z": {"mom_20": 1.0 - index, "mom_60": 0.5 - index,
                                   "trend": 0.25 - index}})
            return {"ok": True, "value": {"rows": rows}}

        return FakeRun({"series": series, "factors": factors})

    def test_never_run_reports_a_note(self):
        result = v3_analytics.strategy_last(self.home)
        self.assertEqual(result, {"ok": True, "run": None, "note": "尚未运行研究流水线"})

    def test_pipeline_produces_stages_and_proposals(self):
        run = self._run()
        self.write_watchlist(["A", "B", "C", "D"])
        result = v3_analytics.strategy_run(run, self.home, {"topN": 2})
        self.assertTrue(result["ok"], result)
        summary = result["run"]
        self.assertEqual(summary["universe"], ["A", "B", "C", "D"])
        self.assertRegex(summary["asOf"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        stages = summary["stages"]
        self.assertEqual(set(stages), {"PDAT", "PAAT", "PCPT", "PRT", "PET"})
        self.assertEqual(stages["PDAT"]["universe"], ["A", "B", "C", "D"])
        self.assertEqual(stages["PDAT"]["bars"], 800)          # 4 标的 × 200 根
        self.assertEqual(stages["PDAT"]["errors"], [])
        self.assertEqual(stages["PAAT"]["analyzed"], 4)
        self.assertEqual(stages["PAAT"]["withFactors"], 4)
        self.assertEqual(stages["PAAT"]["scoreSource"], "workbench/factors(z)")
        self.assertIsNone(stages["PAAT"]["factorsError"])
        self.assertEqual(stages["PCPT"]["longs"], ["A", "B"])
        self.assertEqual(stages["PCPT"]["reduces"], ["D", "C"])
        self.assertEqual(stages["PRT"]["weightPctPerName"], 2.0)
        self.assertTrue(stages["PRT"]["capped"])
        self.assertEqual(stages["PET"]["proposals"], 4)
        self.assertEqual(len(summary["proposals"]), 4)
        long_proposal = summary["proposals"][0]
        self.assertEqual(set(long_proposal),
                         {"ticker", "action", "targetWeightPct", "basis", "riskLevel",
                          "action_hint"})
        self.assertEqual(long_proposal["action"], "增持")
        self.assertEqual(long_proposal["targetWeightPct"], 2.0)
        self.assertEqual(long_proposal["riskLevel"], "低")
        self.assertIn("综合动量 z=", long_proposal["basis"])
        self.assertIn("mom_20=0.04", long_proposal["basis"])
        self.assertEqual(summary["proposals"][2]["action"], "减持")
        self.assertEqual(summary["proposals"][2]["targetWeightPct"], 0)
        self.assertEqual(summary["proposals"][2]["riskLevel"], "中")
        self.assertIn("排名末位", summary["proposals"][2]["basis"])

    def test_series_limit_follows_the_window(self):
        run = self._run()
        v3_analytics.strategy_run(run, self.home, {"universe": ["A", "B"], "window": 40})
        self.assertEqual(run.payloads("series")[0]["limit"], 160)

    def test_series_failures_are_listed_in_pdat(self):
        closes = closes_from([0.002] * 199)          # 200 根日 K

        def series(payload):
            if payload["ticker"] == "B":
                return {"ok": False, "error": {"code": "trading/unavailable",
                                               "message": "B 取不到"}}
            return series_envelope(make_bars(closes), ticker=payload["ticker"])

        run = FakeRun({"series": series,
                       "factors": {"ok": True, "value": {"rows": []}}})
        result = v3_analytics.strategy_run(run, self.home, {"universe": ["A", "B"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["run"]["stages"]["PDAT"]["errors"],
                         [{"ticker": "B", "error": "B 取不到"}])
        self.assertEqual(result["run"]["stages"]["PDAT"]["bars"], 200)

    def test_factor_fallback_is_labelled(self):
        run = self._run(factors_ok=False)
        result = v3_analytics.strategy_run(run, self.home, {"universe": ["A", "B", "C"]})
        self.assertTrue(result["ok"], result)
        paat = result["run"]["stages"]["PAAT"]
        self.assertEqual(paat["scoreSource"],
                         "local/series 动量横截面 z（workbench factors 不可用时的兜底）")
        self.assertEqual(paat["factorsError"]["code"], "trading/unavailable")
        self.assertEqual(paat["withFactors"], 3)

    def test_no_double_listing_when_universe_is_small(self):
        run = self._run()
        result = v3_analytics.strategy_run(run, self.home, {"universe": ["A", "B"], "topN": 2})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["run"]["stages"]["PCPT"]["longs"], ["A", "B"])
        self.assertEqual(result["run"]["stages"]["PCPT"]["reduces"], [])
        self.assertEqual([item["action"] for item in result["run"]["proposals"]],
                         ["增持", "增持"])

    def test_empty_universe_is_an_error(self):
        run = self._run()
        self.assertError(v3_analytics.strategy_run(run, self.home, {}), "strategy/no-universe")

    def test_result_is_persisted_and_read_back(self):
        run = self._run()
        first = v3_analytics.strategy_run(run, self.home, {"universe": ["A", "B"]})
        self.assertTrue(first["ok"], first)
        path = Path(self.home) / v3_analytics.STRATEGY_RUNS_FILE
        self.assertTrue(path.is_file())
        self.assertEqual(len(path.read_text(encoding="utf-8").strip().splitlines()), 1)
        last = v3_analytics.strategy_last(self.home)
        self.assertTrue(last["ok"])
        self.assertEqual(last["run"]["asOf"], first["run"]["asOf"])
        second = v3_analytics.strategy_run(run, self.home, {"universe": ["C", "D"]})
        self.assertTrue(second["ok"], second)
        self.assertEqual(len(path.read_text(encoding="utf-8").strip().splitlines()), 2)
        self.assertEqual(v3_analytics.strategy_last(self.home)["run"]["universe"], ["C", "D"])

    def test_corrupt_lines_are_skipped(self):
        path = Path(self.home) / v3_analytics.STRATEGY_RUNS_FILE
        path.write_text('{"asOf": "first"}\nnot json\n', encoding="utf-8")
        self.assertEqual(v3_analytics.read_last_strategy_run(self.home), {"asOf": "first"})
        path.write_text("not json\n", encoding="utf-8")
        self.assertIsNone(v3_analytics.read_last_strategy_run(self.home))
        self.assertEqual(v3_analytics.strategy_last(self.home)["run"], None)

    def test_missing_home_directory_is_tolerated(self):
        missing = str(Path(self.home) / "nope" / "deeper")
        self.assertEqual(v3_analytics.read_watchlist(missing), [])
        self.assertIsNone(v3_analytics.read_last_strategy_run(missing))

    def test_corrupt_platform_config_gives_an_empty_watchlist(self):
        (Path(self.home) / "trading-platform.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(v3_analytics.read_watchlist(self.home), [])


class TestMlRoutes(RouteCase):
    def _bars(self, count=120):
        return make_bars([100 * (1.001 ** index) + 2 * math.sin(index / 3.0)
                          for index in range(count)])

    def test_sweep_grid(self):
        run = FakeRun({"series": series_envelope(self._bars())})
        result = v3_analytics.ml_sweep(run, ticker="SH.600519", windows_raw="5,10",
                                       rebalance_raw="5,10", limit=500)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ticker"], "SH.600519")
        self.assertEqual(len(result["grid"]), 4)
        for row in result["grid"]:
            self.assertEqual(set(row), {"window", "rebalanceDays", "sharpe", "annReturnPct",
                                        "maxDrawdownPct"})
        self.assertEqual(run.payloads("series")[0],
                         {"ticker": "SH.600519", "period": "1d", "limit": 500})
        valid = [row for row in result["grid"] if row["sharpe"] is not None]
        self.assertEqual(result["best"]["sharpe"], max(row["sharpe"] for row in valid))

    def test_sweep_records_per_cell_errors_without_filling_zeros(self):
        run = FakeRun({"series": series_envelope(self._bars(80))})
        result = v3_analytics.ml_sweep(run, windows_raw="5,500", rebalance_raw="5")
        self.assertTrue(result["ok"], result)
        failed = [row for row in result["grid"] if "error" in row]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["window"], 500)
        self.assertIsNone(failed[0]["sharpe"])

    def test_sweep_rejects_unparsable_grids(self):
        run = FakeRun({"series": series_envelope(self._bars())})
        self.assertError(v3_analytics.ml_sweep(run, windows_raw="abc"), "bad-request")
        self.assertError(v3_analytics.ml_sweep(run, rebalance_raw="0,-3"), "bad-request")
        self.assertFalse(run.payloads("series"))

    def test_sweep_defaults_and_propagated_failure(self):
        run = FakeRun({"series": series_envelope(self._bars())})
        result = v3_analytics.ml_sweep(run, windows_raw="", rebalance_raw="")
        self.assertEqual(len(result["grid"]), 12)
        broken = FakeRun({"series": {"ok": False, "error": {"code": "trading/unavailable",
                                                            "message": "K 线不可用"}}})
        self.assertError(v3_analytics.ml_sweep(broken), "trading/unavailable")

    def test_backtest_contract(self):
        run = FakeRun({"series": series_envelope(self._bars())})
        result = v3_analytics.ml_backtest(run, {"ticker": "SH.600519", "window": 20,
                                                "rebalanceDays": 5, "limit": 500})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ticker"], "SH.600519")
        self.assertEqual(set(result["metrics"]),
                         {"sharpe", "annReturnPct", "maxDrawdownPct", "signalFlips",
                          "heldDays", "flatDays", "days", "winRatePct"})
        self.assertEqual(result["metrics"]["days"], 119)
        self.assertEqual(len(result["equity"]), 119)
        self.assertEqual(set(result["equity"][0]), {"t", "value"})
        self.assertEqual(set(result), {"ok", "ticker", "metrics", "equity"})

    def test_backtest_defaults_and_insufficient(self):
        run = FakeRun({"series": series_envelope(self._bars(120))})
        result = v3_analytics.ml_backtest(run, {"ticker": "SH.600519"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.payloads("series")[0]["limit"], 500)   # limit 缺省
        self.assertEqual(result["metrics"]["days"], 119)

        short = FakeRun({"series": series_envelope(self._bars(30))})
        failed = v3_analytics.ml_backtest(short, {"ticker": "SH.600519", "window": 60})
        self.assertError(failed, "backtest/insufficient")
        self.assertIn("need >= 62", failed["error"]["message"])

    def test_backtest_requires_a_ticker(self):
        run = FakeRun({})
        self.assertError(v3_analytics.ml_backtest(run, {}), "bad-request")
        self.assertError(v3_analytics.ml_backtest(run, {"ticker": "  "}), "bad-request")
        self.assertFalse(run.calls)


class TestRegisterRoutes(RouteCase):
    def _app(self, run):
        app = FakeApp()
        v3_analytics.register(app, run, self.home)
        return app

    def test_registers_exactly_the_agreed_paths(self):
        app = self._app(FakeRun({}))
        self.assertEqual(set(app.routes), ROUTES)
        for function in app.routes.values():
            self.assertTrue(asyncio.iscoroutinefunction(function), function)

    def test_get_handlers_return_200_envelopes(self):
        closes = closes_from([0.01 if index % 2 == 0 else -0.008 for index in range(60)])

        def series(payload):
            return series_envelope(make_bars(closes), ticker=payload["ticker"])

        rows = [{"ticker": ticker, "as_of": "2026-09-18",
                 "factors": {"mom_20": 0.04}, "z": {"mom_20": 1.0}}
                for ticker in ("A", "B", "C")]
        run = FakeRun({"series": series,
                       "factors": {"ok": True, "value": {"rows": rows}},
                       "ic": {"ok": True, "value": {"points": [{"t": "2026-09-08", "ic": 0.4}]}},
                       "plan": {"ok": True, "value": {"plans": []}},
                       "equity": {"ok": True, "value": {"current": 500000.0}}})
        self.write_watchlist(["A", "B"])
        app = self._app(run)

        risk = envelope_of(asyncio.run(app.routes[("GET", "/api/v3/risk/analytics")](
            limit=250, confidence=0.95, benchmark="SH.000300", weights=None)))
        self.assertTrue(risk["ok"], risk)
        self.assertEqual(risk["portfolioSource"], "自选池等权（2 只）")
        self.assertEqual(risk["nav"], 500000.0)

        matrix = envelope_of(asyncio.run(app.routes[("GET", "/api/v3/factors/matrix")](
            tickers="A,B,C", factor="mom_20", forward_days=None, forward=7)))
        self.assertTrue(matrix["ok"], matrix)
        self.assertEqual(run.payloads("ic")[0]["forward"], 7)

        strategy = envelope_of(asyncio.run(app.routes[("GET", "/api/v3/strategy")]()))
        self.assertEqual(strategy, {"ok": True, "run": None, "note": "尚未运行研究流水线"})

        sweep = envelope_of(asyncio.run(app.routes[("GET", "/api/v3/ml/sweep")](
            ticker="SH.600519", windows="5", rebalance="5", limit=500)))
        self.assertTrue(sweep["ok"], sweep)
        self.assertEqual(len(sweep["grid"]), 1)

    def test_post_handlers_return_200_envelopes(self):
        closes = closes_from([0.002 * math.sin(index / 4.0) + 0.001 for index in range(200)])
        run = FakeRun({
            "series": lambda payload: series_envelope(make_bars(closes),
                                                      ticker=payload["ticker"]),
            "factors": {"ok": True, "value": {"rows": [
                {"ticker": "A", "as_of": "2026-09-18", "factors": {"mom_20": 0.04},
                 "z": {"mom_20": 1.0}},
                {"ticker": "B", "as_of": "2026-09-18", "factors": {"mom_20": 0.02},
                 "z": {"mom_20": 0.5}}]}},
        })
        app = self._app(run)

        strategy = envelope_of(asyncio.run(app.routes[("POST", "/api/v3/strategy/run")](
            FakeRequest(json.dumps({"topN": 1, "universe": ["A", "B"]}).encode()))))
        self.assertTrue(strategy["ok"], strategy)
        self.assertEqual(strategy["run"]["stages"]["PCPT"]["longs"], ["A"])

        backtest = envelope_of(asyncio.run(app.routes[("POST", "/api/v3/ml/backtest")](
            FakeRequest(json.dumps({"ticker": "SH.600519", "window": 10,
                                    "rebalanceDays": 5}).encode()))))
        self.assertTrue(backtest["ok"], backtest)
        self.assertEqual(backtest["ticker"], "SH.600519")

    def test_post_body_edge_cases_never_500(self):
        run = FakeRun({"series": {"ok": True, "value": {"bars": make_bars([100, 101, 102])}}})
        app = self._app(run)

        empty = envelope_of(asyncio.run(app.routes[("POST", "/api/v3/strategy/run")](
            FakeRequest(b""))))
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["error"]["code"], "strategy/no-universe")

        garbage = envelope_of(asyncio.run(app.routes[("POST", "/api/v3/ml/backtest")](
            FakeRequest(b"{not json"))))
        self.assertFalse(garbage["ok"])
        self.assertEqual(garbage["error"]["code"], "bad-request")

        array_body = envelope_of(asyncio.run(app.routes[("POST", "/api/v3/ml/backtest")](
            FakeRequest(b"[1,2,3]"))))
        self.assertFalse(array_body["ok"])
        self.assertEqual(array_body["error"]["code"], "bad-request")

        no_ticker = envelope_of(asyncio.run(app.routes[("POST", "/api/v3/ml/backtest")](
            FakeRequest(b"{}"))))
        self.assertFalse(no_ticker["ok"])
        self.assertEqual(no_ticker["error"]["code"], "bad-request")

    def test_handler_never_raises_even_when_the_tool_explodes(self):
        def boom(payload):
            raise RuntimeError("boom")

        run = FakeRun({"series": boom, "plan": boom, "equity": boom, "factors": boom,
                       "ic": boom})
        self.write_watchlist(["A", "B"])
        app = self._app(run)
        get_kwargs = {
            "/api/v3/risk/analytics": {"limit": 250, "confidence": 0.95,
                                       "benchmark": "SH.000300", "weights": None},
            "/api/v3/factors/matrix": {"tickers": None, "factor": "mom_20",
                                       "forward_days": None, "forward": None},
            "/api/v3/strategy": {},
            "/api/v3/ml/sweep": {"ticker": "SH.600519", "windows": "5",
                                 "rebalance": "5", "limit": 500},
        }
        post_body = json.dumps({"ticker": "SH.600519", "universe": ["A", "B"]}).encode()
        for (method, path), function in app.routes.items():
            if method == "GET":
                response = asyncio.run(function(**get_kwargs[path]))
            else:
                response = asyncio.run(function(FakeRequest(post_body)))
            body = envelope_of(response)
            self.assertIsInstance(body, dict, path)
            self.assertIn("ok", body, path)

    def test_register_is_bound_to_its_own_dependencies(self):
        first_home = Path(self.home) / "one"
        second_home = Path(self.home) / "two"
        first_home.mkdir()
        second_home.mkdir()
        (first_home / "trading-platform.json").write_text(
            json.dumps({"watchlist": []}), encoding="utf-8")
        (second_home / "trading-platform.json").write_text(
            json.dumps({"watchlist": []}), encoding="utf-8")
        app_one = FakeApp()
        app_two = FakeApp()
        v3_analytics.register(app_one, FakeRun({}), str(first_home))
        v3_analytics.register(app_two, FakeRun({}), str(second_home))
        self.assertIsNot(app_one.routes, app_two.routes)


if __name__ == "__main__":
    unittest.main()
