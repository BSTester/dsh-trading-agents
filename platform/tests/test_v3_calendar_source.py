"""``server.v3_calendar_source`` 契约测试：**全离线**（假工具面 / 假 akshare / 固定时钟）。

覆盖点（每条都对应一个「如实」的承诺）:
  * 来源优先级：进程内 TTL 缓存 → 落盘缓存 → 富途 ``info_trading_days`` → AKShare
    ``tool_trade_date_hist_sina`` → ``<home>/market-holidays.json`` 人工兜底 → ``None``；
  * 富途命中：``source``/``chain``/``coverage``/``as_of``/``data_as_of`` 齐全，交易日升序去重；
  * 富途失败 → 降级 AKShare（A 股），并按**源的真实覆盖**算 ``coverage``/``complete``；
    港/美股如实标「无开源自历」；
  * 两源都失败 → 读人工兜底表（``source='file'``）；文件也没有 → ``None`` + 逐级原因；
  * ``holidays_for``：工作日 − 交易日，**周末不计入**；
  * TTL 命中不重复请求；TTL 过期（且落盘缓存也过期）后重新取数；
  * 落盘/读回一致：``<home>/market-calendar.json`` 结构（version/updated_at/markets）与内容；
  * ``live=False``（无 ``wb_call`` 且未注入 akshare）**绝不打网络**、也不 import akshare；
  * ``refresh_cache``：跳过缓存重取、只把**实时源**结果写盘、逐市场如实小结。

``setUp`` 把真实网络出口（socket/urllib/httpx）与 ``importlib.import_module("akshare")``
全部封死：任何漏注入的取数都会**显式失败**，而不是悄悄联网后「碰巧通过」。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_calendar_source -v``
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_calendar_source as src  # noqa: E402
from server import v3_fallback  # noqa: E402

# ── 固定样本（2026 国庆：10-01/02 + 10-05/06/07 是工作日休市）────────────────────

NATIONAL_DAY = (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
                date(2026, 10, 6), date(2026, 10, 7))
NATIONAL_DAY_ISO = [day.isoformat() for day in NATIONAL_DAY]
WINDOW = ("2026-09-28", "2026-10-12")
WINDOW_WORKDAYS = ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
                   "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
                   "2026-10-12")
TRADING_IN_WINDOW = [day for day in WINDOW_WORKDAYS if day not in NATIONAL_DAY_ISO]


def workdays(first, last, skip=()):
    blocked = set(skip)
    out = []
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5 and cursor not in blocked:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


SH_DAYS = workdays(date(2026, 8, 1), date(2027, 12, 31), NATIONAL_DAY)
HK_DAYS = workdays(date(2026, 8, 1), date(2027, 12, 31), (date(2026, 10, 1),))


class MutableClock:
    """可推进的假时钟（**秒**，与 ``time.monotonic`` 同单位）。"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeWbCall:
    """假工具面：按 ``market`` 返回 ``info_trading_days`` 信封（与真实上游同形）。"""

    def __init__(self, days_by_market=None, error=None, raises=None):
        self.days_by_market = {key: list(value) for key, value in (days_by_market or {}).items()}
        self.error = error
        self.raises = raises
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        if self.raises is not None:
            raise self.raises
        if self.error is not None:
            return {"ok": False, "error": {"code": "futu/rate-limited", "message": self.error}}
        payload = payload or {}
        days = self.days_by_market.get(payload.get("market"))
        if days is None:
            return {"ok": True, "value": {"trading_days": []}}
        first, last = str(payload.get("start") or ""), str(payload.get("end") or "")
        picked = [day for day in days if (not first or str(day) >= first)
                  and (not last or str(day) <= last)]
        return {"ok": True, "value": {"trading_days": [{"time": str(day)} for day in picked]}}

    @property
    def markets(self):
        return [payload.get("market") for _name, payload in self.calls]


