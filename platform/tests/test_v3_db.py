"""``server/v3_db.py`` 的离线单测：schema / 迁移幂等 / KV / 并发 / 统计 / 备份。

运行::

    cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_db -v

**不打网络、不起服务、不碰 8397 的真实数据**：每个用例一份 ``tempfile`` home，
所有断言都对着临时目录里的 ``v3.db`` 与临时造的 JSONL/JSON 文件。覆盖策略：

  * 路径口径：显式 ``data_dir`` → ``$QUANT_V3_DATA`` → ``home``；``$QUANT_V3_DB`` 覆盖；
  * schema：6 张业务表 + ``kv`` + ``schema_version``；``init_db`` 幂等；
  * 无损：``append_event`` 写进去的 payload 原样读回来（``list_events`` 逐字段相等）；
  * 迁移：JSONL/OMS JSON → 表；**不删不改原文件**（字节比对）；重复调用不重复插入；
    迁移后又追加的新行只补尾巴（内容去重兜底）；
  * KV：``set_kv``/``get_kv`` + TTL 过期；
  * 并发：多线程 100 次追加一条不丢（WAL + busy_timeout + 短事务）；
  * 统计：``stats()`` 的 ``path/sizeBytes/tables/writes/reads/migrated``；
  * 备份：``VACUUM INTO`` 快照可用（快照自带的表行数与源一致）；
  * 失败面：库不可用时 ``list_events`` 抛 ``V3DbError``、``stats()`` 不抛只如实报 ``ok=false``。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_db  # noqa: E402

STRATEGY_RUNS = v3_db.STRATEGY_RUNS_FILE
OMS_ORDERS = v3_db.OMS_ORDERS_FILE
OMS_SYNC = v3_db.OMS_SYNC_FILE


class DbTestCase(unittest.TestCase):
    """临时 home + 环境变量清理（路径覆盖的两个变量必须还原，避免污染别的用例）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="v3-db-")
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._env = {}
        for name in (v3_db.DB_ENV, v3_db.DATA_DIR_ENV, v3_db.TIMEOUT_ENV):
            self._env[name] = os.environ.pop(name, None)
        self.addCleanup(self._restore_env)
        v3_db.reset_counters()

    def _restore_env(self):
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def write(self, name, content):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_jsonl(self, name, records):
        return self.write(name, "".join(
            json.dumps(record, ensure_ascii=False) + "\n" for record in records))


# ---------------------------------------------------------------------------
# 路径口径
# ---------------------------------------------------------------------------
class PathTests(DbTestCase):
    def test_default_path_is_home_v3_db(self):
        self.assertEqual(v3_db.db_path(self.home), self.home / "v3.db")
        self.assertEqual(v3_db.data_dir_path(self.home), self.home)

    def test_data_dir_argument_and_env(self):
        other = self.home / "data"
        self.assertEqual(v3_db.db_path(self.home, data_dir=other), other / "v3.db")
        os.environ[v3_db.DATA_DIR_ENV] = str(other)
        self.assertEqual(v3_db.db_path(self.home), other / "v3.db")
        self.assertEqual(v3_db.data_dir_path(self.home), other)
        # 显式参数优先于环境变量（与「既有 QUANT_V3_DATA 口径」一致：显式 > env > home）
        self.assertEqual(v3_db.db_path(self.home, data_dir=self.home / "explicit"),
                         self.home / "explicit" / "v3.db")

    def test_db_env_overrides_everything(self):
        os.environ[v3_db.DB_ENV] = str(self.home / "custom.db")
        self.assertEqual(v3_db.db_path(self.home), self.home / "custom.db")
        v3_db.init_db(self.home)
        self.assertTrue((self.home / "custom.db").is_file())
        self.assertFalse((self.home / "v3.db").exists())


