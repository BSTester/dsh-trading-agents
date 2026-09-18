// 标的联想候选纯函数（WP24）：代码归一 + 候选构造/过滤 + 多标的逗号串。
// 宿主无关（不引 React/DOM），node --test 直测；与后端 to_futu_symbol 的跨语言镜像由
// tests/test_wp24_symbol_mirror.py 锁定。
import test from "node:test";
import assert from "node:assert/strict";
import {
  toFutuSymbol, symbolCandidates, symbolCandidatesFromPayloads, filterSymbolCandidates,
  splitSymbolList, joinSymbolList,
} from "../src/services/symbols.js";

// ---------------------------------------------------------------- toFutuSymbol

test("toFutuSymbol：已带前缀原样（大小写/空白归一）", () => {
  assert.equal(toFutuSymbol("SH.600519"), "SH.600519");
  assert.equal(toFutuSymbol("hk.00700"), "HK.00700");
  assert.equal(toFutuSymbol("  Bj.830799  "), "BJ.830799");
  assert.equal(toFutuSymbol("US.AAPL"), "US.AAPL");
  // 美股代码里的点号属于代码本身（^[A-Z0-9.]+$ 允许），不得当成「代码.市场」换序
  assert.equal(toFutuSymbol("US.BRK.B"), "US.BRK.B");
});

test("toFutuSymbol：6 位纯数字按首位分流（6/9→SH、4/8→BJ、其余→SZ）", () => {
  assert.equal(toFutuSymbol("600519"), "SH.600519");
  assert.equal(toFutuSymbol("900901"), "SH.900901");
  assert.equal(toFutuSymbol("000001"), "SZ.000001");
  assert.equal(toFutuSymbol("002475"), "SZ.002475");
  assert.equal(toFutuSymbol("300750"), "SZ.300750");
  assert.equal(toFutuSymbol("830799"), "BJ.830799");
  assert.equal(toFutuSymbol("430047"), "BJ.430047");
});

test("toFutuSymbol：1~5 位纯数字按港股处理（左侧补零到 5 位）", () => {
  assert.equal(toFutuSymbol("700"), "HK.00700");
  assert.equal(toFutuSymbol("0700"), "HK.00700");
  assert.equal(toFutuSymbol("00700"), "HK.00700");
  assert.equal(toFutuSymbol("1"), "HK.00001");
  assert.equal(toFutuSymbol("99999"), "HK.99999");
});

test("toFutuSymbol：代码.市场 写法换序", () => {
  assert.equal(toFutuSymbol("600519.SH"), "SH.600519");
  assert.equal(toFutuSymbol("000001.sz"), "SZ.000001");
  assert.equal(toFutuSymbol("aapl.us"), "US.AAPL");
});

test("toFutuSymbol：其余一律按美股处理", () => {
  assert.equal(toFutuSymbol("AAPL"), "US.AAPL");
  assert.equal(toFutuSymbol("brk.b"), "US.BRK.B");
  assert.equal(toFutuSymbol("SPY"), "US.SPY");
});

// ----------------------------------------------------------- symbolCandidates

const POSITIONS = {
  mode: "sim",
  groups: [
    { market: 1, positions: [{ symbol: "00700", name: "腾讯控股" },
                             { symbol: "00100", name: "MINIMAX-W" }] },
    { market: 3, positions: [{ symbol: "600519", name: "" }] },
  ],
};

test("symbolCandidates：关注池在前、持仓在后，持仓裸代码经 toFutuSymbol 归一", () => {
  const rows = symbolCandidates({ watchlist: ["SH.600519", "HK.00700"], positions: POSITIONS });
  assert.deepEqual(rows, [
    { value: "SH.600519", label: "SH.600519", market: "SH" },
    { value: "HK.00700", label: "HK.00700 腾讯控股", market: "HK" },
    { value: "HK.00100", label: "HK.00100 MINIMAX-W", market: "HK" },
  ]);
});

test("symbolCandidates：同值只留一条，位置保持首次出现处、名称取有名称的那条", () => {
  const rows = symbolCandidates({ watchlist: ["SH.600519", "HK.00700"],
                                  positions: { groups: [{ positions: [{ symbol: "600519", name: "贵州茅台" }] }] } });
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0], { value: "SH.600519", label: "SH.600519 贵州茅台", market: "SH" });
  assert.deepEqual(rows[1], { value: "HK.00700", label: "HK.00700", market: "HK" });
});

test("symbolCandidates：关注池里的裸代码也归一（配置写错写法不产出垃圾候选）", () => {
  const rows = symbolCandidates({ watchlist: ["600519", "700"], positions: null });
  assert.deepEqual(rows, [
    { value: "SH.600519", label: "SH.600519", market: "SH" },
    { value: "HK.00700", label: "HK.00700", market: "HK" },
  ]);
});

test("symbolCandidates：持仓行可直接给数组；空输入给空数组", () => {
  const rows = symbolCandidates({ watchlist: [], positions: [{ symbol: "AAPL", name: "苹果" }] });
  assert.deepEqual(rows, [{ value: "US.AAPL", label: "US.AAPL 苹果", market: "US" }]);
  assert.deepEqual(symbolCandidates({}), []);
  assert.deepEqual(symbolCandidates(), []);
  assert.deepEqual(symbolCandidates({ watchlist: null, positions: { groups: null } }), []);
});

