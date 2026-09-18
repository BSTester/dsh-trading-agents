"""WP10 任务 2：``auto_pipeline`` 设置读写端点（Web-only，不进 MCP 工具面）。

全部离线（临时 home + TestClient；无网络、无券商）。覆盖：

  * GET ``/api/wb/auto_pipeline``：未配置 → 完整缺省结构（enabled=false + 默认
    exec_at/exec_window_minutes/reconcile_at）；已配置 → **生效值**（缺省补全后，
    与调度侧 ``autopipeline.auto_pipeline_config`` 同源）；
  * POST 保存：原子写 ``trading-platform.json`` 的 ``auto_pipeline`` 键，**其余键零
    改动**（service/watchlist/futu_channel 原样保留）；落盘的是提交的 overlay 本身
    （不是补全后的完整配置）；
  * 校验面：未知字段 / enabled 非 bool / exec_at 非 HH:MM 或未知市场 / strategies
    结构错 / exec_window_minutes 非正整数 / 空载荷 → ``trading/invalid-operation``
    业务失败信封，且**文件零改动**（逐字节反证）；
  * 单一实现：校验复用 ``trading_core.autopipeline.apply_overlay``——同一非法载荷在
    两侧报**完全相同的消息**（证明没有第二套规则）；载荷白名单与 core 默认值键集一致；
  * 声明面：``auto_pipeline`` 进 ``store_access.endpoints()``（snapshot 向前端声明）与
    端点工具对等集，但**有意排除在 MCP 工具面之外**（模型不得自拨自动执行开关，
    与 confirm-decide/openapi_* 同类）；
  * 端到端：POST ``enabled=true`` 后 ``autopipeline.build_jobs`` 出现自动作业链
    （build_plan/auto_execute/GLOBAL reconcile）——开关真的改变了调度装配。

WP10 任务 1（流程页 pipeline 端点）的用例在 tests/test_wp10_pipeline.py。
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import config as server_config  # noqa: E402
from server import mcp_tools, settings_api, store_access  # noqa: E402
from server.store_access import WorkbenchError  # noqa: E402
from trading_core import autopipeline  # noqa: E402

DEFAULTS = autopipeline.AUTO_PIPELINE_DEFAULTS


class IdleScheduler:
    """``create_app(scheduler=...)`` 的替身：start/stop 只记账（零线程）。"""

    def __init__(self):
        self.alive = False
        self.last_error = None
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class SettingsBase(unittest.TestCase):
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

    def client(self):
        app = app_module.create_app(home=str(self.home),
                                    dist=str(self.home / "dist-missing"),
                                    scheduler=IdleScheduler())
        return TestClient(app)


class AutoPipelineSettingsTests(SettingsBase):
    """settings_api 层：读/写/校验/落盘纪律。"""

    def test_get_defaults_when_unconfigured(self):
        self.assertEqual(settings_api.get_auto_pipeline(self.home), DEFAULTS)
        # 缺省值不得是共享对象：调用方修改不能污染 core 的常量
        got = settings_api.get_auto_pipeline(self.home)
        got["enabled"] = True
        self.assertFalse(autopipeline.AUTO_PIPELINE_DEFAULTS["enabled"])

    def test_save_writes_overlay_and_preserves_other_keys(self):
        self.write_platform({"service": {"port": 8397},
                             "watchlist": {"SH": ["SH.600519"]},
                             "futu_channel": "mcp"})
        payload = {"enabled": True,
                   "strategies": [{"market": "SH", "strategy": "watchlist_rsi"}]}
        out = settings_api.save_auto_pipeline(self.home, payload)
        raw = self.read_platform()
        # 落盘的是提交的 overlay 本身（缺省值在读取时补，不把默认值冻结进用户文件）
        self.assertEqual(raw["auto_pipeline"], payload)
        # 其余键零改动
        self.assertEqual(raw["service"], {"port": 8397})
        self.assertEqual(raw["watchlist"], {"SH": ["SH.600519"]})
        self.assertEqual(raw["futu_channel"], "mcp")
        # 返回值是生效值快照（与 GET / 调度侧同源）
        self.assertTrue(out["enabled"])
        self.assertEqual(out["exec_at"], DEFAULTS["exec_at"])
        self.assertEqual(out["exec_window_minutes"], DEFAULTS["exec_window_minutes"])
        # 策略项补齐缺省池键（core 归一后的形状）
        self.assertEqual(out["strategies"][0]["market"], "SH")
        self.assertEqual(out["strategies"][0]["strategy"], "watchlist_rsi")
        self.assertTrue(out["strategies"][0]["watchlist"])

    def test_save_returns_same_shape_as_get(self):
        payload = {"enabled": True, "exec_at": {"US": "22:40"}}
        settings_api.save_auto_pipeline(self.home, payload)
        self.assertEqual(settings_api.get_auto_pipeline(self.home),
                         autopipeline.auto_pipeline_config(str(self.home)))
        # 部分覆盖：未给的市场保留默认时刻
        self.assertEqual(settings_api.get_auto_pipeline(self.home)["exec_at"],
                         {"SH": "09:35", "HK": "09:45", "US": "22:40"})

    def test_validation_rejections(self):
        cases = (
            ({"bogus": 1}, "未知 auto_pipeline 字段"),
            ({"enabled": "yes"}, "enabled 需为布尔值"),
            ({"exec_at": {"SH": "9:35"}}, "HH:MM"),
            ({"exec_at": {"XX": "09:35"}}, "未知市场"),
            ({"exec_at": "09:35"}, "exec_at 需为对象"),
            ({"strategies": {"market": "SH"}}, "需为列表"),
            ({"strategies": [{"market": "SH"}]}, "缺少字段"),
            ({"strategies": [{"market": "JP", "strategy": "rsi"}]}, "market 非法"),
            ({"strategies": [{"market": "SH", "strategy": ""}]}, "非空字符串"),
            ({"strategies": [{"market": "SH", "strategy": "rsi", "x": 1}]},
             "未知策略字段"),
            ({"exec_window_minutes": True}, "正整数"),
            ({"exec_window_minutes": 0}, "正整数"),
            ({"exec_window_minutes": autopipeline.EXEC_WINDOW_MAX_MINUTES + 1},
             "不得超过"),
            ({"reconcile_at": "25:00"}, "越界"),
        )
        for payload, fragment in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(WorkbenchError) as caught:
                    settings_api.save_auto_pipeline(self.home, payload)
                self.assertIn(fragment, str(caught.exception))
        # 全部拒绝路径都不得留下文件（零部分写入）
        self.assertFalse(self.platform_path().exists())

    def test_validation_failure_leaves_file_untouched(self):
        self.write_platform({"watchlist": {"SH": ["SH.600519"]}})
        before = self.platform_path().read_bytes()
        with self.assertRaises(WorkbenchError):
            settings_api.save_auto_pipeline(self.home, {"enabled": True, "nope": 1})
        self.assertEqual(self.platform_path().read_bytes(), before)

    def test_empty_payload_rejected_at_save_level(self):
        with self.assertRaises(WorkbenchError):
            settings_api.save_auto_pipeline(self.home, {})
        with self.assertRaises(WorkbenchError):
            settings_api.save_auto_pipeline(self.home, None)

    def test_corrupt_config_file_fails_closed(self):
        self.platform_path().write_text("{ not json", encoding="utf-8")
        before = self.platform_path().read_bytes()
        with self.assertRaises(WorkbenchError):
            settings_api.save_auto_pipeline(self.home, {"enabled": True})
        self.assertEqual(self.platform_path().read_bytes(), before)

    def test_unknown_strategy_name_is_accepted(self):
        """策略名不做注册表校验（有意）：core 只校验非空串。

        理由：WP14 的规则 id 不在静态 REGISTRY 里，静态白名单会把合法配置拒掉；
        真正跑不起来时由策略层 fail-closed 软跳过并告警（作业入口），不在写入侧
        假装能判定。本用例把这个取舍钉住，避免后人「顺手加校验」。
        """
        out = settings_api.save_auto_pipeline(
            self.home, {"enabled": True,
                        "strategies": [{"market": "US", "strategy": "rule_alpha_v9"}]})
        self.assertEqual(out["strategies"][0]["strategy"], "rule_alpha_v9")

    def test_field_whitelist_matches_core_keys(self):
        self.assertEqual(set(settings_api.AUTO_PIPELINE_FIELDS), set(DEFAULTS))
        self.assertIs(app_module.AUTO_PIPELINE_FIELDS, settings_api.AUTO_PIPELINE_FIELDS)

    def test_validation_is_the_core_implementation(self):
        """同一非法载荷两侧报完全相同的消息 = 只有一份校验实现。"""
        bad = {"exec_at": {"SH": "9:35"}}
        with self.assertRaises(ValueError) as core_error:
            autopipeline.apply_overlay(copy.deepcopy(DEFAULTS), bad)
        with self.assertRaises(WorkbenchError) as api_error:
            settings_api.save_auto_pipeline(self.home, bad)
        self.assertEqual(str(api_error.exception), str(core_error.exception))


class AutoPipelineHttpTests(SettingsBase):
    """路由层：GET 专用路由 + POST 信封（业务失败一律 200 + 信封）。"""

    def test_get_route_returns_effective_config(self):
        with self.client() as client:
            resp = client.get("/api/wb/auto_pipeline")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["value"], DEFAULTS)

    def test_post_then_get_round_trip(self):
        payload = {"enabled": True, "reconcile_at": "20:00"}
        with self.client() as client:
            posted = client.post("/api/wb/auto_pipeline", json=payload)
            fetched = client.post("/api/wb/auto_pipeline", json={})
            via_get = client.get("/api/wb/auto_pipeline")
        self.assertEqual(posted.status_code, 200)
        self.assertTrue(posted.json()["value"]["enabled"])
        self.assertEqual(posted.json()["value"]["reconcile_at"], "20:00")
        # POST 空载荷 = 读（与 GET 同实现）
        self.assertEqual(fetched.json()["value"], posted.json()["value"])
        self.assertEqual(via_get.json()["value"], posted.json()["value"])
        # 读不吃缓存：第二次 GET 仍反映刚写入的值
        self.assertEqual(self.read_platform()["auto_pipeline"], payload)

    def test_post_unknown_field_rejected_without_write(self):
        self.write_platform({"watchlist": {"HK": ["HK.00700"]}})
        before = self.platform_path().read_bytes()
        with self.client() as client:
            resp = client.post("/api/wb/auto_pipeline", json={"enabled": True, "x": 1})
        self.assertEqual(resp.status_code, 200)  # 仓库口径：业务失败 200 + 信封
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertEqual(self.platform_path().read_bytes(), before)

    def test_declared_in_endpoints_and_snapshot(self):
        self.assertIn("auto_pipeline", store_access.endpoints())
        with self.client() as client:
            declared = client.post("/api/wb/snapshot", json={}).json()["value"]["endpoints"]
        self.assertIn("auto_pipeline", declared)

    def test_endpoint_tool_parity_holds(self):
        """R5 对等性：端点工具集 ≡ 端点清单 − 有意排除集（本任务只加排除项）。"""
        endpoints = store_access.endpoints()
        forwarded = {d.endpoint for d in mcp_tools.TOOLS if d.endpoint}
        self.assertEqual(forwarded, set(endpoints) - mcp_tools.MCP_EXCLUDED_ENDPOINTS)


class AutoPipelineSurfaceTests(SettingsBase):
    """工具面边界：开关是人工 Web 动作，模型不可达。"""

    def test_excluded_from_mcp_tool_surface(self):
        names = {tool.name for tool in mcp_tools.TOOLS}
        self.assertIn("auto_pipeline", mcp_tools.MCP_EXCLUDED_ENDPOINTS)
        self.assertNotIn("auto_pipeline", names)
        # 本任务不新增工具（新端点是排除项）；总数随 WP11 任务 3 的 sentiment_history 增至 61
        self.assertEqual(mcp_tools.TOOL_COUNT, 77)
        self.assertEqual(len(mcp_tools.TOOLS), 77)
        # 流程页（读类）仍在工具面——边界只排除「拨开关」
        self.assertIn("pipeline", names)

    def test_settings_like_endpoints_all_excluded(self):
        for name in ("auto_pipeline", "openapi_config", "openapi_test", "openapi_oauth",
                     "confirm-decide"):
            self.assertIn(name, mcp_tools.MCP_EXCLUDED_ENDPOINTS)


class AutoPipelineEndToEndTests(SettingsBase):
    """开关真的改变调度装配（与 WP9 的 build_jobs 同一读路径）。"""

    def auto_job_names(self):
        jobs = autopipeline.build_jobs(str(self.home))
        return {market: [job["name"] for job in chain] for market, chain in jobs.items()}

    def test_disabled_then_enabled_changes_build_jobs(self):
        names = self.auto_job_names()
        # 关闭态：GLOBAL 链只有**基础**作业（日历同步 + 研究入队，均与交易开关解耦——
        # 审查 A-2 / WP18），没有 reconcile 这类 auto_pipeline 派生作业；市场链也没有
        # build_plan/auto_execute
        self.assertEqual(names[autopipeline.GLOBAL_CHAIN],
                         ["sync_calendar", "enqueue_research"])
        self.assertNotIn("reconcile", names[autopipeline.GLOBAL_CHAIN])
        self.assertNotIn("auto_execute", names["SH"])

        with self.client() as client:
            resp = client.post("/api/wb/auto_pipeline", json={"enabled": True})
        self.assertTrue(resp.json()["value"]["enabled"])

        names = self.auto_job_names()
        self.assertIn(autopipeline.GLOBAL_CHAIN, names)
        self.assertIn("reconcile", names[autopipeline.GLOBAL_CHAIN])
        self.assertIn("enqueue_research", names[autopipeline.GLOBAL_CHAIN])
        # 开启态：入队排在 reconcile 之后（同链按时刻排序，digest 先落库）；
        # 日历同步仍在链首（18:50，日历必须先于当日对账/计划就位——WP18）
        global_chain = autopipeline.build_jobs(str(self.home))[autopipeline.GLOBAL_CHAIN]
        self.assertEqual([job["name"] for job in global_chain],
                         ["sync_calendar", "reconcile", "enqueue_research"])
        self.assertIn("build_plan", names["SH"])
        self.assertIn("auto_execute", names["SH"])
        self.assertIn("auto_execute", names["HK"])
        self.assertIn("auto_execute", names["US"])

    def test_write_is_visible_to_core_reader(self):
        settings_api.save_auto_pipeline(self.home, {"enabled": True,
                                                    "exec_at": {"SH": "09:40"}})
        cfg = autopipeline.auto_pipeline_config(str(self.home))
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["exec_at"]["SH"], "09:40")
        # save_futu_channel 与新键互不干扰（同一原子写实现的两个调用方）
        server_config.save_futu_channel(self.home, "openapi")
        raw = self.read_platform()
        self.assertEqual(raw["futu_channel"], "openapi")
        self.assertTrue(raw["auto_pipeline"]["enabled"])


if __name__ == "__main__":
    unittest.main()
