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