# ---------------------------------------------------------------------------
# schema / init
# ---------------------------------------------------------------------------
class InitTests(DbTestCase):
    def test_init_creates_every_table_and_is_idempotent(self):
        state = v3_db.init_db(self.home)
        self.assertTrue(state["ok"], state)
        self.assertEqual(state["path"], str(self.home / "v3.db"))
        self.assertTrue(state["exists"])
        for table in v3_db.ALL_TABLES:
            self.assertIn(table, state["tables"], table)
        self.assertEqual(state["schemaVersion"], v3_db.SCHEMA_VERSION)
        self.assertGreater(state["sizeBytes"], 0)
        for table in v3_db.TABLES:
            self.assertEqual(state["tables"][table], 0, table)
        # init 会为 5 个迁移源各写一条 kv 迁移记录（行数 0 也算「看过」，便于 stats 上报）
        self.assertEqual(state["tables"]["kv"], len(v3_db.MIGRATIONS))

        again = v3_db.init_db(self.home)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["tables"]["schema_version"], 1,
                         "重复 init 不重复写 schema_version")
        self.assertEqual(again["schemaVersion"], v3_db.SCHEMA_VERSION)

    def test_row_factory_and_pragmas(self):
        conn = v3_db.connect(self.home)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertGreater(conn.execute("PRAGMA busy_timeout").fetchone()[0], 0)
        finally:
            conn.close()

    def test_init_creates_missing_home(self):
        nested = self.home / "deep" / "deeper"
        state = v3_db.init_db(nested)
        self.assertTrue(state["ok"], state)
        self.assertTrue((nested / "v3.db").is_file())


