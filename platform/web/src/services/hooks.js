import React from "react";
import { callApi } from "./api.js";

/** endpoint + payload 驱动的取数 hook；refresh() 绕过客户端与服务端缓存。 */
export function useEndpoint(endpoint, payload, deps = []) {
  const [state, setState] = React.useState({ loading: true });
  const key = JSON.stringify(payload ?? {});
  React.useEffect(() => {
    let alive = true;
    setState((prev) => ({ ...prev, loading: true, error: undefined }));
    callApi(endpoint, payload ?? {}).then(
      (value) => { if (alive) setState({ loading: false, value }); },
      (error) => { if (alive) setState({ loading: false, error: String(error.message || error) }); });
    return () => { alive = false; };
  }, [...deps, endpoint, key]);
  const refresh = React.useCallback(() => {
    callApi(endpoint, payload ?? {}, { refresh: true }).then(
      (value) => setState({ loading: false, value }),
      (error) => setState({ loading: false, error: String(error.message || error) }));
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
