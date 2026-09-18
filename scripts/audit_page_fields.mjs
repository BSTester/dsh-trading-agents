#!/usr/bin/env node
/**
 * 页面字段级审计（后端事实 × 浏览器渲染，零新依赖）
 *
 * 与 `scripts/e2e_web.mjs` 的分工：那个脚本问「页面有没有渲染出来」（锚点/白屏/console 错误），
 * 本脚本问「**每个字段显示得对不对**」——同一个端点值在页面上的呈现是否与格式契约一致。
 *
 * 三段事实，缺一不可：
 *   ① 后端事实：直接 `POST /api/wb/<endpoint>`（与浏览器同一入口），拿到真实载荷；
 *   ② 页面事实：真 Chromium（CDP）加载 `http://127.0.0.1:8397/#/<route>`，抓取 antd
 *      Statistic / Descriptions / Table 的**标签→渲染文本**（含 `—` 的单元格），
 *      以及内容区全文（排除「原始返回」折叠块，那是 JSON 逃生口，不该按展示文本判）；
 *   ③ 判定：按 `EXPECT` 里的**格式契约**（与 `platform/web/src/services/formatCore.js`
 *      同一口径）算出「后端值应显示成什么」，与渲染文本比对，给出判定码。
 *
 * 判定码（每条都在报告里带后端值/渲染值/说明，不做无证据的结论）：
 *   ok                  期望文本在渲染里出现（分页/筛选造成的不完全覆盖记 note）
 *   MISSING_WHEN_DATA   后端有非空值，渲染全是 —/空            ⇒ 缺陷
 *   FABRICATED          后端缺失/为空，渲染却是 0/0.00/NaN 等  ⇒ 缺陷
 *   FORMAT              两端都有值但文本对不上（千分位/×100/时间戳带 T…）⇒ 缺陷
 *   NOT_RENDERED        该标签在当前页面 DOM 里根本没出现           ⇒ 观察（可能是条件渲染）
 *   EMPTY               两端都空 → 数据确实没有，显示 — 是**正确**行为 ⇒ ok
 *   UNJUDGED            端点数取失败，无后端事实可比 ⇒ 观察（不判缺陷，如实列出）
 *
 * 用法：
 *   node scripts/audit_page_fields.mjs [--pages a,b] [--json] [--symbol SH.600000] [--help]
 *     --pages a,b   只审计指定路由（单页复验；未知键报错并列出可用键）
 *     --json        额外把报告路径打到 stdout（报告始终落盘）
 *     --symbol      标的类页面用的标的（默认取关注池第一只，缺省 SH.600000）
 *     --verbose     打印每条字段明细（默认只打印非 ok 条目）
 *   环境：AUDIT_BASE / AUDIT_CHROME / AUDIT_MAX_WAIT_MS / AUDIT_ROUTE_BUDGET_MS / AUDIT_KEEP
 * 退出码：0 = 未发现真实缺陷；1 = 发现真实缺陷；2 = 服务未就绪或运行中断。
 * 产物：`~/.dsh/logs/page-fields-<ts>/report.json`。
 * 只读：不点击任何提交类控件、不改服务状态、不安装依赖、不写服务配置。
 */
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

// 只导入做**对照**用：CONTRACTS 是本工具独立实现的契约，`tests/audit-page-fields.test.mjs`
// 负责把它与这份真实实现锁在一起（工具不能靠引用被测实现来自证正确）。
import {
  clockText, dayText, minuteText, numText, pctOfText, stampText,
} from "../platform/web/src/services/formatCore.js";

const BASE = process.env.AUDIT_BASE ?? "http://127.0.0.1:8397";
const CHROME = process.env.AUDIT_CHROME
  ?? path.join(homedir(), ".cache/ms-playwright/chromium-1234/chrome-linux/chrome");
const IDLE_MS = Number(process.env.AUDIT_IDLE_MS ?? 400);
const MAX_WAIT_MS = Number(process.env.AUDIT_MAX_WAIT_MS ?? 25000);
const ROUTE_BUDGET_MS = Number(process.env.AUDIT_ROUTE_BUDGET_MS ?? 90000);
const HEALTH_TIMEOUT_MS = Number(process.env.AUDIT_HEALTH_TIMEOUT_MS ?? 60000);
const TS = new Date().toISOString().replace(/[:.]/g, "-");
const OUT_DIR = path.join(homedir(), ".dsh", "logs", `page-fields-${TS}`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (m) => process.stdout.write(`${m}\n`);

// ---------------------------------------------------------------------------
// 格式契约：后端值 → 「页面上应该长这样」。
// 与 services/formatCore.js 的 numText/pctOfText/stampText 同口径，但**独立实现**——
// 工具不能靠「引用被测实现」来自证正确，两者对不上时按契约判缺陷并在 note 里写明。
// ---------------------------------------------------------------------------
const DASH = "—";

/** 千分位 + 最多 digits 位小数（与 format.jsx num 同口径）。 */
function expectNum(value, digits = 2) {
  if (value === null || value === undefined || value === "") return DASH;
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits })
    : String(value);
}

/** 比例 ×100（0.0123 → 1.23%）。 */
function expectPctRatio(value, digits = 2) {
  const parsed = Number(value);
  if (value === null || value === undefined || !Number.isFinite(parsed)) return DASH;
  return `${(parsed * 100).toFixed(digits)}%`;
}

/** 已是百分数（1.23 → 1.23%）。 */
function expectPctValue(value, digits = 2) {
  const parsed = Number(value);
  if (value === null || value === undefined || !Number.isFinite(parsed)) return DASH;
  return `${parsed.toFixed(digits)}%`;
}

/** 带正号的百分数（行情页涨跌幅：+1.23% / -1.23%）。 */
function expectSignedPct(value, digits = 2) {
  const parsed = Number(value);
  if (value === null || value === undefined || !Number.isFinite(parsed)) return DASH;
  return `${parsed > 0 ? "+" : ""}${parsed.toFixed(digits)}%`;
}

/** 时间戳：ISO 的 T → 空格、截 19 位。 */
function expectStamp(value) {
  return value ? String(value).replace("T", " ").slice(0, 19) : DASH;
}

/** 原样文本；空 → —。 */
function expectText(value) {
  if (value === null || value === undefined || value === "") return DASH;
  return String(value);
}

/** 整数计数。 */
function expectCount(value) {
  return expectNum(value, 0);
}

