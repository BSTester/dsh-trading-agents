// 设置页「策略」下拉的取值域与展示（WP22，2026-09-18 实机反馈）。
//
// 背景：设置页的自动流水线卡片里，「策略」原本是自由文本框（placeholder
// 「策略名，如 watchlist_rsi」）。但策略名**不是能靠记忆写对的字符串**——合法取值只有两类：
//
//   1. 内置策略 id：core ``trading_core/strategies.py`` 的 ``REGISTRY`` 模块级注册项；
//   2. ``rules`` 表里 ``status='enabled'`` 的 ``rule_id``（消费口径见
//      ``planner._resolve_strategy``：每次回查 DB 状态，非 enabled 一律拒绝消费）。
//
// 本模块把「可选什么」「为什么当前值不可用」算清楚。取值域的事实源在**服务端**
// （``settings_api.save_auto_pipeline`` 的写入侧校验，fail-closed），这里只是展示派生：
// 不新增、不放宽可选集合，也就不可能与服务端放行集漂移出「页面能选但服务端拒绝」。
//
// 漂移锁（tests/test_wp22_settings_options.py 正则解析本文件 + Python 常量比对，
// 不相信「记得同步」）：
//   * ``BUILTIN_STRATEGIES`` 的 id 集合 == core ``REGISTRY`` 的内置策略集合；
//   * ``selectable: false`` 的集合 == 缺 ``target_weights`` 的内置策略集合
//     （2026-09-18 实测：``plan_auto`` 对单标的策略抛
//     ``AttributeError: 'RsiStrategy' object has no attribute 'target_weights'``，
//     违反它自己的「永不抛」契约）。**这一条只影响下拉的可用性提示**：写入侧仍按
//     「内置策略 id 一律合法」放行——能力判定若也写进写入侧，就会多出第二份
//     「哪些策略能在自动路径跑」的登记表，两处迟早漂移。
import { statusLabel } from "./rules.js";

/** 内置策略清单（core ``strategies.REGISTRY`` 的前端镜像；顺序只影响下拉展示）。
 *
 * ``label`` = 中文口径（一句话说清「它怎么选股」），``note`` = 更细的说明（下拉 title）。
 * ``selectable: false`` = 自动流水线目前消费不了它（缺 ``target_weights``）——下拉里
 * **保留可见但禁止选择**，并写明原因；已配置的历史值仍原样回显（见 ``strategyFallbackOption``）。
 */
export const BUILTIN_STRATEGIES = [
  { id: "watchlist_rsi", label: "关注池 RSI 组合", note: "关注池内 RSI 超卖（<25）的标的等权买入，按风控上限（单票 ≤25%、最多 5 只）截断，其余留现金" },
  { id: "momentum_value_top5", label: "动量+价值前 5", note: "沪深 300 成分内按动量（60/20 日）与 EP 多因子打分，取前 5 名等权" },
  { id: "ma_cross", label: "双均线（单标的）", note: "5/20 日均线金叉买入、死叉卖出；自动流水线不支持（单标的策略没有 target_weights，选中它当日 build_plan 作业会失败）", selectable: false },
  { id: "rsi", label: "RSI（单标的）", note: "RSI(25/75) 超卖买入、超买卖出；自动流水线不支持（单标的策略没有 target_weights，选中它当日 build_plan 作业会失败）", selectable: false },
];

/** 该 id 是否内置策略（规则 id 不在其中——它们每次都要回查 DB 状态）。 */
export function isBuiltinStrategy(id) {
  return BUILTIN_STRATEGIES.some((item) => item.id === id);
}

const GROUP_BUILTIN = "内置策略";
const GROUP_RULES = "已批准规则";
const GROUP_CURRENT = "配置里的当前值（不可消费）";

/** 假设摘要：下拉里只放前 N 个字符（全文进 option.title，不丢信息）。 */
function summarize(text, limit = 40) {
  const full = typeof text === "string" ? text.trim() : "";
  if (!full) return "（无假设说明）";
  return full.length > limit ? `${full.slice(0, limit)}…` : full;
}

