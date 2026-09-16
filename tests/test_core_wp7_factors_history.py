"""WP7 任务 2：因子快照定时收集（store/CLI/daemon 接线）。全部离线。

* store：``factor_snapshots`` 表（v3 幂等追加，不改 SCHEMA_VERSION，对齐 WP4 alerts
  先例）、``save_factor_snapshot`` 同日期 upsert、``list_factor_snapshots`` 倒序 + limit；
* CLI：``factors-snapshot`` 复用 ``trading_core.factors`` 既有实现（测试替换
  ``factors.REGISTRY`` 注入替身，不另写计算），``factors-history`` 输出历史；
* daemon：``JOBS_DEFAULT`` 每个有作业的市场链末尾追加 ``factors_snapshot`` 作业
  （cmd 形式走既有 runner：@watchlist 替换 / 900s 超时 / 失败告警语义不变）。
"""
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import cli, daemon, factors, store  # noqa: E402


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)


class StoreFactorSnapshotTest(StoreTestBase):
    def test_schema_appends_table_without_version_bump(self):
        """WP4 alerts 先例：CREATE TABLE IF NOT EXISTS 幂等追加。WP7 当时保持 3；
        WP9 起 SCHEMA_VERSION=4（plans 补 origin/market，与本表的追加方式无关）。"""
        self.assertEqual(store.SCHEMA_VERSION, 4)
        names = {row["name"] for row in
                 self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("factor_snapshots", names)

    def test_save_and_list_roundtrip_deserializes_payload(self):
        store.save_factor_snapshot(
            self.conn, "2026-09-16",
            {"date": "2026-09-16", "tickers": {"SH.600519": {"momentum_20": 0.12}}},
            )
        rows = store.list_factor_snapshots(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-09-16")
        self.assertEqual(rows[0]["payload"]["tickers"]["SH.600519"], {"momentum_20": 0.12})
        self.assertTrue(rows[0]["created_at"], "created_at 应由 store 落 now")

    def test_upsert_same_date_overwrites(self):
        store.save_factor_snapshot(self.conn, "2026-09-16", {"v": 1})
        store.save_factor_snapshot(self.conn, "2026-09-16", {"v": 2})
        rows = store.list_factor_snapshots(self.conn)
        self.assertEqual(len(rows), 1, "同日期覆盖，不得出现第二行")
        self.assertEqual(rows[0]["payload"], {"v": 2})

    def test_list_orders_desc_and_respects_limit(self):
        for day in ("2026-09-14", "2026-09-16", "2026-09-15"):
            store.save_factor_snapshot(self.conn, day, {"day": day})
        rows = store.list_factor_snapshots(self.conn, limit=2)
        self.assertEqual([r["date"] for r in rows], ["2026-09-16", "2026-09-15"])
        self.assertEqual([r["date"] for r in store.list_factor_snapshots(self.conn)],
                         ["2026-09-16", "2026-09-15", "2026-09-14"])


class CliTestBase(StoreTestBase):
    def _cli(self, argv):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main([*argv, "--db", str(self.home / "t.sqlite")])
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())


class FactorsSnapshotCliTest(CliTestBase):
    def setUp(self):
        super().setUp()
        # 注入替身：因子计算唯一入口 factors.REGISTRY（fn(conn, symbol, as_of)），
        # 用完恢复——绝不另写一套计算，只换注册表这一处。
        self.calls = []
        self._original_registry = factors.REGISTRY

        def fake_mom(conn, symbol, as_of):
            self.calls.append((symbol, as_of))
            return 0.25 if symbol == "SH.600519" else None

        factors.REGISTRY = {"momentum_20": fake_mom}
        self.addCleanup(setattr, factors, "REGISTRY", self._original_registry)

    def test_snapshot_computes_via_registry_and_persists_payload(self):
        out = self._cli(["factors-snapshot", "--tickers", "SH.600519,HK.00700",
                         "--date", "2026-09-16"])
        self.assertEqual(out, {"ok": True, "date": "2026-09-16", "tickers": 2})
        self.assertEqual(self.calls, [("SH.600519", "2026-09-16"), ("HK.00700", "2026-09-16")])
        rows = store.list_factor_snapshots(self.conn)
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload"]
        self.assertEqual(payload["date"], "2026-09-16")
        self.assertEqual(payload["tickers"],
                         {"SH.600519": {"momentum_20": 0.25},
                          "HK.00700": {"momentum_20": None}})
        self.assertIn("momentum_20", payload["note"])
        self.assertTrue(payload["computed_at"])

    def test_snapshot_defaults_date_to_today(self):
        import datetime as dt
        today = dt.date.today().isoformat()
        out = self._cli(["factors-snapshot", "--tickers", "SH.600519"])
        self.assertEqual(out["date"], today)
        self.assertEqual(self.calls[0][1], today)

    def test_snapshot_failure_prints_ok_false_and_exits_nonzero_without_persisting(self):
        def broken(conn, symbol, as_of):
            raise RuntimeError("bars 缺失")

        factors.REGISTRY = {"momentum_20": broken}
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["factors-snapshot", "--tickers", "SH.600519",
                             "--date", "2026-09-16", "--db", str(self.home / "t.sqlite")])
        self.assertNotEqual(code, 0)
        body = json.loads(buffer.getvalue())
        self.assertFalse(body["ok"])
        self.assertIn("bars 缺失", body["error"])
        self.assertEqual(store.list_factor_snapshots(self.conn), [], "失败不得落库")

    def test_snapshot_without_any_factor_data_is_ok_false(self):
        factors.REGISTRY = {"momentum_20": lambda conn, symbol, as_of: None}
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["factors-snapshot", "--tickers", "SH.600519",
                             "--date", "2026-09-16", "--db", str(self.home / "t.sqlite")])
        self.assertNotEqual(code, 0)
        body = json.loads(buffer.getvalue())
        self.assertFalse(body["ok"])
        self.assertIn("无可用因子数据", body["error"])
        self.assertEqual(store.list_factor_snapshots(self.conn), [])


