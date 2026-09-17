// 规则候选池展示纯函数（WP14 任务 4）。宿主无关，node --test 直测。
// 断言锚点：状态枚举来自 rule_engine.RULE_STATUSES（未知状态原样回显，不猜档位）；
// 批准/停用的可用性来自 rule_engine.decide_rule 与 RULE_TRANSITIONS 的真实放行集；
// 验证摘要字段来自 factors.ic_report / passes_gate（t_stat/n/gate_reasons/passed）。
import test from "node:test";
import assert from "node:assert/strict";
import {
  RULE_STATUS, canDisable, canEnable, decideDisabledReason, ruleRows, ruleRowsForTable,
  statusColor, statusLabel, validationSummary, walkforwardSummary,
} from "../src/services/rules.js";

const PASSED = {
  rule_id: "news_momentum_v1", status: "passed",
  hypothesis: "公告超预期 + 资金流入 → 短期动量",
  factors: ["momentum_20", "ep"], combine: "zscore_equal_weight",
  universe: "watchlist.SH", top_n: 5, rebalance: "weekly",
  provenance: { research_run_id: "R-1" }, created_at: "2026-09-16 10:00:00",
  validation: { passed: true, gate_reasons: [],
    factors: { momentum_20: { t_stat: 2.41, n: 21 }, ep: { t_stat: 3.02, n: 21 } } },
};

test("statusLabel/statusColor：六个状态有中文标签；未知状态原样回显不猜", () => {
  assert.equal(statusLabel("candidate"), "候选");
  assert.equal(statusLabel("validating"), "验证中");
  assert.equal(statusLabel("passed"), "已通过");
  assert.equal(statusLabel("failed"), "未通过");
  assert.equal(statusLabel("enabled"), "已启用");
  assert.equal(statusLabel("disabled"), "已停用");
  assert.equal(statusLabel("archived"), "archived", "未知码原样回显");
  assert.equal(statusLabel(null), "—");
  assert.equal(statusLabel(""), "—");
  assert.equal(Object.keys(RULE_STATUS).length, 6);
  assert.equal(statusColor("failed"), "red");
  assert.equal(statusColor("archived"), "default");
});

test("canEnable：只有 passed 可启用（与服务端唯一放行条件一致）", () => {
  assert.equal(canEnable(PASSED), true);
  for (const status of ["candidate", "validating", "failed", "enabled", "disabled"]) {
    assert.equal(canEnable({ status }), false, status);
  }
  assert.equal(canEnable(null), false);
});

test("canDisable：candidate/failed/passed/enabled 可停用；disabled 是终态", () => {
  for (const status of ["candidate", "failed", "passed", "enabled"]) {
    assert.equal(canDisable({ status }), true, status);
  }
  assert.equal(canDisable({ status: "disabled" }), false);
  assert.equal(canDisable({ status: "validating" }), false);
});

test("decideDisabledReason：不可用时给出原因，可用时为 null", () => {
  assert.equal(decideDisabledReason(PASSED, "enable"), null);
  assert.equal(decideDisabledReason(PASSED, "disable"), null);
  assert.match(decideDisabledReason({ rule_id: "r", status: "candidate" }, "enable"),
               /仅「已通过」的规则可启用（当前：候选）/);
  assert.equal(decideDisabledReason({ rule_id: "r", status: "enabled" }, "enable"), "已启用");
  assert.match(decideDisabledReason({ rule_id: "r", status: "disabled" }, "disable"),
               /当前状态不可停用（已停用）/);
  assert.match(decideDisabledReason({ status: "passed" }, "enable"), /缺少 rule_id/);
  assert.match(decideDisabledReason(PASSED, "remove"), /未知决定 remove/);
});

