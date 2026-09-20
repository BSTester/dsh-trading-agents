"""``server/v3_ops.py`` 契约测试（V3 运维/通道/治理接口）。

全部离线：注入记录型 ``v3_run`` 替身（不打网络、不依赖运行中的 8397 服务、不读真实
``~/.dsh``——每个用例用临时 home）。覆盖面逐条对任务书：

  * ``/api/v3/metrics``：进程内计数（每次 v3_run 调用累加）、workbenchUp 探测、
    SDK 无数据源如实标注；
  * ``/api/v3/gateway``：真实心跳（schedule 工具）、rules 空数组 + 说明、
    SDK/Headless 通道 ``unavailable`` + reason；
  * ``/api/v3/tools``：清单来自 ``mcp_tools.TOOLS`` 的真实导入枚举（不硬编码）+
    5 个 V3 本地计算工具 ``kind='local'``；
  * ``/api/v3/settings``：**绝不出密钥值**（只报注入与来源；OpenAPI 只报键名；
    futu-token 只报存在性）；
  * ``/api/v3/execution`` + ``/api/v3/oms/*``：风控分级**三态**（risk_passed / manual /
    blocked）、多账户多币种 **NAV 不折算**（NAV=0，保守退回人工确认）、
    与 orders_open 对账命中 → ``submitted``、台账落盘路径；
  * ``/api/v3/events`` / ``/api/v3/audit``：直接透传工具信封（``value`` → ``data``）；
  * ``/api/v3/brain``：decision 只读 ``<home>/v3-strategy-runs.jsonl`` 最后一条，
    读不到即 ``null``；SDK/Headless 无数据源如实标注。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_ops -v``
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import mcp_tools, v3_ops  # noqa: E402

DAY = "2026-09-19"


def base_values(**overrides):
    """一套自洽的假工具面信封（值都是结构清晰的常量，便于逐字段断言）。"""
    values = {
        "snapshot": {"ok": True, "value": {"mode": "sim"}},
        "schedule": {"ok": True, "value": {
            "heartbeat": {"at": "2026-09-19T08:30:00+08:00", "pid": 4242},
            "critical": False, "kill": False, "halt": False,
            "jobs": [{"job": "sync", "ran": "2026-09-19T08:00:00+08:00"},
                     {"job": "eod", "ran": "2026-09-19T16:10:00+08:00"}]}},
        "equity": {"ok": True, "value": {"current": 100000.0, "max_drawdown": -0.03}},
        "positions": {"ok": True, "value": {
            "mode": "sim", "as_of": DAY,
            "groups": [{"acc_id": "SIM-1", "market": "SH",
                        "positions": [{"code": "SH.600000", "market_val": 12000.0}]}]}},
        "orders_open": {"ok": True, "value": {"mode": "sim", "as_of": DAY, "groups": []}},
        "confirmation": {"ok": True, "value": {"pending": [], "ttl_seconds": 300}},
        "plan": {"ok": True, "value": {"plans": [], "alerts": []}},
        "deals_today": {"ok": True, "value": {"mode": "sim", "as_of": DAY, "deals": []}},
        "sources": {"ok": True, "value": {"channels": [{"name": "futu", "ok": True}]}},
        "events": {"ok": True, "value": {"ticker": "SH.600000", "as_of": DAY,
                                         "window_days": 180, "events": []}},
        "audit": {"ok": True, "value": {"chain": [], "alerts": [], "tca": {"rows": []}}},
    }
    values.update(overrides)
    return values


def order(client_order_id, symbol="SH.600000", side="BUY", qty=100, price=10.0,
          status="frozen", **extra):
    row = {"client_order_id": client_order_id, "symbol": symbol, "side": side, "qty": qty,
           "price": price, "status": status, "broker_order_id": None, "risk_verdict": "预检通过"}
    row.update(extra)
    return row


def plan(orders, plan_id="P-1", status="frozen"):
    return {"plan_id": plan_id, "as_of": DAY, "mode": "sim", "strategy_id": "mom_20",
            "target": {}, "content_hash": "h", "status": status, "created_at": DAY,
            "orders": list(orders)}


class FakeWorkbench:
    """记录型 ``v3_run`` 替身：记下每次 (name, payload)，返回预置信封。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        if name not in self.values:
            return {"ok": False, "error": {"code": "v3/unknown-tool",
                                           "message": f"未知工具 {name}"}}
        return self.values[name]

    def payloads(self, name):
        return [payload for call, payload in self.calls if call == name]


