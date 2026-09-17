"""WP14 任务 3：验证门统计（t 检验 / 分层单调 / 半衰期 / 换手）+ ic CLI 扩展。

口径披露（诚实清单）：
- IC 序列的 t 检验用**正态近似**（``statistics.NormalDist``），不是小样本 t 分布；
  因此报告样本 ``n`` 且门槛要求 ``n >= IC_MIN_SAMPLES``（见 factors.passes_gate 的说明）；
- 半衰期由因子的 **lag-1 自相关**估计，单位是**采样间隔（调仓期数）**，不是自然日；
- 全部构造数据离线且确定性（固定 seed），不依赖网络。
"""
import io
import json
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import cli, factors, store  # noqa: E402


def _panel(n_dates=40, n_symbols=30, factor_noise=1.5, return_noise=3.0, seed=3,
           reverse=False):
    """构造有效性很好的因子面板：因子=符号序+抖动，前向收益=因子+噪声。

    抖动必须足以让**逐期 IC 有方差**——否则 IC 序列零方差、t 统计不可估计
    （实现如实返回 None，这是正确行为，测试数据要避免踩到）。
    """
    rng = random.Random(seed)
    factor_panel, fwd_panel = {}, {}
    sign = -1.0 if reverse else 1.0
    for d in range(n_dates):
        date = f"2026-01-{d + 1:02d}"
        values = {f"S{i:02d}": float(i) + rng.gauss(0, factor_noise)
                  for i in range(n_symbols)}
        factor_panel[date] = values
        fwd_panel[date] = {s: sign * v + rng.gauss(0, return_noise)
                           for s, v in values.items()}
    return factor_panel, fwd_panel


class IcReportTest(unittest.TestCase):
    def test_ic_tstat_and_layering(self):
        factor_panel, fwd_panel = _panel()
        report = factors.ic_report(factor_panel, fwd_panel)
        self.assertEqual(report["n"], 40)
        self.assertGreater(report["rank_ic_mean"], 0.8)
        self.assertGreater(report["t_stat"], 5)
        self.assertLess(report["p_value"], 0.01)
        self.assertEqual(len(report["layers"]), 5)
        self.assertEqual([row["q"] for row in report["layers"]], [1, 2, 3, 4, 5])
        self.assertLess(report["layers"][0]["ret"], report["layers"][4]["ret"])
        self.assertTrue(report["monotonic"])

    def test_random_factor_fails_gate(self):
        rng = random.Random(11)
        factor_panel, fwd_panel = {}, {}
        for d in range(40):
            date = f"2026-02-{d + 1:02d}"
            factor_panel[date] = {f"S{i:02d}": rng.gauss(0, 1) for i in range(30)}
            fwd_panel[date] = {f"S{i:02d}": rng.gauss(0, 1) for i in range(30)}
        report = factors.ic_report(factor_panel, fwd_panel)
        self.assertLess(abs(report["t_stat"]), 3)
        self.assertFalse(report["monotonic"])
        ok, reasons = factors.passes_gate(report)
        self.assertFalse(ok)
        self.assertTrue(any("单调" in r for r in reasons), reasons)

    def test_pass_gate_requires_samples(self):
        factor_panel, fwd_panel = _panel(n_dates=5)
        report = factors.ic_report(factor_panel, fwd_panel)
        self.assertEqual(report["n"], 5)
        ok, reasons = factors.passes_gate(report)
        self.assertFalse(ok)
        self.assertTrue(any("样本" in r for r in reasons), reasons)

    def test_pass_gate_rejects_negative_direction(self):
        factor_panel, fwd_panel = _panel(reverse=True)
        report = factors.ic_report(factor_panel, fwd_panel)
        self.assertLess(report["rank_ic_mean"], -0.8)
        self.assertLess(report["t_stat"], -5)
        ok, reasons = factors.passes_gate(report)
        self.assertFalse(ok)
        self.assertTrue(any("方向" in r or "t 检验" in r for r in reasons), reasons)

    def test_pass_gate_accepts_good_factor(self):
        report = factors.ic_report(*_panel())
        ok, reasons = factors.passes_gate(report)
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_sparse_dates_are_skipped(self):
        """单期有效样本 < 3 的日期不计入 IC 序列（rank_ic 既有语义）。"""
        factor_panel, fwd_panel = _panel(n_dates=25)
        factor_panel["2026-03-01"] = {"S00": 1.0, "S01": 2.0}
        fwd_panel["2026-03-01"] = {"S00": 0.1, "S01": 0.2}
        report = factors.ic_report(factor_panel, fwd_panel)
        self.assertEqual(report["n"], 25)