class FakeAkshare:
    """假 akshare：只提供 ``tool_trade_date_hist_sina``（可注入数据/异常/缺失）。"""

    def __init__(self, days=None, error=None, missing=False):
        self.days = list(days or [])
        self.error = error
        self.calls = 0
        if not missing:
            self.tool_trade_date_hist_sina = self._sina

    def _sina(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [{"trade_date": day} for day in self.days]


def fast_retry(**overrides):
    """不真等的重试参数（把 sleep 换成记录器，attempts 默认 2）。"""
    options = {"attempts": 2, "base_ms": 10, "sleep": lambda _seconds: None,
               "rand": lambda: 0.0}
    options.update(overrides)
    return options


class OfflineBase(unittest.TestCase):
    def setUp(self):
        # 别让宿主机环境变量改变判定（空串 = 未设置，见 load_holidays/_ttl_ms）。
        env_patcher = mock.patch.dict(os.environ, {src.HOLIDAYS_ENV: "", src.TTL_ENV: ""})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        patchers = [
            mock.patch("socket.socket.connect", side_effect=AssertionError("测试禁止真实网络：connect")),
            mock.patch("socket.create_connection", side_effect=AssertionError("测试禁止真实网络：create_connection")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("测试禁止真实网络：urlopen")),
            mock.patch("httpx.Client", side_effect=AssertionError("测试禁止真实网络：httpx.Client")),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        real_import = __import__

        def guarded(name, *args, **kwargs):
            if name == "akshare":
                raise AssertionError("测试禁止真实 akshare：必须注入假模块或 live=False")
            return real_import(name, *args, **kwargs)

        import_patcher = mock.patch("importlib.import_module", side_effect=guarded)
        import_patcher.start()
        self.addCleanup(import_patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="v3calsrc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        src.clear_memory_cache()
        self.addCleanup(src.clear_memory_cache)

    def write_manual_holidays(self, payload):
        path = os.path.join(self.tmp, src.LEGACY_HOLIDAYS_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def calendar_path(self):
        return os.path.join(self.tmp, src.CALENDAR_FILE)


# ── holidays_for ───────────────────────────────────────────────────────────────


class HolidaysForTests(unittest.TestCase):
    def test_workdays_minus_trading_and_no_weekends(self):
        out = src.holidays_for("SH", WINDOW[0], WINDOW[1], TRADING_IN_WINDOW)
        self.assertEqual(out, ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"])
        for text in out:
            self.assertLess(date.fromisoformat(text).weekday(), 5, text)
        self.assertNotIn("2026-10-03", out, "周六不是节假日")
        self.assertNotIn("2026-10-04", out, "周日不是节假日")

    def test_accepts_date_objects_and_rejects_bad_input(self):
        self.assertEqual(src.holidays_for("SH", date(2026, 10, 1), date(2026, 10, 2), []),
                         ["2026-10-01", "2026-10-02"])
        self.assertEqual(src.holidays_for("SH", "bad", "2026-10-02", []), [])
        self.assertEqual(src.holidays_for("SH", "2026-10-02", "2026-10-01", []), [])

    def test_window_and_ttl_helpers(self):
        first, last = src.calendar_window(date(2026, 9, 20))
        self.assertEqual(first, date(2026, 8, 6))
        self.assertEqual(last, date(2027, 10, 25))
        self.assertEqual(src.calendar_window(date(2026, 9, 20), lookback_days=1, lookahead_days=2),
                         (date(2026, 9, 19), date(2026, 9, 22)))
        self.assertEqual(src._ttl_ms(None, {}), src.DEFAULT_TTL_MS)
        self.assertEqual(src._ttl_ms(None, {src.TTL_ENV: "50"}), 50)
        self.assertEqual(src._ttl_ms(None, {src.TTL_ENV: "nonsense"}), src.DEFAULT_TTL_MS)
        self.assertEqual(src._ttl_ms(7, {}), 7)


# ── 来源优先级 ──────────────────────────────────────────────────────────────────


class ResolveSourceTests(OfflineBase):
    def test_futu_hit_shape_chain_and_holidays(self):
        clock = MutableClock(1000.0)
        wb = FakeWbCall({"SH": SH_DAYS})
        result = src.resolve_trading_days(wb, self.tmp, "SH", start=WINDOW[0], end=WINDOW[1],
                                          clock=clock, akshare=FakeAkshare(missing=True))
        self.assertEqual(result["market"], "SH")
        self.assertEqual(result["start"], WINDOW[0])
        self.assertEqual(result["end"], WINDOW[1])
        self.assertEqual(result["source"], "futu/info_trading_days")
        self.assertEqual(result["trading_days"], TRADING_IN_WINDOW)
        self.assertEqual(result["holidays"], NATIONAL_DAY_ISO)
        self.assertEqual(result["coverage"], {"start": "2026-09-28", "end": "2026-10-12"})
        self.assertTrue(result["complete"])
        self.assertTrue(result["as_of"])
        self.assertTrue(result["data_as_of"])
        self.assertEqual(wb.markets, ["SH"], "富途命中的市场码必须原样传给工具面")
        self.assertEqual([item["source"] for item in result["chain"]],
                         ["cache/memory", "cache/disk", "futu/info_trading_days"])
        self.assertFalse(result["chain"][0]["ok"])
        self.assertTrue(result["chain"][-1]["ok"])
        for item in result["chain"]:
            self.assertIn("ms", item)

    def test_deduplicates_and_sorts_trading_days(self):
        wb = FakeWbCall({"SH": [date(2026, 10, 9), date(2026, 9, 28), date(2026, 10, 9)]})
        result = src.resolve_trading_days(wb, self.tmp, "SH", start="2026-09-28", end="2026-10-09",
                                          akshare=FakeAkshare(missing=True))
        self.assertEqual(result["trading_days"], ["2026-09-28", "2026-10-09"])

    def test_futu_failure_falls_back_to_akshare_with_real_coverage(self):
        wb = FakeWbCall(error="上游 -12006 限流")
        ak = FakeAkshare(days=workdays(date(1990, 12, 19), date(2026, 11, 30), NATIONAL_DAY))
        result = src.resolve_trading_days(wb, self.tmp, "SH", start=WINDOW[0], end="2027-10-25",
                                          akshare=ak, retry=fast_retry())
        self.assertEqual(result["source"], "akshare/tool_trade_date_hist_sina")
        self.assertEqual(result["coverage"], {"start": "2026-09-28", "end": "2026-11-30"})
        self.assertFalse(result["complete"], "AKShare 源只到 2026-11-30 → 请求窗口没被完全覆盖")
        self.assertIn("2026-10-01", result["holidays"])
        self.assertEqual(ak.calls, 1, "首次就成功 → 不该重试")
        futu_attempt = [item for item in result["chain"]
                        if item["source"] == "futu/info_trading_days"][0]
        self.assertIn("-12006", futu_attempt["error"]["message"])

    def test_hk_and_us_have_no_akshare_calendar(self):
        wb = FakeWbCall(error="upstream down")
        ak = FakeAkshare(days=SH_DAYS)
        for market in ("HK", "US"):
            detail = src.resolve_market(wb, self.tmp, market, start=WINDOW[0], end=WINDOW[1],
                                        akshare=ak, retry=fast_retry())
            self.assertIsNone(detail["result"], market)
            attempt = [item for item in detail["chain"]
                       if item["source"] == "akshare/tool_trade_date_hist_sina"][0]
            self.assertFalse(attempt["ok"], market)
            self.assertEqual(attempt["error"]["code"], "calendar/akshare-unsupported-market")
            self.assertIn("无开源自历", attempt["error"]["message"])
            self.assertIn("未取到交易日历", detail["reason"])

    def test_both_sources_fail_then_manual_file_is_used(self):
        wb = FakeWbCall(error="upstream down")
        self.write_manual_holidays({"SH": NATIONAL_DAY_ISO})
        result = src.resolve_trading_days(wb, self.tmp, "SH", start=WINDOW[0], end=WINDOW[1],
                                          akshare=FakeAkshare(missing=True), retry=fast_retry())
        self.assertEqual(result["source"], "file")
        self.assertEqual(result["holidays"], NATIONAL_DAY_ISO)
        self.assertEqual(result["trading_days"], TRADING_IN_WINDOW)
        self.assertIn(src.LEGACY_HOLIDAYS_FILE, result["note"])
        self.assertEqual([item["source"] for item in result["chain"]],
                         ["cache/memory", "cache/disk", "futu/info_trading_days",
                          "akshare/tool_trade_date_hist_sina",
                          f"file/{src.LEGACY_HOLIDAYS_FILE}"])

    def test_manual_file_is_not_cached(self):
        wb = FakeWbCall(error="upstream down")
        self.write_manual_holidays({"SH": NATIONAL_DAY_ISO})
        src.resolve_trading_days(wb, self.tmp, "SH", start=WINDOW[0], end=WINDOW[1],
                                 akshare=FakeAkshare(missing=True), retry=fast_retry())
        self.assertFalse(os.path.exists(self.calendar_path()),
                         "人工兜底表不进日历缓存（否则上游恢复后仍被判为没有日历）")

    def test_nothing_available_returns_none_with_reason(self):
        detail = src.resolve_market(None, self.tmp, "SH", start=WINDOW[0], end=WINDOW[1],
                                    live=False)
        self.assertIsNone(detail["result"])
        self.assertFalse(detail["ok"])
        self.assertIn("未取到交易日历", detail["reason"])
        self.assertIn(f"file/{src.LEGACY_HOLIDAYS_FILE}", detail["reason"])
        self.assertIn("未找到节假日文件", detail["reason"])
        self.assertIsNone(src.resolve_trading_days(None, self.tmp, "SH",
                                                  start=WINDOW[0], end=WINDOW[1], live=False))

    def test_bad_window_is_rejected_without_touching_upstream(self):
        wb = FakeWbCall({"SH": SH_DAYS})
        detail = src.resolve_market(wb, self.tmp, "SH", start="2026-10-12", end="2026-09-28")
        self.assertIsNone(detail["result"])
        self.assertIn("start", detail["reason"])
        self.assertEqual(detail["chain"], [])
        self.assertEqual(wb.calls, [])

    def test_live_false_never_calls_upstream(self):
        wb = FakeWbCall({"SH": SH_DAYS})
        detail = src.resolve_market(wb, self.tmp, "SH", start=WINDOW[0], end=WINDOW[1],
                                    live=False)
        self.assertIsNone(detail["result"])
        self.assertEqual(wb.calls, [], "live=False 时不许碰工具面")
        self.assertEqual([item["source"] for item in detail["chain"]],
                         ["cache/memory", "cache/disk", f"file/{src.LEGACY_HOLIDAYS_FILE}"])

    def test_futu_tool_contract_and_unknown_market(self):
        wb = FakeWbCall({"US": workdays(date(2026, 8, 1), date(2027, 12, 31))})
        out = src.fetch_futu_trading_days(wb, "US", "2026-09-28", "2026-10-12")
        self.assertTrue(out["ok"], out)
        self.assertEqual(wb.calls[0][0], src.FUTU_CALENDAR_TOOL)
        self.assertEqual(wb.calls[0][1], {"market": "US", "start": "2026-09-28",
                                          "end": "2026-10-12"})
        self.assertIn("2026-09-28", out["trading_days"])
        bad = src.fetch_futu_trading_days(wb, "XX", "2026-09-28", "2026-10-12")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "calendar/unknown-market")
        empty = src.fetch_futu_trading_days(FakeWbCall(), "SH", "2026-09-28", "2026-10-12")
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["error"]["code"], "calendar/futu-empty")


# ── TTL 与落盘缓存 ─────────────────────────────────────────────────────────────


class CacheTests(OfflineBase):
    def test_ttl_hit_avoids_refetch_and_stale_cache_refetches(self):
        clock = MutableClock(500.0)
        wb = FakeWbCall({"SH": SH_DAYS})
        kwargs = dict(start=WINDOW[0], end=WINDOW[1], clock=clock, ttl_ms=1000,
                      akshare=FakeAkshare(missing=True), retry=fast_retry())
        first = src.resolve_trading_days(wb, self.tmp, "SH", **kwargs)
        self.assertEqual(first["source"], "futu/info_trading_days")
        second = src.resolve_trading_days(wb, self.tmp, "SH", **kwargs)
        self.assertEqual(len(wb.calls), 1, "TTL 内不许重复请求上游")
        self.assertEqual(second["source"], "cache")
        self.assertEqual(second["served_from"], "cache/memory")
        self.assertEqual(second["origin_source"], "futu/info_trading_days")
        self.assertEqual(second["trading_days"], first["trading_days"])
        self.assertEqual(second["holidays"], first["holidays"])
        # 把落盘缓存的 updated_at 改成很旧（模拟 TTL 过期）→ 必须重新取数
        with open(self.calendar_path(), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        stored["updated_at"] = "2000-01-01T00:00:00+00:00"
        with open(self.calendar_path(), "w", encoding="utf-8") as handle:
            json.dump(stored, handle)
        clock.advance(5.0)
        third = src.resolve_trading_days(wb, self.tmp, "SH", **kwargs)
        self.assertEqual(len(wb.calls), 2, "缓存过期后必须重新取数")
        self.assertEqual(third["source"], "futu/info_trading_days")

    def test_disk_cache_roundtrip_and_structure(self):
        clock = MutableClock(1000.0)
        wb = FakeWbCall({"SH": SH_DAYS})
        first = src.resolve_trading_days(wb, self.tmp, "SH", start=WINDOW[0], end="2026-10-31",
                                         clock=clock, akshare=FakeAkshare(missing=True))
        self.assertTrue(os.path.exists(self.calendar_path()))
        with open(self.calendar_path(), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["version"], src.CALENDAR_VERSION)
        self.assertTrue(stored["updated_at"])
        entry = stored["markets"]["SH"]
        self.assertEqual(entry["trading_days"], first["trading_days"])
        self.assertEqual(entry["holidays"], first["holidays"])
        self.assertEqual(entry["source"], "futu/info_trading_days")
        self.assertEqual(entry["coverage"], first["coverage"])
        # 清掉进程内缓存 + 换一个必失败的工具面：这次只能靠落盘缓存
        src.clear_memory_cache()
        broken = FakeWbCall(error="upstream down")
        second = src.resolve_trading_days(broken, self.tmp, "SH", start=WINDOW[0],
                                          end="2026-10-31", clock=MutableClock(2000.0),
                                          akshare=FakeAkshare(missing=True), retry=fast_retry())
        self.assertEqual(second["source"], "cache")
        self.assertEqual(second["served_from"], "cache/disk")
        self.assertEqual(second["trading_days"], first["trading_days"])
        self.assertEqual(second["holidays"], first["holidays"])
        self.assertEqual(broken.calls, [], "落盘缓存命中 → 一次上游都没打")

    def test_write_merges_other_markets_and_survives_broken_file(self):
        src.write_calendar_file(self.tmp, {"SH": {"market": "SH", "trading_days": ["2026-09-28"],
                                                  "holidays": [], "source": "futu/info_trading_days",
                                                  "start": WINDOW[0], "end": WINDOW[1],
                                                  "coverage": {"start": WINDOW[0], "end": WINDOW[1]},
                                                  "complete": True, "data_as_of": "now"}})
        src.write_calendar_file(self.tmp, {"HK": {"market": "HK", "trading_days": ["2026-09-28"],
                                                  "holidays": [], "source": "futu/info_trading_days",
                                                  "start": WINDOW[0], "end": WINDOW[1],
                                                  "coverage": {"start": WINDOW[0], "end": WINDOW[1]},
                                                  "complete": True, "data_as_of": "now"}})
        with open(self.calendar_path(), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(sorted(stored["markets"]), ["HK", "SH"], "写盘必须合并而不是覆盖")
        with open(self.calendar_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        entry, error = src.load_calendar_file(self.tmp, "SH", WINDOW[0], WINDOW[1])
        self.assertIsNone(entry)
        self.assertIn("读取失败", error)
        payload, _ = src.read_calendar_file(self.tmp)
        self.assertIsNone(payload)


# ── refresh_cache ──────────────────────────────────────────────────────────────


class RefreshCacheTests(OfflineBase):
    def test_refresh_writes_live_sources_and_reports_each_market(self):
        wb = FakeWbCall({"SH": SH_DAYS, "HK": HK_DAYS})  # US 缺失 → 空列表 → 失败
        payload = src.refresh_cache(self.tmp, ("SH", "HK", "US"), horizon_days=60, wb_call=wb,
                                    akshare=FakeAkshare(missing=True), retry=fast_retry(),
                                    today=date(2026, 9, 21), clock=MutableClock(1000.0))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["window"]["start"], "2026-08-07")
        self.assertEqual(payload["window"]["end"], "2026-11-20")
        self.assertTrue(payload["markets"]["SH"]["ok"])
        self.assertTrue(payload["markets"]["HK"]["ok"])
        self.assertEqual(payload["markets"]["SH"]["source"], "futu/info_trading_days")
        self.assertFalse(payload["markets"]["US"]["ok"])
        self.assertIn("未取到交易日历", payload["markets"]["US"]["reason"])
        self.assertEqual(payload["path"], self.calendar_path())
        with open(self.calendar_path(), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(sorted(stored["markets"]), ["HK", "SH"], "取不到的市场不写占位条目")
        self.assertEqual(stored["markets"]["SH"]["holidays"][:5], NATIONAL_DAY_ISO[:5])

    def test_refresh_does_not_cache_manual_fallback(self):
        self.write_manual_holidays({"SH": NATIONAL_DAY_ISO})
        payload = src.refresh_cache(self.tmp, ("SH",), horizon_days=60,
                                    wb_call=FakeWbCall(error="upstream down"),
                                    akshare=FakeAkshare(missing=True), retry=fast_retry(),
                                    today=date(2026, 9, 21))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["markets"]["SH"]["source"], "file")
        self.assertIn("非实时源", payload["markets"]["SH"]["reason"])
        self.assertEqual(payload["error"]["code"], "calendar/refresh-no-live-source")
        self.assertFalse(os.path.exists(self.calendar_path()),
                         "全都没取到实时源时不得生成（空）缓存文件")

    def test_refresh_skips_process_and_disk_cache(self):
        clock = MutableClock(1000.0)
        wb = FakeWbCall({"SH": SH_DAYS})
        kwargs = dict(start=WINDOW[0], end=WINDOW[1], clock=clock, ttl_ms=10 ** 9,
                      akshare=FakeAkshare(missing=True), retry=fast_retry())
        src.resolve_trading_days(wb, self.tmp, "SH", **kwargs)
        self.assertEqual(len(wb.calls), 1)
        payload = src.refresh_cache(self.tmp, ("SH",), horizon_days=60, wb_call=wb,
                                    akshare=FakeAkshare(missing=True), retry=fast_retry(),
                                    today=date(2026, 9, 21), clock=clock)
        self.assertTrue(payload["markets"]["SH"]["ok"])
        self.assertEqual(payload["markets"]["SH"]["source"], "futu/info_trading_days")
        self.assertEqual(len(wb.calls), 2, "refresh 必须跳过缓存重取一次")

    def test_resolve_calendar_covers_all_markets(self):
        wb = FakeWbCall({"SH": SH_DAYS, "HK": HK_DAYS, "US": SH_DAYS})
        resolved = src.resolve_calendar(wb, self.tmp, ("SH", "HK", "US"),
                                        start=WINDOW[0], end=WINDOW[1],
                                        akshare=FakeAkshare(missing=True))
        self.assertEqual(sorted(resolved), ["HK", "SH", "US"])
        self.assertEqual(resolved["HK"]["result"]["holidays"], ["2026-10-01"])
        self.assertTrue(resolved["US"]["result"]["source"].startswith("futu/"))


# ── 与 v3_fallback 的配合（重试用的是同一份实现）─────────────────────────────


class RetryIntegrationTests(OfflineBase):
    def test_akshare_retry_is_used_and_recorded(self):
        wb = FakeWbCall(error="upstream down")
        ak = FakeAkshare(days=SH_DAYS, error=ConnectionError("RemoteDisconnected('x')"))
        sleeps = []
        detail = src.resolve_market(wb, self.tmp, "SH", start=WINDOW[0], end=WINDOW[1],
                                    akshare=ak, retry=fast_retry(attempts=3, base_ms=100,
                                                                sleep=sleeps.append))
        self.assertIsNone(detail["result"])
        attempt = [item for item in detail["chain"]
                   if item["source"] == "akshare/tool_trade_date_hist_sina"][0]
        self.assertFalse(attempt["ok"])
        self.assertIn("重试 3 次仍失败", attempt["error"]["message"])
        self.assertIn("RemoteDisconnected", attempt["error"]["message"])
        self.assertEqual(ak.calls, 3, "连接类错误必须重试到 attempts 次")
        self.assertEqual(sleeps, [0.1, 0.2], "退避按注入的假 sleep 递增（10 → 20 → …）")
        self.assertEqual(v3_fallback.is_retryable_akshare(ValueError("bad param")), False)


if __name__ == "__main__":
    unittest.main()
