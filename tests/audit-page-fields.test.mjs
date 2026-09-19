// 字段审计工具的自洽性锁（scripts/audit_page_fields.mjs）。
//
// 审计工具的价值全在「判定」是否可信，而判定由两半组成：
//   ① 期望文本（CONTRACTS）——按任务书的格式契约**独立实现**，不与页面共用同一份代码，
//      否则「实现有 bug」会被工具原样祝福；本测试把它与 services/formatCore.js 的
//      真实实现逐值比对，两处漂移先红（工具与产品口径必须一致，但不能是同一份代码）；
//   ② 路径解析（resolvePath）——候选键 / 数组摊平 / 下标，取错就会把「后端没有」与
//      「页面没显示」判反。
// 另断言 16 个路由都有规格、每条字段引用的端点都在该页 endpoints 里声明（漏声明会让
// 整条字段静默变成 UNJUDGED，等于没测）。
import test from "node:test";
import assert from "node:assert/strict";

import {
  PAGE_SPEC, contractOf, judgeField, renderedFor, resolvePath,
} from "../scripts/audit_page_fields.mjs";
import {
  clockText, dayText, minuteText, numText, pctOfText, stampText,
} from "../platform/web/lib/services/formatCore.js";
import { fmtNum } from "../platform/web/lib/services/f10.js";

const ROUTES = ["overview", "market", "capital", "options", "signal", "portfolio", "risk",
  "factors", "execution", "research", "events", "plan", "pipeline", "schedule", "audit",
  "settings"];

test("16 个路由都有字段规格，且字段引用的端点都在该页声明（含交互 phase）", () => {
  assert.deepEqual(PAGE_SPEC.map((page) => page.key), ROUTES);
  const problems = [];
  for (const page of PAGE_SPEC) {
    if (page.fields.length === 0) problems.push(`${page.key} 没有任何字段规格`);
    // phase（交互取证）自带端点表与动作：字段只管自己那一份，两处都要能查到。
    const scopes = [[page.key, page.endpoints, page.fields]];
    for (const phase of page.phases ?? []) {
      if (typeof phase.action !== "function") problems.push(`${page.key}.${phase.key} 缺 action`);
      if (!phase.key) problems.push(`${page.key} 有 phase 缺 key`);
      scopes.push([`${page.key}.${phase.key}`, phase.endpoints ?? {}, phase.fields ?? []]);
    }
    for (const [scope, declared, fields] of scopes) {
      for (const field of fields) {
        if (!field.label || !field.where || !field.kind) {
          problems.push(`${scope} 字段缺 label/where/kind：${JSON.stringify(field)}`);
        }
        if (!Object.prototype.hasOwnProperty.call(declared, field.endpoint)) {
          problems.push(`${scope} 字段「${field.label}」引用了未声明端点 ${field.endpoint}`);
        }
        if (!field.path && !field.paths && typeof field.values !== "function") {
          problems.push(`${scope} 字段「${field.label}」没有取值路径/取值函数`);
        }
      }
    }
  }
  assert.deepEqual(problems, [], problems.join("\n"));
});

