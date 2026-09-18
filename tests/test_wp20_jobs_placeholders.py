"""WP20：全新安装演练暴露的三个真实缺陷（作业定义与占位符 / 公告合并可读失败 / 依赖声明）。

背景（2026-09-18 全新安装演练实测，根因见 docs/HANDOVER.md §8.8）：

1. `JOBS_DEFAULT["SH"]` 的 quality 作业只传 `--market SH`，而 CLI 的 quality **必填**
   `--symbols/--start/--end` → argparse 退出 2，该作业在新库上**必然失败**；
2. `@latest-quarter` 从未实现：字面量被当报告期传给 `akshare.stock_yjbb_em`，
   上游抛 `TypeError: 'NoneType' object is not subscriptable`——**我们自己的占位符
   漏实现，却表现为看不懂的上游故障**；
3. `yfinance` 是数据层生产依赖（Yahoo 财报备用源 + 港美股长历史通道）却没进任何
   安装步骤：缺它时 `fundamentals.load_returns` 静默降级到 AKShare（仅 A 股）、
   `fetch_yahoo` 直接失败。

全部离线：不访问网络、不启动子进程、不碰真实 DSH_HOME。
"""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
from trading_core import alerts, cli, daemon, rule_engine, store, sync  # noqa: E402

#: 作业表按名字索引（占位符/CLI 契约用例都要按名字取作业，不靠位置）
JOBS_BY_NAME = {job["name"]: job
                for chain in daemon.JOBS_DEFAULT.values() for job in chain}

#: 固定时钟（北京日 2026-09-18）——占位符用例一律注入，保证确定性
FIXED_NOW = lambda: "2026-09-18 16:10:00"  # noqa: E731


"""WP20：全新安装演练暴露的三个真实缺陷（作业定义与占位符 / 公告合并可读失败 / 依赖声明）。

背景（2026-09-18 全新安装演练实测，根因见 docs/HANDOVER.md §8.8）：

1. `JOBS_DEFAULT["SH"]` 的 quality 作业只传 `--market SH`，而 CLI 的 quality **必填**
   `--symbols/--start/--end` → argparse 退出 2，该作业在新库上**必然失败**；
2. `@latest-quarter` 从未实现：字面量被当报告期传给 `akshare.stock_yjbb_em`，
   上游抛 `TypeError: 'NoneType' object is not subscriptable`——**我们自己的占位符
   漏实现，却表现为看不懂的上游故障**；
3. `yfinance` 是数据层生产依赖（Yahoo 财报备用源 + 港美股长历史通道）却没进任何
   安装步骤：缺它时 `fundamentals.load_returns` 静默降级到 AKShare（仅 A 股）、
   `fetch_yahoo` 直接失败。

全部离线：不访问网络、不启动子进程、不碰真实 DSH_HOME。
"""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
from trading_core import alerts, cli, daemon, rule_engine, store, sync  # noqa: E402

#: 作业表按名字索引（占位符/CLI 契约用例都要按名字取作业，不靠位置）
JOBS_BY_NAME = {job["name"]: job
                for chain in daemon.JOBS_DEFAULT.values() for job in chain}

#: 固定时钟（北京日 2026-09-18）——占位符用例一律注入，保证确定性
FIXED_NOW = lambda: "2026-09-18 16:10:00"  # noqa: E731


