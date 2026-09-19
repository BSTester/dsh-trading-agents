// V3 控制台取数层：GET /api/v3/*（与既有 POST /api/wb/* 并存，互不影响）。
// 鉴权沿用既有约定：localStorage.trading_token → Authorization: Bearer。
import React from "react";

const TOKEN_KEY = "trading_token";

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) ?? ""; } catch { return ""; }
}

export async function callV3(path, params = {}, { refresh = false } = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  if (refresh) query.set("_", String(Date.now()));
  const qs = query.toString();
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`/api/v3/${path}${qs ? `?${qs}` : ""}`, { headers });
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  if (response.status === 401) throw new Error("需要访问令牌：右上角「令牌」处填入服务配置的 token");
  if (!response.ok) throw new Error(body?.error?.message || `HTTP ${response.status}`);
  if (body && body.ok === false) throw new Error(body?.error?.message || "接口返回 ok=false");
  return body;
}

/** 写动作：POST /api/v3/<path>，body 为 JSON。返回解析后的信封（ok=false 不抛错，交由页面展示）。 */
export async function postV3(path, body = {}) {
  const headers = { "content-type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`/api/v3/${path}`, { method: "POST", headers, body: JSON.stringify(body ?? {}) });
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (response.status === 401) throw new Error("需要访问令牌：右上角「令牌」处填入服务配置的 token");
  if (!response.ok) throw new Error(payload?.error?.message || `HTTP ${response.status}`);
  return payload;
}

/** endpoint + params 驱动的取数 hook（与既有 useEndpoint 同形，便于页面共用写法）。 */
export function useV3(path, params = {}, deps = []) {
  const [state, setState] = React.useState({ loading: true });
  const seqRef = React.useRef(0);
  const skip = params === null;
  const key = skip ? null : JSON.stringify(params ?? {});
  React.useEffect(() => {
    seqRef.current += 1;
    if (skip) { setState({ loading: false }); return undefined; }
    const seq = seqRef.current;
    setState((prev) => ({ ...prev, loading: true, error: undefined }));
    callV3(path, params ?? {}).then(
      (value) => { if (seqRef.current === seq) setState({ loading: false, value }); },
      (error) => { if (seqRef.current === seq) setState({ loading: false, error: String(error.message || error) }); });
    return () => { seqRef.current += 1; };
  }, [...deps, path, key]);
  const refresh = React.useCallback(() => {
    if (skip) return;
    const seq = seqRef.current;
    callV3(path, params ?? {}, { refresh: true }).then(
      (value) => { if (seqRef.current === seq) setState({ loading: false, value }); },
      (error) => { if (seqRef.current === seq) setState({ loading: false, error: String(error.message || error) }); });
  }, [path, key, skip]);
  return { ...state, refresh };
}
