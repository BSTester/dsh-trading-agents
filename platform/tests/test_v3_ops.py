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
import copy
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import mcp_tools, observability, v3_db, v3_ops  # noqa: E402

DAY = "2026-09-19"
PROBE_AS_OF = "2026-09-20T12:00:00+00:00"


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


class FakeHttpProbe:
    """数据源**能力探测**的离线替身（默认端点可达；可切成失败）。

    为什么必须有这个替身：数据源可用性的判据已从「包能不能 import」改成「真发一次 HTTP
    探测 / 认最近一次真实调用」——单测绝不能因此真的打上游（否则用例变成有网才绿）。
    """

    def __init__(self, reachable=True, evidence=None, status=200):
        self.reachable = bool(reachable)
        self.status = status
        self.evidence = evidence
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        ok = self.reachable and self.status is not None and 200 <= self.status < 400
        if self.evidence is not None:
            evidence = self.evidence
        elif not self.reachable:
            evidence = "URLError: <urlopen error [Errno -3] Temporary failure in name resolution>"
        elif ok:
            evidence = f"HTTP {self.status}（GET {url}，首段 4096 字节）"
        else:
            evidence = f"HTTP {self.status}（GET {url}）——端点可达但返回了错误码"
        return {"ok": ok,
                "reachable": self.reachable,
                "status": self.status,
                "evidence": evidence,
                "as_of": PROBE_AS_OF}


class FakeToolManager:
    def __init__(self, names):
        self.names = list(names)

    def list_tools(self):
        return [SimpleNamespace(name=name) for name in self.names]


class FakeMcpServer:
    """MCP SDK ``MCPServer`` 的最小替身：只需要注册表能被同步读出来。"""

    def __init__(self, names):
        self._tool_manager = FakeToolManager(names)


