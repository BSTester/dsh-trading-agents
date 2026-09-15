// 模式切换载荷与徽章纯函数（规格 §4.5:1）。宿主无关，node --test 直测。
// 断言锚点：服务端 store_access.py:306-322 switch_mode 的字段名与口令门槛；
// 页面侧只是同一套规则的提前校验，服务端复核仍在（浏览器绕不过）。
import test from "node:test";
import assert from "node:assert/strict";
import { LIVE_CONFIRMATION, modeBadge, switchModeRequest } from "../src/services/mode.js";

test("sim 目标免口令：confirmation 可为 undefined，mode 为 target", () => {
  assert.deepEqual(switchModeRequest({ target: "sim", current: "live" }), {
    mode: "sim", expected_mode: "live", confirmation: undefined,
  });
  assert.deepEqual(switchModeRequest({ target: "sim", current: "sim", confirmation: "" }), {
    mode: "sim", expected_mode: "sim", confirmation: "",
  });
});

test("live 目标且当前非 live：无口令 / 错口令一律抛错且文案指向口令", () => {
  const expected = `请输入「${LIVE_CONFIRMATION}」；切换模式不等于授权下单`;
  assert.throws(() => switchModeRequest({ target: "live", current: "sim" }),
    (error) => error.message === expected);
  assert.throws(() => switchModeRequest({ target: "live", current: "sim", confirmation: "" }),
    (error) => error.message === expected);
  assert.throws(() => switchModeRequest({ target: "live", current: "sim", confirmation: "确认" }),
    (error) => error.message === expected);
  // 相近但不逐字的口令同样拒绝（含空格差异）
  assert.throws(() => switchModeRequest({ target: "live", current: "sim", confirmation: "确认实盘 " }),
    (error) => error.message === expected);
});

test("live 对口令成功，且 expected_mode 原样透传 current", () => {
  assert.deepEqual(
    switchModeRequest({ target: "live", current: "sim", confirmation: LIVE_CONFIRMATION }),
    { mode: "live", expected_mode: "sim", confirmation: LIVE_CONFIRMATION });
  // 已是 live 时重复切换不需要口令（服务端 current != "live" 条件同义）
  assert.deepEqual(switchModeRequest({ target: "live", current: "live" }),
    { mode: "live", expected_mode: "live", confirmation: undefined });
  // expected_mode 必须透传，不能被目标值覆盖
  assert.equal(switchModeRequest({ target: "live", current: "sim", confirmation: "确认实盘" })
    .expected_mode, "sim");
});

test("非法目标抛错：不在 sim/live 内一律拒绝", () => {
  for (const target of ["LIVE", "real", "", null, undefined, 1]) {
    assert.throws(() => switchModeRequest({ target, current: "sim" }),
      (error) => /未知目标模式/.test(error.message));
  }
});

test("modeBadge：live 红 / 其余绿，未知与缺失按 sim 展示", () => {
  assert.deepEqual(modeBadge("live"), { text: "实盘 LIVE", color: "red" });
  assert.deepEqual(modeBadge("sim"), { text: "模拟 SIM", color: "green" });
  assert.deepEqual(modeBadge(undefined), { text: "模拟 SIM", color: "green" });
  assert.deepEqual(modeBadge("其他"), { text: "模拟 SIM", color: "green" });
});
