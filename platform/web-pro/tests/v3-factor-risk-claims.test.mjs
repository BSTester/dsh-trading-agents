// V3.0 规格补全（FR-EXEC-003 四子项 / FR-STRAT-001 四类因子 / Headless 表述）的**源码级契约**。
//
// 为什么是源码级：工作台无 jsdom，这些断言钉的是「页面真的接了服务端读数、缺数据时显示
// 「无数据源 + 原因」而不是 0/占位」——渲染层断言留给人工与 /api/v3 的真实读数。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");
const read = (rel) => readFileSync(join(SRC, rel), "utf8");

// ===== A. 风险监控页：FR-EXEC-003 四个子项真的进了页面 =====

test("A1 风险页消费 analytics.risk_detail，并逐项渲染四个子项", () => {
  const source = read("pages/risk.jsx");
  assert.match(source, /env\.risk_detail/, "risk.jsx 未读 risk_detail（四个子项的载体）");
  for (const key of ["leverage", "liquidity", "attribution", "fundsCheck"]) {
    assert.match(source, new RegExp(`detail\\.${key}`), `risk.jsx 未渲染 risk_detail.${key}`);
  }
  // 四项各自必须能显示「无数据源 + 原因」，不能把缺失当 0
  assert.match(source, /<NoSource what="杠杆率" why=\{leverage\.error/, "杠杆率缺读数时未走 NoSource");
  assert.match(source, /liquidity\.grade_note \|\| liquidity\.reason/, "流动性缺读数时未显示原因");
  assert.match(source, /methods\.factor/, "因子归因未展示「缺 PIT 建仓敞口」的原因");
  assert.match(source, /funds\.reason/, "资金检查未展示原因");
});

test("A2 流动性读数必须带 ADV 窗口与来源（口径可核）", () => {
  const source = read("pages/risk.jsx");
  assert.match(source, /adv_window_days/, "未显示 ADV 窗口（任务要求注明用的哪个窗口）");
  assert.match(source, /adv_source/, "未显示 ADV 来源（K 线链路）");
  assert.match(source, /participation_pct/, "未显示参与率");
});

test("A3 杠杆率不再声称「positions 不返回融资余额」这种越界结论", () => {
  const source = read("pages/risk.jsx");
  assert.doesNotMatch(source, /positions 不返回融资余额与盘口深度/,
    "旧文案把「上游无融资字段」写成「positions 不返回」，且与已实现的杠杆率读数相矛盾");
  assert.match(source, /margin_debt_pct|provider_note|融资负债/,
    "融资负债那一项必须如实标注为上游不提供（no-data）");
});

test("A4 无数据源清单只剩真正没有数据源的项（杠杆率/流动性已实现）", () => {
  const source = read("pages/risk.jsx");
  const start = source.indexOf("function NoSourceCard()");
  const body = source.slice(start, source.indexOf("/* ── 页面", start));
  assert.doesNotMatch(body, /what: "杠杆率"/, "杠杆率已实现，不该再列在无数据源清单");
  assert.doesNotMatch(body, /what: "流动性评分"/, "流动性参与率已实现，不该再列在无数据源清单");
  assert.match(body, /融资负债 \/ 净资产/, "真正的缺口（融资负债倍数）必须留在清单里并写清原因");
  assert.match(body, /因子归因/, "因子归因仍缺 PIT 敞口，必须留在清单里");
});

// ===== B. 策略页：FR-STRAT-001 六类因子 =====

test("B1 策略页因子类别覆盖六类（价值/成长/动量/质量/情绪/另类）", () => {
  const source = read("pages/strategy.jsx");
  for (const label of ["价值", "成长", "动量", "质量", "情绪", "另类"]) {
    assert.match(source, new RegExp(`label: "${label}"`), `因子类别缺 ${label}`);
  }
  // 类别规则顺序：质量/成长必须在动量之后、情绪/另类之前按名字精确匹配
  assert.match(source, /revenue_yoy\|net_profit_yoy/, "成长因子未按真实因子名归类");
  assert.match(source, /roe\|roa\|gross_margin/, "质量因子未按真实因子名归类");
  assert.match(source, /capital_flow\|short_interest/, "另类新因子未归类");
});

test("B2 因子表显示覆盖率（有数据的标的数）并把缺数据的因子写清原因", () => {
  const source = read("pages/strategy.jsx");
  assert.match(source, /baseEnv\.factors/, "未读 factors/matrix 的覆盖率数组");
  assert.match(source, /coverageOf\(name\)/, "表格行未接覆盖率");
  assert.match(source, /覆盖（有数据的标的）/, "缺覆盖率列");
  assert.match(source, /baseEnv\.factorsMissing/, "未展示缺席因子的原因");
  assert.match(source, /factors\/registry/, "未指向六类因子注册表端点");
});

test("B3 五阶段流水线的扩维口径在页面上可见（rankBy / factorCoverage）", () => {
  const source = read("pages/strategy.jsx");
  assert.match(source, /rankBy/, "PCPT 的 rankBy 未展示（扩维打分是否生效看不出来）");
  assert.match(source, /factorCoverage/, "PAAT 的 factorCoverage 未展示");
});

// ===== C. Headless 表述：前端不得留失效断言 =====

test("C1 风险页不再写「本服务未挂载 SDK/Headless 通道」", () => {
  const source = read("pages/risk.jsx");
  assert.doesNotMatch(source, /本服务未挂载 SDK JSON-RPC \/ Headless 通道/,
    "Headless Runner 已实现（server/v3_headless.py），该断言与事实相反");
});

test("C2 预览页 Headless 通道文案来自服务端 reason，不写死「未挂载」", () => {
  const source = read("pages/overview.jsx");
  assert.match(source, /br\.headless\.reason|headlessReason/,
    "overview.jsx 的 Headless 说明必须取服务端 reason");
  assert.doesNotMatch(source, /Headless 通道未挂载/,
    "「未挂载」是失效断言：应显示服务端的真实状态（已实现 / 已注册 / 未接线）");
});

test("C3 两个页面都不再对 Headless 状态写死「无数据源」", () => {
  for (const key of ["overview", "risk"]) {
    const source = read(`pages/${key}.jsx`);
    assert.doesNotMatch(source, /Headless[^。\n]{0,20}无数据源/,
      `${key}.jsx 把 Headless 一律写成无数据源（today/last 都有真实读数）`);
  }
});
