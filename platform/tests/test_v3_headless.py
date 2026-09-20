"""``server/v3_headless.py`` 契约测试（FR-GATEWAY-003/004、§5.2.3/§7.2/附录 B、FR-MON-003）。

**全部离线**：外部进程用替身（``FakeSpawner``）除了三处**故意用真子进程**的用例
（stdout/stderr 分离、超时 kill、token 预算 kill）——它们用的是本机 ``sys.executable``
打印几行字/睡一小会儿，不打网络、不装 rpofile、不依赖 8397 服务。真机 ``dsh`` 用例
（``RealDshProbeTests``）默认 skip，见类文档。

覆盖面对任务书逐条：

  * FR-GATEWAY-003：退出码映射（0/1/130/None/信号）、stdout 与 stderr **分管道**、
    超时 kill 且**不写成成功**、``argv`` 里 prompt 是独立参数；
  * 外部熔断：并发上限 3 + **排队**（不是丢弃）、单次超时、token 预算（估算口径标注）；
  * §5.2.3/附录 B：三类提示词模板**逐字**（与规格同一份常量）＋ 缺上下文报错 ＋
    持仓/风控阈值/市场摘要打包进提示词；
  * FR-GATEWAY-004：定时（交易日/非交易日/节假日/当天只触发一次）、事件（四条，
    真实数据来源 + ``no-data``）、流水线节点（两条）、开关、下一次触发时间、
    ``GET /api/v3/headless/schedule``；
  * FR-MON-003：``headless_log`` 每列齐全（prompt/stdout/stderr/exit_code/duration_ms/
    tokens_estimate/success）＋ ``GET /api/v3/headless/log`` 分页/筛选 ＋
    ``v3_ops`` 的 ``headless.last`` 读**真实表**（不再是硬编码 ``[]``）；
  * 白名单：仓库材料与 ``deny_tool_names()`` 一致、MCP 工具面里**没有漏网的写/交易工具**、
    校验不过就**不起进程**（fail-closed）。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_headless -v``
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import mcp_tools, v3_db, v3_headless, v3_ops  # noqa: E402

#: 2026-09-23 是周三（09:00 +08:00 时三个定时点里 08:30 已到）；09-26 是周六。
WEDNESDAY_0900 = datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc)
SATURDAY_0900 = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)


class FakeSpawner:
    """子进程替身：返回预置 :class:`v3_headless.ProcessResult`，并记录 argv。"""

    def __init__(self, result=None, results=None):
        self.result = result or v3_headless.ProcessResult(stdout="OK\n", exit_code=0,
                                                          duration_ms=12.5)
        self.results = list(results or [])
        self.calls = []
        self.lock = threading.Lock()

    def run(self, argv, *, timeout, token_budget, env, cwd):
        with self.lock:
            self.calls.append({"argv": list(argv), "timeout": timeout,
                               "tokenBudget": token_budget, "env": dict(env), "cwd": cwd})
        if self.results:
            return self.results.pop(0)
        return self.result


class GatedSpawner(FakeSpawner):
    """并发用例用：进 run 就计数并卡在闸门上，直到用例放行。"""

    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.gate = threading.Event()

    def run(self, argv, *, timeout, token_budget, env, cwd):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append({"argv": list(argv), "timeout": timeout,
                               "tokenBudget": token_budget, "env": dict(env), "cwd": cwd})
        self.gate.wait(10)
        with self.lock:
            self.active -= 1
        return v3_headless.ProcessResult(stdout="OK\n", exit_code=0, duration_ms=1.0)


def make_config(**overrides):
    config = json.loads(json.dumps(v3_headless.DEFAULT_CONFIG))
    config.update(overrides)
    return config


def make_home(prefix="v3-headless-"):
    home = Path(tempfile.mkdtemp(prefix=prefix))
    v3_db.init_db(home)
    return home


def install_profile(home, profile="quant-headless"):
    """造一个「装好了」的空 profile 目录（preflight 只看 package.json 是否存在）。"""
    directory = Path(home) / "profiles" / profile
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text("{}", encoding="utf-8")
    return directory


def make_runner(home, *, config=None, spawner=None, v3_run=None, **overrides):
    """建一个 preflight 能过的 runner（默认关白名单校验，专测执行语义）。"""
    base = make_config(requireWhitelist=False)
    base.update(overrides)
    if config:
        base.update(config)
    return v3_headless.HeadlessRunner(
        str(home), config=base, spawner=spawner or FakeSpawner(), v3_run=v3_run,
        env={"QUANT_HEADLESS_DSH_BIN": "/bin/true"})


class FakeWorkbench:
    """记录型 ``v3_run`` 替身（只读工具面）。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        return self.values.get(name, {"ok": False, "error": {
            "code": "v3/unknown-tool", "message": f"未知工具 {name}"}})


