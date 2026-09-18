// 期权筛选项 → `option_screen` 载荷的纯函数（2026-09-18 用户需求：把「手写 JSON」的
// strategy / field_filter 两个输入框换成表单控件，同时保留一个 JSON 逃生口）。
//
// 本模块是**唯一**的载荷组装处（页面只接线与展示错误，不做形状判断）；
// node --test 直测，见 platform/web/tests/optionScreen.test.mjs。
//
// 事实来源：`docs/TOOL-LIMITS.md`「期权筛选（option_screen）的最小可用载荷
// （2026-09-17 真机验证）」与服务端 `platform/server/futu_data.py`
// （OPTION_SCREEN_EXAMPLE / _is_field_filter_placeholder / option_screen 的参数校验面）。
//
// 两条刻意的克制（都是「不编造」的落实）：
//   1. **市场类别只给 7 个、不给自由输入**：`market_category_list` 的非支持值会被上游
//      **静默忽略**（回空列表 + total=0）——放进自由输入框，用户看到的是一个「看起来
//      成功、其实什么都没筛」的假象，比报错更坏。真机验证过的就是
//      0..6 这七个（US_STOCK/US_INDEX/US_FUTURE/HK_STOCK/HK_INDEX/JP_STOCK/JP_INDEX）。
//   2. **指标类型与返回字段允许自由输入，默认值只用已验证值**：本仓库对
//      `indicator_type` 只验证过 `1003`，对 `field_filter` 字段名只验证过
//      option_type/volume/implied_volatility 三个——写成封闭枚举就是在编造；
//      这里预填已验证值 + 允许自由输入，并用文案如实标注「其余未验证」。
//   3. **不抄服务端的第二份校验**：`field_filter` 值形状（int 用 1、string 用非空串、
//      嵌套用非空对象）的唯一实现是服务端 `_is_field_filter_placeholder`；前端只做
//      「非空对象」这类会决定**是否值得发这次请求**的前置判断，其余一律透传，由服务端
//      拒绝并让页面原样展示原因（JSON 逃生口路径同理，见 parseOptionScreenJson）。
//
// 市场类别的 7 个值另有两处镜像，由 python 锁测试 `tests/test_wp25_option_form.py`
// 三向比对（本文件 ↔ futu_data.OPTION_MARKET_CATEGORIES ↔ docs/TOOL-LIMITS.md）。

/**
 * 期权筛选的市场类别（`strategy.market_category_list` 的取值面）。
 *
 * 来源：真机清单（docs/TOOL-LIMITS.md，2026-09-17）——本仓库**只**验证过这 7 个；
 * 非支持值被后端静默忽略（回空列表 + total=0），所以表单不提供自由输入。
 */
export const OPTION_MARKET_CATEGORIES = [
  { value: 0, label: "美股个股（US_STOCK）" },
  { value: 1, label: "美股指数（US_INDEX）" },
  { value: 2, label: "美股期货（US_FUTURE）" },
  { value: 3, label: "港股个股（HK_STOCK）" },
  { value: 4, label: "港股指数（HK_INDEX）" },
  { value: 5, label: "日股个股（JP_STOCK）" },
  { value: 6, label: "日股指数（JP_INDEX）" },
];

/**
 * 返回字段（`field_filter`）的预置项。
 *
 * **仅这三个字段名经真机验证**（docs/TOOL-LIMITS.md 的最小可用载荷）；其它字段名可以
 * 自由输入，但未验证——所以控件是「预置 + 允许自由输入」，不是封闭枚举。
 * 占位值一律 `1`（proto 占位规则：int 字段用 1），由 buildOptionScreenFilter 生成。
 */
export const OPTION_FIELD_FILTER_FIELDS = [
  { value: "option_type", label: "期权类型 option_type（已验证）" },
  { value: "volume", label: "成交量 volume（已验证）" },
  { value: "implied_volatility", label: "隐含波动率 implied_volatility（已验证）" },
];

/** 本仓库唯一验证过的指标类型（`indicator_type`）；其余取值未验证，界面据此标注。 */
export const OPTION_INDICATOR_TYPE_VERIFIED = 1003;

/**
 * 表单默认态（对应真机示例的最小可用载荷，仅 limit 从 3 放宽到 20）：
 * strategy.market_category_list=[0]，不带指标条件（filter_group_list=[]，
 * 该形状在 tests/test_wp8_market.py 里是被上游接受的），field_filter 为已验证的 3 个字段。
 */
export const DEFAULT_OPTION_SCREEN_FORM = {
  marketCategories: [0],
  withIndicator: false,
  indicatorType: OPTION_INDICATOR_TYPE_VERIFIED,
  indicatorValues: [1],
  fieldFilter: ["option_type", "volume", "implied_volatility"],
  limit: 20,
  sortEnabled: false,
  sortField: "volume",
  sortAsc: false,
};

/** 严格整数（数字字面量或纯数字字符串）；`1.5`/`true`/`"x"`/null 一律 null。 */
function toInt(value) {
  if (typeof value === "number") return Number.isInteger(value) ? value : null;
  if (typeof value === "string" && /^[+-]?\d+$/.test(value.trim())) return Number(value.trim());
  return null;
}

function toIntList(value) {
  return (Array.isArray(value) ? value : []).map(toInt).filter((item) => item !== null);
}

function toStringList(value) {
  return (Array.isArray(value) ? value : [])
    .map((item) => (typeof item === "string" ? item.trim() : ""))
    .filter((item) => item.length > 0);
}

function isNonEmptyObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length > 0;
}

