"""V3 ML 策略族（FR-STRAT-002）的确定性单测：**不打网络、不起服务、不装依赖**。

运行::

    cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_ml -v

覆盖策略：
  * ``server/v3_ml.py`` 的特征工程用**构造序列**断言（列定义、标签口径、脏数据丢弃、
    **PIT 无未来函数**——把 ``t`` 之后的数据换成随机值，``t`` 日特征逐元素不变）；
  * 三个模型用**合成数据**断言真实能力（Lasso 在稀疏真值上选出非零系数；GBDT/MLP 在
    非线性数据上明显优于线性基线），并断言**可复现**（固定随机种子）；
  * 实现名（``impl``）如实：本环境没有 ``sklearn``/``lightgbm``，故猴子补丁「声称有」
    之后仍必须回落 numpy 且**不得谎称**用了官方库；
  * ``server/v3_analytics.ml_models`` 端点用**假 v3_run** 驱动，断言信封字段/错误码/
    只读纪律（只调 ``series``/``positions``，绝不触达任何写端点）。

为什么不用 TestClient：与 ``tests/test_v3_analytics.py`` 同一理由——真 app 需要完整数据层/
调度器/富途通道；``FakeRun``/``FakeApp`` 让每条分支（含失败分支）确定性复现。
"""

import asyncio
import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from server import v3_analytics, v3_ml

# 本部署实测：sklearn / lightgbm 均未安装（报告里如实记录，不作为静默前提）。
HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None
HAS_LIGHTGBM = importlib.util.find_spec("lightgbm") is not None


# ---------------------------------------------------------------------------
# 测试替身与构造工具
# ---------------------------------------------------------------------------
def make_bars(closes, start="2025-01-01", *, full=True):
    """收盘价序列 → 日 K 列表（``t`` 逐日递增）。``full=False`` 只留 ``t``/``c`` 两字段。"""
    base = dt.date.fromisoformat(start)
    bars = []
    for index, close in enumerate(closes):
        date = (base + dt.timedelta(days=index)).isoformat()
        if full:
            bars.append({"t": date, "o": close, "h": close, "l": close, "c": close, "v": 1.0})
        else:
            bars.append({"t": date, "c": close})
    return bars


def random_walk(n, *, seed=0, start=100.0, drift=0.0003, vol=0.02):
    """几何随机游走收盘价（测试用的确定性「真实感」序列）。"""
    rng = np.random.default_rng(seed)
    return list(start * np.exp(np.cumsum(rng.normal(drift, vol, n))))


def series_envelope(bars, ticker="X", source="futu/quote_history_kline"):
    return {"ok": True, "value": {"ticker": ticker, "period": "1d", "source": source,
                                  "as_of": bars[-1]["t"] if bars else None,
                                  "count": len(bars), "bars": list(bars)}}


class FakeRun:
    """``v3_run`` 替身（与 ``tests/test_v3_analytics.py`` 同形）：回放信封 + 记录调用。"""

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

    def names(self):
        return sorted({name for name, _ in self.calls})

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


def envelope_of(response):
    assert response.status_code == 200, response.status_code
    return json.loads(response.body)


