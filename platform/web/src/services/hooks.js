import React from "react";
import { callApi } from "./api.js";

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