/** 错误消息里的原值展示（undefined/null 也要可读）。 */
function describe(value) {
  if (typeof value === "string") return value === "" ? "（空）" : value;
  if (value === undefined) return "（未填）";
  return JSON.stringify(value);
}

/**
 * 表单态 → `{filter}` 或 `{error}`（**不抛**：页面要原样展示错误）。
 *
 * 生成规则（与 futu_data.option_screen 的校验面一一对应）：
 *   * `strategy.market_category_list` —— 至少一个类别码；
 *   * `strategy.filter_group_list` —— 加了指标条件时给
 *     `[{option_list: [{indicator_type, indicator_value: {value_list}}]}]`（真机形状），
 *     未加时给 `[]`（上游接受的空形状）；指标类型/值缺失或非数字 → 可读错误（宁可不发）；
 *   * `field_filter` —— 每个返回字段取值 `1`；为空 → 可读错误（上游会 -3，服务端也会拒绝）；
 *   * `limit` —— 为空不写该键；非 0..1000 整数 → 可读错误；
 *   * `sort_obj` —— 仅在「启用排序且字段非空」时写（形状见 tests/test_wp7_futu_data.py:180）。
 */
export function buildOptionScreenFilter(form = {}) {
  const markets = toIntList(form.marketCategories);
  if (markets.length === 0) {
    return { error: "市场类别至少选一个（strategy.market_category_list 不能为空）。"
      + "非支持值会被上游静默忽略（回空列表 + total=0），所以只提供真机验证过的 7 个类别。" };
  }
  const strategy = { market_category_list: markets };
  if (form.withIndicator === true) {
    const indicatorType = toInt(form.indicatorType);
    if (indicatorType === null) {
      return { error: `指标类型（indicator_type）必须是整数；本仓库仅验证过 `
        + `${OPTION_INDICATOR_TYPE_VERIFIED}，其余取值未验证。当前值：${describe(form.indicatorType)}` };
    }
    const values = toIntList(form.indicatorValues);
    if (values.length === 0) {
      return { error: "指标值（indicator_value.value_list）至少填一个整数，如 1。"
        + "留空/非数字不会生成载荷——空的指标值属于上游会拒绝的形状。" };
    }
    strategy.filter_group_list = [{
      option_list: [{ indicator_type: indicatorType, indicator_value: { value_list: values } }],
    }];
  } else {
    // 官方口径：每组内 underlying/option/chain/combo 只能一个非空；不加条件时给空数组
    strategy.filter_group_list = [];
  }
  const fields = toStringList(form.fieldFilter);
  if (fields.length === 0) {
    return { error: "返回字段（field_filter）不能为空：上游会以 -3 invalid parameter 拒绝，"
      + "服务端也会拒绝空 field_filter（省略时上游只返回 4 个默认字段、其余全 null）。"
      + "见 docs/TOOL-LIMITS.md。" };
  }
  const fieldFilter = {};
  for (const field of fields) fieldFilter[field] = 1;
  const filter = { strategy, field_filter: fieldFilter };
  const rawLimit = form.limit;
  const blankLimit = rawLimit === null || rawLimit === undefined
    || (typeof rawLimit === "string" && rawLimit.trim() === "");
  if (!blankLimit) {
    const limit = toInt(rawLimit);
    if (limit === null || limit < 0 || limit > 1000) {
      return { error: `条数上限（limit）必须是 0..1000 的整数，留空则不带该键。`
        + `当前值：${describe(rawLimit)}` };
    }
    filter.limit = limit;
  }
  if (form.sortEnabled === true) {
    const sortField = String(form.sortField ?? "").trim();
    if (sortField) filter.sort_obj = { sort_field: sortField, is_asc: form.sortAsc === true };
  }
  return { filter };
}

/**
 * 高级模式：手写整份 `filter` JSON → `{filter}` 或 `{error}`。
 *
 * 只做**决定是否值得发请求**的前置校验（filter/strategy/field_filter 都是非空对象——
 * 这三种错服务端拒绝得很含糊，先说是非）；`field_filter` 的值形状**有意不在这里重复
 * 实现**：服务端 `_is_field_filter_placeholder` 是唯一事实源，页面把它的拒绝原因原样
 * 展示给用户（抄第二份实现只会漂移）。
 */
export function parseOptionScreenJson(text) {
  let parsed;
  try {
    parsed = JSON.parse(typeof text === "string" ? text : String(text ?? ""));
  } catch (error) {
    return { error: `filter JSON 解析失败：${error.message}。`
      + "可先把「改为手动编辑 JSON」关掉，用表单生成一份再对照。" };
  }
  if (!isNonEmptyObject(parsed)) {
    return { error: "filter 必须是非空对象，形如 "
      + '{"strategy": {...}, "field_filter": {...}, "limit": 3}。' };
  }
  if (!isNonEmptyObject(parsed.strategy)) {
    return { error: "filter.strategy 必须是非空对象（上游必填，如 "
      + '{"market_category_list": [0]}；缺了会被上游拒绝）。' };
  }
  if (!isNonEmptyObject(parsed.field_filter)) {
    return { error: "filter.field_filter 必须是非空对象（省略/{} 时上游只返回 4 个默认字段、"
      + "其余全 null；服务端也会直接拒绝）。值形状（int 用 1、string 用非空串、嵌套用非空"
      + "对象）由服务端校验并原样返回原因。" };
  }
  return { filter: parsed };
}

/** 稳定的美化 JSON（高级面板展示「表单生成的载荷」，同一 filter 永远同一文本）。 */
export function formatOptionScreenFilter(filter) {
  return JSON.stringify(filter ?? {}, null, 2);
}
