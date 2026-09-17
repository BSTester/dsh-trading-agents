"""WP7 任务 2：``factors-history`` 查询（HTTP 端点 + MCP 工具；工具面在 WP8 任务 6 起 59）。全部离线注入。

* compute：``factors_history(limit)`` 跑 ``python -m trading_core factors-history --limit N``，
  解析与 snapshot_cli 同一出口（parse_stdout：error 键 → ComputeError）；
* app：路由 ``factors-history``——字段白名单 ["limit"]、limit 1..120 缺省 30、
  shape ["snapshots"]、TTL 5m、错误码 trading/core-unavailable（经 caches.cached）；
* MCP：工具 ``factors_history`` 进工具面（任务 2 时 26 → 27），端点工具集 ≡ 端点清单 − 排除集。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import caches, compute, mcp_tools, store_access  # noqa: E402

SNAPSHOTS = [{"date": "2026-09-16", "payload": {"date": "2026-09-16", "tickers": {}},
              "created_at": "2026-09-16 16:15:00"},
             {"date": "2026-09-15", "payload": {"date": "2026-09-15", "tickers": {}},
              "created_at": "2026-09-15 16:15:00"}]


def fake_runner(stdout="", returncode=0, stderr=""):
    """``compute._spawn`` 同签名替身：记录命令行并回固定 stdout（parse_stdout 原样吃）。"""
    calls = []

    def run(command, timeout):
        calls.append((list(command), timeout))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    run.calls = calls
    return run


class ComputeFactorsHistoryTest(unittest.TestCase):
    def test_builds_trading_core_command_and_parses_snapshots(self):
        runner = fake_runner(stdout=json.dumps({"ok": True, "snapshots": SNAPSHOTS}))
        value = compute.factors_history(5, runner=runner)
        self.assertEqual(value, {"ok": True, "snapshots": SNAPSHOTS})
        command, _timeout = runner.calls[0]
        self.assertEqual(command[1:], ["-m", "trading_core", "factors-history", "--limit", "5"])

    def test_default_limit_is_30(self):
        runner = fake_runner(stdout=json.dumps({"ok": True, "snapshots": []}))
        compute.factors_history(runner=runner)
        self.assertIn("--limit", runner.calls[0][0])
        self.assertEqual(runner.calls[0][0][runner.calls[0][0].index("--limit") + 1], "30")

    def test_limit_out_of_range_is_rejected_before_spawn(self):
        runner = fake_runner()
        for bad in (0, 121, -1, "5", True, 1.5):
            with self.assertRaises(compute.ComputeError, msg=bad):
                compute.factors_history(bad, runner=runner)
        self.assertEqual(runner.calls, [], "校验失败不得起子进程")

    def test_cli_error_envelope_maps_to_compute_error(self):
        runner = fake_runner(stdout=json.dumps({"ok": False, "error": "无可用因子数据"}),
                             returncode=1)
        with self.assertRaises(compute.ComputeError) as caught:
            compute.factors_history(runner=runner)
        self.assertIn("无可用因子数据", str(caught.exception))


class FactorsHistoryTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir(parents=True)
        # 缓存目录隔离：caches 是模块级单例（HTTP 与 MCP 共用）
        caches.configure(home=str(self.home))
        self.addCleanup(caches.configure)
        self.runner = fake_runner(stdout=json.dumps({"ok": True, "snapshots": SNAPSHOTS}))
        # 注入 runner 的 core 桥：路由逻辑全真，只有子进程被替身
        self.core = {"factors-history": lambda limit=30: compute.factors_history(
            limit, runner=self.runner)}

    def make_handler(self):
        return app_module.create_handler(str(self.home), analytics={}, series=None,
                                         core=self.core)

    def make_app(self):
        return app_module.create_app(home=str(self.home),
                                     dist=str(self.home.parent / "dist-missing"),
                                     analytics={}, core=self.core)

    def post(self, handler_or_client, payload):
        if isinstance(handler_or_client, TestClient):
            return handler_or_client.post("/api/wb/factors-history", json=payload).json()
        return handler_or_client("factors-history", payload)


class FactorsHistoryRouteTest(FactorsHistoryTestBase):
    def test_route_envelope_carries_snapshots_and_cache_fields(self):
        body = self.post(self.make_handler(), {})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"], {"ok": True, "snapshots": SNAPSHOTS})
        self.assertFalse(body["cached"])
        self.assertIn("cached_at", body)
        self.assertEqual(self.runner.calls[0][0][-1], "30", "缺省 limit 下发 30")

    def test_route_passes_limit_and_dedupes_refresh_marker(self):
        self.post(self.make_handler(), {"limit": 7})
        self.assertEqual(self.runner.calls[0][0][-2:], ["--limit", "7"])

    def test_route_whitelist_rejects_unknown_field(self):
        body = self.post(self.make_handler(), {"limit": 5, "ticker": "SH.600519"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("Unexpected factors-history field", body["error"]["message"])
        self.assertEqual(self.runner.calls, [], "白名单拒绝不得起子进程")

    def test_route_limit_out_of_range_maps_to_core_unavailable(self):
        body = self.post(self.make_handler(), {"limit": 999})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/core-unavailable")
        self.assertIn("Invalid limit (1..120)", body["error"]["message"])

    def test_route_failure_maps_to_core_unavailable(self):
        self.runner = fake_runner(stderr="boom", returncode=1)
        self.core = {"factors-history": lambda limit=30: compute.factors_history(
            limit, runner=self.runner)}
        body = self.post(self.make_handler(), {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/core-unavailable")

    def test_route_shape_mismatch_is_failure_not_cached(self):
        self.runner = fake_runner(stdout=json.dumps({"ok": True, "nope": 1}))
        self.core = {"factors-history": lambda limit=30: compute.factors_history(
            limit, runner=self.runner)}
        body = self.post(self.make_handler(), {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/core-unavailable")
        self.assertIn("载荷不完整", body["error"]["message"])

    def test_route_caches_within_ttl_and_force_refresh_bypasses(self):
        handler = self.make_handler()
        first = self.post(handler, {"limit": 3})
        second = self.post(handler, {"limit": 3})
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"], "TTL 内同载荷应命中缓存")
        self.assertEqual(len(self.runner.calls), 1, "命中缓存不得重起子进程")
        third = handler("factors-history", {"limit": 3, "_refresh": True})
        self.assertTrue(third["ok"])
        self.assertFalse(third["cached"])
        self.assertEqual(len(self.runner.calls), 2, "_refresh 绕过缓存重取")


class FactorsHistoryAppWiringTest(FactorsHistoryTestBase):
    def test_default_core_reaches_factors_history_endpoint(self):
        """默认 core 桥含 factors-history：create_app() 不注入 core 也路由可达（端点在白名单内）。"""
        original = compute.factors_history
        compute.factors_history = lambda limit=30: {"ok": True, "snapshots": SNAPSHOTS}
        self.addCleanup(setattr, compute, "factors_history", original)
        app = app_module.create_app(home=str(self.home),
                                    dist=str(self.home.parent / "dist-missing"),
                                    analytics={})  # core 不注入 → 默认桥
        body = self.post(TestClient(app), {})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"]["snapshots"], SNAPSHOTS)


class FactorsHistoryMcpToolTest(FactorsHistoryTestBase):
    def test_tool_surface_is_27_and_includes_factors_history(self):
        # WP8 任务 6 起 59、WP10 起 60、WP11 起 61、WP12 任务 4 起 74；本用例钉「factors_history 在面内」
        self.assertEqual(mcp_tools.TOOL_COUNT, 77)
        self.assertEqual(len(mcp_tools.TOOLS), 77)
        self.assertIn("factors_history", mcp_tools.TOOL_NAMES)
        self.assertEqual(mcp_tools.ENDPOINT_TOOL_ENDPOINTS["factors_history"],
                         "factors-history")

    def test_endpoint_tool_set_equals_endpoints_minus_excluded(self):
        endpoints = store_access.endpoints()
        forwarded = set(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values())
        self.assertEqual(forwarded, set(endpoints) - mcp_tools.MCP_EXCLUDED_ENDPOINTS)

    def test_tool_dispatch_forwards_payload_to_handle(self):
        handle, calls = None, []

        def recording(endpoint, payload):
            calls.append((endpoint, dict(payload)))
            return {"ok": True, "value": {"ok": True, "snapshots": SNAPSHOTS}}

        handle = recording
        tool = next(t for t in mcp_tools.build_tools(handle, mcp_tools.StoreApi(str(self.home)))
                    if t.name == "factors_history")
        body = mcp_tools.result_payload(tool.call({"limit": 9}))
        self.assertTrue(body["ok"])
        self.assertEqual(calls, [("factors-history", {"limit": 9})])
        refreshed = mcp_tools.result_payload(tool.call({"limit": 9, "refresh": True}))
        self.assertTrue(refreshed["ok"])
        self.assertEqual(calls[-1], ("factors-history", {"limit": 9, "_refresh": True}),
                         "refresh 映射为 _refresh 旁路标记")

    def test_tool_fields_and_description_declare_limit_and_range(self):
        definition = next(t for t in mcp_tools.TOOLS if t.name == "factors_history")
        self.assertEqual(definition.fields, ("limit", "refresh"))
        self.assertEqual([(p.minimum, p.maximum) for p in definition.params if p.name == "limit"],
                         [(1, 120)])
        self.assertIn("服务定时收集", definition.description)


if __name__ == "__main__":
    unittest.main()