class V3OpsTestCase(unittest.TestCase):
    def setUp(self):
        v3_ops.reset_counters()
        v3_ops.reset_source_probes()  # 能力探测有 TTL 进程内缓存：用例之间必须清干净
        self.home = Path(tempfile.mkdtemp(prefix="v3-ops-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.fake = FakeWorkbench(base_values())
        self.http_probe = FakeHttpProbe()
        self.app = FastAPI()
        v3_ops.register(self.app, self.fake, str(self.home),
                        deps={"http_probe": self.http_probe})
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
                    "oms", "sdk", "generated_at", "futu"):
            self.assertIn(key, body)
        for key in ("enabled", "calls", "coalesced", "retries", "rateLimited",
                    "throttleWaitMs", "cooldownUntil", "inFlight", "queued"):
            self.assertIn(key, body["futu"], "metrics.futu 是限流治理的真实读数口")
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
        self.assertEqual(body["sdk"]["status"], "unavailable")
        self.assertIn("/api/v3/sdk/status", body["sdk"]["reason"])
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
        # 工具数**不锁常量**：既有 ``tools`` 与新增 ``tools_total`` 都必须等于**运行时**
        # 读出的 MCP 工具面（注册表长度随 app.py 的装配变化：77 → 116），而
        # ``tools_domain_catalog`` 是六域目录（77+5）。锁死 82/116 会在下次装配变更时假绿。
        surface = v3_ops.mcp_tool_surface(self.app)
        self.assertEqual(channels["mcp"]["tools"], surface["mcp_total"])
        self.assertEqual(channels["mcp"]["tools_total"], surface["mcp_total"])
        self.assertEqual(channels["mcp"]["tools_domain_catalog"], v3_ops.catalog_total())
        self.assertEqual(channels["mcp"]["tools_bridge"], surface["bridge"])
        self.assertIn("mcp_tools.TOOLS", channels["mcp"]["tools_source"])
        self.assertIn("MCP", channels["mcp"]["protocol"])
        self.assertEqual(channels["sdk"]["status"], "unavailable")
        self.assertIn("/api/v3/sdk/status", channels["sdk"]["reason"])
        self.assertIn("v3_sdk", channels["sdk"]["reason"])
        # Headless 通道**已实现**（server/v3_headless.py）：status 说的是「本进程有没有
        # 装配它」，reason 不得再出现「未挂载/不可用/未实现」这类失效断言。
        self.assertEqual(channels["headless"]["status"], "implemented-not-registered")
        self.assertIs(channels["headless"]["registered"], False)
        self.assertIn("已实现", channels["headless"]["reason"])
        self.assertNotIn("未挂载", channels["headless"]["reason"])
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

        # Headless 通道读数**全部来自真实表/真实配置**：today 是当日口径统计（临时 home 的
        # 库里没有记录 → 0，但字段面与真值同形），breaker 取 v3_headless 的熔断参数。
        today = body["headless"]["today"]
        self.assertEqual(today["total"], 0)
        self.assertEqual(today["success"], 0)
        self.assertEqual(today["failed"], 0)
        self.assertEqual(today["killed"], 0)
        self.assertEqual(today["avgMs"], 0)
        self.assertEqual(today["tokensEstimate"], 0)
        self.assertTrue(str(today["date"]).startswith("20"))
        self.assertIn("headless_log", body["headless"]["logNote"])
        self.assertIn("UTC", today["basis"])
        breaker = body["headless"]["breaker"]
        self.assertIsNotNone(breaker, "熔断参数来自 v3_headless 真实配置，不是 null")
        for key in ("maxConcurrency", "timeoutSeconds", "tokenBudget", "enabled", "profile"):
            self.assertIn(key, breaker)
        self.assertEqual(body["headless"]["last"], [])
        self.assertEqual(body["headless"]["status"], "implemented-not-registered")
        self.assertIsNone(body["headless"]["schedulerAlive"])
        self.assertIsNone(body["headless"]["nextFireTimes"])
        # 「已实现」的断言：源码里必须真有 spawn 点（不是文案）
        self.assertIn("headless", body["headless"]["source"])
        self.assertIn("generated_at", body)

    def test_mcp_tool_total_tracks_live_registry(self):
        """口径必须是**动态真值**：注册表里有多少就报多少（绝不写死 82 / 116）。"""
        names = [f"wb_{index}" for index in range(7)] + [f"v3_{index}" for index in range(3)]
        self.app.state.mcp = FakeMcpServer(names)
        self.app.state.mcp_tools = names[:7]
        self.app.state.v3_mcp_tools = names[7:]
        mcp = self.get("/api/v3/gateway")["channels"]["mcp"]
        self.assertEqual(mcp["tools_total"], 10)
        self.assertEqual(mcp["tools"], 10)
        self.assertEqual(mcp["tools_bridge"], 3)
        self.assertEqual(mcp["tools_domain_catalog"], v3_ops.catalog_total())
        self.assertNotEqual(mcp["tools_total"], mcp["tools_domain_catalog"],
                            "两个口径本来就不同（注册表 vs 六域目录），必须分开报")
        self.assertIn("list_tools", mcp["tools_source"])

        # 注册表变长 → 读数跟着变（证明来源是注册表而不是常量）
        self.app.state.mcp = FakeMcpServer(names + ["v3_extra"])
        again = self.get("/api/v3/gateway")["channels"]["mcp"]
        self.assertEqual(again["tools_total"], 11)
        self.assertEqual(again["tools"], 11)

    def test_mcp_tool_total_falls_back_to_binding_lists(self):
        """注册表不可读时退到 app.py 的两份装配名单，并在 source 里如实标注。"""
        self.app.state.mcp_tools = [f"wb_{index}" for index in range(77)]
        self.app.state.v3_mcp_tools = [f"v3_{index}" for index in range(39)]
        mcp = self.get("/api/v3/gateway")["channels"]["mcp"]
        self.assertEqual(mcp["tools_total"], 116)
        self.assertEqual(mcp["tools_bridge"], 39)
        self.assertEqual(mcp["tools_domain_catalog"], v3_ops.catalog_total())
        self.assertIn("app.state", mcp["tools_source"])

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
        # 每一行都必须自带证据来源与时间（口径可追，不允许「写死的可用性」）
        for row in body["data_sources"]:
            self.assertIn("source", row, row["name"])
            self.assertIn("as_of", row, row["name"])
            self.assertTrue(row["source"], row["name"])
            self.assertTrue(row["as_of"], row["name"])

    def test_sec_edgar_availability_comes_from_capability_probe(self):
        """SEC EDGAR 的可用性 = 真实探测结果，**不是**写死的「本服务未实现」。"""
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            body = self.get("/api/v3/settings")
        edgar = [row for row in body["data_sources"] if "SEC EDGAR" in row["name"]][0]
        self.assertTrue(edgar["available"])
        self.assertIn("companyconcept", edgar["detail"])
        self.assertIn("HTTP 200", edgar["detail"])
        self.assertEqual(edgar["source"], "http-probe:data.sec.gov")
        self.assertEqual(edgar["as_of"], PROBE_AS_OF)
        # 探测打的是真实取数端点（v3_sources.SEC_CONCEPT_URL），不是随便一个 URL
        self.assertEqual(len(self.http_probe.calls), 1)
        self.assertIn("/api/xbrl/companyconcept/", self.http_probe.calls[0]["url"])
        self.assertIn("user-agent", self.http_probe.calls[0]["headers"])

    def test_sec_edgar_probe_failure_reports_real_reason_and_as_of(self):
        """探测失败 → 如实写失败原因 + as_of；**绝不**写成「服务未实现」这类写死结论。"""
        self.http_probe.reachable = False
        v3_ops.reset_source_probes()
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            body = self.get("/api/v3/settings")
        edgar = [row for row in body["data_sources"] if "SEC EDGAR" in row["name"]][0]
        self.assertFalse(edgar["available"])
        self.assertIn("name resolution", edgar["detail"])
        self.assertIn("HTTP 能力探测失败", edgar["detail"])
        self.assertEqual(edgar["as_of"], PROBE_AS_OF)
        self.assertNotIn("未实现", edgar["detail"])

    def test_sec_edgar_distinguishes_reachable_from_available(self):
        """端点可达但返回 4xx ≠ 可用，也 ≠ 网络不通：三种事实分开报，不合并成一句「不可用」。"""
        self.http_probe.status = 403
        v3_ops.reset_source_probes()
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            body = self.get("/api/v3/settings")
        edgar = [row for row in body["data_sources"] if "SEC EDGAR" in row["name"]][0]
        self.assertFalse(edgar["available"], "拿到 403 不算可用")
        self.assertIn("端点本身可达", edgar["detail"])
        self.assertIn("HTTP 403", edgar["detail"])
        self.assertIn("不是「服务未实现」", edgar["detail"])

    def test_sec_edgar_prefers_last_real_call_evidence(self):
        """有落盘的真实调用证据（/api/v3/sources/status 写入）时，不再另发 HTTP 探测。"""
        self.write("v3-datasource-probe.json", json.dumps({
            "version": 1, "probed_at": 1789000000.0, "probed_at_iso": DAY + "T08:00:00+00:00",
            "chains": [{"key": "financials_us", "label": "美股财务报表",
                        "primary": "sec/companyconcept(us-gaap XBRL)",
                        "fallback": "openbb/equity.fundamental", "available": True,
                        "last_source": "sec/companyconcept(us-gaap XBRL)",
                        "chain_size": 2, "error": None}]}))
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            body = self.get("/api/v3/settings")
        edgar = [row for row in body["data_sources"] if "SEC EDGAR" in row["name"]][0]
        self.assertTrue(edgar["available"])
        self.assertEqual(edgar["as_of"], DAY + "T08:00:00+00:00")
        self.assertIn("sec/companyconcept", edgar["source"])
        self.assertIn("sec/companyconcept", edgar["detail"])
        self.assertEqual(self.http_probe.calls, [],
                         "已有真实调用证据就不该再打一次上游（设置页会被反复打开）")

    def test_sec_edgar_reports_recorded_failure(self):
        self.write("v3-datasource-probe.json", json.dumps({
            "version": 1, "probed_at": 1789000000.0, "probed_at_iso": DAY + "T08:00:00+00:00",
            "chains": [{"key": "financials_us", "available": False,
                        "last_source": "sec/companyconcept(us-gaap XBRL)",
                        "error": {"code": "sec/network", "message": "connection reset by peer"}}]}))
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            body = self.get("/api/v3/settings")
        edgar = [row for row in body["data_sources"] if "SEC EDGAR" in row["name"]][0]
        self.assertFalse(edgar["available"])
        self.assertIn("connection reset by peer", edgar["detail"])
        self.assertIn(DAY, edgar["detail"])
        self.assertIn("重跑", edgar["detail"])

    def test_tushare_availability_is_token_times_reachability(self):
        """Tushare 判据 = 凭据就绪 × 端点 HTTP 可达；**与 tushare 包能否 import 无关**。"""
        def tushare_row(body):
            return [row for row in body["data_sources"] if row["name"] == "Tushare Pro"][0]

        # 1) 未配置凭据 → 不发任何请求（如实说明「未探测」）
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            body = self.get("/api/v3/settings")
        row = tushare_row(body)
        self.assertFalse(row["available"])
        self.assertIn("未注入", row["detail"])
        self.assertEqual(row["source"], "credentials:未配置")
        self.assertEqual([call for call in self.http_probe.calls
                          if "tushare" in call["url"]], [],
                         "没有凭据就不该对 tushare 发任何请求")

        # 2) 凭据就绪 + 端点可达 → 可用；且**包不可导入也照样可用**（这正是修掉的那条错判据）
        self.http_probe.reachable = True
        v3_ops.reset_source_probes()
        with unittest.mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "t" * 32}, clear=True):
            with unittest.mock.patch.object(v3_ops, "_module_available", return_value=False):
                body = self.get("/api/v3/settings")
        row = tushare_row(body)
        self.assertTrue(row["available"], "实现走 HTTP，不需要 tushare 包")
        self.assertEqual(row["source"], "http-probe:api.tushare.pro + credentials")
        self.assertEqual(row["as_of"], PROBE_AS_OF)
        self.assertIn("环境变量 TUSHARE_TOKEN", row["detail"])
        self.assertIn("未做真实取数验证", row["detail"])
        self.assertEqual([call["url"] for call in self.http_probe.calls
                          if "tushare" in call["url"]], [v3_ops.TUSHARE_PROBE_URL])
        self.assertNotIn("包", row["detail"])

        # 3) 凭据就绪但端点不可达 → 如实报失败原因
        v3_ops.reset_source_probes()
        self.http_probe.reachable = False
        with unittest.mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "t" * 32}, clear=True):
            body = self.get("/api/v3/settings")
        row = tushare_row(body)
        self.assertFalse(row["available"])
        self.assertIn("不可达", row["detail"])
        self.assertIn("name resolution", row["detail"])

    def test_data_source_probe_is_cached_within_ttl(self):
        """设置页会被反复打开：TTL 内复用同一份探测结果（as_of 也不刷新），不重复打上游。"""
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            first = self.get("/api/v3/settings")
            second = self.get("/api/v3/settings")
        self.assertEqual(len(self.http_probe.calls), 1, "TTL 内不该重复探测 SEC")
        first_edgar = [row for row in first["data_sources"] if "SEC EDGAR" in row["name"]][0]
        second_edgar = [row for row in second["data_sources"] if "SEC EDGAR" in row["name"]][0]
        self.assertEqual(first_edgar["as_of"], second_edgar["as_of"],
                         "复用缓存时不得把 as_of 刷成「刚刚测过」")

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
        # 单笔原因仍在，且**额外的**行业原因必须显式写明「不可用 → 未参与阻断」（fail-open 留痕）
        self.assertEqual(by_id["CID-BIG"]["risk"]["reasons"][0],
                         "单笔占比 20.00% > 2%，需人工确认")
        self.assertTrue(any(reason.startswith("行业暴露数据不可用，未参与阻断")
                            for reason in by_id["CID-BIG"]["risk"]["reasons"]))
        self.assertEqual(by_id["CID-BIG"]["risk"]["rule"], "single-order")
        self.assertEqual(by_id["CID-BIG"]["industry_source"], "no-data")
        self.assertIsNone(by_id["CID-BIG"]["industry_pct"])

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
# 6.5 行业闸门前的历史判定：视图层归一化（只读；不回写、不重判）
# ---------------------------------------------------------------------------
class LegacyIndustryViewTests(V3OpsTestCase):
    """2026-09-19 落盘的 10 笔存量订单带的是**闸门前的判定快照**（``industry_pct=0.0``
    + ``industry_source="no-data（工具面无行业分类数据源…）"``、``risk``/``history`` 无
    ``rule``/``industry_*``）。两处不诚实：那句「工具面无行业分类数据源」在
    ``futu/info_owner_plate`` 接通后已失效；``0.0`` 会被读成「该单行业暴露 0%」，
    而事实是**当时没有行业读数**。

    本用例集钉住三件事：① 闸门前的旧记录 → 视图 ``industry_pct is None`` + legacy 标记
    + 文案不再出现那句失效断言；② 闸门后的新记录字段原样（真实读数 + 来源 + as_of + rule）；
    ③ 原始 ``risk`` / ``history`` 与磁盘/库里的原文一个字都不被改写。
    """

    #: 与真实存量单逐字段同形（``~/.dsh/v3-oms-orders.json`` 实测快照，2026-09-19T19:15:06Z）。
    LEGACY_SOURCE = "no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）"

    def legacy_record(self, identifier="CID-LEGACY"):
        return {
            "id": identifier, "plan_id": "PLN-20260918-SIM-12FB", "plan_status": "frozen",
            "mode": "sim", "strategy_id": "mom_20", "ticker": "SH.600000", "side": "BUY",
            "qty": 3300.0, "price": 9.07, "value": 29931.0, "broker_order_id": None,
            "stage": "manual",
            "risk": {"action": "manual", "reasons": ["单笔占比 6.90% > 2%，需人工确认"]},
            "nav_used": 100000.0, "nav_source": "sim-ledger(equity.current)",
            "drawdown_used": 3.0, "drawdown_source": "sim-ledger(max_drawdown)",
            "industry_pct": 0.0, "industry_source": self.LEGACY_SOURCE,
            "open_hit": False, "first_seen_at": "2026-09-19T12:25:45.641546+00:00",
            "updated_at": "2026-09-19T19:15:06.630146+00:00",
            "history": [
                {"at": "2026-09-19T12:25:45.641546+00:00", "stage": "manual",
                 "reasons": ["单笔占比 6.90% > 2%，需人工确认"]},
                {"at": "2026-09-19T19:15:06.630146+00:00", "stage": "manual",
                 "reasons": ["单笔占比 6.90% > 2%，需人工确认"]},
            ],
        }

    def write_legacy_ledger(self, identifier="CID-LEGACY"):
        record = self.legacy_record(identifier)
        path = self.write(v3_ops.OMS_FILENAME,
                          json.dumps({"version": 1, "orders": {identifier: record}},
                                     ensure_ascii=False))
        return record, path

    def fresh_probe_payload(self, pct=37.5, industry="股份制银行Ⅱ", market="SH"):
        """一份**新鲜**的落盘行业读数（真实格式：``~/.dsh/v3-risk-probe.json`` 的字段面）。"""
        return {
            "version": 1, "generated_at": time.time(),
            "generated_at_iso": "2026-09-20T12:00:00+00:00", "limit_pct": 20.0,
            "markets": [market], "market": market, "top_industry": industry,
            "top_weight_pct": pct, "breach": pct > 20.0,
            "source": "futu/info_owner_plate", "missing": 0,
            "per_market": {market: {
                "market": market, "ok": True, "top_industry": industry,
                "top_weight_pct": pct, "breach": pct > 20.0,
                "source": "futu/info_owner_plate", "missing": 0, "universe": 28,
                "weight_source": "platform/portfolio（自选池等权）", "error": None,
                "no_data": False}},
            "error": None,
        }

    # ---- ① 闸门前的旧记录 ----
    def test_pre_gate_record_is_normalized_in_the_view(self):
        original, path = self.write_legacy_ledger()
        on_disk_before = path.read_text(encoding="utf-8")

        body = self.get("/api/v3/oms/orders")
        order_view = body["orders"][0]
        # 0.0 → None（不是「行业暴露 0%」）
        self.assertIsNone(order_view["industry_pct"],
                          "闸门前没有行业读数，必须是 None 而不是 0.0")
        # 显式自解释标记
        self.assertTrue(order_view["legacy_pre_gate"])
        self.assertFalse(order_view["industry_graded"])
        # 如实文案；**不再出现**那句已失效的断言
        self.assertEqual(order_view["industry_source"], v3_ops.LEGACY_INDUSTRY_SOURCE)
        self.assertIn("历史判定", order_view["industry_source"])
        whole = json.dumps(order_view, ensure_ascii=False)
        self.assertNotIn("工具面无行业分类数据源", whole)
        self.assertIn("未包含行业红线", order_view["industry_note"])
        # 历史是历史：原始 reasons / history 一个字不动
        self.assertEqual(order_view["risk"], original["risk"])
        self.assertEqual(order_view["history"], original["history"])
        self.assertEqual(order_view["risk"]["reasons"], original["risk"]["reasons"])
        self.assertNotIn("rule", order_view["risk"], "视图不得补写历史 risk.rule")
        # 视图归一化是纯读：磁盘原文逐字节不变（绝不回写）
        self.assertEqual(path.read_text(encoding="utf-8"), on_disk_before,
                         "视图层归一化不得回写台账文件")
        # 同源的 /api/v3/execution 视图走同一函数 → 口径不可能漂移
        execution = self.get("/api/v3/execution")["oms"]["orders"][0]
        self.assertIsNone(execution["industry_pct"])
        self.assertTrue(execution["legacy_pre_gate"])
        self.assertEqual(execution["history"], original["history"])
        # 阶段计数只看 stage，不受归一化影响
        self.assertEqual(body["stages"], {"manual": 1})

    def test_pre_gate_normalization_does_not_rewrite_the_database(self):
        """落库的存量单同样只在返回视图里归一化，库里 payload 原样。"""
        record = self.legacy_record("CID-DB")
        v3_db.append_event(self.home, "oms_orders", record)
        stored_before = v3_db.list_events(self.home, "oms_orders", limit=None)

        body = self.get("/api/v3/oms/orders")
        self.assertIsNone(body["orders"][0]["industry_pct"])

        stored_after = v3_db.list_events(self.home, "oms_orders", limit=None)
        self.assertEqual(stored_after, stored_before, "归一化不得回写数据库")
        self.assertEqual(stored_after[0]["industry_pct"], 0.0,
                         "库里的历史原文仍是 0.0（历史是历史）")
        self.assertIn("工具面无行业分类数据源", stored_after[0]["industry_source"])

    # ---- ② 闸门后的新记录 ----
    def test_post_gate_record_keeps_its_real_reading(self):
        self.write(observability.RISK_PROBE_FILENAME,
                   json.dumps(self.fresh_probe_payload(), ensure_ascii=False))
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [plan([
            order("CID-NEW", symbol="SH.600000", qty=100, price=10.0)])]}}
        synced = self.sync()
        self.assertTrue(synced["ok"], synced)
        self.assertEqual(synced["industry_pct"], 37.5)
        self.assertEqual(synced["industry_source"], "cache/futu/info_owner_plate")

        row = {item["id"]: item for item in self.get("/api/v3/oms/orders")["orders"]}["CID-NEW"]
        # 真实字段原样
        self.assertEqual(row["industry_pct"], 37.5)
        self.assertEqual(row["industry_source"], "cache/futu/info_owner_plate")
        self.assertEqual(row["industry_as_of"], "2026-09-20T12:00:00+00:00")
        # 37.5% > 20% 上限 → 行业红线**真的**参与并阻断（与历史判定的「未含行业红线」对照）
        self.assertEqual(row["risk"]["rule"], "industry-red-line")
        self.assertEqual(row["stage"], "blocked_industry")
        self.assertIsNotNone(row["industry_probe_age_ms"])
        # 只追加两个自解释标记，不改任何既有值
        self.assertFalse(row["legacy_pre_gate"])
        self.assertTrue(row["industry_graded"])
        self.assertNotIn("industry_note", row)
        # 留痕也带闸门标记（闸门后的记录天然不会被误判成历史）
        self.assertEqual(row["history"][-1]["rule"], "industry-red-line")
        self.assertEqual(row["history"][-1]["industry_source"],
                         "cache/futu/info_owner_plate")
        # 「字段原样」逐字段核对：视图 = 落盘原文 + 两个标记，别的一个字段都没被碰
        stored = json.loads((self.home / v3_ops.OMS_FILENAME)
                            .read_text(encoding="utf-8"))["orders"]["CID-NEW"]
        self.assertEqual({key: value for key, value in row.items()
                          if key not in ("industry_graded", "legacy_pre_gate")}, stored)
        self.assertEqual(sorted(set(row) - set(stored)),
                         ["industry_graded", "legacy_pre_gate"],
                         "闸门后的新记录只允许追加这两个标记")

    def test_post_gate_record_without_a_reading_is_not_called_legacy(self):
        """闸门后 + 没有新鲜读数 → 真实字段 ``industry_pct=None`` / ``no-data``，不是历史判定。"""
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [plan([order("CID-G")])]}}
        self.sync()
        row = self.get("/api/v3/oms/orders")["orders"][0]
        self.assertIsNone(row["industry_pct"])
        self.assertEqual(row["industry_source"], v3_ops.INDUSTRY_SOURCE)
        self.assertNotEqual(row["industry_source"], v3_ops.LEGACY_INDUSTRY_SOURCE)
        self.assertFalse(row["legacy_pre_gate"])
        self.assertTrue(row["industry_graded"],
                        "fail-open 也是「经闸门的判定」（reasons 里已写明未参与阻断）")

    # ---- ③ 判据本身：结构性、可解释、不猜时间戳 ----
    def test_legacy_judgement_is_structural_not_timestamp_based(self):
        legacy = self.legacy_record()
        self.assertTrue(v3_ops.legacy_pre_gate(legacy))
        # 只要有了闸门留痕（risk.rule / history[].industry_*）就不再是历史判定——判据不看时间
        with_rule = copy.deepcopy(legacy)
        with_rule["industry_source"] = "cache/futu/info_owner_plate"
        with_rule["industry_pct"] = 37.5
        with_rule["risk"]["rule"] = "single-order"
        self.assertFalse(v3_ops.legacy_pre_gate(with_rule))
        # 留痕里带了 industry_* 同样如此
        with_trail = copy.deepcopy(with_rule)
        with_trail["risk"] = {"action": "manual", "reasons": ["x"]}
        with_trail["history"][-1]["industry_source"] = "cache/futu/info_owner_plate"
        self.assertFalse(v3_ops.legacy_pre_gate(with_trail))
        with_trail_rule = copy.deepcopy(with_trail)
        with_trail_rule["history"][-1].pop("industry_source")
        with_trail_rule["history"][-1]["rule"] = "single-order"
        self.assertFalse(v3_ops.legacy_pre_gate(with_trail_rule))
        # 那句失效断言单独出现也足以认定（当前实现已不可能产生它）
        self.assertTrue(v3_ops.legacy_pre_gate({
            "industry_source": self.LEGACY_SOURCE, "risk": {"rule": "within-limits"}}))
        # 压根没有分级留痕的迁移数据 → **不**下「历史判定」结论（无从谈含不含行业红线）
        self.assertFalse(v3_ops.legacy_pre_gate({"id": "M", "stage": "manual",
                                                 "updated_at": "5"}))
        self.assertFalse(v3_ops.legacy_pre_gate({"id": "M",
                                                 "risk": {"action": "manual",
                                                          "reasons": ["x"],
                                                          "rule": "single-order"}}))
        self.assertFalse(v3_ops.legacy_pre_gate(None))

    def test_order_view_returns_a_copy(self):
        record = self.legacy_record()
        view = v3_ops.order_view(record)
        view["industry_source"] = "tampered"
        view["history"].append({"at": "now"})
        self.assertEqual(record["industry_source"], self.LEGACY_SOURCE)
        self.assertEqual(len(record["history"]), 2, "视图是副本：改它不得动到原始记录")
        self.assertEqual(record["industry_pct"], 0.0)


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
        self.assertEqual(body["headless"]["today"]["total"], 0)
        self.assertIn("date", body["headless"]["today"])
        self.assertIsNotNone(body["headless"]["breaker"])
        self.assertEqual(body["headless"]["last"], [])
        self.assertEqual(body["headless"]["status"], "implemented-not-registered")
        self.assertIn("/api/v3/sdk/status", body["sdk"]["reason"])
        self.assertEqual(body["sdk"]["turns"], [])
        self.assertEqual(body["sdk"]["events"], [])
        self.assertIn("无数据源", body["sources"]["decision"])
        self.assertIn("v3-strategy-runs.jsonl", body["sources"]["decision"])
        self.assertIn("已实现", body["sources"]["headless"])
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
# SQLite 持久化层接线（v3_db）：只加断言，不改既有用例
# ---------------------------------------------------------------------------
class HeadlessRealReadTests(V3OpsTestCase):
    """FR-MON-003 / FR-GATEWAY-004：``headless`` 块必须读**真实表与真实配置**。

    这组用例钉的是「4 处失效表述已修」这件事本身：``today`` 不再恒 0、``breaker`` 不再恒
    null、``status`` 不再恒 ``unavailable``、``reason`` 不再出现「未挂载 Headless CLI 子通道」。
    """

    def setUp(self):
        super().setUp()
        # 进程内注册表是全局的：别的用例注册过的 home 会残留，必须清干净（不 stop，没有进程）。
        from server import v3_headless
        self.headless = v3_headless
        v3_headless.reset_registry()
        self.addCleanup(v3_headless.reset_registry)

    def _seed_log(self, rows):
        """往真实表里写调用日志（``v3_headless`` 的写入路径同源：``v3_db.append_event``）。"""
        for row in rows:
            v3_db.append_event(self.home, "headless_log", row)

    def test_today_counts_real_rows_from_the_table(self):
        today = datetime.now(timezone.utc).date().isoformat()
        self._seed_log([
            {"started_at": f"{today}T01:00:00+00:00", "success": True, "exit_code": 0,
             "duration_ms": 1200.0, "tokens_estimate": 900, "task_type": "risk_review",
             "trigger": "risk_breach", "outcome": "completed", "killed": False},
            {"started_at": f"{today}T02:00:00+00:00", "success": False, "exit_code": 1,
             "duration_ms": 800.0, "tokens_estimate": 100, "task_type": "pre_market_scan",
             "trigger": "slot:pre_market_scan", "outcome": "incomplete", "killed": False},
            {"started_at": "2020-01-01T00:00:00+00:00", "success": True, "exit_code": 0,
             "duration_ms": 10.0, "tokens_estimate": 1, "task_type": "old", "trigger": "old",
             "outcome": "completed", "killed": False},
        ])
        body = self.get("/api/v3/gateway")
        headless = body["headless"]
        self.assertEqual(headless["today"]["total"], 2, "只数当日（UTC）")
        self.assertEqual(headless["today"]["success"], 1)
        self.assertEqual(headless["today"]["failed"], 1)
        self.assertEqual(headless["today"]["tokensEstimate"], 1000)
        self.assertEqual(headless["today"]["avgMs"], 1000.0)
        self.assertEqual(len(headless["last"]), 3)
        self.assertEqual(body["channels"]["headless"]["status"], "implemented-not-registered")
        self.assertNotIn("未挂载", body["channels"]["headless"]["reason"])

    def test_breaker_comes_from_v3_headless_config(self):
        runner = self.headless.HeadlessRunner(self.home)
        expected = runner.params()
        body = self.get("/api/v3/gateway")["headless"]
        self.assertEqual(body["breaker"]["maxConcurrency"], expected["maxConcurrency"])
        self.assertEqual(body["breaker"]["timeoutSeconds"], expected["timeoutSeconds"])
        self.assertEqual(body["breaker"]["tokenBudget"], expected["tokenBudget"])
        self.assertEqual(body["breaker"]["profile"], expected["profile"])

    def test_registered_module_reports_live_scheduler_state(self):
        """真装配一次（register 起对象、**不起线程**）→ status/registered/schedulerAlive 变真值。"""
        app = FastAPI()
        runner, scheduler = self.headless.register(app, self.fake, str(self.home))
        self.addCleanup(runner.stop)
        try:
            body = self.get("/api/v3/gateway")["headless"]
            self.assertTrue(body["registered"])
            self.assertEqual(body["status"], "registered-idle")
            self.assertIs(body["schedulerAlive"], False)
            self.assertTrue(body["nextFireTimes"], "真实调度器的下次触发时间应可读")
            self.assertEqual(body["breaker"]["maxConcurrency"],
                             runner.params()["maxConcurrency"])
            self.assertIn("已注册", body["reason"])
            # 本模块**不起进程**：注册只建对象，Runner 没有子进程句柄（真起进程只在 submit 里）
            self.assertFalse(hasattr(runner, "pid"))
        finally:
            self.headless.reset_registry()

    def test_gateway_and_brain_share_one_reading(self):
        paths = {"headless", "channels"}
        gateway = self.get("/api/v3/gateway")
        brain = self.get("/api/v3/brain")
        self.assertEqual(brain["headless"]["today"], gateway["headless"]["today"])
        self.assertEqual(brain["headless"]["status"], gateway["headless"]["status"])
        self.assertEqual(brain["headless"]["breaker"], gateway["headless"]["breaker"])
        self.assertEqual(brain["sources"]["headless"], gateway["headless"]["reason"])
        self.assertEqual(paths, {"headless", "channels"})  # 只是防止两个键被改名时静默跳过