class HalfLifeTurnoverTest(unittest.TestCase):
    def test_half_life_ar05(self):
        rng = random.Random(7)
        series, prev = [], 0.0
        for _ in range(4000):
            prev = 0.5 * prev + rng.gauss(0, 1)
            series.append(prev)
        half_life = factors.factor_half_life(series)
        self.assertIsNotNone(half_life)
        # 估计量有抽样误差（se≈(1-ρ²)/√n）；单位＝采样间隔
        self.assertGreater(half_life, 0.9)
        self.assertLess(half_life, 1.1)

    def test_half_life_edges(self):
        self.assertIsNone(factors.factor_half_life([1.0, 2.0]))          # 样本不足
        self.assertIsNone(factors.factor_half_life([1.0] * 50))          # 零方差
        alternate = [1.0 if i % 2 == 0 else -1.0 for i in range(100)]
        self.assertEqual(factors.factor_half_life(alternate), 0.0)       # 无持续性
        self.assertIsNone(factors.factor_half_life([1.0, None, 2.0]))    # 剔除 None 后不足

    def test_turnover(self):
        self.assertAlmostEqual(
            factors.turnover([{"A", "B", "C"}, {"B", "C", "D"}]), 1 / 3, places=6)
        self.assertAlmostEqual(
            factors.turnover([{"A", "B"}, {"B", "A"}]), 0.0, places=6)
        self.assertAlmostEqual(
            factors.turnover([{"A"}, {"B"}, {"B"}]), 0.5, places=6)  # (1 + 0) / 2
        self.assertIsNone(factors.turnover([{"A", "B"}]))            # 期数不足
        self.assertIsNone(factors.turnover([]))


class IcCliTest(unittest.TestCase):
    DAYS = [f"2026-{m:02d}-{d:02d}" for m in range(1, 5) for d in range(1, 26)]
    SYMS = ["SH.600519", "SH.000858", "SZ.300750", "SH.601899", "SH.600036",
            "SZ.000333", "SH.601318", "SZ.002415"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.sqlite")
        conn = store.connect(self.db)
        try:
            for k, symbol in enumerate(self.SYMS):
                bars = [{"t": d, "o": 10.0 + k, "h": 11.0 + k, "l": 9.0 + k,
                         "c": 10.0 + k + ((self.DAYS.index(d) * 7 + k * 3) % 11 - 5) * 0.1,
                         "v": 10000} for d in self.DAYS]
                store.upsert_bars(conn, symbol, "1d", bars, "test")
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_cli_ic_output(self):
        out = self._run(["ic", "--factor", "momentum_20",
                         "--symbols", ",".join(self.SYMS),
                         "--as-of", self.DAYS[80], "--horizon", "5",
                         "--lookback", "12", "--step", "5", "--db", self.db])
        # 既有键不删（向后兼容）
        self.assertEqual(out["factor"], "momentum_20")
        self.assertIsInstance(out["rank_ic"], float)
        self.assertEqual(out["samples"], len(self.SYMS))
        # 新增键：验证门全集
        for key in ("rank_ic_mean", "t_stat", "p_value", "n", "layers", "monotonic",
                    "passes_gate", "gate_reasons"):
            self.assertIn(key, out)
        self.assertGreaterEqual(out["n"], 2)
        self.assertIsInstance(out["monotonic"], bool)
        self.assertIsInstance(out["layers"], list)
        self.assertIsInstance(out["passes_gate"], bool)


if __name__ == "__main__":
    unittest.main()
