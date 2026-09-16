// 情绪快照摘要派生纯函数（WP11 任务 3）。宿主无关，node --test 直测。
import test from "node:test";
import assert from "node:assert/strict";
import { sentimentRows, sentimentHeadline, SENTIMENT_SOURCES }
  from "../src/services/sentiment.js";

test("sentimentRows：三源固定顺序，缺席标 present=false 且 count=0", () => {
  const rows = sentimentRows({ sources: { fin_sentiment: 12 } });
  assert.deepEqual(rows.map((row) => row.source), [...SENTIMENT_SOURCES]);
  assert.deepEqual(rows[0], { source: "fin_sentiment", count: 12, present: true });
  assert.deepEqual(rows[1], { source: "fin_news", count: 0, present: false });
  assert.deepEqual(rows[2], { source: "last30days", count: 0, present: false });
});

test("sentimentRows：未知源如实追加在末尾，不静默丢弃", () => {
  const rows = sentimentRows({ sources: { fin_news: 3, extra_channel: 7 } });
  assert.deepEqual(rows.map((row) => row.source),
    ["fin_sentiment", "fin_news", "last30days", "extra_channel"]);
  assert.deepEqual(rows[3], { source: "extra_channel", count: 7, present: true });
});

test("sentimentRows：空/缺失摘要返回三源全缺席，不抛错", () => {
  for (const summary of [null, undefined, {}, { sources: {} }]) {
    const rows = sentimentRows(summary);
    assert.equal(rows.length, 3, JSON.stringify(summary));
    assert.ok(rows.every((row) => row.present === false && row.count === 0));
  }
});

test("sentimentHeadline：有记录报日期/条数/累计天数，无记录不给日期", () => {
  assert.equal(sentimentHeadline({ date: "2026-09-16", records: 42, symbols: 7, days: 2 }),
    "2026-09-16 采集 42 条，覆盖 7 个标的；已积累 2 天");
  assert.equal(sentimentHeadline({ date: "2026-09-16", records: 1, symbols: 1, days: 0 }),
    "2026-09-16 采集 1 条，覆盖 1 个标的", "累零天不写「已积累 0 天」");
  assert.equal(sentimentHeadline({ date: null, records: 0, symbols: 0, days: 0 }),
    "暂无情绪快照记录");
  assert.equal(sentimentHeadline(null), "暂无情绪快照记录");
  assert.equal(sentimentHeadline({}), "暂无情绪快照记录");
});
