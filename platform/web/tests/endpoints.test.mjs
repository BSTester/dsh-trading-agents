// 声明端点预检纯函数（规格 §4.3）。宿主无关，node --test 直测。
// 实现在 services/endpoints.js（api.js 只做接线：浏览器 fetch/localStorage 不进测试）。
import test from "node:test";
import assert from "node:assert/strict";
import { declaredEndpoints, endpointMissing } from "../src/services/endpoints.js";

test("declaredEndpoints：取 snapshot.endpoints；缺失/非数组一律 null（不拦）", () => {
  assert.deepEqual(declaredEndpoints({ endpoints: ["snapshot", "switch-mode"] }),
    ["snapshot", "switch-mode"]);
  assert.deepEqual(declaredEndpoints({ endpoints: [] }), []);
  assert.equal(declaredEndpoints({}), null);
  assert.equal(declaredEndpoints({ endpoints: "snapshot" }), null);
  assert.equal(declaredEndpoints({ endpoints: null }), null);
  assert.equal(declaredEndpoints(null), null);
  assert.equal(declaredEndpoints(undefined), null);
});

test("声明集合缺失该端点时给出提示文案（含端点名与服务陈旧说明）", () => {
  const declared = ["snapshot", "series"];
  assert.equal(endpointMissing("switch-mode", declared),
    "服务未提供 switch-mode（服务版本陈旧，请重启服务后刷新）");
});

test("声明集合为 null（未声明集合）时不拦：返回 null", () => {
  assert.equal(endpointMissing("switch-mode", null), null);
  assert.equal(endpointMissing("switch-mode", undefined), null);
  assert.equal(endpointMissing("switch-mode", "switch-mode"), null);
  assert.equal(endpointMissing("switch-mode", declaredEndpoints({})), null);
});

test("已声明端点放行；空声明数组视为「什么都不提供」而非未知", () => {
  assert.equal(endpointMissing("switch-mode", ["switch-mode"]), null);
  assert.equal(endpointMissing("snapshot", ["snapshot", "switch-mode", "series"]), null);
  assert.equal(endpointMissing("snapshot", []),
    "服务未提供 snapshot（服务版本陈旧，请重启服务后刷新）");
});
