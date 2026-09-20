"""FR-DATA-003：PIT 唯一读取入口（``server/data/cache.py``）的离线单测。

运行::

    cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_data_cache -v

覆盖策略（**不打网络、不起服务**：``fetch`` 路径用本地构造的回调，本地路径用临时交易库）：
  * ``as_of`` 语义：两个模式显式可选、边界（含/不含当日）逐日可断言、``as_of`` 必填；
  * **PIT 严格性**：跨期序列里 ``> as_of`` 的记录不得进入窗口（并给出被挡掉的计数证据）；
    基本面的 ``announced_at`` 与 ``period_end`` 是**两道独立闸门**，各自都能挡行；
  * **缓存键隔离**：不同 ``as_of`` / 不同标的 / 不同模式 / 不同交易库互不串味；
  * **幂等**：同一 ``as_of`` 连读两次逐字段相同（含 TTL 命中与过期重取）；
  * **缺失即 null + 原因**：绝不返回 0/空数组冒充读数；
  * **与 caches.py 的 TTL 交互**：TTL 内命中、过期后重取，且重取不会把新落库的「未来」
    数据带进窗口；
  * **鉴别力**：故意放宽闸门 / 故意把 ``as_of`` 从缓存键里删掉，测试必须变红——本模块
    用「断言反过来跑一遍」的方式证明这些断言真的有鉴别力（不是恒真断言）；
  * **迁移接线**：5 个原 PIT 点确实改走了本模块（用 spy 断言调用参数），而不是留了老路。

为什么用临时交易库而不是 mock：``bars``/``fundamentals``/``sentiment_snapshots`` 的列名、
排序、TEXT 比较语义都是被测对象的一部分（``ts<=as_of`` 是**字符串**比较），mock 掉数据库
就把这部分从测试里删掉了。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import caches
from server import v3_analytics, v3_math, v3_ml
from server.data import cache as pit


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------
def make_bars(closes, start_day=10, *, month="2026-09"):
    """收盘价序列 → 日 K（``t`` 逐日递增，从 ``month-start_day`` 起）。"""
    return [{"t": f"{month}-{start_day + index:02d}", "o": float(close), "h": float(close),
             "l": float(close), "c": float(close), "v": 1.0}
            for index, close in enumerate(closes)]


def make_fundamentals(rows):
    """``(field, period_end, announced_at, value)`` 四元组 → ``upsert_fundamentals`` 行。"""
    return [{"field": field, "period_end": period, "announced_at": announced,
             "value": value, "source": "test"}
            for field, period, announced, value in rows]


class PitCase(unittest.TestCase):
    """公共夹具：临时 home + **隔离的** TTL 缓存 + 可控时钟 + 临时交易库。

    ``caches.configure`` 会替换模块级缓存实例，因此本类在 tearDown 里**还原**——否则
    同进程的其它测试会连带用上本类的临时缓存目录/时钟。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._saved_cache = caches._CACHE
        self.addCleanup(self._restore_cache)
        self.clock = [1_700_000_000_000.0]  # 毫秒
        caches.configure(home=str(self.home), now=lambda: self.clock[0])
        self.store_path = self.home / "trading-data" / "trading.sqlite"

    def _restore_cache(self):
        caches._CACHE = self._saved_cache  # noqa: SLF001 —— 恢复被 configure 换掉的实例

    def build_store(self, *, bars=None, fundamentals=None, sentiments=None, symbol="X.TEST",
                    path=None):
        """建临时交易库并返回**同一个**连接（只发 SELECT，writes 已在建库时完成）。"""
        from trading_core import store as core_store
        target = Path(path) if path else self.store_path
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = core_store.connect(target)
        if bars:
            core_store.upsert_bars(conn, symbol, "1d", bars, "test")
        if fundamentals:
            core_store.upsert_fundamentals(conn, symbol, fundamentals, "test")
        for date, text in sentiments or []:
            conn.execute(
                "INSERT OR REPLACE INTO sentiment_snapshots(date,symbol,source,payload,"
                "fetched_at) VALUES(?,?,?,?,?)",
                (date, symbol, "fin_news", text, date))
        conn.commit()
        self.addCleanup(conn.close)
        return conn

    def add_bar(self, conn, bar, symbol="X.TEST"):
        """往库里追加一根 K 线（模拟「上游新数据落库」；用于 TTL 过期后的重取断言）。"""
        from trading_core import store as core_store
        core_store.upsert_bars(conn, symbol, "1d", [bar], "test")

    @staticmethod
    def has_future(envelope, as_of):
        """窗口内是否混进了 ``> as_of`` 的记录（PIT 的核心不变量）。"""
        return any(str(bar.get("t")) > as_of for bar in (envelope.get("bars") or []))

    @staticmethod
    def _strip(node):
        if isinstance(node, dict):
            return {key: PitCase._strip(value) for key, value in node.items()
                    if key not in ("cached", "cache_policy")}
        if isinstance(node, list):
            return [PitCase._strip(item) for item in node]
        return node

    @classmethod
    def core_of(cls, envelope):
        """去掉缓存元数据后的**读数本体**（幂等断言按它逐字段比；嵌套信封一并剥）。"""
        return cls._strip(envelope)

    @classmethod
    def reading_of(cls, envelope):
        """读数里**与 PIT 口径有关**的字段（不含 ``rejected`` 计数——新落库的未来行会改它）。"""
        core = cls._strip(envelope)
        core.pop("rejected", None)
        return core


