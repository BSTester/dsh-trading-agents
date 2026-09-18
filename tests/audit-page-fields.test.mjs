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
  PAGE_SPEC, contractOf, resolvePath,
} from "../scripts/audit_page_fields.mjs";
import {
  clockText, dayText, minuteText, numText, pctOfText, stampText,
} from "../platform/web/src/services/formatCore.js";

const ROUTES = ["overview", "market", "capital", "options", "signal", "portfolio", "risk",
  "factors", "execution", "research", "events", "plan", "pipeline", "schedule", "audit",
  "settings"];

test("16 个路由都有字段规格，且字段引用的端点都在该页声明", () => {
  assert.deepEqual(PAGE_SPEC.map((page) => page.key), ROUTES);
  const problems = [];
  for (const page of PAGE_SPEC) {
    if (page.fields.length === 0) problems.push(`${page.key} 没有任何字段规格`);
    for (const field of page.fields) {
      if (!field.label || !field.where || !field.kind) {
        problems.push(`${page.key} 字段缺 label/where/kind：${JSON.stringify(field)}`);
      }
      if (!Object.prototype.hasOwnProperty.call(page.endpoints, field.endpoint)) {
        problems.push(`${page.key} 字段「${field.label}」引用了未声明端点 ${field.endpoint}`);
      }
      if (!field.path && !field.paths && typeof field.values !== "function") {
        problems.push(`${page.key} 字段「${field.label}」没有取值路径/取值函数`);
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