# ---------------------------------------------------------------------------
# append / list（无损读写）
# ---------------------------------------------------------------------------
class EventTests(DbTestCase):
    def test_append_and_read_back_is_lossless(self):
        payload = {
            "asOf": "2026-09-20T06:48:50.653Z",
            "universe": ["HK.00100", "HK.09988"],
            "market": "HK",
            "stages": {"PDAT": {"universe": ["HK.00100"], "bars": 600, "errors": []}},
            "proposals": [{"ticker": "HK.00100", "action": "增持", "targetWeightPct": 2.0}],
            "nested": {"unicode": "港股/美股", "numbers": [1, 2.5, None, True]},
        }
        rowid = v3_db.append_event(self.home, "strategy_runs", payload)
        self.assertIsInstance(rowid, int)
        records = v3_db.list_events(self.home, "strategy_runs", limit=None, order="asc")
        self.assertEqual(records, [payload], "payload 逐字段原样读回（无损）")
        self.assertEqual(v3_db.count_events(self.home, "strategy_runs"), 1)

    def test_order_limit_and_where(self):
        for index in range(5):
            v3_db.append_event(self.home, "strategy_runs",
                               {"asOf": f"t{index}", "market": "HK" if index % 2 else "SH"})
        desc = v3_db.list_events(self.home, "strategy_runs", limit=2)
        self.assertEqual([row["asOf"] for row in desc], ["t4", "t3"])
        asc = v3_db.list_events(self.home, "strategy_runs", limit=2, order="asc")
        self.assertEqual([row["asOf"] for row in asc], ["t0", "t1"])
        hk = v3_db.list_events(self.home, "strategy_runs", limit=None, order="asc",
                               where={"market": "HK"})
        self.assertEqual([row["asOf"] for row in hk], ["t1", "t3"])
        self.assertEqual([row["asOf"] for row in
                          v3_db.list_events(self.home, "strategy_runs", limit=None,
                                            where={"market": "HK"})],
                         ["t3", "t1"], "缺省倒序（与 OMS 台账视图的『最新在前』一致）")
        with self.assertRaises(v3_db.V3DbError):
            v3_db.list_events(self.home, "strategy_runs", where={"nope": 1})

    def test_common_columns_are_extracted_and_queryable(self):
        v3_db.append_event(self.home, "headless_log", {
            "started_at": "2026-09-20T00:00:00Z", "success": True, "exit_code": 0,
            "duration_ms": 1234, "tokens_estimate": 42, "task": "e2e"})
        conn = v3_db.connect(self.home)
        try:
            row = conn.execute("SELECT * FROM headless_log").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["success"], 1)
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["duration_ms"], 1234.0)
        self.assertEqual(row["tokens_estimate"], 42)
        rows = v3_db.list_events(self.home, "headless_log", where={"success": 1})
        self.assertEqual(rows[0]["task"], "e2e")

    def test_unknown_table_is_rejected(self):
        with self.assertRaises(v3_db.V3DbError):
            v3_db.append_event(self.home, "nope", {"a": 1})
        with self.assertRaises(v3_db.V3DbError):
            v3_db.list_events(self.home, "nope")

    def test_oms_orders_upsert_by_id(self):
        v3_db.append_event(self.home, "oms_orders", {"id": "A", "ticker": "SH.600000",
                                                     "stage": "manual", "qty": 100})
        v3_db.append_event(self.home, "oms_orders", {"id": "A", "ticker": "SH.600000",
                                                     "stage": "filled", "qty": 100})
        rows = v3_db.list_events(self.home, "oms_orders", limit=None)
        self.assertEqual(len(rows), 1, "同 id 是 upsert，不产生第二行")
        self.assertEqual(rows[0]["stage"], "filled")
        self.assertEqual(v3_db.count_events(self.home, "oms_orders"), 1)

    def test_replace_events_mirrors_the_given_set(self):
        v3_db.replace_events(self.home, "oms_orders", [
            {"id": "A", "ticker": "SH.600000", "stage": "manual"},
            {"id": "B", "ticker": "HK.00700", "stage": "risk_passed"}])
        self.assertEqual(sorted(row["id"] for row in
                                v3_db.list_events(self.home, "oms_orders", limit=None)),
                         ["A", "B"])
        result = v3_db.replace_events(self.home, "oms_orders", [
            {"id": "B", "ticker": "HK.00700", "stage": "filled"},
            {"id": "C", "ticker": "US.NVDA", "stage": "blocked"}])
        self.assertEqual(result, {"inserted": 1, "updated": 1, "deleted": 1}, result)
        rows = {row["id"]: row for row in v3_db.list_events(self.home, "oms_orders", limit=None)}
        self.assertEqual(sorted(rows), ["B", "C"], "A 已不在集合里 → 删除")
        self.assertEqual(rows["B"]["stage"], "filled")
        # 幂等：再替换一次同样的集合 → 零写入
        self.assertEqual(v3_db.replace_events(self.home, "oms_orders", [
            {"id": "B", "ticker": "HK.00700", "stage": "filled"},
            {"id": "C", "ticker": "US.NVDA", "stage": "blocked"}]),
            {"inserted": 0, "updated": 0, "deleted": 0})

    def test_sqlite_errors_never_escape_as_500(self):
        # ① 父目录不存在 → 读写都是明确错误（调用方据此回退文件），不是崩溃
        missing = self.home / "nope" / "deeper"
        with self.assertRaises(v3_db.V3DbError):
            v3_db.list_events(missing, "strategy_runs")
        with self.assertRaises(v3_db.V3DbError):
            v3_db.append_event(missing, "strategy_runs", {"asOf": "x"})
        state = v3_db.stats(missing)
        self.assertTrue(state["ok"], state)
        self.assertFalse(state["exists"])
        self.assertEqual(state["sizeBytes"], 0)
        self.assertFalse((missing / "v3.db").exists(), "stats 不该创建库文件")
        # ② 库文件损坏 → stats 不抛，如实报 ok=false + error（绝不 500）
        (self.home / "v3.db").write_bytes(b"this is not a sqlite database at all" * 40)
        broken = v3_db.stats(self.home)
        self.assertFalse(broken["ok"], broken)
        self.assertIn("error", broken)


