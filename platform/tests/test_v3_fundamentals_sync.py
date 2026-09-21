"""``server/v3_fundamentals_sync`` 的离线单测：**不打网络**，fetcher / 注册表全部注入。

运行::

    cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_fundamentals_sync -v

覆盖策略（对应 2026-09-21 数据缺口任务的验收点）：

  * **upsert 幂等**：同键重跑行数不变（``(symbol, field, period_end)`` 冲突键）；
  * **announced_at PIT 写入与矩阵读取闭环**：写一条 ``announced_at`` 晚于 as_of 的
    行，断言 ``store.read_fundamentals``（与矩阵同一 WHERE：``announced_at IS NOT
    NULL AND announced_at<=as_of``）在 as_of 处**不可见**、披露日后**可见**；
  * **失败清单路径**：单标的通道异常 → 记入 ``failures``，其余标的照常落库；
  * **披露闸门**：注册表缺该期公告日 → 拒写（宁缺毋假）；NOTICE_DATE 越出法定
    披露窗口（yjbb UPDATE_DATE 陷阱）→ 拒写；
  * **修复通道** ``fix_announced_dates``：把 akshare/yjbb 的 UPDATE_DATE 污染值
    对齐到 NOTICE_DATE，且幂等；
  * **CLI 参数校验**：缺 ``--market``、坏 ``--periods`` 格式的可读失败；
  * ``--dry-run`` 不写库。

为什么不走真通道：矩阵取数纪律是「不打网络」，本模块的通道测试同理——
yfinance/东财的响应形状由 ``fetcher``/``registry_fetcher`` 替身精确复刻。
"""

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from server import v3_fundamentals_sync as sync_mod
from trading_core import store


# ---------------------------------------------------------------------------
# 测试替身：pandas 面（columns=报告期，index=科目）——与 yfinance 返回同形
# ---------------------------------------------------------------------------
def make_balance(**periods):
    import pandas as pd
    return pd.DataFrame.from_dict(periods, orient="index").T.infer_objects()


BALANCE = make_balance(**{
    "2025-06-30": {"Stockholders Equity": 8.0e10, "Total Assets": 1.0e12},
    "2026-06-30": {"Stockholders Equity": 8.4e10, "Total Assets": 1.04e13},
})
INCOME = make_balance(**{
    "2025-06-30": {"Net Income Common Stockholders": 1.2e9},
    "2026-06-30": {"Net Income Common Stockholders": 1.3e9},
})

REGISTRY_2025H1 = {"result": {"pages": 1, "data": [
    {"SECURITY_CODE": "600000", "NOTICE_DATE": "2025-08-28 00:00:00"},
    {"SECURITY_CODE": "600009", "NOTICE_DATE": "2025-08-26 00:00:00"}]}}
#: yjbb UPDATE_DATE 陷阱的复刻：把下一年同季的公告日当成 2025 中报披露日
REGISTRY_TRAP = {"result": {"pages": 1, "data": [
    {"SECURITY_CODE": "600000", "NOTICE_DATE": "2026-08-28 00:00:00"}]}}
REGISTRY_EMPTY = {"result": {"pages": 1, "data": []}}


def ok_fetcher(ticker):
    return BALANCE, INCOME


def failing_fetcher(ticker):
    raise RuntimeError("模拟上游异常（网络超时）")


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        sync_mod._REGISTRIES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "trading.sqlite"
        self.conn = store.connect(self.db)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)
        self.addCleanup(sync_mod._REGISTRIES.clear)

    def seed_polluted_row(self):
        """yjbb UPDATE_DATE 污染的存量行（2026-09-21 实测的形状）。"""
        store.upsert_fundamentals(self.conn, "SH.600000", [
            {"field": "revenue", "period_end": "2025-06-30", "value": 1.0,
             "announced_at": "2026-08-28", "announced_source": "akshare/yjbb"}],
            "futu/statements")


