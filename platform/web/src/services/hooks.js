import React from "react";
import { callApi } from "./api.js";
import { symbolCandidatesFromPayloads } from "./symbols.js";

/** endpoint + payload 驱动的取数 hook；refresh() 绕过客户端与服务端缓存。 */
export function useEndpoint(endpoint, payload, deps = []) {
  const [state, setState] = React.useState({ loading: true });
  const seqRef = React.useRef(0);            // 取数代际：payload/endpoint 变化即换代
  // payload === null 表示「前置条件不满足，跳过请求」：不发调用，
  // state 归位为 { loading: false }（无 error 无 value）。
  // 这样页面不用拿空 payload 去撞服务端校验，也不必各自挡在渲染层。
  const skip = payload === null;
  const key = skip ? null : JSON.stringify(payload ?? {});
  React.useEffect(() => {
    seqRef.current += 1;                     // 换代：在途的旧响应一律作废
    if (skip) { setState({ loading: false }); return undefined; }
    const seq = seqRef.current;
    setState((prev) => ({ ...prev, loading: true, error: undefined }));
    callApi(endpoint, payload ?? {}).then(
      (value) => { if (seqRef.current === seq) setState({ loading: false, value }); },
      (error) => { if (seqRef.current === seq) setState({ loading: false, error: String(error.message || error) }); });
    return () => { seqRef.current += 1; };   // 卸载或 deps 变化都使在途响应作废
  }, [...deps, endpoint, key]);
  const refresh = React.useCallback(() => {
    if (skip) return;                        // 跳过态没有可刷新的数据
    const seq = seqRef.current;
    callApi(endpoint, payload ?? {}, { refresh: true }).then(
      (value) => { if (seqRef.current === seq) setState({ loading: false, value }); },
      (error) => { if (seqRef.current === seq) setState({ loading: false, error: String(error.message || error) }); });
  }, [endpoint, key, skip]);
  return { ...state, refresh };
}

/** snapshot 兜底轮询（60 秒）；unmount 停止。 */
export function useSnapshotPoll(intervalMs = 60_000) {
  const snapshot = useEndpoint("snapshot", {}, []);
  React.useEffect(() => {
    const timer = setInterval(() => snapshot.refresh(), intervalMs);
    return () => clearInterval(timer);
  }, [snapshot.refresh, intervalMs]);
  return snapshot;
}

// ---------------------------------------------------------------------------
// 标的联想候选（WP24）：平台关注池（snapshot.watchlist）+ 当前持仓（positions）
// ---------------------------------------------------------------------------
// 为什么共享一份而不是每个输入框各自 useEndpoint：
//   * snapshot 的客户端 TTL 是 0（api.js 的 TTL_MS）——每个输入框各自取数就是各自一次
//     POST；一个页面可能有两三个标的输入框（期权页 2 个、执行页多张卡片）；
//   * 候选不是页面数据，是**同一份**帮助信息，页面内的多个输入框不该各问一遍。
// 所以这里按模式只取一次，所有实例订阅同一份结果（React.useSyncExternalStore）。
// **不新增轮询**：取一次就够（关注池/持仓都是低频事实），刷新由调用方重挂载或显式传新
// mode 触发。模式（sim/live）由第一次取到的 snapshot 自己给出——positions 缺省 mode 是
// sim，直接不带 mode 会在实盘下查到模拟持仓，那是错的候选。
//
// 已知边界（如实写在这里）：切换 sim/live 后本缓存不会自动重取（新取数会打乱页面上正在
// 输入的内容），刷新页面即更新。候选只影响下拉里有什么，不影响任何提交语义。
const CANDIDATE_STORE = (() => {
  let key = null;                    // 已取数的模式；null = 尚未取数
  let generation = 0;                // 取数代际：旧响应一律作废
  let state = { loading: true, candidates: [], error: null };
  const listeners = new Set();

  function emit() {
    for (const listener of listeners) listener();
  }

  function load(mode) {
    const wanted = mode ?? "auto";
    if (key === wanted && !state.loading && !state.error) return;   // 已有同一份，不重复取
    key = wanted;
    generation += 1;
    const mine = generation;
    state = { loading: true, candidates: [], error: null };
    emit();
    callApi("snapshot", {}).then((snapshot) => {
      const resolved = mode ?? snapshot?.mode ?? "sim";
      // 持仓取不到（券商不可达/无权限）不是致命错：候选退化为只有关注池，
      // 页面自己的持仓视图会如实报错——这里不重复弹错、也不清空已有候选。
      return callApi("positions", { mode: resolved })
        .then((positions) => ({ snapshot, positions }), () => ({ snapshot, positions: null }));
    }).then((data) => {
      if (mine !== generation) return;
      state = { loading: false, error: null,
                candidates: symbolCandidatesFromPayloads(data.snapshot, data.positions) };
      emit();
    }, (error) => {
      if (mine !== generation) return;
      state = { loading: false, candidates: [], error: String(error?.message || error) };
      emit();
    });
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    getState() { return state; },
    load,
  };
})();

/** 候选读取入口：``const { candidates, loading, error } = useSymbolCandidates();`` */
export function useSymbolCandidates(mode) {
  const state = React.useSyncExternalStore(CANDIDATE_STORE.subscribe, CANDIDATE_STORE.getState);
  React.useEffect(() => { CANDIDATE_STORE.load(mode); }, [mode]);
  return state;
}
