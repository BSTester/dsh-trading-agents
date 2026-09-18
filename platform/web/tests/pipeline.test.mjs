// 流程页展示纯函数（WP10 任务 3）。宿主无关，node --test 直测。
// 实现在 services/pipeline.js（页面 pipeline.jsx 只做接线：React/antd 不进测试）。
// 断言锚点：服务端阶段形状 {label, status, at, scheduled, summary} 与状态集合
// {ok, pending, skipped, failed}（plugins/core/python/trading_core/pipeline.py
// _stage/_market_stages 与文件头「阶段状态口径」）；下钻 key 与 app.jsx PAGES 同集合。
import test from "node:test";
import assert from "node:assert/strict";
import {
  AUTO_PIPELINE_KEYS, MARKETS, POOL_KEY_DEFAULT, autoPipelineBadge, autoPipelineDraft,
  autoPipelinePayload, hhmmToTime, marketLabel, newStrategyRow, poolKeyOptions, stageDrill,
  stageEntries, stageStatus, stageStatusText, stageTagColor, stageTimeText, timeToHhmm,
} from "../src/services/pipeline.js";

test("stageStatus：四种服务端状态各归各位；未知/缺失回落 wait（不猜成功）", () => {
  assert.equal(stageStatus("ok"), "finish");
  assert.equal(stageStatus("pending"), "wait");
  assert.equal(stageStatus("skipped"), "wait");
  assert.equal(stageStatus("failed"), "error");
  assert.equal(stageStatus("running"), "wait");
  assert.equal(stageStatus(undefined), "wait");
  assert.equal(stageStatus(null), "wait");
});

test("stageTagColor：与调度页同一套三档语义色；未知一律 default", () => {
  assert.equal(stageTagColor("ok"), "green");
  assert.equal(stageTagColor("failed"), "red");
  assert.equal(stageTagColor("skipped"), "default");
  assert.equal(stageTagColor("pending"), "default");
  assert.equal(stageTagColor("whatever"), "default");
});

test("stageStatusText：未知状态原样回显状态码，空值给 —（不编词）", () => {
  assert.equal(stageStatusText("ok"), "已完成");
  assert.equal(stageStatusText("pending"), "待运行");
  assert.equal(stageStatusText("skipped"), "已跳过");
  assert.equal(stageStatusText("failed"), "失败");
  assert.equal(stageStatusText("weird"), "weird");
  assert.equal(stageStatusText(undefined), "—");
  assert.equal(stageStatusText(null), "—");
  assert.equal(stageStatusText(""), "—");
});

test("stageDrill：表驱动阶段去对应来源页，作业阶段回落调度页", () => {
  assert.equal(stageDrill("plan"), "#/plan");
  assert.equal(stageDrill("execute"), "#/execution");
  assert.equal(stageDrill("digest"), "#/audit");
  assert.equal(stageDrill("reconcile"), "#/audit");
  assert.equal(stageDrill("sync_bars"), "#/schedule");
  assert.equal(stageDrill("factors_snapshot"), "#/schedule");
  assert.equal(stageDrill(undefined), "#/schedule");
});

test("stageTimeText：at 优先于 scheduled；都缺给 —（不拿当前时间冒充）", () => {
  assert.equal(stageTimeText({ at: "16:00:05", scheduled: "16:00" }), "16:00:05");
  assert.equal(stageTimeText({ at: null, scheduled: "16:00" }), "计划 16:00");
  assert.equal(stageTimeText({ at: "", scheduled: "" }), "—");
  assert.equal(stageTimeText({}), "—");
  assert.equal(stageTimeText(null), "—");
});

test("stageEntries：保序（作业链顺序即服务端 dict 顺序，不重排）", () => {
  const stages = {
    sync_bars: { label: "行情同步", status: "ok", at: "16:00:05" },
    quality: { label: "数据质量", status: "pending" },
    factors_snapshot: { label: "因子快照", status: "skipped", summary: "关注池为空" },
  };
  assert.deepEqual(stageEntries(stages).map((row) => row.key),
    ["sync_bars", "quality", "factors_snapshot"]);
  assert.equal(stageEntries(stages)[0].label, "行情同步");
  assert.equal(stageEntries(stages)[2].summary, "关注池为空");
  assert.deepEqual(stageEntries(null), []);
  assert.deepEqual(stageEntries(undefined), []);
  assert.deepEqual(stageEntries("nope"), []);
});

test("marketLabel：三个市场有中文标签，未登记键原样回显", () => {
  assert.deepEqual(MARKETS, ["SH", "HK", "US"]);
  assert.equal(marketLabel("SH"), "A股 SH");
  assert.equal(marketLabel("HK"), "港股 HK");
  assert.equal(marketLabel("US"), "美股 US");
  assert.equal(marketLabel("SG"), "SG");
  assert.equal(marketLabel(undefined), "—");
});

test("autoPipelineBadge：非法配置显示「配置非法」而非「关闭」", () => {
  assert.deepEqual(autoPipelineBadge({ enabled: true }), { text: "自动执行已开启", color: "green" });
  assert.deepEqual(autoPipelineBadge({ enabled: false }), { text: "自动执行关闭", color: "default" });
  assert.deepEqual(autoPipelineBadge({ enabled: false, error: "exec_at 需为 HH:MM 格式" }),
    { text: "配置非法", color: "red" });
  // 文件级不可解析（JSON 坏）与语义非法同样可见：core 按空配置容错，但摘要带 config_error
  assert.deepEqual(autoPipelineBadge({ enabled: false, config_error: "配置文件无法解析" }),
    { text: "配置非法", color: "red" });
  assert.deepEqual(autoPipelineBadge(undefined), { text: "—", color: "default" });
});

