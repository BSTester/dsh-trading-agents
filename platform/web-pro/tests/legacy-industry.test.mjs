// 「存量单不渲染成 0.00%」回归断言集（2026-09-20 数字诚实性缺口）。
//
// 真实缺陷：`/api/v3/oms/orders` 的 10 笔存量订单带的是**行业闸门上线前**落盘的判定快照
//   industry_pct: 0.0
//   industry_source: "no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）"
// 两处不诚实：① 那句「工具面无行业分类数据源」在 `futu/info_owner_plate` 接通后已失效
// （实测 SH 37.5% / HK 40% / US 50%）；② 页面把 `0.0` 渲染成「行业暴露 0.00%」，
// 而事实是**当时没有行业读数**，不是 0。
//
// 后端（`platform/server/v3_ops.py` 的 `order_view`）已在返回视图里归一化成
// `industry_pct=null` + `legacy_pre_gate=true` + 如实文案；本文件钉住前端的两件事：
//   A. 纯函数层 `orderIndustryView`：闸门前记录 → 「—」+ 历史判定说明，**绝不**「0.00%」；
//      闸门后真读数照常显示；闸门后 `null` 是 fail-open；**真 0 仍显示 0.00%**（不能一刀切）。
//   B. 源码层：执行页确实走这个纯函数，且旧的内联 `fmt.pct(current.industry_pct)` 不再回来。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  LEGACY_INDUSTRY_SOURCE, RETIRED_INDUSTRY_SOURCE_TEXT, orderIndustryView,
} from "../src/lib/risk-labels.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");
const read = (rel) => readFileSync(join(SRC, rel), "utf8");

/** 闸门前的存量单逐字段同形（`~/.dsh/v3-oms-orders.json` 实测快照）。 */
const LEGACY_AS_STORED = {
  id: "e59ef4b6fffa4819b137d6bb4fb79631",
  ticker: "SH.600000",
  stage: "manual",
  value: 29931.0,
  updated_at: "2026-09-19T19:15:06.630146+00:00",
  industry_pct: 0.0,
  industry_source: "no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）",
  risk: { action: "manual", reasons: ["单笔占比 2.99% > 2%，需人工确认"] },
  history: [{ at: "2026-09-19T12:25:45.641546+00:00", stage: "manual",
              reasons: ["单笔占比 2.99% > 2%，需人工确认"] }],
};

/** 同一条记录经后端 `order_view` 归一化后的**返回视图**（industry_pct 已是 null）。 */
const LEGACY_AS_VIEWED = {
  ...LEGACY_AS_STORED,
  industry_pct: null,
  industry_source: LEGACY_INDUSTRY_SOURCE,
  industry_graded: false,
  legacy_pre_gate: true,
  industry_note: "该判定未包含行业红线：登记时行业闸门尚未接入，台账没有行业读数——"
    + "这里的「—」是「当时没读到」，不是「行业暴露 0%」。原始 risk.reasons / history 原样保留，"
    + "本视图不回写、不重判。",
};

const FRESH_ORDER = {
  id: "CID-NEW", ticker: "SH.600000", stage: "blocked_industry",
  industry_pct: 37.5,
  industry_source: "cache/futu/info_owner_plate",
  industry_as_of: "2026-09-20T12:00:00+00:00",
  industry_probe_age_ms: 12000,
  industry_graded: true,
  legacy_pre_gate: false,
  risk: { action: "blocked_industry", rule: "industry-red-line",
          reasons: ["单一行业暴露 37.5% > 20%（top=股份制银行Ⅱ，来源 cache/futu/info_owner_plate），强制阻断"] },
};

// ===== A. 纯函数：逐单行业读数 =====

test("A1 闸门前旧记录（后端未归一化）→ 「—」+ 历史判定，既不显示 0.00% 也不复述失效断言", () => {
  const view = orderIndustryView(LEGACY_AS_STORED);
  assert.equal(view.legacyPreGate, true);
  assert.equal(view.hasReading, false, "闸门前没有行业读数：hasReading 必须为 false");
  assert.equal(view.pct, null);
  assert.match(view.text, /—/, "没有读数必须落「—」");
  assert.match(view.text, /历史判定/);
  assert.match(view.text, /未含行业红线/);
  assert.doesNotMatch(view.text, /0\.00\s*%/, "存量单不得再渲染成「行业暴露 0.00%」");
  assert.equal(view.sourceLine.includes(RETIRED_INDUSTRY_SOURCE_TEXT), false,
    "那句「工具面无行业分类数据源」已失效，不得再上屏");
  assert.match(view.note, /未包含行业红线/);
  assert.match(view.note, /不是「行业暴露 0%」/);
});

test("A2 后端归一化后的返回视图 → 同样「—」+ 历史判定（两条路径不可能漂移）", () => {
  const view = orderIndustryView(LEGACY_AS_VIEWED);
  assert.equal(view.legacyPreGate, true);
  assert.equal(view.hasReading, false);
  assert.equal(view.text, "行业暴露 — · 历史判定（未含行业红线）");
  assert.doesNotMatch(JSON.stringify(view), /0\.00\s*%/);
  assert.equal(view.source, LEGACY_INDUSTRY_SOURCE);
  // 后端给的说明优先原样上屏（前端不另编一套措辞）
  assert.equal(view.note, LEGACY_AS_VIEWED.industry_note);
});

