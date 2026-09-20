// 「数字诚实性」回归断言集（2026-09-20 审计 H1–H12 修复）。
//
// 两层：
//   A. 纯函数层：`src/lib/stat-core.js` 的四态判定 —— 缺数据必须落 `—` + 原因，
//      **真 0 必须保留为 0**（否则「没有数据」与「确实是 0」又混在一起）。
//   B. 源码层：钉住已修掉的回退值不再回来（antd `Statistic` 的 `value` 默认 0、
//      `String(null)` 渲染字面量 null 是实测行为，不是猜测：
//      `node_modules/antd/es/statistic/Statistic.js` 解构默认 `value = 0`；
//      `node_modules/antd/es/statistic/Number.js` 走 `String(value)`）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { STAT, statState, statText, statNote, statNotes } from "../src/lib/stat-core.js";
import {
  STAGE_LABELS, industryGateView, isBlockedStage, probeAgeText, ruleLabel, stageLabel, stageTone,
} from "../src/lib/risk-labels.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");
const read = (rel) => readFileSync(join(SRC, rel), "utf8");

// ===== A. 纯函数：四态判定 =====

test("statState：加载中 → loading + 「加载中…」（不给 0）", () => {
  const state = statState({ loading: true }, 0);
  assert.equal(state.state, STAT.LOADING);
  assert.equal(state.value, null);
  assert.match(state.reason, /加载中/);
  assert.equal(statText(state), "—");
});

test("statState：取数失败 → error + 真实错误原文", () => {
  const state = statState({ loading: false, error: "HTTP 500" }, 0);
  assert.equal(state.state, STAT.ERROR);
  assert.match(state.reason, /HTTP 500/);
  assert.equal(statText(state), "—");
});

test("statState：字段缺失（undefined / null / 空串）→ missing + 调用方原因", () => {
  for (const missing of [undefined, null, ""]) {
    const state = statState({ loading: false }, missing, { missingReason: "接口未返回 mcp.calls" });
    assert.equal(state.state, STAT.MISSING, `值 ${String(missing)} 应判 missing`);
    assert.equal(statText(state), "—");
    assert.equal(statNote(state), "接口未返回 mcp.calls");
  }
});

test("statState：接口确实返回 0 → value 0（真 0 必须保留，不能变「—」）", () => {
  const state = statState({ loading: false }, 0);
  assert.equal(state.state, STAT.VALUE);
  assert.equal(statText(state), 0);
  assert.equal(statNote(state), null);
});

test("statText 永不返回 undefined/null —— 否则 antd Statistic 会渲染成 0", () => {
  const states = [
    statState({ loading: true }, undefined),
    statState({ loading: false, error: "boom" }, undefined),
    statState({ loading: false }, undefined),
    statState(null, 5),
  ];
  for (const state of states) {
    const text = statText(state);
    assert.notEqual(text, undefined);
    assert.notEqual(text, null);
    assert.equal(text, "—");
  }
});

test("statNotes：只列非真实读数项，全有读数时为空（页面不渲染该行）", () => {
  const ok = statNotes([
    ["调用", statState({ loading: false }, 0)],
    ["失败", statState({ loading: false }, 0)],
  ]);
  assert.deepEqual(ok, []);
  const mixed = statNotes([
    ["调用", statState({ loading: false }, 3)],
    ["失败", statState({ loading: true }, 0)],
  ]);
  assert.equal(mixed.length, 1);
  assert.match(mixed[0], /^失败：/);
});

// ===== B. 源码层：审计点名的回退值不得复活 =====

test("gateway.jsx：MCP 计数不再用 `?? 0` 兜底（H1）", () => {
  const src = read("pages/gateway.jsx");
  assert.ok(!src.includes("metrics.mcp?.calls ?? 0"), "gateway.jsx 又出现 calls ?? 0");
  assert.ok(!src.includes("metrics.mcp?.errors ?? 0"), "gateway.jsx 又出现 errors ?? 0");
  assert.ok(!src.includes("metrics.mcp?.avgMs ?? 0"), "gateway.jsx 又出现 avgMs ?? 0");
  assert.ok(src.includes("statState("), "gateway.jsx 未走三态判定");
});

test("tools.jsx：Statistic 不再收到 undefined（H2）与逐域/逐工具 0 兜底（H3）", () => {
  const src = read("pages/tools.jsx");
  assert.ok(!src.includes("? undefined"), "tools.jsx 又出现把 undefined 交给 Statistic 的写法");
  // H3 的要害不在求和本身，而在**渲染**：metrics 未取到时必须渲染「—」而不是 0。
  // 故钉住两处渲染都经过 statText(state)，且状态由 metrics 信封推导。
  assert.match(src, /\$\{all\.length\} 个工具 · 今日 \$\{statText\(callsState\)\}/, "域卡「今日」未走三态渲染");
  assert.match(src, /今日 \$\{statText\(toolCallsStat\(tool\.name\)\)\}/, "工具行「今日」未走三态渲染");
  assert.ok(src.includes("const domainCallsState = (sum) => callsEnvState()"),
    "域计数未按 metrics 信封决定三态（取不到时不得回落 0）");
  assert.ok(!/value=\{domainKeys\.length > 0 \? domainKeys\.length : undefined\}/.test(src),
    "tools.jsx 工具域 Statistic 又收到 undefined");
});

