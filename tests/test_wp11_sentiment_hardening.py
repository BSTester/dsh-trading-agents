"""WP11 实机加固（S-1/S-2/S-3，2026-09-17 最后一轮 e2e 缺口）。

实机证据（协调者亲自跑）：20 标的 × 三源 **>7 分钟无输出**（对外像挂死、实际静默推进）、
多次重启仍卡在同一标的、超时杀父进程后 **fin_sentiment/浏览器子进程继续存活**（孤儿泄漏）、
而服务对作业有 900s 上限——三者共同作用会让情绪阶段**每天被判失败**（数据实际采不到）。

本文件覆盖任务清单的 ①–⑥：

  * ① 总预算耗尽 → 处理一部分、其余记 ``skipped_symbols(budget)``、摘要含
    ``budget_exhausted``、warn 告警、**退出码 0**（跑了但没采完 ≠ 失败）；
  * ② 逐标的超时 → 该标的 failed(超时)、其余继续、**超时值来自配置/参数**；
  * ③ 子进程清理 → 真进程组 SIGKILL（超时路径）+ SIGTERM 信号守卫（端到端真跑子进程）；
  * ④ ``--limit``/``--symbols``/``--budget`` 生效与非法值 fail-closed；
  * ⑤ pipeline 的预算耗尽内容结局（阶段 skipped + 摘要「当日未采完：预算耗尽」）；
  * ⑥ 既有三源落库/缺席降级/全失败不阻塞的回归（沿用 test_wp11_sentiment_job.py）。

进度可见性（S-3）：进度行写 **stderr**，故本文件断言 stderr 有逐标的进度、而 stdout 的
作业摘要仍可被 ``daemon._last_json_object`` 解析（两者不互相破坏）。
"""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "plugins" / "core" / "python"
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(CORE))

from trading_core import alerts, cli, daemon, pipeline, sentiment, store  # noqa: E402

TODAY = "2026-09-16"
SYMBOLS = ["SH.600519", "SH.600000", "SH.601318"]


class _FakeClock:
    """可注入单调时钟：**只在 runner 真的跑一次时才推进**（模拟真实耗时），
    另可设初始偏移（模拟「开工时预算已被前面的源吃掉」）。预算逻辑因此不真等。"""

    def __init__(self, cost_per_call=1.0, initial=0.0):
        self.now = float(initial)
        self.cost_per_call = float(cost_per_call)

    def __call__(self):
        return self.now

    def advance(self, seconds=None):
        self.now += self.cost_per_call if seconds is None else seconds