class FetchQuarterlyReturnsTests(SyncTestBase):
    def test_parses_periods_and_ratios(self):
        out = sync_mod.fetch_quarterly_returns("SH.600000", fetcher=ok_fetcher)
        self.assertEqual(out["symbol"], "600000.SS")
        self.assertEqual(out["source"], "yahoo/yfinance")
        self.assertEqual(sorted(out["periods"]), ["2025-06-30", "2026-06-30"])
        entry = out["periods"]["2025-06-30"]
        self.assertEqual(entry["roe"], round(1.2e9 / 8.0e10 * 100, 2))
        self.assertEqual(entry["roa"], round(1.2e9 / 1.0e12 * 100, 2))
        self.assertEqual(entry["equity"], 8.0e10)

    def test_non_positive_denominator_gives_none(self):
        balance = make_balance(**{"2025-06-30": {"Stockholders Equity": -1.0,
                                                 "Total Assets": 1.0e12}})
        out = sync_mod.fetch_quarterly_returns("SH.600000", fetcher=lambda t: (balance, INCOME))
        self.assertIsNone(out["periods"]["2025-06-30"]["roe"])

    def test_channel_failure_is_readable(self):
        with self.assertRaises(RuntimeError) as caught:
            sync_mod.fetch_quarterly_returns("SH.600000", fetcher=failing_fetcher)
        self.assertIn("模拟上游异常", str(caught.exception))


class SyncIdempotencyTests(SyncTestBase):
    def sync_once(self, **kwargs):
        return sync_mod.sync(self.conn, market="SH", tickers=["SH.600000"],
                             periods=["20250630", "20260630"],
                             registry_fetcher=lambda params: REGISTRY_2025H1,
                             fetcher=ok_fetcher, sleep_seconds=0, **kwargs)

    def test_upsert_idempotent_same_key_no_new_rows(self):
        first = self.sync_once()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["failures"], [])
        count_after_first = self.conn.execute(
            "SELECT COUNT(*) FROM fundamentals WHERE symbol='SH.600000'").fetchone()[0]
        second = self.sync_once()
        count_after_second = self.conn.execute(
            "SELECT COUNT(*) FROM fundamentals WHERE symbol='SH.600000'").fetchone()[0]
        self.assertEqual(count_after_first, count_after_second)
        self.assertEqual(first["rows_written"], second["rows_written"])
        # announced_at 不被重跑打回 / 改写
        row = self.conn.execute(
            "SELECT announced_at, announced_source, source FROM fundamentals"
            " WHERE symbol='SH.600000' AND field='roe' AND period_end='2025-06-30'").fetchone()
        self.assertEqual(row["announced_at"], "2025-08-28")
        self.assertEqual(row["announced_source"], sync_mod.SOURCE_NOTICE_DATE)
        self.assertEqual(row["source"], "yahoo/yfinance")

    def test_written_fields_carry_disclosure_time(self):
        result = self.sync_once()
        fields = {(row["field"], row["period_end"]) for row in self.conn.execute(
            "SELECT field, period_end FROM fundamentals WHERE symbol='SH.600000'")}
        self.assertIn(("roe", "2025-06-30"), fields)
        self.assertIn(("roa", "2025-06-30"), fields)
        self.assertIn(("equity", "2025-06-30"), fields)
        self.assertEqual(result["rows_written"], len(fields))


class PitVisibilityTests(SyncTestBase):
    """announced_at 写入 → 矩阵同口径读取（``read_fundamentals`` 的 WHERE）闭环。"""

    def test_row_invisible_before_announced_at_visible_after(self):
        result = sync_mod.sync(self.conn, market="SH", tickers=["SH.600000"],
                               periods=["20250630"],
                               registry_fetcher=lambda params: REGISTRY_2025H1,
                               fetcher=ok_fetcher, sleep_seconds=0)
        self.assertTrue(result["ok"], result)
        # as_of = 报告期末（披露前）→ 不可见；as_of = 披露日 → 可见（含当日，闭区间）
        before = store.read_fundamentals(self.conn, "SH.600000", "2025-06-30")
        on_day = store.read_fundamentals(self.conn, "SH.600000", "2025-08-28")
        after = store.read_fundamentals(self.conn, "SH.600000", "2025-12-31")
        self.assertEqual(before, [])
        self.assertIn("roe", {row["field"] for row in on_day})
        self.assertIn("roe", {row["field"] for row in after})
        self.assertTrue(all(row["announced_at"] == "2025-08-28"
                            for row in on_day if row["field"] == "roe"))