test("tools.jsx：不再用码位构造「示例」再替换成「样例」（H13）", () => {
  const src = read("pages/tools.jsx");
  assert.ok(!src.includes("String.fromCharCode(0x793a"), "tools.jsx 又出现码位构造的词面替换");
  assert.ok(!src.includes('join("样例")'), "tools.jsx 又出现同义替换");
  assert.ok(src.includes("replace(/\\*\\*/g"), "tools.jsx 应只保留 markdown 强调符归一化");
});

test("strategy.jsx：回测指标缺失传「—」而不是 null（H4）", () => {
  const src = read("pages/strategy.jsx");
  assert.ok(!src.includes("Number(metrics.annReturnPct) : null"), "strategy.jsx 又给 Statistic 传 null");
  assert.ok(!src.includes("Number(metrics.sharpe) : null"), "strategy.jsx 夏普又传 null");
  assert.match(src, /value=\{has \? Number\(card\.value\) : "—"\}/, "strategy.jsx 未显式传「—」");
});

test("overview.jsx：红线阈值标注来源，且不再声称「回撤阈值取自台账」（H5/H6）", () => {
  const src = read("pages/overview.jsx");
  assert.ok(!src.includes("回撤阈值取自台账"), "overview.jsx 又出现不实声明");
  assert.ok(!/limit: 2,/.test(src), "overview.jsx 又硬编码单笔 2%");
  assert.ok(!/limit: 20,/.test(src), "overview.jsx 又硬编码行业 20%");
  assert.ok(!/limit: 15,/.test(src), "overview.jsx 又硬编码回撤 15%");
  assert.ok(src.includes("BUILTIN_LIMIT_LABEL"), "overview.jsx 缺少内置常量标注");
  assert.ok(src.includes("risk/industry"), "overview.jsx 未读行业上限接口字段");
});

test("overview.jsx：reasons 缺失不再断言「阈值内」（H7）", () => {
  const src = read("pages/overview.jsx");
  assert.ok(!src.includes('|| ["阈值内"]'), "overview.jsx 又在 reasons 缺失时断言阈值内");
  assert.ok(src.includes("未返回判定依据"), "overview.jsx 未给出「未返回判定依据」");
});

test("risk.jsx：单笔阈值改由台账原文回读、图注刻度与图形同源（H8/H9）", () => {
  const src = read("pages/risk.jsx");
  assert.ok(!src.includes('threshold: "≤ 权益 2%（OMS check_order 口径）"'), "risk.jsx 又用前端字面量 2% 当阈值");
  assert.ok(!/singleRatio > 2 \?/.test(src), "risk.jsx 又用字面量 2 判定超限");
  assert.ok(src.includes("从台账 risk.reasons 原文回读"), "risk.jsx 未标注阈值来源");
  assert.ok(!src.includes("满刻度 30%"), "risk.jsx 又写死满刻度 30%");
  assert.ok(src.includes("<BarList max={barScalePct}"), "risk.jsx 条形未使用与图注同一个满刻度变量");
});

test("execution.jsx：TTL 回退常量已删除，缺失显示未取到（H10）", () => {
  const src = read("pages/execution.jsx");
  assert.ok(!src.includes("FALLBACK_TTL_SECONDS"), "execution.jsx 又出现 TTL 回退常量");
  assert.ok(src.includes("未取到 confirmation.ttl_ms"), "execution.jsx 未显式说明 TTL 未取到");
  assert.ok(!src.includes("单笔占比 ≤ 2%"), "execution.jsx 又硬编码单笔 2%");
});

test("settings.jsx：双人复核如实写未启用，阈值标注内置常量（H11/H12）", () => {
  const src = read("pages/settings.jsx");
  assert.ok(!src.includes("双人复核后执行"), "settings.jsx 又宣称存在双人复核");
  assert.ok(src.includes("未启用"), "settings.jsx 未写「未启用」");
  assert.ok(src.includes("内置常量"), "settings.jsx 阈值未标注内置常量");
});

test("research.jsx：五个计数走三态，不再拿 length 当读数（H14）", () => {
  const src = read("pages/research.jsx");
  assert.ok(src.includes("statState("), "research.jsx 未走三态判定");
  assert.ok(src.includes("statText("), "research.jsx 未走三态渲染");
  assert.ok(!/fontVariantNumeric: "tabular-nums" \}\}>\{item\.value\}/.test(src), "research.jsx 又直接渲染原始 length");
});