class _Runner:
    """假子进程执行体：可按标的抛超时/失败，记录每次收到的 timeout。"""

    def __init__(self, fail_symbols=(), timeout_symbols=(), clock=None):
        self.fail_symbols = set(fail_symbols)
        self.timeout_symbols = set(timeout_symbols)
        self.clock = clock            # 有则每次调用推进它（预算裁剪因此可确定性验证）
        self.calls = []
        self.timeouts = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        self.timeouts.append(timeout)
        if self.clock is not None:
            self.clock.advance()
        symbol = next((part for part in cmd if str(part).startswith(("SH.", "SZ.", "HK.", "US."))),
                      "")
        if symbol in self.timeout_symbols:
            raise TimeoutError(f"超时（>{timeout}s）已回收进程组 0")
        if symbol in self.fail_symbols:
            raise RuntimeError(f"通道失败：{symbol}")
        return json.dumps({"ticker": symbol}, ensure_ascii=False)

    def symbols_called(self):
        out = []
        for cmd in self.calls:
            out.append(next((str(p) for p in cmd
                             if str(p).startswith(("SH.", "SZ.", "HK.", "US."))), ""))
        return out


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        base = self.home / "trading-python" / "fin-data"
        base.mkdir(parents=True, exist_ok=True)
        for name in ("fin_sentiment.py", "fin_news.py"):
            (base / name).write_text("# stub\n", encoding="utf-8")
        self.stream = io.StringIO()          # 进度流（S-3）

    def _config(self, symbols=None, **extra):
        cfg = {"watchlist": SYMBOLS if symbols is None else symbols}
        cfg.update(extra)
        (self.home / "trading-platform.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _calendar(self, market="SH", days=(TODAY,)):
        store.upsert_calendar(self.conn, market, [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in days])

    def _titles(self, level=None):
        sql = "SELECT level,title FROM alerts" + (" WHERE level=?" if level else "")
        rows = self.conn.execute(sql, (level,) if level else ()).fetchall()
        return [r["title"] for r in rows]

    def _run(self, runner, **kwargs):
        self._calendar()
        self._config(**kwargs.pop("config", {}))
        return sentiment.run(str(self.home), "SH", conn=self.conn, runner=runner,
                             today=TODAY, progress_stream=self.stream, **kwargs)


# ---------------------------------------------------------------------------
# ① 总预算与优雅收尾（S-1）
# ---------------------------------------------------------------------------
class BudgetTest(_Base):
    """S-1 硬上界：每次调用前按剩余预算裁剪超时；剩余 ≤ 0 不再发起新调用。

    时钟按「runner 真的跑了一次」推进（cost_per_call），因此断言的是**真实语义**：
    预算由实际耗时消耗，而不是由读时钟的次数消耗。
    """

    def test_budget_exhausts_and_skips_remaining(self):
        """3 标的、预算只够 1 个（每源耗 1s、在场 2 源）→ 处理 1、其余 skipped(budget)。"""
        clock = _FakeClock(cost_per_call=1.0)
        runner = _Runner(clock=clock)
        result = self._run(runner, budget_seconds=2, monotonic=clock)

        self.assertTrue(result["ok"])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["ok_symbols"], 1)
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual([item["symbol"] for item in result["skipped_symbols"]],
                         SYMBOLS[1:])
        self.assertEqual({item["reason"] for item in result["skipped_symbols"]}, {"budget"})
        # 预算内只处理了首个标的（在场源各一次）
        self.assertEqual(set(runner.symbols_called()), {SYMBOLS[0]})
        self.assertIn(sentiment.BUDGET_ALERT_TITLE, self._titles("warn"))
        self.assertIn("saved", result)
        self.assertIn("elapsed_seconds", result)

    def test_budget_alert_detail_carries_counts_and_market(self):
        """告警 detail 必带 market=XX（pipeline 归因）与已处理/剩余数量（运维可读）。"""
        clock = _FakeClock(cost_per_call=1.0)
        self._run(_Runner(clock=clock), budget_seconds=2, monotonic=clock)
        row = self.conn.execute(
            "SELECT detail FROM alerts WHERE title=?", (sentiment.BUDGET_ALERT_TITLE,)
        ).fetchone()
        self.assertIn("market=SH", row["detail"])
        self.assertIn("1/3", row["detail"])
        self.assertIn("剩余 2", row["detail"])

    def test_tiny_budget_bounds_round_to_a_single_call(self):
        """极小预算（1s）也把整轮限制在**一次被裁剪的调用**内：不会再发起第二次。

        备注：``started`` 与首次检查同一时刻，故「开工前预算就被吃掉」在真实时钟下不可达
        ——预算耗尽只可能发生在**调用之间**（这条与下一条一起覆盖硬上界的两个分支）。
        """
        clock = _FakeClock(cost_per_call=1.0)
        runner = _Runner(clock=clock)
        result = self._run(runner, budget_seconds=1, per_symbol_timeout=90, monotonic=clock)

        self.assertTrue(result["ok"])
        self.assertEqual(runner.timeouts, [1], "唯一那次调用必须被裁到剩余预算 1s")
        self.assertEqual(result["processed"], 1)
        self.assertEqual([item["symbol"] for item in result["skipped_symbols"]],
                         SYMBOLS[1:])
        self.assertIn(sentiment.BUDGET_ALERT_TITLE, self._titles("warn"))

    def test_per_call_timeout_is_capped_by_remaining_budget(self):
        """**硬上界核心**：单次调用超时被剩余预算裁剪（不是恒为 symbol_timeout）。"""
        clock = _FakeClock(cost_per_call=2.0)
        runner = _Runner(clock=clock)
        result = self._run(runner, budget_seconds=3, per_symbol_timeout=90,
                           config={"watchlist": SYMBOLS[:1]}, monotonic=clock)

        # 标的 1：源 1 剩余 3s → timeout 3；跑掉 2s 后源 2 剩余 1s → timeout 1
        self.assertEqual(runner.timeouts, [3, 1])
        self.assertEqual(result["processed"], 1)
        # 只裁剪了最后一次调用的超时、**没有跳过任何调用** → 不标 exhausted（诚实语义：
        # exhausted 表示「有东西没采到」，不是「预算被用满了」）
        self.assertFalse(result["budget_exhausted"])
        self.assertEqual(result["skipped_symbols"], [])
        self.assertNotIn(sentiment.BUDGET_ALERT_TITLE, self._titles("warn"))

    def test_non_positive_remaining_stops_new_calls(self):
        """剩余 ≤ 0 时**不再发起**任何新调用（含同一标的的剩余源）。"""
        clock = _FakeClock(cost_per_call=5.0)
        runner = _Runner(clock=clock)
        result = self._run(runner, budget_seconds=2, per_symbol_timeout=90, monotonic=clock)

        self.assertEqual(len(runner.calls), 1, "剩余预算为负后不得再发起调用")
        self.assertEqual(runner.timeouts, [2])
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual([item["symbol"] for item in result["skipped_symbols"]],
                         SYMBOLS[1:])

    def test_per_call_timeout_never_below_one_second(self):
        """裁剪结果不得小于 1s（`max(1, …)`）——否则会把在跑的调用瞬间掐死。"""
        clock = _FakeClock(cost_per_call=0.1, initial=0.6)
        runner = _Runner(clock=clock)
        self._run(runner, budget_seconds=1, per_symbol_timeout=90,
                  config={"watchlist": SYMBOLS[:1]}, monotonic=clock)

        self.assertTrue(runner.timeouts, "至少应发起一次调用")
        self.assertEqual(min(runner.timeouts), 1)

    def test_no_budget_alert_when_collection_completes(self):
        """预算充足 → 无预算告警、无 skipped_symbols、budget_exhausted=False（反证）。"""
        clock = _FakeClock(cost_per_call=1.0)
        runner = _Runner(clock=clock)
        result = self._run(runner, budget_seconds=100, monotonic=clock)

        self.assertFalse(result["budget_exhausted"])
        self.assertEqual(result["skipped_symbols"], [])
        self.assertEqual(result["processed"], len(SYMBOLS))
        self.assertNotIn(sentiment.BUDGET_ALERT_TITLE, self._titles("warn"))

    def test_budget_config_read_and_default(self):
        """配置键生效；缺省用 DEFAULT_BUDGET_SECONDS。"""
        self._config(sentiment_budget_seconds=123,
                     sentiment_symbol_timeout_seconds=7)
        self.assertEqual(sentiment.config(str(self.home)),
                         {"budget_seconds": 123, "symbol_timeout_seconds": 7})
        (self.home / "trading-platform.json").unlink()
        self.assertEqual(sentiment.config(str(self.home)),
                         {"budget_seconds": sentiment.DEFAULT_BUDGET_SECONDS,
                          "symbol_timeout_seconds": sentiment.PER_CALL_TIMEOUT})

    def test_invalid_config_is_fail_closed(self):
        """非法配置 fail-closed 信封（绝不静默用默认值——预算被忽略会重新撞 900s 上限）。"""
        for bad in (0, -5, "600", True, 1.5):
            with self.subTest(value=bad):
                self._config(sentiment_budget_seconds=bad)
                self._calendar()
                result = sentiment.run(str(self.home), "SH", conn=self.conn,
                                       runner=_Runner(), today=TODAY,
                                       progress_stream=self.stream)
                self.assertFalse(result["ok"])
                self.assertIn("sentiment_budget_seconds", result["error"])

    def test_budget_plus_worst_overrun_must_fit_job_limit(self):
        """冗余安全界（预算 + 源数×单标的超时）必须留在作业上限内，否则配置期拒绝。

        硬上界生效后真实上界是「预算 + 1s」；这条守的是**裁剪逻辑退化**时的兜底，
        故保留更保守的判据（见 sentiment._budget_guard docstring）。
        """
        self._config(sentiment_budget_seconds=700)   # 700 + 3×90 = 970 ≥ 900
        self._calendar()
        result = sentiment.run(str(self.home), "SH", conn=self.conn,
                               runner=_Runner(), today=TODAY,
                               progress_stream=self.stream)
        self.assertFalse(result["ok"])
        self.assertIn("作业上限", result["error"])