test("symbolCandidates：空代码/无 symbol 的持仓行被丢弃，不产出空候选", () => {
  const rows = symbolCandidates({ watchlist: ["", "  "],
                                  positions: { groups: [{ positions: [{ symbol: "", name: "x" },
                                                                       { name: "无代码" }] }] } });
  assert.deepEqual(rows, []);
});

// ------------------------------------------------- snapshot/positions 接线

test("symbolCandidatesFromPayloads：从**端点载荷**组装候选（关注池键名就是 watchlist）", () => {
  // 这是一次真实缺陷的回归钉：hooks 里的取数曾把 snapshot 关注池装进名为 pool 的局部变量，
  // 组装时传成 `{pool, positions}`，而 symbolCandidates 要的是 `{watchlist, …}` ——
  // 关注池被静默丢弃，候选只剩持仓（下拉里看不到池子里的 A 股）。接线只此一处，
  // 用真实的载荷形状测它，才不会再有第二种「键名碰巧对不上」的写法。
  const snapshotValue = { mode: "sim", endpoints: ["snapshot"], watchlist: ["SH.600519", "HK.00700"] };
  const positionsValue = { groups: [{ market: 3, positions: [{ symbol: "600519", name: "贵州茅台" }] }] };
  assert.deepEqual(symbolCandidatesFromPayloads(snapshotValue, positionsValue), [
    { value: "SH.600519", label: "SH.600519 贵州茅台", market: "SH" },
    { value: "HK.00700", label: "HK.00700", market: "HK" },
  ]);
  // 旧服务/异常载荷：没有 watchlist 键时只剩持仓候选，不抛错
  assert.deepEqual(
    symbolCandidatesFromPayloads({ mode: "sim" }, { groups: [{ positions: [{ symbol: "700" }] }] }),
    [{ value: "HK.00700", label: "HK.00700", market: "HK" }]);
  assert.deepEqual(symbolCandidatesFromPayloads(undefined, undefined), []);
});

// ----------------------------------------------------- filterSymbolCandidates

const CANDIDATES = symbolCandidates({
  watchlist: ["SH.600519", "SH.600036", "SZ.000001", "HK.00700", "US.AAPL"],
  positions: { groups: [{ market: 3, positions: [{ symbol: "601899", name: "紫金矿业" }] }] },
});

test("filterSymbolCandidates：大小写不敏感，支持只输数字/带前缀/中文名", () => {
  const values = (input) => filterSymbolCandidates(CANDIDATES, input).map((row) => row.value);
  assert.deepEqual(values("600"), ["SH.600519", "SH.600036"]);
  assert.deepEqual(values("hk.007"), ["HK.00700"]);
  assert.deepEqual(values("HK.007"), ["HK.00700"]);
  assert.deepEqual(values("aapl"), ["US.AAPL"]);
  assert.deepEqual(values("紫金"), ["SH.601899"]);
  assert.deepEqual(values("000001"), ["SZ.000001"]);
  assert.deepEqual(values("不存在的代码"), []);
});

test("filterSymbolCandidates：空输入按原序给前 N 条（点开即可选）", () => {
  assert.deepEqual(filterSymbolCandidates(CANDIDATES, "").map((row) => row.value),
                   CANDIDATES.map((row) => row.value));
  assert.deepEqual(filterSymbolCandidates(CANDIDATES, "   ").map((row) => row.value),
                   CANDIDATES.map((row) => row.value));
  assert.equal(filterSymbolCandidates(CANDIDATES, "").length, 6);
});

test("filterSymbolCandidates：limit 默认 20，显式 limit/非法 limit 有确定行为", () => {
  const many = Array.from({ length: 30 }, (_, index) => ({
    value: `SH.60${String(index).padStart(4, "0")}`, label: `x${index}`, market: "SH",
  }));
  assert.equal(filterSymbolCandidates(many, "").length, 20);
  assert.equal(filterSymbolCandidates(many, "", 5).length, 5);
  assert.deepEqual(filterSymbolCandidates(many, "", 0), []);
  assert.deepEqual(filterSymbolCandidates(many, "", -3), []);
  assert.deepEqual(filterSymbolCandidates(null, "600"), []);
});

// -------------------------------------------------- split/joinSymbolList

test("splitSymbolList：逗号（中英文）/空白分隔、去空、大写归一、去重保序", () => {
  assert.deepEqual(splitSymbolList("SH.600519,SH.600036"), ["SH.600519", "SH.600036"]);
  assert.deepEqual(splitSymbolList("sh.600519，SH.600519  hk.00700"), ["SH.600519", "HK.00700"]);
  assert.deepEqual(splitSymbolList("  600519 , , 000001 "), ["600519", "000001"]);
  assert.deepEqual(splitSymbolList(""), []);
  assert.deepEqual(splitSymbolList(null), []);
  assert.deepEqual(splitSymbolList(" , ， "), []);
});

test("joinSymbolList：与 split 互逆（往返稳定）", () => {
  assert.equal(joinSymbolList(["sh.600519", " SH.600036 "]), "SH.600519,SH.600036");
  assert.equal(joinSymbolList([]), "");
  assert.equal(joinSymbolList(null), "");
  const text = "SH.600519, sh.600519,SZ.000001";
  const once = joinSymbolList(splitSymbolList(text));
  assert.equal(once, "SH.600519,SZ.000001");
  assert.equal(joinSymbolList(splitSymbolList(once)), once);
});
