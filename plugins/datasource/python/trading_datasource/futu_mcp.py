#!/usr/bin/env python3
"""富途远程 MCP 客户端 —— **全仓库唯一**的一份实现。

统一入口原则：能用富途 MCP 拿到的数据优先走富途（权威、覆盖全市场），
其他源（AKShare / Yahoo / X / Reddit）作为备用。

此前有 4 处各自实现这段 JSON-RPC 握手（workbench/futu_client.py、
workbench/bars.py、engine/market_data.py、workbench/sources.py 探测），
富途侧任何变更都要改 4 遍。现在只此一份。

用法：
  from trading_datasource.futu_mcp import call_tool, FutuUnavailable
  data = call_tool("quote_history_kline",
                   {"symbol": "HK.00700", "ktype": 2, "num": 20, "end": "2026-09-13"})
"""
import json
import os
import threading
import time
from pathlib import Path

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
BASE_URL = "https://mcp.futunn.com/mcp"
PROTOCOL_VERSION = "2025-03-26"
DEFAULT_CLIENT_NAME = "trading-datasource"
SESSION_TTL_SECONDS = 300


class FutuUnavailable(RuntimeError):
    """富途通道不可用（未授权/网络/服务端错误）——调用方应回退备用源。"""


def token_path():
    return DSH / "futu-token"


def has_token():
    """是否已配置非空 token（不读取内容，供渠道状态页探测使用）。"""
    path = token_path()
    try:
        return path.exists() and bool(path.read_text().strip())
    except OSError:
        return False


def _headers():
    path = token_path()
    if not path.exists() or not path.read_text().strip():
        raise FutuUnavailable("缺少富途 token（未授权）")
    return {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {path.read_text().strip()}"}


def _post(payload, session, headers, tries=4, timeout=30):
    """带退避重试的 JSON-RPC POST（网络抖动常见）。"""
    import urllib.request  # 延迟导入：纯本地路径不应拉起 ssl/socket 栈
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(BASE_URL, data=json.dumps(payload).encode(),
                                             headers=headers, method="POST")
            if session:
                request.add_header("mcp-session-id", session)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.headers.get("mcp-session-id"), response.read().decode()
        except Exception as error:  # noqa: BLE001 - 网络层异常统一重试
            last = error
            time.sleep(1.5 * (attempt + 1))
    raise FutuUnavailable(f"MCP 请求失败：{str(last)[:140]}")


_cache = {"session": None, "at": 0.0}
# 会话建立必须串行：并发首次调用否则会各自 initialize，浪费往返甚至互相覆盖会话。
# 加锁后 call_tool 可安全地被线程池并发调用（批量读持仓时用得上）。
_session_lock = threading.Lock()


def _session(headers, client_name):
    """复用会话（5 分钟内），减少握手开销。线程安全。"""
    if _cache["session"] and time.time() - _cache["at"] < SESSION_TTL_SECONDS:
        return _cache["session"]
    with _session_lock:
        if _cache["session"] and time.time() - _cache["at"] < SESSION_TTL_SECONDS:
            return _cache["session"]  # 等锁期间别人已建立
        session, _ = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": {"name": client_name, "version": "1"}}},
                           None, headers)
        _cache["session"], _cache["at"] = session, time.time()
        return session


def reset_session():
    """丢弃缓存的会话（供测试或强制重新握手使用）。"""
    _cache["session"], _cache["at"] = None, 0.0


# 服务端的合法空结果：不是错误，也不是结构异常（实测 account_funds 无数据时返回 "no data"）。
EMPTY_RESULT_TEXTS = frozenset({"", "no data", "nodata", "null", "{}"})

# 服务端同时存在两种返回信封（实测）：
#   行情类 quote_*    {"ret_code": 0, "data": {...}}
#   账户类 account_*  {"s": "ok",    "d": {...}}
# 只认其中一种会把另一种的**成功响应**判成失败——实测 account_authorized_trd_accs
# 因此报 "ret=None None"，拿不到 acc_id，持仓必然读不出来。
RET_CODE_KEY = "ret_code"
STATUS_KEY = "s"
STATUS_OK_VALUES = frozenset({"ok", "success"})


