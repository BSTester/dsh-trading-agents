"""``server.v3_market_calendar`` 契约测试：**离线**、固定时钟、注入替身。

覆盖点（每条都对应一个「如实」的承诺）:
  * 三市场真实时段：SH 09:30–11:30 / 13:00–15:00、HK 09:30–12:00 / 13:00–16:00、
    US 09:30–16:00（含夏令时，用 ``America/New_York`` 判定，测试给冬/夏两个固定时刻）；
  * 周末/节假日 → ``isTradingDay=false``、``session='closed'``、``label`` 说明原因；
  * 节假日表：``<home>/market-holidays.json`` 与 ``QUANT_MARKET_HOLIDAYS`` 内联 JSON 两条路径；
    **文件不存在时只按周末判断**且 ``holidays_loaded=false``；
  * ``nextOpen``：未开盘 → 当天；已收盘 → 下一交易日（周五收盘 → 下周一）；
  * 未知市场/坏 ``now``/空 markets → 错误信封（不 500、不猜）；
  * ``register`` 挂 2 条路由（日历 + 只读语义的刷新端点），``?now=`` 与 ``deps['now']``
    都可注入时钟；
  * **自动获取**（2026-09-21 新增）：假工具面命中富途 → ``holidays_loaded=true``、
    ``calendar_source=futu/info_trading_days``、国庆落在工作日 → ``holiday``/``label`` 正确，
    **周末不计入 holidays**；TTL 内二次请求命中缓存不再打上游；富途失败且无 AKShare →
    退回「只按周末判断」并把真实原因写进 note；刷新端点只写 ``<home>/market-calendar.json``
    （只读语义：home 里不产生别的文件）。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_calendar -v``
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_calendar_source as src  # noqa: E402
from server import v3_market_calendar as cal  # noqa: E402

SH = ZoneInfo("Asia/Shanghai")


def at(text):
    """ISO 串 → aware datetime（测试统一用带偏移的字符串，避免隐式时区）。"""
    return datetime.fromisoformat(text)


def weekdays_between(first, last, skip=()):
    """``[first, last]`` 内的工作日（可选剔除 ``skip``）——用来造假的交易日历。"""
    blocked = set(skip)
    out = []
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5 and cursor not in blocked:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


class FakeFutuTool:
    """假工具面：按 ``market`` 返回 ``info_trading_days`` 信封，并记录每次调用。"""

    def __init__(self, days_by_market=None, error=None):
        self.days_by_market = days_by_market or {}
        self.error = error
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        if self.error is not None:
            return {"ok": False, "error": {"code": "futu/rate-limited", "message": self.error}}
        payload = payload or {}
        market = payload.get("market")
        days = self.days_by_market.get(market)
        if days is None:
            return {"ok": True, "value": {"trading_days": []}}
        # 与真实富途一致：只返回 **请求窗口内** 的交易日。
        first, last = str(payload.get("start") or ""), str(payload.get("end") or "")
        picked = [day for day in days if (not first or str(day) >= first)
                  and (not last or str(day) <= last)]
        return {"ok": True,
                "value": {"trading_days": [{"time": str(day)} for day in picked]}}

    @property
    def markets(self):
        return [payload.get("market") for _name, payload in self.calls]


class FakeApp:
    def __init__(self):
        self.routes = {}
        self.state = types.SimpleNamespace()

    def get(self, path):
        def decorator(func):
            self.routes[path] = func
            return func

        return decorator

    def post(self, path):
        def decorator(func):
            self.routes[path] = func
            return func

        return decorator


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v3cal-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ── 时段 ────────────────────────────────────────────────────────────────
    def test_sh_session_boundaries(self):
        cases = {
            "2026-09-21T09:00:00+08:00": "pre",
            "2026-09-21T09:30:00+08:00": "open",
            "2026-09-21T11:29:00+08:00": "open",
            "2026-09-21T11:30:00+08:00": "lunch",
            "2026-09-21T12:59:00+08:00": "lunch",
            "2026-09-21T13:00:00+08:00": "open",
            "2026-09-21T15:00:00+08:00": "post",
        }
        for text, expected in cases.items():
            state = cal.market_state("SH", at(text), {})
            self.assertTrue(state["isTradingDay"], text)
            self.assertEqual(state["session"], expected, text)
            self.assertEqual(state["open"], "09:30")
            self.assertEqual(state["close"], "15:00")
            self.assertEqual(state["lunch"], ["11:30", "13:00"])

    def test_hk_lunch_ends_at_1600(self):
        state = cal.market_state("HK", at("2026-09-21T12:30:00+08:00"), {})
        self.assertEqual(state["session"], "lunch")
        self.assertEqual(state["close"], "16:00")
        self.assertEqual(state["lunch"], ["12:00", "13:00"])
        self.assertEqual(cal.market_state("HK", at("2026-09-21T15:59:00+08:00"), {})["session"], "open")
        self.assertEqual(cal.market_state("HK", at("2026-09-21T16:00:00+08:00"), {})["session"], "post")

    def test_us_has_no_lunch_and_respects_dst(self):
        # 冬令时：2026-01-15 10:00 EST = 23:00 +08:00
        winter = cal.market_state("US", at("2026-01-15T23:00:00+08:00"), {})
        self.assertEqual(winter["session"], "open")
        self.assertEqual(winter["lunch"], [], "美股没有午休，不能硬凑一段")
        self.assertEqual(winter["timezone"], "America/New_York")
        self.assertTrue(winter["now"].endswith("-05:00"), winter["now"])
        # 夏令时：2026-07-15 10:00 EDT = 22:00 +08:00
        summer = cal.market_state("US", at("2026-07-15T22:00:00+08:00"), {})
        self.assertEqual(summer["session"], "open")
        self.assertTrue(summer["now"].endswith("-04:00"), summer["now"])
        # 同一 +08 墙上时间在夏令时下仍处盘中（16:00 EDT = 次日 04:00 +08:00）
        self.assertEqual(cal.market_state("US", at("2026-07-15T22:30:00+08:00"), {})["session"], "open")
        self.assertEqual(cal.market_state("US", at("2026-07-16T04:00:00+08:00"), {})["session"], "post")

    # ── 周末 / 节假日 ───────────────────────────────────────────────────────
    def test_weekend_is_closed_with_reason(self):
        payload = cal.calendar_payload("SH,HK,US", now=at("2026-09-20T10:00:00+08:00"), home=self.tmp)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertEqual(payload["source"], "platform/market_calendar")
        self.assertFalse(payload["holidays_loaded"], "没有节假日文件时必须说明只按周末判断")
        self.assertIn("只按周末判断", payload["holidays_note"])
        for market in ("SH", "HK", "US"):
            state = payload["markets"][market]
            self.assertFalse(state["isTradingDay"], market)
            self.assertEqual(state["session"], "closed", market)
            self.assertIsNone(state["holiday"], market)
            self.assertEqual(state["label"], "休市（周末）", market)

    def test_holiday_file_is_used_and_labelled(self):
        with open(os.path.join(self.tmp, cal.HOLIDAYS_FILE), "w", encoding="utf-8") as handle:
            json.dump({"SH": ["2026-10-01", "2026-10-02"], "HK": [], "US": []}, handle)
        payload = cal.calendar_payload("SH,HK,US", now=at("2026-10-01T10:00:00+08:00"), home=self.tmp)
        self.assertTrue(payload["holidays_loaded"])
        self.assertIn(cal.HOLIDAYS_FILE, payload["holidays_note"])
        sh = payload["markets"]["SH"]
        self.assertFalse(sh["isTradingDay"])
        self.assertEqual(sh["holiday"], "2026-10-01")
        self.assertIn("节假日", sh["label"])
        self.assertEqual(payload["markets"]["HK"]["isTradingDay"], True, "HK 表为空 → 当天照常交易")
        self.assertIsNone(payload["markets"]["HK"]["holiday"])

    def test_holidays_from_env_inline_json(self):
        env = {cal.HOLIDAYS_ENV: json.dumps({"US": ["2026-11-26"]})}
        payload = cal.calendar_payload("US", now=at("2026-11-26T23:00:00+08:00"), home=self.tmp, env=env)
        self.assertTrue(payload["holidays_loaded"])
        self.assertEqual(payload["markets"]["US"]["holiday"], "2026-11-26")
        self.assertIn("inline", payload["holidays_note"])

    def test_holidays_file_from_env_path_and_bad_entries_ignored(self):
        path = os.path.join(self.tmp, "custom-holidays.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"SH": ["20261001", "not-a-date", 20261002]}, handle)
        payload = cal.calendar_payload("SH", now=at("2026-10-02T10:00:00+08:00"),
                                      home=self.tmp, env={cal.HOLIDAYS_ENV: path})
        self.assertTrue(payload["holidays_loaded"])
        self.assertEqual(payload["markets"]["SH"]["holiday"], "2026-10-02")
        self.assertIn("忽略", payload["holidays_note"])

    def test_broken_holiday_file_falls_back_to_weekends(self):
        with open(os.path.join(self.tmp, cal.HOLIDAYS_FILE), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        payload = cal.calendar_payload("SH", now=at("2026-09-21T10:00:00+08:00"), home=self.tmp)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["holidays_loaded"])
        self.assertIn("读取失败", payload["holidays_note"])
        self.assertEqual(payload["markets"]["SH"]["session"], "open")

    # ── nextOpen ────────────────────────────────────────────────────────────
    def test_next_open_before_and_after_close(self):
        before = cal.calendar_payload("SH", now=at("2026-09-21T08:00:00+08:00"), home=self.tmp)
        self.assertEqual(before["markets"]["SH"]["nextOpen"], "2026-09-21T09:30:00+08:00")
        after = cal.calendar_payload("SH", now=at("2026-09-21T16:00:00+08:00"), home=self.tmp)
        self.assertEqual(after["markets"]["SH"]["nextOpen"], "2026-09-22T09:30:00+08:00")

    def test_next_open_skips_weekend(self):
        payload = cal.calendar_payload("SH", now=at("2026-09-18T16:00:00+08:00"), home=self.tmp)
        self.assertEqual(payload["markets"]["SH"]["nextOpen"], "2026-09-21T09:30:00+08:00")

    def test_next_open_skips_configured_holiday(self):
        holidays = {"SH": {date(2026, 10, 1), date(2026, 10, 2)}}
        payload = cal.calendar_payload("SH", now=at("2026-09-30T16:00:00+08:00"),
                                       holidays=holidays, holidays_note="注入")
        self.assertEqual(payload["markets"]["SH"]["nextOpen"], "2026-10-05T09:30:00+08:00")

    def test_next_open_returns_none_beyond_lookahead(self):
        # 用注入的「永久休市」表把 30 天窗口全部占满 → 宁可 null 也不编日期
        holidays = {"SH": {date(2026, 9, 21).fromordinal(date(2026, 9, 21).toordinal() + offset)
                           for offset in range(0, 40)}}
        payload = cal.calendar_payload("SH", now=at("2026-09-21T10:00:00+08:00"), holidays=holidays)
        self.assertIsNone(payload["markets"]["SH"]["nextOpen"])

    # ── 错误分支 ────────────────────────────────────────────────────────────
    def test_unknown_market_and_bad_now(self):
        payload = cal.calendar_payload("SH,XX", now=at("2026-09-21T10:00:00+08:00"), home=self.tmp)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "calendar/bad-market")
        self.assertEqual(payload["error"]["supported"], sorted(cal.MARKET_SESSIONS))
        payload = cal.calendar_payload("", now=at("2026-09-21T10:00:00+08:00"), home=self.tmp)
        self.assertEqual(payload["error"]["code"], "calendar/bad-args")
        payload = cal.calendar_payload("SH", now="not-a-time", home=self.tmp)
        self.assertEqual(payload["error"]["code"], "calendar/bad-now")

    def test_naive_now_is_documented_as_utc(self):
        payload = cal.calendar_payload("SH", now=datetime(2026, 9, 21, 2, 0), home=self.tmp)
        self.assertTrue(payload["ok"])
        self.assertIn("UTC", payload.get("now_note", ""))
        self.assertEqual(payload["markets"]["SH"]["session"], "open", "02:00Z = 10:00+08")

    def test_parse_markets_dedupes_and_uppercases(self):
        known, unknown = cal.parse_markets("sh, HK ,SH,ZZ")
        self.assertEqual(known, ["SH", "HK"])
        self.assertEqual(unknown, ["ZZ"])

    # ── register ────────────────────────────────────────────────────────────
    def test_register_route_and_injected_clock(self):
        app = FakeApp()
        cal.register(app, lambda *a, **k: None, self.tmp, deps={"now": "2026-09-20T10:00:00+08:00"})
        # 2026-09-21：日历改为自动获取后新增了刷新端点（只读语义：取数 + 写缓存）。
        self.assertIn("/api/v3/markets/calendar", app.routes)
        self.assertIn("/api/v3/markets/calendar/refresh", app.routes)
        payload = asyncio.run(app.routes["/api/v3/markets/calendar"](markets="SH"))
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["markets"]["SH"]["isTradingDay"])
        self.assertEqual(payload["markets"]["SH"]["now"], "2026-09-20T10:00:00+08:00")

    def test_route_now_param_overrides_clock(self):
        app = FakeApp()
        cal.register(app, lambda *a, **k: None, self.tmp, deps={"now": "2026-09-20T10:00:00+08:00"})
        payload = asyncio.run(app.routes["/api/v3/markets/calendar"](
            markets="SH", now="2026-09-21T10:00:00+08:00"))
        self.assertEqual(payload["markets"]["SH"]["session"], "open")

    def test_route_internal_error_becomes_envelope(self):
        app = FakeApp()
        # 注入一个形态非法的节假日表（不是映射）→ 内部抛错必须转错误信封而不是 500
        cal.register(app, lambda *a, **k: None, self.tmp, deps={"holidays": ["not-a-mapping"]})
        payload = asyncio.run(app.routes["/api/v3/markets/calendar"](markets="SH", now=""))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "calendar/internal")


# ── 交易日历自动获取（2026-09-21）─────────────────────────────────────────────


class CalendarAutoSourceTests(unittest.TestCase):
    """``holidays_loaded`` / ``calendar_source`` / 刷新端点：全部离线（假工具面 + 假时钟）。

    固定时钟用 ``?now=`` 注入；``source_deps={"live": True}`` 打开实时取数路径
    （``deps`` 非空时 ``register`` 默认关闭它，避免单测打网络）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v3cal-auto-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        src.clear_memory_cache()
        self.addCleanup(src.clear_memory_cache)

    def _register(self, tool, **deps):
        app = FakeApp()
        cal.register(app, tool, self.tmp, deps=deps)
        return app

    def test_futu_hit_marks_holidays_loaded_and_only_weekdays_are_holidays(self):
        # 窗口按注入的 now（2026-10-01）算：2026-08-17 ~ 2027-11-05。
        national_day = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
                        date(2026, 10, 6), date(2026, 10, 7)]
        first, last = date(2026, 8, 1), date(2027, 12, 31)
        tool = FakeFutuTool({
            "SH": weekdays_between(first, last, national_day),
            "SZ": weekdays_between(first, last, national_day),
            "BJ": weekdays_between(first, last, national_day),
            "HK": weekdays_between(first, last, [date(2026, 10, 1)]),
            "US": weekdays_between(first, last),
        })
        app = self._register(tool, now="2026-10-01T10:00:00+08:00",
                            source_deps={"live": True, "clock": lambda: 100.0})
        payload = asyncio.run(app.routes["/api/v3/markets/calendar"](markets="SH,HK,US"))
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["holidays_loaded"], payload["holidays_note"])
        self.assertEqual(payload["calendar_source"], "futu/info_trading_days")
        self.assertTrue(payload["calendar_complete"])
        self.assertIn("工作日 − 交易日", payload["calendar_note"])
        self.assertEqual(tool.markets, ["SH", "HK", "US"])
        sh = payload["markets"]["SH"]
        self.assertFalse(sh["isTradingDay"])
        self.assertEqual(sh["session"], "closed")
        self.assertEqual(sh["holiday"], "2026-10-01")
        self.assertIn("节假日", sh["label"])
        self.assertEqual(sh["calendar_source"], "futu/info_trading_days")
        self.assertEqual(sh["holidays_in_window"], 5,
                         "国庆 5 个工作日休市；周末不计入 holidays")
        # 港股 10-01 休市、美股 10-01 照常交易 —— 三市场各自判定，不互相污染
        self.assertEqual(payload["markets"]["HK"]["holiday"], "2026-10-01")
        self.assertTrue(payload["markets"]["US"]["isTradingDay"])
        # 周末：休市但**不是**节假日（holiday=null），且不计进 holidays
        weekend = asyncio.run(app.routes["/api/v3/markets/calendar"](
            markets="SH", now="2026-10-03T10:00:00+08:00"))["markets"]["SH"]
        self.assertFalse(weekend["isTradingDay"])
        self.assertIsNone(weekend["holiday"])
        self.assertEqual(weekend["label"], "休市（周末）")
        self.assertEqual(weekend["holidays_in_window"], 5)

    def test_memory_cache_hit_avoids_second_upstream_call(self):
        first, last = date(2026, 8, 1), date(2027, 12, 31)
        tool = FakeFutuTool({"SH": weekdays_between(first, last, [date(2026, 10, 1)])})
        app = self._register(tool, now="2026-10-01T10:00:00+08:00",
                            source_deps={"live": True, "clock": lambda: 100.0})
        first_payload = asyncio.run(app.routes["/api/v3/markets/calendar"](markets="SH"))
        self.assertEqual(first_payload["calendar_source"], "futu/info_trading_days")
        second_payload = asyncio.run(app.routes["/api/v3/markets/calendar"](markets="SH"))
        self.assertEqual(len(tool.calls), 1, "TTL 内第二次请求必须命中进程内缓存，不再打上游")
        self.assertEqual(second_payload["calendar_source"], "cache")
        self.assertEqual(second_payload["markets"]["SH"]["holiday"], "2026-10-01")
        self.assertEqual(second_payload["markets"]["SH"]["calendar_source"], "cache")

    def test_upstream_failure_keeps_weekend_only_with_real_reason(self):
        empty_akshare = types.ModuleType("akshare")  # 没有 tool_trade_date_hist_sina
        tool = FakeFutuTool(error="上游 -12006 限流")
        app = self._register(tool, now="2026-10-01T10:00:00+08:00",
                            source_deps={"live": True, "clock": lambda: 100.0,
                                         "akshare": empty_akshare})
        payload = asyncio.run(app.routes["/api/v3/markets/calendar"](markets="SH"))
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(payload["holidays_loaded"])
        self.assertIsNone(payload["calendar_source"])
        self.assertFalse(payload["calendar_complete"],
                         "一个市场都没取到日历 → coverage 不完整（不是 null）")
        self.assertIn("只按周末判断", payload["holidays_note"])
        self.assertIn("-12006", payload["holidays_note"], "上游真实原因必须可见")
        self.assertIsNone(payload["markets"]["SH"]["calendar_source"])
        self.assertIsNone(payload["markets"]["SH"]["holidays_in_window"])

    def test_refresh_route_fetches_and_only_writes_the_cache_file(self):
        today = date.today()
        # 造一个**工作日**休市（周末本来就不是节假日，用周末会验不出节假日口径）。
        off_day = today + timedelta(days=9)
        while off_day.weekday() >= 5:
            off_day += timedelta(days=1)
        tool = FakeFutuTool({"SH": weekdays_between(today - timedelta(days=60),
                                                    today + timedelta(days=430),
                                                    [off_day])})
        app = self._register(tool, source_deps={"live": True, "clock": lambda: 100.0})
        payload = asyncio.run(app.routes["/api/v3/markets/calendar/refresh"](
            markets="SH", horizon_days=60))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["markets"]["SH"]["source"], "futu/info_trading_days")
        self.assertTrue(payload["markets"]["SH"]["ok"])
        self.assertEqual(payload["path"], os.path.join(self.tmp, cal.CALENDAR_FILE))
        self.assertEqual(sorted(os.listdir(self.tmp)), [cal.CALENDAR_FILE],
                         "刷新是只读语义：home 里只允许出现日历缓存文件")
        with open(payload["path"], "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["version"], 1)
        self.assertTrue(stored["updated_at"])
        entry = stored["markets"]["SH"]
        self.assertEqual(entry["source"], "futu/info_trading_days")
        self.assertEqual(entry["holidays"], [off_day.isoformat()])
        # 落盘缓存可被后续请求命中（同进程内先清内存缓存，验证走的是磁盘那一级）
        src.clear_memory_cache()
        reused = src.resolve_market(tool, self.tmp, "SH",
                                    start=(today - timedelta(days=45)).isoformat(),
                                    end=(today + timedelta(days=50)).isoformat(),
                                    live=True, clock=lambda: 200.0)
        self.assertEqual(reused["result"]["source"], "cache")
        self.assertEqual(reused["result"]["served_from"], "cache/disk")

    def test_refresh_route_rejects_unknown_market(self):
        app = self._register(FakeFutuTool(), source_deps={"live": True})
        payload = asyncio.run(app.routes["/api/v3/markets/calendar/refresh"](markets="SH,XX"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "calendar/bad-market")


if __name__ == "__main__":
    unittest.main()
