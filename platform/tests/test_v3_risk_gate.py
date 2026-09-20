"""行业集中度闸门（``server/v3_risk_gate`` + ``v3_ops`` 接入）**离线**契约测试。

本文件覆盖任务书的每一条验收点，全部离线（临时 home + 注入记录型 ``v3_run`` 替身，
不读真实 ``~/.dsh``、不打富途、**绝不下单**——只用构造的台账订单过 ``check_order``/
``OmsLedger.sync`` 这条纯判定链）：

  * 缓存命中 → 行业超限 → ``stage=blocked_industry``（硬阻断），reasons 带读数/来源/as_of；
  * 缓存过期 + 现取成功 → 同样阻断（现取走 ``industry_exposure``）；
  * 现取失败 → **fail-open 不阻断**，但 reasons 必须写明「数据不可用，未参与阻断」；
  * ``industry_pct == limit`` 边界 → ``>`` 才阻断（等于不阻断）；
  * 单笔 2%／回撤 15% 既有行为不回归；
  * 多市场隔离：SH 超限不影响 HK 单；
  * 台账 ``history`` 留痕字段齐全（读数/来源/as_of/规则/是否变态）；
  * ``missing`` 非空 → 读数只是**下界**，reasons 里显式标注；
  * 缓存 ``generated_at`` 缺失/非法 → 当没有读数（不猜时刻），fail-open；
  * ``/api/v3/metrics`` 的 ``industryGate`` 计数（``blockedIndustry``）来自台账，
    且抓取路径**不增加**任何工具调用计数。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_risk_gate -v``
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import observability, v3_ops, v3_risk_gate  # noqa: E402

DAY = "2026-09-20"
NOW = 1789898332.0536346  # 与真实缓存同一量级的 epoch（测试里显式传入，不读系统钟）


class FakeV3Run:
    """记录型 ``v3_run`` 替身：未预置的工具一律返回 ``test/unrouted``（= 现取失败）。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        value = self.values.get(name)
        if value is None:
            return {"ok": False,
                    "error": {"code": "test/unrouted", "message": f"未预置 {name}"}}
        if callable(value):
            return value(dict(payload or {}))
        return value

    def count(self, name):
        return sum(1 for tool, _payload in self.calls if tool == name)


def oms_values(**overrides):
    """台账侧的最小工具面信封（NAV=100000 → 1% 的单不触发单笔红线）。"""
    values = {
        "equity": {"ok": True, "value": {"current": 100000.0, "max_drawdown": -0.03}},
        "positions": {"ok": True, "value": {"mode": "sim", "groups": []}},
        "orders_open": {"ok": True, "value": {"mode": "sim", "as_of": DAY, "groups": []}},
        "confirmation": {"ok": True, "value": {"pending": [], "ttl_seconds": 300}},
    }
    values.update(overrides)
    return values


def plan(orders, plan_id="P-1", status="frozen", target=None):
    return {"plan_id": plan_id, "status": status, "mode": "sim",
            "target": dict(target if target is not None else {"SH.600000": 1}), 
            "orders": list(orders)}


def order(client_order_id, symbol="SH.600000", side="BUY", qty=100, price=10.0,
          status="frozen"):
    return {"client_order_id": client_order_id, "symbol": symbol, "side": side, "qty": qty,
            "price": price, "status": status, "broker_order_id": None}


