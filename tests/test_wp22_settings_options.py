"""WP22：设置页「有限集合」字段的选项化 + 写入侧策略名校验（2026-09-18 实机反馈）。

用户可见缺陷（原始抱怨）：设置页自动流水线卡片里的**策略**是一个自由文本框
（placeholder「策略名，如 watchlist_rsi」），关注池键与两个时刻同样是自由文本。
策略名不是靠记忆能写对的字符串——合法取值只有两类：

  1. 内置策略 id：``trading_core.strategies.REGISTRY`` 的模块级注册项（4 个）；
  2. ``rules`` 表里 ``status='enabled'`` 的 ``rule_id``（``planner._resolve_strategy``
     每次回查 DB 状态，非 enabled 一律拒绝消费）。

写错一个字母时旧实现**静默保存成功**，此后 ``plan_auto`` 每天软跳过「策略未注册」，
页面上只是「没动静」——没有任何线索指向真正的原因。本文件把「选项化」与「写入侧
fail-closed 校验」两件事钉住：

  * ``FrontendStrategyMirrorTests`` —— ``platform/web/src/services/strategies.js`` 的
    内置策略清单与 core ``REGISTRY`` 的**漂移锁**（正则解析 JS 源码 + Python 常量比对，
    沿用 ``tests/test_wp10_locks.py`` 手法：不相信「记得同步」）；
  * ``SettingsPageControlTests`` —— 设置页那 4 个字段不再是自由文本框的结构锁
    （策略/池键 → Select，两个时刻 → TimePicker）；
  * ``StrategyNameValidationTests`` —— 写入侧取值域 = 内置策略 ∪ 已批准规则；
    非法值拒绝且**错误信息列出合法取值**，未批准规则拒绝，规则库不可读时 fail-closed。

调度侧读取行为**不变**：历史配置里已经写坏的策略名仍由 ``plan_auto`` 的
「策略未注册」软跳过 + 告警处理（写入侧拦截新错误，读取侧兜住旧错误）。
"""
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import settings_api  # noqa: E402
from server.store_access import WorkbenchError  # noqa: E402
from trading_core import autopipeline, rule_engine, store, strategies, watchlist  # noqa: E402

STRATEGIES_JS = ROOT / "platform" / "web" / "src" / "services" / "strategies.js"
PIPELINE_JS = ROOT / "platform" / "web" / "src" / "services" / "pipeline.js"
SETTINGS_JSX = ROOT / "platform" / "web" / "src" / "pages" / "settings.jsx"

#: BUILTIN_STRATEGIES 的条目形状（一字不差地要求 id/label/note 三个键：
#: 标签是给人看的，说明是给「记不住策略名」的人看的，缺一个都不算完成选项化）。
#: 第 4 个键 ``selectable`` 可省略（省略 = 可选）；``selectable: false`` 表示自动流水线
#: 消费不了它（缺 ``target_weights``，见 ``test_selectable_matches_core_capability``）。
ENTRY_RE = re.compile(
    r'\{\s*id:\s*"([^"]+)"\s*,\s*label:\s*"([^"]*)"\s*,\s*note:\s*"([^"]*)"'
    r'(?:\s*,\s*selectable:\s*(true|false))?\s*\}')


def _array_body(source, name):
    """抠出 ``export const NAME = [ ... ]`` 的方括号体（方括号配平）。"""
    match = re.search(rf"export\s+const\s+{name}\s*=\s*\[", source)
    if not match:
        raise AssertionError(f"未找到 {name} 的数组字面量")
    start = match.end() - 1
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "[":
            depth += 1
        elif source[index] == "]":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
    raise AssertionError(f"{name} 方括号不配平")