class FundsCheckTests(V3OpsTestCase):
    """FR-EXEC-003 资金检查（事前风控）：**只读**、缺省不接、缺读数不改判定。"""

    FUNDS = {"ok": True, "value": {
        "mode": "sim", "source": "futu/sim_trade_cash_info", "as_of": DAY,
        "groups": [{"acc_id": "SIM-1", "market": "SH",
                    "cash": {"balance": "50000", "max_power_long": "60000",
                             "total_asset": "80000", "mv": "30000"}},
                   {"acc_id": "SIM-2", "market": "HK",
                    "cash": {"balance": "1000", "max_power_long": "1000",
                             "total_asset": "2000"}}]}}

    def test_sync_defaults_to_no_funds_dimension(self):
        """缺省 ``funds`` 不给 → 逐字段与历史一致（不调 account_funds、不加 funds_check）。"""
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [
            plan([order("CID-1", qty=100, price=10.0)])]}}
        result = self.sync()
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["funds_check"])
        self.assertEqual([name for name, _ in self.fake.calls if name == "account_funds"], [])
        body = self.get("/api/v3/oms/orders")
        self.assertNotIn("funds_check", body["orders"][0])

    def test_sync_with_funds_blocks_orders_over_buying_power(self):
        self.fake.values["account_funds"] = self.FUNDS
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [
            plan([order("SMALL", qty=100, price=10.0),        # 1000 ≤ 60000 → noted
                  order("BIG", qty=100, price=1000.0)])]}}    # 100000 > 60000 → blocked
        response = self.client.post("/api/v3/oms/sync", json={"funds": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"], body)
        self.assertIsNotNone(body["funds_check"])
        orders = {row["id"]: row for row in self.get("/api/v3/oms/orders")["orders"]}
        self.assertEqual(orders["SMALL"]["stage"], "risk_passed")
        self.assertEqual(orders["SMALL"]["funds_check"]["action"], "noted")
        self.assertEqual(orders["BIG"]["stage"], "blocked")
        self.assertEqual(orders["BIG"]["funds_check"]["action"], "blocked")
        self.assertEqual(orders["BIG"]["funds_check"]["readings"]["buyingPower"], 61000.0)
        self.assertTrue(any("资金检查" in reason for reason in orders["BIG"]["risk"]["reasons"]))

    def test_sync_with_funds_is_fail_open_when_no_buying_power_field(self):
        """上游没有购买力字段 → ``unknown``，**不改判定**，但必须留痕。"""
        self.fake.values["account_funds"] = {"ok": True, "value": {
            "mode": "live", "source": "futu/account_funds", "as_of": DAY,
            "groups": [{"acc_id": "LIVE-1", "market": "SH",
                        "cash": {"total_assets": "80000"}}]}}
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [
            plan([order("CID-1", qty=100, price=10.0)])]}}
        body = self.client.post("/api/v3/oms/sync?funds=1").json()
        self.assertIsNotNone(body["funds_check"]["error"])
        record = self.get("/api/v3/oms/orders")["orders"][0]
        self.assertEqual(record["stage"], "risk_passed")
        self.assertEqual(record["funds_check"]["action"], "unknown")
        self.assertTrue(any("资金检查无读数" in reason
                            for reason in record["risk"]["reasons"]))

    def test_gateway_never_calls_account_funds(self):
        """网关/大脑是只读展示位：**不许**在抓取路径上多打一次账户查询。"""
        self.get("/api/v3/gateway")
        self.get("/api/v3/brain")
        self.assertEqual([name for name, _ in self.fake.calls if name == "account_funds"], [])

    def test_unit_check_order_with_funds(self):
        blocked, reasons = v3_ops.check_order(
            150000.0, 1000000.0, funds={"action": "blocked", "readings": {"buyingPower": 60000.0},
                                        "funding_basis_field": ["max_power_long"],
                                        "source": "futu/sim_trade_cash_info", "as_of": DAY})
        self.assertEqual(blocked, "blocked")
        self.assertTrue(any("强制阻断" in reason for reason in reasons))
        unknown, reasons = v3_ops.check_order(
            1000.0, 1000000.0, funds={"action": "unknown", "reason": "没有购买力字段",
                                      "readings": {}})
        self.assertEqual(unknown, "auto", "读不到资金不改判定（fail-open + 留痕）")
        self.assertTrue(any("资金检查无读数" in reason for reason in reasons))
        # 缺省（funds=None）→ 与历史逐字段一致
        legacy, legacy_reasons = v3_ops.check_order(1000.0, 1000000.0)
        self.assertEqual((legacy, legacy_reasons), ("auto", []))


