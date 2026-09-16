"""WP8 任务 4：富途 WS 推送（行情 + 交易事件）——除 ``DSH_WP8_SLOW=1`` 冒烟外全部离线。

协议事实来源：``docs/superpowers/plans/2026-09-16-wp8-futu-openapi-unification.md``
附录 A（2026-09-16 实抓官方文档）。本文件逐条对齐的断言：

* **WS 签名原文**：``{timestamp_ms}\\n{nonce}\\nWEBSOCKET\\nws/auth``（**不是** REST 的
  五段原文）——``AppKeySigner.ws_signing_message`` 逐字节断言 + 真实私钥公钥验签
  （Ed25519 与 RSA-SHA256 各一）；
* 鉴权帧：appkey 的 auth_type/credential_id/authorization/timestamp_ms/nonce 逐字段；
  OAuth 的 ``auth_type:"oauth2"`` + ``Bearer <token>``；刷新帧与鉴权帧同构（仅 action
  不同，timestamp_ms/nonce 必须是新值）；
* 行情：**鉴权成功前不得订阅**、订阅/反订阅帧与幂等（重复订阅不重发）、应答 code!=0
  如实登记并在下一次 flush 重试、5 分钟 refresh（假时钟）、断线→重连→重新鉴权→按
  本地订阅意图表重订阅、**绝不发业务层心跳**；
* 交易：鉴权成功后被动接收（不发送任何订阅帧）、10 类事件 → OMS 白名单迁移/告警、
  乱序与未知类型不崩、重连回调触发对账、**断线期间事件不补发**；
* 对账兜底：重连后 REST 对账收敛（数量口径/状态子串，不解读未公开状态枚举）、
  差异只记录不猜、REST 失败不崩、``reconcile:push`` 进 reconcile 快照；
* 服务接线：lifespan 仅在 ``futu_channel=openapi`` 且凭据可用时启动两个客户端
  （否则零副作用）、``/healthz`` 的 ``push`` 字段形状、行情推送 → 进程内快照缓存
  供 ``rt_quote`` 命中并标注来源。

假连接（``FakeConnection``/``FakeTransport``）可脚本化「鉴权成功/失败、订阅应答、
推送帧、断线」；``now_ms``/``sleep`` 均可注入，测试不依赖真实网络与真实时间。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import futu_data, futu_push, trading  # noqa: E402
from trading_core import alerts as core_alerts  # noqa: E402
from trading_core import snapshots as core_snapshots  # noqa: E402
from trading_core import store as core_store  # noqa: E402
from trading_datasource import futu_openapi as fo  # noqa: E402

QUOTE_URL = "wss://webapi-quote.futunn.com/ws"
TRADE_URL = "wss://webapi-trade.futunn.com/ws"
TRADE_EVENTS = ("EVENT_NEW", "EVENT_REPLACED", "EVENT_CANCELED", "EVENT_EXPIRED",
                "EVENT_FILL", "EVENT_NEW_REJECTED", "EVENT_REPLACE_REJECTED",
                "EVENT_CANCEL_REJECTED", "EVENT_FILL_CORRECT", "EVENT_FILL_CANCEL")


# ---------------------------------------------------------------------------
# 假连接 / 假传输（可脚本化「鉴权成功/失败、订阅应答、推送帧、断线」）
# ---------------------------------------------------------------------------
class FakeConnection:
    """注入式假连接：``sent`` 记录出站帧，``inbound`` 队列脚本化入站帧。

    ``drop()`` 让后续 ``recv()`` 抛 ``PushDisconnected``（模拟服务端断开）；
    也可直接 ``push(PushDisconnected(...))``。
    """

    def __init__(self):
        self.inbound = asyncio.Queue()
        self.sent = []
        self.closed = False
        self._dropped = False

    async def send(self, data):
        self.sent.append(data if isinstance(data, dict) else json.loads(data))

    async def recv(self):
        if self._dropped and self.inbound.empty():
            raise futu_push.PushDisconnected("假连接：测试断线")
        item = await self.inbound.get()
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, str) else json.dumps(item)

    async def close(self):
        self.closed = True

    def push(self, frame):
        self.inbound.put_nowait(frame if isinstance(frame, str)
                                else json.dumps(frame))

    def drop(self):
        self._dropped = True
        self.inbound.put_nowait(futu_push.PushDisconnected("假连接：测试断线"))

    def actions(self):
        return [frame.get("action") for frame in self.sent]

    async def wait_sent(self, count, timeout=2.0):
        await wait_for(lambda: len(self.sent) >= count, timeout,
                       f"{count} 帧出站（已发 {len(self.sent)}：{self.actions()}）")


class _ConnCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        await self.conn.close()
        return False


class FakeTransport:
    """可注入传输：每次调用返回下一个脚本化连接；耗尽后继续给空连接（记录调用）。"""

    def __init__(self, *connections):
        self.connections = list(connections)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": dict(kwargs)})
        if self.connections:
            conn = self.connections.pop(0)
        else:
            conn = FakeConnection()
        return _ConnCtx(conn)


class UrlTransport:
    """按 URL 分配假连接（行情与交易各一条链），耗尽后给空连接。"""

    def __init__(self, mapping=None):
        self.mapping = {url: list(conns) for url, conns in (mapping or {}).items()}
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": dict(kwargs)})
        conns = self.mapping.setdefault(url, [])
        return _ConnCtx(conns.pop(0) if conns else FakeConnection())

    def url_calls(self, url):
        return [call for call in self.calls if call["url"] == url]


async def wait_for(predicate, timeout=2.0, label="条件"):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"等待{label}超时")
        await asyncio.sleep(0.002)


def _pem(key):
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class KeyedTemp:
    """临时目录 + Ed25519 私钥 PEM（appkey 凭据 fixture）。"""

    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.key = Ed25519PrivateKey.generate()
        self.pem = self.path / "appkey.pem"
        self.pem.write_bytes(_pem(self.key))
        self.cred = {"mode": "appkey", "app_key": "AK-TEST-0001",
                     "private_key_path": str(self.pem), "algorithm": "Ed25519"}


def oauth_cred(token="tok-1"):
    return {"mode": "oauth", "client_id": "cid", "access_token": token}


class Clock:
    """假时钟：每次调用 +step 毫秒（帧里的 timestamp_ms 因此可逐帧断言）。"""

    def __init__(self, start=1_700_000_000_000, step=1000):
        self.value = start
        self.step = step
        self.calls = 0

    def __call__(self):
        self.calls += 1
        current = self.value
        self.value += self.step
        return current


class FakeSleeper:
    """假 sleep：记录退避时长，立即返回（重连测试不真等）。"""

    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)
        await asyncio.sleep(0)


def auth_ack(session_id="sess-1", server_time=1_700_000_000_000_000):
    return {"id": 1, "session_id": session_id, "server_time": server_time}


# ---------------------------------------------------------------------------
# 1. WS 签名（对比官方示例原文 + 真实私钥验签）
# ---------------------------------------------------------------------------
class WsSignatureTest(unittest.TestCase):
    def test_ws_signing_message_is_protocol_exact(self):
        """原文 = ``{timestamp_ms}\\n{nonce}\\nWEBSOCKET\\nws/auth``（四段）。"""
        message = fo.AppKeySigner.ws_signing_message(1_700_000_000_000, "nonce-abc")
        self.assertEqual(message, b"1700000000000\nnonce-abc\nWEBSOCKET\nws/auth")
        self.assertEqual(len(message.decode("utf-8").split("\n")), 4)

    def test_ws_signing_message_differs_from_rest_five_part_message(self):
        """不得复用 REST 五段原文（同一 ts/nonce 下两者必不相等）。"""
        ts, nonce = 1_700_000_000_000, "nonce-abc"
        rest = fo.AppKeySigner.signing_message(ts, "POST", "/x", "", b"{}")
        self.assertNotEqual(rest, fo.AppKeySigner.ws_signing_message(ts, nonce))
        self.assertNotIn(b"WEBSOCKET", rest)

    def test_sign_ws_ed25519_verifies_with_public_key(self):
        key = Ed25519PrivateKey.generate()
        signer = fo.AppKeySigner(key, "Ed25519")
        ts, nonce = 1_700_000_000_000, "nonce-ed"
        signature = signer.sign_ws(ts, nonce)
        self.assertIsInstance(signature, str)
        self.assertNotIn("Bearer", signature)
        key.public_key().verify(
            fo.base64.b64decode(signature), signer.ws_signing_message(ts, nonce))

    def test_sign_ws_rsa_sha256_verifies_with_public_key(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signer = fo.AppKeySigner(key, "RSA-SHA256")
        ts, nonce = 1_700_000_000_000, "nonce-rsa"
        signature = signer.sign_ws(ts, nonce)
        key.public_key().verify(
            fo.base64.b64decode(signature), signer.ws_signing_message(ts, nonce),
            fo.padding.PKCS1v15(), fo.hashes.SHA256())

    def test_sign_ws_uses_its_own_message_not_rest_sign(self):
        """同一个 signer：sign_ws 与 sign 对同一 ts/nonce 给出不同签名。"""
        key = Ed25519PrivateKey.generate()
        signer = fo.AppKeySigner(key, "Ed25519")
        self.assertNotEqual(signer.sign_ws(123, "n"), signer.sign(123, "GET", "/a", "", b""))


# ---------------------------------------------------------------------------
# 2. 鉴权/刷新帧（appkey 与 oauth 两种）
# ---------------------------------------------------------------------------
class AuthFrameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = KeyedTemp(self)
        self.clock = Clock()

    def test_appkey_auth_frame_fields(self):
        ts, nonce = 1_700_000_000_000, "nonce-1"
        frame = futu_push.auth_frame(self.tmp.cred, ts, nonce)
        self.assertEqual(frame["action"], "auth")
        self.assertNotIn("id", frame, "附录 A 的鉴权帧不带 id")
        data = frame["data"]
        self.assertEqual(data["auth_type"], "appkey")
        self.assertEqual(data["credential_id"], "AK-TEST-0001")
        self.assertEqual(data["timestamp_ms"], ts)
        self.assertEqual(data["nonce"], nonce)
        self.tmp.key.public_key().verify(
            fo.base64.b64decode(data["authorization"]),
            fo.AppKeySigner.ws_signing_message(ts, nonce))

    def test_oauth_auth_frame_uses_bearer_token(self):
        frame = futu_push.auth_frame(oauth_cred("tok-9"), 1_700_000_000_000, "nonce-o")
        data = frame["data"]
        self.assertEqual(data["auth_type"], "oauth2")
        self.assertEqual(data["authorization"], "Bearer tok-9")
        self.assertNotIn("credential_id", data)

    def test_refresh_frame_is_isomorphic_with_new_timestamp_and_nonce(self):
        auth = futu_push.auth_frame(self.tmp.cred, 1_700_000_000_000, "nonce-a")
        refresh = futu_push.refresh_frame(self.tmp.cred, 1_700_000_300_000, "nonce-b")
        self.assertEqual(refresh["action"], "refresh")
        self.assertEqual(set(refresh["data"]), set(auth["data"]),
                         "刷新帧与鉴权帧字段同构（附录 A）")
        self.assertNotEqual(refresh["data"]["timestamp_ms"], auth["data"]["timestamp_ms"])
        self.assertNotEqual(refresh["data"]["nonce"], auth["data"]["nonce"])

    def test_auth_frame_rejects_unknown_credential_mode(self):
        with self.assertRaises(futu_push.PushAuthError):
            futu_push.auth_frame({"mode": "nope"}, 1, "n")
        with self.assertRaises(futu_push.PushAuthError):
            futu_push.auth_frame({"mode": "oauth", "access_token": ""}, 1, "n")

    def test_refresh_interval_defaults_to_five_minutes_under_ten_minute_cap(self):
        self.assertEqual(futu_push.DEFAULT_REFRESH_SECONDS, 300.0)
        self.assertLess(futu_push.DEFAULT_REFRESH_SECONDS,
                        futu_push.MAX_REFRESH_SECONDS)
        self.assertEqual(futu_push.MAX_REFRESH_SECONDS, 600.0)

    def test_client_rejects_refresh_interval_over_protocol_cap(self):
        with self.assertRaises(ValueError):
            futu_push.QuotePushClient(QUOTE_URL, oauth_cred(), on_message=lambda _f: None,
                                      transport=FakeTransport(), refresh_interval=601.0)
        with self.assertRaises(ValueError):
            futu_push.QuotePushClient(QUOTE_URL, oauth_cred(), on_message=lambda _f: None,
                                      transport=FakeTransport(), refresh_interval=0)


# ---------------------------------------------------------------------------
# 3. 行情推送客户端
# ---------------------------------------------------------------------------
class QuotePushClientTest(unittest.TestCase):
    def make(self, conn, *, on_message=None, cred=None, **kwargs):
        kwargs.setdefault("now_ms", Clock())
        kwargs.setdefault("sleep", FakeSleeper())
        kwargs.setdefault("auth_timeout", 0.5)
        client = futu_push.QuotePushClient(
            QUOTE_URL, cred or oauth_cred(), on_message=on_message or (lambda _f: None),
            transport=FakeTransport(conn), **kwargs)
        return client

    def test_no_subscribe_before_auth_success(self):
        async def case():
            conn = FakeConnection()
            messages = []
            client = self.make(conn, on_message=messages.append)
            await client.start()
            await conn.wait_sent(1)
            self.assertEqual(conn.sent[0]["action"], "auth", "首帧必须是鉴权帧")
            client.subscribe({"quote": ["HK.00700"]})
            await asyncio.sleep(0.02)
            self.assertEqual(conn.actions(), ["auth"], "鉴权成功前不得订阅")
            conn.push(auth_ack())
            await conn.wait_sent(2)
            self.assertEqual(conn.sent[1]["action"], "subscribe")
            self.assertEqual(conn.sent[1]["quote"], ["HK.00700"])
            self.assertTrue(client.status()["authenticated"])
            await client.stop()

        asyncio.run(case())

    def test_subscribe_is_idempotent_and_unsubscribe_sends_only_diff(self):
        async def case():
            conn = FakeConnection()
            client = self.make(conn)
            await client.start()
            await conn.wait_sent(1)
            client.subscribe({"quote": ["HK.00700"], "order_book": ["HK.00700"]})
            conn.push(auth_ack())
            await conn.wait_sent(2)
            # 服务端确认订阅（code=0）→ 已确认集合建立
            conn.push({"id": conn.sent[1]["id"], "code": 0, "message": ""})
            await asyncio.sleep(0.02)
            # 重复订阅同样的意图：不得重发
            client.subscribe({"quote": ["HK.00700"], "order_book": ["HK.00700"]})
            await asyncio.sleep(0.05)
            self.assertEqual(conn.actions(), ["auth", "subscribe"])
            # 追加一个被订阅通道的新标的：只发增量
            client.subscribe({"quote": ["HK.00001"]})
            await conn.wait_sent(3)
            self.assertEqual(conn.sent[2]["quote"], ["HK.00001"])
            self.assertNotIn("order_book", conn.sent[2])
            conn.push({"id": conn.sent[2]["id"], "code": 0, "message": ""})
            await asyncio.sleep(0.02)
            # 反订阅：只发被移除的那个
            client.unsubscribe({"quote": ["HK.00001"]})
            await conn.wait_sent(4)
            self.assertEqual(conn.sent[3]["action"], "unsubscribe")
            self.assertEqual(conn.sent[3]["quote"], ["HK.00001"])
            self.assertEqual(client.snapshot_intent()["quote"], ["HK.00700"])
            await client.stop()

        asyncio.run(case())

    def test_subscribe_ack_error_surfaced_and_retried_on_next_flush(self):
        async def case():
            conn = FakeConnection()
            client = self.make(conn, refresh_interval=0.05)
            await client.start()
            await conn.wait_sent(1)
            conn.push(auth_ack())
            client.subscribe({"quote": ["HK.00700"]})
            await conn.wait_sent(2)
            ok_id = conn.sent[1]["id"]
            conn.push({"id": ok_id, "code": -1, "message": "unknown symbol"})
            await wait_for(lambda: client.status()["last_error"] is not None, 2.0,
                           "订阅失败登记")
            self.assertIn("unknown symbol", client.status()["last_error"])
            # 未确认的订阅在下一次 flush（refresh 周期）重试，意图不丢
            await wait_for(lambda: len(conn.sent) >= 3, 2.0, "订阅重试")
            self.assertEqual(conn.sent[2]["action"], "subscribe")
            self.assertEqual(conn.sent[2]["quote"], ["HK.00700"])
            await client.stop()

        asyncio.run(case())

    def test_refresh_frame_sent_on_interval_with_fresh_timestamp_and_nonce(self):
        async def case():
            conn = FakeConnection()
            client = self.make(conn, refresh_interval=0.05)
            await client.start()
            await conn.wait_sent(1)
            auth_frame = conn.sent[0]
            conn.push(auth_ack())
            await wait_for(lambda: len(conn.sent) >= 2, 2.0, "刷新帧")
            refresh = conn.sent[1]
            self.assertEqual(refresh["action"], "refresh")
            self.assertNotEqual(refresh["data"]["timestamp_ms"],
                                auth_frame["data"]["timestamp_ms"])
            self.assertNotEqual(refresh["data"]["nonce"], auth_frame["data"]["nonce"])
            self.assertEqual(set(refresh["data"]), set(auth_frame["data"]))
            await client.stop()

        asyncio.run(case())

    def test_disconnect_reconnects_reauthenticates_and_resubscribes(self):
        async def case():
            first, second = FakeConnection(), FakeConnection()
            transport = FakeTransport(first, second)
            sleeper = FakeSleeper()
            client = futu_push.QuotePushClient(
                QUOTE_URL, oauth_cred(), on_message=lambda _f: None, transport=transport,
                now_ms=Clock(), sleep=sleeper, auth_timeout=0.5)
            client.subscribe({"quote": ["HK.00700"], "kline": [
                {"symbol": "HK.00700", "period": "1m", "adjust": "none"}]})
            await client.start()
            await first.wait_sent(1)
            first.push(auth_ack("sess-1"))
            await first.wait_sent(2)
            first.drop()
            await wait_for(lambda: len(transport.calls) == 2, 2.0, "重连")
            await second.wait_sent(1)
            self.assertEqual(second.sent[0]["action"], "auth", "重连后必须重新鉴权")
            second.push(auth_ack("sess-2"))
            await second.wait_sent(2)
            self.assertEqual(second.sent[1]["action"], "subscribe")
            self.assertEqual(second.sent[1]["quote"], ["HK.00700"],
                             "按本地订阅意图表重新订阅")
            self.assertEqual(second.sent[1]["kline"][0]["symbol"], "HK.00700")
            self.assertGreaterEqual(client.status()["reconnects"], 1)
            self.assertEqual(sleeper.delays[0], 1.0, "指数退避首个延迟")
            await client.stop()

        asyncio.run(case())

    def test_never_sends_business_heartbeat_frame(self):
        async def case():
            conn = FakeConnection()
            client = self.make(conn, refresh_interval=0.05)
            await client.start()
            await conn.wait_sent(1)
            conn.push(auth_ack())
            await wait_for(lambda: len(conn.sent) >= 2, 2.0, "刷新帧")
            actions = conn.actions()
            self.assertNotIn("heartbeat", actions)
            self.assertTrue(all(frame.get("action") != "heartbeat" for frame in conn.sent))
            self.assertIn("refresh", actions)
            await client.stop()

        asyncio.run(case())

    def test_auth_failure_records_error_and_never_subscribes(self):
        async def case():
            conn = FakeConnection()
            conn.push({"code": 401, "message": "invalid signature"})
            client = self.make(conn, max_auth_failures=1)
            client.subscribe({"quote": ["HK.00700"]})
            await client.start()
            await wait_for(lambda: not client.status()["started"], 2.0, "鉴权失败后停止重连")
            self.assertIn("invalid signature", client.status()["last_error"])
            self.assertNotIn("subscribe", conn.actions())
            self.assertFalse(client.status()["authenticated"])
            await client.stop()

        asyncio.run(case())


# ---------------------------------------------------------------------------
# 4. 交易推送客户端
# ---------------------------------------------------------------------------
class TradePushClientTest(unittest.TestCase):
    def test_ten_event_types_forwarded_passively_without_subscribe(self):
        async def case():
            conn = FakeConnection()
            events = []
            client = futu_push.TradePushClient(
                TRADE_URL, oauth_cred(), on_event=events.append,
                transport=FakeTransport(conn), now_ms=Clock(), sleep=FakeSleeper(),
                auth_timeout=0.5)
            await client.start()
            await conn.wait_sent(1)
            conn.push(auth_ack())
            for index, event in enumerate(TRADE_EVENTS):
                conn.push({"event_type": event, "order_id": f"B{index}"})
            await wait_for(lambda: len(events) == len(TRADE_EVENTS), 2.0, "10 类事件")
            self.assertEqual([frame["event_type"] for frame in events], list(TRADE_EVENTS))
            self.assertEqual(conn.actions(), ["auth"],
                             "交易 WS 鉴权成功后自动订阅全部事件，客户端不发订阅帧")
            await client.stop()

        asyncio.run(case())

    def test_reconnect_calls_on_reconnect_and_does_not_replay_missed_events(self):
        async def case():
            first, second = FakeConnection(), FakeConnection()
            transport = FakeTransport(first, second)
            events, reconnects = [], []
            client = futu_push.TradePushClient(
                TRADE_URL, oauth_cred(), on_event=events.append, transport=transport,
                on_reconnect=reconnects.append, now_ms=Clock(), sleep=FakeSleeper(),
                auth_timeout=0.5)
            await client.start()
            await first.wait_sent(1)
            first.push(auth_ack("sess-1"))
            first.push({"event_type": "EVENT_NEW", "order_id": "B1"})
            await wait_for(lambda: len(events) == 1, 2.0, "首个事件")
            first.drop()
            await wait_for(lambda: len(transport.calls) == 2, 2.0, "重连")
            await second.wait_sent(1)
            second.push(auth_ack("sess-2"))
            await wait_for(lambda: len(reconnects) == 1, 2.0, "重连回调")
            # 断线期间的事件不补发：重连后不会凭空出现
            await asyncio.sleep(0.05)
            self.assertEqual(len(events), 1, "重连不假设补发（对账由 on_reconnect 触发）")
            self.assertEqual(reconnects[0]["session_id"], "sess-2")
            self.assertGreaterEqual(client.status()["reconnects"], 1)
            await client.stop()

        asyncio.run(case())


# ---------------------------------------------------------------------------
# 5. 交易事件 → OMS 迁移 + 告警
# ---------------------------------------------------------------------------
class EventBridgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.bridge = trading.TradeEventBridge(str(self.home))

    def seed(self, cid, status, broker="B1", qty=100, side="BUY", symbol="HK.00700"):
        conn = self._conn()
        now = "2026-09-16 10:00:00"
        conn.execute(
            "INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,status,"
            "broker_order_id,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, "", symbol, symbol.split(".")[0], side, qty, 1.0, status, broker, "live",
             now, now))
        conn.commit()
        conn.close()

    def _conn(self):
        return core_store.connect(core_store.db_path(str(self.home)))

    def status_of(self, cid):
        conn = self._conn()
        try:
            return conn.execute("SELECT status, broker_order_id, qty FROM orders"
                                " WHERE client_order_id=?", (cid,)).fetchone()["status"]
        finally:
            conn.close()

    def alerts(self):
        conn = self._conn()
        try:
            return core_alerts.list_recent(conn, limit=50)
        finally:
            conn.close()

    def fills(self, cid):
        conn = self._conn()
        try:
            return core_store.fills_by_order(conn, cid)
        finally:
            conn.close()


class TradeEventBridgeTest(EventBridgeBase):
    def test_event_new_moves_submitting_order_to_submitted(self):
        self.seed("c-new", "submitting", broker="B-new")
        result = self.bridge.handle({"event_type": "EVENT_NEW", "order_id": "B-new"})
        self.assertTrue(result["applied"])
        self.assertEqual(self.status_of("c-new"), "submitted")

    def test_event_fill_quantity_decides_partial_or_filled(self):
        self.seed("c-fill", "submitted", broker="B-fill", qty=100)
        self.bridge.handle({"event_type": "EVENT_FILL", "order_id": "B-fill",
                            "dealt_qty": 40, "qty": 100})
        self.assertEqual(self.status_of("c-fill"), "partial")
        self.bridge.handle({"event_type": "EVENT_FILL", "order_id": "B-fill",
                            "dealt_qty": 100, "qty": 100})
        self.assertEqual(self.status_of("c-fill"), "filled")

    def test_event_fill_without_quantity_is_conservatively_partial(self):
        self.seed("c-fill2", "submitted", broker="B-fill2", qty=100)
        self.bridge.handle({"event_type": "EVENT_FILL", "order_id": "B-fill2"})
        self.assertEqual(self.status_of("c-fill2"), "partial")

    def test_event_canceled_and_expired_are_terminal_cancelled(self):
        self.seed("c-can", "submitted", broker="B-can")
        self.bridge.handle({"event_type": "EVENT_CANCELED", "order_id": "B-can"})
        self.assertEqual(self.status_of("c-can"), "cancelled")
        self.seed("c-exp", "submitted", broker="B-exp")
        self.bridge.handle({"event_type": "EVENT_EXPIRED", "order_id": "B-exp"})
        self.assertEqual(self.status_of("c-exp"), "cancelled")

    def test_event_new_rejected_moves_submitting_order_to_rejected_with_alert(self):
        self.seed("c-rej", "submitting", broker="B-rej")
        result = self.bridge.handle({"event_type": "EVENT_NEW_REJECTED", "order_id": "B-rej",
                                    "reason": "资金不足"})
        self.assertTrue(result["applied"])
        self.assertEqual(self.status_of("c-rej"), "rejected")
        levels = [row["level"] for row in self.alerts()]
        self.assertIn("warn", levels)

    def test_replace_and_cancel_rejected_alert_without_guessing_state(self):
        for event in ("EVENT_REPLACE_REJECTED", "EVENT_CANCEL_REJECTED"):
            cid = f"c-{event.lower()}"
            broker = f"B-{event}"
            self.seed(cid, "submitted", broker=broker)
            result = self.bridge.handle({"event_type": event, "order_id": broker})
            self.assertFalse(result["applied"])
            self.assertEqual(self.status_of(cid), "submitted", "保守处理：状态不动")
            self.assertIn("warn", [row["level"] for row in self.alerts()])

    def test_fill_correct_and_fill_cancel_are_warned_and_do_not_downgrade_state(self):
        self.seed("c-corr", "filled", broker="B-corr")
        for event in ("EVENT_FILL_CORRECT", "EVENT_FILL_CANCEL"):
            result = self.bridge.handle({"event_type": event, "order_id": "B-corr"})
            self.assertFalse(result["applied"])
            self.assertEqual(self.status_of("c-corr"), "filled")
        self.assertGreaterEqual(
            len([row for row in self.alerts() if row["level"] == "warn"]), 2)

    def test_event_replaced_keeps_state_but_is_recorded(self):
        self.seed("c-rep", "submitted", broker="B-rep")
        result = self.bridge.handle({"event_type": "EVENT_REPLACED", "order_id": "B-rep"})
        self.assertFalse(result["applied"])
        self.assertEqual(self.status_of("c-rep"), "submitted")
        self.assertEqual(result["reason"], "state-unchanged")

    def test_out_of_order_event_is_alerted_not_crashed(self):
        """乱序：CANCELED 先到（订单仍 submitting）→ 白名单拒绝，记录告警不崩。"""
        self.seed("c-ooo", "submitting", broker="B-ooo")
        result = self.bridge.handle({"event_type": "EVENT_CANCELED", "order_id": "B-ooo"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "illegal-transition")
        self.assertEqual(self.status_of("c-ooo"), "submitting")
        self.assertIn("warn", [row["level"] for row in self.alerts()])

    def test_unknown_event_type_counted_without_crash(self):
        result = self.bridge.handle({"event_type": "EVENT_FUTURE_THING", "order_id": "B1"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "unknown-event")
        self.assertEqual(self.bridge.status()["unknown_events"], 1)
        self.assertEqual(self.bridge.status()["received"], 1)

    def test_unmatched_order_id_alerts_without_crash(self):
        result = self.bridge.handle({"event_type": "EVENT_FILL", "order_id": "NOPE"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "order-not-found")
        self.assertIn("warn", [row["level"] for row in self.alerts()])

    def test_all_ten_event_types_are_handled_without_exception(self):
        for index, event in enumerate(TRADE_EVENTS):
            cid = f"c-all-{index}"
            broker = f"B-all-{index}"
            self.seed(cid, "submitted", broker=broker)
            result = self.bridge.handle({"event_type": event, "order_id": broker})
            self.assertIsInstance(result, dict)
            self.assertEqual(result["event"], event)
        self.assertEqual(self.bridge.status()["received"], len(TRADE_EVENTS))
        self.assertEqual(self.bridge.status()["errors"], 0)

    def test_fill_event_records_deduplicated_fill_row(self):
        self.seed("c-fills", "submitted", broker="B-fills", qty=100)
        event = {"event_type": "EVENT_FILL", "order_id": "B-fills", "fill_id": "F1",
                 "price": "1.5", "qty": 100, "dealt_qty": 100,
                 "traded_at": "2026-09-16 10:00:01"}
        self.bridge.handle(event)
        self.bridge.handle(event)
        rows = self.fills("c-fills")
        self.assertEqual(len(rows), 1, "同一 fill_id 重复推送不重复落成交")
        self.assertEqual(rows[0]["price"], 1.5)
        self.assertEqual(self.status_of("c-fills"), "filled")


class TradeEventWiringTest(EventBridgeBase):
    """端到端：交易 WS 收帧 → on_event 回调 → TradeEventBridge → OMS（真实链路装配）。"""

    def test_trade_client_event_reaches_oms_bridge(self):
        self.seed("c-wire", "submitted", broker="B-wire")
        bridge = trading.TradeEventBridge(str(self.home))

        async def case():
            conn = FakeConnection()
            client = futu_push.TradePushClient(
                TRADE_URL, oauth_cred(), on_event=bridge.handle,
                transport=FakeTransport(conn), now_ms=Clock(), sleep=FakeSleeper(),
                auth_timeout=0.5)
            await client.start()
            await conn.wait_sent(1)
            conn.push(auth_ack())
            conn.push({"event_type": "EVENT_CANCELED", "order_id": "B-wire"})
            await wait_for(lambda: bridge.status()["applied"] == 1, 2.0, "事件到达 OMS")
            await client.stop()

        asyncio.run(case())
        self.assertEqual(self.status_of("c-wire"), "cancelled")


# ---------------------------------------------------------------------------
# 6. 重连对账（REST 兜底）
# ---------------------------------------------------------------------------
def ok_env(rows, market="HK"):
    return {"ok": True, "value": {
        "mode": "live", "source": "futu/openapi:orders", "as_of": "2026-09-16T10:00:00.000Z",
        "groups": [{"acc_id": "1", "market": market, "rows": rows, "page_flag": "",
                    "completed": True}],
        "errors": [], "note": ""}}


class FakeGate:
    def __init__(self, envelope=None, error=None):
        self.envelope = envelope if envelope is not None else ok_env([])
        self.error = error
        self.calls = []

    def orders_open(self, payload):
        self.calls.append(dict(payload))
        if self.error is not None:
            raise self.error
        return self.envelope


class PushReconcilerTest(EventBridgeBase):
    def test_reconcile_converges_order_from_rest_open_orders(self):
        self.seed("c-rec", "submitted", broker="B-rec", qty=100)
        gate = FakeGate(ok_env([{"order_id": "B-rec", "status": "FILLED_ALL",
                                 "dealt_qty": 100, "qty": 100}]))
        reconciler = trading.PushReconciler(str(self.home), gate=gate, markets=("HK",))
        record = reconciler.run(reason="ws-reconnect")
        self.assertEqual(self.status_of("c-rec"), "filled")
        self.assertEqual(record["reason"], "ws-reconnect")
        self.assertEqual(record["updated"][0]["to"], "filled")
        self.assertEqual(gate.calls[0]["mode"], "live", "对账走 live REST 查询")
        conn = self._conn()
        try:
            stored = core_store.kv_get(conn, "reconcile:push")
            snapshot = core_snapshots.reconcile_snapshot(conn)
        finally:
            conn.close()
        self.assertEqual(stored["reason"], "ws-reconnect")
        self.assertEqual(snapshot["push"]["reason"], "ws-reconnect",
                         "reconcile 快照暴露推送重连对账记录")

    def test_reconcile_reports_local_only_diff_without_guessing(self):
        self.seed("c-only", "submitted", broker="B-only", qty=100)
        gate = FakeGate(ok_env([]))
        reconciler = trading.PushReconciler(str(self.home), gate=gate, markets=("HK",))
        record = reconciler.run(reason="ws-reconnect")
        self.assertEqual(self.status_of("c-only"), "submitted", "差异只记录，不猜终态")
        self.assertEqual(record["diffs"][0]["kind"], "local_only")
        self.assertIn("warn", [row["level"] for row in self.alerts()])

    def test_reconcile_ignores_illegal_transition_and_keeps_evidence(self):
        """submitting → cancelled 不在 OMS 白名单：只记录差异，状态不动。"""
        self.seed("c-bad", "submitting", broker="B-bad", qty=100)
        gate = FakeGate(ok_env([{"order_id": "B-bad", "status": "CANCELED_ALL",
                                 "dealt_qty": 0, "qty": 100}]))
        reconciler = trading.PushReconciler(str(self.home), gate=gate, markets=("HK",))
        record = reconciler.run()
        self.assertEqual(self.status_of("c-bad"), "submitting")
        self.assertTrue(any(diff["kind"] == "illegal-transition" for diff in record["diffs"]))

    def test_reconcile_records_rest_failure_and_never_raises(self):
        gate = FakeGate(error=RuntimeError("REST 通道不可用"))
        reconciler = trading.PushReconciler(str(self.home), gate=gate, markets=("HK", "US"))
        record = reconciler.run(reason="ws-reconnect")
        self.assertTrue(record["errors"], "REST 失败如实记录")
        self.assertIn("warn", [row["level"] for row in self.alerts()])


# ---------------------------------------------------------------------------
# 7. 行情推送 → 进程内快照缓存
# ---------------------------------------------------------------------------
class QuoteSnapshotCacheTest(unittest.TestCase):
    def test_quote_frame_recorded_and_looked_up_within_ttl(self):
        clock = Clock(start=1_000_000, step=0)
        cache = futu_push.QuoteSnapshotCache(ttl_ms=5000, now_ms=clock)
        recorded = cache.record({"type": "QUOTE",
                                 "data": {"code": "HK.00700", "last_price": 1.5}})
        self.assertEqual(recorded, ["HK.00700"])
        hit = cache.lookup(["HK.00700"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["entries"][0]["last_price"], 1.5)

    def test_expired_entry_misses(self):
        clock = Clock(start=1_000_000, step=0)
        cache = futu_push.QuoteSnapshotCache(ttl_ms=1000, now_ms=clock)
        cache.record({"type": "QUOTE", "data": {"code": "HK.00700", "last_price": 1.5}})
        clock.value += 5000
        self.assertIsNone(cache.lookup(["HK.00700"]))
        self.assertIsNone(cache.lookup(["HK.99999"]), "缺失标的也算未命中")

    def test_non_quote_frames_are_ignored(self):
        cache = futu_push.QuoteSnapshotCache(now_ms=Clock(step=0))
        self.assertEqual(cache.record({"type": "TICKER", "data": {"code": "HK.00700"}}), [])
        self.assertEqual(cache.record({"type": "KLINE", "data": {"code": "HK.00700"}}), [])
        self.assertIsNone(cache.lookup(["HK.00700"]))


# ---------------------------------------------------------------------------
# 8. 服务接线（lifespan / healthz / rt_quote 命中）
# ---------------------------------------------------------------------------
class IdleScheduler:
    def __init__(self):
        self.calls = []
        self.alive = False
        self.last_error = None

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class RecordingReconciler:
    def __init__(self):
        self.calls = []

    def run(self, reason="ws-reconnect"):
        self.calls.append(reason)
        return {"reason": reason, "updated": [], "diffs": [], "errors": []}


class BrokenStatusRuntime:
    """status() 抛异常的推送运行时：healthz 主字段必须不受影响。"""

    async def start(self):
        return True

    async def stop(self):
        return None

    @property
    def quote_cache(self):
        return futu_push.QuoteSnapshotCache()

    def status(self):
        raise RuntimeError("push status boom")


class PushServiceWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)

    def write_config(self, channel):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"futu_channel": channel}), encoding="utf-8")

    def write_cred(self, cred=None):
        path = self.home / "cred.json"
        path.write_text(json.dumps(cred or oauth_cred()), encoding="utf-8")
        return str(path)

    def make_app(self, runtime):
        return app_module.create_app(home=str(self.home),
                                     dist=str(self.home / "dist-missing"),
                                     scheduler=IdleScheduler(), push=runtime)

    def test_channel_mcp_does_not_start_clients(self):
        self.write_config("mcp")
        conn = FakeConnection()
        transport = FakeTransport(conn)
        runtime = futu_push.PushRuntime(home=str(self.home), transport=transport,
                                        credential_path=self.write_cred())
        with TestClient(self.make_app(runtime)) as client:
            body = client.get("/healthz").json()
        self.assertEqual(transport.calls, [], "通道=mcp 时零副作用（不建连接）")
        self.assertFalse(body["push"]["enabled"])
        self.assertFalse(body["push"]["quote"]["connected"])
        self.assertFalse(body["push"]["trade"]["connected"])

    def test_missing_credentials_does_not_start_clients(self):
        self.write_config("openapi")
        conn = FakeConnection()
        transport = FakeTransport(conn)
        runtime = futu_push.PushRuntime(
            home=str(self.home), transport=transport,
            credential_path=str(self.home / "no-such-cred.json"))
        with TestClient(self.make_app(runtime)) as client:
            body = client.get("/healthz").json()
        self.assertEqual(transport.calls, [], "无凭据时零副作用（不建连接）")
        self.assertFalse(body["push"]["enabled"])

    def test_openapi_with_credentials_starts_both_clients(self):
        self.write_config("openapi")
        quote_conn, trade_conn = FakeConnection(), FakeConnection()
        quote_conn.push(auth_ack("q-1"))
        trade_conn.push(auth_ack("t-1"))
        transport = FakeTransport(quote_conn, trade_conn)
        reconciler = RecordingReconciler()
        runtime = futu_push.PushRuntime(home=str(self.home), transport=transport,
                                        credential_path=self.write_cred(),
                                        reconciler=reconciler,
                                        now_ms=Clock(), sleep=FakeSleeper())
        with TestClient(self.make_app(runtime)) as client:
            body = client.get("/healthz").json()
        self.assertEqual(len(transport.calls), 2, "行情与交易各建一条连接")
        self.assertEqual(transport.calls[0]["url"], QUOTE_URL)
        self.assertEqual(transport.calls[1]["url"], TRADE_URL)
        for call in transport.calls:
            self.assertIn("ping_interval", call["kwargs"])
            self.assertIn("ping_timeout", call["kwargs"])
        self.assertEqual(quote_conn.actions(), ["auth"])
        self.assertEqual(trade_conn.actions(), ["auth"])
        push = body["push"]
        self.assertTrue(push["enabled"])
        self.assertTrue(push["quote"]["connected"])
        self.assertTrue(push["quote"]["authenticated"])
        self.assertTrue(push["trade"]["authenticated"])
        self.assertIsInstance(push["quote"]["last_message_at"], str)
        self.assertEqual(push["quote"]["reconnects"], 0)
        self.assertIsNone(push["quote"]["last_error"])

    def test_runtime_trade_reconnect_triggers_reconciler(self):
        """装配层：交易链重连 → PushRuntime 触发一次 REST 对账（事件不补发的兜底）。"""
        self.write_config("openapi")
        credential_path = self.write_cred()
        quote_conn = FakeConnection()
        quote_conn.push(auth_ack("q-1"))
        first, second = FakeConnection(), FakeConnection()
        first.push(auth_ack("t-1"))
        second.push(auth_ack("t-2"))
        transport = UrlTransport({QUOTE_URL: [quote_conn], TRADE_URL: [first, second]})
        reconciler = RecordingReconciler()
        runtime = futu_push.PushRuntime(home=str(self.home), transport=transport,
                                        credential_path=credential_path,
                                        reconciler=reconciler, now_ms=Clock(),
                                        sleep=FakeSleeper())

        async def case():
            try:
                self.assertTrue(await runtime.start())
                await first.wait_sent(1)
                first.drop()
                await wait_for(lambda: len(transport.url_calls(TRADE_URL)) == 2, 2.0,
                               "交易链重连")
                await wait_for(lambda: reconciler.calls, 2.0, "重连触发对账")
            finally:
                await runtime.stop()
            return reconciler.calls

        calls = asyncio.run(case())
        self.assertEqual(calls, ["ws-reconnect"])
        self.assertEqual(len(transport.url_calls(QUOTE_URL)), 1, "行情链不应受影响")

    def test_lifespan_stop_closes_connections(self):
        self.write_config("openapi")
        quote_conn, trade_conn = FakeConnection(), FakeConnection()
        quote_conn.push(auth_ack())
        trade_conn.push(auth_ack())
        runtime = futu_push.PushRuntime(home=str(self.home),
                                        transport=FakeTransport(quote_conn, trade_conn),
                                        credential_path=self.write_cred(),
                                        reconciler=RecordingReconciler(),
                                        now_ms=Clock(), sleep=FakeSleeper())
        with TestClient(self.make_app(runtime)):
            pass
        self.assertTrue(quote_conn.closed, "lifespan 退出应关闭连接")
        self.assertTrue(trade_conn.closed)
        self.assertFalse(runtime.status()["quote"]["connected"])

    def test_healthz_push_shape_survives_status_error(self):
        self.write_config("mcp")
        with TestClient(self.make_app(BrokenStatusRuntime())) as client:
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("mode", body)
        self.assertIn("scheduler", body)
        self.assertIsInstance(body["push"]["quote"], dict)
        self.assertIsInstance(body["push"]["trade"], dict)
        self.assertIn("error", body["push"])

    def test_rt_quote_hits_push_cache_and_marks_source(self):
        cache = futu_push.QuoteSnapshotCache(ttl_ms=5000, now_ms=Clock(step=0))
        cache.record({"type": "QUOTE", "data": {"code": "HK.00700", "last_price": 1.5}})
        futu = futu_data.FutuData(market=object(), channel="openapi", push=cache)
        envelope = futu.handle("rt_quote", {"codes": ["00700.HK"]})
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["value"]["source"], "push")
        self.assertEqual(envelope["value"]["code_list"][0]["last_price"], 1.5)

    def test_rt_quote_falls_back_to_channel_when_cache_cold(self):
        calls = []

        class Market:
            @staticmethod
            def stock_quote(codes):
                calls.append(list(codes))
                return {"code_list": [{"code": codes[0]}]}

        futu = futu_data.FutuData(market=Market(), channel="openapi", push=None)
        envelope = futu.handle("rt_quote", {"codes": ["00700.HK"]})
        self.assertTrue(envelope["ok"])
        self.assertNotIn("source", envelope["value"])
        self.assertEqual(len(calls), 1, "缓存未命中必须走通道取数")


# ---------------------------------------------------------------------------
# 9. 真实 WS 冒烟（DSH_WP8_SLOW=1 且有凭据时）
# ---------------------------------------------------------------------------
@unittest.skipUnless(os.environ.get("DSH_WP8_SLOW") == "1", "需 DSH_WP8_SLOW=1")
class RealWsSmokeTest(unittest.TestCase):
    """真实连接 5 秒收鉴权结果（附录 A 的活体验证）；无凭据/无网络自动跳过。"""

    def test_quote_ws_auth_roundtrip(self):
        if not futu_data.openapi_ready():
            self.skipTest("未配置 OpenAPI 凭据")
        from trading_datasource.futu_openapi import CredentialStore

        store = CredentialStore()
        frames = []

        async def case():
            client = futu_push.QuotePushClient(QUOTE_URL, store.load,
                                               on_message=frames.append)
            await client.start()
            try:
                await asyncio.sleep(5)
                status = client.status()  # stop() 会把 connected/authenticated 归位
            finally:
                await client.stop()
            return status

        status = asyncio.run(case())
        self.assertTrue(status["authenticated"], f"鉴权失败：{status['last_error']}")
        self.assertTrue(status["connected"])
        self.assertIsNotNone(status["session_id"])

    def test_quote_ws_refresh_keeps_session_alive(self):
        """刷新帧活体验证：把 5 分钟周期压缩成 3 秒，服务端不应因此断开。"""
        if not futu_data.openapi_ready():
            self.skipTest("未配置 OpenAPI 凭据")
        from trading_datasource.futu_openapi import CredentialStore

        store = CredentialStore()

        async def case():
            client = futu_push.QuotePushClient(QUOTE_URL, store.load,
                                               on_message=lambda _f: None,
                                               refresh_interval=3.0)
            await client.start()
            try:
                await asyncio.sleep(9)  # 至少两次 refresh 帧
                status = client.status()
            finally:
                await client.stop()
            return status

        status = asyncio.run(case())
        self.assertTrue(status["authenticated"], f"会话中断：{status['last_error']}")
        self.assertEqual(status["reconnects"], 0, "刷新帧被接受时不应触发重连")

    def test_trade_ws_auth_roundtrip(self):
        """交易 WS 与行情 WS 同构（同签名原文/同帧）：活体验证第二条链。"""
        if not futu_data.openapi_ready():
            self.skipTest("未配置 OpenAPI 凭据")
        from trading_datasource.futu_openapi import CredentialStore

        store = CredentialStore()
        events = []

        async def case():
            client = futu_push.TradePushClient(TRADE_URL, store.load,
                                               on_event=events.append)
            await client.start()
            try:
                await asyncio.sleep(5)
                status = client.status()
            finally:
                await client.stop()
            return status

        status = asyncio.run(case())
        self.assertTrue(status["authenticated"], f"鉴权失败：{status['last_error']}")
        self.assertIsNotNone(status["session_id"])
        self.assertEqual(status["intent"]["quote"], [], "交易 WS 不发订阅帧")


if __name__ == "__main__":
    unittest.main(verbosity=2)