def probe(markets, *, generated_at=NOW, limit_pct=20.0):
    """构造 ``v3-risk-probe.json`` 的载荷（形状与 ``observability.build_risk_probe_payload`` 一致）。"""
    rows = {}
    for market, spec in markets.items():
        rows[market] = {
            "market": market, "ok": True, "top_industry": spec.get("industry", "银行"),
            "top_weight_pct": spec.get("pct"), "breach": spec.get("breach"),
            "source": spec.get("source", "futu/info_owner_plate"),
            "missing": spec.get("missing", 0), "universe": spec.get("universe", 8),
            "weight_source": "platform/portfolio（自选池等权（8 只））",
            "error": spec.get("error"), "no_data": bool(spec.get("no_data", False)),
        }
    usable = [row for row in rows.values()
              if row["ok"] and row["top_weight_pct"] is not None]
    top = max(usable, key=lambda row: row["top_weight_pct"]) if usable else None
    return {
        "version": 1, "generated_at": generated_at,
        "generated_at_iso": "2026-09-20T09:58:52.053639+00:00", "limit_pct": limit_pct,
        "markets": sorted(rows), "market": None if top is None else top["market"],
        "top_industry": None if top is None else top["top_industry"],
        "top_weight_pct": None if top is None else top["top_weight_pct"],
        "breach": None if top is None else bool(top["top_weight_pct"] > limit_pct),
        "source": None if top is None else top["source"],
        "missing": None if top is None else top["missing"],
        "per_market": rows, "error": None,
    }


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="v3gate-"))
        self.addCleanup(shutil.rmtree, self.home, True)

    # ---- 小工具 ----
    def write_probe(self, payload):
        observability.record_risk_probe(self.home, payload)

    def write_watchlist(self, tickers):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": list(tickers)}), encoding="utf-8")

    def ledger(self, values=None):
        self.fake = FakeV3Run(values if values is not None else oms_values())
        return v3_ops.OmsLedger(self.fake, self.home), self.fake

    def sync(self, ledger, orders, *, values=None):
        if values is not None:
            self.fake.values.update(values)
        self.fake.values["plan"] = {"ok": True, "value": {"plans": [plan(orders)]}}
        return ledger.sync()

    def records(self, ledger):
        return ledger.read()


# ---------------------------------------------------------------------------
# 1. 缓存解析（纯函数）：新鲜度、市场隔离、缺失字段一律不猜
# ---------------------------------------------------------------------------
class ProbeParseTests(GateTestCase):
    def test_fresh_cache_row_readings_and_age(self):
        raw = probe({"SH": {"pct": 37.5, "industry": "股份制银行Ⅱ", "breach": True,
                            "universe": 28}})
        envelope = v3_risk_gate.parse_risk_probe(raw, market="SH", now=NOW + 60, max_age_ms=60000)
        self.assertEqual(envelope["industry_pct"], 37.5)
        self.assertEqual(envelope["industry_source"], "cache/futu/info_owner_plate")
        self.assertEqual(envelope["top_industry"], "股份制银行Ⅱ")
        self.assertEqual(envelope["probe_age_ms"], 60000.0)
        self.assertEqual(envelope["as_of"], raw["generated_at_iso"])
        self.assertEqual(envelope["missing"], 0)
        self.assertTrue(envelope["breach"])
        self.assertEqual(envelope["cache_state"], "fresh")
        self.assertIsNone(envelope["reason"])

    def test_expired_cache_has_no_reading(self):
        raw = probe({"SH": {"pct": 37.5}})
        envelope = v3_risk_gate.parse_risk_probe(raw, market="SH", now=NOW + 120,
                                                max_age_ms=60000)
        self.assertIsNone(envelope["industry_pct"], "过期读数不得当结论")
        self.assertEqual(envelope["industry_source"], "no-data")
        self.assertEqual(envelope["cache_state"], "stale")
        self.assertTrue(envelope["stale"])
        self.assertIn("过期", envelope["reason"])

    def test_market_without_row_is_no_data(self):
        raw = probe({"HK": {"pct": 40.0}})
        envelope = v3_risk_gate.parse_risk_probe(raw, market="SH", now=NOW, max_age_ms=60000)
        self.assertIsNone(envelope["industry_pct"])
        self.assertIn("没有 SH 的读数", envelope["reason"])

    def test_missing_or_invalid_generated_at_is_no_data(self):
        for broken in ({"per_market": {"SH": {"ok": True, "top_weight_pct": 37.5}}},
                       {"generated_at": "2026-09-20", "per_market": {}},
                       {"generated_at": 0, "per_market": {}}):
            envelope = v3_risk_gate.parse_risk_probe(broken, market="SH", now=NOW)
            self.assertIsNone(envelope["industry_pct"])
            self.assertIn(envelope["cache_state"], ("invalid", "missing"))
        missing = v3_risk_gate.parse_risk_probe(None, market="SH", now=NOW)
        self.assertIsNone(missing["industry_pct"])
        self.assertEqual(missing["cache_state"], "missing")

    def test_max_age_default_matches_observability(self):
        """闸门与 /metrics 的新鲜度口径必须同源（默认 6h）。"""
        envelope = v3_risk_gate.industry_context(self.home, "SH", now=NOW)
        self.assertEqual(envelope["max_age_ms"], observability.risk_probe_max_age() * 1000.0)
        self.assertEqual(envelope["max_age_ms"], 21600000.0)

    def test_market_normalization_and_bad_market(self):
        self.write_probe(probe({"SH": {"pct": 37.5}}))
        # SZ/BJ 归 SH（与 v3_universe 同口径）
        self.assertEqual(v3_risk_gate.industry_context(self.home, "SZ", now=NOW)["industry_pct"],
                         37.5)
        bad = v3_risk_gate.industry_context(self.home, "XX", now=NOW)
        self.assertIsNone(bad["industry_pct"])
        self.assertIn("SH/HK/US", bad["reason"])


