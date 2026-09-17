"""WP14 阶段 B 修复（重要 1）：前向收益唯一实现——验证门与运行时同口径。

修复前的实测证据：
  * ``cli._forward_return``（验证门 ``_rule_panels`` 用）是近似口径
    （``as_of + horizon*2 日历日`` 近似窗口 + ``limit=horizon`` + ``len(bars)>=2`` 就返回）：
    60 根完整 bar、horizon=20 时与精确口径差 1.81pp；**其后仅 2 根时返回 0.11 却被当作
    20 日收益**喂进 t 检验；
  * ``rule_engine._forward_return``（``ic_weighted`` 运行时用）是精确口径。

两份实现让「验证门批准的统计」与「运行时 ic_weighted 用的统计」不可比——同一因子的
IC 在批准时与运行时按不同前向收益计算。本文件锁定统一后的口径：

  * 精确窗口：``as_of`` 当根收盘 → 其后第 ``horizon`` 根收盘；
  * 不足 ``horizon`` 根 / 无 ``as_of`` 当根 → ``None``（绝不返回短窗口收益冒充）；
  * ``end``：``None`` = 到该标的最新 bar（历史验证，前向收益已实现）；
    给定值 = 不越界（运行时防前视：评估日之后的数据不得参与）；
  * 两处调用点共用同一实现（结构断言 + 间谍断言 + 同日同标的值相等断言）。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_core import cli, factors, rule_engine, store  # noqa: E402

#: bar 标签（read_bars 只做字符串比较，不要求真实交易日历）
DAYS = [f"2026-01-{d:02d}" for d in range(1, 32)]
SYMBOL = "SH.600519"
GATE_FACTOR = "momentum_20"


def _seed(conn, symbol=SYMBOL, days=None, base=100.0, step=1.0):
    """写入递增收盘的日线；返回写入的 bar 列表（测试据此算期望值）。"""
    days = DAYS if days is None else days
    bars = [{"t": d, "o": base, "h": base, "l": base,
             "c": base + index * step, "v": 1000} for index, d in enumerate(days)]
    store.upsert_bars(conn, symbol, "1d", bars, "test")
    return bars


class ForwardReturnUnitTest(unittest.TestCase):
    """``factors.forward_return`` 的口径与边界。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        store.migrate(self.conn)
        self.addCleanup(self.conn.close)

    def test_exact_window_uses_as_of_close(self):
        bars = _seed(self.conn)
        got = factors.forward_return(self.conn, SYMBOL, DAYS[5], 20)
        self.assertAlmostEqual(got, bars[25]["c"] / bars[5]["c"] - 1.0, places=12,
                               msg="基准必须是 as_of 当根收盘，终点是其后第 horizon 根")

    def test_end_is_inclusive_cap(self):
        bars = _seed(self.conn)
        got = factors.forward_return(self.conn, SYMBOL, DAYS[5], 20, end=DAYS[25])
        self.assertAlmostEqual(got, bars[25]["c"] / bars[5]["c"] - 1.0, places=12)

    def test_end_cap_with_insufficient_forward_bars(self):
        _seed(self.conn)
        self.assertIsNone(factors.forward_return(self.conn, SYMBOL, DAYS[5], 20, end=DAYS[10]),
                          "上限内不足 horizon 根 → None（不外推、不缩短窗口）")

    def test_sparse_tail_returns_none_not_short_window(self):
        """修复前的缺陷复现：其后仅 2 根时不得把短窗口收益当 horizon 日收益。"""
        bars = _seed(self.conn, days=DAYS[:3])
        short_window = bars[1]["c"] / bars[0]["c"] - 1.0
        self.assertAlmostEqual(short_window, 0.01, places=12)  # 短窗口收益确实存在
        self.assertIsNone(factors.forward_return(self.conn, SYMBOL, DAYS[0], 20),
                          "不足 20 根 → None（修复前 cli 口径会返回该 1.01% 当作 20 日收益）")

    def test_end_none_is_latest_bar(self):
        _seed(self.conn)
        self.assertEqual(factors.forward_return(self.conn, SYMBOL, DAYS[5], 20),
                         factors.forward_return(self.conn, SYMBOL, DAYS[5], 20, end=DAYS[-1]))

    def test_horizon_boundary_exactly_at_last_bar(self):
        bars = _seed(self.conn, days=DAYS[:6])
        got = factors.forward_return(self.conn, SYMBOL, DAYS[0], 5)
        self.assertAlmostEqual(got, bars[5]["c"] / bars[0]["c"] - 1.0, places=12,
                               msg="恰好 horizon 根（最后一根即终点）应算得出")

    def test_missing_as_of_bar_or_empty_library(self):
        _seed(self.conn, days=DAYS[:10])
        self.assertIsNone(factors.forward_return(self.conn, SYMBOL, "2026-02-01", 5),
                          "as_of 不是 bar 日 → None")
        self.assertIsNone(factors.forward_return(self.conn, "SH.000001", DAYS[0], 5),
                          "库内无该标的 → None")

    def test_horizon_must_be_positive(self):
        _seed(self.conn)
        with self.assertRaises(ValueError):
            factors.forward_return(self.conn, SYMBOL, DAYS[0], 0)