# ---------------------------------------------------------------------------
# KV
# ---------------------------------------------------------------------------
class KvTests(DbTestCase):
    def test_set_get_round_trip_and_missing(self):
        v3_db.set_kv(self.home, "universe", "SH", {"tickers": ["SH.600000"], "n": 1})
        self.assertEqual(v3_db.get_kv(self.home, "universe", "SH"),
                         {"tickers": ["SH.600000"], "n": 1})
        self.assertIsNone(v3_db.get_kv(self.home, "universe", "HK"))
        v3_db.set_kv(self.home, "universe", "SH", ["replaced"])
        self.assertEqual(v3_db.get_kv(self.home, "universe", "SH"), ["replaced"])

    def test_ttl_expiry(self):
        v3_db.set_kv(self.home, "cache", "k", {"v": 1}, ttl_ms=-1)
        self.assertIsNone(v3_db.get_kv(self.home, "cache", "k"), "过期即 None")
        v3_db.set_kv(self.home, "cache", "k2", {"v": 2}, ttl_ms=60_000)
        self.assertEqual(v3_db.get_kv(self.home, "cache", "k2"), {"v": 2})


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------
class MigrationTests(DbTestCase):
    def seed(self):
        runs = [{"asOf": "2026-09-19T00:00:00.000Z", "universe": ["SH.600000"],
                 "stages": {"PDAT": {"bars": 960}}},
                {"asOf": "2026-09-20T00:00:00.000Z", "market": "HK",
                 "universe": ["HK.00700"], "universe_source": "config/x"}]
        self.write_jsonl(STRATEGY_RUNS, runs)
        # 坏行必须被跳过（迁移不能因为一行坏 JSON 就整文件放弃）
        with (self.home / STRATEGY_RUNS).open("a", encoding="utf-8") as handle:
            handle.write("not json\n")
        self.write(OMS_ORDERS, json.dumps({
            "version": 1, "updated_at": "2026-09-19T19:15:06.667218+00:00",
            "orders": {"A": {"id": "A", "ticker": "SH.600000", "stage": "manual",
                             "risk": {"action": "manual", "reasons": ["单笔占比 2.99% > 2%"]},
                             "history": [{"at": "t", "stage": "manual"}]},
                       "B": {"id": "B", "ticker": "HK.00700", "stage": "risk_passed"}}},
            ensure_ascii=False, indent=2) + "\n")
        self.write_jsonl(OMS_SYNC, [{"at": "2026-09-19T12:25:45.664860+00:00", "plans": 2,
                                     "orders": 10, "stages": {"manual": 10}}])
        return runs

    def test_migration_is_lossless_and_leaves_the_files_alone(self):
        runs = self.seed()
        before = {name: (self.home / name).read_bytes()
                  for name in (STRATEGY_RUNS, OMS_ORDERS, OMS_SYNC)}
        state = v3_db.init_db(self.home)
        self.assertTrue(state["ok"], state)
        self.assertEqual(state["tables"]["strategy_runs"], 2)
        self.assertEqual(state["tables"]["oms_orders"], 2)
        self.assertEqual(state["tables"]["oms_sync"], 1)
        self.assertEqual(v3_db.list_events(self.home, "strategy_runs", limit=None, order="asc"),
                         runs, "迁移后的记录与文件逐字段一致")
        orders = {row["id"]: row for row in
                  v3_db.list_events(self.home, "oms_orders", limit=None)}
        self.assertEqual(set(orders), {"A", "B"})
        self.assertEqual(orders["A"]["risk"]["reasons"], ["单笔占比 2.99% > 2%"])
        self.assertEqual(orders["A"]["history"], [{"at": "t", "stage": "manual"}])
        for name, raw in before.items():
            self.assertEqual((self.home / name).read_bytes(), raw, f"{name} 未被改写")
        self.assertIn(STRATEGY_RUNS, state["migrated"])
        self.assertEqual(state["migrated"][STRATEGY_RUNS]["rows"], 2)

    def test_migration_is_idempotent(self):
        self.seed()
        first = v3_db.migrate_from_files(self.home)
        self.assertEqual(first[STRATEGY_RUNS]["inserted"], 2)
        self.assertEqual(first[OMS_ORDERS]["inserted"], 2)
        second = v3_db.migrate_from_files(self.home)
        for name in (STRATEGY_RUNS, OMS_ORDERS, OMS_SYNC):
            self.assertEqual(second[name]["inserted"], 0, name)
            self.assertEqual(second[name]["updated"], 0, name)
            self.assertTrue(second[name]["skipped"], name)
        self.assertEqual(v3_db.count_events(self.home, "strategy_runs"), 2)
        self.assertEqual(v3_db.count_events(self.home, "oms_orders"), 2)
        self.assertEqual(v3_db.count_events(self.home, "oms_sync"), 1)
        # 三次也还是 2（重复调用不重复插入）
        v3_db.migrate_from_files(self.home)
        self.assertEqual(v3_db.count_events(self.home, "strategy_runs"), 2)

    def test_dual_written_tail_is_not_duplicated(self):
        """迁移后平台又经 append_event 追加、冷备文件同步增长 → 再迁移不产生重复行。"""
        self.seed()
        v3_db.init_db(self.home)
        extra = {"asOf": "2026-09-21T00:00:00.000Z", "market": "US"}
        v3_db.append_event(self.home, "strategy_runs", extra)
        with (self.home / STRATEGY_RUNS).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(extra, ensure_ascii=False) + "\n")
        again = v3_db.migrate_from_files(self.home)
        self.assertEqual(again[STRATEGY_RUNS]["inserted"], 0, again[STRATEGY_RUNS])
        self.assertEqual(v3_db.count_events(self.home, "strategy_runs"), 3)

    def test_rows_appended_to_the_file_only_are_recovered(self):
        """库写失败但文件写成功：下次迁移把尾巴补进库（内容去重，不重放已有的）。"""
        self.seed()
        v3_db.init_db(self.home)
        tail = {"asOf": "2026-09-22T00:00:00.000Z", "market": "SH"}
        with (self.home / STRATEGY_RUNS).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(tail, ensure_ascii=False) + "\n")
        result = v3_db.migrate_from_files(self.home)
        self.assertEqual(result[STRATEGY_RUNS]["inserted"], 1, result[STRATEGY_RUNS])
        self.assertEqual(v3_db.count_events(self.home, "strategy_runs"), 3)

    def test_oms_file_newer_than_the_database_wins(self):
        v3_db.init_db(self.home)
        self.write(OMS_ORDERS, json.dumps({"orders": {
            "A": {"id": "A", "ticker": "SH.600000", "stage": "manual"}}}))
        v3_db.migrate_from_files(self.home)
        self.assertEqual(v3_db.list_events(self.home, "oms_orders", limit=None)[0]["stage"],
                         "manual")
        self.write(OMS_ORDERS, json.dumps({"orders": {
            "A": {"id": "A", "ticker": "SH.600000", "stage": "filled"}}}))
        result = v3_db.migrate_from_files(self.home)
        self.assertEqual(result[OMS_ORDERS]["updated"], 1, result[OMS_ORDERS])
        self.assertEqual(v3_db.list_events(self.home, "oms_orders", limit=None)[0]["stage"],
                         "filled", "冷备里更新的台账要能覆盖回来（无损）")

    def test_missing_files_are_reported_not_fatal(self):
        state = v3_db.init_db(self.home)
        self.assertTrue(state["ok"], state)
        for name in v3_db.LEGACY_FILES:
            entry = state["migration"][name]
            self.assertTrue(entry["skipped"], name)
            self.assertEqual(entry["rows"], 0, name)
        self.assertEqual(state["tables"]["strategy_runs"], 0)

    def test_corrupt_oms_json_is_reported_not_fatal(self):
        self.write(OMS_ORDERS, "{oops")
        state = v3_db.init_db(self.home)
        self.assertTrue(state["ok"], state)
        self.assertEqual(state["migration"][OMS_ORDERS]["rows"], 0)
        self.assertIn("JSON 解析失败", state["migration"][OMS_ORDERS].get("note", ""))

    def test_migration_reads_from_data_dir(self):
        other = self.home / "platform-data"
        other.mkdir()
        (other / STRATEGY_RUNS).write_text(
            json.dumps({"asOf": "from-data-dir"}) + "\n", encoding="utf-8")
        os.environ[v3_db.DATA_DIR_ENV] = str(other)
        state = v3_db.init_db(self.home)
        self.assertTrue(state["ok"], state)
        self.assertEqual(state["path"], str(other / "v3.db"))
        self.assertEqual(state["tables"]["strategy_runs"], 1)
        self.assertEqual(v3_db.list_events(self.home, "strategy_runs", limit=None)[0]["asOf"],
                         "from-data-dir")


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------
class ConcurrencyTests(DbTestCase):
    def test_hundred_concurrent_appends_are_complete(self):
        v3_db.init_db(self.home)
        errors = []
        barrier = threading.Barrier(4)

        def worker(base):
            barrier.wait()
            for index in range(25):
                try:
                    v3_db.append_event(self.home, "strategy_runs",
                                       {"asOf": f"{base}-{index}", "market": "SH"})
                except Exception as error:  # noqa: BLE001 —— 记下来，最后统一断言
                    errors.append(f"{base}-{index}: {error}")

        threads = [threading.Thread(target=worker, args=(base,)) for base in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [], errors)
        records = v3_db.list_events(self.home, "strategy_runs", limit=None)
        self.assertEqual(len(records), 100, "并发追加 100 条不丢不重")
        self.assertEqual(len({row["asOf"] for row in records}), 100)

    def test_concurrent_migration_is_safe(self):
        self.write_jsonl(STRATEGY_RUNS, [{"asOf": f"r{index}"} for index in range(20)])
        errors = []

        def migrate():
            try:
                v3_db.migrate_from_files(self.home)
            except Exception as error:  # noqa: BLE001
                errors.append(repr(error))

        threads = [threading.Thread(target=migrate) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(v3_db.count_events(self.home, "strategy_runs"), 20,
                         "并发迁移不产生重复行")


# ---------------------------------------------------------------------------
# 统计 / 备份
# ---------------------------------------------------------------------------
class StatsAndBackupTests(DbTestCase):
    def test_stats_shape(self):
        v3_db.init_db(self.home)
        v3_db.append_event(self.home, "strategy_runs", {"asOf": "x"})
        v3_db.list_events(self.home, "strategy_runs")
        state = v3_db.stats(self.home)
        for key in ("path", "sizeBytes", "tables", "writes", "reads", "migrated", "ok"):
            self.assertIn(key, state, key)
        self.assertEqual(state["path"], str(self.home / "v3.db"))
        self.assertTrue(state["exists"])
        self.assertGreater(state["sizeBytes"], 0)
        self.assertGreaterEqual(state["writes"], 1)
        self.assertGreaterEqual(state["reads"], 1)
        self.assertEqual(state["tables"]["strategy_runs"], 1)
        self.assertIn("strategy_runs", state["tables"])

    def test_stats_without_a_database_reports_zeros_and_creates_nothing(self):
        state = v3_db.stats(self.home)
        self.assertFalse(state["exists"])
        self.assertEqual(state["sizeBytes"], 0)
        self.assertEqual(state["tables"]["strategy_runs"], 0)
        self.assertFalse((self.home / "v3.db").exists(), "看一眼 stats 不该创建库文件")

    def test_reset_counters(self):
        v3_db.init_db(self.home)
        v3_db.append_event(self.home, "strategy_runs", {"asOf": "x"})
        self.assertGreaterEqual(v3_db.counters()["writes"], 1)
        v3_db.reset_counters()
        self.assertEqual(v3_db.counters()["writes"], 0)

    def test_backup_snapshot_is_readable_and_complete(self):
        v3_db.init_db(self.home)
        for index in range(3):
            v3_db.append_event(self.home, "strategy_runs", {"asOf": f"r{index}"})
        dest = self.home / "snapshot.db"
        result = v3_db.backup_to(self.home, dest)
        self.assertTrue(result["ok"], result)
        self.assertTrue(dest.is_file())
        self.assertGreater(result["sizeBytes"], 0)
        import sqlite3

        snapshot = sqlite3.connect(str(dest))
        try:
            count = snapshot.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0]
        finally:
            snapshot.close()
        self.assertEqual(count, 3)
        self.assertFalse(v3_db.backup_to(self.home, dest)["ok"], "已存在的目标拒绝覆盖")


if __name__ == "__main__":
    unittest.main()