test("validationSummary：未验证/通过/未通过三态如实表达（并带样本外状态）", () => {
  assert.equal(validationSummary({ status: "candidate" }), "尚未验证（先跑 rules-validate）");
  assert.equal(validationSummary(PASSED),
               "通过：momentum_20(t=2.41, n=21)、ep(t=3.02, n=21)；样本外：未请求");
  assert.equal(
    validationSummary({ validation: { passed: false,
      gate_reasons: ["momentum_20：t 检验不显著：t=1.20 < 2.0"], factors: {} } }),
    "未通过：momentum_20：t 检验不显著：t=1.20 < 2.0；样本外：未请求");
  assert.equal(validationSummary({ validation: { passed: false, gate_reasons: [] } }),
               "未通过（未记录原因）；样本外：未请求");
  // 统计缺失时用 — 而不是编造 0
  assert.equal(validationSummary({ validation: { passed: true, factors: { f: {} } } }),
               "通过：f(t=—, n=—)；样本外：未请求");
});

test("walkforwardSummary：样本外状态三态如实表达（批准人必须看得到）", () => {
  // ① 未请求（缺字段 / not_run）
  assert.equal(walkforwardSummary(undefined), "样本外：未请求");
  assert.equal(walkforwardSummary(null), "样本外：未请求");
  assert.equal(walkforwardSummary({}), "样本外：未请求");
  assert.equal(walkforwardSummary({ status: "not_run",
    reason: "--walkforward 需同时给 --start 与 --end（与 backtest 同口径）" }),
    "样本外：未请求——--walkforward 需同时给 --start 与 --end（与 backtest 同口径）");
  // ② 未跑通 + 原因
  assert.equal(walkforwardSummary({ status: "failed", reason: "规则注册失败：combine 非法" }),
               "样本外：未跑通——规则注册失败：combine 非法");
  assert.equal(walkforwardSummary({ status: "failed" }), "样本外：未跑通");
  assert.equal(walkforwardSummary({ status: "no_folds", reason: "数据窗口不足，未产出 OOS 折" }),
               "样本外：未产出 OOS 折——数据窗口不足，未产出 OOS 折");
  // ③ 已跑 N 折
  assert.equal(walkforwardSummary({ status: "ok", folds: 3 }), "样本外：已跑 3 折");
  assert.equal(walkforwardSummary({ status: "ok" }), "样本外：已跑（折数未记录）");
  // 未知状态原样回显，不猜档位
  assert.equal(walkforwardSummary({ status: "archived" }), "样本外：archived");
});

test("ruleRows：参数/来源/批准逐项落地，缺值显示 —", () => {
  const rows = Object.fromEntries(ruleRows(PASSED).map((row) => [row.label, row.value]));
  assert.equal(rows["假设"], PASSED.hypothesis);
  assert.equal(rows["因子"], "momentum_20、ep");
  assert.equal(rows["合成"], "zscore_equal_weight");
  assert.equal(rows["池 / 市场"], "watchlist.SH");
  assert.equal(rows["Top-N / 再平衡"], "5 / weekly");
  assert.equal(rows["研究来源"], "R-1");
  assert.equal(rows["批准"], "—", "未批准不得显示批准人");
  const approved = Object.fromEntries(
    ruleRows({ ...PASSED, approved_by: "web", approved_at: "2026-09-16 20:00:00" })
      .map((row) => [row.label, row.value]));
  assert.equal(approved["批准"], "web @ 2026-09-16 20:00:00");
  const empty = Object.fromEntries(ruleRows({}).map((row) => [row.label, row.value]));
  assert.equal(empty["因子"], "—");
  assert.equal(empty["Top-N / 再平衡"], "— / —");
  assert.equal(empty["样本外"], "样本外：未请求", "展开区也要能看到样本外是否跑过");
  const withOos = Object.fromEntries(
    ruleRows({ ...PASSED,
      validation: { ...PASSED.validation, walkforward: { status: "ok", folds: 3 } } })
      .map((row) => [row.label, row.value]));
  assert.equal(withOos["样本外"], "样本外：已跑 3 折");
});

test("ruleRowsForTable：稳定 rowKey，缺 rule_id 时按序补位", () => {
  const rows = ruleRowsForTable([PASSED, { status: "candidate" }, null]);
  assert.equal(rows[0].key, "news_momentum_v1");
  assert.equal(rows[1].key, "row-1");
  assert.equal(rows[2].key, "row-2");
  assert.deepEqual(ruleRowsForTable(null), [], "非数组输入退化为空表");
});
