// 期权筛选表单 → option_screen 载荷的纯函数（2026-09-18 用户需求：「能改成选项或输入框吗？
// 而不是这种 json 格式的」）。宿主无关，node --test 直测；页面只负责接线与展示错误。
//
// 这里锁三件事：
//   1. **真机示例逐字段一致** —— 表单生成的载荷必须与 docs/TOOL-LIMITS.md 里 2026-09-17
//      真机验证过的那份**逐键逐序**相同（连 JSON 字符串都相同），否则「表单默认值可用」
//      只是口号；
//   2. **不生成注定被拒的载荷** —— 空 field_filter（上游 -3 / 服务端前置校验拒绝）在本地
//      就给出可读错误，而不是发一次必败的请求；
//   3. **枚举不编造** —— 市场类别只放真机验证过的 7 个（非支持值会被上游静默忽略，
//      等于给用户一个「看起来成功、其实没筛」的陷阱），指标类型/返回字段允许自由输入
//      但默认值只用已验证值。
import test from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_OPTION_SCREEN_FORM, OPTION_FIELD_FILTER_FIELDS, OPTION_MARKET_CATEGORIES,
  buildOptionScreenFilter, formatOptionScreenFilter, parseOptionScreenJson,
} from "../src/services/optionScreen.js";

// docs/TOOL-LIMITS.md「期权筛选（option_screen）的最小可用载荷（2026-09-17 真机验证）」
// 的 filter 原文（此处按 JSON 文本比对，键序也算契约：真机就是这么发的）。
const VERIFIED_FILTER_JSON = '{"strategy":{"market_category_list":[0],'
  + '"filter_group_list":[{"option_list":[{"indicator_type":1003,'
  + '"indicator_value":{"value_list":[1]}}]}]},"field_filter":{"option_type":1,'
  + '"volume":1,"implied_volatility":1},"limit":3}';

/** 真机示例对应的表单态（在默认表单上只改三处：开指标条件、limit 3）。 */
function verifiedForm() {
  return { ...DEFAULT_OPTION_SCREEN_FORM, withIndicator: true, limit: 3 };
}

test("OPTION_MARKET_CATEGORIES：只放真机验证过的 7 个类别码（0..6），不提供自由输入", () => {
  assert.deepEqual(OPTION_MARKET_CATEGORIES.map((row) => row.value), [0, 1, 2, 3, 4, 5, 6]);
  // label 里带上游英文码，用户与文档能对上号
  const codes = ["US_STOCK", "US_INDEX", "US_FUTURE", "HK_STOCK", "HK_INDEX", "JP_STOCK",
                 "JP_INDEX"];
  assert.deepEqual(
    OPTION_MARKET_CATEGORIES.map((row) => codes.find((code) => row.label.includes(code))),
    codes);
  for (const row of OPTION_MARKET_CATEGORIES) {
    assert.equal(typeof row.label, "string");
    assert.ok(row.label.length > 0);
  }
});

test("OPTION_FIELD_FILTER_FIELDS：预置的 3 个字段名就是真机验证过的那三个", () => {
  assert.deepEqual(OPTION_FIELD_FILTER_FIELDS.map((row) => row.value),
                   ["option_type", "volume", "implied_volatility"]);
});

test("DEFAULT_OPTION_SCREEN_FORM：市场类别 [0]，默认不带指标条件与排序", () => {
  assert.deepEqual(DEFAULT_OPTION_SCREEN_FORM.marketCategories, [0]);
  assert.equal(DEFAULT_OPTION_SCREEN_FORM.withIndicator, false);
  assert.equal(DEFAULT_OPTION_SCREEN_FORM.indicatorType, 1003);
  assert.deepEqual(DEFAULT_OPTION_SCREEN_FORM.indicatorValues, [1]);
  assert.deepEqual(DEFAULT_OPTION_SCREEN_FORM.fieldFilter,
                   ["option_type", "volume", "implied_volatility"]);
  assert.equal(DEFAULT_OPTION_SCREEN_FORM.limit, 20);
  assert.equal(DEFAULT_OPTION_SCREEN_FORM.sortEnabled, false);
});

test("buildOptionScreenFilter：与真机验证过的最小载荷逐字段（含键序）一致", () => {
  const { filter, error } = buildOptionScreenFilter(verifiedForm());
  assert.equal(error, undefined);
  assert.equal(JSON.stringify(filter), VERIFIED_FILTER_JSON);
});

test("buildOptionScreenFilter：默认表单（不加指标条件）filter_group_list 给空数组", () => {
  const { filter, error } = buildOptionScreenFilter(DEFAULT_OPTION_SCREEN_FORM);
  assert.equal(error, undefined);
  assert.deepEqual(filter.strategy, { market_category_list: [0], filter_group_list: [] });
  assert.deepEqual(filter.field_filter,
                   { option_type: 1, volume: 1, implied_volatility: 1 });
  assert.equal(filter.limit, 20);
  // tests/test_wp8_market.py 的 option_screen 用例正是用空数组形状
});

test("buildOptionScreenFilter：指标类型/值从标签输入里取数字串也归一成数字", () => {
  const { filter } = buildOptionScreenFilter({
    ...DEFAULT_OPTION_SCREEN_FORM, withIndicator: true,
    indicatorType: "1003", indicatorValues: ["1", "2"],
  });
  assert.deepEqual(filter.strategy.filter_group_list, [{ option_list: [
    { indicator_type: 1003, indicator_value: { value_list: [1, 2] } }] }]);
});

test("buildOptionScreenFilter：市场类别为空 → 可读错误，不生成载荷", () => {
  const bad = buildOptionScreenFilter({ ...DEFAULT_OPTION_SCREEN_FORM, marketCategories: [] });
  assert.equal(bad.filter, undefined);
  assert.match(bad.error, /市场类别/);
});