/** 毫秒/微秒整数 → 分钟精度时刻（独立实现，与 formatCore.minuteText 由单测锁一致）。 */
function microToMinute(value) {
  if (value === null || value === undefined || value === "") return DASH;
  const number = Number(value);
  // 不是数字、或不是时间戳量级（ISO 串等）→ 与 formatCore.minuteText 同口径：T 换空格、截 16 位
  if (!Number.isFinite(number)) return String(value).replace("T", " ").slice(0, 16);
  const ms = Math.abs(number) >= 1e14 ? number / 1000 : number;
  if (Math.abs(ms) < 1e11) return String(value).replace("T", " ").slice(0, 16);
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return DASH;
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 毫秒时间戳 → 可接受的展示形态（分时列 HH:mm、带日期列 YYYY-MM-DD HH:mm）。 */
function millisToText(value, withDate) {
  const ms = Number(value);
  if (!Number.isFinite(ms) || value === null || value === undefined || value === "") return [DASH];
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return [DASH];
  const pad = (n) => String(n).padStart(2, "0");
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return withDate ? [`${day} ${clock}`, clock, day] : [clock, `${day} ${clock}`];
}

/**
 * 展示契约：kind → { accept: 可接受的渲染文本, reject: 明确的错误形态, why }。
 * `accept` 可以有多项（同一后端值允许两种合理写法，如 12:30 与 2026-09-19 12:30）；
 * `reject` 用来把「一看就不对」的形态单独判出来（时间列里的 13 位毫秒整数）。
 */
const CONTRACTS = {
  num: (value) => ({ accept: [expectNum(value)], reject: null }),
  num0: (value) => ({ accept: [expectNum(value, 0)], reject: null }),
  num3: (value) => ({ accept: [expectNum(value, 3)], reject: null }),
  pctRatio: (value) => ({ accept: [expectPctRatio(value)], reject: null }),
  pctValue: (value) => ({ accept: [expectPctValue(value)], reject: null }),
  signedPct: (value) => ({ accept: [expectSignedPct(value)], reject: null }),
  stamp: (value) => ({ accept: [expectStamp(value)], reject: null }),
  /** 分钟精度的时间戳（执行/审计页 timeOf：T→空格、截 16 位）。 */
  stampMinute: (value) => ({ accept: [expectStamp(value).slice(0, 16)], reject: null }),
  /** 可能给毫秒/微秒整数的时刻列：不得原样显示 11 位以上整数。 */
  microStamp: (value) => ({ accept: [microToMinute(value)], reject: /^\d{11,}$/,
    why: "时间列显示原始微秒/毫秒时间戳" }),
  /** 上游原文原样展示的数值列（页面 rawCell：不重算、不加千分位）。 */
  rawString: (value) => ({ accept: [expectText(value)], reject: null }),
  text: (value) => ({ accept: [expectText(value)], reject: null }),
  count: (value) => ({ accept: [expectCount(value)], reject: null }),
  /** 原样字符串（antd Statistic 对数字走 String(value)，不做任何位数处理）。 */
  raw: (value) => ({ accept: [expectText(value)], reject: null }),
  /** 正数带 + 号的原样值（factors 综合分：+0.2517 / -0.1）。 */
  signedRaw: (value) => ({ accept: [`${Number(value) > 0 ? "+" : ""}${value}`], reject: null }),
  /** 倍数标注（风控「止损距离」：2 × ATR → 2.0 × ATR）。 */
  mult: (value) => ({ accept: [`${Number(value).toFixed(1)} × ATR`], reject: null }),
  /** 毫秒时间戳的时间列：不得原样显示 13 位整数。 */
  timeMs: (value) => ({ accept: millisToText(value, false), reject: /^\d{10,}$/,
    why: "时间列显示原始毫秒时间戳" }),
  /** 毫秒时间戳的日期列：不得原样显示 10 位整数（页面 slice(0,10) 的后果）。 */
  dateMs: (value) => ({ accept: millisToText(value, true), reject: /^\d{8,}$/,
    why: "日期列显示原始毫秒时间戳" }),
};

function contractOf(value, kind) {
  const fn = CONTRACTS[kind];
  if (!fn) throw new Error(`未知 kind：${kind}`);
  return fn(value);
}

/** 该值是否算「后端确实有」（决定 EMPTY 与 MISSING_WHEN_DATA 的分界）。 */
function hasData(value) {
  return value !== null && value !== undefined && value !== "";
}

/** 渲染文本里不可能出现的东西（出现即「拿未知当已知」）。 */
const POISON = ["NaN", "undefined", "[object Object]", "Invalid Date", "Infinity"];

// ---------------------------------------------------------------------------
// 页面规格：每页「调哪个端点 + 页面上哪个标签对应端点里的哪个字段」
//   path      字符串=对象键；数字=数组下标；"[]"=数组摊平；数组=候选键（取第一个有值的）
//   values    后端事实无法用路径表达时的取值函数（计算列/派生计数）
//   where     statistic | description | table_column | step | text（text 需 pattern）
//   table     仅 table_column：所在卡片标题的子串（同页同名表头靠它区分）
//   contains  渲染值含期望文本即算命中（复合单元格：「40.85%（00981）」）
// 只收录**页面上真实渲染的数据字段**；错误文案/空态文案不属于数据字段，不在此表。
// ---------------------------------------------------------------------------
const S = "__symbol__";      // 标的占位
const T3 = "__tickers3__";   // 关注池前 3 只（factors/ic 需要多标的）

/** 概览页「因子快照」：实测 payload = {date, tickers:{标的:{因子:值}}, computed_at, note}。 */
function factorSnapshot(eps) {
  const payload = eps.factorsHistory.value?.snapshots?.[0]?.payload ?? {};
  const tickers = payload.tickers && typeof payload.tickers === "object" ? payload.tickers : {};
  const keys = new Set();
  for (const row of Object.values(tickers)) {
    if (row && typeof row === "object") for (const key of Object.keys(row)) keys.add(key);
  }
  return { covered: Object.keys(tickers).length, factors: keys.size };
}

const PAGE_SPEC = [
  {
    key: "overview", name: "概览", needsSymbol: false,
    endpoints: {
      snapshot: ["snapshot", {}],
      positions: ["positions", { mode: "$mode" }],
      deals: ["deals_today", { mode: "$mode" }],
      reconcile: ["reconcile", {}],
      equity: ["equity", { mode: "$mode", window: 60 }],
      factorsHistory: ["factors-history", { limit: 30 }],
    },
    fields: [
      { label: "持仓数", where: "statistic", endpoint: "positions", path: ["counts", "positions"], kind: "count" },
      { label: "活跃告警", where: "statistic", endpoint: "reconcile",
        values: (eps) => [(eps.reconcile.value?.alerts ?? []).length], kind: "count" },
      { label: "最新快照日期", where: "statistic", endpoint: "factorsHistory",
        path: ["snapshots", 0, "date"], kind: "text" },
      { label: "因子数", where: "statistic", endpoint: "factorsHistory",
        values: (eps) => [factorSnapshot(eps).factors], kind: "count",
        kindNote: "因子集合 = payload.tickers 各标的值字典的键并集" },
      { label: "覆盖标的", where: "statistic", endpoint: "factorsHistory",
        values: (eps) => [factorSnapshot(eps).covered], kind: "count",
        kindNote: "payload.tickers 是对象（非数组），覆盖数 = 键的个数" },
      { label: "数量", where: "table_column", table: "持仓 Top5", endpoint: "positions",
        path: ["groups", "[]", "positions", "[]", "qty"], kind: "num0" },
      { label: "市值", where: "table_column", table: "持仓 Top5", endpoint: "positions",
        path: ["groups", "[]", "positions", "[]", "market_value"], kind: "num" },
      { label: "盈亏", where: "table_column", table: "持仓 Top5", endpoint: "positions",
        path: ["groups", "[]", "positions", "[]", "pl_val"], kind: "num" },
      { label: "级别", where: "table_column", table: "最新告警", endpoint: "reconcile",
        path: ["alerts", "[]", "level"], kind: "text" },
      { label: "时间", where: "table_column", table: "最新告警", endpoint: "reconcile",
        path: ["alerts", "[]", "created_at"], kind: "stamp" },
    ],
  },
  {
    key: "market", name: "行情", needsSymbol: true,
    endpoints: {
      instrument: ["instrument", { ticker: S }],
      rtQuote: ["rt_quote", { codes: [S] }],
      rtBook: ["rt_order_book", { code: S }],
      series: ["series", { ticker: S, period: "1d", limit: 250 }],
    },
    fields: [
      { label: "名称", where: "description", endpoint: "instrument", path: ["name"], kind: "text" },
      { label: "最新价", where: "description", endpoint: "instrument", path: ["price"], kind: "num" },
      { label: "涨跌幅", where: "description", endpoint: "instrument", path: ["change_pct"], kind: "signedPct" },
      { label: "今开 / 最高 / 最低", where: "description", endpoint: "instrument", contains: true,
        paths: [["open"], ["high"], ["low"]], kind: "num" },
      { label: "成交量", where: "description", endpoint: "instrument", path: ["volume"], kind: "num0" },
      { label: "成交额", where: "description", endpoint: "instrument", path: ["turnover"], kind: "num0" },
      { label: "每手股数", where: "description", endpoint: "instrument", path: ["lot_size"], kind: "num0" },
      { label: "数据时间", where: "description", endpoint: "instrument", path: ["as_of"], kind: "stamp" },
      { label: "最新价", where: "description", endpoint: "rtQuote",
        path: [["code_list", "quote_list"], 0, "last_price"], kind: "num" },
      { label: "涨跌幅", where: "description", endpoint: "rtQuote",
        path: [["code_list", "quote_list"], 0, ["change_rate", "change_pct"]], kind: "signedPct" },
      { label: "昨收", where: "description", endpoint: "rtQuote",
        path: [["code_list", "quote_list"], 0, ["prev_close_price", "prev_close"]], kind: "num" },
      { label: "成交量", where: "description", endpoint: "rtQuote",
        path: [["code_list", "quote_list"], 0, ["volume", "turnover_vol"]], kind: "num0" },
      { label: "成交额", where: "description", endpoint: "rtQuote",
        path: [["code_list", "quote_list"], 0, ["turnover", "amount"]], kind: "num0" },
    ],
  },
  {
    key: "capital", name: "资金", needsSymbol: true,
    endpoints: {
      flow: ["capital_flow", { code: S }],
      hist: ["capital_flow_history", { code: S, days: 30 }],
      dist: ["capital_distribution", { code: S }],
    },
    fields: [
      { label: "时间", where: "table_column", table: "分时资金流", endpoint: "flow",
        path: ["flow_list", "[]", "capital_flow_item_time"], kind: "timeMs" },
      { label: "净流入", where: "table_column", table: "分时资金流", endpoint: "flow",
        path: ["flow_list", "[]", ["in_flow", "main_inflow"]], kind: "num" },
      { label: "超大单", where: "table_column", table: "分时资金流", endpoint: "flow",
        path: ["flow_list", "[]", ["super_inflow", "super_in_flow", "super_net_inflow"]], kind: "num" },
      { label: "大单", where: "table_column", table: "分时资金流", endpoint: "flow",
        path: ["flow_list", "[]", ["big_inflow", "big_in_flow", "large_net_inflow"]], kind: "num" },
      { label: "中单", where: "table_column", table: "分时资金流", endpoint: "flow",
        path: ["flow_list", "[]", ["mid_inflow", "mid_in_flow", "medium_net_inflow"]], kind: "num" },
      { label: "小单", where: "table_column", table: "分时资金流", endpoint: "flow",
        path: ["flow_list", "[]", ["sml_inflow", "sml_in_flow", "small_net_inflow"]], kind: "num" },
      { label: "日期", where: "table_column", table: "历史资金流", endpoint: "hist",
        path: ["flow_list", "[]", "capital_flow_item_time"], kind: "dateMs" },
      { label: "净流入", where: "table_column", table: "历史资金流", endpoint: "hist",
        path: ["flow_list", "[]", ["in_flow", "main_inflow"]], kind: "num" },
      // 资金分布：上游给的是「流入/流出」两套键（capital_in_*/capital_out_*），
      // 「净流入」= 流入 − 流出（纯展示换算，与页面既有的占比换算同层）。
      { label: "净流入", where: "table_column", table: "资金分布", endpoint: "dist",
        values: (eps) => {
          const body = eps.dist.value ?? {};
          return ["super", "big", "mid", "small"].map((bucket) => {
            const inflow = body[`capital_in_${bucket}`];
            const outflow = body[`capital_out_${bucket}`];
            if (inflow === undefined && outflow === undefined) return undefined;
            return Number(inflow ?? 0) - Number(outflow ?? 0);
          });
        }, kind: "num" },
      { label: "占比", where: "table_column", table: "资金分布", endpoint: "dist",
        values: (eps) => {
          const body = eps.dist.value ?? {};
          const nets = ["super", "big", "mid", "small"].map((bucket) =>
            (Number(body[`capital_in_${bucket}`] ?? 0) - Number(body[`capital_out_${bucket}`] ?? 0)));
          const total = nets.reduce((sum, value) => sum + Math.abs(value), 0);
          return total > 0 ? nets.map((value) => (Math.abs(value) / total) * 100) : [];
        }, kind: "pctValue" },
    ],
  },
  {
    key: "options", name: "期权", needsSymbol: true, preferMarket: "HK",
    endpoints: {
      expirations: ["option_expiration", { code: S }],
      chain: ["option_chain", { code: S }],
    },
    fields: [
      { label: "代码", where: "table_column", table: "期权链", endpoint: "chain",
        path: ["option_chain", "[]", ["code", "option_code"]], kind: "text" },
      { label: "名称", where: "table_column", table: "期权链", endpoint: "chain",
        path: ["option_chain", "[]", ["name", "option_name"]], kind: "text" },
      { label: "到期日", where: "table_column", table: "期权链", endpoint: "chain",
        path: ["option_chain", "[]", ["expiration_date", "strike_time"]], kind: "text" },
      { label: "行权价", where: "table_column", table: "期权链", endpoint: "chain",
        path: ["option_chain", "[]", ["strike_price", "strike"]], kind: "num" },
      { label: "成交量", where: "table_column", table: "期权链", endpoint: "chain",
        path: ["option_chain", "[]", ["volume", "turnover_vol"]], kind: "num0" },
    ],
  },
  {
    key: "signal", name: "信号", needsSymbol: false,
    endpoints: { snapshot: ["snapshot", {}] },
    fields: [
      { label: "标的", where: "statistic", endpoint: "snapshot",
        values: (eps) => {
          const row = (eps.snapshot.value?.previews ?? [])
            .find((item) => item?.kind === "signal" && item?.value?.ticker !== undefined);
          return row ? [row.value.ticker] : [];
        }, kind: "text" },
      { label: "策略", where: "statistic", endpoint: "snapshot",
        values: (eps) => previewField(eps, "strategy"), kind: "text" },
      { label: "收盘价", where: "statistic", endpoint: "snapshot",
        values: (eps) => previewField(eps, "price"), kind: "num" },
      { label: "ATR(14)", where: "statistic", endpoint: "snapshot",
        values: (eps) => previewField(eps, "atr"), kind: "num" },
      { label: "时间", where: "table_column", table: "历史预览", endpoint: "snapshot",
        path: ["previews", "[]", "at"], kind: "stamp" },
      { label: "策略", where: "table_column", table: "历史预览", endpoint: "snapshot",
        path: ["previews", "[]", "value", "strategy"], kind: "text" },
    ],
  },
  {
    key: "portfolio", name: "组合", needsSymbol: false,
    endpoints: {
      positions: ["positions", { mode: "$mode" }],
      equity: ["equity", { mode: "$mode", window: 250 }],
    },
    fields: [
      { label: "最新权益", where: "statistic", endpoint: "equity", path: ["current"], kind: "num" },
      { label: "累计收益率", where: "statistic", endpoint: "equity", path: ["total_return"], kind: "pctRatio" },
      { label: "最大回撤", where: "statistic", endpoint: "equity", path: ["max_drawdown"], kind: "pctRatio" },
      { label: "台账成交笔数", where: "statistic", endpoint: "equity", path: ["trades"], kind: "count" },
      { label: "数量", where: "table_column", endpoint: "positions",
        path: ["groups", "[]", "positions", "[]", "qty"], kind: "num0" },
      { label: "市值", where: "table_column", endpoint: "positions",
        path: ["groups", "[]", "positions", "[]", "market_value"], kind: "num" },
      { label: "盈亏", where: "table_column", endpoint: "positions",
        path: ["groups", "[]", "positions", "[]", "pl_val"], kind: "num" },
    ],
  },
  {
    key: "risk", name: "风险", needsSymbol: false,
    endpoints: {
      risk: ["risk", {}],
      positions: ["positions", { mode: "sim" }],
    },
    fields: [
      { label: "单笔风险占权益比例", where: "description", endpoint: "risk",
        path: ["config", "risk_per_trade"], kind: "pctRatio" },
      { label: "止损距离", where: "description", endpoint: "risk",
        path: ["config", "stop_atr_mult"], kind: "mult" },
      { label: "最大同时持仓数", where: "description", endpoint: "risk",
        path: ["config", "max_positions"], kind: "count" },
      { label: "单日亏损熔断阈值", where: "description", endpoint: "risk",
        path: ["config", "daily_loss_limit_pct"], kind: "pctRatio" },
      { label: "单一标的最大仓位占权益比例", where: "description", endpoint: "risk",
        path: ["config", "max_position_pct"], kind: "pctRatio" },
      { label: "检查账户数", where: "description", endpoint: "positions",
        path: ["counts", "accounts_checked"], kind: "count" },
      { label: "有持仓账户数", where: "description", endpoint: "positions",
        path: ["counts", "accounts_with_positions"], kind: "count" },
      { label: "持仓笔数", where: "description", endpoint: "positions",
        path: ["counts", "positions"], kind: "count" },
      { label: "持仓数", where: "table_column", endpoint: "positions",
        path: ["groups", "[]", "risk", "positions"], kind: "count" },
      { label: "最大集中度", where: "table_column", endpoint: "positions", contains: true,
        path: ["groups", "[]", "risk", "max_share_of_positions"], kind: "pctValue" },
      { label: "盈利持仓数", where: "table_column", endpoint: "positions",
        path: ["groups", "[]", "risk", "winners", "count"], kind: "count" },
      { label: "持仓市值", where: "table_column", endpoint: "positions",
        path: ["groups", "[]", "risk", "top", "[]", "market_value"], kind: "num" },
      { label: "占本账户持仓市值", where: "table_column", endpoint: "positions",
        path: ["groups", "[]", "risk", "top", "[]", "share_of_positions"], kind: "pctValue" },
      { label: "占本账户总资产", where: "table_column", endpoint: "positions",
        path: ["groups", "[]", "risk", "top", "[]", "share_of_assets"], kind: "pctValue" },
    ],
  },
  {
    key: "factors", name: "因子", needsSymbol: false, needsTickers: true,
    endpoints: {
      factors: ["factors", { tickers: T3, window: 250 }],
      ic: ["ic", { tickers: T3, factor: "mom_20", forward: 5, window: 250 }],
      quality: ["quality", { ticker: S }],
      sentiment: ["sentiment-history", {}],
    },
    fields: [
      { label: "条数", where: "table_column", endpoint: "sentiment",
        values: (eps) => Object.values(eps.sentiment.value?.summary?.sources ?? {}), kind: "count" },
      { label: "均值 IC", where: "statistic", endpoint: "ic", path: ["mean_ic"], kind: "raw" },
      { label: "IC 标准差", where: "statistic", endpoint: "ic", path: ["ic_std"], kind: "raw" },
      { label: "ICIR", where: "statistic", endpoint: "ic", path: ["icir"], kind: "raw" },
      { label: "正 IC 占比", where: "statistic", endpoint: "ic", path: ["positive_ratio"], kind: "pctRatio" },
      { label: "样本期数", where: "statistic", endpoint: "ic", path: ["count"], kind: "count" },
      { label: "排名", where: "table_column", table: "因子打分", endpoint: "factors",
        path: ["rows", "[]", "rank"], kind: "count" },
      { label: "标的", where: "table_column", table: "因子打分", endpoint: "factors",
        path: ["rows", "[]", "ticker"], kind: "text" },
      { label: "综合分", where: "table_column", table: "因子打分", endpoint: "factors",
        path: ["rows", "[]", "score"], kind: "signedRaw" },
      { label: "动量20", where: "table_column", table: "因子打分", endpoint: "factors",
        path: ["rows", "[]", "factors", "mom_20"], kind: "num3" },
      { label: "PE(TTM)", where: "table_column", table: "因子打分", endpoint: "factors",
        path: ["rows", "[]", "factors", "pe_ttm"], kind: "num3" },
      { label: "close", where: "table_column", table: "因子打分", endpoint: "factors",
        path: ["rows", "[]", "factors", "close"], kind: "num3" },
      { label: "因子日", where: "table_column", table: "因子打分", endpoint: "factors",
        path: ["rows", "[]", "as_of"], kind: "text" },
      { label: "报告期", where: "description", endpoint: "quality", path: ["latest", "period_end"], kind: "text" },
      { label: "营业收入", where: "description", endpoint: "quality", path: ["latest", "revenue"], kind: "num" },
      { label: "毛利率", where: "description", endpoint: "quality", path: ["latest", "gross_margin"], kind: "pctValue" },
    ],
  },
  {
    key: "execution", name: "执行", needsSymbol: false,
    endpoints: {
      snapshot: ["snapshot", {}],
      trades: ["trades", { mode: "$mode" }],
      ordersOpen: ["orders_open", { mode: "$mode" }],
      ordersHistory: ["orders_history", { mode: "$mode", page_size: 50 }],
      dealsToday: ["deals_today", { mode: "$mode" }],
      dealsHistory: ["deals_history", { mode: "$mode", page_size: 50 }],
    },
    fields: [
      // 本地台账与 OpenAPI 四张表都用页面的 rawCell（原文原样，不重算/不加千分位）——
      // 这是文件内写明的展示纪律，故这里按「原文」判定，不按 num 判定。
      { label: "日期", where: "table_column", table: "本地台账", endpoint: "trades",
        path: ["trades", "[]", "date"], kind: "text" },
      { label: "标的", where: "table_column", table: "本地台账", endpoint: "trades",
        path: ["trades", "[]", "ticker"], kind: "text" },
      { label: "数量", where: "table_column", table: "本地台账", endpoint: "trades",
        path: ["trades", "[]", "shares"], kind: "rawString" },
      { label: "价格", where: "table_column", table: "本地台账", endpoint: "trades",
        path: ["trades", "[]", "price"], kind: "rawString" },
      { label: "费用", where: "table_column", table: "本地台账", endpoint: "trades",
        path: ["trades", "[]", "fee"], kind: "rawString" },
      { label: "收益", where: "table_column", table: "本地台账", endpoint: "trades",
        path: ["trades", "[]", "return"], kind: "rawString" },
      { label: "胜率（卖出计）", where: "statistic", endpoint: "trades", path: ["win_rate"], kind: "raw" },
      { label: "累计费用", where: "statistic", endpoint: "trades", path: ["total_fees"], kind: "num" },
      // OpenAPI 四张表同属一张卡片（标题「OpenAPI 订单与成交」）且列名重名，
      // 按 DOM 顺序定位：0=orders_open 1=orders_history 2=deals_today 3=deals_history
      { label: "标的", where: "table_column", table: "OpenAPI 订单与成交", tableOrder: 1,
        endpoint: "ordersHistory", path: ["groups", "[]", "rows", "[]", ["code", "symbol"]],
        kind: "rawString" },
      { label: "数量", where: "table_column", table: "OpenAPI 订单与成交", tableOrder: 1,
        endpoint: "ordersHistory", path: ["groups", "[]", "rows", "[]", "qty"], kind: "rawString" },
      { label: "委托价", where: "table_column", table: "OpenAPI 订单与成交", tableOrder: 1,
        endpoint: "ordersHistory", path: ["groups", "[]", "rows", "[]", ["price", "aux_price"]],
        kind: "rawString" },
      { label: "更新时间", where: "table_column", table: "OpenAPI 订单与成交", tableOrder: 1,
        endpoint: "ordersHistory",
        path: ["groups", "[]", "rows", "[]", ["update_time", "create_time"]], kind: "microStamp" },
      { label: "数量", where: "table_column", table: "OpenAPI 订单与成交", tableOrder: 1,
        endpoint: "dealsHistory", path: ["groups", "[]", "rows", "[]", ["qty", "filled_qty"]],
        kind: "rawString" },
      { label: "成交价", where: "table_column", table: "OpenAPI 订单与成交", tableOrder: 1,
        endpoint: "dealsHistory", path: ["groups", "[]", "rows", "[]", ["price", "avg_price"]],
        kind: "rawString" },
    ],
  },
  {
    key: "research", name: "研究", needsSymbol: false,
    endpoints: { snapshot: ["snapshot", {}], rules: ["rules", {}] },
    fields: [
      { label: "状态", where: "table_column", table: "运行", endpoint: "snapshot",
        path: ["runs", "[]", "status"], kind: "text" },
      { label: "标的", where: "table_column", table: "运行", endpoint: "snapshot",
        path: ["runs", "[]", "ticker"], kind: "text" },
      { label: "规则", where: "table_column", table: "规则候选池", endpoint: "rules",
        path: ["rules", "[]", "rule_id"], kind: "text" },
    ],
  },
  {
    key: "events", name: "事件", needsSymbol: true,
    endpoints: { events: ["events", { ticker: S, days: 400 }] },
    fields: [
      { label: "(事件日期)", where: "text", endpoint: "events", pattern: "(\\d{4}-\\d{2}-\\d{2})",
        values: (eps) => (eps.events.value?.events ?? []).map((row) => row.date), kind: "text" },
    ],
  },
  {
    key: "plan", name: "计划", needsSymbol: false,
    endpoints: { plan: ["plan", {}] },
    fields: [
      { label: "标的", where: "table_column", table: "当前计划", endpoint: "plan",
        path: ["plans", 0, "orders", "[]", "symbol"], kind: "text" },
      { label: "数量", where: "table_column", table: "当前计划", endpoint: "plan",
        path: ["plans", 0, "orders", "[]", "qty"], kind: "num0" },
      { label: "限价", where: "table_column", table: "当前计划", endpoint: "plan",
        path: ["plans", 0, "orders", "[]", "price"], kind: "num" },
      { label: "创建时间", where: "table_column", table: "计划列表", endpoint: "plan",
        path: ["plans", "[]", "created_at"], kind: "stamp" },
      { label: "订单数", where: "table_column", table: "计划列表", endpoint: "plan",
        values: (eps) => (eps.plan.value?.plans ?? []).map((row) => (row.orders ?? []).length),
        kind: "count" },
      { label: "(冻结时间)", where: "text", endpoint: "plan",
        pattern: "冻结时间 (\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})",
        values: (eps) => [eps.plan.value?.plans?.[0]?.created_at], kind: "stamp" },
    ],
  },
  {
    key: "pipeline", name: "流程", needsSymbol: false,
    endpoints: { pipeline: ["pipeline", {}], snapshot: ["snapshot", {}] },
    fields: [
      { label: "(日期)", where: "text", endpoint: "pipeline", pattern: "日期 (\\d{4}-\\d{2}-\\d{2})",
        values: (eps) => [eps.pipeline.value?.date], kind: "text" },
      { label: "行情同步", where: "step", endpoint: "pipeline", contains: true,
        path: ["markets", "SH", "stages", "sync_bars", "scheduled"], kind: "text" },
    ],
  },
  {
    key: "schedule", name: "调度", needsSymbol: false,
    endpoints: {
      schedule: ["schedule", {}], reconcile: ["reconcile", {}], pipeline: ["pipeline", {}],
    },
    fields: [
      { label: "最近运行", where: "table_column", table: "作业历史", endpoint: "schedule",
        path: ["jobs", "[]", "ran"], kind: "text" },
      { label: "作业", where: "table_column", table: "作业历史", endpoint: "schedule",
        path: ["jobs", "[]", "job"], kind: "text" },
      { label: "时间", where: "table_column", table: "告警", endpoint: "reconcile",
        path: ["alerts", "[]", "created_at"], kind: "stamp" },
      { label: "级别", where: "table_column", table: "告警", endpoint: "reconcile",
        path: ["alerts", "[]", "level"], kind: "text" },
    ],
  },
  {
    key: "audit", name: "审计", needsSymbol: false,
    endpoints: {
      audit: ["audit", {}], reconcile: ["reconcile", {}], sources: ["sources", {}],
    },
    fields: [
      { label: "信号", where: "statistic", endpoint: "audit", path: ["stats", "signals"], kind: "count" },
      { label: "订单相关响应", where: "statistic", endpoint: "audit", path: ["stats", "orders"], kind: "count" },
      { label: "成交（本地台账）", where: "statistic", endpoint: "audit", path: ["stats", "fills"], kind: "count" },
      { label: "时间", where: "table_column", table: "时间线", endpoint: "audit",
        path: ["entries", "[]", "at"], kind: "stampMinute" },
      { label: "标的", where: "table_column", table: "时间线", endpoint: "audit",
        path: ["entries", "[]", "ticker"], kind: "text" },
      { label: "标的", where: "table_column", table: "对账差异", endpoint: "reconcile",
        path: ["diffs", "[]", "symbol"], kind: "text" },
      { label: "本地", where: "table_column", table: "对账差异", endpoint: "reconcile",
        path: ["diffs", "[]", ["local", "local_value"]], kind: "num" },
      { label: "券商", where: "table_column", table: "对账差异", endpoint: "reconcile",
        path: ["diffs", "[]", ["broker", "broker_value"]], kind: "num" },
      { label: "(自检时间)", where: "text", endpoint: "sources",
        pattern: "自检时间 (\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2})",
        values: (eps) => [eps.sources.value?.checked_at], kind: "stamp" },
    ],
  },
  {
    key: "settings", name: "设置", needsSymbol: false,
    endpoints: {
      openapi: ["openapi_config", {}],
      autoPipeline: ["auto_pipeline", {}],
      rules: ["rules", {}],
    },
    fields: [
      { label: "凭据", where: "description", endpoint: "openapi",
        values: (eps) => [eps.openapi.value?.configured === true ? "已配置" : "未配置"], kind: "text" },
      { label: "通道", where: "description", endpoint: "openapi", path: ["channel"],
        kind: "text", contains: true },
      { label: "签名算法", where: "description", endpoint: "openapi", path: ["algorithm"], kind: "text" },
      { label: "AppKey（掩码）", where: "description", endpoint: "openapi", path: ["app_key_masked"], kind: "text" },
    ],
  },
];

/** 信号页：最新一条 signal 预览的某个字段。 */
function previewField(eps, key) {
  const row = (eps.snapshot.value?.previews ?? [])
    .find((item) => item?.kind === "signal" && item?.value && item.value[key] !== undefined);
  return row ? [row.value[key]] : [];
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
function parseArgs(argv) {
  const opts = { pages: null, json: false, symbol: null, verbose: false, help: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") opts.help = true;
    else if (arg === "--json") opts.json = true;
    else if (arg === "--verbose") opts.verbose = true;
    else if (arg === "--pages") opts.pages = String(argv[++i] ?? "");
    else if (arg.startsWith("--pages=")) opts.pages = arg.slice("--pages=".length);
    else if (arg === "--symbol") opts.symbol = String(argv[++i] ?? "");
    else {
      process.stderr.write(`未知参数：${arg}\n`);
      opts.help = true;
    }
  }
  return opts;
}
const OPTS = parseArgs(process.argv.slice(2));

async function getJson(url, timeoutMs = 3000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/** 与浏览器同一入口：POST /api/wb/<endpoint>，解开信封。 */
async function callApi(endpoint, payload = {}, timeoutMs = 60000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}/api/wb/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    });
    const body = await res.json().catch(() => null);
    if (!body) return { error: `响应非 JSON（HTTP ${res.status}）` };
    if (!body.ok) {
      return { error: String(body.error?.message ?? body.error?.code ?? "请求失败") };
    }
    return { value: body.value };
  } catch (error) {
    return { error: String(error?.message ?? error) };
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// 路径解析：字符串键 / 数字下标 / "[]" 摊平 / 候选键数组（取第一个非空）
// ---------------------------------------------------------------------------
function resolvePath(value, segments, depth = 0) {
  if (!Array.isArray(segments) || segments.length === 0) return [value];
  const [head, ...rest] = segments;
  if (head === "[]") {
    if (!Array.isArray(value)) return [];
    return value.flatMap((item) => resolvePath(item, rest, depth + 1));
  }
  if (typeof head === "number") {
    if (!Array.isArray(value)) return [];
    const picked = head < 0 ? value[value.length + head] : value[head];
    return resolvePath(picked, rest, depth + 1);
  }
  if (Array.isArray(head)) {
    // 候选键：取第一个「有值」的（空字符串/undefined/null 视为没有）
    if (value === null || typeof value !== "object") return [];
    for (const key of head) {
      if (hasData(value[key])) return resolvePath(value[key], rest, depth + 1);
    }
    // 全部缺失：仍按第一个键走下去，让「后端确实没有」也能被判定出来
    return resolvePath(undefined, rest, depth + 1);
  }
  if (value === null || typeof value !== "object") return [];
  return resolvePath(value[head], rest, depth + 1);
}

// ---------------------------------------------------------------------------
// CDP（与 e2e_web.mjs 同一手法：真实网络空闲 + DOM 稳定）
// ---------------------------------------------------------------------------
class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
    ws.addEventListener("message", (ev) => {
      let msg;
      try {
        msg = JSON.parse(typeof ev.data === "string" ? ev.data : String(ev.data));
      } catch { return; }
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
        return;
      }
      if (msg.method) for (const fn of this.handlers.get(msg.method) ?? []) fn(msg.params ?? {});
    });
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
  send(method, params = {}, timeoutMs = 30000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP 超时：${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => (clearTimeout(timer), resolve(v)),
        reject: (e) => (clearTimeout(timer), reject(e)),
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", () => reject(new Error(`WebSocket 连接失败：${wsUrl}`)), { once: true });
    });
    return new Cdp(ws);
  }
}

