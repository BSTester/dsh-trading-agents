"""TTL 缓存与载荷形状校验（WP6 补遗 C）。

移植源（逐行为准）：
  * ``plugins/workbench/src/rpc.js:9-37`` —— CACHE_TTL_MS 常量表 + stableKey 稳定序列化；
  * ``rpc.js:73-95`` —— cached() 包装：命中（TTL 内且形状合法）→ cached:true，
    未命中 → 取数 → 形状校验 → 写缓存（仅 TTL>0）→ cached:false；
    形状不符抛「{endpoint} 返回的载荷不完整，已按失败处理」，一切异常落成失败信封；
  * ``plugins/workbench/src/endpoints.js:35-68`` —— ENDPOINT_SHAPE 表与 matchesShape。

有意差异：缓存是**进程内 dict**（Node 侧 rpc.js 用的也是内存 cache.js，语义一致）；
键与 payload 的稳定序列化照抄 rpc.js:34-37（键排序后成对序列化，键顺序不影响命中）。
"""
import json
import threading
import time
from datetime import datetime, timezone

# rpc.js:11-32 的 17 个端点 TTL（毫秒）
CACHE_TTL_MS = {
    "instrument": 10 * 60_000,
    "series": 10 * 60_000,
    "equity": 5 * 60_000,
    "positions": 5 * 60_000,
    "correlation": 30 * 60_000,
    "sensitivity": 60 * 60_000,
    "risk": 15 * 60_000,
    "trades": 5 * 60_000,
    "events": 60 * 60_000,
    "factors": 30 * 60_000,
    "ic": 30 * 60_000,
    "audit": 2 * 60_000,
    "sources": 5 * 60_000,
    "quality": 60 * 60_000,
    # WP4：调度态是低频变化的面板数据；心跳/作业给短 TTL，对账给中等 TTL
    "plan": 60_000,
    "schedule": 30_000,
    "reconcile": 5 * 60_000,
}

# endpoints.js:42-60 的端点最小字段（plan-execute 是动作端点，不进缓存形状表）
ENDPOINT_SHAPE = {
    "series": ["ticker", "bars"],
    "equity": ["mode", "points"],
    "positions": ["mode", "groups"],
    "correlation": ["matrix"],
    "sensitivity": ["ticker", "matrix"],
    "risk": ["config"],
    "trades": ["trades"],
    "events": ["ticker", "events"],
    "factors": ["tickers"],
    "ic": ["points"],
    "sources": ["sources"],
    "instrument": ["ticker"],
    "quality": ["ticker"],
    "plan": ["plans", "alerts"],
    "schedule": ["heartbeat", "jobs"],
    "reconcile": ["diffs", "tca"],
}

_CACHE = {}
_LOCK = threading.Lock()


def stable_key(payload):
    """rpc.js:34-37 stableKey：键排序后按 ``[[key, value], ...]`` 序列化。"""
    keys = sorted((payload or {}).keys())
    return json.dumps([[key, (payload or {})[key]] for key in keys],
                      ensure_ascii=False, separators=(",", ":"), default=str)


def matches_shape(endpoint, value):
    """endpoints.js:63-67 matchesShape：未知端点放行；缺字段/非对象一律 false。"""
    required = ENDPOINT_SHAPE.get(endpoint)
    if not required:
        return True
    if not isinstance(value, dict):
        return False
    return all(field in value for field in required)


def cache_key(endpoint, payload):
    """rpc.js:75：```${endpoint}|${stableKey(payload)}`。"""
    return f"{endpoint}|{stable_key(payload)}"


def read(endpoint, payload, ttl_ms, now=None):
    """命中返回 ``(at_ms, value)``，未命中返回 ``None``（rpc.js:76-83 的 cache.read）。"""
    if ttl_ms <= 0:
        return None
    current = time.time() * 1000 if now is None else now
    with _LOCK:
        entry = _CACHE.get(cache_key(endpoint, payload))
    if entry is None:
        return None
    if current - entry["at"] >= ttl_ms:  # rpc.js:64 ``now() - hit.at < CACHE_TTL_MS``
        return None
    return entry["at"], entry["value"]


def write(endpoint, payload, value, now=None):
    """写缓存并返回 ``{"at": ms}``（rpc.js:89 ``cache.write``）。"""
    at = time.time() * 1000 if now is None else now
    with _LOCK:
        _CACHE[cache_key(endpoint, payload)] = {"at": at, "value": value}
    return {"at": at}


def clear():
    """测试/诊断用：清空进程内缓存（Node 侧无对应 API，不影响契约）。"""
    with _LOCK:
        _CACHE.clear()


def iso_from_ms(at_ms):
    """``new Date(ms).toISOString()``：毫秒时间戳 → UTC ISO 串（rpc.js:81/90）。

    与 store_access._iso_now 同格式（毫秒精度、Z 结尾），此处自带实现是为了不跨模块
    依赖私有助手（本模块只依赖标准库）。
    """
    moment = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def cached(endpoint, payload, force, produce, error_code):
    """``rpc.js:73-95 cached()``：命中/取数/形状校验/失败信封，逐条对齐。

    返回四种形态之一：
      ``{"ok": True, "value": ..., "cached": True,  "cached_at": ISO}``
      ``{"ok": True, "value": ..., "cached": False, "cached_at": ISO}``
      ``{"ok": False, "error": {"code": error_code, "message": ≤300, "details": {}}}``
    形状不符按失败处理（消息与 rpc.js:87 完全一致），避免把取数失败缓存成「没有数据」。
    """
    ttl = CACHE_TTL_MS.get(endpoint, 0)
    if not force and ttl > 0:
        hit = read(endpoint, payload, ttl)
        if hit is not None:
            at, value = hit
            # 命中也要校验形状：坏条目当作未命中并重新取数（rpc.js:78-79）
            if matches_shape(endpoint, value):
                return {"ok": True, "value": value, "cached": True, "cached_at": iso_from_ms(at)}
    try:
        value = produce()
        if not matches_shape(endpoint, value):
            raise RuntimeError(f"{endpoint} 返回的载荷不完整，已按失败处理")
        entry = write(endpoint, payload, value) if ttl > 0 else {"at": time.time() * 1000}
        return {"ok": True, "value": value, "cached": False, "cached_at": iso_from_ms(entry["at"])}
    except Exception as error:  # noqa: BLE001 —— rpc.js:91 同样兜住任何异常
        message = str(error) if str(error) else error.__class__.__name__
        return {"ok": False, "error": {"code": error_code, "message": message[:300], "details": {}}}
