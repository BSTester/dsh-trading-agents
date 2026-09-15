// 待确认实盘操作卡片的纯逻辑（倒计时/按钮禁用/摘要行）。宿主无关，node --test 直测。
// 断言锚点：服务端 store_access.confirmation_view 的字段（id/expires_at/summary.fields）
// 与 CONFIRM_TTL_MS=120s 的 fail-closed 语义；页面侧只是同一规则的提前判定，
// 服务端 confirm-decide 仍会独立复核（编号不匹配/已超时一律拒绝）。
import test from "node:test";
import assert from "node:assert/strict";
import { CONFIRM_TTL_MS, decideDisabled, remainingSeconds, summaryLines }
  from "../src/services/confirm.js";

const NOW = Date.parse("2026-09-16T02:00:00.000Z");

test("120 秒 TTL 与服务端 store_access.CONFIRM_TTL_MS 同值", () => {
  assert.equal(CONFIRM_TTL_MS, 120_000);
});

test("未过期：剩余秒数为正且随时间递减", () => {
  const expires = "2026-09-16T02:02:00.000Z"; // NOW + 120s（整段 TTL）
  assert.equal(remainingSeconds(expires, NOW), 120);
  assert.equal(remainingSeconds(expires, NOW + 90_000), 30);
  // 半秒向上取整：卡片显示「还剩 1 秒」时请求仍可能被受理
  assert.equal(remainingSeconds(expires, NOW + 119_500), 1);
});

test("过期：剩余秒数夹到 0，绝不出现负数", () => {
  const expires = "2026-09-16T01:59:00.000Z"; // NOW - 60s
  assert.equal(remainingSeconds(expires, NOW), 0);
  assert.equal(remainingSeconds(expires, NOW + 3_600_000), 0);
});

test("非法/缺失到期时间：返回 null（未知）", () => {
  assert.equal(remainingSeconds(undefined, NOW), null);
  assert.equal(remainingSeconds(null, NOW), null);
  assert.equal(remainingSeconds("not-a-date", NOW), null);
});

test("按钮禁用：无待确认 / 缺编号 / 已过期 / 到期时间未知", () => {
  const pending = {
    id: "c1", expires_at: "2026-09-16T02:02:00.000Z",
    operation: "下单", tool: "trade_input_order",
  };
  assert.equal(decideDisabled(pending, NOW), false, "正常待确认可作答");
  assert.equal(decideDisabled(pending, NOW + 121_000), true, "过期禁用");
  assert.equal(decideDisabled(null, NOW), true, "无待确认禁用");
  assert.equal(decideDisabled({ ...pending, id: undefined }, NOW), true, "缺编号禁用");
  assert.equal(decideDisabled({ ...pending, id: "" }, NOW), true, "空编号禁用");
  assert.equal(decideDisabled({ ...pending, expires_at: "garbage" }, NOW), true,
    "到期时间未知按禁用处理（宁可不亮按钮，也不误导用户点一个必然失败的操作）");
});

test("摘要行：summary.fields 逐项渲染「标签：值」，缺失字段如实显示 —", () => {
  assert.deepEqual(
    summaryLines({
      fields: [
        { label: "账户", value: "A-9" },
        { label: "标的", value: "SH.600519" },
        { label: "方向", value: "买入（order_side=1）" },
        { label: "数量", value: "100" },
        { label: "价格", value: "123.5" },
      ],
    }),
    ["账户：A-9", "标的：SH.600519", "方向：买入（order_side=1）", "数量：100", "价格：123.5"]);
  assert.deepEqual(summaryLines({ fields: [] }), []);
  assert.deepEqual(summaryLines(null), []);
  assert.deepEqual(summaryLines({ fields: [{ label: "市场" }] }), ["市场：—"]);
});