def _unwrap(name, data):
    """从 JSON-RPC 响应中取出工具的业务数据。

    工具返回的 content[0].text 通常是 JSON，但空结果时是纯文本哨兵（如 "no data"）。
    """
    if "error" in data:
        raise FutuUnavailable(f"{name}: {data['error'].get('message', '')[:120]}")
    try:
        text = data["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as error:
        raise FutuUnavailable(f"{name}: 响应缺少 content（{str(error)[:60]}）") from error
    try:
        inner = json.loads(text)
    except (TypeError, ValueError):
        if str(text).strip().lower() in EMPTY_RESULT_TEXTS:
            return {}
        raise FutuUnavailable(f"{name}: 非结构化返回：{str(text)[:100]}")
    if not isinstance(inner, dict):
        raise FutuUnavailable(f"{name}: 返回不是对象：{str(inner)[:80]}")

    if STATUS_KEY in inner:  # 账户类信封 {"s": "ok", "d": {...}}
        status = inner[STATUS_KEY]
        if status is not None and str(status).lower() not in STATUS_OK_VALUES:
            detail = inner.get("m") or inner.get("msg") or inner.get("message") or ""
            raise FutuUnavailable(f"{name}: s={status} {str(detail)[:100]}")
        return inner.get("d") or {}

    if RET_CODE_KEY in inner:  # 行情类信封 {"ret_code": 0, "data": {...}}
        if inner[RET_CODE_KEY] != 0:
            raise FutuUnavailable(f"{name}: ret={inner[RET_CODE_KEY]} {inner.get('ret_msg')}")
        data = inner.get("data") or {}
        pagination = inner.get("pagination")  # 分页游标信封（实测成分股工具带此键）
        if pagination:  # 剥层时必须透出，否则调用方永远只见第一页；无此键返回形状不变
            data = {**data, "pagination": pagination}
        return data

    raise FutuUnavailable(f"{name}: 未识别的返回信封（键：{sorted(inner)[:6]}）")


def _call_once(name, arguments, timeout, client_name):
    headers = _headers()
    session = _session(headers, client_name)
    try:
        _, body = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}},
                        session, headers, timeout=timeout)
    except FutuUnavailable:
        reset_session()  # 会话可能失效，下次重新握手
        raise
    return _unwrap(name, json.loads(body))


def call_tool(name, arguments, timeout=30, client_name=DEFAULT_CLIENT_NAME, auto_refresh=True):
    """调用一个富途 MCP 工具，返回 inner['data']；失败抛 FutuUnavailable。

    access_token 只有 2 小时有效期，过期后服务端对所有工具返回 internal error。
    因此遇到该特征时先自动续期再重试一次——用户不需要每两小时重走一次授权页。
    """
    try:
        return _call_once(name, arguments, timeout, client_name)
    except FutuUnavailable as error:
        if not (auto_refresh and _looks_like_auth_failure(error)):
            raise
        if not refresh_access_token():
            raise
    return _call_once(name, arguments, timeout, client_name)


# 探测用工具：无账户依赖、参数最少、返回稳定。
# 不能只做 initialize —— 实测 token 过期时 initialize 仍然成功，
# 而所有 tools/call 返回 "internal error"，旧探测因此长期误报「token 有效」。
PROBE_TOOL = "quote_trading_days"


# ---- access token 有效期 ----
# 实测 OAuth 响应：access_token 的 expires_in = 7200（2 小时），且续期不会换发
# 新的 refresh_token。2 小时很短，因此必须能被自动续期，否则每隔两小时就会
# 出现「所有工具突然报 internal error」——服务端不返回 401，只给 internal error。
EXPIRY_FILE = DSH / "futu-token-expiry"
TOKEN_URL = "https://webapi.futunn.com/oauth2/token"
CLIENT_FILE = DSH / "futu-client-id"
REFRESH_FILE = DSH / "futu-refresh"


def record_expiry(expires_in):
    """记录 access_token 到期时刻（ISO 本地时间，仅供展示与预警）。"""
    if not expires_in:
        return None
    from datetime import datetime, timedelta
    moment = datetime.now() + timedelta(seconds=float(expires_in))
    try:
        DSH.mkdir(parents=True, exist_ok=True)
        EXPIRY_FILE.write_text(moment.isoformat(timespec="seconds"))
    except OSError:
        return moment
    return moment