class V3OpsTestCase(unittest.TestCase):
    def setUp(self):
        v3_ops.reset_counters()
        self.home = Path(tempfile.mkdtemp(prefix="v3-ops-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.fake = FakeWorkbench(base_values())
        self.app = FastAPI()
        v3_ops.register(self.app, self.fake, str(self.home))
        self.client = TestClient(self.app)

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def sync(self):
        response = self.client.post("/api/v3/oms/sync")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def write(self, name, content):
        path = self.home / name
        path.write_text(content, encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# 1. /api/v3/metrics
# ---------------------------------------------------------------------------
class MetricsTests(V3OpsTestCase):
    def test_shape_counters_and_sdk_honesty(self):
        body = self.get("/api/v3/metrics")
        for key in ("ok", "toolTotal", "toolDomains", "workbenchUp", "mcp", "wb", "http",
                    "oms", "sdk", "generated_at"):
            self.assertIn(key, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["toolTotal"], v3_ops.catalog_total())
        self.assertEqual(body["toolDomains"], 6)
        self.assertTrue(body["workbenchUp"])
        # 计数：每次 v3_run 调用累加；mcp 与 wb 同源
        self.assertEqual(body["mcp"]["calls"], 1)
        self.assertEqual(body["wb"]["calls"], 1)
        self.assertEqual(body["mcp"]["tools"], {"schedule": 1})
        self.assertEqual(body["wb"]["byTool"], {"schedule": 1})
        self.assertEqual(body["http"], {"requests": 1, "errors": 0})
        self.assertEqual(body["sdk"], {"status": "unavailable",
                                       "reason": "本服务未挂载 SDK JSON-RPC 通道"})
        self.assertIsInstance(body["oms"], dict)

        again = self.get("/api/v3/metrics")
        self.assertEqual(again["http"]["requests"], 2)
        self.assertEqual(again["mcp"]["calls"], 2)
        self.assertEqual(again["wb"]["byTool"], {"schedule": 2})

    def test_workbench_down_is_reported_not_faked(self):
        self.fake.values["schedule"] = {"ok": False,
                                        "error": {"code": "trading/core-unavailable",
                                                  "message": "core bridge down"}}
        body = self.get("/api/v3/metrics")
        self.assertFalse(body["workbenchUp"])
        self.assertEqual(body["mcp"]["errors"], 1)

    def test_oms_stage_counts_follow_ledger(self):
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [
            plan([order("CID-1", qty=100, price=10.0), order("CID-2", qty=1000, price=10.0)])]}}
        self.sync()
        body = self.get("/api/v3/metrics")
        self.assertEqual(body["oms"], {"risk_passed": 1, "manual": 1})


# ---------------------------------------------------------------------------
# 2. /api/v3/gateway
# ---------------------------------------------------------------------------
class GatewayTests(V3OpsTestCase):
    def test_channels_scheduler_headless(self):
        body = self.get("/api/v3/gateway")
        self.assertTrue(body["ok"])
        channels = body["channels"]
        self.assertEqual(channels["mcp"]["status"], "running")
        self.assertEqual(channels["mcp"]["tools"], v3_ops.catalog_total())
        self.assertIn("MCP", channels["mcp"]["protocol"])
        self.assertEqual(channels["sdk"]["status"], "unavailable")
        self.assertEqual(channels["sdk"]["reason"], "本服务未挂载 SDK JSON-RPC 通道")
        self.assertEqual(channels["headless"]["status"], "unavailable")
        self.assertIn("Headless", channels["headless"]["reason"])
        self.assertIn("headless", channels["headless"]["command"])

        scheduler = body["scheduler"]
        self.assertEqual(scheduler["rules"], [])
        self.assertIn("rules", scheduler["note"])
        self.assertEqual(scheduler["heartbeat"],
                         {"at": "2026-09-19T08:30:00+08:00", "pid": 4242})
        self.assertEqual(scheduler["recent"],
                         [{"job": "sync", "ran": "2026-09-19T08:00:00+08:00"},
                          {"job": "eod", "ran": "2026-09-19T16:10:00+08:00"}])
        self.assertFalse(scheduler["kill"])
        self.assertFalse(scheduler["halt"])

        # Headless 通道无数据源：零计数、breaker=null、last=[]，绝不编造
        self.assertEqual(body["headless"]["today"],
                         {"total": 0, "success": 0, "failed": 0, "avgMs": 0, "killed": 0})
        self.assertIsNone(body["headless"]["breaker"])
        self.assertEqual(body["headless"]["last"], [])
        self.assertIn("generated_at", body)

    def test_schedule_failure_is_disclosed(self):
        self.fake.values["schedule"] = {"ok": False,
                                        "error": {"code": "trading/core-unavailable",
                                                  "message": "no heartbeat store"}}
        body = self.get("/api/v3/gateway")
        self.assertEqual(body["scheduler"]["heartbeat"], {})
        self.assertEqual(body["scheduler"]["recent"], [])
        self.assertIn("取数失败", body["scheduler"]["note"])
        self.assertIn("core-unavailable", body["scheduler"]["note"])


# ---------------------------------------------------------------------------
# 3. /api/v3/tools（清单来自真实工具面导入枚举）
# ---------------------------------------------------------------------------
class ToolsTests(V3OpsTestCase):
    def test_catalog_comes_from_real_tool_list(self):
        body = self.get("/api/v3/tools")
        self.assertTrue(body["ok"])
        self.assertEqual(list(body["domains"].keys()),
                         ["data", "alpha", "ml", "risk", "execution", "ecosystem"])
        self.assertEqual(body["total"], len(mcp_tools.TOOLS) + len(v3_ops.V3_LOCAL_TOOLS))
        self.assertEqual(body["total"],
                         sum(len(rows) for rows in body["domains"].values()))

        entries = {entry["name"]: entry
                   for rows in body["domains"].values() for entry in rows}
        self.assertEqual(len(entries), body["total"])
        descriptions = {definition.name: definition.description
                        for definition in mcp_tools.TOOLS}
        for definition in mcp_tools.TOOLS:
            entry = entries[definition.name]
            self.assertEqual(entry["kind"], "proxy")
            self.assertEqual(entry["wb"], definition.name)
            self.assertEqual(entry["desc"], descriptions[definition.name])
        # 名字确实来自导入枚举，没有硬编码漂移
        for name in v3_ops.WB_TOOL_NAMES:
            self.assertIn(name, entries)

        for name, domain, _desc in v3_ops.V3_LOCAL_TOOLS:
            self.assertEqual(entries[name]["kind"], "local")
            self.assertIsNone(entries[name]["wb"])
            self.assertIn(entries[name], body["domains"][domain])
        self.assertEqual(sorted(entries),
                         sorted(list(v3_ops.WB_TOOL_NAMES)
                                + [name for name, _d, _x in v3_ops.V3_LOCAL_TOOLS]))

    def test_domain_filter_and_bad_domain(self):
        body = self.get("/api/v3/tools?domain=ml")
        self.assertTrue(body["ok"])
        self.assertEqual(list(body["domains"].keys()), ["ml"])
        self.assertEqual(body["total"], len(body["domains"]["ml"]))
        self.assertTrue(all(row["kind"] == "local" for row in body["domains"]["ml"]))

        bad = self.get("/api/v3/tools?domain=nope")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "v3/bad-request")