class DisclosureGateTests(SyncTestBase):
    def test_missing_notice_date_refused(self):
        result = sync_mod.sync(self.conn, market="SH", tickers=["SH.600000"],
                               periods=["20250630"],
                               registry_fetcher=lambda params: REGISTRY_EMPTY,
                               fetcher=ok_fetcher, sleep_seconds=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows_written"], 0)
        self.assertEqual(result["written"], [])
        self.assertTrue(any("宁缺毋假" in item["reason"] for item in result["skipped"]))
        count = self.conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
        self.assertEqual(count, 0)

    def test_update_date_trap_refused_by_statutory_window(self):
        result = sync_mod.sync(self.conn, market="SH", tickers=["SH.600000"],
                               periods=["20250630"],
                               registry_fetcher=lambda params: REGISTRY_TRAP,
                               fetcher=ok_fetcher, sleep_seconds=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows_written"], 0)
        self.assertTrue(any("法定披露窗口" in item["reason"] for item in result["skipped"]))

    def test_registry_failure_recorded_and_nothing_written(self):
        def broken_registry(params):
            raise ValueError(" boom")

        result = sync_mod.sync(self.conn, market="SH", tickers=["SH.600000"],
                               periods=["20250630"],
                               registry_fetcher=broken_registry,
                               fetcher=ok_fetcher, sleep_seconds=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows_written"], 0)
        self.assertTrue(any("披露注册表获取失败" in item["reason"]
                            for item in result["failures"]))


class FailureListTests(SyncTestBase):
    def test_one_bad_ticker_does_not_block_the_rest(self):
        def mixed_fetcher(ticker):
            if ticker == "SH.600000":
                return failing_fetcher(ticker)
            return ok_fetcher(ticker)

        result = sync_mod.sync(self.conn, market="SH",
                               tickers=["SH.600000", "SH.600009"],
                               periods=["20250630"],
                               registry_fetcher=lambda params: REGISTRY_2025H1,
                               fetcher=mixed_fetcher, sleep_seconds=0)
        self.assertTrue(result["ok"])
        self.assertEqual([f["ticker"] for f in result["failures"]], ["SH.600000"])
        self.assertIn("模拟上游异常", result["failures"][0]["reason"])
        self.assertEqual([w["ticker"] for w in result["written"]], ["SH.600009"])
        self.assertGreater(result["rows_written"], 0)

    def test_zero_success_with_failures_fails_cli(self):
        # patch 掉模块级取数入口 → CLI 全失败路径**不打网络**
        with unittest.mock.patch.object(sync_mod, "fetch_quarterly_returns",
                                        side_effect=RuntimeError("模拟上游异常")):
            exit_code = sync_mod.main(["--market", "SH", "--tickers", "SH.600000",
                                       "--periods", "20250630",
                                       "--db", str(self.db)])
        self.assertEqual(exit_code, 1)  # 零成功且有失败：调用方（作业链）必须看见


class DryRunTests(SyncTestBase):
    def test_dry_run_writes_nothing(self):
        result = sync_mod.sync(self.conn, market="SH", tickers=["SH.600000"],
                               periods=["20250630"],
                               registry_fetcher=lambda params: REGISTRY_2025H1,
                               fetcher=ok_fetcher, sleep_seconds=0, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertGreater(len(result["written"]), 0)
        count = self.conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
        self.assertEqual(count, 0)


class NonAShareTests(SyncTestBase):
    def test_hk_ticker_falls_back_to_run_date(self):
        hk_balance = make_balance(**{"2025-06-30": {"Stockholders Equity": 9.0e11,
                                                    "Total Assets": 1.2e12}})
        hk_income = make_balance(**{"2025-06-30": {"Net Income Common Stockholders": 6.0e10}})
        result = sync_mod.sync(self.conn, market="HK", tickers=["HK.00700"],
                               periods=["20250630"], fetcher=lambda t: (hk_balance, hk_income),
                               sleep_seconds=0, today="2026-09-21")
        self.assertTrue(result["ok"], result)
        row = self.conn.execute(
            "SELECT announced_at, announced_source FROM fundamentals"
            " WHERE symbol='HK.00700' AND field='roe'").fetchone()
        self.assertEqual(row["announced_at"], "2026-09-21")
        self.assertEqual(row["announced_source"], sync_mod.SOURCE_RUN_DATE)


class FixAnnouncedDatesTests(SyncTestBase):
    def test_corrects_update_date_pollution(self):
        self.seed_polluted_row()
        result = sync_mod.fix_announced_dates(
            self.conn, registry_fetcher=lambda params: REGISTRY_2025H1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["corrected"], 1)
        self.assertEqual(result["detail"][0]["from"], "2026-08-28")
        self.assertEqual(result["detail"][0]["to"], "2025-08-28")
        row = self.conn.execute(
            "SELECT announced_at, announced_source FROM fundamentals"
            " WHERE symbol='SH.600000' AND period_end='2025-06-30'").fetchone()
        self.assertEqual(row["announced_at"], "2025-08-28")
        self.assertEqual(row["announced_source"], sync_mod.SOURCE_NOTICE_DATE)

    def test_fix_is_idempotent(self):
        self.seed_polluted_row()
        sync_mod.fix_announced_dates(self.conn,
                                     registry_fetcher=lambda params: REGISTRY_2025H1)
        again = sync_mod.fix_announced_dates(
            self.conn, registry_fetcher=lambda params: REGISTRY_2025H1)
        self.assertEqual(again["corrected"], 0)
        self.assertEqual(again["unchanged"], 1)

    def test_trap_notice_date_is_refused_not_written(self):
        self.seed_polluted_row()
        result = sync_mod.fix_announced_dates(
            self.conn, registry_fetcher=lambda params: REGISTRY_TRAP)
        self.assertEqual(result["corrected"], 0)
        self.assertTrue(any("越出窗口" in item["reason"] for item in result["refused"]))
        row = self.conn.execute(
            "SELECT announced_at FROM fundamentals"
            " WHERE symbol='SH.600000' AND period_end='2025-06-30'").fetchone()
        self.assertEqual(row["announced_at"], "2026-08-28")  # 宁可保留也不写错值

    def test_registry_pagination_collects_all_codes_before_early_stop(self):
        # 回归（2026-09-21 实测缺陷）：逐代码取注册表会让首个代码把缓存截断在
        # 第一页（「集齐 wanted 即停」），其余代码全部误报 missing。
        # 两个污染行、注册表分两页：只有**带着全量 wanted** 一次取，才都修得到。
        store.upsert_fundamentals(self.conn, "SH.600009", [
            {"field": "revenue", "period_end": "2025-06-30", "value": 2.0,
             "announced_at": "2026-08-20", "announced_source": "akshare/yjbb"}],
            "futu/statements")
        self.seed_polluted_row()
        pages = {
            "1": {"result": {"pages": 2, "data": [
                {"SECURITY_CODE": "600000", "NOTICE_DATE": "2025-08-28 00:00:00"}]}},
            "2": {"result": {"pages": 2, "data": [
                {"SECURITY_CODE": "600009", "NOTICE_DATE": "2025-08-26 00:00:00"}]}},
        }
        calls = []

        def paged_registry(params):
            page = str(params.get("pageNumber", "1"))
            calls.append((params["filter"], page))
            return pages[page]

        result = sync_mod.fix_announced_dates(self.conn, registry_fetcher=paged_registry)
        self.assertEqual(result["corrected"], 2)
        self.assertEqual(result["missing"], [])
        # 该报告期只取了一轮注册表（不是每个代码一轮）
        self.assertEqual(len([page for _, page in calls if page == "1"]), 1)


class CliValidationTests(SyncTestBase):
    def test_market_is_required(self):
        with self.assertRaises(SystemExit) as caught:
            sync_mod.main(["--tickers", "SH.600000"])
        self.assertEqual(caught.exception.code, 2)

    def test_bad_period_format_is_readable_failure(self):
        exit_code = sync_mod.main(["--market", "SH", "--tickers", "SH.600000",
                                   "--periods", "2025-6-30", "--db", str(self.db)])
        self.assertEqual(exit_code, 1)

    def test_fix_mode_dry_run_reports_without_writing(self):
        self.seed_polluted_row()
        exit_code = sync_mod.main(["--market", "SH", "--fix-announced-at",
                                   "--dry-run", "--db", str(self.db)])
        self.assertEqual(exit_code, 0)
        row = self.conn.execute(
            "SELECT announced_at FROM fundamentals"
            " WHERE symbol='SH.600000' AND period_end='2025-06-30'").fetchone()
        self.assertEqual(row["announced_at"], "2026-08-28")  # dry-run 未动库


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
