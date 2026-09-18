"""WP18 第二组：`auto_execute` 守卫链补**交易日判定**（纵深防御）。

**缺口与实测**（2026-09-18）：九守卫（1 开关 / 2 sim-only / 3 kill / 4 halt / 5+6 计划存在
且 mode+market 匹配 / 7 当日幂等 / 9 执行窗口 / 8 写指令）**不含交易日判定**。在法定假日
2026-05-01 09:35 直接调 `auto_execute`（临时库 + 真实日历）照样写出了 `execute_plan` 指令；
只有下游 `risk.pre_trade_checks` 的规则 3（`is_trading_day`）逐单拒单兜底。线上调度器会挡住
（非交易日不跑市场链），所以这是**纵深防御**缺口：CLI / MCP / E2E 直接调用时必须有同一道闸。

**分级**：`RuntimeError`（日历未同步）→ warn（与守卫 5+6 的既有写法同分级）；False
（真实休市）→ **info**——真实休市不是故障，与 `planner.plan_auto` 的软跳过语义一致。
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from trading_core import autopilot, cli, commands, daemon, pipeline, store  # noqa: E402

#: 日历覆盖 2026-09-16(三)~2026-10-09(五)，其中 09-19/09-20 是周末、10-01~10-08 是国庆
#: 长假（真实休市：**在覆盖范围内但不在白名单**）；TRADING 是白名单内的交易日。
DAYS = ["2026-09-16", "2026-09-17", "2026-09-18",
        "2026-09-21", "2026-09-22", "2026-09-30", "2026-10-09"]
TRADING = "2026-09-18"      # 周五
WEEKEND = "2026-09-19"      # 周六
HOLIDAY = "2026-10-01"      # 周四，国庆长假（真实法定假日）
PREV = "2026-09-17"


def _config(enabled=True):
    return {"watchlist": ["SH.600519"], "auto_pipeline": {
        "enabled": enabled,
        "strategies": [{"market": "SH", "strategy": "rsi", "watchlist": "SH"}],
        "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
        "reconcile_at": "19:00"}}


class TradingDayGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.conn = store.connect(str(store.db_path(str(self.home))))
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        store.upsert_calendar(self.conn, "SH", [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in DAYS])
        (self.home / "trading-platform.json").write_text(
            json.dumps(_config(), ensure_ascii=False), encoding="utf-8")
        (self.home / "trading-account-mode").write_text("sim", encoding="utf-8")

    # —— 夹具 ——
    def _plan(self, as_of=PREV, plan_id="PLN-1", content_hash="h1"):
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
            "status,created_at,origin,market) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (plan_id, as_of, "sim", "rsi", "{}", content_hash, "frozen",
             f"{as_of} 16:20:00", "auto", "SH"))
        self.conn.commit()

    def _pending(self):
        d = self.home / "trading-commands" / "pending"
        return sorted(p.name for p in d.glob("*.json")) if d.exists() else []

    def _alerts(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT level,title,detail FROM alerts ORDER BY id")]

    def _run(self, today, now=None, market="SH"):
        stamp = now or f"{today} 09:40:00"      # 09:40 在 SH 默认窗口（09:35 起 30 分钟）内
        return daemon.auto_execute(self.conn, str(self.home), market, today=today,
                                   now=lambda: stamp)

    # —— 假日 / 周末：不写任何指令 ——
    def test_weekend_writes_no_command(self):
        self._plan()
        out = self._run(WEEKEND)
        self.assertTrue(out["ok"])
        self.assertIn("非", out["skipped"])
        self.assertIn("交易日", out["skipped"])
        self.assertEqual(self._pending(), [], "周末不得写出 execute_plan 指令")
        self.assertIsNone(store.kv_get(self.conn, f"auto_exec:{'SH'}:{WEEKEND}"))

    def test_weekday_holiday_writes_no_command(self):
        self._plan(as_of="2026-09-30")
        out = self._run(HOLIDAY)
        self.assertTrue(out["ok"])
        self.assertIn("交易日", out["skipped"])
        self.assertEqual(self._pending(), [], "法定假日不得写出 execute_plan 指令")

    def test_holiday_skip_is_info_with_stable_title(self):
        """真实休市不是故障 → info（与 plan_auto 的软跳过同分级），标题是稳定字面量。"""
        self._plan()
        self._run(WEEKEND)
        rows = self._alerts()
        self.assertEqual([r["level"] for r in rows], ["info"])
        self.assertEqual(rows[0]["title"], autopilot.TRADING_DAY_ALERT_TITLE)
        self.assertIn(WEEKEND, rows[0]["detail"])

    def test_guard_runs_before_plan_lookup(self):
        """顺序锁：假日即使**没有任何计划**，告警也必须是「非交易日」而不是「无待执行计划」
        ——证明守卫插在守卫 5+6（计划存在性）之前。"""
        out = self._run(HOLIDAY)
        self.assertIn("交易日", out["skipped"])
        self.assertEqual([r["title"] for r in self._alerts()],
                         [autopilot.TRADING_DAY_ALERT_TITLE])

    def test_cli_exit_code_is_zero_on_holiday(self):
        """软跳过=退出 0（当日不执行是正常结论），且零指令。"""
        self._plan()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["auto-execute", "--market", "SH", "--home", str(self.home),
                             "--db", str(store.db_path(str(self.home))),
                             "--today", WEEKEND, "--now", f"{WEEKEND} 09:40:00"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertEqual(self._pending(), [])

    # —— 交易日：行为与既有逐字一致 ——
    def test_trading_day_still_writes_command(self):
        self._plan(content_hash="hash-abc")
        out = self._run(TRADING)
        self.assertTrue(out["ok"])
        self.assertEqual(out["plan"]["content_hash"], "hash-abc")
        pending = self._pending()
        self.assertEqual(len(pending), 1)
        body = json.loads((self.home / "trading-commands" / "pending" / pending[0])
                          .read_text(encoding="utf-8"))
        self.assertEqual(body["type"], "execute_plan")
        self.assertEqual(body["plan_hash"], "hash-abc")
        self.assertEqual(body["expected_mode"], "sim")
        self.assertIsNotNone(store.kv_get(self.conn, f"auto_exec:SH:{TRADING}"))

    def test_calendar_missing_warns_and_writes_nothing(self):
        """日历零行 → warn（与守卫 5+6 的既有分级一致），不写指令、不抛。"""
        empty = store.connect(":memory:")
        self.addCleanup(empty.close)
        store.migrate(empty)
        out = daemon.auto_execute(empty, str(self.home), "SH", today=TRADING,
                                  now=lambda: f"{TRADING} 09:40:00")
        self.assertTrue(out["ok"])
        self.assertIn("日历", out["skipped"])
        rows = [dict(r) for r in empty.execute(
            "SELECT level,title FROM alerts ORDER BY id")]
        self.assertEqual([(r["level"], r["title"]) for r in rows], [("warn", "日历未同步")])
        self.assertEqual(self._pending(), [])

    # —— 标题登记（pipeline 归因表按标题精确匹配） ——
    def test_title_is_registered_for_auto_execute_stage(self):
        self.assertEqual(autopilot.TRADING_DAY_ALERT_TITLE, "非交易日")
        self.assertEqual(pipeline._ALERT_STATUS[autopilot.TRADING_DAY_ALERT_TITLE],
                         ("auto_execute", "skipped"))

    def test_guard_is_documented_in_docstring(self):
        """纵深防御的**理由**必须写在 docstring 里（否则下一轮会有人把它当多余守卫删掉）。"""
        self.assertIn("纵深防御", autopilot.auto_execute.__doc__)


if __name__ == "__main__":
    unittest.main()
