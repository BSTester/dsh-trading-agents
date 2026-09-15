// 模式切换载荷构造与徽章文案（纯函数，无浏览器依赖，node --test 直测）。
// 载荷形状与口令门槛逐条对齐服务端 store_access.py:306-322 switch_mode：
//   {mode, expected_mode, confirmation?}；sim→live 需逐字口令「确认实盘」，
//   live→live / sim 目标不需要口令；成功响应的 order_authorized 恒 false。
// 只在此处校验并给出可读文案，服务端仍会独立复核一遍（浏览器绕不过服务端）。

/** sim→live 且当前非 live 时的逐字口令。 */
export const LIVE_CONFIRMATION = "确认实盘";

const MODES = ["sim", "live"];

/**
 * 构造 switch-mode 请求载荷；不合规直接抛错（页面据此 message.error，不发起请求）。
 * @param {{target: string, current: string, confirmation?: string}} input
 * @returns {{mode: string, expected_mode: string, confirmation?: string}}
 */
export function switchModeRequest({ target, current, confirmation } = {}) {
  if (!MODES.includes(target)) {
    throw new Error(`未知目标模式 ${target ?? "—"}（只支持 sim / live）`);
  }
  if (target === "live" && current !== "live" && confirmation !== LIVE_CONFIRMATION) {
    throw new Error(`请输入「${LIVE_CONFIRMATION}」；切换模式不等于授权下单`);
  }
  return { mode: target, expected_mode: current, confirmation };
}

/**
 * 页头徽章文案与颜色；未知/缺失模式按 sim 展示（服务端默认也是 sim）。
 * @param {string} mode
 * @returns {{text: string, color: string}}
 */
export function modeBadge(mode) {
  return mode === "live"
    ? { text: "实盘 LIVE", color: "red" }
    : { text: "模拟 SIM", color: "green" };
}