class GateRuntimeParityTest(unittest.TestCase):
    """验证门（``cli._rule_panels``）与运行时（``rule_engine._ic_factor_weights``）同源。"""

    #: 运行时评估日（= end 上限）；其前设快照日，使 horizon 窗口落在 end 之内
    RUNTIME_END = DAYS[20]
    HORIZON = 5
    #: 横截面最少样本（``rule_engine.MIN_CROSS_SECTION``）要求多标的
    SYMBOLS = [SYMBOL, "SH.000858", "SZ.300750", "SH.601899"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        store.migrate(self.conn)
        self.addCleanup(self.conn.close)
        for index, symbol in enumerate(self.SYMBOLS):
            _seed(self.conn, symbol=symbol, base=100.0 + index * 10, step=1.0 + index)
        # 因子快照：payload 形状与 factors-snapshot 一致（tickers[symbol][factor]=值）
        self.snapshot_dates = [DAYS[10], DAYS[15]]
        for index, day in enumerate(self.snapshot_dates):
            store.save_factor_snapshot(self.conn, day, {
                "date": day,
                "tickers": {symbol: {GATE_FACTOR: 0.1 + index + position}
                            for position, symbol in enumerate(self.SYMBOLS)},
                "computed_at": f"{day} 16:15:00"})

    def _runtime_snapshots(self):
        return list(store.list_factor_snapshots(self.conn, limit=6))

    def _spy(self, calls, real):
        def spy(conn, symbol, as_of, horizon, end=None):
            result = real(conn, symbol, as_of, horizon, end)
            calls.append({"symbol": symbol, "as_of": as_of, "horizon": horizon,
                          "end": end, "result": result})
            return result
        return spy

    def test_both_paths_share_one_implementation_and_same_values(self):
        calls = []
        real = factors.forward_return
        with mock.patch.object(factors, "forward_return",
                               side_effect=self._spy(calls, real)):
            cli._rule_panels(self.conn, {"factors": [GATE_FACTOR]}, self.SYMBOLS,
                             self.RUNTIME_END, self.HORIZON, lookback=6, step=5,
                             registry={GATE_FACTOR: lambda conn, sym, day: 1.0})
            gate_calls = list(calls)
            calls.clear()
            rule_engine._ic_factor_weights(
                self.conn, [GATE_FACTOR], horizon=self.HORIZON, window=6, min_days=1,
                end=self.RUNTIME_END, snapshots=self._runtime_snapshots)
            runtime_calls = list(calls)

        self.assertTrue(gate_calls, "验证门必须经唯一实现取前向收益")
        self.assertTrue(runtime_calls, "运行时必须经唯一实现取前向收益")
        self.assertTrue(all(c["end"] is None for c in gate_calls),
                        "验证门走历史已验证窗口（end=None，不截断）")
        self.assertTrue(all(c["end"] == self.RUNTIME_END for c in runtime_calls),
                        "运行时必须传 end 上限（防前视）")

        gate = {(c["symbol"], c["as_of"]): c["result"] for c in gate_calls
                if c["result"] is not None}
        runtime = {(c["symbol"], c["as_of"]): c["result"] for c in runtime_calls
                   if c["result"] is not None}
        shared = set(gate) & set(runtime)
        self.assertTrue(shared, "至少应有一天两路径都算得出前向收益")
        for key in sorted(shared):
            self.assertAlmostEqual(gate[key], runtime[key], places=12,
                                   msg=f"同 end 窗口内前向收益必须逐值相等：{key}")

    def test_no_duplicate_private_implementations(self):
        """结构性护栏：两处私有实现已删除，防止再分叉。"""
        self.assertFalse(hasattr(cli, "_forward_return"))
        self.assertFalse(hasattr(rule_engine, "_forward_return"))


class IcCliForwardWindowTest(unittest.TestCase):
    """``ic`` 子命令的诚实语义：前向窗口不存在时 rank_ic 为 null（不编造）。"""

    SYMS = ["SH.600519", "SH.000858", "SZ.300750", "SH.601899"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "t.sqlite")
        conn = store.connect(self.db)
        try:
            store.migrate(conn)
            for index, symbol in enumerate(self.SYMS):
                _seed(conn, symbol=symbol, base=100.0 + index * 10, step=1.0 + index)
        finally:
            conn.close()

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def _ic(self, as_of):
        return self._run(["ic", "--factor", GATE_FACTOR, "--symbols", ",".join(self.SYMS),
                          "--as-of", as_of, "--horizon", "5", "--lookback", "4",
                          "--step", "5", "--db", self.db])

    def test_rank_ic_present_when_forward_window_exists(self):
        out = self._ic(DAYS[20])
        self.assertIsInstance(out["rank_ic"], float)
        self.assertEqual(out["samples"], len(self.SYMS))

    def test_rank_ic_null_at_latest_bar(self):
        out = self._ic(DAYS[-1])
        self.assertIsNone(out["rank_ic"],
                          "最新 bar 日没有前向窗口 → null（修复前用近似窗口编造了一个值）")
        self.assertEqual(out["samples"], len(self.SYMS), "样本数按因子值计，不受前向窗口影响")


if __name__ == "__main__":
    unittest.main()
