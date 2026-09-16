"""WP9 任务 5：auto-execute 作业（CLI auto-execute）——八守卫 + 指令文件复用人工窄门。

全部离线：临时 home、显式日期、日历与 plans 直接种子；券商通道仅在「与人工路径同构」
用例注入假 call。覆盖计划任务 5 的全部要求与边界：

  * 八守卫逐项跳过：enabled=False（静默）/ mode=live（info，文案含「等待人工执行」）/
    kill 文件 / halt / 无 auto frozen 计划 / market 不符 / 当日已执行 / expected_mode；
  * 正常路径：pending/ 出现 execute_plan 指令（payload 含 plan_hash 与 expected_mode=sim）
    且 kv 当日标记写入；
  * 幂等：同日第二次调用 → 跳过、不产生第二条指令；
  * 与人工路径同构：该指令喂给 daemon.handle_command → 订单 submitted（复用窄门）；
  * fail-closed：配置非法 / 模式文件非法 → ok=False + warn 告警 + 零指令；
  * 加固与边界：live 计划绝不自动执行、手工计划不参与、陈旧计划（早于上一交易日）拒绝、
    今日计划接受（跨市场日期空间容差）、日历缺失跳过。
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import cli, commands, daemon, oms, store  # noqa: E402

TODAY = "2026-09-16"
PREV = "2026-09-15"
STALE = "2026-09-14"
# 交易日序列：09-11(五) / 09-14(一) / 09-15(二) / 09-16(三)
DAYS = ["2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16"]


def _config(enabled=True, market="SH", strategy="rsi"):
    return {"watchlist": ["SH.600519"], "auto_pipeline": {
        "enabled": enabled,
        "strategies": [{"market": market, "strategy": strategy, "watchlist": market}],
        "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
        "reconcile_at": "19:00"}}


class AutoExecuteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # 用 CLI 的规范库路径（--home 解析到 <home>/trading-data/trading.sqlite），
        # 否则 CLI 子命令会打开另一个空库、与测试种子分叉
        self.conn = store.connect(str(store.db_path(str(self.home))))
        store.migrate(self.conn)
        store.upsert_calendar(self.conn, "SH", [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in DAYS])
        self._write_config()
        self._set_mode("sim")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    # —— 种子工具 ——
    def _write_config(self, payload=None):
        (self.home / "trading-platform.json").write_text(
            json.dumps(payload if payload is not None else _config(), ensure_ascii=False),
            encoding="utf-8")

    def _set_mode(self, mode):
        (self.home / "trading-account-mode").write_text(mode, encoding="utf-8")

    def _plan(self, as_of=PREV, mode="sim", origin="auto", status="frozen",
              market="SH", plan_id="PLN-1", content_hash="h1"):
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
            "status,created_at,origin,market) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (plan_id, as_of, mode, "rsi", "{}", content_hash, status,
             f"{as_of} 16:20:00", origin, market))
        self.conn.commit()
        return plan_id

    def _pending(self):
        d = self.home / "trading-commands" / "pending"
        return sorted(p.name for p in d.glob("*.json")) if d.exists() else []

    def _alerts(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT level,title,detail FROM alerts ORDER BY id")]

    def _mark(self, market="SH", day=TODAY):
        return store.kv_get(self.conn, f"auto_exec:{market}:{day}")

    def _run(self, market="SH", today=TODAY):
        return daemon.auto_execute(self.conn, str(self.home), market, today=today)

    # —— 守卫 1：总开关 ——
    def test_disabled_silently_skips(self):
        self._write_config(_config(enabled=False))
        self._plan()
        out = self._run()
        self.assertTrue(out["ok"])
        self.assertIn("未启用", out["skipped"])
        self.assertEqual(self._pending(), [])
        self.assertEqual(self._alerts(), [])       # 关闭是默认态：不告警
        self.assertIsNone(self._mark())

    def test_invalid_config_fails_closed(self):
        payload = _config()
        payload["auto_pipeline"]["enabled"] = "yes"
        self._write_config(payload)
        self._plan()
        out = self._run()
        self.assertFalse(out["ok"])
        self.assertIn("配置非法", out["error"])
        self.assertEqual([a["level"] for a in self._alerts()], ["warn"])
        self.assertEqual(self._pending(), [])

    # —— 守卫 2：模式 ——
    def test_live_mode_waits_for_human(self):
        self._set_mode("live")
        self._plan()
        out = self._run()
        self.assertTrue(out["ok"])
        self.assertIn("等待人工执行", out["skipped"])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "info")
        self.assertIn("等待人工执行", alerts[0]["title"] + alerts[0]["detail"])
        self.assertEqual(self._pending(), [])
        self.assertIsNone(self._mark())

    def test_invalid_mode_file_fails_closed(self):
        self._set_mode("weird")
        self._plan()
        out = self._run()
        self.assertFalse(out["ok"])
        self.assertEqual(self._pending(), [])
        self.assertEqual([a["level"] for a in self._alerts()], ["warn"])

    # —— 守卫 3/4：kill 与熔断 ——
    def test_kill_file_blocks(self):
        daemon.kill_path(self.home).write_text("", encoding="utf-8")
        self._plan()
        out = self._run()
        self.assertIn("kill", out["skipped"])
        self.assertEqual([a["level"] for a in self._alerts()], ["info"])
        self.assertEqual(self._pending(), [])
        self.assertIsNone(self._mark())

    def test_halt_blocks(self):
        store.set_halt(self.conn, True, reason="daily_loss")
        self._plan()
        out = self._run()
        self.assertIn("熔断", out["skipped"])
        self.assertEqual(self._pending(), [])
        self.assertIsNone(self._mark())

    # —— 守卫 5：计划存在性/来源/状态 ——
    def test_no_plan_skips(self):
        out = self._run()
        self.assertTrue(out["ok"])
        self.assertIn("无", out["skipped"])
        self.assertEqual(self._pending(), [])
        self.assertIsNone(self._mark())

    def test_manual_plan_never_auto_executed(self):
        self._plan(origin="manual")
        out = self._run()
        self.assertIn("无", out["skipped"])
        self.assertEqual(self._pending(), [])

    def test_live_plan_never_auto_executed(self):
        # 加固：计划自身 mode=live（live 期生成），模式文件已切回 sim —— 仍不得自动执行
        self._plan(mode="live")
        out = self._run()
        self.assertTrue(out["ok"])
        self.assertIn("live", out["skipped"])
        self.assertEqual(self._pending(), [])
        self.assertIsNone(self._mark())

    def test_stale_plan_rejected(self):
        self._plan(as_of=STALE)
        out = self._run()
        self.assertIn("无", out["skipped"])
        self.assertEqual(self._pending(), [])

    def test_unfrozen_plan_rejected(self):
        self._plan(status="executing")
        out = self._run()
        self.assertIn("无", out["skipped"])
        self.assertEqual(self._pending(), [])

    # —— 守卫 6：市场切片 ——
    def test_other_market_plan_not_used(self):
        store.upsert_calendar(self.conn, "HK", [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in DAYS])
        self._plan(market="HK")
        out = self._run(market="SH")
        self.assertIn("无", out["skipped"])
        self.assertEqual(self._pending(), [])

    # —— 正常路径（计划 as_of = 上一交易日）——
    def test_prev_day_plan_writes_command_and_mark(self):
        self._plan(as_of=PREV, content_hash="hash-abc")
        out = self._run()
        self.assertTrue(out["ok"])
        self.assertEqual(out["plan"]["content_hash"], "hash-abc")
        self.assertEqual(out["as_of"], PREV)
        pending = self._pending()
        self.assertEqual(len(pending), 1)
        body = json.loads((self.home / "trading-commands" / "pending" / pending[0])
                          .read_text(encoding="utf-8"))
        self.assertEqual(body["type"], "execute_plan")
        self.assertEqual(body["plan_hash"], "hash-abc")
        self.assertEqual(body["expected_mode"], "sim")
        self.assertEqual(body["nonce"], out["nonce"])
        mark = self._mark()
        self.assertIsNotNone(mark)
        self.assertEqual(mark["plan_id"], "PLN-1")

    def test_today_plan_accepted(self):
        # 跨市场日期空间容差：日历按市场本地日期落库、作业按北京时间触发，美股链条下
        # 「最近已收盘交易日」在两种解释下分别落在最近两个交易日内 —— 今日计划照样接受
        self._plan(as_of=TODAY)
        out = self._run()
        self.assertTrue(out["ok"])
        self.assertIn("nonce", out)
        self.assertEqual(len(self._pending()), 1)

    # —— 守卫 7：当日幂等 ——
    def test_same_day_is_idempotent(self):
        self._plan(as_of=PREV)
        self._run()
        out = self._run()
        self.assertTrue(out["ok"])
        self.assertIn("已执行", out["skipped"])
        self.assertEqual(len(self._pending()), 1)   # 不产生第二条
        self.assertEqual([a["level"] for a in self._alerts()], ["info"])

    # —— 与人工路径同构：指令被既有 handle_command 消费 ——
    def test_command_consumed_by_handle_command(self):
        self._plan(as_of=PREV, content_hash="hash-abc")
        order = oms.register_order(self.conn, "PLN-1", "SH.600519", "SH", "BUY",
                                   100, 100.0, "sim", "hash-abc")
        oms.transition(self.conn, order["client_order_id"], "frozen")
        self._run()
        seen_broker = []

        def fake_broker(name, args, timeout=30):
            seen_broker.append((name, args))
            return {"order_id": "1"}

        out = commands.poll(str(self.home), lambda cmd: daemon._execute_plan(
            self.conn, str(self.home), cmd, broker_call=fake_broker,
            equity=1_000_000.0, today=TODAY, calendar_ok=True))
        self.assertTrue(out[0]["result"]["ok"])
        statuses = {r["status"] for r in store.get_orders_by_plan(self.conn, "PLN-1")}
        self.assertEqual(statuses, {"submitted"})
        self.assertEqual(self._pending(), [])       # 已移入 processed/
        self.assertTrue(seen_broker)                # 确实触达券商（且仅经既有窄门）

    # —— 边界：日历缺失 ——
    def test_calendar_missing_skips(self):
        conn2 = store.connect(":memory:")           # 无日历的空库
        store.migrate(conn2)
        self._plan()                                # 计划写原库，这里换空库跑
        out = daemon.auto_execute(conn2, str(self.home), "SH", today=TODAY)
        conn2.close()
        self.assertTrue(out["ok"])
        self.assertIn("日历", out["skipped"])
        self.assertEqual(self._pending(), [])

    # —— CLI 退出码：软跳过=0，fail-closed=1 ——
    def test_cli_exit_codes(self):
        self._plan(as_of=PREV)
        buf = io.StringIO()
        # CLI 的库由 --db 解析（--home 只用于配置文件），与既有 CLI 测试同口径
        with contextlib.redirect_stdout(buf):
            code = cli.main(["auto-execute", "--market", "SH", "--home", str(self.home),
                             "--db", str(store.db_path(str(self.home))), "--today", TODAY])
        self.assertEqual(code, 0)
        self.assertEqual(len(self._pending()), 1)

        self._write_config({"auto_pipeline": {"enabled": "yes"}})
        with contextlib.redirect_stdout(buf):
            code = cli.main(["auto-execute", "--market", "SH", "--home", str(self.home),
                             "--db", str(store.db_path(str(self.home))), "--today", TODAY])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