# ---------------------------------------------------------------------------
# ② 逐标的超时（S-2）
# ---------------------------------------------------------------------------
class SymbolTimeoutTest(_Base):
    def test_timeout_marks_symbol_failed_and_continues(self):
        """超时只判该标的 failed（原因含「超时」），其余标的照常采集。"""
        runner = _Runner(timeout_symbols={SYMBOLS[1]})
        result = self._run(runner, per_symbol_timeout=7)

        self.assertTrue(result["ok"])
        timed_out = [item for item in result["failed"]
                     if item["symbol"] == SYMBOLS[1]]
        self.assertTrue(timed_out, "超时标的必须进 failed")
        self.assertIn("超时", timed_out[0]["error"])
        # 后续标的继续（SYMBOLS[2] 被采集到）
        self.assertIn(SYMBOLS[2], runner.symbols_called())
        self.assertGreater(result["saved"], 0)

    def test_configured_timeout_is_passed_to_runner(self):
        """超时值来自参数/配置（不是写死的常量）——否则配置形同虚设。"""
        runner = _Runner()
        self._run(runner, per_symbol_timeout=7)
        self.assertTrue(runner.timeouts)
        self.assertEqual(set(runner.timeouts), {7})

        runner2 = _Runner()
        self._run(runner2, config={"sentiment_symbol_timeout_seconds": 11})
        self.assertEqual(set(runner2.timeouts), {11})

    def test_all_symbols_timed_out_is_not_blocking(self):
        """全部超时 → 仍 ok=True（退出 0）+ 全部失败告警（不拖垮调度链）。"""
        runner = _Runner(timeout_symbols=set(SYMBOLS))
        result = self._run(runner, per_symbol_timeout=5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["saved"], 0)
        # 失败按「单源单标的」记账（在场源数 × 标的数），故断言标的集合而非条目数
        self.assertEqual({item["symbol"] for item in result["failed"]}, set(SYMBOLS))
        self.assertEqual(result["ok_symbols"], 0)
        self.assertIn("情绪快照全部失败", self._titles("warn"))


