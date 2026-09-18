// 因子快照统计口径（2026-09-19 字段审计：概览页「因子数 / 覆盖标的」恒显示 —）。
//
// 载荷形状来自对 127.0.0.1:8397 的真实响应（用例里的样本逐字取自该响应）：
//   {date, tickers:{标的:{因子键: 值}}, computed_at, note}
// 关键两点：**没有 factors 键**、tickers 是**对象**——首版按 .length 取数必然 undefined。
import test from "node:test";
import assert from "node:assert/strict";

import { factorSnapshotStats, factorTickerRows } from "../src/services/factorSnapshot.js";

const PAYLOAD = {
  date: "2026-09-18",
  computed_at: "2026-09-18T16:38:57",
  note: "registry=momentum_20,momentum_60,momentum_120,volatility_20,ep",
  tickers: {
    "SH.600000": { momentum_20: 0.0022, momentum_60: 0.0354, momentum_120: -0.0948, volatility_20: 0.2242, ep: null },
    "SH.600009": { momentum_20: -0.0105, momentum_60: -0.0224, momentum_120: -0.1812, volatility_20: 0.1455, ep: null },
  },
};

test("真实形状：覆盖标的 = tickers 键数，因子数 = 因子键并集", () => {
  const stats = factorSnapshotStats(PAYLOAD);
  assert.equal(stats.covered, 2);
  assert.equal(stats.factors, 5);
});

test("不同标的因子键不一致时按并集计数（不重复、不遗漏）", () => {
  const stats = factorSnapshotStats({
    tickers: {
      "SH.600000": { mom: 1, vol: 2 },
      "HK.00700": { mom: 1, ep: 3 },
    },
  });
  assert.equal(stats.covered, 2);
  assert.equal(stats.factors, 3);          // mom / vol / ep
});

test("缺失 / 异常形状一律 0（不抛异常、不编造）", () => {
  for (const payload of [undefined, null, {}, { tickers: null }, { tickers: 5 }]) {
    assert.deepEqual(factorSnapshotStats(payload), { covered: 0, factors: 0 });
  }
  assert.deepEqual(factorSnapshotStats({ tickers: { "SH.600000": null } }), { covered: 1, factors: 0 });
});

test("tickers 也接受数组形态（旧/替代形状），对象按值展开", () => {
  assert.equal(factorTickerRows({ tickers: [{ mom: 1 }, { vol: 2 }] }).length, 2);
  assert.equal(factorTickerRows({ tickers: { a: { mom: 1 } } }).length, 1);
  assert.deepEqual(factorTickerRows({}), []);
});