# ---------------------------------------------------------------------------
# 2. check_order 纯函数：边界、fail-open 措辞、既有行为不回归
# ---------------------------------------------------------------------------
class CheckOrderTests(unittest.TestCase):
    def test_industry_pct_equal_to_limit_is_not_blocked(self):
        action, reasons = v3_ops.check_order(1000.0, 100000.0, industry_pct=20.0,
                                            industry_source="cache/futu/info_owner_plate")
        self.assertEqual(action, "auto", "== 20% 不阻断（只有 > 才阻断）")
        self.assertEqual(reasons, [], "未超限的读数不产生任何原因")
        action, reasons = v3_ops.check_order(1000.0, 100000.0, industry_pct=20.0001,
                                            industry_source="cache/futu/info_owner_plate",
                                            industry_top="股份制银行Ⅱ",
                                            industry_as_of="2026-09-20T09:58:52+00:00")
        self.assertEqual(action, "blocked_industry")
        self.assertIn("单一行业暴露 20.0% > 20%", reasons[0])
        self.assertIn("top=股份制银行Ⅱ", reasons[0])
        self.assertIn("来源 cache/futu/info_owner_plate", reasons[0])
        self.assertIn("as_of 2026-09-20T09:58:52+00:00", reasons[0])
        self.assertIn("强制阻断", reasons[0])

    def test_industry_red_line_beats_single_order_conclusion(self):
        action, reasons = v3_ops.check_order(50000.0, 100000.0, industry_pct=25.0,
                                            industry_source="cache/x")
        self.assertEqual(action, "blocked_industry")
        self.assertTrue(any("单笔占比 50.00%" in reason for reason in reasons))
        self.assertTrue(any("强制阻断" in reason for reason in reasons))

    def test_missing_marks_reading_as_lower_bound(self):
        action, reasons = v3_ops.check_order(1000.0, 100000.0, industry_pct=10.0,
                                            industry_source="cache/x", industry_missing=3,
                                            industry_universe=28)
        self.assertEqual(action, "auto")
        self.assertTrue(any("下界" in reason for reason in reasons))
        self.assertTrue(any("3 只标的未取到行业分类" in reason for reason in reasons))

    def test_no_data_fail_open_is_explicit(self):
        action, reasons = v3_ops.check_order(1000.0, 100000.0, industry_pct=None,
                                            industry_source="no-data",
                                            industry_reason="探测缓存已过期（90000s > 21600s）")
        self.assertEqual(action, "auto")
        self.assertEqual(reasons, ["行业暴露数据不可用，未参与阻断"
                                   "（原因：探测缓存已过期（90000s > 21600s））"])

    def test_legacy_defaults_unchanged(self):
        """不传行业参数（历史调用口径）→ 逐字段与改动前一致（连 reasons 都不多一条）。"""
        self.assertEqual(v3_ops.check_order(1000.0, 100000.0), ("auto", []))
        self.assertEqual(v3_ops.check_order(10000.0, 100000.0)[0], "manual")
        self.assertEqual(v3_ops.check_order(1000.0, 0)[0], "manual")
        self.assertEqual(v3_ops.check_order(1000.0, 100000.0, drawdown_pct=15.0)[0], "blocked")
        self.assertEqual(v3_ops.check_order(50000.0, 100000.0, drawdown_pct=20.0)[0], "blocked")
        # 显式传 no-data 才算「接了行业口径」→ 才留痕 fail-open
        self.assertEqual(len(v3_ops.check_order(1000.0, 100000.0, industry_source="no-data")[1]),
                         1)

    def test_risk_reasons_detail_shape(self):
        """审计留痕辅助：rule/readings 与 action 一致（行业口径可追溯）。"""
        context = {"nav": 100000.0, "nav_source": "sim-ledger(equity.current)",
                   "drawdown_pct": 3.0, "drawdown_source": "sim-ledger(max_drawdown)",
                   "industry_pct": 37.5, "industry_source": "cache/futu/info_owner_plate"}
        detail = v3_ops.risk_reasons_detail(
            "blocked_industry", ["单一行业暴露 37.5% > 20%，强制阻断"], context,
            {"top_industry": "股份制银行Ⅱ", "market": "SH", "as_of": "2026-09-20T09:58:52Z",
             "probe_age_ms": 3463000.0, "missing": 0, "universe": 28})
        self.assertEqual(detail["rule"], "industry-red-line")
        self.assertEqual(detail["readings"]["industry_pct"], 37.5)
        self.assertEqual(detail["readings"]["industry_top"], "股份制银行Ⅱ")
        self.assertEqual(detail["readings"]["industry_universe"], 28)
        self.assertEqual(detail["reasons"], ["单一行业暴露 37.5% > 20%，强制阻断"])
        self.assertEqual(v3_ops.risk_reasons_detail("blocked", [], {})["rule"],
                         "drawdown-red-line")
        self.assertEqual(v3_ops.risk_reasons_detail("manual", [], {})["rule"], "single-order")
        self.assertEqual(v3_ops.risk_reasons_detail("auto", [], {})["rule"], "within-limits")