async function launchChromium(port, profileDir) {
  const child = spawn(CHROME, [
    "--headless=new", `--remote-debugging-port=${port}`, "--no-sandbox", "--disable-gpu",
    "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--window-size=1440,2400",
    `--user-data-dir=${profileDir}`, "about:blank",
  ], { stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.on("data", (d) => (stderr += String(d)));
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      const version = await getJson(`http://127.0.0.1:${port}/json/version`);
      return { child, version };
    } catch {
      if (child.exitCode !== null) throw new Error(`Chromium 提前退出：${stderr.slice(-300)}`);
      await sleep(250);
    }
  }
  child.kill("SIGKILL");
  throw new Error(`Chromium 20s 内未就绪：${stderr.slice(-300)}`);
}

async function findPageTarget(port) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    try {
      const list = await getJson(`http://127.0.0.1:${port}/json/list`);
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch { /* 继续等 */ }
    await sleep(200);
  }
  throw new Error("未找到可用的 page target");
}

async function evaluate(cdp, expression) {
  const res = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (res.exceptionDetails) throw new Error(`页面内求值失败：${res.exceptionDetails.text ?? "未知"}`);
  return res.result?.value;
}

/**
 * 页面内探针：标签→渲染文本。
 *   * statistic：`.ant-statistic-title` + `.ant-statistic-content`（含后缀）
 *   * description：`.ant-descriptions-item-label` + `-content`
 *   * table：表头 → 该列所有单元格（保留 `—`，空串也保留）
 *   * 每张表带上所属卡片标题，便于区分同页同名的表头
 *   * scanText：内容区全文，但**剔除**「原始返回」折叠块（JSON 逃生口不是展示文本）
 */
