"""同步逐标的容错与退出码语义（E2E 缺陷 F-b），2026-09-17 实机取证。

原始证据：`sync-bars --tickers <20 只>` 因**单只**标的瞬时网络超时抛 `RuntimeError`
（未捕获）→ 裸 traceback、非零退出，且**其余标的完全不被处理**（无逐标的隔离）。

退出码语义（CLI help 与 `main` 注释同口径）：
  * 0 = 全部成功，**或**部分失败（失败明细在 summary.failed，并由 `_run_job` 发 warn 告警）；
  * 1 = 零成功（全部失败）或参数/致命错误（可读错误，不打裸 traceback）。
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _dir in (ROOT / "plugins" / "datasource" / "python",
             ROOT / "plugins" / "core" / "python"):
    sys.path.insert(0, str(_dir))

from trading_core import cli, store, sync  # noqa: E402


def _bars(count=3):
    return ([{"t": f"2026-09-{10 + i:02d}", "o": 1.0, "h": 1.0, "l": 1.0,
              "c": 1.0 + i, "v": 10.0} for i in range(count)], "futu/x", False)


class SyncBarsBatchTests(unittest.TestCase):
    """逐标的故障隔离（sync 层，直接注入 loader，零网络）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(store.db_path(self.tmp.name))
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def test_one_bad_symbol_does_not_abort_the_rest(self):
        def loader(ticker, period, needed):
            if ticker == "SH.600519":
                raise RuntimeError("取数失败：网络传输失败（TimeoutError）")
            return _bars()

        out = sync.sync_bars_batch(self.conn, ["SH.600000", "SH.600519", "SH.601398"],
                                   loader=loader)
        self.assertEqual(sorted(x["ticker"] for x in out["ok"]),
                         ["SH.600000", "SH.601398"], "其余标的必须继续处理")
        self.assertIn("SH.600519", out["failed"])
        self.assertIn("TimeoutError", out["failed"]["SH.600519"])
        self.assertEqual((out["ok_count"], out["failed_count"], out["total"]), (2, 1, 3))

    def test_all_bad_symbols_still_return_a_summary(self):
        def loader(ticker, period, needed):
            raise RuntimeError("全挂")

        out = sync.sync_bars_batch(self.conn, ["SH.600000", "SZ.000858"], loader=loader)
        self.assertEqual(out["ok_count"], 0)
        self.assertEqual(out["failed_count"], 2)
        self.assertEqual(out["total"], 2)

    def test_all_good_symbols_have_no_failures(self):
        out = sync.sync_bars_batch(self.conn, ["SH.600000", "SZ.000858"],
                                   loader=lambda t, p, n: _bars())
        self.assertEqual(out["failed"], {})
        self.assertEqual(out["ok_count"], 2)

    def test_backfill_summary_carries_counts(self):
        out = sync.backfill_bars(self.conn, ["SH.600000"], loader=lambda t, p, n: _bars(),
                                 sleep_seconds=0)
        self.assertEqual(out["ok_count"], 1)
        self.assertEqual(out["failed_count"], 0)


class CliExitCodeTests(unittest.TestCase):
    """退出码语义（patch 掉批处理实现，零网络）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "t.sqlite")

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_partial_failure_exits_zero_with_visible_failure(self):
        summary = {"ok": [{"ticker": "SH.600000"}], "failed": {"SH.600519": "超时"},
                   "ok_count": 19, "failed_count": 1, "total": 20}
        with mock.patch.object(sync, "sync_bars_batch", return_value=summary):
            code, out = self._run(["sync-bars", "--db", self.db, "--tickers", "SH.600000"])
        self.assertEqual(code, 0, "部分失败不当作整批失败")
        self.assertEqual(json.loads(out)["failed"], {"SH.600519": "超时"})

    def test_zero_success_exits_nonzero(self):
        summary = {"ok": [], "failed": {"SH.600519": "超时"}, "ok_count": 0,
                   "failed_count": 1, "total": 1}
        with mock.patch.object(sync, "sync_bars_batch", return_value=summary):
            code, out = self._run(["sync-bars", "--db", self.db, "--tickers", "SH.600519"])
        self.assertEqual(code, 1, "零成功必须非零退出（调度层才判定为作业失败）")
        self.assertEqual(json.loads(out)["failed_count"], 1)

    def test_transport_exception_is_readable_not_bare_traceback(self):
        with mock.patch.object(sync, "sync_bars_batch",
                               side_effect=RuntimeError("取数失败：网络传输失败（TimeoutError）")):
            code, out = self._run(["sync-bars", "--db", self.db, "--tickers", "SH.600519"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("TimeoutError", payload["error"], "错误必须可读且含原因")

    def test_empty_tickers_is_a_readable_error(self):
        code, out = self._run(["sync-bars", "--db", self.db, "--tickers", " , "])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_backfill_zero_success_exits_nonzero(self):
        summary = {"ok": [], "failed": {"SH.600519": "超时"}, "ok_count": 0,
                   "failed_count": 1, "done_total": 0, "pending": []}
        with mock.patch.object(sync, "backfill_bars", return_value=summary):
            code, _ = self._run(["backfill", "--db", self.db, "--tickers", "SH.600519"])
        self.assertEqual(code, 1)

    def test_backfill_partial_exits_zero(self):
        summary = {"ok": [{"ticker": "SH.600000"}], "failed": {"SH.600519": "超时"},
                   "ok_count": 1, "failed_count": 1, "done_total": 1, "pending": []}
        with mock.patch.object(sync, "backfill_bars", return_value=summary):
            code, _ = self._run(["backfill", "--db", self.db, "--tickers", "SH.600000"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