# ---------------------------------------------------------------------------
# 3. 台账接入：缓存命中阻断 / 过期现取 / 现取失败 / 多市场 / 留痕
# ---------------------------------------------------------------------------
class LedgerGateTests(GateTestCase):
    def test_fresh_cache_breach_blocks_and_never_calls_upstream(self):
        self.write_probe(probe({"SH": {"pct": 37.5, "industry": "股份制银行Ⅱ", "breach": True,
                                       "universe": 28}}))
        ledger, fake = self.ledger()
        result = self.sync(ledger, [order("CID-SH")])
        self.assertTrue(result["ok"])
        self.assertEqual(result["stages"], {"blocked_industry": 1})
        self.assertEqual(result["industry_pct"], 37.5)
        self.assertIn("cache/futu/info_owner_plate", result["industry_source"])
        # 缓存命中 → 一次行业上游调用都不许发
        for tool in ("info_owner_plate", "plate_list", "plate_stock"):
            self.assertEqual(fake.count(tool), 0, f"缓存命中不该调用 {tool}")

        record = ledger.read()["CID-SH"]
        self.assertEqual(record["stage"], "blocked_industry")
        self.assertEqual(record["risk"]["action"], "blocked_industry")
        self.assertEqual(record["risk"]["rule"], "industry-red-line")
        self.assertEqual(record["industry_pct"], 37.5)
        self.assertEqual(record["industry_source"], "cache/futu/info_owner_plate")
        self.assertEqual(record["industry_top"], "股份制银行Ⅱ")
        self.assertEqual(record["industry_missing"], 0)
        reason = record["risk"]["reasons"][0]
        self.assertIn("单一行业暴露 37.5% > 20%", reason)
        self.assertIn("top=股份制银行Ⅱ", reason)
        self.assertIn("来源 cache/futu/info_owner_plate", reason)
        self.assertIn("as_of 2026-09-20T09:58:52.053639+00:00", reason)
        self.assertIn("市场 SH", reason)
        self.assertIn("强制阻断", reason)

    def test_expired_cache_with_successful_fetch_blocks(self):
        self.write_probe(probe({"SH": {"pct": 37.5}}, generated_at=NOW - 90000))
        self.write_watchlist(["SH.600000", "SH.600009"])
        values = oms_values(
            # 冻结计划给出「SH.600000 / SH.600009 各 50%」→ 两只同属银行业 → 该行业 100% > 20%
            plan={"ok": True, "value": {"plans": [plan([order("CID-SH")], target={
                "SH.600000": 0.5, "SH.600009": 0.5})], "alerts": []}},
            info_owner_plate={"ok": True, "value": {"sectors": [
                {"plate_code": "SH.LIST0949", "plate_type": "INDUSTRY",
                 "plate_sc_name": "股份制银行Ⅱ", "sc_name": "浦发银行"}]}},
        )
        ledger, fake = self.ledger(values)
        result = ledger.sync()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stages"], {"blocked_industry": 1})
        self.assertGreater(fake.count("info_owner_plate"), 0, "过期缓存应触发一次现取")
        record = ledger.read()["CID-SH"]
        self.assertEqual(record["stage"], "blocked_industry")
        self.assertEqual(record["industry_pct"], 100.0)
        self.assertIn("fetch/", record["industry_source"])
        self.assertIn("单一行业暴露 100.0% > 20%", record["risk"]["reasons"][0])
        self.assertIn("强制阻断", record["risk"]["reasons"][0])

    def test_expired_cache_and_failed_fetch_fails_open_with_reason(self):
        self.write_probe(probe({"SH": {"pct": 37.5}}, generated_at=NOW - 90000))
        ledger, fake = self.ledger()
        result = self.sync(ledger, [order("CID-SH")])
        self.assertTrue(result["ok"])
        # 行业不可用**不阻断**：单笔 1% → risk_passed（fail-open），但 reasons 必须写明
        self.assertEqual(result["stages"], {"risk_passed": 1})
        self.assertIsNone(result["industry_pct"])
        self.assertEqual(result["industry_source"], "no-data")
        self.assertIn("现取失败", result["industry_error"] or "")
        record = ledger.read()["CID-SH"]
        self.assertEqual(record["stage"], "risk_passed")
        self.assertEqual(record["industry_source"], "no-data")
        self.assertIsNone(record["industry_pct"])
        self.assertEqual(record["risk"]["rule"], "within-limits")
        reason = record["risk"]["reasons"][0]
        self.assertTrue(reason.startswith("行业暴露数据不可用，未参与阻断（原因："), reason)
        self.assertIn("现取失败", reason)
        self.assertIn("过期", reason)
        self.assertIn("industry/no-universe", reason)

    def test_reading_but_not_breaching_is_auto(self):
        self.write_probe(probe({"SH": {"pct": 15.0, "industry": "银行", "breach": False}}))
        ledger, _fake = self.ledger()
        result = self.sync(ledger, [order("CID-SH")])
        self.assertEqual(result["stages"], {"risk_passed": 1})
        record = ledger.read()["CID-SH"]
        self.assertEqual(record["stage"], "risk_passed")
        self.assertEqual(record["risk"]["reasons"], [])
        self.assertEqual(record["industry_pct"], 15.0)

    def test_limit_boundary_equal_is_not_blocked(self):
        self.write_probe(probe({"SH": {"pct": 20.0, "industry": "银行", "breach": False}}))
        ledger, _fake = self.ledger()
        result = self.sync(ledger, [order("CID-SH")])
        self.assertEqual(result["stages"], {"risk_passed": 1}, "== 20% 不阻断")
        record = ledger.read()["CID-SH"]
        self.assertEqual(record["stage"], "risk_passed")
        self.assertEqual(record["risk"]["action"], "auto")

    def test_single_order_and_drawdown_red_lines_do_not_regress(self):
        self.write_probe(probe({"SH": {"pct": 15.0, "industry": "银行", "breach": False}}))
        ledger, _fake = self.ledger()
        result = self.sync(ledger, [order("CID-BIG", qty=2000, price=10.0)])  # 20% → manual
        self.assertEqual(result["stages"], {"manual": 1})
        record = ledger.read()["CID-BIG"]
        self.assertEqual(record["risk"]["action"], "manual")
        self.assertEqual(record["risk"]["rule"], "single-order")
        self.assertEqual(record["risk"]["reasons"][0], "单笔占比 20.00% > 2%，需人工确认")
        self.assertEqual(len(record["risk"]["reasons"]), 1, "未超限的行业读数不产生原因")

        # 回撤 18% → blocked（drawdown 规则）；**另起一个 home**，不把上一单混进统计
        other_home = Path(tempfile.mkdtemp(prefix="v3gate-"))
        self.addCleanup(shutil.rmtree, other_home, True)
        observability.record_risk_probe(
            other_home, probe({"SH": {"pct": 15.0, "industry": "银行", "breach": False}}))
        fake2 = FakeV3Run(oms_values(
            equity={"ok": True, "value": {"current": 100000.0, "max_drawdown": -0.18}}))
        ledger2 = v3_ops.OmsLedger(fake2, other_home)
        fake2.values["plan"] = {"ok": True, "value": {"plans": [plan([order("CID-DD")])]}}
        result2 = ledger2.sync()
        self.assertEqual(result2["stages"], {"blocked": 1})
        record2 = ledger2.read()["CID-DD"]
        self.assertEqual(record2["stage"], "blocked")
        self.assertEqual(record2["risk"]["rule"], "drawdown-red-line")
        self.assertTrue(any("回撤 18.0% 触及 15% 红线" in reason
                            for reason in record2["risk"]["reasons"]))
        self.assertEqual(record2["industry_pct"], 15.0)

    def test_multi_market_isolation(self):
        self.write_probe(probe({"SH": {"pct": 37.5, "industry": "股份制银行Ⅱ"},
                                "HK": {"pct": 10.0, "industry": "数码解决方案服务"},
                                "US": {"pct": 12.0, "industry": "半导体"}}))
        ledger, _fake = self.ledger()
        result = self.sync(ledger, [order("CID-SH", symbol="SH.600000"),
                                    order("CID-HK", symbol="HK.00700"),
                                    order("CID-US", symbol="US.NVDA")])
        self.assertEqual(result["stages"], {"blocked_industry": 1, "risk_passed": 2})
        rows = ledger.read()
        self.assertEqual(rows["CID-SH"]["stage"], "blocked_industry")
        self.assertEqual(rows["CID-SH"]["industry_pct"], 37.5)
        self.assertEqual(rows["CID-HK"]["stage"], "risk_passed")
        self.assertEqual(rows["CID-HK"]["industry_pct"], 10.0)
        self.assertEqual(rows["CID-HK"]["industry_top"], "数码解决方案服务")
        self.assertEqual(rows["CID-US"]["stage"], "risk_passed")
        self.assertEqual(rows["CID-US"]["industry_pct"], 12.0)

    def test_unattributed_symbol_takes_strictest_reading(self):
        self.write_probe(probe({"SH": {"pct": 37.5}, "HK": {"pct": 40.0},
                                "US": {"pct": 50.0}}))
        ledger, _fake = self.ledger()
        # 裸代码没有市场前缀 → 按最严（US 50%）口径，并写明理由（不跨市场合并）
        result = self.sync(ledger, [order("CID-RAW", symbol="600000")])
        self.assertEqual(result["stages"], {"blocked_industry": 1})
        record = ledger.read()["CID-RAW"]
        self.assertEqual(record["industry_pct"], 50.0)
        self.assertIn("认不出市场前缀", record["industry_reason"])
        self.assertIn("认不出市场前缀", record["history"][-1]["industry_reason"])

    def test_history_trail_fields_are_complete(self):
        self.write_probe(probe({"SH": {"pct": 37.5, "industry": "股份制银行Ⅱ",
                                       "universe": 28, "missing": 0}}))
        ledger, _fake = self.ledger()
        self.sync(ledger, [order("CID-SH")])
        record = ledger.read()["CID-SH"]
        entry = record["history"][-1]
        for key in ("at", "stage", "rule", "changed", "reasons", "industry_pct",
                    "industry_source", "industry_top", "industry_market", "industry_as_of",
                    "industry_probe_age_ms", "industry_missing", "industry_universe",
                    "industry_reason"):
            self.assertIn(key, entry, f"history 留痕缺字段 {key}")
        self.assertEqual(entry["stage"], "blocked_industry")
        self.assertEqual(entry["rule"], "industry-red-line")
        self.assertEqual(entry["changed"], True)
        self.assertEqual(entry["industry_pct"], 37.5)
        self.assertEqual(entry["industry_source"], "cache/futu/info_owner_plate")
        self.assertEqual(entry["industry_market"], "SH")
        self.assertEqual(entry["industry_universe"], 28)
        self.assertIn("强制阻断", entry["reasons"][0])

        # 二次对账：同一态 → changed=false（留痕仍逐次追加，不丢历史）
        self.sync(ledger, [order("CID-SH")])
        again = ledger.read()["CID-SH"]
        self.assertEqual(len(again["history"]), 2)
        self.assertEqual(again["history"][-1]["changed"], False)

    def test_missing_marks_reading_as_lower_bound_in_reasons(self):
        self.write_probe(probe({"SH": {"pct": 18.0, "industry": "银行", "missing": 3,
                                       "universe": 28, "breach": True}}))
        ledger, _fake = self.ledger()
        result = self.sync(ledger, [order("CID-SH")])
        self.assertEqual(result["stages"], {"risk_passed": 1}, "18% 未超限 → 不阻断")
        record = ledger.read()["CID-SH"]
        self.assertEqual(record["industry_missing"], 3)
        reasons = record["risk"]["reasons"]
        self.assertTrue(any("读数只是下界" in reason for reason in reasons), reasons)
        self.assertTrue(any("3 只标的未取到行业分类（28 只标的）" in reason for reason in reasons),
                        reasons)

    def test_history_audit_survives_in_sync_log(self):
        self.write_probe(probe({"SH": {"pct": 37.5, "industry": "股份制银行Ⅱ"}}))
        ledger, _fake = self.ledger()
        result = self.sync(ledger, [order("CID-SH")])
        self.assertEqual(result["industry_pct"], 37.5)
        self.assertEqual(result["stages"], {"blocked_industry": 1})
        log = (self.home / v3_ops.OMS_SYNC_FILENAME).read_text(encoding="utf-8").strip()
        row = json.loads(log.splitlines()[-1])
        self.assertEqual(row["stages"], {"blocked_industry": 1})
        self.assertIn("cache/futu/info_owner_plate", json.dumps(row["industry_markets"]))