const DOM_PROBE = `(() => {
  const pick = () => document.querySelector('.ant-pro-layout-content')
    || document.querySelector('.ant-layout-content')
    || document.querySelector('#root main')
    || document.getElementById('root');
  const content = pick();
  if (!content) return { missing: true };
  const txt = (el) => (el ? (el.innerText || '').replace(/\\s+/g, ' ').trim() : '');
  const items = [];
  content.querySelectorAll('.ant-statistic').forEach((node) => {
    items.push({ where: 'statistic', label: txt(node.querySelector('.ant-statistic-title')),
      value: txt(node.querySelector('.ant-statistic-content')) });
  });
  // 说明：antd 5 的 Descriptions **两种形态**——bordered 渲染成 th/td（没有 .ant-descriptions-item
  // 包裹元素），非 bordered 才有包裹 div。只按 .ant-descriptions-item 取会漏掉全部 bordered 卡片
  // （实测：market 标的卡、risk 两张卡整片 NOT_RENDERED），故一律以 label 元素为锚点取它的内容兄弟。
  content.querySelectorAll('.ant-descriptions-item-label').forEach((labelEl) => {
    let valueEl = labelEl.nextElementSibling;
    if (!valueEl || !valueEl.classList.contains('ant-descriptions-item-content')) {
      const row = labelEl.closest('tr') || labelEl.parentElement;
      valueEl = row ? row.querySelector('.ant-descriptions-item-content') : null;
    }
    items.push({ where: 'description', label: txt(labelEl), value: txt(valueEl) });
  });
  const tables = [];
  content.querySelectorAll('.ant-table').forEach((table) => {
    const card = table.closest('.ant-card');
    const cardTitle = card ? txt(card.querySelector('.ant-card-head-title')) : '';
    const headers = [...table.querySelectorAll('.ant-table-thead th')].map(txt);
    const rows = [...table.querySelectorAll('.ant-table-tbody tr')]
      .filter((tr) => !tr.classList.contains('ant-table-measure-row'))
      .map((tr) => [...tr.querySelectorAll('td')].map(txt));
    tables.push({ card: cardTitle, headers, rows });
  });
  const scan = content.cloneNode(true);
  scan.querySelectorAll('.ant-collapse-content, .ant-table-measure-row').forEach((node) => node.remove());
  const cards = [...content.querySelectorAll('.ant-card-head-title')].map(txt);
  const steps = [];
  content.querySelectorAll('.ant-steps-item').forEach((node) => {
    steps.push({
      title: txt(node.querySelector('.ant-steps-item-title')),
      subTitle: txt(node.querySelector('.ant-steps-item-subtitle')),
      description: txt(node.querySelector('.ant-steps-item-description')),
    });
  });
  return {
    missing: false,
    items,
    tables,
    steps,
    cards,
    scanText: (scan.innerText || '').replace(/\\s+/g, ' ').trim(),
  };
})()`;