# ---------------------------------------------------------------------------
# ③ 子进程清理（S-2）：真进程组
# ---------------------------------------------------------------------------
_SLEEPER = ("import os,sys,time\n"
            "open(sys.argv[1],'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n")


def _wait_for_file(path, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists() and Path(path).read_text().strip():
            return Path(path).read_text().strip()
        time.sleep(0.05)
    return ""


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


class ProcessCleanupTest(_Base):
    """真起子进程验证回收——假 runner 测不出「孤儿泄漏」（实机观察到的正是这个）。"""

    def test_timeout_kills_process_group(self):
        """超时 → 整组 SIGKILL：子进程（及其进程组）必须消失。"""
        pidfile = self.home / "sleeper.pid"
        cmd = [sys.executable, "-c", _SLEEPER, str(pidfile)]
        started = time.time()
        with self.assertRaises(TimeoutError) as ctx:
            sentiment._default_runner(cmd, timeout=1)
        self.assertIn("超时", str(ctx.exception))
        self.assertLess(time.time() - started, 8, "超时应在 1s 量级返回，不挂死")

        pid = _wait_for_file(pidfile, timeout=3)
        self.assertTrue(pid, "子进程应先写出自己的 pid")
        deadline = time.time() + 5
        while time.time() < deadline and _alive(pid):
            time.sleep(0.1)
        self.assertFalse(_alive(pid), f"子进程 {pid} 应在超时后被回收")
        self.assertEqual(sentiment._LIVE_PGIDS, set(), "受管进程组登记必须清空")

    def test_successful_run_leaves_no_tracked_group_and_installs_guard(self):
        """正常结束不残留登记；且 runner **确实安装了** SIGTERM 守卫（接线可证）。"""
        out = sentiment._default_runner(
            [sys.executable, "-c", "print('{\"ok\": true}')"], timeout=10)
        self.assertIn("ok", out)
        self.assertEqual(sentiment._LIVE_PGIDS, set())
        self.assertTrue(sentiment._SIGNAL_GUARD_INSTALLED,
                        "真实子进程执行体必须安装守卫，否则 SIGTERM 会留孤儿")

    def test_signal_guard_installs_and_kills_tracked_children(self):
        """SIGTERM 守卫：装得上；收到处理函数时回收受管子进程组。"""
        sentiment._install_signal_guard()
        self.assertIsNot(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

        pidfile = self.home / "guarded.pid"
        proc = subprocess.Popen([sys.executable, "-c", _SLEEPER, str(pidfile)],
                                start_new_session=True)
        try:
            pid = _wait_for_file(pidfile, timeout=5)
            self.assertTrue(pid)
            sentiment._LIVE_PGIDS.add(os.getpgid(proc.pid))
            sentiment._kill_live_children()
            # 必须 wait 回收：SIGKILL 后未回收的是僵尸，os.kill(pid,0) 对僵尸仍成功
            self.assertEqual(proc.wait(timeout=5), -signal.SIGKILL,
                             "受管子进程组必须被守卫 SIGKILL 回收")
            self.assertEqual(sentiment._LIVE_PGIDS, set())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_sigterm_end_to_end_cleans_grandchild(self):
        """端到端（真进程）：父进程被 SIGTERM 时，其子进程组必须一起消失。

        为什么必须真跑：SIGTERM 的默认动作直接终止进程、``finally`` 不执行——实机正是
        这样留下 fin_sentiment/浏览器孤儿。这里用一个真实子进程装守卫、起一个 sleeper，
        然后由测试进程发 SIGTERM，断言 sleeper 与父进程都退出。
        """
        sleeper_pid_file = self.home / "grand.pid"
        ready_file = self.home / "ready"
        program = (
            "import sys, threading, time\n"
            f"sys.path.insert(0, {str(CORE)!r})\n"
            "from trading_core import sentiment\n"
            "sentiment._install_signal_guard()\n"
            f"pidfile = {str(sleeper_pid_file)!r}\n"
            f"ready = {str(ready_file)!r}\n"
            "def child():\n"
            "    sentiment._default_runner([sys.executable, '-c', "
            f"{_SLEEPER!r}, pidfile], timeout=60)\n"
            "threading.Thread(target=child, daemon=True).start()\n"
            "open(ready,'w').write('1')\n"
            "time.sleep(60)\n"
        )
        # 就绪余量刻意放宽：全量套件满载（2100+ 用例）时，子进程「导入 trading_core →
        # 装守卫 → 起线程 → 起孙进程」实测可超过 10s（曾因此在全量跑里假红一次）。
        # stderr 落文件而非 DEVNULL：失败时能把子进程的真实报错贴进断言消息。
        log_path = self.home / "sigterm_child.log"
        with log_path.open("w", encoding="utf-8") as log:
            parent = subprocess.Popen([sys.executable, "-c", program],
                                      stdout=log, stderr=subprocess.STDOUT)
            try:
                self.assertTrue(
                    _wait_for_file(ready_file, timeout=30),
                    f"子进程应就绪（30s 内）；child 输出：{log_path.read_text()[:400]!r}")
                grandpid = _wait_for_file(sleeper_pid_file, timeout=30)
                self.assertTrue(grandpid, "孙进程应先写出自己的 pid")
                parent.send_signal(signal.SIGTERM)
                self.assertIsNotNone(parent.wait(timeout=15), "父进程应被 SIGTERM 终止")
                deadline = time.time() + 10
                while time.time() < deadline and _alive(grandpid):
                    time.sleep(0.1)
                self.assertFalse(_alive(grandpid), f"孙进程 {grandpid} 必须被一起回收")
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait()
                stale = _wait_for_file(sleeper_pid_file, timeout=0.5)
                if stale and _alive(stale):
                    with contextlib.suppress(ProcessLookupError, ValueError):
                        os.kill(int(stale), signal.SIGKILL)


# ---------------------------------------------------------------------------
# ④ CLI 覆盖与非法值（S-1/S-3 运维面）
# ---------------------------------------------------------------------------
class CliTest(_Base):
    def _cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _base_args(self):
        return ["sentiment-snapshot", "--market", "SH", "--home", str(self.home),
                "--today", TODAY, "--db", str(self.home / "t.sqlite")]

    def test_symbols_and_limit_and_budget_honored(self):
        """--symbols/--limit/--budget 生效：只采指定标的、预算耗尽仍退出 0。"""
        self._calendar()
        self._config()
        code, out, err = self._cli(self._base_args()
                                   + ["--symbols", SYMBOLS[0], "--limit", "1",
                                      "--budget", "100"])
        self.assertEqual(code, 0, out)
        payload = daemon._last_json_object(out)
        self.assertTrue(payload and payload["ok"], out)
        self.assertEqual(payload["symbols"], [SYMBOLS[0]])

    def test_limit_rejects_non_positive(self):
        """--limit 非法 → fail-closed 非零退出（不静默忽略）。"""
        self._calendar()
        self._config()
        code, out, _ = self._cli(self._base_args() + ["--limit", "0"])
        self.assertEqual(code, 1)
        self.assertIn("limit", daemon._last_json_object(out)["error"])

    def test_budget_rejects_non_positive(self):
        """--budget 非法 → 配置层 fail-closed。"""
        self._calendar()
        self._config()
        code, out, _ = self._cli(self._base_args() + ["--budget", "0"])
        self.assertEqual(code, 1)
        self.assertIn("budget_seconds", daemon._last_json_object(out)["error"])

    def test_progress_on_stderr_and_summary_still_parseable(self):
        """S-3：进度行进 stderr，stdout 的作业摘要仍可被 daemon 解析（互不破坏）。"""
        self._calendar()
        self._config(symbols=SYMBOLS[:2])
        # CLI 内部走真实 runner：把引擎脚本换成会立刻输出 JSON 的假脚本
        base = self.home / "trading-python" / "fin-data"
        for name in ("fin_sentiment.py", "fin_news.py"):
            (base / name).write_text("import json;print(json.dumps({'ticker':'x'}))\n",
                                     encoding="utf-8")
        code, out, err = self._cli(self._base_args() + ["--budget", "100"])
        self.assertEqual(code, 0, out + err)
        payload = daemon._last_json_object(out)
        self.assertIsNotNone(payload, f"stdout 必须仍是可解析的作业摘要：{out!r}")
        self.assertTrue(payload["ok"])
        self.assertIn("[sentiment] 1/2", err, "进度必须逐标的输出到 stderr")
        self.assertNotIn("[sentiment]", out, "进度不得污染 stdout 摘要")


# ---------------------------------------------------------------------------
# ⑤ pipeline 内容结局
# ---------------------------------------------------------------------------
class PipelineOutcomeTest(_Base):
    def _senti_stage(self, ran=False):
        """→ SH 情绪阶段（告警按**真实当日**落库：pipeline 只读当日告警）。"""
        from trading_core import clock
        date = clock.now_stamp()[:10]
        alerts.emit(self.conn, home=str(self.home), level="warn",
                    title=sentiment.BUDGET_ALERT_TITLE,
                    detail="market=SH 已处理 1/3 个标的（预算 600s 耗尽），剩余 2 个未采集")
        if ran:  # 「作业跑完了」：ran 标记让阶段走内容结局路径（而非未跑完归因）
            store.kv_set(self.conn, "daemon:state",
                         {"ran": {f"SH:sentiment_snapshot:{date}": f"{date} 19:05:00"}})
        value = pipeline.pipeline_snapshot(self.conn, str(self.home), date=date)
        return value["markets"]["SH"]["stages"]["sentiment_snapshot"]

    def test_budget_exhausted_after_run_is_skipped_with_summary(self):
        """**作业跑完后**的内容结局：阶段 skipped + 摘要「当日未采完：预算耗尽」。"""
        stage = self._senti_stage(ran=True)
        self.assertEqual(stage["status"], "skipped")
        self.assertIn("预算耗尽", stage["summary"])

    def test_budget_alert_attributes_skipped_when_job_did_not_finish(self):
        """**作业没跑完**（无 ran 标记）：告警归因同样给 skipped（不是绿色 pending/ok）。"""
        stage = self._senti_stage(ran=False)
        self.assertEqual(stage["status"], "skipped")
        self.assertEqual(stage["summary"], sentiment.BUDGET_ALERT_TITLE)

    def test_budget_title_in_pipeline_maps(self):
        """标题必须同时进两张映射表（状态归因 + 内容结局），防只加一处。"""
        self.assertIn(sentiment.BUDGET_ALERT_TITLE, pipeline._CONTENT_OUTCOMES)
        self.assertIn(sentiment.BUDGET_ALERT_TITLE,
                      {title: job for title, (job, _status) in
                       [(t, (v[0], v[1])) for t, v in pipeline._ALERT_STATUS.items()]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
