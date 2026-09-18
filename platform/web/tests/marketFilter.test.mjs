// 全局市场维度纯函数（2026-09-18 用户需求：各页面按市场分类查看）。宿主无关，node --test 直测。
import test from "node:test";
import assert from "node:assert/strict";
import {
  MARKET_ALL, MARKET_CHOICES, normalizeMarketChoice, marketChainOf, symbolChain,
  isAllMarkets, filterGroups, filterBySymbol, filterByMarketField, groupByMarketChain,
  hasMultipleMarkets,
} from "../src/services/marketFilter.js";

test("marketChainOf：交易所前缀与富途 market_id 都归一到三个市场链", () => {
  // 交易所前缀：SZ/BJ 与 SH 同属 A 股链（与 planner.CALENDAR_MARKET 同口径）
  assert.equal(marketChainOf("SH"), "SH");
  assert.equal(marketChainOf("sz"), "SH");
  assert.equal(marketChainOf("BJ"), "SH");
  assert.equal(marketChainOf("HK"), "HK");
  assert.equal(marketChainOf("US"), "US");
  // 富途 market_id（实测 1=港股 3=A股 100=美股）
  assert.equal(marketChainOf(3), "SH");
  assert.equal(marketChainOf("3"), "SH");
  assert.equal(marketChainOf(1), "HK");
  assert.equal(marketChainOf(100), "US");
});

test("marketChainOf：非市场码与空值一律 null（不猜测、不硬塞进某个市场）", () => {
  // 实测 deals_today 的 groups[].market 会出现 '9'/'10'/'16' 这类账户类型码，不是市场
  for (const value of ["9", "10", "16", "", null, undefined, "FUTURES", 0]) {
    assert.equal(marketChainOf(value), null, `${String(value)} 不该被归到任何市场链`);
  }
});

test("symbolChain：从标的写法取市场链；无前缀/未知前缀返回 null", () => {
  assert.equal(symbolChain("SH.600519"), "SH");
  assert.equal(symbolChain("SZ.002475"), "SH");
  assert.equal(symbolChain("HK.00700"), "HK");
  assert.equal(symbolChain("US.AAPL"), "US");
  assert.equal(symbolChain("600519"), null);
  assert.equal(symbolChain("XX.0001"), null);
  assert.equal(symbolChain(null), null);
});

test("filterGroups：按分组 market 字段过滤；组无 market 只在「全部」下出现", () => {
  const groups = [{ market: "HK", acc_id: "9393" }, { market: "SH", acc_id: "3" },
                  { market: "9", acc_id: "fut" }, { acc_id: "unknown" }];
  assert.equal(filterGroups(groups, MARKET_ALL).length, 4, "全部市场不得丢任何组");
  assert.deepEqual(filterGroups(groups, "SH").map((g) => g.acc_id), ["3"]);
  assert.deepEqual(filterGroups(groups, "HK").map((g) => g.acc_id), ["9393"]);
  assert.deepEqual(filterGroups(groups, "US"), [], "没有美股账户时返回空而不是全部");
  // 数字 market_id 也能过滤（部分端点给的是 market_id）
  assert.deepEqual(filterGroups([{ market: 3, acc_id: "a" }], "SH").map((g) => g.acc_id), ["a"]);
});

test("filterBySymbol：按标的前缀过滤；SZ/BJ 归 A 股链；其它字段名可指定", () => {
  const rows = [{ symbol: "SH.600519" }, { symbol: "SZ.002475" }, { symbol: "HK.00700" },
                { symbol: "US.AAPL" }, { code: "600000" }];
  assert.equal(filterBySymbol(rows, MARKET_ALL).length, 5);
  assert.deepEqual(filterBySymbol(rows, "SH").map((r) => r.symbol), ["SH.600519", "SZ.002475"]);
  assert.deepEqual(filterBySymbol(rows, "US").map((r) => r.symbol), ["US.AAPL"]);
  assert.deepEqual(filterBySymbol(rows, "SH", "code"), [], "无前缀的 code 归不到市场链");
});

test("filterByMarketField：plan/schedule 这类自带 market 的行按市场过滤", () => {
  const rows = [{ market: "SH", plan_id: "p1" }, { market: "HK", plan_id: "p2" }];
  assert.equal(filterByMarketField(rows, MARKET_ALL).length, 2);
  assert.deepEqual(filterByMarketField(rows, "HK").map((r) => r.plan_id), ["p2"]);
});

test("groupByMarketChain：保序分段，归不到的行进 OTHER（不隐藏）", () => {
  const rows = [{ symbol: "HK.00700" }, { symbol: "SH.600519" }, { symbol: "US.AAPL" },
                { symbol: "600000" }, { symbol: "SH.600036" }];
  const grouped = groupByMarketChain(rows);
  assert.deepEqual(grouped.SH.map((r) => r.symbol), ["SH.600519", "SH.600036"]);
  assert.deepEqual(grouped.HK.map((r) => r.symbol), ["HK.00700"]);
  assert.deepEqual(grouped.US.map((r) => r.symbol), ["US.AAPL"]);
  assert.deepEqual(grouped.OTHER.map((r) => r.symbol), ["600000"], "归不到的行不能被丢掉");
  assert.equal(hasMultipleMarkets(rows), true);
  assert.equal(hasMultipleMarkets([{ symbol: "SH.600519" }]), false,
               "只有一个市场时有数据时分段是噪音");
});

test("normalizeMarketChoice：脏值/缺失一律回落「全部」，不抛错", () => {
  assert.equal(normalizeMarketChoice("US"), "US");
  assert.equal(normalizeMarketChoice(MARKET_ALL), MARKET_ALL);
  for (const bad of ["us", "沪深", "", null, undefined, 42]) {
    assert.equal(normalizeMarketChoice(bad), MARKET_ALL, `${String(bad)} 应回落全部`);
  }
  assert.equal(isAllMarkets(undefined), true);
  assert.equal(isAllMarkets("SH"), false);
  assert.deepEqual(MARKET_CHOICES.map((c) => c.value), [MARKET_ALL, "SH", "HK", "US"]);
});