/** 标的输入框查找器（页面内求值）：**排除只读的搜索框**——页头市场筛选下拉也是一个
 *  `.ant-select-selection-search-input` 且 readOnly，按 document.querySelector 取第一个会打到它。 */
const INPUT_FINDER = `(() => {
  const root = document.querySelector('.ant-pro-layout-content')
    || document.querySelector('.ant-layout-content') || document.getElementById('root');
  const usable = (el) => el && !el.readOnly && !el.disabled;
  const scoped = [...(root ?? document).querySelectorAll('.ant-select-selection-search-input')].find(usable);
  if (scoped) return scoped;
  const anySelect = [...document.querySelectorAll('.ant-select-selection-search-input')].find(usable);
  if (anySelect) return anySelect;
  const plain = [...(root ?? document).querySelectorAll('input')]
    .find((el) => usable(el) && el.type !== 'hidden');
  return plain ?? null;
})()`;

/** 轮询等待标的输入框出现（SPA 首屏渲染后才有；AutoComplete 渲染为 .ant-select-search__field
 *  / .ant-select-selection-search-input，多数 antd 版本两者之一）。 */
async function waitForInput(cdp, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const found = await evaluate(cdp, `(() => {
      const el = ${INPUT_FINDER};
      return el ? (el.tagName + '|' + el.className + '|' + (el.placeholder || '')) : null;
    })()`);
    if (found) return found;
    await sleep(300);
  }
  return null;
}

