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
  DEFAULT_OPTION_SCREEN_FORM, MIN_SMILE_ROWS, OPTION_FIELD_FILTER_FIELDS,
  OPTION_MARKET_CATEGORIES, OPTION_UNKNOWN, buildOptionScreenFilter, formatOptionScreenFilter,
  optionScreenGroups, parseOptionCode, parseOptionScreenJson, smileSeries, strikeBars,
} from "../src/services/optionScreen.js";

// docs/TOOL-LIMITS.md「期权筛选（option_screen）的最小可用载荷（2026-09-17 真机验证）」
// 的 filter 原文（此处按 JSON 文本比对，键序也算契约：真机就是这么发的）。
const VERIFIED_FILTER_JSON = '{"strategy":{"market_category_list":[0],'
  + '"filter_group_list":[{"option_list":[{"indicator_type":1003,'
  + '"indicator_value":{"value_list":[1]}}]}]},"field_filter":{"option_type":1,'
  + '"volume":1,"implied_volatility":1},"limit":3}';

/** 真机示例对应的表单态：文档里的载荷是**最小可用面**（3 个字段），所以字段表显式钉住
 *  ——表单默认值在 WP26 多带了图表要用的 open_interest/strike_date/code（见下一条用例）。 */
function verifiedForm() {
  return {
    ...DEFAULT_OPTION_SCREEN_FORM, withIndicator: true, limit: 3,
    fieldFilter: ["option_type", "volume", "implied_volatility"],
  };
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

test("OPTION_FIELD_FILTER_FIELDS：预置字段是真机验证过的那几个（含图表要用的三个）", () => {
  assert.deepEqual(OPTION_FIELD_FILTER_FIELDS.map((row) => row.value),
                   ["option_type", "volume", "implied_volatility", "open_interest",
                    "strike_date", "code"]);
});

test("DEFAULT_OPTION_SCREEN_FORM：市场类别 [0]，默认不带指标条件与排序", () => {
  assert.deepEqual(DEFAULT_OPTION_SCREEN_FORM.marketCategories, [0]);
  assert.equal(DEFAULT_OPTION_SCREEN_FORM.withIndicator, false);
  assert.equal(DEFAULT_OPTION_SCREEN_FORM.indicatorType, 1003);
  assert.deepEqual(DEFAULT_OPTION_SCREEN_FORM.indicatorValues, [1]);
  // WP26：open_interest / strike_date 不请求就恒为 null（2026-09-18 真机实测），
  // 所以默认字段表必须带上它们与 code，否则图表区永远是空的
  assert.deepEqual(DEFAULT_OPTION_SCREEN_FORM.fieldFilter,
                   ["option_type", "volume", "implied_volatility", "open_interest",
                    "strike_date", "code"]);
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
                   { option_type: 1, volume: 1, implied_volatility: 1, open_interest: 1,
                     strike_date: 1, code: 1 });
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

// ---------------------------------------------------------------------------
// WP26（2026-09-18 第一批图表）：option_screen 结果行 → 期权页两张图的纯函数。
//
// 真机事实（2026-09-18 实测）：`value.option_list[]` 的字段是 code / underlying /
// strike_date（**实为到期日**，如 '20260918'）/ option_type（'CALL'/'PUT'）/
// implied_volatility（数字）/ volume / open_interest（**数字字符串**），未请求或上游无值
// 的字段是 null。**行权价不在字段里**，只编码在 code 尾部。
// 这四条样本是实机抓到的原文，逐字抄进用例。
// ---------------------------------------------------------------------------

/** 造一条真机形状的结果行（默认值就是实机样本的那一行）。 */
function screenRow(code, patch = {}) {
  return {
    code,
    underlying: (code.match(/^[A-Z.]+/) || ["US.SPY"])[0],   // US.SPY260918C… → "US.SPY"
    strike_date: "20260918",
    option_type: "CALL",
    implied_volatility: 13.111,
    volume: "350107",
    open_interest: "12345",
    ...patch,
  };
}

test("parseOptionCode：4 个真机样本解析出到期/看涨看跌/行权价", () => {
  const cases = [
    ["US.SPY260918C760000", "260918", "CALL", 760],
    ["US.SPY260918P759000", "260918", "PUT", 759],
    ["US.QQQ260918C718000", "260918", "CALL", 718],
    ["US.NVDA260918C220000", "260918", "CALL", 220],
  ];
  for (const [code, expiry, side, strike] of cases) {
    assert.deepEqual(parseOptionCode(code), { expiry, side, strike }, code);
  }
  // 行权价 = 尾部整数 ÷ 1000（760000 → 760.000 → 760），不是 ÷ 100
  assert.equal(parseOptionCode("US.SPY260918C760500").strike, 760.5);
  // 2026-09-18 真机实测：尾数是**变宽**的（strike×1000 不带前导零），固定 6 位会丢掉
  // 200 行里的 45 行。每条都用当时的现价交叉核对过（括号里是 rt_quote 实测价）。
  const variableWidth = [
    ["US.HYG270219C80000", "270219", "CALL", 80],      // HYG 现价 78.49
    ["US.EFA270319P95000", "270319", "PUT", 95],       // EFA 现价 104.73
    ["US.EFA270319P75000", "270319", "PUT", 75],
    ["US.MU260918C1000000", "260918", "CALL", 1000],   // MU 现价 990.46
  ];
  for (const [code, expiry, side, strike] of variableWidth) {
    assert.deepEqual(parseOptionCode(code), { expiry, side, strike }, code);
  }
  // 大小写与首尾空白归一后再解析（上游有时给大写，用户手抄可能带空格）
  assert.deepEqual(parseOptionCode("  us.spy260918c760000  "),
                   { expiry: "260918", side: "CALL", strike: 760 });
});

test("parseOptionCode：解析失败一律 null（不猜行权价、不猜方向）", () => {
  const bad = [
    "US.AAPL260116C00200000",     // 尾数带前导零（页面占位符里的那种写法）：实测真机码
                                  // 从不补零，这种写法判为畸形而不是「行权价 2000」
    "US.SPY26091C760000",         // 到期只有 5 位
    "US.SPY260918X760000",        // 第 7 位不是 C/P
    "US.SPY",                     // 没有编码段
    "US.SPY260918C",              // 只有方向没有行权价
    "",
    null,
    undefined,
    12345,
    {},
  ];
  for (const code of bad) {
    assert.equal(parseOptionCode(code), null, JSON.stringify(code));
  }
  // 5 位尾数**不是**畸形（真机里 HYG/EFA 就是 5 位，见上一条用例）：`US.SPY260918C76000`
  // 是 SPY 的 76 美元行权价，短尾数只是价格低，不是坏码。
  assert.deepEqual(parseOptionCode("US.SPY260918C76000"),
                   { expiry: "260918", side: "CALL", strike: 76 });
  // 有意**不**校验市场/标的前缀：前缀不属于 `…(\d{6})([CP])([1-9]\d*)$` 这段编码契约，
  // 自己加一条「必须像 US.XXX」的规则就是在契约外加戏（页面喂进来的都是上游 code）。
  assert.deepEqual(parseOptionCode("SPY260918C760000"),
                   { expiry: "260918", side: "CALL", strike: 760 });
});

test("optionScreenGroups：按「标的 + 到期日」分组，组按行数降序；解析失败的行单独计数", () => {
  const rows = [
    screenRow("US.SPY260918C760000"),
    screenRow("US.SPY260918P759000", { option_type: "PUT", implied_volatility: 12.5 }),
    screenRow("US.SPY260918C761000"),
    screenRow("US.QQQ260918C718000", { underlying: "US.QQQ" }),
    screenRow("US.SPY260925C760000", { strike_date: "20260925" }),
    screenRow("US.AAPL260116C00200000", { underlying: "US.AAPL" }),  // 解析失败
  ];
  const { groups, unparsed } = optionScreenGroups(rows);
  assert.equal(unparsed, 1, "解析不了 code 的行不进任何组，也不猜行权价");
  // 行数降序；同为 1 行时按 key 升序（顺序必须稳定，否则每次渲染缺省组会跳来跳去）
  assert.deepEqual(groups.map((group) => [group.underlying, group.expiry, group.count]),
                   [["US.SPY", "20260918", 3], ["US.QQQ", "20260918", 1], ["US.SPY", "20260925", 1]]);
  assert.deepEqual(groups.map((group) => group.key),
                   ["US.SPY|20260918", "US.QQQ|20260918", "US.SPY|20260925"]);
  // 组内点按行权价升序（页面按组取数时顺序稳定）
  assert.deepEqual(groups[0].points.map((point) => point.strike), [759, 760, 761]);
  assert.equal(groups[0].points[0].side, "PUT");
});

test("optionScreenGroups：数字字符串转数值、null 保留为 null（缺失不是 0）", () => {
  const rows = [
    screenRow("US.SPY260918C760000", { volume: "350107", open_interest: null,
                                       implied_volatility: 13.111 }),
    screenRow("US.SPY260918C761000", { volume: null, open_interest: "0",
                                       implied_volatility: null }),
  ];
  const { groups } = optionScreenGroups(rows);
  const [first, second] = groups[0].points;
  assert.equal(first.volume, 350107);
  assert.equal(first.openInterest, null);
  assert.equal(first.iv, 13.111);
  assert.equal(second.volume, null);
  assert.equal(second.openInterest, 0, "持仓量为 0 是有效数值，不是缺失");
  assert.equal(second.iv, null);
});

test("optionScreenGroups：标的从 code 前缀推（真机 underlying 是嵌套对象，字段拿不到）", () => {
  // 2026-09-18 本机真机形状：`underlying` 恒为嵌套对象 `{stock_id: null, ...}`（请求
  // underlying/stock_id/underlying_code 都不变）——标的一律从 code 前缀推，不猜、不丢组。
  const nested = optionScreenGroups([
    screenRow("US.SPY260918C760000", { underlying: { stock_id: null }, strike_date: null }),
    screenRow("US.QQQ260918C760000", { underlying: { stock_id: null }, strike_date: null }),
  ]).groups;
  // 两组各 1 行 → 按 key 升序（US.QQQ… < US.SPY…）；顺序必须稳定，否则缺省组会跳
  assert.deepEqual(nested.map((group) => group.underlying), ["US.QQQ", "US.SPY"]);
  assert.equal(nested.find((group) => group.underlying === "US.SPY").expiry, "代码内 260918",
               "到期日缺失时用代码里的 6 位并标明来源");
  // 上游哪天给出字符串形式的 underlying 就优先用它（不跟代码前缀较劲）
  const asString = optionScreenGroups([screenRow("US.SPY260918C760000", { underlying: "US.SPY.US" })])
    .groups[0];
  assert.equal(asString.underlying, "US.SPY.US");
  // 数字代码（港股）也要成立：剥掉尾部编码后是 HK.00700
  const hk = optionScreenGroups([screenRow("HK.00700260918C500000", { underlying: null })]).groups[0];
  assert.equal(hk.underlying, "HK.00700");
  assert.equal(hk.points[0].strike, 500);  assert.equal(optionScreenGroups(null).groups.length, 0);
  assert.equal(optionScreenGroups(null).unparsed, 0);
});

test("smileSeries：CALL/PUT 两条系列，点按行权价升序，meta 是排版好的成交量/持仓量", () => {
  const group = optionScreenGroups([
    screenRow("US.SPY260918C761000", { implied_volatility: 14.2, volume: "2000", open_interest: "300" }),
    screenRow("US.SPY260918C760000", { implied_volatility: 13.1, volume: "350107", open_interest: null }),
    screenRow("US.SPY260918P759000", { option_type: "PUT", implied_volatility: 12.5 }),
  ]).groups[0];
  const series = smileSeries(group);
  assert.deepEqual(series.map((one) => one.key), ["CALL", "PUT"]);
  assert.deepEqual(series[0].points.map((point) => [point.x, point.y]), [[760, 13.1], [761, 14.2]]);
  assert.deepEqual(series[1].points.map((point) => [point.x, point.y]), [[759, 12.5]]);
  assert.deepEqual(series[0].points[0].meta,
                   { 成交量: "35.01万", 持仓量: "—" },
                   "缺失的持仓量显示 —，不能显示 0");
  assert.equal(series[0].label, "CALL");
});

test("smileSeries：只有一侧有行时不产出空系列；IV 缺失的点仍然给出（由组件跳过并计数）", () => {
  const callsOnly = optionScreenGroups([screenRow("US.SPY260918C760000")]).groups[0];
  assert.deepEqual(smileSeries(callsOnly).map((one) => one.key), ["CALL"]);
  const noIv = optionScreenGroups([screenRow("US.SPY260918C760000", { implied_volatility: null })])
    .groups[0];
  const series = smileSeries(noIv);
  assert.equal(series[0].points.length, 1, "IV 为 null 也要给出去——由 ScatterChart 跳过并如实计数");
  assert.equal(series[0].points[0].y, null);
});

test("strikeBars：按行权价聚合成交量/持仓量，升序，缺失值不参与求和", () => {
  const group = optionScreenGroups([
    screenRow("US.SPY260918C760000", { volume: "100", open_interest: "10" }),
    screenRow("US.SPY260918P760000", { volume: "50", open_interest: null }),
    screenRow("US.SPY260918C759000", { volume: null, open_interest: "5" }),
    screenRow("US.SPY260918C761000", { volume: "0", open_interest: "0" }),
  ]).groups[0];
  const bars = strikeBars(group);
  assert.deepEqual(bars.volume, [
    { label: "759", value: null },      // 两个来源都是 null → 该行权价没有成交量事实
    { label: "760", value: 150 },       // CALL 100 + PUT 50（同价位合并）
    { label: "761", value: 0 },         // 0 是有效数值
  ]);
  assert.deepEqual(bars.openInterest, [
    { label: "759", value: 5 },
    { label: "760", value: 10 },
    { label: "761", value: 0 },
  ]);
  assert.equal(MIN_SMILE_ROWS, 4, "页面用它决定『画不出微笑』的阈值（limit 默认 20 时应提示调大）");
});
