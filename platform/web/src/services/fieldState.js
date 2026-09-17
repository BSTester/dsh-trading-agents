// 加载态 vs 空态的**唯一判定**（E2E 取证：加载窗口把「未知」断言成事实）。
//
// 症状（浏览器定时采样，t=1s 与 t=3s 对比）：页面在请求尚未返回时就渲染
//   「调度器 无心跳记录」「数据源 —」「推送 —」「持仓 Top5 暂无持仓。」
// 三秒后全部自愈——即**把「还没读到」说成了「确实没有」**。事实口吻的空态文案
// 只有在一个确定性结论下才允许出现：请求已结束且结果为空。
//
// 本模块只做判定，不做渲染（纯函数，node --test 直测）；页面据 kind 选择文案。
export const LOADING_TEXT = "读取中…";
export const ERROR_TEXT = "读取失败";

/**
 * 判定一个取数结果处于哪种状态。
 *
 * state: useEndpoint 的返回（{loading, value, error}）
 * 返回 {kind, text}：kind ∈ loading | error | empty | value；text 仅前三者给出可渲染文案。
 * `isEmpty` 可覆盖「什么算空」（默认 undefined/null/"" 视为空）。
 */
export function fieldState(state, {
  loadingText = LOADING_TEXT,
  errorText = ERROR_TEXT,
  emptyText = "—",
  isEmpty = (value) => value === undefined || value === null || value === "",
} = {}) {
  const { loading, value, error } = state ?? {};
  if (loading) return { kind: "loading", text: loadingText };
  if (error) return { kind: "error", text: errorText };
  if (isEmpty(value)) return { kind: "empty", text: emptyText };
  return { kind: "value", text: null };
}

/** 阶段链的空态文案：加载中不得说「今日无该链阶段」（那是个事实断言）。 */
export function stageChainEmptyText(loading) {
  return loading ? LOADING_TEXT : "今日无该链阶段。";
}

/** 首启配置缺口提示 → 渲染行（服务端 config_warnings 的原样透传 + 可执行 hint）。 */
export function configWarningRows(value) {
  return (value?.config_warnings ?? []).map((warning) => ({
    code: warning?.code ?? "",
    message: warning?.message ?? "",
    hint: warning?.hint ?? "",
  }));
}