# ---------------------------------------------------------------------------
# 4. /api/v3/settings（绝不出密钥）
# ---------------------------------------------------------------------------
class SettingsTests(V3OpsTestCase):
    SECRET_KEY_PEM = "-----BEGIN PRIVATE KEY-----SUPERSECRETPEM-----END PRIVATE KEY-----"
    SECRET_API_KEY = "sk-deepseek-must-never-leak"
    SECRET_TUSHARE = "tushare-token-must-never-leak"
    SECRET_BEARER = "futu-mcp-bearer-must-never-leak"

    def prepare(self):
        self.write("trading-platform.json", json.dumps({"futu_channel": "openapi",
                                                        "service": {"token": "svc-secret"}}))
        self.write("futu-openapi.json", json.dumps({
            "mode": "appkey", "app_key": "APPKEY-VALUE-MUST-NOT-LEAK",
            "algorithm": "Ed25519", "private_key_pem": self.SECRET_KEY_PEM,
            "private_key_path": "/tmp/nope.pem"}))
        self.write("futu-token", self.SECRET_BEARER)
        self.write("futu-token-expiry", "2026-12-31T23:59:59+08:00")

    def test_settings_never_leaks_secret_values(self):
        self.prepare()
        environ = {"DEEPSEEK_API_KEY": self.SECRET_API_KEY, "TUSHARE_TOKEN": self.SECRET_TUSHARE}
        with unittest.mock.patch.dict(os.environ, environ, clear=True):
            response = self.client.get("/api/v3/settings")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            text = response.text
        self.assertTrue(body["ok"])

        for secret in (self.SECRET_KEY_PEM, self.SECRET_API_KEY, self.SECRET_TUSHARE,
                       self.SECRET_BEARER, "APPKEY-VALUE-MUST-NOT-LEAK", "svc-secret"):
            self.assertNotIn(secret, text)

        # futu：只读元信息（channel / token 是否存在与有效期 / OpenAPI 的键名）
        self.assertEqual(body["futu"]["channel"], "openapi")
        self.assertEqual(body["futu"]["mcp_bearer"],
                         {"present": True, "expiry": "2026-12-31T23:59:59+08:00"})
        self.assertEqual(body["futu"]["openapi"]["mode"], "appkey")
        self.assertEqual(body["futu"]["openapi"]["config_keys"],
                         ["algorithm", "app_key", "mode", "private_key_path",
                          "private_key_pem"])

        # env：只有 key/injected/source 三个字段，没有值字段
        self.assertEqual([row["key"] for row in body["env"]], list(v3_ops.ENV_KEYS))
        for row in body["env"]:
            self.assertEqual(sorted(row.keys()), ["injected", "key", "source"])
            self.assertIsInstance(row["injected"], bool)
            self.assertIsInstance(row["source"], str)
        by_key = {row["key"]: row for row in body["env"]}
        self.assertEqual(by_key["DEEPSEEK_API_KEY"],
                         {"key": "DEEPSEEK_API_KEY", "injected": True, "source": "环境变量"})
        self.assertEqual(by_key["QUANT_MCP_CWD"],
                         {"key": "QUANT_MCP_CWD", "injected": False,
                          "source": "环境变量（未注入）"})
        self.assertEqual(by_key["DSH_HOME"],
                         {"key": "DSH_HOME", "injected": True, "source": "默认（DSH_HOME）"})

    def test_trading_mode_and_data_sources(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            body = self.get("/api/v3/settings")
        self.assertEqual(body["trading_mode"], "sim")  # 模式文件缺失 = sim（服务口径）
        self.assertIn("switch_mode", body["mode_note"])
        names = [row["name"] for row in body["data_sources"]]
        self.assertEqual(len(names), 5)
        self.assertTrue(any("workbench" in name for name in names))
        edgar = [row for row in body["data_sources"] if "SEC EDGAR" in row["name"]][0]
        self.assertFalse(edgar["available"])
        self.assertIn("无数据源", edgar["detail"])

    def test_trading_mode_reads_service_mode_file(self):
        # 服务真实模式 = 模式文件 trading-account-mode（store_access.read_mode 的口径）
        self.write("trading-account-mode", "live")
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            body = self.get("/api/v3/settings")
        self.assertEqual(body["trading_mode"], "live")

        self.write("trading-account-mode", "not-a-mode")
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            broken = self.get("/api/v3/settings")
        self.assertEqual(broken["trading_mode"], "unknown")  # 绝不静默回退成 sim
        self.assertIn("模式文件读取失败", broken["mode_note"])


# ---------------------------------------------------------------------------
# 5/6. 执行 + OMS 台账（分级三态 / NAV 口径 / 对账 / 落盘）
# ---------------------------------------------------------------------------
class ExecutionTests(V3OpsTestCase):
    def test_execution_passthrough_and_oms_view(self):
        body = self.get("/api/v3/execution")
        self.assertTrue(body["ok"])
        self.assertEqual(body["positions"], self.fake.values["positions"]["value"])
        self.assertEqual(body["orders_open"], self.fake.values["orders_open"]["value"])
        self.assertEqual(body["deals_today"], self.fake.values["deals_today"]["value"])
        oms = body["oms"]
        for key in ("note", "confirmation", "nav", "nav_source", "stages", "orders"):
            self.assertIn(key, oms)
        self.assertIn("工作台 Web", oms["note"])
        self.assertIn("plan_execute", oms["note"])
        self.assertEqual(oms["confirmation"], {"pending": [], "ttl_seconds": 300})
        self.assertEqual(oms["nav"], 100000.0)
        self.assertEqual(oms["nav_source"], "sim-ledger(equity.current)")
        self.assertEqual(body["errors"], [])

    def test_execution_reports_tool_errors_without_faking(self):
        self.fake.values["positions"] = {"ok": False,
                                         "error": {"code": "trading/openapi-unavailable",
                                                   "message": "需要 OpenAPI 交易通道"}}
        body = self.get("/api/v3/execution")
        self.assertIsNone(body["positions"])
        self.assertEqual([row["tool"] for row in body["errors"]], ["positions"])

    def test_nav_falls_back_to_single_account_positions(self):
        self.fake.values["equity"] = {"ok": False,
                                      "error": {"code": "trading/analytics-unavailable",
                                                "message": "no ledger"}}
        body = self.get("/api/v3/oms/orders")
        self.assertEqual(body["nav"], 12000.0)
        self.assertIn("positions(单账户 SIM-1", body["nav_source"])

    def test_multi_account_nav_is_not_converted(self):
        """多账户（多币种）不折算：NAV=0，且订单保守退回人工确认。"""
        self.fake.values["equity"] = {"ok": False,
                                      "error": {"code": "trading/analytics-unavailable",
                                                "message": "no ledger"}}
        self.fake.values["positions"] = {"ok": True, "value": {"mode": "sim", "groups": [
            {"acc_id": "SIM-1", "market": "SH",
             "positions": [{"code": "SH.600000", "market_val": 80000.0}]},
            {"acc_id": "SIM-2", "market": "HK",
             "positions": [{"code": "HK.00700", "market_val": 20000.0}]},
        ]}}
        view = self.get("/api/v3/oms/orders")
        self.assertEqual(view["nav"], 0)
        self.assertIn("multi-account(2 个账户/多币种) 不折算", view["nav_source"])

        self.fake.values["plan"] = {"ok": True, "value": {"plans": [plan([order("CID-M")])]}}
        synced = self.sync()
        self.assertTrue(synced["ok"])
        self.assertEqual(synced["nav"], 0)
        self.assertEqual(synced["stages"], {"manual": 1})

        orders = self.get("/api/v3/oms/orders")["orders"]
        self.assertEqual(orders[0]["stage"], "manual")
        self.assertEqual(orders[0]["risk"]["action"], "manual")
        self.assertTrue(any("NAV 不可用" in reason for reason in orders[0]["risk"]["reasons"]))
        self.assertEqual(orders[0]["nav_used"], 0)

    def test_risk_grading_three_states(self):
        """三态：阈值内 → risk_passed(auto)；单笔 >2% → manual；回撤 ≥15% → blocked。"""
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [plan([
            order("CID-SMALL", qty=100, price=10.0),      # 1000 / 100000 = 1% → auto
            order("CID-BIG", qty=2000, price=10.0),       # 20000 / 100000 = 20% → manual
        ])]}}
        synced = self.sync()
        self.assertTrue(synced["ok"])
        self.assertEqual(synced["orders"], 2)
        self.assertEqual(synced["stages"], {"risk_passed": 1, "manual": 1})
        by_id = {row["id"]: row for row in self.get("/api/v3/oms/orders")["orders"]}
        self.assertEqual(by_id["CID-SMALL"]["risk"]["action"], "auto")
        self.assertEqual(by_id["CID-SMALL"]["stage"], "risk_passed")
        self.assertEqual(by_id["CID-BIG"]["risk"]["action"], "manual")
        self.assertEqual(by_id["CID-BIG"]["stage"], "manual")
        self.assertEqual(by_id["CID-BIG"]["risk"]["reasons"],
                         ["单笔占比 20.00% > 2%，需人工确认"])
        self.assertEqual(by_id["CID-BIG"]["industry_source"], v3_ops.INDUSTRY_SOURCE)

        # 回撤触及 15% 红线 → 全部 blocked（红线优先，覆盖单笔结论）
        self.fake.values["equity"] = {"ok": True,
                                      "value": {"current": 100000.0, "max_drawdown": -0.18}}
        synced = self.sync()
        self.assertEqual(synced["stages"], {"blocked": 2})
        by_id = {row["id"]: row for row in self.get("/api/v3/oms/orders")["orders"]}
        self.assertTrue(all(row["risk"]["action"] == "blocked" for row in by_id.values()))
        self.assertTrue(any("回撤 18.0% 触及 15% 红线" in reason
                            for reason in by_id["CID-SMALL"]["risk"]["reasons"]))
        self.assertEqual(by_id["CID-SMALL"]["drawdown_used"], 18.0)

    def test_reconcile_marks_submitted_and_persists_ledger(self):
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [plan([
            order("CID-OPEN"), order("CID-IDLE", symbol="SZ.000001")])]}}
        self.fake.values["orders_open"] = {"ok": True, "value": {
            "mode": "sim", "groups": [{"acc_id": "SIM-1", "market": "SH", "rows": [
                {"order_id": "9001", "client_order_id": "CID-OPEN", "code": "SH.600000",
                 "trd_side": "BUY", "qty": 100, "price": 10.0}]}]}}
        synced = self.sync()
        self.assertTrue(synced["ok"])
        by_id = {row["id"]: row for row in self.get("/api/v3/oms/orders")["orders"]}
        self.assertEqual(by_id["CID-OPEN"]["stage"], "submitted")
        self.assertTrue(by_id["CID-OPEN"]["open_hit"])
        self.assertEqual(by_id["CID-IDLE"]["stage"], "risk_passed")

        # 落盘：orders.json（台账）+ sync.jsonl（对账留痕）
        ledger_path = self.home / v3_ops.OMS_FILENAME
        self.assertTrue(ledger_path.is_file())
        stored = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 1)
        self.assertEqual(sorted(stored["orders"]), ["CID-IDLE", "CID-OPEN"])
        self.assertEqual(stored["orders"]["CID-OPEN"]["stage"], "submitted")
        self.assertEqual(stored["orders"]["CID-OPEN"]["plan_id"], "P-1")
        self.assertEqual(stored["orders"]["CID-OPEN"]["ticker"], "SH.600000")
        self.assertIn("risk", stored["orders"]["CID-OPEN"])
        self.assertTrue((self.home / v3_ops.OMS_SYNC_FILENAME).is_file())

        # 二次对账：历史累积、first_seen_at 不变、状态可演进
        before = stored["orders"]["CID-OPEN"]["first_seen_at"]
        self.fake.values["orders_open"] = {"ok": True, "value": {"groups": []}}
        self.sync()
        after = json.loads(ledger_path.read_text(encoding="utf-8"))["orders"]["CID-OPEN"]
        self.assertEqual(after["first_seen_at"], before)
        self.assertEqual(after["stage"], "risk_passed")
        self.assertEqual(len(after["history"]), 2)
        self.assertEqual([row["stage"] for row in after["history"]],
                         ["submitted", "risk_passed"])

    def test_sync_reports_plan_failure(self):
        self.fake.values["plan"] = {"ok": False,
                                    "error": {"code": "trading/core-unavailable",
                                              "message": "core bridge unavailable"}}
        body = self.sync()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/core-unavailable")
        self.assertFalse((self.home / v3_ops.OMS_FILENAME).exists())

    def test_sync_registers_frozen_orders_from_plan_tool(self):
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [
            plan([order("CID-A")], plan_id="P-frozen", status="frozen"),
            plan([order("CID-B", status="filled")], plan_id="P-filled", status="executed"),
        ]}}
        self.sync()
        by_id = {row["id"]: row for row in self.get("/api/v3/oms/orders")["orders"]}
        self.assertEqual(by_id["CID-A"]["plan_status"], "frozen")
        self.assertEqual(by_id["CID-B"]["plan_status"], "executed")
        self.assertEqual(by_id["CID-B"]["stage"], "filled")