class FactorsHistoryCliTest(CliTestBase):
    def test_history_lists_snapshots_newest_first_with_limit(self):
        store.save_factor_snapshot(self.conn, "2026-09-15", {"day": "15"})
        store.save_factor_snapshot(self.conn, "2026-09-16", {"day": "16"})
        out = self._cli(["factors-history", "--limit", "1"])
        self.assertTrue(out["ok"])
        self.assertEqual([s["date"] for s in out["snapshots"]], ["2026-09-16"])
        self.assertEqual(out["snapshots"][0]["payload"], {"day": "16"})

    def test_history_of_empty_store_is_empty_list(self):
        out = self._cli(["factors-history"])
        self.assertEqual(out, {"ok": True, "snapshots": []})


class FactorsSnapshotJobWiringTest(unittest.TestCase):
    """调度接线：每个有作业的市场收盘链末尾追加 factors_snapshot（daemon 协议零改动）。"""

    def test_every_market_chain_ends_with_factors_snapshot_cmd_job(self):
        self.assertTrue(daemon.JOBS_DEFAULT)
        for market, chain in daemon.JOBS_DEFAULT.items():
            self.assertGreaterEqual(len(chain), 2, market)
            last = chain[-1]
            self.assertEqual(last["name"], "factors_snapshot", market)
            self.assertEqual(last["cmd"], ["factors-snapshot", "--tickers", "@watchlist"], market)
            # 收盘链末尾：触发时刻晚于链内前一个作业（前序数据作业先落库，快照吃到当日数据）
            self.assertGreater(last["at"], chain[-2]["at"], market)
            for job in chain[:-1]:
                self.assertNotEqual(job["name"], "factors_snapshot", market)

    def test_factors_snapshot_job_resolves_watchlist_via_existing_runner_protocol(self):
        """cmd 形式走既有协议：@watchlist 由 resolve_command 替换（关注池空返回 None 跳过）。"""
        import os
        import tempfile as tf
        with tf.TemporaryDirectory() as home:
            config = Path(home) / "trading-platform.json"
            config.write_text(json.dumps({"watchlist": ["SH.600519", "HK.00700"]}),
                              encoding="utf-8")
            old = os.environ.get("DSH_HOME")
            os.environ["DSH_HOME"] = home
            try:
                job = daemon.JOBS_DEFAULT["SH"][-1]
                self.assertEqual(daemon.resolve_command(job["cmd"], home),
                                 ["factors-snapshot", "--tickers", "SH.600519,HK.00700"])
                config.write_text("{}", encoding="utf-8")
                self.assertIsNone(daemon.resolve_command(job["cmd"], home),
                                  "关注池为空应返回 None（作业跳过）")
            finally:
                if old is None:
                    os.environ.pop("DSH_HOME", None)
                else:
                    os.environ["DSH_HOME"] = old


if __name__ == "__main__":
    unittest.main()