function builtinOption(item) {
  const blocked = item.selectable === false;
  return {
    value: item.id,
    label: blocked
      ? `${item.label}（${item.id}）｜自动流水线不支持`
      : `${item.label}（${item.id}）`,
    title: item.note,
    disabled: blocked,
  };
}

function ruleOption(rule) {
  return {
    value: rule.rule_id,
    label: `${rule.rule_id}（已批准规则）——${summarize(rule.hypothesis)}`,
    title: typeof rule.hypothesis === "string" ? rule.hypothesis : "",
    disabled: false,
  };
}

/**
 * 当前值不可用时的补位项（null = 正常选项里已有，不需要补位）。
 *
 * 存在的理由：**不能因为选择器里没有该项就把配置悄悄改掉**。配置里存着一个已停用规则名、
 * 或一个历史脏值时，下拉必须显示它并说明为什么不能用（而不是空着让用户以为「没配」，
 * 或在保存时被静默替换）。补位项 ``disabled: true`` 只挡「再选一次」，不影响回显。
 * @param {string} value 配置里的策略名
 * @param {Array<object>|undefined} rules ``rules`` 端点的规则清单（判状态用）
 * @returns {{value: string, label: string, title: string, disabled: boolean}|null}
 */
export function strategyFallbackOption(value, rules) {
  if (typeof value !== "string" || !value.trim()) return null;
  const id = value.trim();
  const builtin = BUILTIN_STRATEGIES.find((item) => item.id === id);
  if (builtin) {
    if (builtin.selectable !== false) return null; // 正常选项里已有
    return {
      value: id, disabled: true, title: builtin.note,
      label: `${builtin.label}（${id}）｜自动流水线不支持（单标的策略缺 target_weights）`,
    };
  }
  const rule = (Array.isArray(rules) ? rules : [])
    .find((item) => item && item.rule_id === id);
  if (rule && rule.status === "enabled") return null; // 正常选项里已有
  if (rule) {
    return {
      value: id, disabled: true,
      label: `${id}（${statusLabel(rule.status)}，不可消费：自动流水线只消费「已启用」的规则）`,
      title: typeof rule.hypothesis === "string" ? rule.hypothesis : "",
    };
  }
  return {
    value: id, disabled: true, title: "",
    label: `${id}（未知策略名：既不是内置策略，也不是已批准规则，保存会被服务端拒绝）`,
  };
}

/**
 * 下拉选项（antd Select 的分组结构）。
 *
 * 规则组**只收** ``status === 'enabled'``：candidate/validating/passed/failed/disabled
 * 都消费不了（``_resolve_strategy`` 一律拒绝），混进可选项等于埋雷（选了当天就软跳过，
 * 页面只说「没动静」）。与内置策略同名的规则不再重复列出——消费口径也是内置优先。
 * @param {Array<object>|undefined} rules ``rules`` 端点的规则清单
 * @param {string|string[]|undefined} current 配置里当前用到的策略名（用于补位不可用值）
 * @returns {Array<{label: string, options: Array<object>}>}
 */
export function strategyOptions(rules, current) {
  const list = Array.isArray(rules) ? rules : [];
  const groups = [{ label: GROUP_BUILTIN, options: BUILTIN_STRATEGIES.map(builtinOption) }];
  const approved = list.filter((rule) => rule && rule.status === "enabled"
    && typeof rule.rule_id === "string" && rule.rule_id.trim()
    && !isBuiltinStrategy(rule.rule_id));
  if (approved.length) {
    groups.push({ label: GROUP_RULES, options: approved.map(ruleOption) });
  }
  const currents = new Set((Array.isArray(current) ? current : [current])
    .filter((value) => typeof value === "string" && value.trim()).map((value) => value.trim()));
  const fallbacks = [...currents]
    .map((value) => strategyFallbackOption(value, list)).filter(Boolean);
  if (fallbacks.length) groups.push({ label: GROUP_CURRENT, options: fallbacks });
  return groups;
}