# ---------------------------------------------------------------------------
# 7/8/9. events / audit / brain
# ---------------------------------------------------------------------------
class ReportingTests(V3OpsTestCase):
    def test_events_passthrough_with_window_mapping(self):
        body = self.get("/api/v3/events?ticker=SH.600000&window=180")
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"], self.fake.values["events"]["value"])
        self.assertEqual(self.fake.calls[-1], ("events", {"ticker": "SH.600000", "days": 180}))

    def test_events_error_envelope_passthrough(self):
        self.fake.values["events"] = {"ok": False,
                                      "error": {"code": "trading/analytics-unavailable",
                                                "message": "akshare 缺失"}}
        body = self.get("/api/v3/events?ticker=SH.600000")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/analytics-unavailable")
        self.assertNotIn("data", body)

    def test_events_requires_ticker(self):
        body = self.get("/api/v3/events")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "v3/bad-request")
        self.assertEqual(self.fake.payloads("events"), [])

    def test_audit_passthrough_and_unsupported_window_dropped(self):
        audit_fields = {param.name for definition in mcp_tools.TOOLS
                        if definition.name == "audit" for param in definition.params}
        self.assertNotIn("window", audit_fields)  # 工具面确实没有 window（只读事实，不是假设）
        body = self.get("/api/v3/audit?window=120")
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"], self.fake.values["audit"]["value"])
        # audit 工具面无 window 字段（非空载荷会被 handle 拒绝）→ 不下传
        self.assertEqual(self.fake.calls[-1], ("audit", {}))

    def test_brain_without_strategy_runs(self):
        body = self.get("/api/v3/brain")
        self.assertTrue(body["ok"])
        self.assertIsNone(body["decision"])
        self.assertEqual(body["headless"]["today"],
                         {"total": 0, "success": 0, "failed": 0, "avgMs": 0, "killed": 0})
        self.assertIsNone(body["headless"]["breaker"])
        self.assertEqual(body["headless"]["last"], [])
        self.assertEqual(body["sdk"], {"status": "unavailable",
                                       "reason": "本服务未挂载 SDK JSON-RPC 通道",
                                       "serverInfo": None, "route": None, "lastTurn": None,
                                       "turns": [], "events": []})
        self.assertIn("无数据源", body["sources"]["decision"])
        self.assertIn("v3-strategy-runs.jsonl", body["sources"]["decision"])
        self.assertTrue(body["sources"]["workbench"]["ok"])

    def test_brain_reads_last_strategy_run_line(self):
        first = {"run_id": "r-1", "at": DAY, "proposals": [{"ticker": "SH.600000"}]}
        last = {"run_id": "r-2", "at": DAY, "proposals": [], "top_n": 2}
        self.write(v3_ops.STRATEGY_RUNS_FILENAME,
                   json.dumps(first, ensure_ascii=False) + "\n"
                   + "\n"  # 空行要被跳过
                   + json.dumps(last, ensure_ascii=False) + "\n")
        body = self.get("/api/v3/brain")
        self.assertEqual(body["decision"], last)
        self.assertIn(v3_ops.STRATEGY_RUNS_FILENAME, body["sources"]["decision"])

    def test_brain_ignores_corrupt_jsonl_lines(self):
        self.write(v3_ops.STRATEGY_RUNS_FILENAME,
                   json.dumps({"run_id": "r-1"}) + "\n" + "{not json}\n")
        body = self.get("/api/v3/brain")
        self.assertEqual(body["decision"], {"run_id": "r-1"})