test("契约与 formatCore 真实实现逐值一致（工具不得自成一派）", () => {
  const values = [0, 1, 9.07, -12935913, 1234567.891, 0.0123, 0.472, 31.19];
  const pairs = [
    ["num", (value) => numText(value)],
    ["num0", (value) => numText(value, 0)],
    ["num3", (value) => numText(value, 3)],
    ["count", (value) => numText(value, 0)],
    ["pctRatio", (value) => pctOfText(value)],
    ["pctValue", (value) => `${Number(value).toFixed(2)}%`],
    ["raw", (value) => String(value)],
    ["text", (value) => String(value)],
    ["rawString", (value) => String(value)],
  ];
  for (const [kind, render] of pairs) {
    for (const value of values) {
      assert.deepEqual(contractOf(value, kind).accept, [render(value)],
        `${kind}(${value}) 的期望文本与 formatCore 不一致`);
    }
  }
  const stamps = ["2026-09-19T02:31:16", "2026-09-19T02:31:16.281Z", "2026-09-18 20:50:24"];
  for (const value of stamps) {
    assert.deepEqual(contractOf(value, "stamp").accept, [stampText(value)]);
    assert.deepEqual(contractOf(value, "stampMinute").accept, [stampText(value).slice(0, 16)]);
  }
  assert.ok(contractOf(1789695000000, "timeMs").accept.includes(clockText(1789695000000)));
  // 微秒/毫秒时刻列：与 formatCore.minuteText 同口径，且 16 位整数是**错误形态**
  for (const value of ["1789363901000000", 1789695000000, "2026-09-18T11:46:28.281Z"]) {
    assert.ok(contractOf(value, "microStamp").accept.includes(minuteText(value)),
      `microStamp(${value}) 与 minuteText 不一致`);
  }
  assert.match("1789363901000000", contractOf("1789363901000000", "microStamp").reject);
  assert.doesNotMatch(minuteText("1789363901000000"), /^\d{11,}$/);
  assert.match(minuteText("1789363901000000"), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.ok(contractOf(1786291200000, "dateMs").accept.includes(dayText(1786291200000)));
  // timeMs/dateMs 的核心判据：13 位毫秒整数是**错误形态**，必须被 reject 命中
  assert.match("1789695000000", contractOf(1789695000000, "timeMs").reject);
  assert.match("1786291200", contractOf(1786291200000, "dateMs").reject);
  assert.equal(contractOf(null, "num").accept[0], "—");
});

test("resolvePath：候选键取第一个有值的、[] 摊平、下标与缺失", () => {
  const payload = {
    flow_list: [{ in_flow: 0, super_in_flow: 12 }, { in_flow: 5 }],
    snapshots: [{ payload: { tickers: { a: 1 } } }],
    diffs: [{ local: 3, local_value: 9 }],
  };
  assert.deepEqual(resolvePath(payload, ["flow_list", "[]", "in_flow"]), [0, 5]);
  assert.deepEqual(
    resolvePath(payload, ["flow_list", "[]", ["super_inflow", "super_in_flow"]]), [12, undefined]);
  assert.deepEqual(resolvePath(payload, ["snapshots", 0, "payload", "tickers"]),
    [{ a: 1 }]);
  assert.deepEqual(resolvePath(payload, ["diffs", "[]", ["local", "local_value"]]), [3]);
  // 路径走不通时返回空数组（调用方据此判 EMPTY / UNJUDGED，而不是当成 0）
  assert.deepEqual(resolvePath(payload, ["nope", "[]", "x"]), []);
  assert.deepEqual(resolvePath(null, ["a"]), []);
});

test("numFixed2 与 f10.fmtNum 逐值一致（固定小数位的唯一口径）", () => {
  for (const value of [0, 1, 61.4, 253, 999809.29, 1310.636, -12935913, 0.5]) {
    assert.deepEqual(contractOf(value, "numFixed2").accept, [fmtNum(value)],
      `numFixed2(${value}) 与 f10.fmtNum 不一致`);
  }
  // 与 num 的差别正是「补零」：这是 antd Statistic precision={2} 与 fmtNum 的共同行为
  assert.equal(contractOf(61.4, "num").accept[0], "61.4");
  assert.equal(contractOf(61.4, "numFixed2").accept[0], "61.40");
  // 非有限值两端同为原样字符串/—（与 fmtNum 的 MISSING 一致）
  assert.deepEqual(contractOf(null, "numFixed2").accept, ["—"]);
  assert.deepEqual(contractOf("", "numFixed2").accept, ["—"]);
});

test("daysUntil：负数=已过去，0 与正数=剩余（与事件页文案一致）", () => {
  assert.deepEqual(contractOf(-127, "daysUntil").accept, ["127 天前"]);
  assert.deepEqual(contractOf(0, "daysUntil").accept, ["0 天后"]);
  assert.deepEqual(contractOf(3, "daysUntil").accept, ["3 天后"]);
  assert.deepEqual(contractOf(null, "daysUntil").accept, ["—"]);
});

test("对象值不再被 String() 兜底成「期望文本」——[object Object] 必须判缺陷", () => {
  const contract = contractOf({ qty: 5200 }, "num");
  assert.deepEqual(contract.accept, []);
  assert.match("[object Object]", contract.reject);
  // 端到端：后端给对象、页面 num() 渲染成 [object Object] ⇒ FABRICATED（不是 ok）
  const endpoints = {
    reconcile: { name: "reconcile", value: { diffs: [{ symbol: "SH.600089", broker: { qty: 5200 } }] } },
  };
  const field = { label: "券商", where: "table_column", table: "对账差异", endpoint: "reconcile",
    path: ["diffs", "[]", ["broker", "broker_value"]], kind: "num" };
  const probe = { items: [], tables: [{ card: "对账差异", headers: ["券商"], rows: [["[object Object]"]] }] };
  const judged = judgeField(field, { key: "audit" }, endpoints, probe, "SH.600000");
  assert.equal(judged.judge, "FABRICATED");
  assert.match(judged.note, /\[object Object\]/);
});

test("renderedFor：时间线按项全文+Tag 取值，键值行按次要色标签配对", () => {
  const probe = {
    items: [], tables: [],
    timeline: [
      { tag: "分红/除权除息", text: "分红/除权除息 2026-05-15 每股派息 5.3 HKD · 财年 2025 127 天前" },
      { tag: "分红/除权除息", text: "分红/除权除息 2025-09-11 每股派息 0.01 USD 373 天前" },
    ],
    pairs: [{ label: "平均隐含波动率", value: "1,310.64" }, { label: "IV 状态", value: "UNDERVALUED" }],
  };
  const text = renderedFor({ where: "timeline" }, probe, {});
  assert.deepEqual(text.values, probe.timeline.map((item) => item.text));
  assert.equal(text.labelFound, true);
  const tags = renderedFor({ where: "timeline", from: "tag" }, probe, {});
  assert.deepEqual(tags.values, ["分红/除权除息", "分红/除权除息"]);
  const pair = renderedFor({ where: "pair", label: "平均隐含波动率" }, probe, {});
  assert.deepEqual(pair.values, ["1,310.64"]);
  assert.equal(renderedFor({ where: "pair", label: "不存在" }, probe, {}).labelFound, false);
  // 时间线一条都没有时 labelFound=false（页面确实没渲染这块）
  assert.equal(renderedFor({ where: "timeline" }, { timeline: [] }, {}).labelFound, false);
});
