#!/usr/bin/env python
"""MCP 工具面成本报告（规格 FR-TOOLS-003 / §10 决策 3）：direct vs discovery 的真实数字。

**这是测量脚本，不是测试**：它按 ``QUANT_MCP_SURFACE`` 的两种取值各装配一次真应用
（离线：假 handle + 空调度器 + 空推送，路由表 / V3 接线 / MCP 注册全是真的），再走
**真 MCP 协议**（in-process ASGI + 真 lifespan）取 ``tools/list``，逐项量出：

* ``tools``    —— ``tools/list`` 的工具条数；
* ``chars``    —— ``json.dumps(result.tools)`` 的字符数（工具名 + 描述 + inputSchema 全量）；
* ``tokens_est`` —— 字符数 ÷ ``CHARS_PER_TOKEN`` 的估算（不是真实分词器：本仓库不引入
  tiktoken 之类依赖；中英混排按 3.0 字符/token 估，见下方常量注释）。

同一个进程里再量一次 discovery 模式 ``list_tools`` 的一页卡片（默认 20 张）字符数——
这是 discovery 模式下模型真正多花的那一次往返的返回体大小。

用法（只读，不碰线上 8397）：::

    cd platform && ~/.dsh/trading-venv/bin/python -B tools/mcp_surface_report.py
    # 加 --json 输出机器可读格式（测试/文档引用同一份口径）
"""
import argparse
import asyncio
import copy
import json
import sys
import tempfile
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from server import app as app_module  # noqa: E402
from server import mcp_discovery  # noqa: E402

#: 字符 → token 估算系数。中英混排经验值：英文 ~4.0 字符/token，中文 ~1.5 字符/token，
#: 工具描述是「中文散文 + 英文标识符」各半。**真实分词器不在本仓库依赖里**，所以给三档
#: 系数一起看（同一份字符数 → 区间），比值因此不受系数选择影响：
#:   * 4.0 —— 本脚本报的 ``tokens_est``。取这一档的理由是它与需求侧的实测口径一致
#:     （116 件 / 79,624 字符 ≈ 2 万 token：79624/4.0 = 19,906）；
#:   * 3.0 / 2.0 —— 偏中文的档位（token 更多、收益更大），作为上界一起给出。
#: 字符数是**硬数字**，token 是估算——任何取舍判断请以字符数为准。
CHARS_PER_TOKEN = 4.0
CHARS_PER_TOKEN_BANDS = (4.0, 3.0, 2.0)


class _DummyScheduler:
    def start(self):
        return None

    def stop(self):
        return None


class _DummyPush:
    quote_cache = None

    async def start(self):
        return False

    async def stop(self):
        return None


def fake_raw():
    return {
        "schedule": {"ok": True, "value": {"heartbeat": {"at": "2026-09-19"}, "jobs": [],
                                           "kill": False, "halt": False}},
        "snapshot": {"ok": True, "value": {"mode": "sim", "generated_at": "2026-09-19"}},
        "risk": {"ok": True, "value": {"config": {"singlePct": 2.0}}},
        "plan": {"ok": True, "value": {"plans": []}},
        "sources": {"ok": True, "value": {"channels": []}},
    }


def build_app(home, surface):
    """离线装配（与 tests/test_mcp_parity.py 同一手法），显式指定表面模式。"""
    raw = fake_raw()
    calls = []

    def fake_handle(endpoint, payload=None):
        calls.append((endpoint, dict(payload or {})))
        if endpoint not in raw:
            return {"ok": False, "error": {"code": "trading/unknown-endpoint",
                                           "message": endpoint}}
        return copy.deepcopy(raw[endpoint])

    patcher = unittest.mock.patch.object(app_module, "create_handler",
                                         lambda *args, **kwargs: fake_handle)
    patcher.start()
    try:
        return app_module.create_app(home=str(home), dist=str(home), config={},
                                     scheduler=_DummyScheduler(), futu=object(),
                                     push=_DummyPush(), mcp_surface=surface)
    finally:
        patcher.stop()