test("A3 闸门后的新订单 → 真实读数 + 来源 + as_of + 探测年龄", () => {
  const view = orderIndustryView(FRESH_ORDER);
  assert.equal(view.legacyPreGate, false);
  assert.equal(view.hasReading, true);
  assert.equal(view.text, "行业暴露 37.50%");
  assert.match(view.sourceLine, /cache\/futu\/info_owner_plate/);
  assert.match(view.sourceLine, /as_of 2026-09-20 12:00:00/);
  assert.match(view.sourceLine, /探测年龄 12s/);
  assert.doesNotMatch(view.text, /历史判定/, "真读数不能带历史判定文案");
  assert.equal(view.note, null);
});

test("A4 闸门后但无新鲜读数（industry_pct=null）→ 未参与阻断（fail-open），不是历史判定", () => {
  const view = orderIndustryView({
    industry_pct: null, industry_source: "no-data",
    industry_graded: true, legacy_pre_gate: false,
  });
  assert.equal(view.legacyPreGate, false);
  assert.equal(view.hasReading, false);
  assert.match(view.text, /未参与阻断（fail-open）/);
  assert.doesNotMatch(view.text, /历史判定/);
  assert.doesNotMatch(view.text, /0\.00\s*%/);
});

test("A5 真 0 仍显示 0.00% —— 不能把「没有数据」与「确实是 0」一起抹掉", () => {
  const view = orderIndustryView({
    industry_pct: 0, industry_source: "cache/futu/info_owner_plate",
    industry_as_of: "2026-09-20T12:00:00+00:00", industry_probe_age_ms: 1500,
    industry_graded: true, legacy_pre_gate: false,
  });
  assert.equal(view.legacyPreGate, false);
  assert.equal(view.hasReading, true);
  assert.equal(view.text, "行业暴露 0.00%", "闸门后真实读数为 0 时必须显示 0.00%");
});

test("A6 台账压根没有 industry_* 字段 → 如实说「未记录」，不替它下结论", () => {
  const view = orderIndustryView({ id: "A", ticker: "SH.600000", stage: "manual" });
  assert.equal(view.legacyPreGate, false);
  assert.match(view.text, /台账未记录行业读数/);
  assert.doesNotMatch(view.text, /fail-open/, "没有字段不等于「闸门 fail-open」");
  assert.doesNotMatch(view.text, /历史判定/);
});

test("A7 非对象输入不抛异常，且落「—」", () => {
  for (const bad of [null, undefined, 0, "x"]) {
    const view = orderIndustryView(bad);
    assert.equal(view.present, false);
    assert.match(view.text, /—/);
  }
});

test("A8 兜底识别只看那句已失效的断言，不看时间戳", () => {
  // 同一 shape，只把失效断言换成一个真实来源 → 不再判为历史判定
  const repaired = { ...LEGACY_AS_STORED, industry_source: "cache/futu/info_owner_plate" };
  assert.equal(orderIndustryView(repaired).legacyPreGate, false);
  // 带日期但来源正常的记录不得被判成历史判定（不猜时间）
  const dated = { ...FRESH_ORDER, updated_at: "2020-01-01T00:00:00+00:00" };
  assert.equal(orderIndustryView(dated).legacyPreGate, false);
});

// ===== B. 源码层：执行页确实走纯函数，旧的 0.00% 渲染不再回来 =====

test("B1 执行页逐单行业读数走 orderIndustryView（不再内联 fmt.pct(industry_pct)）", () => {
  const source = read("pages/execution.jsx");
  assert.match(source, /import \{[^}]*orderIndustryView[^}]*\} from "\.\.\/lib\/risk-labels\.js"/,
    "execution.jsx 未从 lib/risk-labels.js 引入 orderIndustryView");
  assert.match(source, /orderIndustryView\(current\)/, "未对当前订单调用 orderIndustryView");
  assert.doesNotMatch(source, /fmt\.pct\(current\.industry_pct\)/,
    "内联 fmt.pct(current.industry_pct) 会把闸门前的 0.0 渲染成 0.00%");
  assert.doesNotMatch(source, /来源 \$\{current\.industry_source \|\| "no-data"\}/,
    "不得再把台账的原始 industry_source 直接上屏（历史记录里是已失效的断言）");
});

test("B2 历史判定的说明行确实渲染在「风控阈值口径」里（可见，不只悬停）", () => {
  const source = read("pages/execution.jsx");
  assert.match(source, /industryOfOrder\.sourceLine/, "缺来源/说明行");
  assert.match(source, /industryOfOrder\.note/, "缺历史判定说明（悬停 + 说明行都要有出处）");
  assert.match(source, /<Tooltip title=\{industryOfOrder\.note \|\| industryOfOrder\.sourceLine\}>/,
    "缺悬停说明");
});

test("B3 兜底常量与后端同源（失效断言只作为识别依据，不再作为展示文案）", () => {
  const lib = read("lib/risk-labels.js");
  assert.match(lib, /RETIRED_INDUSTRY_SOURCE_TEXT = "工具面无行业分类数据源"/);
  assert.match(lib, /LEGACY_INDUSTRY_SOURCE = "历史判定/);
  // 兜底常量只出现在识别分支里；展示用的 source 一律是 LEGACY_INDUSTRY_SOURCE
  assert.match(lib, /String\(order\.industry_source \|\| ""\)\.includes\(RETIRED_INDUSTRY_SOURCE_TEXT\)/);
});