def token_expiry():
    """返回 access_token 的到期时间；未知返回 None。"""
    from datetime import datetime
    try:
        return datetime.fromisoformat(EXPIRY_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def seconds_until_expiry():
    """距到期还剩多少秒；未知返回 None（未知不等于有效）。"""
    from datetime import datetime
    moment = token_expiry()
    return None if moment is None else (moment - datetime.now()).total_seconds()


def refresh_access_token(timeout=30):
    """用 refresh_token 换新 access_token 并落盘。成功返回 True。

    这是 2 小时过期问题的正解：持久化授权（refresh_token）仍然有效时，
    不需要用户重新点授权页。
    """
    import urllib.parse
    import urllib.request
    client_file, refresh_file = CLIENT_FILE, REFRESH_FILE
    if not (client_file.exists() and refresh_file.exists()):
        return False
    try:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_file.read_text().strip(),
            "client_id": client_file.read_text().strip(),
        }).encode()
        request = urllib.request.Request(TOKEN_URL, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except Exception:  # noqa: BLE001 - 续期失败交由调用方降级
        return False
    access = payload.get("access_token")
    if not access:
        return False
    try:
        token_path().write_text(access)
        token_path().chmod(0o600)
        if payload.get("refresh_token"):
            refresh_file.write_text(payload["refresh_token"])
    except OSError:
        return False
    record_expiry(payload.get("expires_in"))
    reset_session()
    return True


# access_token 的官方有效期（expires_in=7200）。仅在缺少到期记录时用于估算。
ACCESS_TOKEN_TTL_SECONDS = 7200


def estimated_remaining_seconds():
    """距到期还剩多少秒；没有到期记录时按 token 文件写入时间 + 官方 TTL 估算。"""
    recorded = seconds_until_expiry()
    if recorded is not None:
        return recorded
    try:
        age = time.time() - token_path().stat().st_mtime
    except OSError:
        return None
    return ACCESS_TOKEN_TTL_SECONDS - age


def ensure_fresh(within_seconds=1800, force=False):
    """距到期不足 within_seconds 时续期一次。返回 (refreshed, detail)。

    刻意**不无条件续期**：换发新 token 可能让仍被使用中的旧 token 失效，
    因此在确认"确实快过期"之前不动它。
    """
    remaining = None if force else estimated_remaining_seconds()
    if remaining is not None and remaining > within_seconds:
        return False, f"仍有效 {remaining / 60:.0f} 分钟，无需续期"
    if refresh_access_token():
        left = estimated_remaining_seconds()
        suffix = f"，新 token 剩余 {left / 60:.0f} 分钟" if left else ""
        return True, f"已续期（续期前剩余 {max(remaining or 0, 0) / 60:.0f} 分钟）{suffix}"
    return False, "续期失败：refresh_token 可能已失效，需重新完整授权"


def _looks_like_auth_failure(message):
    """服务端在凭证失效时返回 internal error（不是 401），因此按特征识别。"""
    lowered = str(message).lower()
    return "internal error" in lowered or "invalid_token" in lowered or "unauthorized" in lowered


def probe(timeout=20, client_name=DEFAULT_CLIENT_NAME):
    """真正调一次业务工具，判断富途通道是否可用。

    返回 (ok, detail)：
      True  —— 工具调用成功
      False —— 凭证/服务端问题
      None  —— 无法判定（缺 token、网络不可达）
    """
    if not has_token():
        return False, "未授权（无 token 文件）"
    from datetime import date, timedelta
    today = date.today()
    try:
        call_tool(PROBE_TOOL,
                  {"market": "US",
                   "start": (today - timedelta(days=7)).isoformat(),
                   "end": today.isoformat()},
                  timeout=timeout, client_name=client_name or DEFAULT_CLIENT_NAME)
    except FutuUnavailable as error:
        message = str(error)
        if "缺少富途 token" in message:
            return False, "未授权（无 token 文件）"
        if "MCP 请求失败" in message:
            return None, f"网络不可达：{message[:100]}"
        # internal error 无法区分凭证过期与服务端故障；两者都指向同一动作：
        # 先续期，仍失败则稍后重试。
        return False, f"{message[:100]}（可尝试续期：scripts/futu_auth.py --refresh）"
    remaining = seconds_until_expiry()
    if remaining is None:
        return True, "token 有效（到期时间未知）"
    if remaining <= 0:
        return False, "token 已过期（下次调用会自动续期）"
    hours = remaining / 3600
    return True, (f"token 有效 · 剩余约 {hours:.1f} 小时"
                  if hours >= 1 else f"token 有效 · 剩余约 {int(remaining / 60)} 分钟")
