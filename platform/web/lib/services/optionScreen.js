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
//
// WP26（2026-09-18）追加一段：`option_screen` 结果行 → 期权页两张图的数据（合约代码解析、
// 按标的+到期日分组、IV 微笑系列、行权价分布）。数值归一与文案格式化直接复用 `charts/` 下的
// **纯模块**（scale.js 的 finiteNumber、geometry.js 的 compactNumber/axisNumberText）——
// 它们不碰 DOM、node 里可直测；再抄一份「缺失值不是 0」的规则必然漂移。
import { axisNumberText, compactNumber } from "../charts/geometry.js";
import { finiteNumber } from "../charts/scale.js";

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
 * **仅这几个字段名经真机验证**（docs/TOOL-LIMITS.md 的最小可用载荷 + WP26 实测）；
 * 其它字段名可以自由输入，但未验证——所以控件是「预置 + 允许自由输入」，不是封闭枚举。
 * 占位值一律 `1`（proto 占位规则：int 字段用 1），由 buildOptionScreenFilter 生成。
 *
 * WP26 实测补充（2026-09-18，本机真机）：`open_interest` / `strike_date` **必须显式请求
 * 才会返回值**（不请求时字段存在但恒为 `null`）；`code` 不请求也会返回，但图表依赖它，
 * 显式列出以免上游改口径时静默断掉。
 */
export const OPTION_FIELD_FILTER_FIELDS = [
  { value: "option_type", label: "期权类型 option_type（已验证）" },
  { value: "volume", label: "成交量 volume（已验证）" },
  { value: "implied_volatility", label: "隐含波动率 implied_volatility（已验证）" },
  { value: "open_interest", label: "持仓量 open_interest（已验证，图表用）" },
  { value: "strike_date", label: "到期日 strike_date（已验证，图表分组用）" },
  { value: "code", label: "合约代码 code（图表解析行权价用）" },
];

/** 本仓库唯一验证过的指标类型（`indicator_type`）；其余取值未验证，界面据此标注。 */
export const OPTION_INDICATOR_TYPE_VERIFIED = 1003;

/**
 * 表单默认态（对应真机示例的最小可用载荷，limit 从 3 放宽到 20；WP26 加了图表要用的
 * `open_interest` / `strike_date` / `code`——**不请求就是 null**，加了图却没数据等于白加）。
 * strategy.market_category_list=[0]，不带指标条件（filter_group_list=[]，
 * 该形状在 tests/test_wp8_market.py 里是被上游接受的）。
 */