/** 在页面里填入标的并按回车（优先用 CDP 真实键盘输入；失败再退回原生 setter）。 */
async function fillSymbol(cdp, symbol) {
  const found = await waitForInput(cdp);
  if (!found) return { ok: false, reason: "页面没有可聚焦的标的输入框（15s 内未出现 input）" };
  const focused = await evaluate(cdp, `(() => {
    const el = ${INPUT_FINDER};
    if (!el) return false;
    el.focus();
    return document.activeElement === el;
  })()`);
  if (!focused) return { ok: false, reason: "标的输入框无法聚焦" };
  // 真鼠标点一下：tags 型 Select（因子页关注池）与 AutoComplete 都要**先打开下拉**
  // 才会生成可写的搜索输入框；只 focus() 后 insertText 在 tags 模式下不落地（实测）。
  const box = await evaluate(cdp, `(() => {
    const el = ${INPUT_FINDER};
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return { x: rect.left + Math.min(20, rect.width / 2), y: rect.top + rect.height / 2 };
  })()`);
  if (box) {
    for (const type of ["mousePressed", "mouseReleased"]) {
      await cdp.send("Input.dispatchMouseEvent", {
        type, x: box.x, y: box.y, button: "left", clickCount: 1,
      });
    }
    await sleep(250);
  }
  // 逐字符走浏览器真实文本输入路径（比 insertText 对受控 rc-select 更可靠）
  for (const char of symbol) {
    await cdp.send("Input.dispatchKeyEvent", { type: "char", text: char });
  }
  await sleep(150);
  await cdp.send("Input.dispatchKeyEvent", {
    type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
  });
  await cdp.send("Input.dispatchKeyEvent", {
    type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
  });
  await sleep(200);
  // tags 型 Select 提交后会把搜索框清空（值变成 .ant-select-selection-item 里的标签），
  // 所以「搜索框为空」不等于失败：先看标签，再看输入框值。
  const tags = await evaluate(cdp, `(() => {
    return [...document.querySelectorAll('.ant-select-selection-item')]
      .map((el) => (el.textContent || '').trim());
  })()`);
  const committedTags = Array.isArray(tags)
    && symbol.split(",").every((piece) => tags.some((tag) => tag.includes(piece)));
  if (committedTags) return { ok: true, via: "tags", tags };
  const applied = await evaluate(cdp, `(() => {
    const el = ${INPUT_FINDER};
    return el ? el.value : null;
  })()`);
  if (String(applied ?? "") !== symbol) {
    // 退回原生 setter + React 合成事件（CDP insertText 对 rc-select 偶发无效）
    const fallback = await evaluate(cdp, `(() => {
      const el = ${INPUT_FINDER};
      if (!el) return "无输入框";
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(el, ${JSON.stringify(symbol)});
      el.dispatchEvent(new Event('input', { bubbles: true }));
      for (const type of ['keydown', 'keypress', 'keyup']) {
        el.dispatchEvent(new KeyboardEvent(type, {
          key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true,
        }));
      }
      return el.value;
    })()`);
    if (String(fallback ?? "") !== symbol) {
      // 诊断：把页面上所有 input 的类名/可写性列出来，便于判断选择器打到了哪个元素
      const inputs = await evaluate(cdp, `(() => {
        return [...document.querySelectorAll('input')].slice(0, 12).map((el) => ({
          cls: el.className, type: el.type, ro: el.readOnly, dis: el.disabled,
          ph: el.placeholder, val: el.value,
        }));
      })()`);
      return { ok: false, reason: `输入框未被填入（CDP=${JSON.stringify(applied)}，回退=${JSON.stringify(fallback)}）`
        + `｜页面 input：${JSON.stringify(inputs)}` };
    }
  }
  return { ok: true };
}

// ---------------------------------------------------------------------------
// 判定
// ---------------------------------------------------------------------------
function renderedFor(field, probe, spec) {
  if (field.where === "table_column") {
    const out = [];
    let tableFound = false;
    // 同一张卡里可能有多张表（执行页 OpenAPI 四张表共用一张卡片、列名还重名）：
    // 用 tableOrder 按 DOM 顺序定位第 N 张，避免把别的表的数据当成本表的数据。
    let matched = 0;
    for (const table of probe.tables ?? []) {
      if (field.table && !String(table.card ?? "").includes(field.table)) continue;
      const index = (table.headers ?? []).findIndex((h) => h === field.label
        || h.startsWith(field.label));
      if (index < 0) continue;
      if (field.tableOrder !== undefined && matched !== field.tableOrder) { matched += 1; continue; }
      matched += 1;
      tableFound = true;
      for (const row of table.rows ?? []) {
        if (row[index] !== undefined) out.push(row[index]);
      }
      if (field.tableOrder !== undefined) break;
    }
    return { values: out, labelFound: tableFound,
      source: `表列「${field.label}」${field.tableOrder !== undefined ? `（卡片第 ${field.tableOrder + 1} 张表）` : ""}` };
  }
  if (field.where === "step") {
    const values = [];
    for (const step of probe.steps ?? []) {
      if (step.title !== field.label && !String(step.title).startsWith(field.label)) continue;
      values.push([step.subTitle, step.description].filter(Boolean).join(" "));
    }
    return { values, labelFound: values.length > 0, source: `步骤「${field.label}」` };
  }
  if (field.where === "text") {
    // 行内文案（汇总句/元信息）没有标签结构：用正则从内容区全文里抽取值
    if (!field.pattern) return { values: [], labelFound: false, source: "text（未给 pattern）" };
    const re = new RegExp(field.pattern, "g");
    const text = probe.scanText ?? "";
    const values = [];
    for (const match of text.matchAll(re)) {
      const picked = match[field.group ?? 1];
      if (picked !== undefined) values.push(String(picked));
    }
    return { values, labelFound: values.length > 0, source: `文案 /${field.pattern}/` };
  }
  const wanted = field.where === "statistic" ? "statistic" : "description";
  const values = (probe.items ?? [])
    .filter((item) => item.where === wanted
      && (item.label === field.label || String(item.label).startsWith(field.label)))
    .map((item) => item.value);
  return { values, labelFound: values.length > 0, source: `${wanted}「${field.label}」` };
}

/** 后端事实 → 判定用的值列表：values 函数优先；paths 是复合单元格的多路取值；agg=count 收敛成条数。 */
function backendValues(field, eps) {
  if (typeof field.values === "function") {
    const out = field.values(eps);
    return (Array.isArray(out) ? out : [out]).filter((value) => value !== undefined);
  }
  const call = eps[field.endpoint];
  const paths = field.paths ?? [field.path];
  const raw = paths.flatMap((segments) => resolvePath(call?.value, segments ?? []));
  if (field.agg === "count") return [raw.length];
  return raw;
}