test("autoPipelineDraft：只保留白名单键，只读字段（error/date）不带进草稿", () => {
  const draft = autoPipelineDraft({
    date: "2026-09-16", error: "不该带出去",
    enabled: true,
    strategies: [{ market: "HK", strategy: "watchlist_rsi" }],
    exec_at: { SH: "09:40" },
    exec_window_minutes: 15, reconcile_at: "19:30",
  });
  assert.deepEqual(Object.keys(draft).sort(), [...AUTO_PIPELINE_KEYS].sort());
  assert.equal(draft.enabled, true);
  assert.deepEqual(draft.strategies, [
    { market: "HK", strategy: "watchlist_rsi", watchlist: "watchlist" }]);
  // exec_at 缺失的市场补默认（服务端有效配置本就完整；此处兜住非法配置摘要形态）
  assert.deepEqual(draft.exec_at, { SH: "09:40", HK: "09:45", US: "22:35" });
  assert.equal(draft.exec_window_minutes, 15);
  assert.equal(draft.reconcile_at, "19:30");
});

test("autoPipelineDraft：空/损坏输入回落缺省（默认关闭，与调度侧同默认）", () => {
  assert.deepEqual(autoPipelineDraft(undefined), {
    enabled: false, strategies: [],
    exec_at: { SH: "09:35", HK: "09:45", US: "22:35" },
    exec_window_minutes: 30, reconcile_at: "19:00",
  });
  assert.deepEqual(autoPipelineDraft({ strategies: "nope", exec_window_minutes: true }),
    autoPipelineDraft({}));
});

test("autoPipelinePayload：键集恒等于白名单（草稿多余字段不漏给服务端）", () => {
  const payload = autoPipelinePayload({
    ...autoPipelineDraft({}), error: "不该带出去", date: "2026-09-16",
  });
  assert.deepEqual(Object.keys(payload).sort(), [...AUTO_PIPELINE_KEYS].sort());
  assert.equal("error" in payload, false);
  assert.equal("date" in payload, false);
  assert.equal(payload.enabled, false);
  assert.deepEqual(autoPipelinePayload(undefined), {
    enabled: undefined, strategies: undefined, exec_at: undefined,
    exec_window_minutes: undefined, reconcile_at: undefined,
  });
});

test("newStrategyRow：市场 + 策略 + 池键三字段", () => {
  assert.deepEqual(newStrategyRow(), {
    market: "SH", strategy: "watchlist_rsi", watchlist: "watchlist" });
});

// 2026-09-18 实机反馈：设置页的时刻字段是自由文本（HH:MM 靠手输），与「策略名靠手输」
// 同一个毛病。改成 TimePicker 后需要 dayjs ↔ 字符串的纯转换：页面只接线，转换在此直测。
// 口径与服务端 _hhmm 一致：HH:MM、00-23/00-59；**保存写回字符串**，dayjs 对象不入 payload。
test("hhmmToTime/timeToHhmm：HH:MM 往返稳定；非法值给 null/空串（不编造时刻）", () => {
  for (const text of ["00:00", "09:35", "09:45", "22:35", "19:00", "23:59"]) {
    assert.equal(timeToHhmm(hhmmToTime(text)), text);
  }
  // 补零口径：dayjs 格式化恒为两位（服务端要求 ^\d{2}:\d{2}$）
  assert.equal(timeToHhmm(hhmmToTime("9:05")), "09:05");
  // 非法/不可解析：null（TimePicker 显示占位，不假装有值）
  assert.equal(hhmmToTime("25:00"), null);
  assert.equal(hhmmToTime("09:60"), null);
  assert.equal(hhmmToTime("09:5"), null);
  assert.equal(hhmmToTime(""), null);
  assert.equal(hhmmToTime(null), null);
  assert.equal(hhmmToTime("nope"), null);
  // 非 dayjs 值：空串（不把 undefined/字符串塞进配置）
  assert.equal(timeToHhmm(null), "");
  assert.equal(timeToHhmm(undefined), "");
  assert.equal(timeToHhmm("09:35"), "");
});

test("poolKeyOptions：默认池恒在，其余取当前策略行用到的键（去重保序）", () => {
  assert.deepEqual(POOL_KEY_DEFAULT, "watchlist");
  assert.deepEqual(poolKeyOptions(undefined),
    [{ value: "watchlist", label: "watchlist（默认池）" }]);
  const options = poolKeyOptions({ strategies: [
    { watchlist: "watchlist" }, { watchlist: "hk_pool" },
    { watchlist: "hk_pool" }, { watchlist: "" }, { watchlist: 42 }, {},
  ] });
  assert.deepEqual(options.map((option) => option.value), ["watchlist", "hk_pool"]);
  assert.equal(options[1].label, "hk_pool");
});

test("autoPipelinePayload：时刻字段仍是 HH:MM 字符串（dayjs 对象不进载荷）", () => {
  const draft = autoPipelineDraft({
    enabled: true,
    exec_at: { SH: "09:35", HK: "09:45", US: "22:35" }, reconcile_at: "19:00",
  });
  // 模拟页面接线：draft 里是字符串 → TimePicker 取 dayjs → onChange 再转回字符串
  draft.exec_at.SH = timeToHhmm(hhmmToTime(draft.exec_at.SH));
  draft.reconcile_at = timeToHhmm(hhmmToTime(draft.reconcile_at));
  const payload = autoPipelinePayload(draft);
  assert.equal(payload.exec_at.SH, "09:35");
  assert.equal(payload.reconcile_at, "19:00");
  for (const value of [...Object.values(payload.exec_at), payload.reconcile_at]) {
    assert.equal(typeof value, "string");
    assert.match(value, /^\d{2}:\d{2}$/);
  }
});