class RouteCase(unittest.TestCase):
    """路由级用例共用：临时 home + 便捷断言。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def write_watchlists(self, mapping):
        path = Path(self.home) / "trading-platform.json"
        path.write_text(json.dumps({"watchlists": mapping}, ensure_ascii=False),
                        encoding="utf-8")

    def assertError(self, result, code):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], code, result["error"])
        self.assertTrue(result["error"]["message"], result["error"])


# ---------------------------------------------------------------------------
# 1) 特征工程：定义 / 口径 / PIT
# ---------------------------------------------------------------------------
class FeatureBuildTests(unittest.TestCase):
    def test_feature_names_are_stable_and_contain_the_momentum_column(self):
        self.assertEqual(len(v3_ml.FEATURE_NAMES), 10)
        self.assertIn(v3_ml.MOMENTUM_FEATURE, v3_ml.FEATURE_NAMES)
        self.assertEqual(v3_ml.MIN_SAMPLES, 120)
        built = v3_ml.build_features({"X": make_bars(random_walk(80))})
        self.assertEqual(built["feature_names"], list(v3_ml.FEATURE_NAMES))

    def test_shapes_lengths_and_sample_dates(self):
        bars = make_bars(random_walk(200))
        built = v3_ml.build_features({"X": bars}, window=20, horizon=1)
        n = built["X"].shape[0]
        self.assertEqual(built["X"].shape, (n, len(v3_ml.FEATURE_NAMES)))
        self.assertEqual(built["y"].shape, (n,))
        self.assertEqual(len(built["dates"]), n)
        self.assertEqual(len(built["tickers"]), n)
        self.assertTrue(np.all(np.isfinite(built["X"])),
                        "build_features 只返回特征齐全的行（不留 NaN）")
        # window=20 → 前 20 根只作回看，最后一根没有 t+1 标签：200-20-1 = 179
        self.assertEqual(n, 179)
        self.assertEqual(built["dates"][0], bars[20]["t"])

    def test_labels_are_forward_returns_not_contemporaneous(self):
        closes = [100.0 + index for index in range(120)]
        bars = make_bars(closes, full=False)
        built = v3_ml.build_features({"X": bars}, window=20, horizon=1)
        index_of = {bar["t"]: position for position, bar in enumerate(bars)}
        for row in (0, 5, 40, len(built["dates"]) - 1):
            date = built["dates"][row]
            position = index_of[date]
            expected = closes[position + 1] / closes[position] - 1.0
            self.assertAlmostEqual(float(built["y"][row]), expected, places=12)

    def test_horizon_two_uses_two_bars_ahead(self):
        closes = [100.0 + index for index in range(120)]
        bars = make_bars(closes, full=False)
        built = v3_ml.build_features({"X": bars}, window=20, horizon=2)
        index_of = {bar["t"]: position for position, bar in enumerate(bars)}
        date = built["dates"][0]
        position = index_of[date]
        self.assertAlmostEqual(float(built["y"][0]), closes[position + 2] / closes[position] - 1.0,
                               places=12)

    def test_samples_are_sorted_by_date_then_ticker(self):
        first = make_bars(random_walk(90, seed=1), start="2025-01-01")
        second = make_bars(random_walk(90, seed=2), start="2025-01-01")
        built = v3_ml.build_features({"ZZ": second, "AA": first}, window=20, horizon=1)
        keys = list(zip(built["dates"], built["tickers"]))
        self.assertEqual(keys, sorted(keys))
        # 同一日期两个标的时，标的按字典序
        same_day = [item for item in keys if item[0] == built["dates"][0]]
        self.assertEqual(same_day, sorted(same_day))

    def test_dirty_and_duplicate_bars_are_dropped_not_imputed(self):
        closes = random_walk(150)
        bars = make_bars(closes, full=False)
        last_date = bars[-1]["t"]
        bars[0] = {"t": bars[0]["t"], "c": None}          # 缺失收盘价
        bars[1]["c"] = -3.0                                # 非正收盘价
        bars[2] = {"t": bars[2]["t"], "c": "abc"}          # 非数值
        bars.append("not-a-bar")                           # 非字典
        bars.append({"t": last_date, "c": 12345.0})        # 同日期重复 → 保留最后一条
        built = v3_ml.build_features({"X": bars}, window=20, horizon=1)
        self.assertGreater(built["X"].shape[0], 100)
        self.assertEqual(built["tickers"], ["X"] * built["X"].shape[0])

    def test_constant_price_series_yields_no_samples(self):
        # 恒定价格 → range_pos 无定义（分母为 0）→ 整行丢弃：宁可没样本，也不填 0。
        built = v3_ml.build_features({"X": make_bars([100.0] * 200)}, window=20, horizon=1)
        self.assertEqual(built["X"].shape[0], 0)
        self.assertEqual(built["y"].size, 0)

    def test_window_is_clamped_and_horizon_is_validated(self):
        bars = make_bars(random_walk(120))
        tiny = v3_ml.build_features({"X": bars}, window=0, horizon=1)
        self.assertTrue(np.all(np.isfinite(tiny["X"])))
        with self.assertRaises(v3_ml.InsufficientSample):
            v3_ml.run_model_suite({"X": bars}, window=20, horizon=1)


class PITTests(unittest.TestCase):
    """**无未来函数**：``t`` 日特征只由 ``≤ t`` 的收盘价决定。"""

    CUT = 100

    def _mutated(self, closes, cut, seed=99):
        rng = np.random.default_rng(seed)
        out = list(closes)
        for index in range(cut + 1, len(out)):
            out[index] = float(rng.uniform(40.0, 250.0))
        return out

    def test_future_prices_do_not_change_past_features(self):
        closes = random_walk(180, seed=7)
        bars = make_bars(closes)
        base = v3_ml.build_features({"X": bars}, window=20, horizon=1)
        mutated = v3_ml.build_features({"X": make_bars(self._mutated(closes, self.CUT))},
                                       window=20, horizon=1)
        cut_date = bars[self.CUT]["t"]
        left = [index for index, date in enumerate(base["dates"]) if date <= cut_date]
        right = [index for index, date in enumerate(mutated["dates"]) if date <= cut_date]
        self.assertEqual(left, right, "样本行的日期序列必须一致")
        self.assertGreater(len(left), 50)
        np.testing.assert_array_equal(base["X"][left], mutated["X"][right])

    def test_the_test_above_is_not_vacuous(self):
        # 同一份「未来被替换」的数据里，**标签**必须变（标签本来就该用未来），
        # 否则上一个用例会变成「什么都没测到」。
        closes = random_walk(180, seed=7)
        bars = make_bars(closes)
        base = v3_ml.build_features({"X": bars}, window=20, horizon=1)
        mutated = v3_ml.build_features({"X": make_bars(self._mutated(closes, self.CUT))},
                                       window=20, horizon=1)
        cut_date = bars[self.CUT]["t"]
        row = base["dates"].index(cut_date)
        self.assertNotAlmostEqual(float(base["y"][row]), float(mutated["y"][row]), places=6)

    def test_future_shuffle_is_elementwise_invariant_across_tickers(self):
        closes_a = random_walk(160, seed=11)
        closes_b = random_walk(160, seed=12)
        bars_a = make_bars(closes_a)
        bars_b = make_bars(closes_b, start="2025-01-01")
        base = v3_ml.build_features({"A": bars_a, "B": bars_b}, window=20, horizon=1)
        cut_date = bars_a[self.CUT]["t"]
        mutated = v3_ml.build_features(
            {"A": make_bars(self._mutated(closes_a, self.CUT)),
             "B": make_bars(self._mutated(closes_b, self.CUT, seed=77))},
            window=20, horizon=1)
        left = [index for index, date in enumerate(base["dates"]) if date <= cut_date]
        right = [index for index, date in enumerate(mutated["dates"]) if date <= cut_date]
        self.assertEqual(left, right)
        np.testing.assert_array_equal(base["X"][left], mutated["X"][right])

    def test_labels_never_leak_into_features(self):
        # 直接对拍：第 i 行的特征只由 closes[:i+1] 决定——用前缀重建后逐元素比较。
        closes = random_walk(140, seed=21)
        bars = make_bars(closes, full=False)
        built = v3_ml.build_features({"X": bars}, window=20, horizon=1)
        prefix_bars = make_bars(closes[:self.CUT + 1], full=False)
        prefix = v3_ml.build_features({"X": prefix_bars}, window=20, horizon=1)
        cut_date = bars[self.CUT]["t"]
        rows = [index for index, date in enumerate(prefix["dates"]) if date <= cut_date]
        self.assertEqual(prefix["dates"][:len(rows)], built["dates"][:len(rows)])
        np.testing.assert_array_equal(prefix["X"][:len(rows)], built["X"][:len(rows)])


# ---------------------------------------------------------------------------
# 2) 模型：Lasso 稀疏性 / GBDT 与 MLP 的非线性能力
# ---------------------------------------------------------------------------
def sparse_problem(n=300, p=10, seed=11):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, p))
    y = 1.5 * X[:, 0] - 1.2 * X[:, 3] + rng.normal(0.0, 0.1, n)
    return X, y


def nonlinear_problem(n=700, seed=5):
    rng = np.random.default_rng(seed)
    X = rng.uniform(-2.0, 2.0, size=(n, 4))
    y = np.sin(3.0 * X[:, 0]) + 0.5 * X[:, 1] ** 2 + rng.normal(0.0, 0.05, n)
    return X, y


class LassoTests(unittest.TestCase):
    def test_selects_the_sparse_truth(self):
        X, y = sparse_problem()
        model = v3_ml.train_lasso(X, y, alpha=0.05, epochs=300)
        self.assertEqual(model["nonzero"], 2, model["coef"])
        active = {index for index, value in enumerate(model["coef"]) if abs(value) > 1e-8}
        self.assertEqual(active, {0, 3})
        self.assertEqual(model["impl"], "numpy-lasso")
        self.assertEqual(model["params"]["solver"], "coordinate-descent")
        self.assertEqual(model["params"]["lr"], 0.05,
                         "lr 在坐标下降下不参与计算，但必须如实记进 params")

    def test_smaller_alpha_keeps_more_coefficients(self):
        X, y = sparse_problem()
        loose = v3_ml.train_lasso(X, y, alpha=0.001, epochs=300)
        tight = v3_ml.train_lasso(X, y, alpha=0.2, epochs=300)
        self.assertGreaterEqual(loose["nonzero"], tight["nonzero"])

    def test_alpha_zero_matches_least_squares(self):
        X, y = sparse_problem()
        model = v3_ml.train_lasso(X, y, alpha=0.0, epochs=2000)
        # 我方 coef 在标准化空间：与 numpy 最小二乘的标准化解对拍
        mean, std = X.mean(axis=0), X.std(axis=0)
        xs = (X - mean) / std
        ys = (y - y.mean()) / y.std()
        expected = np.linalg.lstsq(xs, ys, rcond=None)[0]
        np.testing.assert_allclose(np.asarray(model["coef"]), expected, atol=1e-3)
        # 预测残差应落回数据的**不可约噪声**（构造时 σ=0.1 → Var≈0.01），而不是拟合不足
        predicted = v3_ml.predict(model, X)
        self.assertLess(float(np.mean((predicted - y) ** 2)), 0.02)
        self.assertGreater(float(np.corrcoef(predicted, y)[0, 1]), 0.99)

    def test_intercept_is_the_raw_units_intercept(self):
        X, y = sparse_problem()
        model = v3_ml.train_lasso(X, y, alpha=0.0, epochs=1000)
        # 契约里的 intercept 是可读的原始量纲截距：pred = intercept + X @ coef_raw
        coef_raw = np.asarray(model["coef"]) * model["_y_std"] / np.asarray(model["_x_std"])
        manual = X @ coef_raw + model["intercept"]
        np.testing.assert_allclose(v3_ml.predict(model, X), manual, atol=1e-9)

    def test_rejects_mismatched_shapes(self):
        with self.assertRaises(ValueError):
            v3_ml.train_lasso(np.zeros((5, 3)), np.zeros(4))
        with self.assertRaises(ValueError):
            v3_ml.train_gbdt(np.zeros((5, 3)), np.zeros(4))
        with self.assertRaises(ValueError):
            v3_ml.train_mlp(np.zeros((5, 3)), np.zeros(4))


class NonlinearModelTests(unittest.TestCase):
    """非线性数据上 GBDT/MLP 应**明显**优于线性基线（阈值取自实测，留足余量）。"""

    def test_gbdt_beats_linear_on_nonlinear_data(self):
        X, y = nonlinear_problem()
        linear = v3_ml.train_lasso(X, y, alpha=0.0, epochs=2000)
        model = v3_ml.train_gbdt(X, y, trees=60, depth=2, lr=0.1)
        mse_linear = v3_ml.evaluate(linear, X, y)["mse"]
        mse_tree = v3_ml.evaluate(model, X, y)["mse"]
        # 实测：linear≈0.84、gbdt-lite≈0.05（同一份数据、同一种子）
        self.assertLess(mse_tree, 0.5 * mse_linear,
                        f"gbdt={mse_tree:.4f} 未明显优于 linear={mse_linear:.4f}")
        self.assertEqual(model["impl"], "numpy-gbdt-lite")
        self.assertLess(mse_tree, float(np.var(y)))

    def test_mlp_beats_linear_on_nonlinear_data(self):
        X, y = nonlinear_problem()
        linear = v3_ml.train_lasso(X, y, alpha=0.0, epochs=2000)
        model = v3_ml.train_mlp(X, y, hidden=16, epochs=600, lr=0.02)
        mse_linear = v3_ml.evaluate(linear, X, y)["mse"]
        mse_mlp = v3_ml.evaluate(model, X, y)["mse"]
        # 实测：linear≈0.84、mlp≈0.02
        self.assertLess(mse_mlp, 0.5 * mse_linear,
                        f"mlp={mse_mlp:.4f} 未明显优于 linear={mse_linear:.4f}")
        self.assertEqual(model["impl"], "numpy-mlp")

    def test_gbdt_prefers_trees_over_a_single_stump(self):
        X, y = nonlinear_problem()
        one = v3_ml.train_gbdt(X, y, trees=1, depth=1)
        many = v3_ml.train_gbdt(X, y, trees=40, depth=2)
        self.assertLess(v3_ml.evaluate(many, X, y)["mse"], v3_ml.evaluate(one, X, y)["mse"])

    def test_gbdt_depth_is_clamped_to_the_spec_range(self):
        X, y = nonlinear_problem(n=200)
        shallow = v3_ml.train_gbdt(X, y, depth=1)
        capped = v3_ml.train_gbdt(X, y, depth=9)
        self.assertEqual(shallow["params"]["depth"], 1)
        self.assertEqual(capped["params"]["depth"], 3, "规格：树深 1..3")
        self.assertEqual(capped["params"]["trees"], 60)

    def test_models_are_reproducible(self):
        X, y = nonlinear_problem(n=300)
        first = v3_ml.evaluate(v3_ml.train_mlp(X, y, epochs=50, seed=7), X, y)
        second = v3_ml.evaluate(v3_ml.train_mlp(X, y, epochs=50, seed=7), X, y)
        self.assertEqual(first, second, "同一 seed 必须逐位可复现")
        tree_one = v3_ml.train_gbdt(X, y, trees=10)
        tree_two = v3_ml.train_gbdt(X, y, trees=10)
        self.assertEqual(json.dumps(tree_one["trees"]), json.dumps(tree_two["trees"]))

    def test_predictions_are_finite_for_every_model(self):
        X, y = nonlinear_problem(n=200)
        for model in (v3_ml.train_lasso(X, y), v3_ml.train_gbdt(X, y),
                      v3_ml.train_mlp(X, y, epochs=30)):
            predicted = v3_ml.predict(model, X)
            self.assertEqual(predicted.shape, (X.shape[0],))
            self.assertTrue(np.all(np.isfinite(predicted)), model["impl"])
        with self.assertRaises(ValueError):
            v3_ml.predict({"kind": "mystery"}, X)


# ---------------------------------------------------------------------------
# 3) 评估口径
# ---------------------------------------------------------------------------
class EvaluateTests(unittest.TestCase):
    def test_metrics_follow_the_documented_definitions(self):
        X, y = sparse_problem(n=200)
        model = v3_ml.train_lasso(X, y, alpha=0.0, epochs=2000)
        predicted = v3_ml.predict(model, X)
        result = v3_ml.evaluate(model, X, y)
        self.assertAlmostEqual(result["mse"], float(np.mean((predicted - y) ** 2)), places=12)
        self.assertAlmostEqual(result["ic"], float(np.corrcoef(predicted, y)[0, 1]), places=12)
        self.assertAlmostEqual(result["hit_rate"],
                               float(np.mean((predicted > 0) == (y > 0))), places=12)
        expected = float(np.mean(np.sign(predicted) * y)) * 252.0 * 100.0
        self.assertAlmostEqual(result["long_short_ann_pct"], expected, places=9)
        self.assertEqual(result["n"], 200)

    def test_cost_bps_charges_turnover_only(self):
        X, y = sparse_problem(n=200)
        model = v3_ml.train_lasso(X, y, alpha=0.0, epochs=1000)
        position = np.sign(v3_ml.predict(model, X))
        turnover = float(np.sum(np.abs(np.diff(position, prepend=0.0))))
        free = v3_ml.evaluate(model, X, y, cost_bps=0.0)["long_short_ann_pct"]
        charged = v3_ml.evaluate(model, X, y, cost_bps=10.0)["long_short_ann_pct"]
        expected_drop = turnover * (10.0 / 10000.0) / X.shape[0] * 252.0 * 100.0
        self.assertAlmostEqual(free - charged, expected_drop, places=6)
        self.assertLess(charged, free)

    def test_horizon_scales_the_annualization(self):
        X, y = sparse_problem(n=200)
        model = v3_ml.train_lasso(X, y, alpha=0.0, epochs=1000)
        daily = v3_ml.evaluate({**model, "horizon": 1}, X, y)["long_short_ann_pct"]
        biweekly = v3_ml.evaluate({**model, "horizon": 2}, X, y)["long_short_ann_pct"]
        self.assertAlmostEqual(daily, biweekly * 2.0, places=9)

    def test_empty_and_constant_inputs_do_not_produce_nan(self):
        empty = v3_ml.evaluate({"kind": "linear"}, np.zeros((0, 3)), np.zeros(0))
        self.assertEqual(empty, {"mse": None, "ic": None, "hit_rate": None,
                                 "long_short_ann_pct": None, "n": 0})
        X, _y = sparse_problem(n=60)
        flat = {**v3_ml.momentum_baseline(X, _y, feature_index=0), "coef": [0.0] * 10}
        result = v3_ml.evaluate(flat, X, _y)
        self.assertEqual(result["ic"], 0.0, "预测恒定 → IC 无定义，取 0 而不是 NaN")
        self.assertTrue(0.0 <= result["hit_rate"] <= 1.0)
        self.assertTrue(np.isfinite(result["mse"]))
        for key, value in result.items():
            self.assertFalse(isinstance(value, float) and not np.isfinite(value), key)


# ---------------------------------------------------------------------------
# 4) 动量基线 + 统一编排
# ---------------------------------------------------------------------------
def suite_bars(*, bars=400, seed=3, tickers=("SH.600519",)):
    return {ticker: make_bars(random_walk(bars, seed=seed + index))
            for index, ticker in enumerate(tickers)}


class MomentumBaselineTests(unittest.TestCase):
    def test_baseline_is_the_univariate_calibration_of_the_momentum_column(self):
        X = np.random.default_rng(1).normal(size=(200, len(v3_ml.FEATURE_NAMES)))
        rng = np.random.default_rng(2)
        index = v3_ml.FEATURE_NAMES.index(v3_ml.MOMENTUM_FEATURE)
        y = 0.01 * X[:, index] + rng.normal(0.0, 1e-4, 200)
        model = v3_ml.momentum_baseline(X, y, feature_index=index,
                                        feature_names=list(v3_ml.FEATURE_NAMES))
        self.assertEqual(model["impl"], "momentum-score")
        self.assertEqual(model["params"]["feature"], v3_ml.MOMENTUM_FEATURE)
        self.assertGreater(model["coef"][index], 0.9, "标准化空间里单变量 OLS 系数≈相关系数")
        self.assertEqual(sum(1 for value in model["coef"] if abs(value) > 1e-12), 1,
                         "基线只用动量那一列，其余系数恒为 0")
        self.assertGreater(v3_ml.evaluate(model, X, y)["ic"], 0.9)


class SuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 同一份输入的套件结果只算一次：run_model_suite 会真跑三个模型，别在用例间重复付出。
        cls.plain = v3_ml.run_model_suite(suite_bars(), window=20, horizon=1)
        cls.charged = v3_ml.run_model_suite(suite_bars(), cost_bps=20.0)

    def test_contract_keys_and_same_evaluation_frame(self):
        result = self.plain
        for key in ("as_of", "window", "horizon", "n_samples", "n_train", "n_test",
                    "feature_names", "models", "baseline", "notes", "backends", "split"):
            self.assertIn(key, result)
        self.assertEqual([item["name"] for item in result["models"]], ["lasso", "gbdt", "mlp"])
        self.assertEqual(result["baseline"]["name"], "momentum")
        self.assertEqual(result["n_samples"], result["n_train"] + result["n_test"])
        self.assertEqual(result["n_test"], result["models"][0]["metrics"]["n"])
        self.assertEqual(result["n_test"], result["baseline"]["metrics"]["n"])
        for model in result["models"] + [result["baseline"]]:
            for key in ("mse", "ic", "hit_rate", "long_short_ann_pct", "n"):
                self.assertIn(key, model["metrics"])
            self.assertEqual(model["metrics"]["n"], result["n_test"])
            self.assertGreater(model["metrics_in_sample"]["n"], model["metrics"]["n"])

    def test_impl_names_are_truthful_and_json_safe(self):
        result = self.plain
        impls = {item["name"]: item["impl"] for item in result["models"]}
        self.assertEqual(impls, {"lasso": "numpy-lasso", "gbdt": "numpy-gbdt-lite",
                                 "mlp": "numpy-mlp"})
        self.assertEqual(result["baseline"]["impl"], "momentum-score")
        self.assertEqual(result["backends"]["sklearn"], HAS_SKLEARN)
        self.assertEqual(result["backends"]["lightgbm"], HAS_LIGHTGBM)
        json.dumps(result, allow_nan=False, ensure_ascii=False)  # 必须无 NaN/Inf

    def test_split_is_chronological_and_notes_are_honest(self):
        result = self.plain
        self.assertEqual(result["n_train"], 265)
        self.assertEqual(result["n_test"], 114)
        self.assertIn("70/30", result["split"])
        notes = " ".join(result["notes"])
        for token in ("PIT", "无未来函数", "样本外", "numpy-gbdt-lite", "purging"):
            self.assertIn(token, notes)

    def test_multi_ticker_pooling_and_per_ticker_breakdown(self):
        result = v3_ml.run_model_suite(
            suite_bars(bars=260, tickers=("SH.600000", "SH.600009", "SH.600010")))
        self.assertEqual(result["tickers"], ["SH.600000", "SH.600009", "SH.600010"])
        baseline = result["baseline"]
        self.assertEqual(sorted(baseline["per_ticker"]), result["tickers"])
        self.assertEqual(sum(row["n"] for row in baseline["per_ticker"].values()),
                         result["n_test"])

    def test_insufficient_samples_raise_instead_of_running_anyway(self):
        with self.assertRaises(v3_ml.InsufficientSample) as caught:
            v3_ml.run_model_suite(suite_bars(bars=60), window=20, horizon=1)
        self.assertEqual(caught.exception.detail["required"], v3_ml.MIN_SAMPLES)
        self.assertLess(caught.exception.detail["n_samples"], v3_ml.MIN_SAMPLES)
        self.assertIn("样本不足时不硬跑", str(caught.exception))

    def test_cost_bps_is_applied_to_every_model_and_the_baseline(self):
        free, charged = self.plain, self.charged
        for index, model in enumerate(free["models"]):
            self.assertLess(charged["models"][index]["metrics"]["long_short_ann_pct"],
                            model["metrics"]["long_short_ann_pct"], model["name"])
        self.assertLess(charged["baseline"]["metrics"]["long_short_ann_pct"],
                        free["baseline"]["metrics"]["long_short_ann_pct"])


# ---------------------------------------------------------------------------
# 5) 可选依赖：探测 + 缺失时**如实**降级
# ---------------------------------------------------------------------------
class BackendDegradationTests(unittest.TestCase):
    def _patch(self, mapping):
        original = v3_ml.available_backends
        v3_ml.available_backends = lambda: dict(mapping)
        self.addCleanup(lambda: setattr(v3_ml, "available_backends", original))

    def test_reports_booleans_and_matches_the_real_environment(self):
        backends = v3_ml.available_backends()
        self.assertEqual(set(backends), {"sklearn", "lightgbm"})
        for value in backends.values():
            self.assertIsInstance(value, bool)
        self.assertEqual(backends["sklearn"], HAS_SKLEARN)
        self.assertEqual(backends["lightgbm"], HAS_LIGHTGBM)

    def test_missing_backends_use_the_numpy_implementations(self):
        self._patch({"sklearn": False, "lightgbm": False})
        X, y = nonlinear_problem(n=200)
        self.assertEqual(v3_ml.train_lasso(X, y)["impl"], "numpy-lasso")
        self.assertEqual(v3_ml.train_gbdt(X, y)["impl"], "numpy-gbdt-lite")
        self.assertEqual(v3_ml.train_mlp(X, y, epochs=20)["impl"], "numpy-mlp")

    def test_claimed_but_absent_backend_falls_back_without_lying(self):
        """探测说「有」但导入失败 → 必须回落 numpy，且**不得**把 impl 写成官方库。"""
        if HAS_SKLEARN or HAS_LIGHTGBM:
            self.skipTest("本环境真的装了可选库，本用例只验证「声称有但导入失败」的降级")
        self._patch({"sklearn": True, "lightgbm": True})
        X, y = nonlinear_problem(n=200)
        lasso = v3_ml.train_lasso(X, y)
        tree = v3_ml.train_gbdt(X, y, trees=5)
        net = v3_ml.train_mlp(X, y, epochs=20)
        self.assertEqual(lasso["impl"], "numpy-lasso")
        self.assertEqual(tree["impl"], "numpy-gbdt-lite")
        self.assertEqual(net["impl"], "numpy-mlp")


# ---------------------------------------------------------------------------
# 6) 端点：GET /api/v3/ml/models（只读）
# ---------------------------------------------------------------------------
WRITE_TOOLS = {"trade_place", "trade_modify", "trade_cancel", "plan_execute", "switch_mode"}
READ_ONLY_TOOLS = {"series", "positions"}

ROUTES = {
    ("GET", "/api/v3/risk/analytics"),
    ("GET", "/api/v3/factors/matrix"),
    ("GET", "/api/v3/factors/registry"),
    ("GET", "/api/v3/risk/funding-check"),
    ("GET", "/api/v3/strategy"),
    ("POST", "/api/v3/strategy/run"),
    ("GET", "/api/v3/ml/sweep"),
    ("POST", "/api/v3/ml/backtest"),
    ("GET", "/api/v3/ml/models"),
}


class MlModelsEndpointTests(RouteCase):
    def _run(self, closes=None, failing=(), source="futu/quote_history_kline"):
        closes = closes if closes is not None else random_walk(400, seed=4)

        def series(payload):
            ticker = payload["ticker"]
            if ticker in failing:
                return {"ok": False, "error": {"code": "futu/rate-limited",
                                               "message": "上游频控（测试构造）"}}
            return series_envelope(make_bars(closes), ticker=ticker, source=source)

        return FakeRun({"series": series,
                        "positions": {"ok": True, "value": {"groups": []}}})

    def _app(self, run):
        app = FakeApp()
        v3_analytics.register(app, run, self.home)
        return app

    def _get(self, run, **params):
        app = self._app(run)
        return envelope_of(asyncio.run(app.routes[("GET", "/api/v3/ml/models")](**params)))

    def test_route_registered_exactly_once_as_a_coroutine(self):
        app = self._app(self._run())
        self.assertEqual(set(app.routes), ROUTES)
        self.assertTrue(asyncio.iscoroutinefunction(app.routes[("GET", "/api/v3/ml/models")]))

    def test_single_ticker_returns_real_metrics_for_three_models_plus_baseline(self):
        run = self._run()
        body = self._get(run, market="SH", ticker="SH.600519", window=20, horizon=1,
                         limit=500, cost_bps=0.0)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["market"], "SH")
        self.assertEqual(body["source"], "futu/quote_history_kline")
        self.assertEqual(body["tickers"], ["SH.600519"])
        self.assertIn("显式 ticker", body["universe_source"])
        self.assertEqual(body["n_samples"], 379)
        self.assertEqual([item["name"] for item in body["models"]], ["lasso", "gbdt", "mlp"])
        self.assertEqual(body["baseline"]["name"], "momentum")
        for item in body["models"]:
            self.assertIn(item["impl"], ("numpy-lasso", "numpy-gbdt-lite", "numpy-mlp"))
            self.assertEqual(item["metrics"]["n"], body["n_test"])
            self.assertEqual(item["name"] == "lasso", "coef_top" in item)
        # 只读纪律：只碰 series（+ 市场宇宙解析可能碰 positions），绝不触达写端点
        self.assertLessEqual(set(run.names()), READ_ONLY_TOOLS)
        self.assertFalse(set(run.names()) & WRITE_TOOLS)
        self.assertEqual([payload["period"] for _, payload in run.calls
                          if _ == "series"], ["1d"])

    def test_ticker_defaults_to_the_market_universe(self):
        self.write_watchlists({"SH": ["SH.600000", "SH.600009"]})
        run = self._run()
        body = self._get(run, market="SH", ticker="", window=20, horizon=1)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["universe_source"], "config/trading-platform.json#watchlists.SH")
        self.assertEqual(body["tickers"], ["SH.600000", "SH.600009"])
        self.assertEqual(body["n_samples"], 758, "两只标的的样本被池化")

    def test_market_without_universe_reports_the_real_reason(self):
        body = self._get(self._run(), market="US", ticker="", window=20, horizon=1)
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["error"]["code"], "market/no-universe")
        self.assertEqual(body["error"]["market"], "US")
        self.assertIn("watchlists.US", body["error"]["detail"])

    def test_bad_market_is_rejected(self):
        body = self._get(self._run(), market="MARS", ticker="SH.600519")
        self.assertEqual(body["error"]["code"], "market/bad-market")

    def test_insufficient_samples_return_the_agreed_error_code(self):
        run = self._run(closes=random_walk(60, seed=8))
        body = self._get(run, market="SH", ticker="SH.600519", window=20, horizon=1)
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["error"]["code"], "ml/insufficient-sample")
        self.assertEqual(body["error"]["market"], "SH")
        self.assertEqual(body["error"]["detail"]["required"], v3_ml.MIN_SAMPLES)
        self.assertLess(body["error"]["detail"]["n_samples"], v3_ml.MIN_SAMPLES)
        self.assertEqual(body["error"]["detail"]["window"], 20)

    def test_upstream_failure_is_passed_through_untouched(self):
        run = self._run(failing=("SH.600519",))
        body = self._get(run, market="SH", ticker="SH.600519", window=20, horizon=1)
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["error"]["code"], "futu/rate-limited")
        self.assertIn("上游频控", body["error"]["message"])

    def test_partial_failure_does_not_break_the_other_tickers(self):
        self.write_watchlists({"SH": ["SH.600000", "SH.600009"]})
        run = self._run(failing=("SH.600009",))
        body = self._get(run, market="SH", ticker="", window=20, horizon=1)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["tickers"], ["SH.600000"])
        self.assertEqual(body["requested_tickers"], ["SH.600000", "SH.600009"])
        self.assertEqual(body["failures"]["SH.600009"]["code"], "futu/rate-limited")
        self.assertEqual(body["n_samples"], 379)

    def test_limit_is_clamped_to_the_series_contract(self):
        run = self._run()
        self._get(run, market="SH", ticker="SH.600519", limit=10)
        self.assertEqual(run.payloads("series")[0]["limit"], 60, "下限 60（series 契约）")
        run2 = self._run()
        self._get(run2, market="SH", ticker="SH.600519", limit=99999)
        self.assertEqual(run2.payloads("series")[0]["limit"], 2000, "上限 2000")

    def test_direct_handler_call_matches_the_route(self):
        run = self._run()
        direct = v3_analytics.ml_models(run, self.home, market="SH", ticker="SH.600519")
        self.assertTrue(direct["ok"], direct)
        self.assertEqual(sorted(direct["models"][0]["metrics"]),
                         ["hit_rate", "ic", "long_short_ann_pct", "mse", "n"])

    def test_internal_errors_become_envelopes_not_500(self):
        def series(payload):
            raise RuntimeError("boom")

        run = FakeRun({"series": series})
        body = self._get(run, market="SH", ticker="SH.600519")
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["error"]["code"], "v3/tool-failed")
        self.assertIn("boom", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