# ---------------------------------------------------------------------------
# 1) as_of 语义：显式、可断言、必填
# ---------------------------------------------------------------------------
class AsOfSemanticsTests(PitCase):
    def test_as_of_is_mandatory_and_has_no_default(self):
        """缺省 as_of 必须**抛错**：悄悄用「今天」就是前视偏差最常见的入口。"""
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        for bad in (None, "", "   "):
            with self.assertRaises(pit.PitError):
                pit.read_bars(bad, pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)

    def test_as_of_must_be_a_iso_date(self):
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        with self.assertRaises(pit.PitError):
            pit.read_bars("2026/09/12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        with self.assertRaises(pit.PitError):
            pit.read_bars("2026-13-99", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)

    def test_mode_must_be_chosen_explicitly(self):
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        with self.assertRaises(pit.PitError):
            pit.read_bars("2026-09-12", None, symbol="X.TEST", conn=conn)
        with self.assertRaises(pit.PitError):
            pit.read_bars("2026-09-12", "inclusive-ish", symbol="X.TEST", conn=conn)

    def test_inclusive_time_boundary_includes_the_day_itself(self):
        """``inclusive`` = 含当日：``as_of`` 当日的记录在窗口里（且窗口末端就是它）。"""
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        self.assertEqual([bar["t"] for bar in result["bars"]],
                         ["2026-09-10", "2026-09-11", "2026-09-12"])
        self.assertEqual(result["window"], {"start": "2026-09-10", "end": "2026-09-12"})
        self.assertEqual(result["rows_used"], 3)
        self.assertIn("含 as_of 当日", result["semantics"])

    def test_exclusive_time_boundary_drops_the_day_itself(self):
        """``exclusive`` = 不含当日：同一序列少一根，窗口末端是 ``as_of`` 前一日。"""
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        result = pit.read_bars("2026-09-12", pit.AS_OF_EXCLUSIVE, symbol="X.TEST", conn=conn)
        self.assertEqual([bar["t"] for bar in result["bars"]],
                         ["2026-09-10", "2026-09-11"])
        self.assertEqual(result["window"], {"start": "2026-09-10", "end": "2026-09-11"})
        self.assertIn("不含 as_of 当日", result["semantics"])

    def test_visible_at_is_a_string_comparison_like_the_sql(self):
        """可见性判定与既有 SQL 的 TEXT 比较逐字一致（含带时间成分的公告日这一既有事实）。"""
        self.assertTrue(pit.visible_at("2026-09-12", "2026-09-12", pit.AS_OF_INCLUSIVE))
        self.assertFalse(pit.visible_at("2026-09-13", "2026-09-12", pit.AS_OF_INCLUSIVE))
        self.assertFalse(pit.visible_at("2026-09-12", "2026-09-12", pit.AS_OF_EXCLUSIVE))
        self.assertFalse(pit.visible_at(None, "2026-09-12", pit.AS_OF_INCLUSIVE))
        # 带时间成分 → 字符串比较下 "2026-09-20T05:00" > "2026-09-20"（迁移前既有行为，如实保留）
        self.assertFalse(pit.visible_at("2026-09-20T05:00:00Z", "2026-09-20",
                                        pit.AS_OF_INCLUSIVE))

    def test_pit_prefix_encodes_the_two_lags(self):
        values = [10, 11, 12, 13, 14]
        self.assertEqual(pit.pit_prefix(values, 3, lag=pit.LAG_SAME_DAY), [10, 11, 12, 13])
        self.assertEqual(pit.pit_prefix(values, 3, lag=pit.LAG_PREV_DAY), [10, 11, 12])
        self.assertEqual(pit.pit_prefix(values, 0, lag=pit.LAG_PREV_DAY), [])
        self.assertEqual(pit.pit_prefix(values, 0, lag=pit.LAG_SAME_DAY), [10])
        with self.assertRaises(pit.PitError):
            pit.pit_prefix(values, 3, lag=None)


# ---------------------------------------------------------------------------
# 2) PIT 严格性：未来数据不得进入
# ---------------------------------------------------------------------------
class PitStrictnessTests(PitCase):
    """构造跨 ``as_of`` 的序列：未来那两根的收盘价是**荒谬值**，一旦泄漏就看得见。"""

    FUTURE_CLOSE = 999.0

    def setUp(self):
        super().setUp()
        bars = make_bars([10, 11, 12]) + [
            {"t": "2026-09-13", "o": 1.0, "h": 1.0, "l": 1.0, "c": self.FUTURE_CLOSE, "v": 1.0},
            {"t": "2026-09-14", "o": 1.0, "h": 1.0, "l": 1.0, "c": self.FUTURE_CLOSE, "v": 1.0}]
        self.conn = self.build_store(bars=bars)

    def test_no_future_record_can_enter_the_window(self):
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn)
        self.assertFalse(self.has_future(result, "2026-09-12"), result["bars"])
        self.assertNotIn(self.FUTURE_CLOSE, result["closes"])
        self.assertEqual(result["window"]["end"], "2026-09-12")

    def test_rejected_future_rows_are_counted_as_evidence(self):
        """挡掉的未来行**要计数**（可核验），不是静默丢弃。"""
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn)
        self.assertEqual(result["rejected"]["future"], 2)
        self.assertEqual(result["rows_used"], 3)

    def test_derived_quantities_only_encode_visible_prices(self):
        """派生量（收益）只能由窗口内的收盘价算出：最后一格不可能含未来收益。"""
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn)
        expected = [None]
        for index in range(1, len(result["closes"])):
            expected.append(result["closes"][index] / result["closes"][index - 1] - 1.0)
        self.assertEqual(result["derived"]["simpleReturns"], expected)
        self.assertEqual(result["derived"]["latestClose"], 12.0)
        self.assertIsNone(result["derived"]["simpleReturns"][0], "第一格没有前值 → null 而不是 0")

    def test_limit_is_applied_after_the_pit_gate(self):
        """``limit`` 是「可见窗口里最近的 N 根」——顺序反了就会把窗口让给未来数据。"""
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn, limit=1)
        self.assertEqual([bar["t"] for bar in result["bars"]], ["2026-09-12"])
        self.assertEqual(result["rejected"]["future"], 2, "截断不改变闸门挡住的行数")

    def test_fundamentals_two_independent_gates(self):
        """``announced_at``（何时可见）与 ``period_end``（哪一期）是**两道独立闸门**。"""
        conn = self.build_store(fundamentals=make_fundamentals([
            ("revenue", "2026-06-30", "2026-08-28", 1000.0),   # 可见
            ("revenue", "2026-09-30", "2026-09-11", 8888.0),   # 公告日可见、报告期在 as_of 之后
            ("revenue", "2026-12-31", "2026-09-13", 9999.0),   # 公告日已在 as_of 之后
        ]))
        result = pit.read_fundamentals("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                       conn=conn)
        self.assertEqual([row["period_end"] for row in result["rows"]], ["2026-06-30"])
        self.assertEqual(result["rejected"]["future"], 1)
        self.assertEqual(result["rejected"]["period_ceiling"], 1)

    def test_fundamentals_without_announcement_date_are_invisible(self):
        """没有公告日的行**一律不可见**：无法证明当时已知（拿报告期当可得日就是前视）。"""
        conn = self.build_store(fundamentals=[
            {"field": "revenue", "period_end": "2026-06-30", "announced_at": None,
             "value": 1000.0, "source": "test"}])
        result = pit.read_fundamentals("2026-12-31", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                       conn=conn)
        self.assertIsNone(result["rows"])
        self.assertEqual(result["rejected"]["undated"], 1)
        self.assertIn("公告日", result["missing"]["reason"])

    def test_latest_period_rows_cannot_pick_a_future_period(self):
        rows = [
            {"field": "revenue", "period_end": "2026-06-30", "announced_at": "2026-08-28",
             "value": 1000.0, "source": "test"},
            {"field": "revenue", "period_end": "2026-12-31", "announced_at": "2026-09-11",
             "value": 9999.0, "source": "test"},
        ]
        fields, period, announced, source, stats = pit.latest_period_rows(
            rows, "2026-09-12", pit.AS_OF_INCLUSIVE)
        self.assertEqual(period, "2026-06-30")
        self.assertEqual(fields["revenue"]["value"], 1000.0)
        self.assertEqual(announced, "2026-08-28")
        self.assertEqual(source, "test")
        self.assertEqual(stats["future"], 1)
        # 站在 2026-12-31 看，才允许把 12-31 那期当「最新」
        fields, period, _announced, _source, _stats = pit.latest_period_rows(
            rows, "2026-12-31", pit.AS_OF_INCLUSIVE)
        self.assertEqual(period, "2026-12-31")
        self.assertEqual(fields["revenue"]["value"], 9999.0)

    def test_sentiment_snapshots_gate_on_the_snapshot_date(self):
        conn = self.build_store(sentiments=[("2026-09-11", '{"items": []}'),
                                            ("2026-09-13", '{"items": []}')])
        result = pit.read_sentiment_snapshots("2026-09-12", pit.AS_OF_INCLUSIVE,
                                              symbol="X.TEST", conn=conn)
        self.assertEqual([row["date"] for row in result["rows"]], ["2026-09-11"])
        self.assertEqual(result["rejected"]["future"], 1)

    def test_cross_section_frame_keeps_per_symbol_pit(self):
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        frame = pit.read_bars_frame("2026-09-11", pit.AS_OF_INCLUSIVE,
                                    symbols=["X.TEST", "Y.TEST"], conn=conn)
        self.assertEqual(frame["present"], ["X.TEST"])
        self.assertEqual(frame["rows_used"], 2)
        self.assertFalse(self.has_future(frame["series"]["X.TEST"], "2026-09-11"))
        self.assertEqual([item["symbol"] for item in frame["missing"]], ["Y.TEST"])
        self.assertIn("reason", frame["missing"][0])


# ---------------------------------------------------------------------------
# 3) 缓存键隔离
# ---------------------------------------------------------------------------
class CacheKeyIsolationTests(PitCase):
    def setUp(self):
        super().setUp()
        self.conn = self.build_store(bars=make_bars([10, 11, 12, 13, 14]))

    def test_different_as_of_do_not_share_a_cache_entry(self):
        first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                              conn=self.conn)
        second = pit.read_bars("2026-09-13", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn)
        self.assertFalse(first["cached"])
        self.assertFalse(second["cached"], "不同 as_of 必须各自成键（否则复用别人的窗口）")
        self.assertEqual(first["rows_used"], 3)
        self.assertEqual(second["rows_used"], 4)
        self.assertEqual(second["as_of"], "2026-09-13")

    def test_different_mode_do_not_share_a_cache_entry(self):
        inclusive = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                  conn=self.conn)
        exclusive = pit.read_bars("2026-09-12", pit.AS_OF_EXCLUSIVE, symbol="X.TEST",
                                  conn=self.conn)
        self.assertEqual(inclusive["rows_used"], 3)
        self.assertEqual(exclusive["rows_used"], 2)
        self.assertFalse(exclusive["cached"])

    def test_different_symbols_do_not_share_a_cache_entry(self):
        other = self.build_store(bars=[{"t": "2026-09-12", "o": 1.0, "h": 1.0, "l": 1.0,
                                        "c": 77.0, "v": 1.0}], symbol="Y.TEST")
        first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                              conn=self.conn)
        second = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="Y.TEST", conn=other)
        self.assertFalse(second["cached"], "不同标的不得共用缓存条目")
        self.assertEqual(first["derived"]["latestClose"], 12.0)
        self.assertEqual(second["derived"]["latestClose"], 77.0)

    def test_different_stores_do_not_share_a_cache_entry(self):
        """同一标的 + 同一 as_of，但在**另一个交易库**里：绝不允许读到上一个库的行。"""
        first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                              conn=self.conn)
        other_path = self.home / "other-home" / "trading-data" / "trading.sqlite"
        other_conn = self.build_store(
            bars=[{"t": "2026-09-12", "o": 1.0, "h": 1.0, "l": 1.0, "c": 55.0, "v": 1.0}],
            symbol="X.TEST", path=other_path)
        self.assertNotEqual(other_conn.execute("PRAGMA database_list").fetchone()[2],
                            self.conn.execute("PRAGMA database_list").fetchone()[2])
        second = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=other_conn)
        self.assertFalse(second["cached"])
        self.assertEqual(first["derived"]["latestClose"], 12.0)
        self.assertEqual(second["derived"]["latestClose"], 55.0)

    def test_in_memory_store_without_identity_is_not_cached(self):
        """内存库没有稳定数据源身份 → **不缓存**（宁可重取，也不冒串味的风险）。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE bars(symbol TEXT, period TEXT, ts TEXT, open REAL,"
                     " high REAL, low REAL, close REAL, volume REAL, source TEXT,"
                     " adj_factor REAL DEFAULT 1.0, PRIMARY KEY(symbol,period,ts))")
        conn.execute("INSERT INTO bars VALUES('X.TEST','1d','2026-09-12',1,1,1,12,1,'t',1.0)")
        conn.commit()
        first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        second = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        self.assertFalse(first["cached"])
        self.assertFalse(second["cached"])
        self.assertTrue(second["cache_policy"].startswith("none:"), second["cache_policy"])

    def test_fundamentals_and_sentiment_keys_also_carry_as_of(self):
        conn = self.build_store(
            fundamentals=make_fundamentals([("revenue", "2026-06-30", "2026-08-28", 1000.0)]),
            sentiments=[("2026-09-11", '{"items": []}')])
        visible = pit.read_fundamentals("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                        conn=conn)
        invisible = pit.read_fundamentals("2026-06-30", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                          conn=conn)
        self.assertEqual(visible["rows_used"], 1)
        self.assertFalse(invisible["cached"])
        self.assertEqual(invisible["rows_used"], 0)
        snap = pit.read_sentiment_snapshots("2026-09-12", pit.AS_OF_INCLUSIVE,
                                            symbol="X.TEST", conn=conn)
        older = pit.read_sentiment_snapshots("2026-09-01", pit.AS_OF_INCLUSIVE,
                                             symbol="X.TEST", conn=conn)
        self.assertEqual(snap["rows_used"], 1)
        self.assertFalse(older["cached"])
        self.assertIsNone(older["rows"])


# ---------------------------------------------------------------------------
# 4) 幂等
# ---------------------------------------------------------------------------
class IdempotenceTests(PitCase):
    def setUp(self):
        super().setUp()
        self.conn = self.build_store(bars=make_bars([10, 11, 12]))

    def test_same_as_of_twice_is_field_by_field_identical(self):
        first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                              conn=self.conn)
        second = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(self.core_of(first), self.core_of(second))

    def test_force_bypasses_the_cache_but_returns_the_same_reading(self):
        first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                              conn=self.conn)
        forced = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn, force=True)
        self.assertFalse(forced["cached"])
        self.assertEqual(self.core_of(first), self.core_of(forced))

    def test_frame_is_idempotent_too(self):
        first = pit.read_bars_frame("2026-09-12", pit.AS_OF_INCLUSIVE,
                                    symbols=["X.TEST"], conn=self.conn)
        second = pit.read_bars_frame("2026-09-12", pit.AS_OF_INCLUSIVE,
                                     symbols=["X.TEST"], conn=self.conn)
        self.assertEqual(self.core_of(first), self.core_of(second))


# ---------------------------------------------------------------------------
# 5) 缺失即 null + 原因
# ---------------------------------------------------------------------------
class MissingDataTests(PitCase):
    def test_empty_symbol_returns_null_with_reason(self):
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="Y.TEST", conn=conn)
        self.assertIsNone(result["bars"])
        self.assertIsNone(result["closes"])
        self.assertIsNone(result["derived"])
        self.assertIsNone(result["window"])
        self.assertEqual(result["rows_used"], 0)
        self.assertEqual(result["missing"]["code"], "pit/no-data")
        self.assertIn("Y.TEST", result["missing"]["reason"])
        self.assertIn("2026-09-12", result["missing"]["reason"])

    def test_missing_store_returns_null_with_reason(self):
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               home=str(self.home))
        self.assertIsNone(result["bars"])
        self.assertEqual(result["missing"]["code"], "pit/no-store")
        self.assertIn("trading.sqlite", result["missing"]["reason"])
        self.assertFalse(result["cached"])

    def test_all_future_series_is_visible_as_empty_not_zero(self):
        """只有未来行的标的：读数是「空 + 原因」，不是 0 根也不是伪造的收盘价。"""
        conn = self.build_store(bars=[{"t": "2026-09-30", "o": 1.0, "h": 1.0, "l": 1.0,
                                       "c": 888.0, "v": 1.0}])
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        self.assertIsNone(result["bars"])
        self.assertEqual(result["rejected"]["future"], 1)
        self.assertIn("挡掉 1 行", result["missing"]["reason"])

    def test_bars_without_timestamp_are_undated_not_silently_kept(self):
        """上游工具面里的无时间戳行：算 ``undated`` 并挡掉（本地库有 NOT NULL，走 fetch 构造）。"""
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               fetch=lambda symbol: ([{"t": None, "c": 5.0}], "test/fetch"))
        self.assertIsNone(result["bars"])
        self.assertEqual(result["rejected"]["undated"], 1)
        self.assertIn("时间戳", result["missing"]["reason"])

    def test_bad_closes_are_dropped_from_derived_series(self):
        """坏收盘价（None）不进派生序列——本地库有 NOT NULL，这种脏行只可能来自上游。"""
        bars = [
            {"t": "2026-09-10", "c": 10.0},
            {"t": "2026-09-11", "c": None},
            {"t": "2026-09-12", "c": 12.0}]
        result = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               fetch=lambda symbol: (bars, "test/fetch"))
        self.assertEqual(result["rows_used"], 3, "行数照实报（含坏行）")
        self.assertEqual(result["closes"], [10.0, 12.0], "派生序列不含 null（不插值、不清零）")


# ---------------------------------------------------------------------------
# 6) 与 caches.py 的 TTL 交互
# ---------------------------------------------------------------------------
class TtlInteractionTests(PitCase):
    def test_ttl_endpoints_are_registered_in_the_single_ttl_table(self):
        for endpoint in (pit.ENDPOINT_BARS, pit.ENDPOINT_FUNDAMENTALS, pit.ENDPOINT_SENTIMENT):
            self.assertGreater(caches.CACHE_TTL_MS.get(endpoint, 0), 0, endpoint)

    def test_hit_then_expiry_refetches_without_changing_the_pit_window(self):
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        hit = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        self.assertFalse(first["cached"])
        self.assertTrue(hit["cached"])
        # 上游又落了一根「未来」K 线（as_of 之后）
        self.add_bar(conn, {"t": "2026-09-13", "o": 1.0, "h": 1.0, "l": 1.0, "c": 999.0,
                            "v": 1.0})
        self.clock[0] += caches.CACHE_TTL_MS[pit.ENDPOINT_BARS] + 1
        after = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        self.assertFalse(after["cached"], "TTL 过期后必须重新取数")
        self.assertEqual(self.reading_of(after), self.reading_of(first),
                         "重取后 PIT 语义不变：窗口一模一样")
        self.assertEqual(after["rejected"]["future"], first["rejected"]["future"] + 1,
                         "新落库的那根被闸门计入 future，而不是进入窗口")
        self.assertFalse(self.has_future(after, "2026-09-12"))

    def test_ttl_expiry_does_not_merge_two_as_of_views(self):
        conn = self.build_store(bars=make_bars([10, 11, 12, 13]))
        first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        self.clock[0] += caches.CACHE_TTL_MS[pit.ENDPOINT_BARS] + 1
        second = pit.read_bars("2026-09-13", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        self.assertEqual(first["rows_used"], 3)
        self.assertEqual(second["rows_used"], 4)
        self.assertEqual(second["window"]["end"], "2026-09-13")

    def test_disk_layer_survives_a_memory_wipe(self):
        conn = self.build_store(bars=make_bars([10, 11, 12]))
        pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        caches.clear()  # 只清内存层（磁盘条目按设计保留）
        again = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
        self.assertTrue(again["cached"], "内存清空后应从磁盘层命中，且键仍含 as_of")


# ---------------------------------------------------------------------------
# 7) 有意不缓存的路径
# ---------------------------------------------------------------------------
class NoCachePathTests(PitCase):
    def test_fetch_path_applies_the_gate_but_never_writes_a_second_ttl_layer(self):
        """上游工具取的 K 线：只做 PIT 过滤，不叠第二套 TTL（理由见模块 docstring 三）。"""
        raw = make_bars([10, 11, 12]) + [
            {"t": "2026-09-13", "o": 1.0, "h": 1.0, "l": 1.0, "c": 999.0, "v": 1.0}]

        def fetch(symbol):
            return list(raw), "test/fetch"

        with mock.patch.object(caches, "write",
                               side_effect=AssertionError("fetch 路径不得写 TTL 缓存")):
            first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                  fetch=fetch)
            second = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                   fetch=fetch)
        self.assertEqual(first["rows_used"], 3)
        self.assertEqual(first["rejected"]["future"], 1)
        self.assertEqual(first["source"], "test/fetch")
        self.assertFalse(first["cached"])
        self.assertFalse(second["cached"])
        self.assertTrue(second["cache_policy"].startswith("none:"))

    def test_fetch_failure_is_raised_not_disguised_as_no_data(self):
        def broken(symbol):
            raise RuntimeError("series 取数失败：Invalid limit")

        with self.assertRaises(pit.PitSourceError) as caught:
            pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST", fetch=broken)
        self.assertIn("Invalid limit", str(caught.exception))

    def test_bad_fetch_return_type_is_a_source_error(self):
        with self.assertRaises(pit.PitSourceError):
            pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                          fetch=lambda symbol: "not-a-list")


# ---------------------------------------------------------------------------
# 8) 鉴别力：放宽闸门 / 去掉键里的 as_of 必须让断言变红
# ---------------------------------------------------------------------------
class DiscriminationTests(PitCase):
    """「测试能变红」本身要被证明：否则绿灯可能只是断言恒真。"""

    def setUp(self):
        super().setUp()
        self.conn = self.build_store(bars=make_bars([10, 11, 12]) + [
            {"t": "2026-09-13", "o": 1.0, "h": 1.0, "l": 1.0, "c": 999.0, "v": 1.0},
            {"t": "2026-09-14", "o": 1.0, "h": 1.0, "l": 1.0, "c": 999.0, "v": 1.0}])

    def test_loosening_the_as_of_gate_makes_the_no_future_assertion_fail(self):
        strict = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn)
        self.assertFalse(self.has_future(strict, "2026-09-12"))
        self.assertEqual(strict["rejected"]["future"], 2)

        with mock.patch.object(pit, "visible_at", lambda ts, as_of, mode: True):
            loose = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                  conn=self.conn, force=True)
        self.assertTrue(self.has_future(loose, "2026-09-12"),
                        "放宽闸门后未来行必须出现——否则「无未来」断言没有鉴别力")
        self.assertEqual(loose["rows_used"], 5)
        self.assertIn(999.0, loose["closes"])

    def test_flipping_the_boundary_comparison_makes_the_boundary_test_fail(self):
        """把紧边界 ``<=`` 写成 ``<``（或反之）：本文件的边界断言必须能察觉。"""
        strict = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                               conn=self.conn)
        with mock.patch.object(pit, "visible_at",
                               lambda ts, as_of, mode: str(ts) < as_of):
            flipped = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                    conn=self.conn, force=True)
        self.assertNotEqual(strict["rows_used"], flipped["rows_used"])
        self.assertEqual(strict["rows_used"], 3)
        self.assertEqual(flipped["rows_used"], 2)

    def test_dropping_as_of_from_the_cache_key_makes_two_views_collide(self):
        """把 ``as_of`` 从缓存键里删掉 → 两个不同的历史视图共用一次读数（前视）。"""
        strict_small = pit.read_bars("2026-09-11", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                     conn=self.conn)
        strict_big = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                   conn=self.conn)
        self.assertNotEqual(strict_small["rows_used"], strict_big["rows_used"])

        real_payload = pit._payload  # noqa: SLF001 —— 故意注入缺陷以证明断言有鉴别力

        def payload_without_as_of(*args, **kwargs):
            body = real_payload(*args, **kwargs)
            body.pop("as_of", None)
            body.pop("mode", None)
            return body

        with mock.patch.object(pit, "_payload", payload_without_as_of):
            first = pit.read_bars("2026-09-12", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                  conn=self.conn)
            collided = pit.read_bars("2026-09-11", pit.AS_OF_INCLUSIVE, symbol="X.TEST",
                                     conn=self.conn)
        self.assertFalse(first["cached"])
        self.assertTrue(collided["cached"], "键里没有 as_of 时第二次会命中第一次的条目")
        self.assertEqual(collided["as_of"], "2026-09-12", "串味：要的是 09-11，拿到的是 09-12")
        self.assertEqual(collided["rows_used"], 3)

    def test_ignoring_the_pit_lag_makes_the_backtest_look_ahead(self):
        """把 ``LAG_PREV_DAY`` 当成 ``LAG_SAME_DAY``：回测信号会用到**当日**收盘价 → 前视。"""
        bars = make_bars([10.0, 10.4, 10.1, 10.9, 11.2, 10.8, 11.5, 12.0, 11.6, 12.4,
                          12.1, 12.9, 13.3, 12.7, 13.6, 14.0, 13.4, 14.2, 14.8, 14.1])
        strict = v3_math.backtest_momentum(bars, window=3, rebalance_days=1)
        self.assertNotIn("error", strict)
        with mock.patch.object(pit, "pit_prefix",
                               lambda values, index, lag=pit.LAG_SAME_DAY: values[:index + 1]):
            loose = v3_math.backtest_momentum(bars, window=3, rebalance_days=1)
        self.assertNotEqual(strict["metrics"], loose["metrics"],
                            "忽略滞后档必须改变回测结果（否则 PIT 断言无鉴别力）")
        self.assertNotEqual(strict["positions"], loose["positions"])

    def test_backtest_is_unchanged_when_only_future_rows_are_appended(self):
        """真正的反证：往序列尾部追加未来数据，**已发生的**信号与持仓必须一模一样。"""
        base = make_bars([10.0, 10.4, 10.1, 10.9, 11.2, 10.8, 11.5, 12.0, 11.6, 12.4])
        extended = base + make_bars([99.0, 98.0, 97.0], start_day=20)
        first = v3_math.backtest_momentum(base, window=3, rebalance_days=1)
        second = v3_math.backtest_momentum(extended, window=3, rebalance_days=1)
        self.assertEqual(first["positions"], second["positions"][:len(first["positions"])],
                         "t 日持仓只由 ≤ t-1 决定：后面的数据改不了前面的持仓")


# ---------------------------------------------------------------------------
# 9) 迁移接线：5 个原 PIT 点确实走本模块
# ---------------------------------------------------------------------------
class MigrationWiringTests(PitCase):
    def setUp(self):
        super().setUp()
        self.conn = self.build_store(
            bars=make_bars([10, 11, 12]),
            fundamentals=make_fundamentals([("revenue", "2026-06-30", "2026-08-28", 1000.0)]),
            sentiments=[("2026-09-11", '{"ticker": "X.TEST", "items": ['
                                       '{"title": "公司业绩大幅增长，利好", "time": "2026-09-11"}]}')])
        self.db_path = str(self.store_path)

    def _spy(self, name, result=None):
        """把 ``pit.<name>`` 换成记录调用的替身（返回 ``result`` 或转调真实现）。"""
        calls = []
        real = getattr(pit, name)

        def spy(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return real(*args, **kwargs) if result is None else result

        patcher = mock.patch.object(pit, name, spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    @staticmethod
    def _mode_of(call):
        """取调用里的 ``mode``（位置参或关键字参都可能——两种都是显式选择）。"""
        if "mode" in call["kwargs"]:
            return call["kwargs"]["mode"]
        return call["args"][1] if len(call["args"]) > 1 else None

    def test_v3_analytics_fundamentals_read_goes_through_data_cache(self):
        calls = self._spy("read_fundamentals")
        rows = v3_analytics._read_pit_fundamentals(self.conn, "X.TEST", "2026-09-12")
        self.assertTrue(calls, "quality/growth 的 PIT 读取必须经 data.cache")
        self.assertEqual(self._mode_of(calls[0]), pit.AS_OF_INCLUSIVE)
        self.assertEqual(rows[0]["period_end"], "2026-06-30")

    def test_v3_analytics_latest_period_selection_goes_through_data_cache(self):
        calls = self._spy("latest_period_rows")
        fields, period, announced, source = v3_analytics._latest_period_rows(
            [{"field": "revenue", "period_end": "2026-06-30", "announced_at": "2026-08-28",
              "value": 1000.0, "source": "t"}], "X.TEST", "2026-09-12")
        self.assertTrue(calls)
        self.assertEqual(period, "2026-06-30")
        self.assertEqual(fields["revenue"], 1000.0)
        self.assertEqual(source, "t")

    def test_v3_analytics_sentiment_snapshot_read_goes_through_data_cache(self):
        calls = self._spy("read_sentiment_snapshots")
        score, meta = v3_analytics.sentiment_factor_values(str(self.home), "X.TEST",
                                                          "2026-09-11")
        self.assertTrue(calls, "情绪快照的 PIT 采集日闸门必须经 data.cache")
        self.assertEqual(self._mode_of(calls[0]), pit.AS_OF_INCLUSIVE)
        self.assertIn("store.sentiment_snapshots", meta["source"])

    def test_v3_metrics_use_the_single_pit_prefix(self):
        calls = self._spy("pit_prefix")
        v3_math.backtest_momentum(make_bars([10, 11, 12, 13, 14]), window=2,
                                  rebalance_days=1)
        self.assertTrue(calls)
        self.assertTrue(all(call["kwargs"].get("lag") == pit.LAG_PREV_DAY for call in calls),
                        "回测的 t 日决策只用 ≤ t-1")

    def test_v3_ml_features_use_the_single_pit_prefix(self):
        calls = self._spy("pit_prefix")
        v3_ml.build_features({"X.TEST": make_bars(list(range(10, 60)))}, window=5, horizon=1)
        self.assertTrue(calls)
        self.assertTrue(all(call["kwargs"].get("lag") == pit.LAG_SAME_DAY for call in calls),
                        "特征的第 i 行只用 ≤ i")

    def test_trading_local_close_goes_through_data_cache_without_ttl(self):
        from server import trading
        calls = self._spy("read_bars")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            close = trading._local_close(conn, "X.TEST", "2026-09-12")
        finally:
            conn.close()
        self.assertEqual(close, 12.0)
        self.assertTrue(calls, "风险基准价必须经 data.cache 的 PIT 闸门")
        self.assertIs(calls[0]["kwargs"].get("cache"), False,
                      "写路径的风险基准价取最新读数：显式 cache=False（理由见 data.cache 文档）")
        self.assertEqual(self._mode_of(calls[0]), pit.AS_OF_INCLUSIVE)

    def test_trading_local_close_never_sees_a_future_bar(self):
        from server import trading
        conn = self.build_store(bars=make_bars([10, 11, 12]) + [
            {"t": "2026-09-30", "o": 1.0, "h": 1.0, "l": 1.0, "c": 999.0, "v": 1.0}])
        try:
            close = trading._local_close(conn, "X.TEST", "2026-09-12")
        finally:
            conn.close()
        self.assertEqual(close, 12.0, "as_of 之后的 999 不得成为风险基准价")


# ---------------------------------------------------------------------------
# 10) 自检（可 `python -m server.data.cache` 直接跑）
# ---------------------------------------------------------------------------
class SelfcheckTests(PitCase):
    def test_selfcheck_reports_every_pit_invariant_green(self):
        report = pit.selfcheck()
        failed = [item["name"] for item in report["checks"] if not item["ok"]]
        self.assertEqual(failed, [], f"PIT 自检失败：{failed}")
        self.assertTrue(report["ok"])
        names = " ".join(item["name"] for item in report["checks"])
        for keyword in ("幂等", "PIT 严格", "键隔离", "null"):
            self.assertIn(keyword, names, "自检必须覆盖：" + keyword)