class SqlitePersistenceTests(V3OpsTestCase):
    """OMS 台账/对账留痕/研究决策走库，JSONL/JSON 保留为冷备；库不可用时回退文件。"""

    def test_metrics_exposes_the_db_block(self):
        v3_db.init_db(self.home)
        body = self.get("/api/v3/metrics")
        db = body["db"]
        for key in ("path", "sizeBytes", "tables", "writes", "reads", "migrated"):
            self.assertIn(key, db, key)
        self.assertEqual(db["path"], str(self.home / "v3.db"))
        self.assertTrue(db["exists"])
        self.assertGreater(db["sizeBytes"], 0)
        self.assertEqual(db["tables"]["oms_orders"], 0)
        self.assertIsInstance(db["migrated"], dict)

    def test_ledger_write_goes_to_the_table_and_the_file_stays_a_mirror(self):
        ledger = v3_ops.OmsLedger(self.fake, self.home)
        ledger.write({"A": {"id": "A", "ticker": "SH.600000", "stage": "manual",
                            "updated_at": "3"}})
        rows = v3_db.list_events(self.home, "oms_orders", limit=None)
        self.assertEqual([row["id"] for row in rows], ["A"])
        self.assertEqual(rows[0]["stage"], "manual")
        on_disk = json.loads((self.home / v3_ops.OMS_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["orders"]["A"]["ticker"], "SH.600000",
                         "冷备 JSON 同步写出（迁移前的外部读者不受影响）")
        # 库是主源：把冷备改成别的，读回来的还是库里的
        (self.home / v3_ops.OMS_FILENAME).write_text(
            json.dumps({"version": 1, "orders": {"Z": {"id": "Z", "ticker": "US.NVDA"}}}),
            encoding="utf-8")
        self.assertEqual([row["id"] for row in ledger.list()], ["A"])

    def test_ledger_falls_back_to_the_file_when_the_database_is_unavailable(self):
        (self.home / v3_ops.OMS_FILENAME).write_text(json.dumps({"version": 1, "orders": {
            "F": {"id": "F", "ticker": "HK.00700", "stage": "risk_passed",
                  "updated_at": "9"}}}), encoding="utf-8")
        with unittest.mock.patch.object(v3_db, "list_events",
                                        side_effect=v3_db.V3DbError("boom")):
            body = self.get("/api/v3/oms/orders")
        self.assertTrue(body["ok"], body)
        self.assertEqual([row["id"] for row in body["orders"]], ["F"])
        self.assertEqual(body["stages"], {"risk_passed": 1})

    def test_oms_sync_appends_to_the_table_and_the_jsonl(self):
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [
            plan([order("CID-1", qty=100, price=10.0)])]}}
        result = self.sync()
        self.assertTrue(result["ok"], result)
        rows = v3_db.list_events(self.home, "oms_sync", limit=None, order="asc")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["plans"], result["plans"])
        self.assertEqual(rows[0]["stages"], result["stages"])
        self.assertEqual(len((self.home / v3_ops.OMS_SYNC_FILENAME)
                             .read_text(encoding="utf-8").strip().splitlines()), 1)
        self.assertEqual(v3_db.count_events(self.home, "oms_orders"), 1,
                         "对账登记的订单同样落库")

    def test_brain_reads_the_database_and_reports_the_real_source(self):
        v3_db.append_event(self.home, "strategy_runs",
                           {"run_id": "db-1", "market": "SH"})
        body = self.get("/api/v3/brain")
        self.assertEqual(body["decision"], {"run_id": "db-1", "market": "SH"})
        self.assertIn("sqlite:", body["sources"]["decision"])
        self.assertIn(v3_ops.STRATEGY_RUNS_FILENAME, body["sources"]["decision"],
                      "来源标注仍带冷备文件名（既有前端/断言口径不变）")

    def test_brain_still_reads_file_only_records(self):
        self.write(v3_ops.STRATEGY_RUNS_FILENAME,
                   json.dumps({"run_id": "file-1"}) + "\n")
        body = self.get("/api/v3/brain")
        self.assertEqual(body["decision"], {"run_id": "file-1"})

    def test_migration_feeds_the_existing_endpoints(self):
        """迁移前就有 OMS 台账/策略轮的 home：init_db 后端点读到的是迁移进来的数据。"""
        orders = {"M": {"id": "M", "ticker": "SZ.002716", "stage": "manual",
                        "updated_at": "5",
                        "risk": {"action": "manual", "reasons": ["占比超限"]}}}
        (self.home / v3_ops.OMS_FILENAME).write_text(
            json.dumps({"version": 1, "orders": orders}, ensure_ascii=False), encoding="utf-8")
        self.write(v3_ops.STRATEGY_RUNS_FILENAME,
                   json.dumps({"run_id": "legacy-run", "market": "SH"}) + "\n")
        state = v3_db.init_db(self.home)
        self.assertEqual(state["tables"]["oms_orders"], 1)
        self.assertEqual(state["tables"]["strategy_runs"], 1)

        body = self.get("/api/v3/oms/orders?market=SH")
        self.assertEqual([row["id"] for row in body["orders"]], ["M"])
        self.assertEqual(body["orders"][0]["risk"]["reasons"], ["占比超限"])
        self.assertEqual(self.get("/api/v3/brain?market=SH")["decision"]["run_id"],
                         "legacy-run")


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
        # 行业红线**单独成态**（action=blocked_industry）——便于 metric/审计区分是哪条红线
        self.assertEqual(v3_ops.check_order(1000.0, 100000.0, industry_pct=25.0)[0],
                         "blocked_industry")
        self.assertEqual(v3_ops.check_order(1000.0, 100000.0, industry_pct=20.0)[0], "auto",
                         "== 20% 不阻断（只有 > 才阻断）")
        # 红线优先于单笔结论：blocked 覆盖 manual
        self.assertEqual(v3_ops.check_order(50000.0, 100000.0, drawdown_pct=20.0)[0], "blocked")
        # 无行业读数 → fail-open（不阻断），但 reasons 必须写明
        action, reasons = v3_ops.check_order(1000.0, 100000.0, industry_pct=None,
                                             industry_source="no-data")
        self.assertEqual(action, "auto")
        self.assertTrue(any("未参与阻断" in reason for reason in reasons))

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


