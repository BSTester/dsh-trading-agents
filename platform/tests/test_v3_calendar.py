"""``server.v3_market_calendar`` 契约测试：**离线**、固定时钟、注入替身。

覆盖点（每条都对应一个「如实」的承诺）:
  * 三市场真实时段：SH 09:30–11:30 / 13:00–15:00、HK 09:30–12:00 / 13:00–16:00、
    US 09:30–16:00（含夏令时，用 ``America/New_York`` 判定，测试给冬/夏两个固定时刻）；
  * 周末/节假日 → ``isTradingDay=false``、``session='closed'``、``label`` 说明原因；
  * 节假日表：``<home>/market-holidays.json`` 与 ``QUANT_MARKET_HOLIDAYS`` 内联 JSON 两条路径；
    **文件不存在时只按周末判断**且 ``holidays_loaded=false``；
  * ``nextOpen``：未开盘 → 当天；已收盘 → 下一交易日（周五收盘 → 下周一）；
  * 未知市场/坏 ``now``/空 markets → 错误信封（不 500、不猜）；
  * ``register`` 只挂 1 条路由，``?now=`` 与 ``deps['now']`` 都可注入时钟。

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
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_market_calendar as cal  # noqa: E402

SH = ZoneInfo("Asia/Shanghai")


def at(text):
    """ISO 串 → aware datetime（测试统一用带偏移的字符串，避免隐式时区）。"""
    return datetime.fromisoformat(text)


class FakeApp:
    def __init__(self):
        self.routes = {}
        self.state = types.SimpleNamespace()

    def get(self, path):
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
        self.assertEqual(list(app.routes), ["/api/v3/markets/calendar"])
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


if __name__ == "__main__":
    unittest.main()