# ---------------------------------------------------------------------------
# 1. FR-GATEWAY-003：退出码契约
# ---------------------------------------------------------------------------
class ExitCodeContractTests(unittest.TestCase):
    def test_exit_code_mapping(self):
        self.assertEqual(v3_headless.map_exit_code(0), "completed")
        self.assertEqual(v3_headless.map_exit_code(1), "incomplete")
        self.assertEqual(v3_headless.map_exit_code(130), "sigint")
        # 没能起进程（Popen 失败）与其它非契约码都归运行器失败
        self.assertEqual(v3_headless.map_exit_code(None), "runner-error")
        self.assertEqual(v3_headless.map_exit_code(2), "runner-error")
        self.assertEqual(v3_headless.map_exit_code(124), "runner-error")
        # 被信号杀（killpg 后 wait() 给 -signum）是「killed」，不是任何一种成功/未完成
        self.assertEqual(v3_headless.map_exit_code(-9), "killed")

    def test_contract_constant_codes(self):
        self.assertEqual((v3_headless.EXIT_COMPLETED, v3_headless.EXIT_INCOMPLETE,
                          v3_headless.EXIT_SIGINT), (0, 1, 130))

    def test_exit_zero_records_contract_limitation(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        runner = make_runner(home)
        record = runner.execute("任务", task_type="pre_market_scan",
                                trigger="slot:pre_market_scan")
        self.assertEqual(record["success"], 1)
        self.assertEqual(record["outcome"], "completed")
        self.assertIn("不证明所请求的文件", record["contract_note"])

    def test_incomplete_and_sigint_are_not_success(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        spawner = FakeSpawner(results=[
            v3_headless.ProcessResult(stdout="", stderr="失败诊断", exit_code=1,
                                      duration_ms=3.0),
            v3_headless.ProcessResult(stdout="", stderr="收到 SIGINT", exit_code=130,
                                      duration_ms=3.0)])
        runner = make_runner(home, spawner=spawner)
        first = runner.execute("a", task_type="risk_review", trigger="manual")
        second = runner.execute("b", task_type="risk_review", trigger="manual")
        self.assertEqual((first["outcome"], first["success"]), ("incomplete", 0))
        self.assertEqual((second["outcome"], second["success"]), ("sigint", 0))
        self.assertEqual(second["stderr"], "收到 SIGINT")

    def test_spawn_error_recorded_as_runner_error(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        spawner = FakeSpawner(result=v3_headless.ProcessResult(exit_code=None,
                                                               spawn_error="No such file"))
        runner = make_runner(home, spawner=spawner)
        record = runner.execute("a", task_type="risk_review", trigger="manual")
        self.assertEqual(record["outcome"], "runner-error")
        self.assertEqual(record["success"], 0)
        self.assertIn("No such file", record["error"]["message"])

    def test_argv_has_prompt_as_single_argument(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        spawner = FakeSpawner()
        runner = make_runner(home, spawner=spawner, profile="quant-headless")
        prompt = "只回复 OK；提到 $HOME 与 `whoami` 也不执行"
        runner.execute(prompt, task_type="risk_review", trigger="manual")
        argv = spawner.calls[0]["argv"]
        self.assertEqual(argv[0], "/bin/true")
        self.assertEqual(argv[1:3], ["--profile", "quant-headless"])
        self.assertEqual(argv[3], prompt)
        self.assertEqual(len(argv), 4, "prompt 必须是独立一个 argv 参数（不拼 shell）")


# ---------------------------------------------------------------------------
# 2. stdout / stderr 分离（真子进程）
# ---------------------------------------------------------------------------
class StreamSeparationTests(unittest.TestCase):
    def test_real_subprocess_streams_stay_separate(self):
        spawner = v3_headless.DefaultSpawner()
        result = spawner.run(
            [sys.executable, "-c",
             "import sys; sys.stdout.write('OUT-LINE\\n'); sys.stderr.write('ERR-LINE\\n')"],
            timeout=30, token_budget=None, env=dict(os.environ), cwd=None)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "OUT-LINE\n")
        self.assertEqual(result.stderr, "ERR-LINE\n")
        self.assertNotIn("ERR-LINE", result.stdout)
        self.assertNotIn("OUT-LINE", result.stderr)

    def test_record_keeps_streams_apart(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        spawner = FakeSpawner(result=v3_headless.ProcessResult(
            stdout="答案正文\n", stderr="诊断信息\n", exit_code=0, duration_ms=8.0))
        runner = make_runner(home, spawner=spawner)
        record = runner.execute("任务", task_type="risk_review", trigger="manual")
        self.assertEqual(record["stdout"], "答案正文\n")
        self.assertEqual(record["stderr"], "诊断信息\n")
        self.assertEqual(record["streams_separated"], 1)
        rows = v3_db.list_events(home, "headless_log")
        self.assertEqual(rows[0]["stdout"], "答案正文\n")
        self.assertEqual(rows[0]["stderr"], "诊断信息\n")


# ---------------------------------------------------------------------------
# 3. 外部熔断：超时 / token 预算（真子进程）
# ---------------------------------------------------------------------------
class BreakerKillTests(unittest.TestCase):
    def test_timeout_kills_and_records_kill(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        runner = make_runner(home, spawner=v3_headless.DefaultSpawner(),
                             timeoutSeconds=0.6)
        # 真子进程睡 30s：超时熔断必须把它杀掉（不是等它自然结束）
        runner._argv = lambda prompt: [sys.executable, "-c",
                                       "import time; time.sleep(30)"]
        started = time.monotonic()
        record = runner.execute("睡太久", task_type="risk_review", trigger="manual")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 15.0, "超时必须真的把进程杀掉，不能等子进程自然结束")
        self.assertEqual(record["outcome"], "timeout")
        self.assertEqual(record["kill_reason"], "timeout")
        self.assertEqual(record["killed"], 1)
        self.assertEqual(record["success"], 0, "被 kill 的调用绝不能写成成功")
        self.assertLess(record["exit_code"], 0, "SIGKILL 后 returncode 是负的信号号")
        self.assertIn("kill", record["error"]["message"])

    def test_token_budget_kills_and_is_labelled_estimate(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        script = ("import sys, time\n"
                  "for _ in range(60):\n"
                  "    sys.stdout.write('x' * 2000)\n"
                  "    sys.stdout.flush()\n"
                  "    time.sleep(0.05)\n")
        runner = make_runner(home, spawner=v3_headless.DefaultSpawner(),
                             tokenBudget=200, timeoutSeconds=30)
        # 子进程走 sys.executable：把 dsh 换成 python 才能离线复现预算熔断
        runner._argv = lambda prompt: [sys.executable, "-c", script]
        record = runner.execute("任务", task_type="risk_review", trigger="manual")
        self.assertEqual(record["outcome"], "token-budget")
        self.assertEqual(record["kill_reason"], "token-budget")
        self.assertEqual(record["success"], 0)
        self.assertGreater(record["tokens_estimate"], 200)
        self.assertIn("估算", record["token_estimate_note"])

    def test_estimate_caliber_is_documented(self):
        tokens, detail = v3_headless.estimate_tokens("abcd", "中文")
        # ASCII 4 字符/token（=1） + 非 ASCII 2 字符（=2）
        self.assertEqual(tokens, 3)
        self.assertEqual((detail["asciiChars"], detail["wideChars"]), (4, 2))
        self.assertIn("估算", detail["note"])
        self.assertIn("估算", v3_headless.TOKEN_ESTIMATE_NOTE)
        self.assertIn("不是计量", v3_headless.TOKEN_ESTIMATE_NOTE)


# ---------------------------------------------------------------------------
# 4. 并发上限 3 + 排队
# ---------------------------------------------------------------------------
class ConcurrencyTests(unittest.TestCase):
    def test_three_parallel_and_the_rest_queue(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        spawner = GatedSpawner()
        runner = make_runner(home, spawner=spawner)
        try:
            tickets = [runner.submit(f"任务{i}", task_type="risk_review", trigger="manual")
                       for i in range(5)]
            deadline = time.monotonic() + 5
            while len(spawner.calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.2)  # 再等一会儿：确认第 4、5 个真的没有起来
            self.assertEqual(len(spawner.calls), 3, "并发上限 3：第 4/5 个必须排队")
            self.assertEqual(spawner.peak, 3)
            self.assertEqual(runner.params()["maxConcurrency"], 3)
            self.assertEqual(runner._queue.qsize(), 2, "超出上限的请求在队列里，不是被丢弃")
            self.assertFalse(any(ticket.done for ticket in tickets))
            spawner.gate.set()
            for ticket in tickets:
                record = ticket.wait(15)
                self.assertEqual(record["outcome"], "completed")
            self.assertEqual(spawner.peak, 3)
            self.assertEqual(runner._queue.qsize(), 0)
        finally:
            spawner.gate.set()
            runner.stop(2)

    def test_max_concurrency_is_configurable(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        runner = make_runner(home, maxConcurrency=1)
        self.assertEqual(runner.params()["maxConcurrency"], 1)


# ---------------------------------------------------------------------------
# 5. 提示词（§5.2.3 / 附录 B）
# ---------------------------------------------------------------------------
class PromptTemplateTests(unittest.TestCase):
    def test_templates_match_spec_verbatim(self):
        self.assertEqual(set(v3_headless.PROMPT_TEMPLATES), set(v3_headless.TASK_TYPES))
        context = {"universe": "A股新能源", "positions": "P", "risk_limits": "R",
                   "news_text": "N", "market_summary": "M"}
        self.assertEqual(
            v3_headless.build_prompt("pre_market_scan", context),
            "扫描 A股新能源 板块，评估隔夜新闻情绪影响。\n"
            "当前持仓：P\n风控阈值：R\n输出调仓建议，包含因子依据。")
        self.assertEqual(
            v3_headless.build_prompt("breaking_news", context),
            "评估以下新闻对当前持仓的影响：\nN\n当前持仓：P\n输出风险等级和应对建议。")
        self.assertEqual(
            v3_headless.build_prompt("risk_review", context),
            "复盘当前组合的风险暴露。\n持仓详情：P\n近期市场数据：M\n"
            "输出风险归因和改进建议。")

    def test_missing_context_is_an_error_not_a_blank(self):
        with self.assertRaises(ValueError) as caught:
            v3_headless.build_prompt("breaking_news", {"positions": "P"})
        self.assertIn("news_text", str(caught.exception))
        with self.assertRaises(ValueError):
            v3_headless.build_prompt("no_such_task", {})

    def test_task_prompt_appends_readonly_boundary(self):
        prompt = v3_headless.build_task_prompt("risk_review", {
            "positions": "P", "market_summary": "M"})
        self.assertTrue(prompt.startswith("复盘当前组合的风险暴露。"))
        for text in ("只读研究", "不得下单", "无数据源", "工具面移除"):
            self.assertIn(text, prompt)

    def test_context_packs_positions_limits_and_market_summary(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        workbench = FakeWorkbench({
            "positions": {"ok": True, "value": {"groups": [
                {"account": "模拟账户", "acc_id": "SIM", "positions": [
                    {"symbol": "SH.600000", "name": "浦发银行", "qty": 100,
                     "cost_price": 10.0, "price": 11.0, "market_value": 1100.0,
                     "pl_val": 100.0, "pl_ratio": 10.0}]}]}},
            "series": {"ok": True, "value": {"source": "futu", "bars": [
                {"close": 10.0}, {"close": 10.5}]}},
        })
        runner = make_runner(home, v3_run=workbench)
        context, sources = v3_headless.ContextBuilder(runner).build(
            "risk_review", slot_context={"window": "intraday"})
        self.assertIn("SH.600000", context["positions"])
        self.assertIn("单标的仓位上限 2.0%", context["risk_limits"])
        self.assertIn("SH.000300", context["market_summary"])
        self.assertIn("对账台账", context["market_summary"])
        self.assertIn("positions", sources)
        prompt = v3_headless.build_task_prompt("risk_review", context)
        for text in ("SH.600000", "SH.000300"):
            self.assertIn(text, prompt)
        # 风控阈值进的是 pre_market_scan 模板（risk_review 模板按规格没有该占位符）
        scan_context, _ = v3_headless.ContextBuilder(runner).build("pre_market_scan")
        self.assertIn("2.0%", v3_headless.build_task_prompt("pre_market_scan", scan_context))

    def test_no_data_is_written_into_the_prompt(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        runner = make_runner(home)  # 没有工具面
        context, _ = v3_headless.ContextBuilder(runner).build("pre_market_scan")
        self.assertIn("无数据源", context["positions"])
        self.assertNotIn("数量=None", context["positions"])


# ---------------------------------------------------------------------------
# 6. 调度：定时 / 事件 / 流水线
# ---------------------------------------------------------------------------
class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.home = make_home()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        install_profile(self.home)
        self.spawner = FakeSpawner()
        self.runner = make_runner(self.home, spawner=self.spawner)
        self.scheduler = v3_headless.HeadlessScheduler(self.runner)
        self.addCleanup(self.runner.stop, 2)

    def keys(self, result, status=None):
        return [item["trigger"] for item in result["checked"]
                if status is None or item["status"] == status]

    def test_weekend_does_not_trigger_any_slot(self):
        result = self.scheduler.tick_once(SATURDAY_0900)
        self.assertEqual(result["fired"], [])
        for item in result["checked"]:
            if item["kind"] == "schedule":
                self.assertEqual(item["status"], "idle")
                self.assertEqual(item["detail"]["reason"], "非交易日")

    def test_holiday_table_blocks_the_slot(self):
        (self.home / "market-holidays.json").write_text(
            json.dumps({"SH": ["2026-09-23"]}), encoding="utf-8")
        result = self.scheduler.tick_once(WEDNESDAY_0900)
        for item in result["checked"]:
            if item["kind"] == "schedule":
                self.assertEqual(item["status"], "idle")
        self.assertEqual(result["fired"], [])

    def test_trading_day_fires_due_slot_once(self):
        result = self.scheduler.tick_once(WEDNESDAY_0900)
        fired = [item["trigger"] for item in result["fired"]]
        self.assertEqual(fired, ["slot:pre_market_scan"])
        self.assertEqual(result["checked"][0]["detail"]["isTradingDay"], True)
        # 同一分钟再 tick 一次：去重键命中，不再起第二次
        again = self.scheduler.tick_once(WEDNESDAY_0900)
        self.assertEqual(again["fired"], [])
        self.assertIn("冷却", json.dumps(again["skipped"], ensure_ascii=False))
        self.runner._queue.join()
        self.assertEqual(len(self.spawner.calls), 1)
        rows = v3_db.list_events(self.home, "headless_log")
        self.assertEqual(rows[0]["trigger"], "slot:pre_market_scan")
        self.assertEqual(rows[0]["task_type"], "pre_market_scan")

    def test_switches_disable_triggers(self):
        runner = make_runner(self.home, spawner=FakeSpawner(),
                             schedule={"pre_market_scan": False, "midday_review": True,
                                       "post_close": True},
                             events={}, pipeline={})
        scheduler = v3_headless.HeadlessScheduler(runner)
        result = scheduler.tick_once(WEDNESDAY_0900)
        self.assertEqual(result["fired"], [])
        skipped = {item["trigger"] for item in result["skipped"]}
        self.assertIn("slot:pre_market_scan", skipped)
        self.assertIn("breaking_news", skipped)
        view = {item["key"]: item for item in scheduler.view(WEDNESDAY_0900)["triggers"]}
        self.assertFalse(view["slot:pre_market_scan"]["enabled"])
        self.assertTrue(view["slot:midday_review"]["enabled"])
        self.assertIn("交易日历", view["slot:midday_review"]["source"])

    def test_disabled_master_switch_does_nothing(self):
        runner = make_runner(self.home, spawner=FakeSpawner(), enabled=False)
        scheduler = v3_headless.HeadlessScheduler(runner)
        result = scheduler.tick_once(WEDNESDAY_0900)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["fired"], [])
        self.assertIn("总开关", result["reason"])

    def test_next_fire_times_are_real_calendar_times(self):
        upcoming = self.scheduler.next_fire_times(WEDNESDAY_0900)
        self.assertTrue(upcoming)
        self.assertEqual(upcoming[0]["at"], "2026-09-23T12:00:00+08:00")
        self.assertEqual([item["trigger"] for item in upcoming[:2]],
                         ["slot:midday_review", "slot:post_close"])
        # 09-24 的开盘前扫描一定在列表里（且都在未来）
        self.assertIn("2026-09-24T08:30:00+08:00", [item["at"] for item in upcoming])

    def test_every_trigger_declares_a_real_source(self):
        for trigger in v3_headless.TRIGGERS:
            self.assertIn(trigger.key, v3_headless.TRIGGER_SOURCES)
            self.assertTrue(trigger.note.strip())
            self.assertIn(trigger.task_type, v3_headless.TASK_TYPES)

    def test_tick_never_raises_on_broken_trigger(self):
        def boom(runner, config, moment):
            raise RuntimeError("源坏了")
        broken = v3_headless.Trigger("position_move", "event", "risk_review", "坏触发", boom)
        scheduler = v3_headless.HeadlessScheduler(self.runner, triggers=[broken])
        result = scheduler.tick_once(WEDNESDAY_0900)
        self.assertEqual(result["checked"][0]["status"], "error")
        self.assertIn("源坏了", result["checked"][0]["detail"]["reason"])


# ---------------------------------------------------------------------------
# 7. 事件 / 流水线触发条件的真实数据来源
# ---------------------------------------------------------------------------
class EventTriggerTests(unittest.TestCase):
    def setUp(self):
        self.home = make_home()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        install_profile(self.home)
        self.config = make_config(requireWhitelist=False)

    def store(self):
        from trading_core import store  # noqa: PLC0415
        return store, store.connect(store.db_path(str(self.home)))

    def runner(self, v3_run=None):
        return make_runner(self.home, v3_run=v3_run)

    # ---- 突发新闻 ----
    def test_breaking_news_no_data_then_fire(self):
        runner = self.runner()
        moment = WEDNESDAY_0900
        result = v3_headless.check_breaking_news(runner, self.config, moment)
        self.assertEqual(result.status, "no-data")
        self.assertIn("无资讯观测", result.detail["reason"])
        store, conn = self.store()
        try:
            for index in range(6):
                store.insert_sentiment(conn, "2026-09-23", f"SH.60000{index}", "fin_news",
                                       {"title": f"新闻{index}"},
                                       fetched_at=(moment.isoformat()))
        finally:
            conn.close()
        result = v3_headless.check_breaking_news(runner, self.config, moment)
        self.assertEqual(result.status, "fire")
        self.assertEqual(result.detail["observations"], 6)
        self.assertIn("不做情绪打分", result.detail["caliber"])

    # ---- 持仓异动 ----
    def test_position_move_reads_pl_ratio(self):
        workbench = FakeWorkbench({"positions": {"ok": True, "value": {"groups": [
            {"account": "模拟账户", "positions": [
                {"symbol": "SH.600000", "pl_ratio": 1.2, "price": 10.1, "cost_price": 10.0},
                {"symbol": "SH.600519", "price": 120.0, "cost_price": 100.0}]}]}}})
        runner = self.runner(workbench)
        result = v3_headless.check_position_move(runner, self.config, WEDNESDAY_0900)
        self.assertEqual(result.status, "fire")
        self.assertEqual(result.detail["worst"]["symbol"], "SH.600519")
        self.assertAlmostEqual(result.detail["worst"]["pct"], 20.0, places=6)
        self.assertEqual(result.detail["worst"]["source"], "positions[].price/cost_price")

    def test_position_move_no_data_without_tool_surface(self):
        result = v3_headless.check_position_move(self.runner(), self.config,
                                                 WEDNESDAY_0900)
        self.assertEqual(result.status, "no-data")
        self.assertIn("positions", result.detail["reason"])

    # ---- 风控阈值突破 ----
    def test_risk_breach_uses_probe_cache_and_sync_table(self):
        runner = self.runner()
        moment = WEDNESDAY_0900
        empty = v3_headless.check_risk_breach(runner, self.config, moment)
        self.assertEqual(empty.status, "no-data")
        (self.home / "v3-risk-probe.json").write_text(json.dumps({
            "generated_at": moment.timestamp(), "generated_at_iso": moment.isoformat(),
            "top_weight_pct": 21.5, "top_industry": "银行", "limit_pct": 20.0,
            "breach": True, "source": "futu/info_owner_plate", "market": "SH"}),
            encoding="utf-8")
        v3_db.append_event(self.home, "oms_sync", {
            "at": moment.isoformat(), "nav": 100000.0, "drawdown_pct": 15.4,
            "nav_source": "sim-ledger(equity.current)"})
        result = v3_headless.check_risk_breach(runner, self.config, moment)
        self.assertEqual(result.status, "fire")
        rules = {item["rule"] for item in result.detail["breaches"]}
        self.assertEqual(rules, {"industry", "drawdown"})

    def test_risk_breach_ignores_stale_probe(self):
        runner = self.runner()
        moment = WEDNESDAY_0900
        (self.home / "v3-risk-probe.json").write_text(json.dumps({
            "generated_at": moment.timestamp() - 7 * 3600, "top_weight_pct": 30.0,
            "breach": True}), encoding="utf-8")
        result = v3_headless.check_risk_breach(runner, self.config, moment)
        self.assertEqual(result.status, "no-data")
        self.assertIn("过期", result.detail["probeNote"])

    # ---- 因子信号反转 ----
    def test_factor_reversal_no_data_then_idle_then_fire(self):
        runner = self.runner()
        moment = WEDNESDAY_0900
        self.assertEqual(v3_headless.check_factor_reversal(runner, self.config,
                                                           moment).status, "no-data")
        store, conn = self.store()
        try:
            store.save_factor_snapshot(conn, "2026-09-22", {
                "date": "2026-09-22", "tickers": {"SH.600000": {"mom_20": -0.02}}})
            store.save_factor_snapshot(conn, "2026-09-23", {
                "date": "2026-09-23", "tickers": {"SH.600000": {"mom_20": -0.01}}})
        finally:
            conn.close()
        self.assertEqual(v3_headless.check_factor_reversal(runner, self.config,
                                                           moment).status, "idle")
        store, conn = self.store()
        try:
            store.save_factor_snapshot(conn, "2026-09-23", {
                "date": "2026-09-23", "tickers": {"SH.600000": {"mom_20": 0.03}}})
        finally:
            conn.close()
        result = v3_headless.check_factor_reversal(runner, self.config, moment)
        self.assertEqual(result.status, "fire")
        self.assertTrue(result.detail["flipped"])
        self.assertEqual(result.as_of, "2026-09-23")

    # ---- 流水线 ----
    def test_pre_rebalance_confirm_reads_frozen_plan_with_open_orders(self):
        runner = self.runner()
        moment = WEDNESDAY_0900
        self.assertEqual(v3_headless.check_pre_rebalance_confirm(
            runner, self.config, moment).status, "idle")
        store, conn = self.store()
        try:
            store.insert_plan(conn, "P-1", "2026-09-23", "sim", "mom_20", "{}", "h")
            store.upsert_plan_status(conn, "P-1", "frozen")
            store.insert_order(conn, "C-1", "P-1", "SH.600000", "SH", "BUY", 100, 10.0,
                               "sim")
        finally:
            conn.close()
        result = v3_headless.check_pre_rebalance_confirm(runner, self.config, moment)
        self.assertEqual(result.status, "fire")
        self.assertEqual(result.event_key, "plan:P-1")
        self.assertEqual(result.detail["pending"][0]["orders"], 1)

    def test_strategy_param_change_reads_rule_candidates(self):
        runner = self.runner()
        moment = WEDNESDAY_0900
        self.assertEqual(v3_headless.check_strategy_param_change(
            runner, self.config, moment).status, "idle")
        store, conn = self.store()
        try:
            store.upsert_rule(conn, "R-1", {"factor": "mom_20"}, status="candidate")
        finally:
            conn.close()
        result = v3_headless.check_strategy_param_change(runner, self.config, moment)
        self.assertEqual(result.status, "fire")
        self.assertEqual(result.event_key, "rule:R-1")

    def test_event_trigger_fires_end_to_end_with_prompt_context(self):
        workbench = FakeWorkbench({"positions": {"ok": True, "value": {"groups": [
            {"account": "模拟账户", "positions": [
                {"symbol": "SH.600000", "pl_ratio": 9.9}]}]}},
            "series": {"ok": True, "value": {"bars": [{"close": 1.0}, {"close": 1.1}]}}})
        spawner = FakeSpawner()
        runner = make_runner(self.home, spawner=spawner, v3_run=workbench)
        scheduler = v3_headless.HeadlessScheduler(runner)
        result = scheduler.tick_once(WEDNESDAY_0900)
        fired = {item["trigger"] for item in result["fired"]}
        self.assertIn("position_move", fired)
        runner._queue.join()
        rows = v3_db.list_events(self.home, "headless_log")
        record = [row for row in rows if row["trigger"] == "position_move"][0]
        self.assertIn("复盘当前组合的风险暴露", record["prompt"])
        self.assertIn("SH.600000", record["prompt"])
        self.assertIn("只读研究", record["prompt"])
        self.assertEqual(record["sources"]["trigger"],
                         v3_headless.TRIGGER_SOURCES["position_move"])
        runner.stop(2)


# ---------------------------------------------------------------------------
# 8. FR-MON-003：调用日志
# ---------------------------------------------------------------------------
class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.home = make_home()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        install_profile(self.home)

    def test_log_row_has_every_required_field(self):
        runner = make_runner(self.home, spawner=FakeSpawner(
            result=v3_headless.ProcessResult(stdout="答案", stderr="诊断", exit_code=0,
                                             duration_ms=42.0)))
        runner.execute("提示词", task_type="risk_review", trigger="manual")
        row = v3_db.list_events(self.home, "headless_log")[0]
        for key in ("prompt", "stdout", "stderr", "exit_code", "duration_ms",
                    "tokens_estimate", "success", "started_at", "outcome", "task_type",
                    "trigger", "profile", "contract_note", "token_estimate_note"):
            self.assertIn(key, row)
        self.assertEqual(row["prompt"], "提示词")
        self.assertEqual(row["stdout"], "答案")
        self.assertEqual(row["stderr"], "诊断")
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["duration_ms"], 42.0)
        self.assertEqual(row["success"], 1)
        self.assertGreaterEqual(row["tokens_estimate"], 1)
        # 表列（不是只有 payload）：常用列真的被提取出来了
        self.assertTrue(v3_db.count_events(self.home, "headless_log") == 1)

    def test_blocked_call_is_still_logged(self):
        runner = make_runner(self.home, spawner=FakeSpawner(), enabled=False)
        record = runner.execute("提示词", task_type="risk_review", trigger="manual")
        self.assertEqual(record["outcome"], "disabled")
        self.assertEqual(record["success"], 0)
        self.assertIsNone(record["exit_code"])
        self.assertEqual(v3_db.count_events(self.home, "headless_log"), 1)

    def test_log_endpoint_paginates_and_filters(self):
        runner = make_runner(self.home)
        for index in range(3):
            # 只有 index=2 那次成功（success 只认 1，不是「非零即真」）
            v3_db.append_event(self.home, "headless_log", {
                "started_at": f"2026-09-2{index + 1}T00:00:00+00:00",
                "success": 1 if index == 2 else 0,
                "exit_code": 0, "duration_ms": 10.0 + index, "tokens_estimate": 100 + index,
                "task_type": "risk_review" if index else "pre_market_scan",
                "trigger": "manual",
                "outcome": "completed" if index == 2 else "incomplete",
                "stdout": f"out{index}", "stderr": ""})
        app = FastAPI()
        v3_headless.register(app, None, str(self.home))
        client = TestClient(app)
        body = client.get("/api/v3/headless/log").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["items"]), 3)
        self.assertEqual(body["items"][0]["stdout"], "out2")
        page = client.get("/api/v3/headless/log?limit=1&offset=1").json()
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["stdout"], "out1")
        filtered = client.get("/api/v3/headless/log?success=true").json()
        self.assertEqual(filtered["filtered"], 1)
        self.assertEqual(filtered["items"][0]["stdout"], "out2")
        by_type = client.get("/api/v3/headless/log?task_type=pre_market_scan").json()
        self.assertEqual(by_type["filtered"], 1)
        self.assertEqual(by_type["items"][0]["stdout"], "out0")
        by_time = client.get(
            "/api/v3/headless/log?since=2026-09-22T00:00:00+00:00").json()
        self.assertEqual(by_time["filtered"], 2)
        self.assertEqual(body["summary"]["total"], 3)
        self.assertIn("估算", body["estimation"])
        self.assertIn("分列", body["streams"])

    def test_ops_headless_last_reads_the_real_table(self):
        runner = make_runner(self.home)
        runner.execute("第一次", task_type="pre_market_scan",
                       trigger="slot:pre_market_scan")
        last = v3_ops._headless_last(self.home)
        self.assertEqual(len(last), 1)
        self.assertEqual(last[0]["taskType"], "pre_market_scan")
        self.assertTrue(last[0]["success"])
        self.assertEqual(last[0]["outcome"], "completed")
        self.assertNotIn("prompt", last[0], "网关视图只取精简投影，不塞全文")

    def test_ops_brain_and_gateway_report_real_last(self):
        runner = make_runner(self.home)
        runner.execute("第一次", task_type="risk_review", trigger="manual")
        workbench = FakeWorkbench({"sources": {"ok": True, "value": {"channels": []}},
                                   "schedule": {"ok": True, "value": {"jobs": []}}})
        app = FastAPI()
        v3_ops.register(app, workbench, str(self.home))
        client = TestClient(app)
        brain = client.get("/api/v3/brain").json()
        self.assertEqual(brain["headless"]["last"][0]["taskType"], "risk_review")
        gateway = client.get("/api/v3/gateway").json()
        self.assertEqual(gateway["headless"]["last"][0]["taskType"], "risk_review")

    def test_schedule_endpoint_exposes_switches_and_next_fire(self):
        app = FastAPI()
        runner, scheduler = v3_headless.register(app, None, str(self.home))
        self.addCleanup(runner.stop, 2)
        client = TestClient(app)
        body = client.get("/api/v3/headless/schedule").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["breaker"], {"maxConcurrency": 3, "timeoutSeconds": 300.0,
                                           "tokenBudget": 200000})
        self.assertTrue(body["nextFireTimes"])
        self.assertIn("profileWhitelist", body)
        keys = {item["key"] for item in body["triggers"]}
        self.assertEqual(keys, set(v3_headless.TRIGGER_SOURCES))
        for item in body["triggers"]:
            self.assertIn("enabled", item)
            self.assertTrue(item["source"])
        self.assertIn("外部熔断", body["breakerNote"])
        v3_headless.reset_registry()


# ---------------------------------------------------------------------------
# 9. 白名单：绝不触发交易
# ---------------------------------------------------------------------------
class WhitelistTests(unittest.TestCase):
    def test_repo_material_verifies(self):
        ok, detail = v3_headless.verify_whitelist(v3_headless.PROFILE_MATERIAL_DIR)
        self.assertTrue(ok, detail.get("error"))
        self.assertEqual(detail["uncoveredTools"], [])
        self.assertEqual(detail["missingDisabledRows"], [])

    def test_material_and_module_agree(self):
        material, error = v3_headless.load_whitelist_material()
        self.assertIsNone(error)
        self.assertEqual(tuple(material["disabledRows"]),
                         v3_headless.REQUIRED_DISABLED_ROWS)
        self.assertEqual(set(material["denyTools"]), set(v3_headless.deny_tool_names()))
        self.assertEqual(material["matcher"],
                         "|".join(v3_headless.deny_matchers()))

    def test_every_write_trade_tool_in_the_mcp_surface_is_denied(self):
        """MCP 工具面里凡是写/交易工具，都必须在拒绝名单里——一个都不能漏。"""
        denied = v3_headless.deny_tool_names()
        probe_endpoints = v3_headless.probe_write_endpoints()
        write_endpoints = {str(item).replace("-", "_")
                           for item in (probe_endpoints | set(v3_ops.WRITE_TOOLS))}
        self.assertTrue(v3_ops.WRITE_TOOLS <= denied)
        checked = 0
        for tool in mcp_tools.TOOLS:
            name = tool.name
            endpoint = (getattr(tool, "endpoint", "") or "").replace("-", "_")
            if name in v3_ops.WRITE_TOOLS or endpoint in write_endpoints:
                self.assertIn(name, denied, f"写/交易工具 {name} 没有进拒绝名单")
                checked += 1
        self.assertGreaterEqual(checked, 5, "至少要覆盖 trade_*/plan_execute/switch_mode")
        self.assertIn("mcp__quantwb__trade_place",
                      v3_headless.deny_matchers(server_name="quantwb"))

    def test_hooks_matcher_covers_every_denied_name(self):
        hooks = json.loads((v3_headless.PROFILE_MATERIAL_DIR
                            / v3_headless.HOOKS_FILENAME).read_text(encoding="utf-8"))
        matchers = [group["matcher"] for group in hooks["hooks"]["PreToolUse"]]
        for name in v3_headless.deny_matchers():
            self.assertTrue(v3_headless._matcher_covers(matchers, name),
                            f"{name} 没有钩子拒绝")

    def test_local_tool_rows_are_disabled_in_patch(self):
        text = (v3_headless.PROFILE_MATERIAL_DIR
                / v3_headless.PATCH_FILENAME).read_text(encoding="utf-8")
        rows = {row["id"]: row for row in v3_headless.parse_patch_rows(text)}
        for name in ("tool-bash", "tool-web", "tool-subagent", "tool-workflow",
                     "tool-ralph", "tool-goal"):
            self.assertTrue(rows[name]["disabled"], f"{name} 必须 disabled")
        self.assertEqual(rows[v3_headless.DENY_ROW_ID]["name"],
                         "@deepseek-ai/dsh-hooks-claude-code")

    def test_whitelist_failure_blocks_the_spawn(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        directory = install_profile(home)
        # 装了个「没有白名单」的 profile：package.json 在，patch/hooks 不在
        runner = make_runner(home, spawner=FakeSpawner(), requireWhitelist=True)
        record = runner.execute("任务", task_type="risk_review", trigger="manual")
        self.assertEqual(record["outcome"], "whitelist-unverified")
        self.assertEqual(record["success"], 0)
        self.assertEqual(runner.spawner.calls, [], "白名单校验不过就绝不能起进程")
        self.assertIsNone(record["exit_code"])
        self.assertIn("fail-closed", record["error"]["message"])
        self.assertEqual(directory, Path(home) / "profiles" / "quant-headless")
        # 记录仍然落库（FR-MON-003：被拦下的调用也要有痕）
        self.assertEqual(v3_db.count_events(home, "headless_log"), 1)

    def test_missing_profile_blocks_the_spawn(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        runner = make_runner(home, spawner=FakeSpawner(), requireWhitelist=False)
        record = runner.execute("任务", task_type="risk_review", trigger="manual")
        self.assertEqual(record["outcome"], "profile-missing")
        self.assertEqual(runner.spawner.calls, [])
        self.assertIn("不自动回退", record["error"]["message"])

    def test_missing_dsh_blocks_the_spawn(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        install_profile(home)
        spawner = FakeSpawner()
        runner = v3_headless.HeadlessRunner(str(home), config=make_config(),
                                            spawner=spawner,
                                            env={"QUANT_HEADLESS_DSH_BIN": "",
                                                 "DSH_BIN": ""})
        runner._env.pop("PATH", None)
        import unittest.mock as mock
        with mock.patch("shutil.which", return_value=None):
            record = runner.execute("任务", task_type="risk_review", trigger="manual")
        self.assertEqual(record["outcome"], "dsh-not-found")
        self.assertEqual(spawner.calls, [])


# ---------------------------------------------------------------------------
# 10. 配置（开关来源可查）
# ---------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def test_file_and_env_sources_are_reported(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / v3_headless.CONFIG_FILENAME).write_text(json.dumps({
            "timeoutSeconds": 120, "events": {"breaking_news": False}}), encoding="utf-8")
        config, sources = v3_headless.load_config(home, env={
            "QUANT_HEADLESS_CONCURRENCY": "5", "QUANT_HEADLESS_ENABLED": "false"})
        self.assertEqual(config["timeoutSeconds"], 120)
        self.assertEqual(config["maxConcurrency"], 5)
        self.assertFalse(config["enabled"])
        self.assertFalse(config["events"]["breaking_news"])
        self.assertTrue(config["events"]["risk_breach"])
        self.assertEqual(sources["sources"]["timeoutSeconds"],
                         f"file:{v3_headless.CONFIG_FILENAME}")
        self.assertEqual(sources["sources"]["maxConcurrency"],
                         "env:QUANT_HEADLESS_CONCURRENCY")
        self.assertEqual(sources["sources"]["enabled"], "env:QUANT_HEADLESS_ENABLED")

    def test_broken_config_falls_back_and_says_so(self):
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / v3_headless.CONFIG_FILENAME).write_text("{ not json", encoding="utf-8")
        config, sources = v3_headless.load_config(home, env={})
        self.assertEqual(config["maxConcurrency"], 3)
        self.assertTrue(any("读取失败" in note for note in sources["notes"]))


# ---------------------------------------------------------------------------
# 11. 真机用例（默认 skip；开跑见 docs/e2e-and-data-gaps.md）
# ---------------------------------------------------------------------------
class RealDshProbeTests(unittest.TestCase):
    """真机 ``dsh`` 调用：默认 skip（要独立 DSH_HOME + 凭据 + 模型路由）。

    打开方式::

        QUANT_HEADLESS_PROBE=1 QUANT_HEADLESS_DSH_HOME=~/.dsh/headless-probe \\
        QUANT_HEADLESS_DSH_BIN=$(command -v dsh) \\
        ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_headless.RealDshProbeTests -v

    ``QUANT_HEADLESS_PROBE_DSH_HOME`` 指向已装好白名单 profile 与凭据的独立 DSH_HOME。
    """

    def test_real_minimal_task(self):
        probe_home = os.environ.get("QUANT_HEADLESS_PROBE_DSH_HOME", "")
        if not os.environ.get("QUANT_HEADLESS_PROBE") or not probe_home:
            self.skipTest("真机 headless 用例默认 skip：需要 QUANT_HEADLESS_PROBE=1 与 "
                          "QUANT_HEADLESS_PROBE_DSH_HOME（独立 DSH_HOME，含凭据与 profile）")
        home = make_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        env = {key: value for key, value in os.environ.items()
               if key in ("PATH", "HOME", "LANG", "LC_ALL")}
        env["QUANT_HEADLESS_DSH_BIN"] = os.environ["QUANT_HEADLESS_DSH_BIN"]
        config = make_config(profile=os.environ.get("QUANT_HEADLESS_PROBE_PROFILE",
                                                    "quant-headless"),
                             requireWhitelist=True, dshHome=probe_home)
        runner = v3_headless.HeadlessRunner(str(home), config=config, env=env)
        record = runner.execute("只回复 OK，不要调用任何工具",
                                task_type="risk_review", trigger="manual")
        self.assertIn(record["outcome"], ("completed", "whitelist-unverified",
                                          "profile-missing", "runner-error", "incomplete"))
        self.assertIn("stdout", record)
        print("\n[真机] outcome=%s exit=%s duration=%sms stdout=%r stderr=%r"
              % (record["outcome"], record["exit_code"], record["duration_ms"],
                 record["stdout"][:200], record["stderr"][:200]))


if __name__ == "__main__":
    unittest.main()