# ---------------------------------------------------------------------------
# /api/v3/overview?market=（deals_today 也按市场过滤，2026-09-20 加强）
# ---------------------------------------------------------------------------
class _DummyScheduler:
    """只为装配 create_app：不起线程、不做任何事（limits 由 lifespan 驱动，本测试不进）。"""

    def start(self):
        return None

    def stop(self):
        return None


class _DummyPush:
    quote_cache = None


class OverviewMarketScopeTests(unittest.TestCase):
    """``GET /api/v3/overview?market=``：positions / plan / **deals_today** 同口径过滤。

    装配方式：patch ``server.app.create_handler`` 返回记录型假 handle（离线、零网络），
    其余 V3 子模块照常自动接线——因此**同一个** app.py 市场分支与
    ``v3_ops.filter_deals_value`` 都被真实跑到，而不是测试里重写一份逻辑。
    """

    def setUp(self):
        from server import app as app_module
        from server import v3_ratelimit, v3_universe

        self.app_module = app_module
        self.ratelimit = v3_ratelimit
        v3_universe.clear_cache()
        # 本环境的 httpx 日志器名是 ``httpx2``（装配链里还顺手 basicConfig 了 root）
        self._loggers = [logging.getLogger("httpx"), logging.getLogger("httpx2")]
        for logger in self._loggers:
            self.addCleanup(logger.setLevel, logger.level)
        root_logger = logging.getLogger()
        self._root_level = root_logger.level
        self._root_handlers = list(root_logger.handlers)
        self.addCleanup(self._restore_root_logging)
        self.home = Path(tempfile.mkdtemp(prefix="v3-overview-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        # 每个用例一份干净 limiter：app.py 接线时抓到它，/api/v3/metrics 读的也是它。
        self.limiter = v3_ratelimit.FutuLimiter(rate_per_sec=1000.0, burst=1000)
        v3_ratelimit.reset_limiter(self.limiter)
        self.addCleanup(v3_ratelimit.reset_limiter, None)

        self.raw = {
            "equity": {"ok": True, "value": {"current": 100000.0, "mode": "sim"}},
            "positions": {"ok": True, "value": {
                "mode": "sim", "as_of": DAY, "source": "futu/sim_trade_position_list",
                "groups": [
                    {"acc_id": "HK-1", "market": 1,
                     "positions": [{"symbol": "00700", "market_val": 100.0}]},
                    {"acc_id": "SH-1", "market": 3,
                     "positions": [{"symbol": "600000", "market_val": 200.0}]},
                    {"acc_id": "OPT-1", "market": 9,
                     "positions": [{"symbol": "00700", "market_val": 400.0}]}]}},
            "deals_today": {"ok": True, "value": {
                "mode": "sim", "as_of": DAY, "source": "futu/sim_trade_order_list(derived)",
                "groups": [
                    {"acc_id": "HK-1", "market": "HK",
                     "rows": [{"deal_id": "D1", "code": "HK.00700"}]},
                    {"acc_id": "SH-1", "market": "SH",
                     "rows": [{"deal_id": "D2", "code": "SH.600000"},
                              {"deal_id": "D3", "code": "SH.600009"}]},
                    {"acc_id": "OPT-1", "market": "9",
                     "rows": [{"deal_id": "D4", "code": "SG.X"}]},
                    # 实盘账户分组没有 market 声明 → 逐行按标的前缀归因（同一份口径）
                    {"acc_id": "LIVE-1",
                     "rows": [{"deal_id": "D5", "code": "HK.00700"},
                              {"deal_id": "D6", "code": "SH.600000"}]}]}},
            "plan": {"ok": True, "value": {"plans": [
                {"plan_id": "P-HK", "target": {"HK.00700": 1}},
                {"plan_id": "P-SH", "target": {"SH.600000": 1}}]}},
            "schedule": {"ok": True, "value": {"heartbeat": {"at": DAY}}},
            "sources": {"ok": True, "value": {"channels": []}},
            "snapshot": {"ok": True, "value": {"mode": "sim"}},
            "push_status": {"ok": True, "value": {"enabled": False}},
        }
        self.handle_calls = []

        def fake_handle(endpoint, payload=None):
            self.handle_calls.append((endpoint, dict(payload or {})))
            if endpoint not in self.raw:
                return {"ok": False, "error": {"code": "trading/unknown-endpoint",
                                               "message": endpoint}}
            return copy.deepcopy(self.raw[endpoint])

        self.handle = fake_handle
        patcher = unittest.mock.patch.object(app_module, "create_handler",
                                             lambda *args, **kwargs: fake_handle)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = app_module.create_app(home=str(self.home), dist=str(self.home), config={},
                                         scheduler=_DummyScheduler(), futu=object(),
                                         push=_DummyPush())
        # 装配链把 root 拉到 INFO 并加了 handler——装配之后再静音，避免测试输出刷屏。
        for logger in self._loggers:
            logger.setLevel(logging.WARNING)
        self.client = TestClient(self.app)

    def _restore_root_logging(self):
        """装配链的 basicConfig 会往 root 加 handler/改级别——收尾还原，别污染后续用例输出。"""
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if handler not in self._root_handlers:
                root_logger.removeHandler(handler)
        root_logger.setLevel(self._root_level)

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_overview_market_filters_deals_today(self):
        body = self.get("/api/v3/overview?market=HK")
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["market"], "HK")
        self.assertEqual([g["acc_id"] for g in body["deals_today"]["groups"]],
                         ["HK-1", "LIVE-1"],
                         "LIVE-1 没有 market 声明 → 只保留按标的前缀归到 HK 的行")
        self.assertEqual([row["deal_id"] for row
                          in body["deals_today"]["groups"][0]["rows"]], ["D1"])
        self.assertEqual([row["deal_id"] for row
                          in body["deals_today"]["groups"][1]["rows"]], ["D5"],
                         "无市场声明的分组逐行归因（SH.600000 被排除）")
        self.assertEqual(body["filter"]["deals"],
                         {"market": "HK", "kept": 2, "excluded": 4,
                          "unknownMarketGroups": 1},
                         "kept/excluded 是成交笔数；market_id=9 不猜市场，如实计未知")
        self.assertEqual([g["acc_id"] for g in body["positions"]["groups"]], ["HK-1"])
        self.assertEqual(body["filter"]["positions"]["keptGroups"], 1)
        self.assertEqual([p["plan_id"] for p in body["plan"]["plans"]], ["P-HK"])
        self.assertEqual(body["filter"]["plan"]["keptPlans"], 1)

    def test_overview_sections_say_deals_is_market_scoped(self):
        body = self.get("/api/v3/overview?market=HK")
        sections = body["sections"]["market_scoped"]
        self.assertIn("已按账户市场过滤", sections["deals_today"])
        self.assertNotIn("不按市场过滤", sections["deals_today"])
        self.assertIn("台账口径", sections["equity"])
        self.assertIn("未按市场拆分", sections["equity"])
        self.assertEqual(body["equity"], self.raw["equity"]["value"],
                         "equity 是单一台账：原样保留，不按市场拆分")

    def test_overview_counts_an_empty_market_honestly(self):
        body = self.get("/api/v3/overview?market=US")
        self.assertEqual(body["deals_today"]["groups"], [])
        self.assertEqual(body["filter"]["deals"],
                         {"market": "US", "kept": 0, "excluded": 6,
                          "unknownMarketGroups": 2},
                         "OPT-1（market_id=9）与 LIVE-1（无声明且一行都归不到 US）"
                         "都如实计未知，绝不错归到某个市场")

    def test_overview_without_market_is_untouched(self):
        body = self.get("/api/v3/overview")
        self.assertEqual(body["deals_today"], self.raw["deals_today"]["value"],
                         "不传 market 时 deals_today 原样透传（与历史完全一致）")
        self.assertEqual(body["positions"], self.raw["positions"]["value"])
        self.assertEqual(body["plan"], self.raw["plan"]["value"])
        self.assertNotIn("market", body)
        self.assertNotIn("filter", body)
        self.assertNotIn("sections", body)

    def test_overview_bad_market(self):
        body = self.get("/api/v3/overview?market=MARS")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "market/bad-market")

    def test_overview_futu_tools_go_through_the_limiter(self):
        marker = len(self.handle_calls)
        self.get("/api/v3/overview?market=HK")
        endpoints = [endpoint for endpoint, _ in self.handle_calls[marker:]]
        futu = [endpoint for endpoint in endpoints if self.ratelimit.is_futu_tool(endpoint)]
        self.assertTrue(futu, f"overview 里应有富途工具：{endpoints}")
        for local in ("equity", "plan", "schedule"):
            self.assertNotIn(local, futu, f"{local} 是本地台账工具，不进限流器")
        view = self.get("/api/v3/metrics")["futu"]
        self.assertEqual(view["calls"], len(futu), "限流器计数 = 富途工具调用数")
        self.assertEqual(view["inFlight"], 0)
        for key in ("enabled", "calls", "coalesced", "retries", "rateLimited",
                    "throttleWaitMs", "cooldownUntil", "cooldownRemainingMs",
                    "inFlight", "queued"):
            self.assertIn(key, view)

    def test_app_startup_initializes_the_sqlite_layer(self):
        """``create_app`` 装配即建库 + 迁移（幂等）：真实 app 分支，不是测试里另写一份。"""
        state = self.app.state.v3_db
        self.assertTrue(state["ok"], state)
        self.assertTrue(state["exists"])
        self.assertEqual(state["schemaVersion"], v3_db.SCHEMA_VERSION)
        self.assertTrue((self.home / "v3.db").is_file())
        again = v3_db.init_db(self.home)
        self.assertEqual(again["tables"]["schema_version"], 1, "重复 init 不重复记账")


if __name__ == "__main__":
    unittest.main()
