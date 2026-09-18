// 设置页策略下拉的选项派生（WP22）。宿主无关，node --test 直测。
//
// 背景（实机反馈）：设置页「策略」原本是自由文本框（placeholder「策略名，如 watchlist_rsi」），
// 而合法取值只有两类：内置策略 id（core ``strategies.REGISTRY``）与 ``rules`` 表中
// ``status === "enabled"`` 的 ``rule_id``（消费口径见 planner._resolve_strategy：每次回查
// DB 状态，非 enabled 一律拒绝消费）。写错一个字母 → 保存成功 → 每天软跳过「策略未注册」。
//
// 本模块只做**展示派生**：把「可选什么」与「当前值为什么不可用」算清楚；取值域的事实源
// 在服务端（写入侧 fail-closed 校验），这里不新增、不放宽可选集合。
import test from "node:test";
import assert from "node:assert/strict";
import {
  BUILTIN_STRATEGIES, isBuiltinStrategy, strategyFallbackOption, strategyOptions,
} from "../src/services/strategies.js";

const flat = (groups) => groups.flatMap((group) => group.options ?? []);

test("BUILTIN_STRATEGIES：4 个内置策略，id 与中文标签、一句话说明齐备", () => {
  assert.deepEqual(BUILTIN_STRATEGIES.map((item) => item.id),
    ["watchlist_rsi", "momentum_value_top5", "ma_cross", "rsi"]);
  for (const item of BUILTIN_STRATEGIES) {
    assert.match(item.label, /[\u4e00-\u9fff]/, `${item.id} 缺中文标签`);
    assert.match(item.note, /[\u4e00-\u9fff]/, `${item.id} 缺中文说明`);
  }
  assert.equal(isBuiltinStrategy("rsi"), true);
  assert.equal(isBuiltinStrategy("rsi_typo"), false);
  // 单标的策略（缺 target_weights）在自动流水线上跑不起来：不可选 + 说明写在 note 里。
  // 跨语言锁在 tests/test_wp22_settings_options.py（与 core 的能力事实比对）。
  assert.equal(BUILTIN_STRATEGIES.find((item) => item.id === "rsi").selectable, false);
  assert.equal(BUILTIN_STRATEGIES.find((item) => item.id === "ma_cross").selectable, false);
  assert.equal(BUILTIN_STRATEGIES
    .find((item) => item.id === "watchlist_rsi").selectable, undefined);
});

test("strategyOptions：无规则时只列内置策略，标签含 id、说明进 title（可核对要存什么）", () => {
  const groups = strategyOptions(undefined);
  assert.deepEqual(groups.map((group) => group.label), ["内置策略"]);
  const options = groups[0].options;
  assert.deepEqual(options.map((option) => option.value),
    BUILTIN_STRATEGIES.map((item) => item.id));
  for (const option of options) {
    assert.ok(option.label.includes(`（${option.value}）`), option.label);
    assert.equal(option.title, BUILTIN_STRATEGIES
      .find((item) => item.id === option.value).note);
  }
  // 自动流水线消费不了的内置策略：可见（用户要能看出「有这个东西」）但禁止选择，并写明原因
  const byId = Object.fromEntries(options.map((option) => [option.value, option]));
  assert.equal(byId.rsi.disabled, true);
  assert.equal(byId.ma_cross.disabled, true);
  assert.ok(byId.rsi.label.includes("自动流水线不支持"), byId.rsi.label);
  assert.equal(byId.watchlist_rsi.disabled, false);
  assert.equal(byId.momentum_value_top5.disabled, false);
  // 空数组 / 畸形输入与 undefined 同形：不抛错、不放宽
  assert.deepEqual(strategyOptions([]), groups);
  assert.deepEqual(strategyOptions("nope"), groups);
  assert.deepEqual(strategyOptions([null, { status: "enabled" }]), groups);
});