/** 一行字段的判定：后端事实 → 契约 → 渲染文本，三态比对。 */
function judgeField(field, spec, endpoints, probe, symbol) {
  const base = {
    page: spec.key, label: field.label, where: field.where,
    endpoint: endpoints[field.endpoint]?.name ?? field.endpoint,
    path: field.paths
      ? field.paths.map((segments) => `/${segments.join("/")}`).join(" + ")
      : (field.path ? `/${field.path.map((p) => (Array.isArray(p) ? p.join("|") : p)).join("/")}`
        : (typeof field.values === "function" ? "(派生值)" : "")),
    kind: field.kind,
  };
  const call = endpoints[field.endpoint];
  if (!call || call.error) {
    return { ...base, judge: "UNJUDGED", backend: null, rendered: null,
      note: `端点数取失败（无后端事实可比）：${call?.error ?? "未声明端点"}` };
  }
  const values = backendValues(field, endpoints);
  const dataValues = values.filter(hasData);
  const rendered = renderedFor(field, probe, spec);
  const renderedValues = rendered.values;

  // 契约：每个非空后端值给出可接受文本 + 明确的错误形态
  const accept = new Set();
  const rejects = [];
  for (const value of dataValues) {
    const contract = contractOf(value, field.kind);
    for (const text of contract.accept) accept.add(text);
    if (contract.reject) rejects.push({ re: contract.reject, why: contract.why });
  }
  const acceptList = [...accept].filter((text) => text !== DASH && text !== "");
  const joined = renderedValues.join(" ｜ ");
  const hit = (text) => ((field.contains || field.paths)
    ? joined.includes(text) : renderedValues.includes(text));

  const summary = {
    ...base,
    backend: dataValues.length === 0
      ? (values.length === 0 ? "(路径无值)" : "(空值)")
      : sample(values),
    rendered: renderedValues.length === 0 ? "(无)" : sample(renderedValues),
  };

  if (!rendered.labelFound) {
    return { ...summary, judge: "NOT_RENDERED",
      note: `页面 DOM 里没有 ${rendered.source}（条件渲染或本页无此卡片）` };
  }
  if (dataValues.length === 0) {
    const poison = renderedValues.find((text) => POISON.some((p) => text.includes(p)));
    if (poison) {
      return { ...summary, judge: "FABRICATED", note: `后端无值、渲染出现 ${poison}` };
    }
    const suspicious = renderedValues.filter((text) => /^[+-]?\d[\d,.]*$/.test(text.trim()));
    if (suspicious.length === 0) {
      return { ...summary, judge: "EMPTY", note: "两端都空：后端值缺失、页面显示 —（正确行为）" };
    }
    return { ...summary, judge: "FABRICATED",
      note: `后端无值却渲染出具体数值：${sample(suspicious)}` };
  }
  const allDash = renderedValues.every((text) => text === "" || text === DASH
    || /^[—\-–]+$/.test(text));
  const hits = acceptList.filter(hit);
  if (hits.length > 0) {
    const coverage = `${hits.length}/${acceptList.length}`;
    return { ...summary, judge: "ok",
      note: field.kindNote ? `${field.kindNote}；期望值命中 ${coverage}` : `期望值命中 ${coverage}` };
  }
  if (allDash) {
    return { ...summary, judge: "MISSING_WHEN_DATA",
      note: `后端有 ${dataValues.length} 个非空值，渲染全是 —：期望至少出现 ${sample(acceptList)}` };
  }
  const poison = renderedValues.find((text) => POISON.some((p) => text.includes(p)));
  if (poison) {
    return { ...summary, judge: "FABRICATED", note: `渲染出现 ${poison}（后端值：${sample(dataValues)}）` };
  }
  const rejected = rejects.find(({ re }) => renderedValues.some((text) => re.test(text)));
  if (rejected) {
    return { ...summary, judge: "FORMAT",
      note: `${rejected.why}：渲染 ${sample(renderedValues)}，期望 ${sample(acceptList)}` };
  }
  return { ...summary, judge: "FORMAT",
    note: `渲染值与后端值对不上：期望 ${sample(acceptList)}，实际 ${sample(renderedValues)}` };
}

function sample(values, limit = 4) {
  const list = values.slice(0, limit).map((value) => (typeof value === "string"
    ? value : JSON.stringify(value)));
  return list.join(" / ") + (values.length > limit ? ` …（共 ${values.length}）` : "");
}