class _HomeCase(unittest.TestCase):
    """带临时 home（含关注池）的用例基类。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.write_config({"watchlist": ["SH.600519"]})

    def write_config(self, payload):
        (self.home / "trading-platform.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def resolve(self, cmd, now=FIXED_NOW):
        return daemon.resolve_command(cmd, str(self.home), now=now)


class LatestQuarterPlaceholderTest(_HomeCase):
    """`@latest-quarter` = 今天之前最近一个**已结束**季度末（YYYYMMDD）。"""

    def period(self, day):
        """把作业放在该日 16:05 跑，返回解析后的 --period。"""
        resolved = self.resolve(["merge-announcements", "--period", "@latest-quarter"],
                                now=lambda: f"{day} 16:05:00")
        return resolved[resolved.index("--period") + 1]

    def test_previous_ended_quarter_for_every_boundary(self):
        cases = {
            "2026-09-18": "20260630",   # 常规：三季度尚未结束
            "2026-09-29": "20260630",   # 季末前一天仍是上一季
            "2026-09-30": "20260630",   # **正好季末当天取上一季**（当日报表未出）
            "2026-10-01": "20260930",   # 次日起才认这一季
            "2026-06-29": "20260331",
            "2026-06-30": "20260331",
            "2026-07-01": "20260630",
            "2026-03-30": "20251231",   # 跨年：一季度末之前是去年年报
            "2026-03-31": "20251231",
            "2026-04-01": "20260331",
            "2026-01-05": "20251231",
            "2026-12-31": "20260930",   # 年末当天取三季报
            "2027-01-04": "20261231",
        }
        for day, expect in cases.items():
            with self.subTest(day=day):
                self.assertEqual(self.period(day), expect)

    def test_format_is_compact_report_period(self):
        period = self.period("2026-09-18")
        self.assertRegex(period, r"^\d{8}$", "报告期口径 YYYYMMDD（akshare yjbb 只认这种）")
        self.assertEqual(dt.date.fromisoformat(
            f"{period[:4]}-{period[4:6]}-{period[6:8]}").isoformat(), "2026-06-30")


class TodayPlaceholderTest(_HomeCase):
    """`@today` / `@today-<N>d` = 北京日（与 tick 的 stamp[:10] 同一口径），ISO 格式。"""

    def test_today_is_iso_date(self):
        self.assertEqual(
            self.resolve(["calendar", "--market", "SH", "--start", "@today", "--end", "@today"]),
            ["calendar", "--market", "SH", "--start", "2026-09-18", "--end", "2026-09-18"])

    def test_today_minus_days(self):
        # 180 自然日前——quality 作业窗口的起点
        self.assertEqual(self.resolve(["quality", "--start", "@today-180d"]),
                         ["quality", "--start", "2026-03-22"])

    def test_today_minus_zero_days(self):
        self.assertEqual(self.resolve(["quality", "--end", "@today-0d"]),
                         ["quality", "--end", "2026-09-18"])

    def test_default_clock_is_the_repo_clock(self):
        """默认 now（不注入）走 ``now_stamp``：``DSH_FAKE_NOW`` 生效，与 tick 一致。"""
        with mock.patch.dict(os.environ, {daemon.FAKE_NOW_ENV: "2026-10-01 00:00:00"}):
            self.assertEqual(
                daemon.resolve_command(["calendar", "--start", "@today"], str(self.home)),
                ["calendar", "--start", "2026-10-01"])

    def test_placeholders_are_resolved_per_occurrence(self):
        self.assertEqual(self.resolve(["x", "@today", "--y", "@today-1d", "@today"]),
                         ["x", "2026-09-18", "--y", "2026-09-17", "2026-09-18"])


class LegacyCallContractTest(_HomeCase):
    """既有两参调用逐字不变（tests/test_core_daemon.py 的同一条断言）。"""

    def test_two_argument_call_is_byte_for_byte_unchanged(self):
        self.assertEqual(daemon.resolve_command(["quality", "--market", "SH"], self.home),
                         ["quality", "--market", "SH"])

    def test_plain_commands_never_read_the_clock(self):
        """没有日期占位符时**不采时钟**：坏掉的 ``DSH_FAKE_NOW`` 也不该影响普通作业。"""
        with mock.patch.dict(os.environ, {daemon.FAKE_NOW_ENV: "不是一个时间戳"}):
            self.assertEqual(daemon.resolve_command(["quality", "--market", "SH"], self.home),
                             ["quality", "--market", "SH"])
            with self.assertRaises(ValueError):
                daemon.resolve_command(["quality", "--start", "@today"], self.home)

    def test_watchlist_contract_unchanged(self):
        self.assertEqual(daemon.resolve_command(["sync-bars", "--tickers", "@watchlist"], self.home),
                         ["sync-bars", "--tickers", "SH.600519"])
        # 多处出现时每一处都替换（且 @watchlist 本身**不会**被判成未知占位符）
        self.assertEqual(self.resolve(["a", "--x", "@watchlist", "--y", "@watchlist"]),
                         ["a", "--x", "SH.600519", "--y", "SH.600519"])
        self.write_config({})
        self.assertIsNone(daemon.resolve_command(["sync-bars", "--tickers", "@watchlist"], self.home))


class UnknownPlaceholderTest(_HomeCase):
    """未知占位符必须响：字面量被传给下游的代价是「看不懂的上游故障」。"""

    def test_typo_raises_with_the_known_list(self):
        with self.assertRaises(ValueError) as caught:
            self.resolve(["merge-announcements", "--period", "@lastest-quarter"])
        message = str(caught.exception)
        self.assertIn("未知占位符：@lastest-quarter", message)
        for known in ("@watchlist", "@latest-quarter", "@today", "@today-Nd"):
            self.assertIn(known, message)

    def test_unknown_date_forms_are_rejected(self):
        for bad in ("@today-180", "@today-abc", "@today-180D", "@Today", "@latest-quarter-1"):
            with self.subTest(arg=bad):
                with self.assertRaises(ValueError):
                    self.resolve(["x", bad])

    def test_at_inside_a_value_is_left_alone(self):
        """只认「参数以 @ 开头」——值里出现 @ 的正常参数不受影响。"""
        self.assertEqual(self.resolve(["note", "--author", "a@b.com", "--x", "1@2"]),
                         ["note", "--author", "a@b.com", "--x", "1@2"])

    def test_job_runner_turns_it_into_a_visible_alert(self):
        """抛错经 _run_job → 「作业异常」告警（可见），而不是静默传字面量。"""
        conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(conn.close)
        job = {"name": "merge_announcements",
               "cmd": ["merge-announcements", "--period", "@lastest-quarter"]}
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)}):
            failure = daemon._run_job(conn, job, str(self.home))
        self.assertIn("未知占位符", failure or "")
        row = next(r for r in alerts.list_recent(conn, limit=10)
                   if r["title"] == daemon.JOB_ERROR_ALERT_TITLE)
        self.assertIn("job=merge_announcements", row["detail"])
        self.assertIn("未知占位符", row["detail"])


class JobDefinitionTest(_HomeCase):
    """作业定义必须能被 CLI 真接受（缺陷 1 的不变量）。"""

    def test_every_cmd_job_is_accepted_by_the_cli(self):
        """作业表里每条 cmd 都必须真的能被 CLI 解析——quality 缺参就是这条不变量被破坏。

        占位符先按作业链的真实解析路径展开（@watchlist → 关注池，日期占位符 → 固定时钟），
        再把解析结果交给真 argparse；缺必填参数会 SystemExit。
        """
        parser = cli.build_parser()
        for market, chain in daemon.JOBS_DEFAULT.items():
            for job in chain:
                if "cmd" not in job:
                    continue
                with self.subTest(market=market, job=job["name"]):
                    resolved = self.resolve(job["cmd"])
                    self.assertIsNotNone(resolved)
                    parser.parse_args(resolved)

    def test_sh_quality_job_passes_every_required_argument(self):
        cmd = JOBS_BY_NAME["quality"]["cmd"]
        self.assertEqual(cmd[0], "quality")
        for flag in ("--market", "--symbols", "--start", "--end"):
            self.assertIn(flag, cmd, f"quality 作业缺 {flag}")
        self.assertEqual(self.resolve(cmd),
                         ["quality", "--market", "SH", "--symbols", "SH.600519",
                          "--start", "2026-03-22", "--end", "2026-09-18"])

    def test_quality_window_is_180_calendar_days_covering_the_ic_window(self):
        """窗口是**日历区间**不是交易日计数：`quality.gap_report` 把 start/end 交给
        ``store.trading_days(market, start, end)`` 取区间内的交易日集合，然后逐日比对库内
        bar。它内部**没有**任何回看常量，所以窗口由作业定义决定。

        取 180 自然日的理由：按 250 交易日/年折算 ≈123 交易日，**≥**
        ``rule_engine.IC_WINDOW_DAYS = 120``（运行时 IC 加权的历史窗口，规格 §9.3）——
        质量报告覆盖的正是 IC 加权真正吃的那段时间。若将来改 IC_WINDOW_DAYS，
        本断言会失败并强制重新评估窗口。
        """
        resolved = self.resolve(JOBS_BY_NAME["quality"]["cmd"])
        start = resolved[resolved.index("--start") + 1]
        end = resolved[resolved.index("--end") + 1]
        self.assertEqual((dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days, 180)
        self.assertGreaterEqual(int(180 * 250 / 365), rule_engine.IC_WINDOW_DAYS)

    def test_market_only_selects_the_calendar(self):
        """`--market` 只决定按哪张日历取交易日（并原样回显）；不筛标的。"""
        resolved = self.resolve(JOBS_BY_NAME["quality"]["cmd"])
        self.assertEqual(resolved[resolved.index("--market") + 1], "SH")
        self.assertEqual(resolved[resolved.index("--symbols") + 1], "SH.600519")

    def test_sh_quality_job_runs_through_the_real_cli(self):
        """按作业定义真跑一次 CLI（离线：临时库 + 显式 --db，不碰真实 DSH_HOME）。"""
        db = str(self.home / "t.sqlite")
        conn = store.connect(db)
        try:
            store.upsert_calendar(conn, "SH", [
                {"day": "2026-09-17", "trade_date_type": "WHOLE", "trade_second": 14400},
                {"day": "2026-09-18", "trade_date_type": "WHOLE", "trade_second": 14400}])
            store.upsert_bars(conn, "SH.600519", "1d", [], "futu/x")  # 无 bar → 全缺口
        finally:
            conn.close()
        cmd = self.resolve(JOBS_BY_NAME["quality"]["cmd"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main([*cmd, "--db", db])
        self.assertEqual(code, 0, buf.getvalue())
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["market"], "SH")
        self.assertEqual(payload["range"], ["2026-03-22", "2026-09-18"])
        self.assertEqual(payload["gaps"]["SH.600519"], ["2026-09-17", "2026-09-18"])
        self.assertIn("announced_coverage", payload)

    def test_quality_is_declared_for_sh_only(self):
        """只有 SH 链挂 quality（刻意，不是漏配）——理由写进 daemon.JOBS_DEFAULT 注释。"""
        self.assertIn("quality", [j["name"] for j in daemon.JOBS_DEFAULT["SH"]])
        for market in ("HK", "US"):
            self.assertNotIn("quality", [j["name"] for j in daemon.JOBS_DEFAULT[market]], market)


class MergeAnnouncementsFailureTest(_HomeCase):
    """akshare 取数异常必须转成**可读**错误（PIT 字段缺数据必须可见）。"""

    def setUp(self):
        super().setUp()
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def test_upstream_error_becomes_a_readable_runtime_error(self):
        class FakeAk:
            @staticmethod
            def stock_yjbb_em(date):
                # 实测的下游形态：akshare 内部对 None 结果取下标
                raise TypeError("'NoneType' object is not subscriptable")

        with self.assertRaises(RuntimeError) as caught:
            sync.merge_announcements_akshare(self.conn, "20260630", akshare_module=FakeAk)
        message = str(caught.exception)
        self.assertIn("上游 akshare/eastmoney 接口异常", message)
        self.assertIn("stock_yjbb_em", message)
        self.assertIn("20260630", message)
        self.assertIn("TypeError", message)
        self.assertIsInstance(caught.exception.__cause__, TypeError,
                              "原始异常必须挂在 __cause__ 上，不丢证据")

    def test_none_or_empty_frame_keeps_the_soft_return(self):
        import pandas as pd

        class NoneAk:
            @staticmethod
            def stock_yjbb_em(date):
                return None

        class EmptyAk:
            @staticmethod
            def stock_yjbb_em(date):
                return pd.DataFrame([])

        for fake in (NoneAk, EmptyAk):
            with self.subTest(fake=fake.__name__):
                self.assertEqual(
                    sync.merge_announcements_akshare(self.conn, "20260630", akshare_module=fake),
                    {"matched": 0, "rows": 0})

    def test_cli_failure_envelope_names_the_upstream(self):
        """CLI 侧必须给**失败信封**：裸 traceback 走 stderr，而作业失败告警的 detail 只
        引用 stdout 尾部——裸抛等于「exit=1 + 没有原因」。"""
        class FakeAk:
            @staticmethod
            def stock_yjbb_em(date):
                raise TypeError("'NoneType' object is not subscriptable")

        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"akshare": FakeAk}):
            with redirect_stdout(buf):
                code = cli.main(["merge-announcements", "--period", "20260630",
                                 "--db", str(self.home / "t.sqlite")])
        self.assertEqual(code, 1)
        printed = buf.getvalue()
        payload = json.loads(printed)
        self.assertFalse(payload["ok"])
        self.assertIn("上游 akshare/eastmoney 接口异常", payload["error"])

        # 与调度层同一条链：runner 的 tail/summary → 「作业失败」告警 detail 能指认上游
        lines = [line for line in printed.splitlines() if line.strip()]
        result = {"exit_code": code, "summary": daemon._last_json_object(printed),
                  "tail": lines[-daemon.OUTPUT_TAIL_LINES:]}
        daemon._report_job_outcome(self.conn, {"name": "merge_announcements"},
                                   str(self.home), result, "SH")
        row = next(r for r in alerts.list_recent(self.conn, limit=10)
                   if r["title"] == daemon.JOB_FAILED_ALERT_TITLE)
        self.assertIn("上游 akshare/eastmoney", row["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
