#!/usr/bin/env python3
"""富途 MCP 客户端（共享）：一次 initialize + 工具调用，带退避重试。

统一入口原则：**能用富途 MCP 拿到的数据优先走富途**（快、权威、覆盖全市场），
其他源（AKShare / Yahoo / X / Reddit）作为备用。

用法：
  from futu_client import call_tool, FutuUnavailable
  data = call_tool("quote_history_kline", {"symbol": "HK.00700", "ktype": 2, "num": 20, "end": "2026-09-13"})
"""
import json
import os
import time
import urllib.request
from pathlib import Path

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
BASE_URL = "https://mcp.futunn.com/mcp"


class FutuUnavailable(RuntimeError):
    """富途通道不可用（未授权/网络/服务端错误）——调用方应回退备用源。"""


def _headers():
    token_path = DSH / "futu-token"
    if not token_path.exists() or not token_path.read_text().strip():
        raise FutuUnavailable("缺少富途 token（未授权）")
    return {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token_path.read_text().strip()}"}


def _post(payload, session, headers, tries=4, timeout=30):
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


def _session(headers):
    """复用会话（5 分钟内），减少握手开销。"""
    if _cache["session"] and time.time() - _cache["at"] < 300:
        return _cache["session"]
    session, _ = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "workbench", "version": "1"}}},
                       None, headers)
    _cache["session"], _cache["at"] = session, time.time()
    return session


def call_tool(name, arguments, timeout=30):
    """调用一个富途 MCP 工具，返回 inner['data']；失败抛 FutuUnavailable。"""
    headers = _headers()
    session = _session(headers)
    try:
        _, body = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}},
                        session, headers, timeout=timeout)
    except FutuUnavailable:
        _cache["session"] = None  # 会话可能失效，下次重新握手
        raise
    data = json.loads(body)
    if "error" in data:
        raise FutuUnavailable(f"{name}: {data['error'].get('message', '')[:120]}")
    inner = json.loads(data["result"]["content"][0]["text"])
    if inner.get("ret_code") != 0:
        raise FutuUnavailable(f"{name}: ret={inner.get('ret_code')} {inner.get('ret_msg')}")
    return inner.get("data") or {}