async def measure(client, surface):
    """走真协议量 tools/list；再量 discovery 模式的一页卡片。"""
    await client.initialize()
    tools = (await client.request("tools/list", {}))["tools"]
    blob = json.dumps(tools, ensure_ascii=False)
    out = {
        "surface": surface,
        "tools": len(tools),
        "chars": len(blob),
        "tokens_est": round(len(blob) / CHARS_PER_TOKEN),
        # 同一份字符数换几个系数一起看：**字符数是硬数字，token 是估算区间**。
        "tokens_bands": {f"{band:.1f}": round(len(blob) / band)
                         for band in CHARS_PER_TOKEN_BANDS},
    }
    if surface == mcp_discovery.DISCOVERY:
        names = {tool["name"] for tool in tools}
        for keyword in ("list_tools", "call_tool"):
            assert keyword in names, f"{surface} 模式缺入口工具 {keyword}"
        page = await client.call_raw("list_tools", {})
        card_blob = json.dumps(page, ensure_ascii=False)
        out["list_tools_page"] = {
            "cards": len(page.get("cards") or []),
            "total": page.get("total"),
            "chars": len(card_blob),
            "tokens_est": round(len(card_blob) / CHARS_PER_TOKEN),
        }
    return out


class RawClient:
    """最小 MCP 客户端（JSON-RPC over streamable-http，与 parity 测试同一手法）。"""

    def __init__(self, http):
        self.http = http
        self.session = None
        self.seq = 0

    async def _post(self, payload):
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session:
            headers["mcp-session-id"] = self.session
        response = await self.http.post("/mcp", json=payload, headers=headers)
        if "mcp-session-id" in response.headers:
            self.session = response.headers["mcp-session-id"]
        response.raise_for_status()
        return response.json()

    async def initialize(self):
        out = await self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18",
                                           "capabilities": {},
                                           "clientInfo": {"name": "surface-report",
                                                          "version": "1"}}})
        self.seq = 1
        await self.http.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                              "mcp-session-id": self.session},
                             json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return out["result"]

    async def request(self, method, params):
        self.seq += 1
        out = await self._post({"jsonrpc": "2.0", "id": self.seq, "method": method,
                                "params": params})
        if "error" in out:
            raise AssertionError(f"{method} 失败：{out['error']}")
        return out["result"]

    async def call_raw(self, name, arguments):
        result = await self.request("tools/call", {"name": name,
                                                   "arguments": arguments or {}})
        return json.loads(result["content"][0]["text"])


async def run(home):
    out = {}
    for surface in (mcp_discovery.DIRECT, mcp_discovery.DISCOVERY):
        app = build_app(home, surface)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8397") as http:
                out[surface] = await measure(RawClient(http), surface)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mcp-surface-report-") as home:
        report = asyncio.run(run(home))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    direct, discovery = report[mcp_discovery.DIRECT], report[mcp_discovery.DISCOVERY]
    saved_chars = direct["chars"] - discovery["chars"]
    print(f"MCP 工具面成本报告（字符是硬数字；token 是按系数的**估算区间**）")
    print(f"{'模式':<12}{'tools':>8}{'chars':>10}   tokens_est @ 4.0 / 3.0 / 2.0 字符每 token")
    for row in (direct, discovery):
        bands = row["tokens_bands"]
        print(f"{row['surface']:<12}{row['tools']:>8}{row['chars']:>10}"
              f"   {bands['4.0']:>6} /{bands['3.0']:>6} /{bands['2.0']:>6}")
    print(f"\n节省：{direct['tools'] - discovery['tools']} 件工具 / "
          f"{saved_chars} 字符（保留 {discovery['chars'] / direct['chars']:.2%}）/"
          f" ≈{round(saved_chars / 4.0)}–{round(saved_chars / 2.0)} token")
    page = discovery.get("list_tools_page")
    if page:
        print(f"discovery 多一次 list_tools 往返：返回一页 {page['cards']} 张卡片 / "
              f"{page['chars']} 字符 / ≈{page['tokens_est']} token"
              f"（命中 {page['total']} 件）")
        print(f"净收益：≈{round(saved_chars / CHARS_PER_TOKEN) - page['tokens_est']} token"
              f"（@3.0：省下的 schema − 一次检索页），代价是多 1 次往返")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
