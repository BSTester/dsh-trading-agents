"""WP14 任务 1：规则协议校验器与 rules 表状态机。全部离线（内存库）。

覆盖：validate_spec 白名单/取值域/未成熟因子拒绝/来源纪律；rules 表持久化与
状态流转（candidate→validating→passed/failed→enabled/disabled，非法流转报错）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import rule_engine, store  # noqa: E402


def _spec(**over):
    spec = {"rule_id": "news_momentum_v1", "hypothesis": "公告超预期+资金流入→短期动量",
            "factors": ["momentum_20"], "combine": "zscore_equal_weight",
            "universe": "watchlist.SH", "top_n": 5, "rebalance": "weekly",
            "provenance": {"research_run_id": "R1", "created_by": "harness",
                           "created_at": "2026-09-16 10:00:00"}}
    spec.update(over)
    return spec


REGISTRY = {"momentum_20": object(), "momentum_60": object(), "ep": object()}


class ValidateSpecTest(unittest.TestCase):
    def test_ok(self):
        ok, errors = rule_engine.validate_spec(_spec(), registry=REGISTRY)
        self.assertTrue(ok, errors)
        self.assertEqual(errors, [])

    def test_unknown_factor_rejected(self):
        ok, errors = rule_engine.validate_spec(_spec(factors=["no_such_factor"]),
                                              registry=REGISTRY)
        self.assertFalse(ok)
        self.assertTrue(any("未注册" in e for e in errors), errors)

    def test_combine_whitelist(self):
        ok, errors = rule_engine.validate_spec(_spec(combine="magic_ml"), registry=REGISTRY)
        self.assertFalse(ok)
        self.assertTrue(any("combine" in e for e in errors), errors)

    def test_top_n_bounds(self):
        for bad in (0, -1, 101, "5"):
            ok, errors = rule_engine.validate_spec(_spec(top_n=bad), registry=REGISTRY)
            self.assertFalse(ok, f"top_n={bad!r} 应被拒绝")

    def test_missing_and_unknown_fields(self):
        spec = _spec()
        del spec["rule_id"]
        ok, errors = rule_engine.validate_spec(spec, registry=REGISTRY)
        self.assertFalse(ok)
        self.assertTrue(any("rule_id" in e for e in errors), errors)

        ok, errors = rule_engine.validate_spec(_spec(extra_key=1), registry=REGISTRY)
        self.assertFalse(ok)
        self.assertTrue(any("extra_key" in e for e in errors), errors)

    def test_empty_factors_and_rebalance_domain(self):
        ok, errors = rule_engine.validate_spec(_spec(factors=[]), registry=REGISTRY)
        self.assertFalse(ok)
        ok, errors = rule_engine.validate_spec(_spec(rebalance="hourly"), registry=REGISTRY)
        self.assertFalse(ok)

    def test_immature_factor_rejected(self):
        """演进条款的机械执行：资讯/F10/做空域因子未满 250 交易日不得进规则。"""
        for name in ("sentiment_score_5d", "f10_analyst_consensus", "short_interest_3d"):
            reg = dict(REGISTRY, **{name: object()})
            ok, errors = rule_engine.validate_spec(_spec(factors=[name]), registry=reg)
            self.assertFalse(ok, f"{name} 应被拒绝")
            self.assertTrue(any("未满 250 交易日" in e for e in errors), errors)

    def test_explicit_immature_set(self):
        ok, errors = rule_engine.validate_spec(_spec(factors=["ep"]), registry=REGISTRY,
                                              immature={"ep"})
        self.assertFalse(ok)
        self.assertTrue(any("未满 250 交易日" in e for e in errors), errors)

    def test_provenance_must_be_harness(self):
        spec = _spec(provenance={"research_run_id": "R1", "created_by": "human",
                                 "created_at": "2026-09-16 10:00:00"})
        ok, errors = rule_engine.validate_spec(spec, registry=REGISTRY)
        self.assertFalse(ok)
        self.assertTrue(any("harness" in e for e in errors), errors)

    def test_default_registry_path(self):
        """生产路径：不传 registry 时用 factors.REGISTRY（真实注册因子可过，未注册不过）。"""
        reg = rule_engine.validate_spec(_spec(factors=["momentum_20"]))[0]
        self.assertTrue(reg)
        self.assertFalse(rule_engine.validate_spec(_spec(factors=["definitely_not_a_factor"]))[0])

    def test_accumulates_multiple_errors(self):
        """一次返回全部问题（提案方一次修完，不挤牙膏）。"""
        ok, errors = rule_engine.validate_spec(
            _spec(factors=["nope"], combine="nope", top_n=0), registry=REGISTRY)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(errors), 3)


class RulesTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_upsert_and_get(self):
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"))
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1", top_n=3))  # 幂等覆盖
        rules = store.get_rules(self.conn)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["rule_id"], "r1")
        self.assertEqual(rules[0]["status"], "candidate")
        self.assertEqual(rules[0]["spec"]["top_n"], 3)
        self.assertEqual(rules[0]["spec"]["combine"], "zscore_equal_weight")

    def test_full_lifecycle(self):
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"))
        rule_engine.set_rule_status(self.conn, "r1", "validating")
        rule_engine.set_rule_status(self.conn, "r1", "passed")
        self.assertEqual(store.get_rules(self.conn, status="passed")[0]["rule_id"], "r1")
        rule_engine.decide_rule(self.conn, "r1", "enable", by="web")
        row = store.get_rules(self.conn)[0]
        self.assertEqual(row["status"], "enabled")
        self.assertEqual(row["approved_by"], "web")
        self.assertTrue(row["approved_at"])
        rule_engine.decide_rule(self.conn, "r1", "disable", by="web")
        self.assertEqual(store.get_rules(self.conn)[0]["status"], "disabled")

    def test_illegal_transition_rejected(self):
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"))
        with self.assertRaises(ValueError):
            rule_engine.decide_rule(self.conn, "r1", "enable", by="web")  # candidate 不能直接启用
        with self.assertRaises(ValueError):
            rule_engine.set_rule_status(self.conn, "r1", "enabled")      # 同上，绕不过
        rule_engine.set_rule_status(self.conn, "r1", "validating")
        with self.assertRaises(ValueError):
            rule_engine.decide_rule(self.conn, "r1", "enable", by="web")  # validating 不可启用

    def test_validation_report_recorded(self):
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"))
        rule_engine.set_rule_status(self.conn, "r1", "validating")
        rule_engine.set_rule_status(self.conn, "r1", "failed", validation={"rank_ic_mean": 0.01,
                                                                          "monotonic": False})
        row = store.get_rules(self.conn)[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["validation"]["monotonic"], False)

    def test_unknown_rule_and_decision(self):
        with self.assertRaises(ValueError):
            rule_engine.decide_rule(self.conn, "nope", "enable", by="web")
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"))
        rule_engine.set_rule_status(self.conn, "r1", "validating")
        rule_engine.set_rule_status(self.conn, "r1", "passed")
        with self.assertRaises(ValueError):
            rule_engine.decide_rule(self.conn, "r1", "approve", by="web")  # 未知决定

    def test_status_filter(self):
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"))
        store.upsert_rule(self.conn, "r2", _spec(rule_id="r2"))
        rule_engine.set_rule_status(self.conn, "r2", "validating")
        self.assertEqual([r["rule_id"] for r in store.get_rules(self.conn, status="candidate")],
                         ["r1"])


if __name__ == "__main__":
    unittest.main()
