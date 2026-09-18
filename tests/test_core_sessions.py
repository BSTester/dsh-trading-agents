"""``trading_core.sessions``：某市场某本地日的**真实交易时段**事实源。

两件事在此锁定：
  * **真实收盘**（全天走已知收盘表、半日/提前收盘 = 开盘 + ``trade_second``）；
  * **可委托窗口**（人工下单闸门用；取向「宁可放过、不可错杀」）。

半日市的算法必须按**单段**（不含午休）理解：港股全天 5.5h 里含 1h 午休
（09:30–12:00 + 13:00–16:00），而半日市**只有上午**（09:30–12:00）——用「全天收盘 −
缺口秒数」会算成 13:00（错），用「开盘 + trade_second」得 12:00（对）。见模块 docstring
与本文件 ``test_hk_half_day_is_single_segment_not_gap_subtraction``。
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import sessions  # noqa: E402

TZ8 = timezone(timedelta(hours=8))


def beijing(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ8)


class MarketTableTest(unittest.TestCase):
    """常量表：与实测（2026-09-17 真机日历）逐值一致，且时区用 IANA 名。"""

    def test_full_day_seconds_match_measured_calendar(self):
        self.assertEqual(sessions.FULL_DAY_SECONDS["SH"], 14400)   # 4h
        self.assertEqual(sessions.FULL_DAY_SECONDS["SZ"], 14400)
        self.assertEqual(sessions.FULL_DAY_SECONDS["BJ"], 14400)
        self.assertEqual(sessions.FULL_DAY_SECONDS["HK"], 19800)   # 5.5h
        self.assertEqual(sessions.FULL_DAY_SECONDS["US"], 23400)   # 6.5h

    def test_timezones_are_iana_names(self):
        for market in ("SH", "SZ", "BJ"):
            self.assertEqual(sessions.market_tz(market).key, "Asia/Shanghai")
        self.assertEqual(sessions.market_tz("HK").key, "Asia/Hong_Kong")
        self.assertEqual(sessions.market_tz("US").key, "America/New_York")

    def test_unknown_market_returns_none(self):
        self.assertIsNone(sessions.market_tz("XX"))
        self.assertIsNone(sessions.full_day_seconds("XX"))


class SessionCloseTest(unittest.TestCase):
    """真实收盘：全天走已知收盘表；半日= 开盘 + trade_second（单段口径）。"""

    def test_full_day_uses_known_close(self):
        self.assertEqual(sessions.session_close("SH", "2026-09-17"),
                         beijing("2026-09-17 15:00:00").astimezone(sessions.market_tz("SH")))
        for market, hhmm in (("HK", "16:00"), ("US", "16:00")):
            close = sessions.session_close(market, "2026-09-17")
            self.assertEqual((close.hour, close.minute), tuple(int(p) for p in hhmm.split(":")))
            self.assertEqual(close.tzinfo.key, sessions.market_tz(market).key)

    def test_full_day_is_used_when_trade_second_missing_or_full(self):
        open_dt = sessions.session_close("HK", "2026-09-17")
        for trade_second in (None, 19800, "19800"):
            self.assertEqual(sessions.session_close("HK", "2026-09-17", trade_second), open_dt)
            self.assertFalse(sessions.is_short_day("HK", trade_second))

    def test_hk_half_day_close_is_open_plus_trade_second(self):
        """港股半日市（trade_second=9000=2.5h）→ 本地 12:00。"""
        close = sessions.session_close("HK", "2026-09-17", 9000)
        self.assertEqual((close.hour, close.minute), (12, 0))
        self.assertTrue(sessions.is_short_day("HK", 9000))

    def test_hk_half_day_is_single_segment_not_gap_subtraction(self):
        """反证：用「全天收盘 − 缺口」会得 13:00——半日市没有午休，故必须按单段算。"""
        close = sessions.session_close("HK", "2026-09-17", 9000)
        self.assertNotEqual((close.hour, close.minute), (13, 0),
                            "港股半日市只有上午（09:30–12:00），午休不适用")

    def test_us_half_day_close_is_open_plus_trade_second(self):
        """美股半日市（trade_second=12600=3.5h）→ 本地 13:00（09:30–13:00 单段）。"""
        close = sessions.session_close("US", "2026-09-17", 12600)
        self.assertEqual((close.hour, close.minute), (13, 0))

    def test_us_full_day_is_not_recomputed_as_open_plus_seconds(self):
        """反证：全天走已知收盘表——用「开盘 + 全天秒」会把美股算成 16:00 之外的值。"""
        self.assertEqual(sessions.session_close("US", "2026-09-17", 23400).hour, 16)
        # DST 由 zoneinfo 负责：冬夏两季的「北京时刻」不同，但本地收盘恒为 16:00
        for day in ("2026-01-15", "2026-07-15"):
            self.assertEqual(sessions.session_close("US", day).hour, 16)
        self.assertNotEqual(sessions.session_close_beijing("US", "2026-01-15"),
                            sessions.session_close_beijing("US", "2026-07-15"),
                            "冬夏两季的北京口径收盘相差 1 小时（zoneinfo 算的，不手算）")

    def test_session_close_beijing_is_naive_beijing(self):
        self.assertEqual(sessions.session_close_beijing("SH", "2026-09-17"),
                         datetime(2026, 9, 17, 15, 0, 0))
        self.assertEqual(sessions.session_close_beijing("HK", "2026-09-17", 9000),
                         datetime(2026, 9, 17, 12, 0, 0))
        # 美股半日：ET 13:00 = 北京次日 01:00（EDT）
        self.assertEqual(sessions.session_close_beijing("US", "2026-09-17", 12600),
                         datetime(2026, 9, 18, 1, 0, 0))


class WindowsTest(unittest.TestCase):
    """可委托窗口（宽松超集：只挡明确闭市）。"""

    def only(self, market, day, **kw):
        windows = sessions.windows_for(market, day, **kw)
        self.assertEqual(len(windows), 1, windows)
        return windows[0]

    def test_sh_window_includes_opening_auction_and_lunch_break(self):
        open_dt, close_dt = self.only("SH", "2026-09-17")
        self.assertEqual((open_dt.hour, open_dt.minute), (9, 15))
        self.assertEqual((close_dt.hour, close_dt.minute), (15, 0))

    def test_sz_bj_share_sh_window(self):
        for market in ("SZ", "BJ"):
            open_dt, _close = self.only(market, "2026-09-17")
            self.assertEqual((open_dt.hour, open_dt.minute), (9, 15))

    def test_hk_window_keeps_closing_auction_on_full_day(self):
        open_dt, close_dt = self.only("HK", "2026-09-17")
        self.assertEqual((open_dt.hour, open_dt.minute), (9, 0))
        self.assertEqual((close_dt.hour, close_dt.minute), (16, 10))

    def test_us_rth_window_by_default_and_when_rth_requested(self):
        for session in (None, "RTH"):
            open_dt, close_dt = self.only("US", "2026-09-17", us_session=session)
            self.assertEqual((open_dt.hour, open_dt.minute), (9, 30))
            self.assertEqual((close_dt.hour, close_dt.minute), (16, 0))

    def test_us_pre_post_sessions_use_extended_window(self):
        """官方 PLACE_SESSIONS 里除 RTH 外的三个取值都按「扩展时段」放行（宁可放过）。"""
        for session in ("RTH+Pre/Post-Mkt", "OVERNIGHT", "ALL_DAY"):
            open_dt, close_dt = self.only("US", "2026-09-17", us_session=session)
            self.assertEqual((open_dt.hour, open_dt.minute), (4, 0))
            self.assertEqual((close_dt.hour, close_dt.minute), (20, 0))

    def test_unknown_us_session_fails_closed(self):
        self.assertEqual(sessions.windows_for("US", "2026-09-17", us_session="NOPE"), [])

    def test_unknown_market_fails_closed(self):
        self.assertEqual(sessions.windows_for("XX", "2026-09-17"), [])

    def test_half_day_shortens_window_upper_bound(self):
        hk = self.only("HK", "2026-09-17", trade_second=9000)
        self.assertEqual((hk[1].hour, hk[1].minute), (12, 0), "港股半日：上界是真实收盘")
        us = self.only("US", "2026-09-17", trade_second=12600, us_session="RTH")
        self.assertEqual((us[1].hour, us[1].minute), (13, 0), "美股半日：上界是真实收盘")

    def test_half_day_keeps_extended_lower_bound(self):
        """美股半日 + 盘前请求：下界 04:00 不变（只收上界），盘前仍可委托。"""
        open_dt, close_dt = self.only("US", "2026-09-17", trade_second=12600,
                                      us_session="RTH+Pre/Post-Mkt")
        self.assertEqual((open_dt.hour, open_dt.minute), (4, 0))
        self.assertEqual((close_dt.hour, close_dt.minute), (13, 0))

    def test_lower_above_upper_is_no_window(self):
        """下界高于上界（极端提前收盘）→ 无可委托时段，而不是负长度窗口。

        表里的下界都早于最早可能的真实收盘，故用打补丁的窗口表构造该边界。
        """
        with mock.patch.dict(sessions.ORDER_WINDOWS, {"SH": ((9, 40), (15, 0))}):
            # trade_second=1 → 真实收盘 09:30:01 < 下界 09:40
            self.assertEqual(sessions.windows_for("SH", "2026-09-17", trade_second=1), [])

    def test_format_windows_is_human_readable(self):
        text = sessions.format_windows(sessions.windows_for("SH", "2026-09-17"))
        self.assertIn("09:15", text)
        self.assertIn("15:00", text)
        self.assertEqual(sessions.format_windows([]), "无（当日已无可委托时段）")


class InWindowTest(unittest.TestCase):
    """``in_window``：闸门判定 + 可读原因（含市场/本地时间/当日窗口）。"""

    def test_sh_before_open_is_rejected(self):
        allowed, reason = sessions.in_window("SH", beijing("2026-09-17 09:00:00"))
        self.assertFalse(allowed)
        self.assertIn("SH", reason)
        self.assertIn("09:15", reason)
        self.assertIn("平台前置校验", reason)

    def test_sh_continuous_auction_is_allowed(self):
        self.assertTrue(sessions.in_window("SH", beijing("2026-09-17 10:00:00"))[0])

    def test_sh_lunch_break_is_allowed(self):
        """午休**刻意算在窗口内**：券商普遍接受午间报单并排队，挡掉就是错杀。"""
        allowed, _reason = sessions.in_window("SH", beijing("2026-09-17 12:15:00"))
        self.assertTrue(allowed)

    def test_sh_after_close_is_rejected(self):
        allowed, reason = sessions.in_window("SH", beijing("2026-09-17 15:30:00"))
        self.assertFalse(allowed)
        self.assertIn("15:00", reason)

    def test_sh_close_boundary_is_half_open(self):
        self.assertTrue(sessions.in_window("SH", beijing("2026-09-17 14:59:59"))[0])
        self.assertFalse(sessions.in_window("SH", beijing("2026-09-17 15:00:00"))[0])

    def test_moment_accepts_naive_beijing_datetime_and_string(self):
        for moment in (datetime(2026, 9, 17, 10, 0, 0), "2026-09-17 10:00:00",
                       beijing("2026-09-17 10:00:00")):
            self.assertTrue(sessions.in_window("SH", moment)[0], moment)

    def test_moment_tz_aware_is_converted_not_relabelled(self):
        """同一时刻的两种写法（aware UTC / 北京时间）判定必须一致。"""
        utc = datetime(2026, 9, 17, 2, 0, 0, tzinfo=timezone.utc)  # = 北京 10:00
        self.assertTrue(sessions.in_window("SH", utc)[0])

    def test_unknown_market_fails_closed_with_reason(self):
        allowed, reason = sessions.in_window("XX", beijing("2026-09-17 10:00:00"))
        self.assertFalse(allowed)
        self.assertIn("未知市场", reason)

    def test_unknown_us_session_fails_closed_with_reason(self):
        allowed, reason = sessions.in_window(
            "US", beijing("2026-09-17 22:00:00"), us_session="NOPE")
        self.assertFalse(allowed)
        self.assertIn("NOPE", reason)

    def test_us_rth_outside_window_rejected_and_premarket_allowed(self):
        # 北京 05:00 = ET 17:00（EDT 前一日）→ 常规时段之外
        moment = beijing("2026-09-18 05:00:00")
        self.assertFalse(sessions.in_window("US", moment, trade_second=23400)[0])
        # 北京 17:00 = ET 05:00 → 盘前（扩展窗口内），请求盘前时放行
        premarket = beijing("2026-09-17 17:00:00")
        self.assertFalse(sessions.in_window("US", premarket)[0], "未请求盘前 → 拒绝")
        self.assertTrue(sessions.in_window("US", premarket,
                                           us_session="RTH+Pre/Post-Mkt")[0])

    def test_hk_half_day_after_real_close_is_rejected(self):
        """本组要修的差别：HK 半日市真实收盘 12:00——12:30 必须拒绝（旧口径按 16:10 放行）。"""
        allowed, reason = sessions.in_window("HK", beijing("2026-09-17 12:30:00"),
                                             trade_second=9000)
        self.assertFalse(allowed)
        self.assertIn("12:00", reason)

    def test_hk_full_day_same_moment_is_allowed(self):
        """对照：同一时刻若当日是全天行（trade_second=19800）则放行。"""
        self.assertTrue(sessions.in_window("HK", beijing("2026-09-17 12:30:00"),
                                           trade_second=19800)[0])

    def test_us_half_day_after_real_close_is_rejected(self):
        # 北京 2026-09-18 02:00 = ET 2026-09-17 14:00（EDT）→ 半日市（13:00 收盘）之后
        allowed, reason = sessions.in_window("US", beijing("2026-09-18 02:00:00"),
                                             trade_second=12600)
        self.assertFalse(allowed)
        self.assertIn("13:00", reason)

    def test_reason_carries_market_local_time_and_windows(self):
        allowed, reason = sessions.in_window("SH", beijing("2026-09-17 20:05:00"))
        self.assertFalse(allowed)
        for text in ("SH", "2026-09-17 20:05", "09:15", "15:00", "券商"):
            self.assertIn(text, reason)


class LocalDateTest(unittest.TestCase):
    """市场本地日折算（闸门据此查当日 trade_second）。"""

    def test_market_local_date_follows_market_timezone(self):
        moment = beijing("2026-09-18 05:00:00")  # ET 2026-09-17 17:00
        self.assertEqual(sessions.market_local("US", moment).date().isoformat(), "2026-09-17")
        self.assertEqual(sessions.market_local("SH", moment).date().isoformat(), "2026-09-18")

    def test_market_local_unknown_market_raises_key_error(self):
        with self.assertRaises(KeyError):
            sessions.market_local("XX", beijing("2026-09-18 05:00:00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