class FrontendStrategyMirrorTests(unittest.TestCase):
    """内置策略清单是**镜像**：core 增删内置策略而前端没跟，下拉就会漏项或多项。"""

    def setUp(self):
        self.source = STRATEGIES_JS.read_text(encoding="utf-8")
        self.body = _array_body(self.source, "BUILTIN_STRATEGIES")
        self.entries = ENTRY_RE.findall(self.body)

    def test_entries_are_parseable(self):
        """反证解析器本身有效：漏一个条目就会让下面的比对变成假绿。"""
        self.assertEqual(len(self.entries), 4, f"BUILTIN_STRATEGIES 解析到 {self.entries!r}")

    def test_ids_match_core_registry(self):
        core = {name for name in strategies.REGISTRY if not strategies.is_rule(name)}
        self.assertEqual({entry[0] for entry in self.entries}, core)
        # 顺序不作要求（展示顺序是产品决定），但 id 必须逐字相等（大小写/下划线都在内）
        self.assertEqual(len(self.entries), len(core))

    def test_every_builtin_has_chinese_label_and_note(self):
        """中文标签 + 一句话说明：这正是「记不住策略名」的解法，不允许留空。"""
        for sid, label, note, _selectable in self.entries:
            with self.subTest(strategy=sid):
                self.assertRegex(label, r"[\u4e00-\u9fff]")
                self.assertRegex(note, r"[\u4e00-\u9fff]")

    def test_selectable_matches_core_capability(self):
        """``selectable: false`` 的集合 == 缺 ``target_weights`` 的内置策略集合。

        2026-09-18 实测：``plan_auto`` 对单标的策略（``rsi``/``ma_cross``）抛
        ``AttributeError: 'RsiStrategy' object has no attribute 'target_weights'``——
        违反它自己的「永不抛」契约，当日 build_plan 作业失败。下拉必须把这件事
        如实标出来（可见但不可选），否则「选项化」只是把输入错误换成了选择错误。

        这条是**镜像**（JS 侧没法反射 Python 对象），所以用漂移锁钉住：core 给单标的
        策略补上 ``target_weights`` 后，本用例会失败并提醒前端去掉禁用。
        """
        core = {name for name in strategies.REGISTRY if not strategies.is_rule(name)}
        without_target_weights = {name for name in core
                                  if not hasattr(strategies.REGISTRY[name], "target_weights")}
        blocked = {entry[0] for entry in self.entries if entry[3] == "false"}
        self.assertEqual(blocked, without_target_weights)
        # 被禁用的条目必须在说明里写清原因（否则用户只看到「灰了」，不知为何）
        for sid, _label, note, selectable in self.entries:
            if selectable == "false":
                with self.subTest(strategy=sid):
                    self.assertIn("target_weights", note)

    def test_pool_key_default_mirrors_core(self):
        """池键缺省值只有一处事实源（``watchlist.DEFAULT_POOL_KEY``），前端是镜像。"""
        match = re.search(r'export\s+const\s+POOL_KEY_DEFAULT\s*=\s*"([^"]*)"',
                          PIPELINE_JS.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "services/pipeline.js 缺 POOL_KEY_DEFAULT 镜像")
        self.assertEqual(match.group(1), watchlist.DEFAULT_POOL_KEY)


class SettingsPageControlTests(unittest.TestCase):
    """设置页控件形状锁：4 个配置字段不得退回自由文本框（本轮缺陷的成因）。"""

    def setUp(self):
        self.jsx = SETTINGS_JSX.read_text(encoding="utf-8")

    def test_strategy_field_is_a_select(self):
        self.assertIn("strategyOptions(", self.jsx)
        self.assertNotIn('placeholder="策略名，如 watchlist_rsi"', self.jsx)
        self.assertNotRegex(self.jsx, r"<Input\s+value=\{row\.strategy\}")

    def test_watchlist_field_is_a_select(self):
        self.assertIn("poolKeyOptions(", self.jsx)
        self.assertNotIn('placeholder="池键，如 watchlist"', self.jsx)
        self.assertNotRegex(self.jsx, r"<Input\s+value=\{row\.watchlist\}")

    def test_time_fields_are_time_pickers(self):
        self.assertIn("TimePicker", self.jsx)
        self.assertNotRegex(self.jsx, r'<Input\s+value=\{base\(\)\.exec_at')
        self.assertNotRegex(self.jsx, r'<Input\s+value=\{base\(\)\.reconcile_at\}')

    def test_time_fields_still_bounded_on_the_server_side_contract(self):
        """执行窗口上界的镜像锁（N2）仍成立：InputNumber 未被顺手改掉。"""
        self.assertIn("max={EXEC_WINDOW_MAX_MINUTES}", self.jsx)

    def test_rules_endpoint_is_fetched_for_the_options(self):
        """「已批准规则」进下拉的前提是拿到规则清单；漏了取数就只能列内置策略。"""
        self.assertIn('useEndpoint("rules"', self.jsx)


