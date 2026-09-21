// V3 工作台（Ant Design Pro 版）取数层。
//
// 两条通道与设计稿版完全一致，**不新增任何数据路径**：
//   * 读：GET  /api/v3/*        （v3Api：overview/metrics/market/strategy/risk/...）
//   * 写：POST /api/wb/<tool>   （既有受约束入口：switch-mode / plan-execute / confirm-decide /
//                                openapi_config / openapi_test / openapi_oauth / auto_pipeline）
// 鉴权沿用既有约定：localStorage.trading_token → Authorization: Bearer。
//
// 约定：所有取数都返回**原始信封**（{ok, ...} 或 {ok:false,error:{code,message}}），页面据此
// 显示真实数据或「无数据源 + 原因」，不做任何占位/估算。
import React from "react";

const TOKEN_KEY = "trading_token";

// 一次性链接带令牌（2026-09-21，局域网访问）：``…/?token=<t>`` —— 页面加载时把令牌收进
// localStorage，并**立刻**用 replaceState 从地址栏与当前历史条目里抹掉，避免它留在
// 浏览器历史 / 截图 / 复制分享里。之后所有取数走 Authorization 头（不再依赖 URL）。
// 服务端对 ``?token=`` 与 Cookie 同样放行（见 server/app.py::check_auth），并回
// ``Referrer-Policy: no-referrer`` 防止带令牌的地址经 Referer 外泄。
(function consumeUrlToken() {
  try {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get("token");
    if (!fromUrl) return;
    localStorage.setItem(TOKEN_KEY, fromUrl);
    url.searchParams.delete("token");
    const query = url.searchParams.toString();
    window.history.replaceState({}, "", `${url.pathname}${query ? `?${query}` : ""}${url.hash}`);
  } catch {
    /* 无 URL/localStorage 能力时按原样继续（页面会提示填令牌） */
  }
})();

function authHeaders(extra = {}) {
  const headers = { ...extra };
  try {
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) headers.Authorization = `Bearer ${token}`;
  } catch {
    /* localStorage 不可用时按未鉴权处理 */
  }
  return headers;
}

function buildQuery(params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `?${text}` : "";
}

async function parse(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (response.status === 401) throw new Error("需要访问令牌：右上角「令牌」填入服务配置的 token");
  if (!response.ok) throw new Error(body?.error?.message || `HTTP ${response.status}`);
  return body;
}

/** GET /api/v3/<path>?params → 信封（ok:false 不抛错，交由页面展示原因） */
export async function getV3(path, params = {}, { refresh = false } = {}) {
  const extra = refresh ? { _: String(Date.now()) } : {};
  const response = await fetch(`/api/v3/${path}${buildQuery({ ...params, ...extra })}`, {
    headers: authHeaders(),
  });
  return parse(response);
}

/** POST /api/wb/<tool>（空载荷=读状态，带载荷=写）→ 信封 */
export async function postWb(tool, payload = {}) {
  const response = await fetch(`/api/wb/${tool}`, {
    method: "POST",
    headers: authHeaders({ "content-type": "application/json" }),
    body: JSON.stringify(payload ?? {}),
  });
  return parse(response);
}

/** 取数 hook：{ value, loading, error, refresh }（与设计稿版 binder 的语义一致） */
export function useV3(path, params = {}, deps = []) {
  const [state, setState] = React.useState({ loading: true });
  const seqRef = React.useRef(0);
  const key = JSON.stringify(params ?? {});
  const run = React.useCallback(
    async (refresh = false) => {
      seqRef.current += 1;
      const seq = seqRef.current;
      setState((prev) => ({ ...prev, loading: true, error: undefined }));
      try {
        const value = await getV3(path, params ?? {}, { refresh });
        if (seqRef.current === seq) setState({ loading: false, value });
      } catch (error) {
        if (seqRef.current === seq) setState({ loading: false, error: String(error.message || error) });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [path, key],
  );
  React.useEffect(() => {
    run(false);
    return () => {
      seqRef.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, path, key]);
  return { ...state, refresh: () => run(true) };
}

/** POST /api/wb/* 的 hook（只在显式调用时发起，绝不自动写） */
export function useWbAction() {
  const [state, setState] = React.useState({ busy: null });
  const run = React.useCallback(async (tool, payload) => {
    setState({ busy: tool });
    try {
      const body = await postWb(tool, payload);
      setState({ busy: null });
      return body;
    } catch (error) {
      setState({ busy: null });
      return { ok: false, error: { code: "net", message: String(error.message || error) } };
    }
  }, []);
  return { ...state, run };
}

/* ── 格式化：与设计稿版口径一致（同一份约定的最小实现，避免两版显示口径漂移） ──
 *  注意：null / undefined / 空串 / 非数字一律返回「—」。**不能**直接 Number(value) 判定，
 *  因为 Number(null) === 0 会把「没有数据」显示成 0.00（真实缺陷，2026-09-20 由 overview 页发现）。
 */
const isNumber = (value) =>
  value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));

export const fmt = {
  isNumber,
  num(value, digits = 2) {
    return isNumber(value) ? Number(value).toFixed(digits) : "—";
  },
  money(value) {
    return isNumber(value)
      ? `¥${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`
      : "—";
  },
  signed(value, digits = 2, suffix = "") {
    return isNumber(value)
      ? `${Number(value) >= 0 ? "+" : "−"}${Math.abs(Number(value)).toFixed(digits)}${suffix}`
      : "—";
  },
  pct(value, digits = 2) {
    return isNumber(value) ? `${Number(value).toFixed(digits)}%` : "—";
  },
  stamp(value) {
    return value ? String(value).replace("T", " ").slice(0, 19) : "—";
  },
  dash(value) {
    return value === null || value === undefined || value === "" ? "—" : value;
  },
};

/** 无数据源区块的统一文案（页面用 <NoSource> 渲染，避免各页自造措辞） */
export function noSourceText(what, why) {
  return `${what}：无数据源${why ? ` · ${why}` : ""}`;
}