test("buildOptionScreenFilter：field_filter 为空 → 可读错误并点明上游 -3 / 服务端拒绝", () => {
  for (const fieldFilter of [[], ["", "  "]]) {
    const bad = buildOptionScreenFilter({ ...DEFAULT_OPTION_SCREEN_FORM, fieldFilter });
    assert.equal(bad.filter, undefined, JSON.stringify(fieldFilter));
    assert.match(bad.error, /field_filter/);
    assert.match(bad.error, /-3/);
    assert.match(bad.error, /TOOL-LIMITS/);
  }
});

test("buildOptionScreenFilter：指标条件开关打开但类型/值非法 → 可读错误", () => {
  for (const patch of [{ indicatorType: null }, { indicatorType: "abc" },
                       { indicatorValues: [] }, { indicatorValues: ["x"] }]) {
    const bad = buildOptionScreenFilter({ ...DEFAULT_OPTION_SCREEN_FORM, withIndicator: true,
                                          ...patch });
    assert.equal(bad.filter, undefined, JSON.stringify(patch));
    assert.match(bad.error, /指标/, JSON.stringify(patch));
  }
});

test("buildOptionScreenFilter：limit 为空不写键，非法 limit 报错", () => {
  for (const limit of [null, undefined, ""]) {
    const { filter, error } = buildOptionScreenFilter({ ...DEFAULT_OPTION_SCREEN_FORM, limit });
    assert.equal(error, undefined, String(limit));
    assert.equal("limit" in filter, false, String(limit));
  }
  for (const limit of [-1, 1001, 1.5, "abc"]) {
    const bad = buildOptionScreenFilter({ ...DEFAULT_OPTION_SCREEN_FORM, limit });
    assert.equal(bad.filter, undefined, String(limit));
    assert.match(bad.error, /limit|条数/);
  }
});

test("buildOptionScreenFilter：sort_obj 仅在启用且字段非空时写", () => {
  const enabled = buildOptionScreenFilter({ ...DEFAULT_OPTION_SCREEN_FORM, sortEnabled: true,
                                            sortField: "volume", sortAsc: false });
  assert.deepEqual(enabled.filter.sort_obj, { sort_field: "volume", is_asc: false });
  assert.equal(JSON.stringify(enabled.filter.sort_obj), '{"sort_field":"volume","is_asc":false}',
               "真机形状见 tests/test_wp7_futu_data.py:180");
  const asc = buildOptionScreenFilter({ ...DEFAULT_OPTION_SCREEN_FORM, sortEnabled: true,
                                        sortField: "volume", sortAsc: true });
  assert.equal(asc.filter.sort_obj.is_asc, true);
  for (const patch of [{ sortEnabled: true, sortField: "" },
                       { sortEnabled: true, sortField: "   " },
                       { sortEnabled: false, sortField: "volume" }]) {
    const { filter } = buildOptionScreenFilter({ ...DEFAULT_OPTION_SCREEN_FORM, ...patch });
    assert.equal("sort_obj" in filter, false, JSON.stringify(patch));
  }
});

test("parseOptionScreenJson：合法 filter 原样返回（未知键也保留，不吞字段）", () => {
  const text = '{"strategy": {"market_category_list": [0]}, "field_filter": {"option_type": 1},'
    + ' "limit": 3, "next_key": "abc"}';
  const { filter, error } = parseOptionScreenJson(text);
  assert.equal(error, undefined);
  assert.deepEqual(filter, JSON.parse(text));
});

test("parseOptionScreenJson：非 JSON / 非对象 / 空对象 / 缺键 / 空键 → 可读错误", () => {
  const cases = [
    ["{", /JSON/],
    ["[]", /非空对象/],
    ["{}", /非空对象/],
    ['{"field_filter": {"option_type": 1}}', /strategy/],
    ['{"strategy": {"market_category_list": [1]}}', /field_filter/],
    ['{"strategy": {}, "field_filter": {"option_type": 1}}', /strategy/],
    ['{"strategy": {"market_category_list": [1]}, "field_filter": {}}', /field_filter/],
    ['{"strategy": {"market_category_list": [1]}, "field_filter": []}', /field_filter/],
    ["", /JSON/],
  ];
  for (const [text, pattern] of cases) {
    const result = parseOptionScreenJson(text);
    assert.equal(result.filter, undefined, text);
    assert.match(result.error, pattern, text);
  }
});

test("parseOptionScreenJson：值形状校验留给服务端（透传 + 原样展示拒绝原因）", () => {
  // 服务端 futu_data._is_field_filter_placeholder 才是占位规则的唯一实现；前端再抄一份
  // 必然漂移（这里是「不抄第二份」的锁：非法值形状解析通过，由服务端给原因）。
  const text = '{"strategy": {"market_category_list": [1]}, "field_filter": {"option_type": []}}';
  const { filter, error } = parseOptionScreenJson(text);
  assert.equal(error, undefined);
  assert.deepEqual(filter, JSON.parse(text));
});

test("formatOptionScreenFilter：稳定美化（同一 filter 永远同一文本，可解析回原值）", () => {
  const { filter } = buildOptionScreenFilter(verifiedForm());
  const text = formatOptionScreenFilter(filter);
  assert.equal(text, formatOptionScreenFilter(filter));
  assert.equal(text, JSON.stringify(filter, null, 2));
  assert.deepEqual(JSON.parse(text), JSON.parse(VERIFIED_FILTER_JSON));
  assert.equal(formatOptionScreenFilter(null), "{}");
});
