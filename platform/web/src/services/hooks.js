import React from "react";
import { callApi } from "./api.js";

/** endpoint + payload 驱动的取数 hook；refresh() 绕过客户端与服务端缓存。 */
export function useEndpoint(endpoint, payload, deps = []) {
  const [state, setState] = React.useState({ loading: true });
  // 审查修复：refresh 的 .then 无 alive 守卫，迟到的 refresh 响应会用旧 payload 覆盖新取数
  const aliveRef = React.useRef(true);
  const key = JSON.stringify(payload ?? {});
  React.useEffect(() => {
    let alive = true;
    aliveRef.current = true;
    setState((prev) => ({ ...prev, loading: true, error: undefined }));
    callApi(endpoint, payload ?? {}).then(
      (value) => { if (alive) setState({ loading: false, value }); },
      (error) => { if (alive) setState({ loading: false, error: String(error.message || error) }); });
    return () => { alive = false; aliveRef.current = false; };
  }, [...deps, endpoint, key]);
  const refresh = React.useCallback(() => {
    callApi(endpoint, payload ?? {}, { refresh: true }).then(
      (value) => { if (!aliveRef.current) return; setState({ loading: false, value }); },
      (error) => { if (!aliveRef.current) return; setState({ loading: false, error: String(error.message || error) }); });
  }, [endpoint, key]);
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