export const DEFAULT_OPTION_SCREEN_FORM = {
  marketCategories: [0],
  withIndicator: false,
  indicatorType: OPTION_INDICATOR_TYPE_VERIFIED,
  indicatorValues: [1],
  fieldFilter: ["option_type", "volume", "implied_volatility", "open_interest", "strike_date",
                "code"],
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

// ---------------------------------------------------------------------------
// WP26（2026-09-18 第一批图表）：option_screen 结果行 → 期权页两张图的数据。
//
// 真机事实（2026-09-18 实测，逐字来自实际返回）：
//   value.option_list[] = [{code: 'US.SPY260918C760000', underlying: 'US.SPY',
//     strike_date: '20260918'（**实为到期日**）, option_type: 'CALL'/'PUT',
//     implied_volatility: 13.111（数字）, volume: '350107'（**数字字符串**）,
//     open_interest: '12345'（**数字字符串**）}]；未请求或上游无值的字段是 null。
//   **行权价不在任何字段里**，只编码在 `code` 尾部（`…260918C760000` → 760.000）。
//
// 本段全是纯函数（node --test 直测，见 tests/optionScreen.test.mjs），页面只做接线：
//   `optionScreenGroups(rows)` → 按「标的 + 到期日」分组（真机结果里混着多个标的与多个
//   到期日，不分组的「微笑」是把 SPY 与 QQQ 连成一条线，纯属编数据）；
//   `smileSeries(group)` → IV 微笑的两条系列（x = 行权价，y = 隐含波动率）；
//   `strikeBars(group)` → 按行权价聚合的成交量/持仓量（竖向柱状）。
// ---------------------------------------------------------------------------

/**
 * 期权合约代码尾部的编码段：`YYMMDD + C/P + strike×1000 的十进制整数`（`parseOptionCode` 与
 * `underlyingOf` 共用同一份正则——两处各写一份就会漂移）。
 *
 * **尾数不带前导零**（2026-09-18 真机实测）：`[1-9]\d*` 而不是 `\d{6}`。原口径 `\d{6}`
 * 在实测 200 行里**丢掉 45 行**（22.5%），因为行权价 ×1000 的位数随价格变：
 *   `US.SPY260918C760000`  6 位 → 760.000（SPY 现价 759.06）
 *   `US.HYG270219C80000`   5 位 →  80.000（HYG 现价 78.49）
 *   `US.EFA270319P95000`   5 位 →  95.000（EFA 现价 104.73）
 *   `US.MU260918C1000000`  7 位 → 1000.000（MU  现价 990.46）
 * 四个都能被现价解释成「平值附近的真实行权价」，所以这是**解码**而不是猜；`[1-9]\d*` 同时
 * 把带前导零的畸形写法（如页面占位符 `US.AAPL260116C00200000`）挡在门外。
 */
const OPTION_CODE_TAIL = /(\d{6})([CP])([1-9]\d*)$/;

/**
 * 期权合约代码 → `{expiry, side, strike}`，**解析失败返回 null（不猜）**。
 *
 * 口径（正则 `/(\d{6})([CP])([1-9]\d*)$/`，与实机样本逐字对齐）：
 *   `US.SPY260918C760000` → `{expiry:'260918', side:'CALL', strike:760}`
 *   `US.SPY260918P759000` → `{expiry:'260918', side:'PUT',  strike:759}`
 *   `US.QQQ260918C718000` → `{expiry:'260918', side:'CALL', strike:718}`
 *   `US.NVDA260918C220000`→ `{expiry:'260918', side:'CALL', strike:220}`
 *   `US.HYG270219C80000`  → `{expiry:'270219', side:'CALL', strike:80}`（实测 5 位尾数）
 *   `US.MU260918C1000000` → `{expiry:'260918', side:'CALL', strike:1000}`（实测 7 位尾数）
 * `strike` = 尾部整数 ÷ 1000（`760000` → 760.000），**不是** ÷100；`expiry` 保持代码里的
 * 6 位 `YYMMDD` 原文（不补世纪：26 是 2026 还是 1926 无法从代码判定，猜了就是编数据）。
 * 到期日取的是 **C/P 之前紧邻的 6 位**——数字型标的（`HK.00700…`）与到期日连在一起也分得开。
 *
 * 失败口径：解析失败返回 `null`，页面**跳过该行并在图下如实标注条数**——
 * `US.AAPL260116C00200000`（尾数带前导零）、`US.SPY26091C760000`（到期 5 位）、
 * `US.SPY260918X760000`（第 7 位不是 C/P）、`US.SPY260918C`（只有方向没有行权价）
 * 都会返回 null。大小写与首尾空白先归一（上游有时给大写）。
 */
export function parseOptionCode(code) {
  if (typeof code !== "string") return null;
  const match = OPTION_CODE_TAIL.exec(code.trim().toUpperCase());
  if (!match) return null;
  return {
    expiry: match[1],
    side: match[2] === "C" ? "CALL" : "PUT",
    strike: Number(match[3]) / 1000,
  };
}

/** 组标签里「上游没给这个字段」的显式占位（不拿别的值冒充）。 */
export const OPTION_UNKNOWN = "（上游未给）";

/**
 * IV 微笑至少需要几个有效点才画：当前 `limit` 默认 20、按「标的+到期日」分组后往往只剩
 * 个位数行，少于 4 个点连「两端翘起」都看不出来。页面据此给空态文案
 * （「把『条数上限』加到 100 以上再筛选」）。
 */
export const MIN_SMILE_ROWS = 4;

/** 组内单行：`{index, code, strike, side, iv, volume, openInterest}`（数值均可为 null）。 */
function normalizeRow(row, index) {
  const parsed = parseOptionCode(row?.code);
  if (!parsed) return null;
  const strikeDate = typeof row?.strike_date === "string" && row.strike_date.trim() !== ""
    ? row.strike_date.trim() : "";
  return {
    index,
    code: row.code,
    strike: parsed.strike,
    side: parsed.side,
    // 到期日优先用行里的 strike_date（实为到期日）；缺失时用代码里的 6 位并**在标签里
    // 标明来源**（代码里只有 YYMMDD，补不出世纪，所以不冒充成 strike_date）
    expiry: strikeDate || `代码内 ${parsed.expiry}`,
    underlying: underlyingOf(row),
    iv: finiteNumber(row?.implied_volatility),
    volume: finiteNumber(row?.volume),
    openInterest: finiteNumber(row?.open_interest),
  };
}

/**
 * 标的：**真机口径是从 `code` 前缀推**，不是读 `underlying` 字段。
 *
 * 2026-09-18 本机实测（`option_screen`，market_category_list=[0]，limit 200）：`underlying`
 * 字段恒为**嵌套对象** `{earnings:{}, index_type:null, open_interest:null, stock_id:null,
 * volume:null}`——不管 `field_filter` 里给 `underlying: 1`、`{"stock_id": 1}` 还是
 * `underlying_code`/`stock_id`，`stock_id` 一律是 null（请求这些键也不报错，只是静默无效）。
 * 而 `code` 的前缀就是标的（`US.SPY260918C760000` → `US.SPY`，`HK.00700…` → `HK.00700`，
 * 对字母与数字代码都成立）。所以：**上游哪天给出字符串形式的 `underlying` 就优先用它**，
 * 否则从 code 前缀推；两者都没有才标「未给」。
 */
function underlyingOf(row) {
  if (typeof row?.underlying === "string" && row.underlying.trim() !== "") {
    return row.underlying.trim();
  }
  if (typeof row?.code !== "string") return OPTION_UNKNOWN;
  const prefix = row.code.trim().toUpperCase().replace(OPTION_CODE_TAIL, "");
  return prefix !== "" ? prefix : OPTION_UNKNOWN;
}

/**
 * 结果行 → 分组视图：`{groups, unparsed}`。
 *
 * `groups[]`：`{key, underlying, expiry, count, points[]}`，按**行数降序**（行数最多的那组
 * 就是页面缺省要展示的组），行数相同按 key 升序保证稳定；`points` 按行权价升序。
 * `unparsed`：`code` 解析不出行权价的行数——这些行**不进入任何组**（没有行权价就画不到
 * x 轴上），页面把条数如实显示出来。`rows` 非数组/为 null 时返回空结果（不抛）。
 */
export function optionScreenGroups(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const points = [];
  let unparsed = 0;
  list.forEach((row, index) => {
    const normalized = normalizeRow(row, index);
    if (!normalized) { unparsed += 1; return; }
    points.push(normalized);
  });
  const buckets = new Map();
  points.forEach((point) => {
    const key = `${point.underlying}|${point.expiry}`;
    if (!buckets.has(key)) {
      buckets.set(key, {
        key, underlying: point.underlying, expiry: point.expiry, count: 0, points: [],
      });
    }
    const bucket = buckets.get(key);
    bucket.points.push(point);
    bucket.count += 1;
  });
  const groups = [...buckets.values()];
  groups.forEach((group) => group.points.sort((left, right) => left.strike - right.strike));
  groups.sort((left, right) => (right.count - left.count) || left.key.localeCompare(right.key));
  return { groups, unparsed };
}

/**
 * 组 → IV 微笑系列：`[{key, label, points:[{x, y, meta}]}]`，x = 行权价、y = 隐含波动率。
 *
 * 只产出**该组确实有行**的方向（该组全是 CALL 就不给一条空的 PUT 系列，图例要能对上事实）；
 * 点的 `y` 可能是 `null`（IV 字段为 null）——**照给不误**，由 `ScatterChart` 跳过并如实
 * 计数（在上游数据层就地丢弃就没人知道丢了多少）。`meta` 是排好版的成交量/持仓量（缺失
 * 显示「—」，不显示 0）。颜色由页面按主题给（本模块不碰主题）。
 */
export function smileSeries(group) {
  const points = Array.isArray(group?.points) ? group.points : [];
  const series = [];
  for (const side of ["CALL", "PUT"]) {
    const sidePoints = points.filter((point) => point.side === side);
    if (sidePoints.length === 0) continue;
    series.push({
      key: side,
      label: side,
      points: [...sidePoints]
        .sort((left, right) => left.strike - right.strike)
        .map((point) => ({
          x: point.strike,
          y: point.iv,
          meta: {
            成交量: compactNumber(point.volume),
            持仓量: compactNumber(point.openInterest),
          },
        })),
    });
  }
  return series;
}

/**
 * 组 → 按行权价聚合的成交量/持仓量：`{volume, openInterest}`，每项 `{label, value}`。
 *
 * 口径：**同一行权价的多行相加**（CALL 与 PUT 在同行权价上合并），行权价升序；某个行权价
 * 下所有来源都是 `null` 时给 `value: null`（**不给 0**——「没有成交量事实」与「成交量是 0」
 * 是两回事），由 `VBarChart` 跳过并如实计数。`label` 用 `axisNumberText`（760 → "760"）。
 */
export function strikeBars(group) {
  const points = Array.isArray(group?.points) ? group.points : [];
  const strikes = [...new Set(points.map((point) => point.strike))].sort((left, right) => left - right);
  const sumAt = (field) => strikes.map((strike) => {
    const values = points
      .filter((point) => point.strike === strike)
      .map((point) => point[field])
      .filter((value) => value !== null);
    return {
      label: axisNumberText(strike),
      value: values.length ? values.reduce((sum, value) => sum + value, 0) : null,
    };
  });
  return { volume: sumAt("volume"), openInterest: sumAt("openInterest") };
}
