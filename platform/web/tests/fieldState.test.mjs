// 加载态 vs 空态（E2E 取证：加载窗口把「未知」断言成事实）。
// 症状：概览页在请求尚未返回时渲染「调度器 无心跳记录」「数据源 —」「推送 —」
// 「暂无持仓。」，三秒后自愈——即把「还没读到」说成了「确实没有」。
import test from "node:test";
import assert from "node:assert/strict";

import { LOADING_TEXT, configWarningRows, fieldState, stageChainEmptyText }
  from "../src/services/fieldState.js";

test("loading 不得给出空态事实口吻", () => {
  for (const emptyText of ["—", "无心跳记录", "暂无持仓。"]) {
    const state = fieldState({ loading: true }, { emptyText });
    assert.equal(state.kind, "loading");
    assert.equal(state.text, LOADING_TEXT);
    assert.notEqual(state.text, emptyText);
  }
});

test("请求结束且为空 → 空态文案", () => {
  assert.deepEqual(fieldState({ loading: false, value: null }, { emptyText: "无心跳记录" }),
                   { kind: "empty", text: "无心跳记录" });
  assert.deepEqual(fieldState({ loading: false, value: undefined }, { emptyText: "—" }),
                   { kind: "empty", text: "—" });
});

test("错误优先于空态（读取失败 != 没有）", () => {
  const state = fieldState({ loading: false, error: "boom" }, { emptyText: "无心跳记录" });
  assert.equal(state.kind, "error");
  assert.notEqual(state.text, "无心跳记录");
});

test("有值 → kind=value，由页面自行渲染", () => {
  assert.deepEqual(fieldState({ loading: false, value: { summary: { ok: 6 } } }),
                   { kind: "value", text: null });
});

test("自定义 isEmpty：空对象也算空", () => {
  const isEmpty = (value) => !value || Object.keys(value).length === 0;
  assert.equal(fieldState({ loading: false, value: {} }, { isEmpty }).kind, "empty");
  assert.equal(fieldState({ loading: false, value: { a: 1 } }, { isEmpty }).kind, "value");
});

test("undefined state 不炸（hook 尚未初始化）", () => {
  assert.equal(fieldState(undefined).kind, "empty");
});

test("阶段链空态随 loading 分流", () => {
  assert.equal(stageChainEmptyText(true), LOADING_TEXT);
  assert.equal(stageChainEmptyText(false), "今日无该链阶段。");
});

test("config_warnings 原样透传并容忍缺字段", () => {
  const rows = configWarningRows({ config_warnings: [
    { code: "watchlist_empty", message: "关注池未配置", hint: "watchlist-init" },
    { code: "x" },
  ] });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].hint, "watchlist-init");
  assert.equal(rows[1].message, "");
  assert.deepEqual(configWarningRows({}), []);
  assert.deepEqual(configWarningRows(undefined), []);
});