# ---------------------------------------------------------------------------
# 4. 端点：/api/v3/metrics 的闸门计数（只加字段、抓取路径不打上游）
# ---------------------------------------------------------------------------
class MetricsGateTests(GateTestCase):
    def setUp(self):
        super().setUp()
        values = oms_values(schedule={"ok": True, "value": {"heartbeat": {"at": DAY}, "jobs": []}})
        self.fake = FakeV3Run(values)
        app = FastAPI()
        v3_ops.register(app, self.fake, self.home)
        v3_ops.reset_counters()
        self.client = TestClient(app)
        self.fake.calls.clear()

    def test_metrics_exposes_blocked_industry_and_does_not_probe(self):
        self.write_probe(probe({"SH": {"pct": 37.5, "industry": "股份制银行Ⅱ"}}))
        ledger = v3_ops.OmsLedger(self.fake, self.home)
        self.fake.values["plan"] = {"ok": True,
                                    "value": {"plans": [plan([order("CID-SH")])]}}
        ledger.sync()
        before = len(self.fake.calls)
        body = self.client.get("/api/v3/metrics").json()
        self.assertTrue(body["ok"])
        gate = body["industryGate"]
        self.assertEqual(gate["blockedIndustry"], 1)
        self.assertEqual(gate["blockedByIndustry"], 1)
        self.assertEqual(gate["blockedByDrawdown"], 0)
        self.assertEqual(gate["industryPct"], 37.5)
        self.assertIn("cache/futu/info_owner_plate", gate["industrySource"])
        self.assertEqual(gate["industryLimitPct"], 20.0)
        self.assertFalse(gate["failOpen"])
        self.assertEqual(gate["perMarket"]["SH"], 37.5)
        self.assertIn("blocked_industry", body["oms"])
        # 抓取路径只发 schedule 这一次（闸门读数只读落盘缓存）
        industry_tools = [name for name, _payload in self.fake.calls[before:]
                          if name in ("info_owner_plate", "plate_list", "plate_stock")]
        self.assertEqual(industry_tools, [])

    def test_metrics_reports_fail_open_when_no_reading(self):
        body = self.client.get("/api/v3/metrics").json()
        gate = body["industryGate"]
        self.assertEqual(gate["blockedIndustry"], 0)
        self.assertIsNone(gate["industryPct"])
        self.assertEqual(gate["industrySource"], "no-data")
        self.assertTrue(gate["failOpen"])

    def test_metrics_refresh_never_calls_the_tool_face(self):
        """抓取路径只读缓存：反复拉 ``/api/v3/metrics`` 不得增加任何工具调用。"""
        first = self.client.get("/api/v3/metrics").json()
        self.assertEqual(first["mcp"]["calls"], 1)  # 只有 schedule（workbenchUp 心跳）
        second = self.client.get("/api/v3/metrics").json()
        self.assertEqual(second["mcp"]["calls"], 2, second["mcp"])
        self.assertEqual(second["mcp"]["tools"], {"schedule": 2})
        self.assertEqual(second["industryGate"]["perMarket"],
                         {"HK": None, "SH": None, "US": None})

    def test_oms_view_carries_industry_fields(self):
        self.write_probe(probe({"SH": {"pct": 37.5, "industry": "股份制银行Ⅱ"}}))
        body = self.client.get("/api/v3/oms/orders").json()
        self.assertEqual(body["industry_pct"], 37.5)
        self.assertIn("cache/futu/info_owner_plate", body["industry_source"])
        self.assertEqual(body["industry_limit_pct"], 20.0)
        self.assertEqual(body["industry_markets"]["SH"]["industry_pct"], 37.5)
        self.assertEqual(body["industry_markets"]["SH"]["top_industry"], "股份制银行Ⅱ")
        self.assertIsNone(body["industry_markets"]["HK"]["industry_pct"],
                          "缓存里没有 HK 行 → 如实 null，不拿 SH 的数顶替")
        self.assertEqual(body["stages"], {})

    def test_expired_cache_is_reported_as_no_data_in_view(self):
        self.write_probe(probe({"SH": {"pct": 37.5}}, generated_at=NOW - 90000))
        body = self.client.get("/api/v3/oms/orders").json()
        self.assertIsNone(body["industry_pct"])
        self.assertEqual(body["industry_source"], "no-data")


if __name__ == "__main__":
    unittest.main()