# ---------------------------------------------------------------------------
# market= 过滤（execution / oms/orders / brain）
# ---------------------------------------------------------------------------
class MarketScopeTests(V3OpsTestCase):
    """``?market=`` 只挑分组/台账，不改既有字段；未知市场标识如实计数不猜。"""

    def setUp(self):
        super().setUp()
        from server import v3_universe

        v3_universe.clear_cache()
        self.fake.values["positions"] = {"ok": True, "value": {
            "mode": "sim", "as_of": DAY, "source": "futu/sim_trade_position_list",
            "groups": [
                {"acc_id": "HK-1", "market": 1, "kind": "simulated",
                 "positions": [{"symbol": "00700", "market_val": 100.0}]},
                {"acc_id": "SH-1", "market": 3, "kind": "simulated",
                 "positions": [{"symbol": "600000", "market_val": 200.0}]},
                {"acc_id": "US-1", "market": 100, "kind": "simulated",
                 "positions": [{"symbol": "NVDA", "market_val": 300.0}]},
                {"acc_id": "OPT-1", "market": 9, "kind": "simulated",
                 "positions": [{"symbol": "00700", "market_val": 400.0}]}]}}
        self.fake.values["orders_open"] = {"ok": True, "value": {
            "mode": "sim", "as_of": DAY, "groups": [
                {"acc_id": "HK-1", "market": "HK", "rows": [{"code": "HK.00700"}]},
                {"acc_id": "SH-1", "market": "SH", "rows": [{"code": "SH.600000"}]},
                {"acc_id": "OTHER", "market": "9", "rows": [{"code": "SG.X"}]}]}}
        self.fake.values["deals_today"] = {"ok": True, "value": {
            "mode": "sim", "as_of": DAY, "groups": [
                {"acc_id": "HK-1", "market": "HK", "rows": [{"code": "HK.00700"}]}]}}

    def test_execution_filters_groups_by_market(self):
        body = self.get("/api/v3/execution?market=HK")
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["market"], "HK")
        self.assertEqual([group["acc_id"] for group in body["positions"]["groups"]], ["HK-1"])
        self.assertEqual([group["acc_id"] for group in body["orders_open"]["groups"]], ["HK-1"])
        self.assertEqual([group["acc_id"] for group in body["deals_today"]["groups"]], ["HK-1"])
        stats = body["filter"]
        self.assertEqual(stats["positions"]["keptGroups"], 1)
        self.assertEqual(stats["positions"]["otherMarketGroups"], 2)
        self.assertEqual(stats["positions"]["unknownMarketGroups"], 1,
                         "market_id=9（港期权）不猜成港股，如实计入未知")
        self.assertEqual(stats["positions"]["unknownMarkets"], [9])
        self.assertEqual(stats["orders_open"]["unknownMarkets"], ["9"])
        self.assertIn("不跨市场合并", stats["positions"]["note"])

    def test_execution_without_market_is_untouched(self):
        body = self.get("/api/v3/execution")
        self.assertEqual(body["positions"], self.fake.values["positions"]["value"],
                         "不传 market 时 value 原样透传（既有字段一字不改）")
        self.assertNotIn("market", body)
        self.assertIsNone(body["oms"]["market"])

    def test_execution_bad_market(self):
        body = self.get("/api/v3/execution?market=MARS")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "market/bad-market")

    def test_oms_orders_filter_the_ledger_and_the_stage_counts(self):
        v3_ops.OmsLedger(self.fake, self.home).write({
            "A": {"id": "A", "ticker": "SZ.002716", "stage": "manual", "updated_at": "3"},
            "B": {"id": "B", "ticker": "HK.00700", "stage": "risk_passed", "updated_at": "2"},
            "C": {"id": "C", "ticker": "US.NVDA", "stage": "blocked", "updated_at": "1"},
            "D": {"id": "D", "ticker": "600000", "stage": "manual", "updated_at": "0"},
        })
        body = self.get("/api/v3/oms/orders?market=SH")
        self.assertEqual([row["id"] for row in body["orders"]], ["A"],
                         "SZ.002716 属 A 股口径（SH）；裸代码无从判断 → 不计入")
        self.assertEqual(body["stages"], {"manual": 1}, "stages 随过滤同步")
        self.assertEqual(body["filter"]["keptOrders"], 1)
        self.assertEqual(body["filter"]["unattributedOrders"], 1)
        self.assertEqual(body["filter"]["prefixes"], {"SH": 1, "HK": 1, "US": 1, "unknown": 1})

        untouched = self.get("/api/v3/oms/orders")
        self.assertEqual(sorted(row["id"] for row in untouched["orders"]),
                         ["A", "B", "C", "D"])
        self.assertEqual(untouched["stages"], {"manual": 2, "risk_passed": 1, "blocked": 1})
        self.assertIsNone(untouched["market"])
        self.assertNotIn("filter", untouched)

    def test_brain_filters_decision_by_run_market(self):
        self.write(v3_ops.STRATEGY_RUNS_FILENAME,
                   json.dumps({"run_id": "sh", "market": "SH"}) + "\n"
                   + json.dumps({"run_id": "hk", "market": "HK"}) + "\n")
        self.assertEqual(self.get("/api/v3/brain?market=SH")["decision"]["run_id"], "sh")
        self.assertEqual(self.get("/api/v3/brain?market=SH")["decisionMarket"], "SH")
        self.assertEqual(self.get("/api/v3/brain")["decision"]["run_id"], "hk",
                         "不传 market 时仍取最后一条（与历史一致）")
        missing = self.get("/api/v3/brain?market=US")
        self.assertIsNone(missing["decision"])
        self.assertIn("没有 market=US", missing["sources"]["decision"])
        bad = self.get("/api/v3/brain?market=MARS")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "market/bad-market")

    def test_brain_keeps_legacy_runs_visible_with_a_null_market(self):
        self.write(v3_ops.STRATEGY_RUNS_FILENAME,
                   json.dumps({"run_id": "legacy-1"}) + "\n"
                   + json.dumps({"run_id": "legacy-2"}) + "\n")
        body = self.get("/api/v3/brain?market=SH")
        self.assertEqual(body["decision"]["run_id"], "legacy-2",
                         "旧记录没有 market 字段 → 不按市场隐藏")
        self.assertIsNone(body["decisionMarket"], "旧记录如实标注 market=null")
        self.assertIn("没有任何带 market 标注的记录", body["sources"]["decision"])

    def test_filter_helpers_report_unknown_markets(self):
        value = {"groups": [{"acc_id": "X", "market": "FU", "rows": [{"code": "SG.X"}]},
                            {"acc_id": "Y", "market": "HK", "rows": [{"code": "HK.00700"}]}]}
        filtered, stats = v3_ops.filter_grouped_value(value, "HK")
        self.assertEqual([group["acc_id"] for group in filtered["groups"]], ["Y"])
        self.assertEqual(stats["unknownMarkets"], ["FU"])
        self.assertEqual(stats["excludedRows"], 1)
        self.assertEqual(value["groups"][0]["acc_id"], "X", "原值不被就地修改")

        plan_value = {"plans": [{"plan_id": "P1", "target": {"SH.600000": 1, "SH.600009": 1}},
                                {"plan_id": "P2", "target": {"HK.00700": 1, "HK.00981": 1}},
                                {"plan_id": "P3"}]}
        kept, plan_stats = v3_ops.filter_plan_value(plan_value, "SH")
        self.assertEqual([plan["plan_id"] for plan in kept["plans"]], ["P1", "P3"],
                         "无 target 的计划不是市场相关条目 → 保留")
        self.assertEqual(plan_stats["otherMarketPlans"], 1)
        self.assertEqual(plan_stats["noTargetPlans"], 1)


