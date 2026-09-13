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


def _session(headers, client_name):
    """复用会话（5 分钟内），减少握手开销。"""
    if _cache["session"] and time.time() - _cache["at"] < SESSION_TTL_SECONDS:
        return _cache["session"]
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
    if inner.get("ret_code") != 0:
        raise FutuUnavailable(f"{name}: ret={inner.get('ret_code')} {inner.get('ret_msg')}")
    return inner.get("data") or {}


def call_tool(name, arguments, timeout=30, client_name=DEFAULT_CLIENT_NAME):
    """调用一个富途 MCP 工具，返回 inner['data']；失败抛 FutuUnavailable。"""
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


# 探测用工具：无账户依赖、参数最少、返回稳定。
# 不能只做 initialize —— 实测 token 过期时 initialize 仍然成功，
# 而所有 tools/call 返回 "internal error"，旧探测因此长期误报「token 有效」。
PROBE_TOOL = "quote_trading_days"


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
        # 服务端返回 internal error 时无法区分 token 过期与服务端故障，
        # 两者都指向同一个动作：先续期，仍失败则稍后重试。
        return False, f"{message[:100]}（先尝试续期：scripts/futu_auth.py --refresh）"
    return True, "token 有效"