test("strategyOptions：只收 status==='enabled' 的规则；candidate/failed/disabled 一个都不进", () => {
  const rules = [
    { rule_id: "r_candidate", status: "candidate", hypothesis: "候选假设" },
    { rule_id: "r_validating", status: "validating", hypothesis: "验证中假设" },
    { rule_id: "r_passed", status: "passed", hypothesis: "已通过但未批准" },
    { rule_id: "r_failed", status: "failed", hypothesis: "未通过假设" },
    { rule_id: "r_disabled", status: "disabled", hypothesis: "已停用假设" },
    { rule_id: "r_enabled", status: "enabled", hypothesis: "已批准规则的假设摘要" },
  ];
  const groups = strategyOptions(rules);
  assert.deepEqual(groups.map((group) => group.label), ["内置策略", "已批准规则"]);
  const values = flat(groups).map((option) => option.value);
  for (const rule of rules.filter((item) => item.status !== "enabled")) {
    assert.equal(values.includes(rule.rule_id), false, `${rule.rule_id} 不该出现在下拉里`);
  }
  const approved = groups[1].options[0];
  assert.equal(approved.value, "r_enabled");
  assert.ok(approved.label.includes("r_enabled"), approved.label);
  assert.ok(approved.label.includes("已批准规则"), approved.label);
  assert.ok(approved.label.includes("已批准规则的假设摘要"), approved.label);
  assert.equal(approved.title, "已批准规则的假设摘要");
});

test("strategyOptions：规则 id 与内置策略同名时不重复（消费口径也是内置优先）", () => {
  const groups = strategyOptions([{ rule_id: "rsi", status: "enabled", hypothesis: "同名" }]);
  assert.deepEqual(groups.map((group) => group.label), ["内置策略"]);
  const values = flat(groups).map((option) => option.value);
  assert.equal(values.filter((value) => value === "rsi").length, 1);
});

test("strategyFallbackOption：未知/未启用当前值标出来，已知值返回 null（不静默丢值）", () => {
  const rules = [
    { rule_id: "r_disabled", status: "disabled", hypothesis: "停用假设" },
    { rule_id: "r_enabled", status: "enabled", hypothesis: "已批准" },
  ];
  // 已停用规则：显示状态，标为不可消费（disabled=true 只挡点击，不影响回显当前值）
  const disabled = strategyFallbackOption("r_disabled", rules);
  assert.equal(disabled.value, "r_disabled");
  assert.ok(disabled.label.includes("已停用"), disabled.label);
  assert.equal(disabled.disabled, true);
  // 完全未知的名字：如实说「未知策略名」，不假装它是内置策略
  const unknown = strategyFallbackOption("wathclist_rsi", rules);
  assert.ok(unknown.label.includes("未知策略名"), unknown.label);
  assert.ok(unknown.label.includes("wathclist_rsi"), unknown.label);
  assert.equal(unknown.disabled, true);
  // 已知可用值：不需要补位（组合策略 selectable、已启用规则 enabled）
  assert.equal(strategyFallbackOption("watchlist_rsi", rules), null);
  assert.equal(strategyFallbackOption("r_enabled", rules), null);
  // 内置但自动流水线消费不了：同样要标出来（不让它看起来「正常可选」）
  const blocked = strategyFallbackOption("rsi", rules);
  assert.ok(blocked.label.includes("自动流水线不支持"), blocked.label);
  assert.equal(blocked.disabled, true);
  // 空值/畸形：不需要补位
  assert.equal(strategyFallbackOption("", rules), null);
  assert.equal(strategyFallbackOption(undefined, rules), null);
});

test("strategyOptions(rules, current)：不可用的当前值仍在下拉里（显示为不可消费，不被悄悄改掉）", () => {
  const rules = [
    { rule_id: "r_disabled", status: "disabled", hypothesis: "停用假设" },
    { rule_id: "r_enabled", status: "enabled", hypothesis: "已批准" },
  ];
  const groups = strategyOptions(rules, ["r_disabled", "ghost_name", "r_enabled"]);
  assert.deepEqual(groups.map((group) => group.label),
    ["内置策略", "已批准规则", "配置里的当前值（不可消费）"]);
  const fallbacks = groups[2].options.map((option) => option.value);
  assert.deepEqual(fallbacks, ["r_disabled", "ghost_name"]);
  // 可用值不进补位组
  assert.equal(fallbacks.includes("r_enabled"), false);
  assert.equal(fallbacks.includes("watchlist_rsi"), false);
  // 单标的策略（内置但自动流水线不支持）同样进补位组，附原因
  const blocked = strategyOptions(rules, ["rsi"])[2].options[0];
  assert.equal(blocked.value, "rsi");
  assert.ok(blocked.label.includes("自动流水线不支持"), blocked.label);
  // 单个字符串也接受
  assert.deepEqual(strategyOptions(rules, "ghost_name")[2].options.map((o) => o.value),
    ["ghost_name"]);
  // 全是可用值时没有补位组
  assert.deepEqual(strategyOptions(rules, "watchlist_rsi").map((group) => group.label),
    ["内置策略", "已批准规则"]);
});