# ---------------------------------------------------------------------------
# 纯函数（分级 / NAV 口径）
# ---------------------------------------------------------------------------
class UnitTests(unittest.TestCase):
    def test_check_order_thresholds(self):
        self.assertEqual(v3_ops.check_order(1000.0, 100000.0)[0], "auto")
        action, reasons = v3_ops.check_order(10000.0, 100000.0)
        self.assertEqual(action, "manual")
        self.assertIn("单笔占比 10.00%", reasons[0])
        self.assertEqual(v3_ops.check_order(1000.0, 0)[0], "manual")
        self.assertEqual(v3_ops.check_order(1000.0, 100000.0, drawdown_pct=15.0)[0], "blocked")
        self.assertEqual(v3_ops.check_order(1000.0, 100000.0, industry_pct=25.0)[0], "blocked")
        # 红线优先于单笔结论：blocked 覆盖 manual
        self.assertEqual(v3_ops.check_order(50000.0, 100000.0, drawdown_pct=20.0)[0], "blocked")

    def test_positions_nav_single_currency_only(self):
        single = {"ok": True, "value": {"groups": [{"acc_id": "A", "market": "SH", "positions": [
            {"code": "SH.600000", "market_val": "1000.5"},
            {"code": "SZ.000001", "market_val": 2000.5}]}]}}
        nav, source = v3_ops.positions_nav(single)
        self.assertEqual(nav, 3001.0)
        self.assertIn("单账户 A", source)

        multi_currency = {"ok": True, "value": {"groups": [{"acc_id": "A", "positions": [
            {"code": "SH.600000", "market_val": 1000}, {"code": "HK.00700",
                                                        "market_val": 2000}]}]}}
        nav, source = v3_ops.positions_nav(multi_currency)
        self.assertEqual(nav, 0)
        self.assertIn("multi-currency", source)

        self.assertEqual(v3_ops.positions_nav({"ok": False, "error": {}})[0], 0)

    def test_open_hit_matches_identifier_and_triple(self):
        rows = [{"code": "SH.600000", "trd_side": "buy", "qty": "100", "order_id": "1"}]
        self.assertTrue(v3_ops.open_hit(rows, "CID-9", "SH.600000", "BUY", 100))
        self.assertFalse(v3_ops.open_hit(rows, "CID-9", "SH.600000", "SELL", 100))
        self.assertTrue(v3_ops.open_hit([{"remark": "CID-9"}], "CID-9", "SZ.000001", "BUY", 1))

    def test_domain_of_matches_reference_rules(self):
        self.assertEqual(v3_ops.domain_of("switch_mode"), "execution")
        self.assertEqual(v3_ops.domain_of("plan_execute"), "execution")
        self.assertEqual(v3_ops.domain_of("series"), "data")
        self.assertEqual(v3_ops.domain_of("ic"), "alpha")
        self.assertEqual(v3_ops.domain_of("equity"), "risk")
        self.assertEqual(v3_ops.domain_of("orders_open"), "execution")
        self.assertEqual(v3_ops.domain_of("reconcile"), "ecosystem")

    def test_catalog_entries_are_imported_not_hardcoded(self):
        catalog = v3_ops.build_catalog()
        names = {row["name"] for rows in catalog.values() for row in rows}
        self.assertTrue(set(v3_ops.WB_TOOL_NAMES) <= names)
        self.assertEqual(len(v3_ops.WB_TOOL_NAMES), len(mcp_tools.TOOLS))
        self.assertEqual(v3_ops.catalog_total(catalog), len(mcp_tools.TOOLS) + 5)


if __name__ == "__main__":
    unittest.main()