class SettingsBase(unittest.TestCase):
    """临时 home + 真实 SQLite（规则状态校验要读 rules 表）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def platform_path(self):
        return self.home / "trading-platform.json"

    def write_platform(self, payload):
        self.platform_path().write_text(json.dumps(payload, ensure_ascii=False),
                                        encoding="utf-8")

    def read_platform(self):
        return json.loads(self.platform_path().read_text(encoding="utf-8"))

    def conn(self):
        conn = store.connect(store.db_path(str(self.home)))
        self.addCleanup(conn.close)
        return conn

    def seed_rule(self, rule_id, status="enabled"):
        """按真实路径落一条规则：先落到 ``passed``，再由 ``decide_rule`` 启用。"""
        conn = self.conn()
        store.upsert_rule(conn, rule_id,
                          {"rule_id": rule_id, "hypothesis": "测试用假设",
                           "factors": ["momentum_20"]},
                          status="passed" if status == "enabled" else status)
        if status == "enabled":
            rule_engine.decide_rule(conn, rule_id, "enable", by="web")
        return conn

    def client(self):
        class IdleScheduler:
            last_error = None

            def start(self):
                pass

            def stop(self):
                pass

        app = app_module.create_app(home=str(self.home),
                                    dist=str(self.home / "dist-missing"),
                                    scheduler=IdleScheduler())
        return TestClient(app)


class StrategyNameValidationTests(SettingsBase):
    """写入侧取值域 = 内置策略 id ∪ status='enabled' 的 rule_id（fail-closed）。"""

    def test_all_builtin_strategy_names_accepted(self):
        builtins = sorted(name for name in strategies.REGISTRY
                          if not strategies.is_rule(name))
        payload = {"enabled": True,
                   "strategies": [{"market": "SH", "strategy": name} for name in builtins]}
        out = settings_api.save_auto_pipeline(self.home, payload)
        self.assertEqual([row["strategy"] for row in out["strategies"]], builtins)

    def test_builtin_only_save_needs_no_rule_db(self):
        """只有内置策略时不碰规则库：全新 home（无 trading-data 目录）也能保存。"""
        self.assertFalse((self.home / "trading-data").exists())
        settings_api.save_auto_pipeline(
            self.home, {"strategies": [{"market": "SH", "strategy": "rsi"}]})
        self.assertEqual(self.read_platform()["auto_pipeline"]["strategies"][0]["strategy"],
                         "rsi")

    def test_enabled_rule_id_accepted(self):
        self.seed_rule("wp22_approved_v1", status="enabled")
        out = settings_api.save_auto_pipeline(
            self.home, {"strategies": [{"market": "HK", "strategy": "wp22_approved_v1"}]})
        self.assertEqual(out["strategies"][0]["strategy"], "wp22_approved_v1")

    def test_unapproved_rule_ids_rejected(self):
        for status in ("candidate", "failed", "disabled"):
            with self.subTest(status=status):
                rule_id = f"wp22_{status}_v1"
                self.seed_rule(rule_id, status=status)
                with self.assertRaises(WorkbenchError) as caught:
                    settings_api.save_auto_pipeline(
                        self.home, {"strategies": [{"market": "SH", "strategy": rule_id}]})
                self.assertIn(rule_id, str(caught.exception))
                self.assertIn("合法取值", str(caught.exception))

    def test_unknown_strategy_rejected_and_lists_legal_values(self):
        self.seed_rule("wp22_approved_v1", status="enabled")
        with self.assertRaises(WorkbenchError) as caught:
            settings_api.save_auto_pipeline(
                self.home,
                {"enabled": True,
                 "strategies": [{"market": "SH", "strategy": "watchlist_rsi_typo"}]})
        message = str(caught.exception)
        self.assertIn("watchlist_rsi_typo", message)
        self.assertIn("合法取值", message)
        # 合法取值必须**逐个列出**（让人一眼能改对），内置 4 个 + 已批准规则都要在
        for name in ("ma_cross", "momentum_value_top5", "rsi", "watchlist_rsi",
                     "wp22_approved_v1"):
            self.assertIn(name, message)

    def test_rejection_leaves_file_untouched(self):
        self.write_platform({"watchlist": ["SH.600519"]})
        before = self.platform_path().read_bytes()
        with self.assertRaises(WorkbenchError):
            settings_api.save_auto_pipeline(
                self.home, {"strategies": [{"market": "SH", "strategy": "nope_v1"}]})
        self.assertEqual(self.platform_path().read_bytes(), before)

    def test_rules_store_unreadable_fails_closed(self):
        """规则库读不出来时无法确认「已批准」→ 拒绝非内置策略名（fail-closed）。"""
        with mock.patch("trading_core.store.connect",
                        side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(WorkbenchError) as caught:
                settings_api.save_auto_pipeline(
                    self.home,
                    {"strategies": [{"market": "SH", "strategy": "some_rule_v1"}]})
        message = str(caught.exception)
        self.assertIn("规则库", message)
        self.assertIn("watchlist_rsi", message)  # 内置策略仍列出，便于改对
        self.assertFalse(self.platform_path().exists())

    def test_time_fields_round_trip_as_hhmm_strings(self):
        """时刻字段保存后仍是 ``"HH:MM"`` 字符串（前端选择器的 dayjs 对象不得进 payload）。"""
        out = settings_api.save_auto_pipeline(
            self.home, {"exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
                        "reconcile_at": "19:00"})
        raw = self.read_platform()["auto_pipeline"]
        self.assertEqual(raw["exec_at"], {"SH": "09:35", "HK": "09:45", "US": "22:35"})
        self.assertEqual(raw["reconcile_at"], "19:00")
        for value in list(raw["exec_at"].values()) + [raw["reconcile_at"]]:
            self.assertIsInstance(value, str)
            self.assertRegex(value, r"^\d{2}:\d{2}$")
        # 响应形状不变：仍是扁平有效配置（键集 = core 缺省值键集）
        self.assertEqual(set(out), set(autopipeline.AUTO_PIPELINE_DEFAULTS))


class StrategyNameHttpEnvelopeTests(SettingsBase):
    """路由层：非法策略名是**业务失败**（200 + trading/invalid-operation 信封）。"""

    def test_post_invalid_strategy_returns_invalid_operation_envelope(self):
        with self.client() as client:
            resp = client.post("/api/wb/auto_pipeline", json={
                "enabled": True,
                "strategies": [{"market": "SH", "strategy": "wathclist_rsi"}]})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        message = body["error"]["message"]
        self.assertIn("wathclist_rsi", message)
        self.assertIn("watchlist_rsi", message)

    def test_post_builtin_strategy_succeeds(self):
        with self.client() as client:
            resp = client.post("/api/wb/auto_pipeline", json={
                "enabled": True,
                "strategies": [{"market": "SH", "strategy": "watchlist_rsi",
                                "watchlist": "watchlist"}],
                "exec_at": {"SH": "09:35"}, "exec_window_minutes": 30,
                "reconcile_at": "19:00"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])


if __name__ == "__main__":
    unittest.main()