test("brain.jsx：不再用固定文案顶替缺失字段（H15）", () => {
  const src = read("pages/brain.jsx");
  assert.ok(!src.includes('value || "经人工审批后执行"'), "brain.jsx 表列又用固定建议文案兜底");
  assert.ok(!src.includes('item.action_hint || "经人工审批后执行"'), "brain.jsx 提案列表又用固定建议文案兜底");
  assert.ok(!src.includes('paat.factorsError ? String(paat.factorsError) : "0 条"'), "brain.jsx 因子错误又拿「0 条」兜底");
});

// ===== C. 行业闸门（2026-09-20 后端新增 blocked_industry 态）=====

test("blocked_industry 必须按红色/阻断渲染，不再落成灰色（G1）", () => {
  assert.equal(stageTone("blocked_industry"), "red");
  assert.equal(stageTone("blocked"), "red");
  assert.equal(isBlockedStage("blocked_industry"), true);
  assert.equal(isBlockedStage("blocked"), true);
  assert.equal(isBlockedStage("manual"), false);
  assert.match(stageLabel("blocked_industry"), /行业/);
  assert.ok(STAGE_LABELS.blocked_industry, "缺 blocked_industry 标签");
});

test("risk.rule 机器码显示成人话（G2）", () => {
  assert.equal(ruleLabel("industry-red-line"), "行业红线");
  assert.equal(ruleLabel("drawdown-red-line"), "回撤红线");
  assert.equal(ruleLabel("single-order"), "单笔超限");
  assert.equal(ruleLabel("within-limits"), "阈值内");
  assert.equal(ruleLabel(null), null);
  assert.equal(ruleLabel("unknown-rule"), "unknown-rule", "未知规则不得硬翻成中文");
});

test("industryGate：未取到 industryGate 时如实说不可用，不猜（G3）", () => {
  const view = industryGateView(undefined);
  assert.equal(view.available, false);
  assert.equal(view.hasReading, false);
  assert.match(view.reason, /未返回 industryGate/);
});

test("industryGate：industryPct=null（no-data）→ fail-open，且绝不显示 0%（G4）", () => {
  const view = industryGateView({
    industryLimitPct: 20, industryPct: null, industrySource: "no-data",
    industryTop: null, industryAsOf: null, industryProbeAgeMs: null, failOpen: true,
  });
  assert.equal(view.available, true);
  assert.equal(view.hasReading, false);
  assert.equal(view.failOpen, true);
  assert.equal(view.pct, undefined);
  assert.equal(view.limitPct, 20);
  assert.match(view.reason, /未参与阻断/);
  assert.match(view.reason, /不是「暴露 0%」/);
});

test("industryGate：真实读数 → 上限比较与来源字段齐全（G5）", () => {
  const view = industryGateView({
    industryLimitPct: 20, industryPct: 37.5, industrySource: "cache/futu/info_owner_plate",
    industryTop: "股份制银行Ⅱ", industryAsOf: "2026-09-20T09:58:52+00:00", industryProbeAgeMs: 123.4,
  });
  assert.equal(view.hasReading, true);
  assert.equal(view.failOpen, false);
  assert.equal(view.pct, 37.5);
  assert.equal(view.breach, true);
  assert.equal(view.top, "股份制银行Ⅱ");
  assert.equal(view.source, "cache/futu/info_owner_plate");
  assert.equal(probeAgeText(123.4), "123ms");
  assert.equal(probeAgeText(1500), "2s");
  assert.equal(probeAgeText(null), "—");
});

test("前端不再留「行业上限只做展示、未接入自动阻断」的过时文案（G6）", () => {
  const execution = read("pages/execution.jsx");
  const risk = read("pages/risk.jsx");
  assert.ok(!execution.includes("未接入自动阻断"), "execution.jsx 仍写「未接入自动阻断」");
  assert.ok(!execution.includes("行业上限只做展示"), "execution.jsx 仍写「行业上限只做展示」");
  assert.ok(!execution.includes("不参与阻断"), "execution.jsx 仍写「行业不参与阻断」");
  assert.ok(!risk.includes("行业红线只做展示与人工核对"), "risk.jsx 仍写「行业红线只做展示」");
  assert.ok(!risk.includes("超限不自动阻断订单"), "risk.jsx 仍写「超限不自动阻断订单」");
  assert.ok(execution.includes("isBlockedStage"), "execution.jsx 未用统一的阻断态判定");
  assert.ok(!execution.includes('["blocked", "rejected"].includes'), "execution.jsx 仍按字面量判阻断色");
  assert.ok(risk.includes("blocked_industry"), "risk.jsx 未提及 blocked_industry");
});