/** 渲染文本的全局扫雷：时间戳带 T / 毒值 / 未分组的大数。 */
function scanText(text) {
  const findings = [];
  const tStamp = text.match(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?/g);
  if (tStamp) findings.push({ kind: "T_TIMESTAMP", sample: [...new Set(tStamp)].slice(0, 3).join(" / ") });
  for (const poison of POISON) {
    if (text.includes(poison)) findings.push({ kind: "POISON", sample: poison });
  }
  return findings;
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------
async function main() {
  if (OPTS.help) {
    log("页面字段级审计（后端事实 × 浏览器渲染，零新依赖）");
    log("  用法：node scripts/audit_page_fields.mjs [--pages a,b] [--json] [--symbol SH.600000] [--verbose]");
    log("    --pages a,b  只审计指定路由（单页复验）");
    log("    --json       额外输出报告路径");
    log("    --symbol     标的类页面用的标的（默认 SH.600000）");
    log("    --verbose    打印每条字段明细");
    log("  退出码：0 无缺陷 / 1 有缺陷 / 2 服务未就绪或中断");
    log(`  可用路由：${PAGE_SPEC.map((p) => p.key).join(",")}`);
    return 0;
  }

  // 健康门
  const healthDeadline = Date.now() + HEALTH_TIMEOUT_MS;
  let health = null;
  let healthError = null;
  while (Date.now() < healthDeadline) {
    try {
      const body = await getJson(`${BASE}/healthz`, 3000);
      if (body?.ok) { health = body; break; }
      healthError = `ok=${body?.ok}`;
    } catch (error) { healthError = String(error?.message ?? error); }
    await sleep(1000);
  }
  if (!health) {
    log(`❌ 服务未就绪：${BASE}/healthz 未就绪（${healthError ?? "无响应"}）`);
    return 2;
  }

  // 标的默认取关注池第一只（与页面候选同源）；多标的页取前 3 只（factors 需 2..8、ic 需 3..8）
  let symbol = OPTS.symbol ?? "";
  let tickers3 = [];
  {
    const snap = await callApi("snapshot", {});
    const watchlist = Array.isArray(snap.value?.watchlist) ? snap.value.watchlist.map(String) : [];
    if (!symbol) symbol = watchlist.length > 0 ? watchlist[0] : "SH.600000";
    tickers3 = watchlist.slice(0, 3);
    if (tickers3.length < 3) tickers3 = [symbol, "SH.600009", "SH.600010"];
    // 部分页面的数据面只支持特定市场（实测：期权链对 A 股标的直接 -8 拒绝），
    // 故允许页面声明 preferMarket —— 从关注池里挑第一只该市场链的标的（挑不到退回默认）。
    for (const page of PAGE_SPEC) {
      if (!page.preferMarket) continue;
      const picked = watchlist.find((item) => String(item).startsWith(`${page.preferMarket}.`));
      page.symbol = picked ?? symbol;
    }
  }
  const mode = health.mode ?? "sim";

  await mkdir(OUT_DIR, { recursive: true });
  const report = {
    startedAt: new Date().toISOString(),
    base: BASE, mode, symbol,
    options: { pages: OPTS.pages, verbose: OPTS.verbose },
    pages: [], defects: [], observations: [],
  };

  let selected = PAGE_SPEC;
  if (OPTS.pages !== null) {
    const wanted = OPTS.pages.split(",").map((s) => s.trim()).filter(Boolean);
    const known = new Set(PAGE_SPEC.map((p) => p.key));
    const unknown = wanted.filter((k) => !known.has(k));
    if (unknown.length) {
      log(`❌ --pages 含未知路由：${unknown.join(",")}（可用：${[...known].join(",")}）`);
      return 2;
    }
    selected = PAGE_SPEC.filter((p) => wanted.includes(p.key));
  }

  log("页面字段级审计");
  log(`  目标：${BASE}｜模式 ${mode}｜标的 ${symbol}｜多标的 ${tickers3.join(",")}`);
  log(`  路由：${selected.map((p) => p.key).join(",")}（共 ${selected.length}/${PAGE_SPEC.length}）`);
  log(`  产物：${OUT_DIR}`);

  const profileDir = await mkdtemp(path.join(tmpdir(), "audit-fields-profile-"));
  const port = 30000 + Math.floor(Math.random() * 20000);
  let child = null;
  try {
    const launched = await launchChromium(port, profileDir);
    child = launched.child;
    const target = await findPageTarget(port);
    const cdp = await Cdp.connect(target.webSocketDebuggerUrl);
    for (const m of ["Page.enable", "Runtime.enable", "Network.enable"]) await cdp.send(m);
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440, height: 2400, deviceScaleFactor: 1, mobile: false,
    });

    const net = { inflight: new Map(), lastEventAt: Date.now() };
    cdp.on("Network.requestWillBeSent", (p) => {
      net.inflight.set(p.requestId, 1);
      net.lastEventAt = Date.now();
    });
    const done = (p) => { net.inflight.delete(p.requestId); net.lastEventAt = Date.now(); };
    cdp.on("Network.loadingFinished", done);
    cdp.on("Network.loadingFailed", done);
    let loadFired = false;
    cdp.on("Page.loadEventFired", () => (loadFired = true));

    for (const spec of selected) {
      const routeStart = Date.now();
      const routeDeadline = routeStart + ROUTE_BUDGET_MS;
      // ① 后端事实（标的页用自己的标的：preferMarket 优先）
      const pageSymbol = spec.symbol ?? symbol;
      const endpoints = {};
      const substitute = (value) => (value === "$mode" ? mode
        : value === S ? pageSymbol
          : value === T3 ? tickers3
            : Array.isArray(value) ? value.map(substitute) : value);
      const resolvePayload = (payload) => Object.fromEntries(
        Object.entries(payload).map(([k, v]) => [k, substitute(v)]));
      for (const [key, [name, payload]] of Object.entries(spec.endpoints)) {
        const effective = resolvePayload(payload);
        const result = await callApi(name, effective);
        endpoints[key] = { name, payload: effective, value: result.value, error: result.error };
      }
      // ② 页面事实
      loadFired = false;
      await cdp.send("Page.navigate", { url: "about:blank" });
      await sleep(150);
      net.inflight.clear();
      net.lastEventAt = Date.now();
      await cdp.send("Page.navigate", { url: `${BASE}/#/${spec.key}` });
      const dl = Date.now() + 15000;
      while (!loadFired && Date.now() < dl) await sleep(80);

      let symbolFill = null;
      if (spec.needsSymbol) {
        // 标的页的卡片只有 query 非空才渲染：必须先填入并回车，再等取数网络
        symbolFill = await fillSymbol(cdp, pageSymbol);
        await sleep(300);
      } else if (spec.needsTickers) {
        // 因子页是 tags 型多标的输入框（Select mode="tags"）：打逗号分隔的 3 只再回车
        symbolFill = await fillSymbol(cdp, tickers3.join(","));
        await sleep(300);
      }
      // 网络空闲
      const waitStart = Date.now();
      let idle = false;
      while (Date.now() - waitStart < MAX_WAIT_MS && Date.now() < routeDeadline) {
        if (net.inflight.size === 0 && Date.now() - net.lastEventAt >= IDLE_MS) { idle = true; break; }
        await sleep(80);
      }
      // DOM 稳定：连续 3 次采样签名不变
      const sig = (p) => `${(p.items ?? []).length}|${(p.tables ?? []).length}|${(p.scanText ?? "").length}`;
      let probe = await evaluate(cdp, DOM_PROBE);
      let stable = 1;
      let last = sig(probe);
      const stableStart = Date.now();
      while (Date.now() - stableStart < Math.min(12000, Math.max(0, routeDeadline - Date.now()))) {
        await sleep(500);
        const cur = await evaluate(cdp, DOM_PROBE);
        const curSig = sig(cur);
        stable = curSig === last ? stable + 1 : 1;
        last = curSig;
        probe = cur;
        if (stable >= 3 && net.inflight.size === 0) break;
      }

      // ③ 判定
      const fields = spec.fields.map((field) => judgeField(field, spec, endpoints, probe, symbol));
      const scans = scanText(probe.scanText ?? "");
      const endpointErrors = Object.entries(endpoints)
        .filter(([, call]) => call.error)
        .map(([key, call]) => ({ endpoint: call.name, error: call.error, key }));
      const entry = {
        route: spec.key, name: spec.name,
        url: `${BASE}/#/${spec.key}`,
        symbol: pageSymbol, idle, symbolFill,
        endpoints: Object.fromEntries(Object.entries(endpoints)
          .map(([k, v]) => [k, { name: v.name, payload: v.payload, error: v.error ?? null }])),
        fieldCount: fields.length,
        fields, scans, endpointErrors,
        cards: probe.cards ?? [],
        // 诊断用：页面上每张表的卡片标题与表头（定位「字段取自哪张表」时的第一手证据）
        tables: (probe.tables ?? []).map((table) => ({
          card: table.card, headers: table.headers, rows: (table.rows ?? []).length,
        })),
        routeMs: Date.now() - routeStart,
      };
      report.pages.push(entry);

      const bad = fields.filter((f) => ["MISSING_WHEN_DATA", "FORMAT", "FABRICATED"].includes(f.judge));
      const flags = [];
      if (bad.length) flags.push(`缺陷x${bad.length}`);
      if (scans.length) flags.push(`扫雷x${scans.length}`);
      if (endpointErrors.length) flags.push(`端点失败x${endpointErrors.length}`);
      if (symbolFill && !symbolFill.ok) flags.push(`标的未填入:${symbolFill.reason}`);
      log(`  ${spec.key.padEnd(10)} 字段${String(fields.length).padStart(3)} ` +
        `${flags.length ? "⚠ " + flags.join(" ") : "✓"}（${entry.routeMs}ms）`);
    }
  } catch (error) {
    report.fatal = String(error?.message ?? error);
    log(`❌ 审计中断：${report.fatal}`);
  } finally {
    if (child && child.exitCode === null) {
      child.kill("SIGTERM");
      const dl = Date.now() + 5000;
      while (child.exitCode === null && Date.now() < dl) await sleep(100);
      if (child.exitCode === null) child.kill("SIGKILL");
    }
    await rm(profileDir, { recursive: true, force: true }).catch(() => {});
  }

  // 汇总
  for (const page of report.pages) {
    for (const field of page.fields) {
      if (["MISSING_WHEN_DATA", "FORMAT", "FABRICATED"].includes(field.judge)) {
        report.defects.push({
          severity: field.judge === "MISSING_WHEN_DATA" ? "严重" : "重要",
          page: field.page, label: field.label, where: field.where,
          endpoint: field.endpoint, path: field.path, kind: field.kind,
          backend: field.backend, rendered: field.rendered,
          judge: field.judge, note: field.note,
        });
      } else if (field.judge === "UNJUDGED" || field.judge === "NOT_RENDERED") {
        report.observations.push({
          severity: "次要", page: field.page, label: field.label, judge: field.judge,
          note: field.note, backend: field.backend, rendered: field.rendered,
        });
      }
    }
    for (const scan of page.scans) {
      report.defects.push({
        severity: "重要", page: page.route, label: `(全文扫雷) ${scan.kind}`,
        backend: null, rendered: scan.sample, judge: scan.kind,
        note: scan.kind === "T_TIMESTAMP"
          ? "内容区出现带 T 的 ISO 时间戳（应经 stampOf 转成空格分隔）"
          : "内容区出现 NaN/undefined/[object Object]/Invalid Date 一类毒值",
      });
    }
    for (const err of page.endpointErrors) {
      report.observations.push({
        severity: "次要", page: page.route, label: err.endpoint, judge: "ENDPOINT_ERROR",
        note: `端点数取失败，相关字段无法判定：${err.error}`, backend: null, rendered: null,
      });
    }
  }

  const totals = {
    pages: report.pages.length,
    fields: report.pages.reduce((sum, p) => sum + p.fieldCount, 0),
    ok: report.pages.reduce((sum, p) => sum + p.fields.filter((f) => f.judge === "ok").length, 0),
    empty: report.pages.reduce((sum, p) => sum + p.fields.filter((f) => f.judge === "EMPTY").length, 0),
    defects: report.defects.length,
    byJudge: report.defects.reduce((acc, d) => {
      acc[d.judge] = (acc[d.judge] ?? 0) + 1;
      return acc;
    }, {}),
    observations: report.observations.length,
  };
  report.totals = totals;
  report.finishedAt = new Date().toISOString();

  const reportFile = path.join(OUT_DIR, "report.json");
  await writeFile(reportFile, JSON.stringify(report, null, 2));

  log("");
  log("=== 结果 ===");
  log(`  页面 ${totals.pages}｜字段 ${totals.fields}（ok ${totals.ok}｜空态正确 ${totals.empty}）`
    + `｜缺陷 ${totals.defects}（${JSON.stringify(totals.byJudge)}）｜观察 ${totals.observations}`);
  for (const page of report.pages) {
    const bad = page.fields.filter((f) => ["MISSING_WHEN_DATA", "FORMAT", "FABRICATED"].includes(f.judge));
    log(`  ${page.route.padEnd(10)} 字段${String(page.fieldCount).padStart(3)} ` +
      `ok${String(page.fields.filter((f) => f.judge === "ok").length).padStart(3)} ` +
      `缺陷${String(bad.length).padStart(2)}` +
      (bad.length ? ` → ${[...new Set(bad.map((f) => `${f.label}:${f.judge}`))].slice(0, 4).join("、")}` : ""));
  }
  if (report.defects.length) {
    log("  缺陷清单：");
    for (const d of report.defects) {
      log(`    [${d.severity}] ${d.page} ${d.label}（${d.where}）${d.judge}：`
        + `后端 ${String(d.backend).slice(0, 60)} ｜ 渲染 ${String(d.rendered).slice(0, 60)}`);
    }
  }
  if (report.observations.length) {
    log("  观察项（不判缺陷）：");
    for (const o of report.observations.slice(0, 15)) {
      log(`    ${o.page} ${o.label} ${o.judge}：${String(o.note).slice(0, 110)}`);
    }
  }
  log(`  报告：${reportFile}`);
  if (OPTS.json) log(`  JSON: ${reportFile}`);

  if (report.fatal) return 2;
  return report.defects.length > 0 ? 1 : 0;
}

// 被 import（单测）时不自动执行——只有当作 CLI 直接运行才跑 main。
const isMain = process.argv[1]
  && pathToFileURL(process.argv[1]).href === import.meta.url;
if (isMain) {
  main()
    .then((code) => process.exit(typeof code === "number" ? code : 0))
    .catch((error) => {
      log(`审计致命错误：${String(error?.stack ?? error)}`);
      process.exit(2);
    });
}

// 纯逻辑导出（`tests/audit-page-fields.test.mjs` 直测：规格自洽 + 契约与 formatCore 同口径）。
export { CONTRACTS, PAGE_SPEC, contractOf, judgeField, renderedFor, resolvePath };
