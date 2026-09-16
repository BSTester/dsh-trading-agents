"""富途 WebSocket 推送（WP8 任务 4）：行情订阅 + 交易事件，含保活与断线重连。

协议事实来源：``docs/superpowers/plans/2026-09-16-wp8-futu-openapi-unification.md``
附录 A（2026-09-16 实抓官方文档）；本模块**逐条对齐**，不引入附录之外的行为：

行情 WS（``wss://webapi-quote.futunn.com/ws``）
  * 首帧鉴权 ``{"action":"auth","data":{auth_type,credential_id,authorization,
    timestamp_ms,nonce}}``（appkey）或 ``auth_type:"oauth2"`` + ``Bearer <token>``；
  * **WS 签名原文 = ``{timestamp_ms}\\n{nonce}\\nWEBSOCKET\\nws/auth``**，由
    ``trading_datasource.futu_openapi.AppKeySigner.sign_ws`` 承担（不复用 REST 五段）；
  * 鉴权成功应答 ``{"id","session_id","server_time"}``；**成功之前不得订阅**；
  * 订阅/反订阅 ``{"id","action":"subscribe|unsubscribe","quote":[...],
    "order_book":[...],"ticker":[...],"kline":[{symbol,period,adjust}]}``，应答
    ``{"id","code":0,"message":""}``（``code!=0`` 为失败）；
  * 刷新：至少每 10 分钟、建议 5 分钟发 ``{"action":"refresh","data":同鉴权字段}``
    （``timestamp_ms``/``nonce`` 必须为新值）；超过 10 分钟服务端主动断开；
  * **无业务层 JSON 心跳**：保活走协议层 ping/pong（库的 ping_interval/ping_timeout），
    本模块绝不发 ``{"action":"heartbeat"}``；
  * 断线 → 重连 → 重新鉴权 → 按**本地订阅意图表**重新订阅；首屏/断线补齐用 REST。

交易 WS（``wss://webapi-trade.futunn.com/ws``）
  * 鉴权/刷新与行情 WS **完全同构**（同一份签名原文、同样的刷新要求）；
  * 鉴权成功后**自动订阅该用户全部交易事件**，客户端不发送任何订阅帧；
  * 断线期间事件**不补发** → 重连后由 ``on_reconnect`` 回调上层触发 REST 对账；
  * 事件不保证严格顺序 → 本模块只做「透传 + 重连信号」，状态机权威在
    ``server/trading.py``（推送仅作加速，不是唯一事实源）。

可测试性：``transport`` 可注入（离线测试用假连接脚本化「鉴权成功/失败、订阅应答、
推送帧、断线」）；``now_ms``/``sleep`` 可注入（假时钟驱动 5 分钟刷新与退避断言）。
真实传输默认惰性导入 ``websockets``（仅在使用默认 transport 时才需要该依赖），
连接参数固定 ``ping_interval``/``ping_timeout``（协议层心跳）。
"""
import asyncio
import inspect
import json
import secrets
import string
import threading
import time
from datetime import datetime, timezone

QUOTE_WS_URL = "wss://webapi-quote.futunn.com/ws"
TRADE_WS_URL = "wss://webapi-trade.futunn.com/ws"
#: 鉴权/刷新的签名原文末段（AppKeySigner.ws_signing_message 的第二分段同名约定）
WS_AUTH_PATH = "ws/auth"

#: 订阅帧的四个可选通道（附录 A：帧里只出现有意图的通道）
SUBSCRIBE_CHANNELS = ("quote", "order_book", "ticker", "kline")
#: 推送类型（附录 A）；QUOTE 之外的帧只透传，不进行情快照缓存
PUSH_KINDS = frozenset({"QUOTE", "ORDER_BOOK", "TICKER", "KLINE", "BROKER_QUEUE",
                        "MARKET_STATE"})
#: 交易事件类型（附录 A，10 类）
TRADE_EVENT_TYPES = frozenset({
    "EVENT_NEW", "EVENT_REPLACED", "EVENT_CANCELED", "EVENT_EXPIRED", "EVENT_FILL",
    "EVENT_NEW_REJECTED", "EVENT_REPLACE_REJECTED", "EVENT_CANCEL_REJECTED",
    "EVENT_FILL_CORRECT", "EVENT_FILL_CANCEL"})

#: 刷新周期：建议 5 分钟；官方硬上限 10 分钟（超时服务端断开）——配置超过上限直接拒绝
DEFAULT_REFRESH_SECONDS = 300.0
MAX_REFRESH_SECONDS = 600.0
DEFAULT_PING_INTERVAL = 20.0
DEFAULT_PING_TIMEOUT = 20.0
DEFAULT_AUTH_TIMEOUT = 10.0
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_MAX = 30.0
#: 连续鉴权失败上限：凭据本身无效时无限重连只会刷日志（且会撞限频），到此停止并留证据
DEFAULT_MAX_AUTH_FAILURES = 5

NONCE_ALPHABET = string.ascii_letters + string.digits + "_-"
_STATUS_FIELDS = ("connected", "authenticated", "last_message_at", "last_message_ms",
                  "reconnects", "last_error")


class PushError(Exception):
    """推送层错误基类。"""


class PushAuthError(PushError):
    """鉴权失败（凭据非法 / 应答 code!=0 / 等待鉴权应答超时）。"""


class PushDisconnected(PushError):
    """连接断开（假连接与真实传输都用它表达「本次会话结束」）。"""


def _iso_from_ms(at_ms):
    """毫秒时间戳 → UTC ISO 串（与 caches.iso_from_ms 同格式，自带实现不跨模块依赖）。"""
    if at_ms is None:
        return None
    moment = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _default_now_ms():
    return int(time.time() * 1000)


def _new_nonce():
    """nonce：1-64 位 [A-Za-z0-9_-]（与 REST 的 X-Nonce 同一约束）。"""
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(32))


def _openapi_module():
    """惰性导入 trading_datasource.futu_openapi（appkey 私钥加载会拉起 cryptography）。"""
    from trading_datasource import futu_openapi  # noqa: PLC0415
    return futu_openapi


def resolve_credential(provider):
    """凭据提供者 → dict：callable（每次重连现取，token 轮换后自动生效）/ CredentialStore / dict。"""
    if provider is None:
        return {}
    if isinstance(provider, dict):
        return dict(provider)
    if callable(provider):
        value = provider()
    elif hasattr(provider, "load"):
        value = provider.load()
    else:
        raise PushAuthError(f"不支持的凭据提供者：{provider!r}")
    return dict(value or {})


def auth_data(credential, timestamp_ms, nonce, signer=None):
    """鉴权/刷新帧的 ``data`` 段（appkey 与 oauth2 两种，附录 A 逐字段）。

    appkey 的 ``credential_id`` 是 AppKey ID（REST 的 ``X-Api-Key`` 同值）；
    ``authorization`` 是 WS 专用原文的签名（base64，无 Bearer 前缀）。
    """
    mode = credential.get("mode")
    timestamp_ms = int(timestamp_ms)
    if mode == "appkey":
        app_key = credential.get("app_key")
        if not app_key:
            raise PushAuthError("appkey 凭据缺少 app_key")
        if signer is None:
            path = credential.get("private_key_path")
            if not path:
                raise PushAuthError("appkey 凭据缺少 private_key_path")
            try:
                signer = _openapi_module().AppKeySigner.from_path(
                    path, credential.get("algorithm", "Ed25519"))
            except PushAuthError:
                raise
            except Exception as error:  # noqa: BLE001 —— 坏 PEM/算法不支持 → 凭据不可用
                raise PushAuthError(f"AppKey 私钥不可用：{error}") from error
        return {"auth_type": "appkey", "credential_id": str(app_key),
                "authorization": signer.sign_ws(timestamp_ms, nonce),
                "timestamp_ms": timestamp_ms, "nonce": nonce}
    if mode == "oauth":
        token = credential.get("access_token")
        if not token:
            raise PushAuthError("oauth 凭据缺少 access_token（先运行 scripts/futu_auth.py --openapi）")
        return {"auth_type": "oauth2", "authorization": f"Bearer {token}",
                "timestamp_ms": timestamp_ms, "nonce": nonce}
    raise PushAuthError(f"凭据 mode 非法：{mode!r}（需 appkey | oauth）")


def auth_frame(credential, timestamp_ms, nonce, signer=None):
    """首帧鉴权（附录 A：不带 ``id``，与订阅帧不同）。"""
    return {"action": "auth", "data": auth_data(credential, timestamp_ms, nonce, signer)}


def refresh_frame(credential, timestamp_ms, nonce, signer=None):
    """刷新帧：与鉴权帧同构，只有 action 与 timestamp/nonce 不同。"""
    return {"action": "refresh",
            "data": auth_data(credential, timestamp_ms, nonce, signer)}


def _normalize_symbol(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"订阅标的必须是非空字符串：{value!r}")
    return value.strip()


def normalize_kline(item):
    """K 线订阅项：``{symbol, period, adjust}``（symbol 必填，其余原样透传）。"""
    if not isinstance(item, dict):
        raise ValueError(f"kline 订阅项必须是对象：{item!r}")
    symbol = _normalize_symbol(item.get("symbol"))
    payload = {"symbol": symbol}
    for key in ("period", "adjust"):
        if item.get(key) is not None:
            payload[key] = item[key]
    return payload


def normalize_items(items):
    """订阅意图 → ``{channel: {key: payload}}``（键用于 diff，与顺序无关）。

    通道白名单外的键直接拒绝（不静默丢弃，避免「订阅了但没生效」的假象）。
    """
    if items is None:
        return {}
    if not isinstance(items, dict):
        raise ValueError(f"订阅意图必须是对象：{items!r}")
    unknown = [key for key in items if key not in SUBSCRIBE_CHANNELS]
    if unknown:
        raise ValueError(f"未知订阅通道：{sorted(unknown)}（允许：{list(SUBSCRIBE_CHANNELS)}）")
    normalized = {}
    for channel, value in items.items():
        if value is None:
            continue
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{channel} 订阅项必须是列表：{value!r}")
        bucket = {}
        for entry in value:
            if channel == "kline":
                payload = normalize_kline(entry)
                key = (payload["symbol"], str(payload.get("period")),
                       str(payload.get("adjust")))
            else:
                payload = {"symbol": _normalize_symbol(entry)}
                key = payload["symbol"]
            bucket[key] = payload
        if bucket:
            normalized[channel] = bucket
    return normalized


def intent_view(intent):
    """订阅意图的用户视图（进程外可序列化，与 ``subscribe()`` 的入参同形）。

    非 kline 通道给标的字符串列表，kline 给 ``{symbol,period,adjust}`` 对象列表；
    因此 ``subscribe(snapshot_intent())`` 是幂等往返。
    """
    view = {channel: [] for channel in SUBSCRIBE_CHANNELS}
    for channel, bucket in (intent or {}).items():
        for key in sorted(bucket):
            payload = bucket[key]
            if channel == "kline":
                view[channel].append(dict(payload))
            else:
                view[channel].append(payload["symbol"])
    return view


def _frame_changes(changes):
    """``{channel: {key: payload}}`` → 帧里的通道字段（只带有意图的通道）。"""
    frame = {}
    for channel in ("quote", "order_book", "ticker"):
        bucket = changes.get(channel)
        if bucket:
            frame[channel] = [bucket[key]["symbol"] for key in sorted(bucket)]
    bucket = changes.get("kline")
    if bucket:
        frame["kline"] = [dict(bucket[key]) for key in sorted(bucket)]
    return frame


def subscribe_frame(changes, request_id):
    frame = {"id": request_id, "action": "subscribe"}
    frame.update(_frame_changes(changes))
    return frame


def unsubscribe_frame(changes, request_id):
    frame = {"id": request_id, "action": "unsubscribe"}
    frame.update(_frame_changes(changes))
    return frame


def _encode(frame):
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def _decode(raw):
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def empty_status():
    """未启动客户端的 healthz 形状（字段齐全，绝不缺键）。"""
    status = {field: None for field in _STATUS_FIELDS}
    status.update({"connected": False, "authenticated": False, "reconnects": 0,
                   "started": False, "session_id": None})
    return status


def _status_of(client):
    return client.status() if client is not None else empty_status()


def safe_status(runtime):
    """healthz 专用：任何 ``status()`` 异常都不影响主字段（推送是旁路，不是存活条件）。"""
    try:
        return runtime.status()
    except Exception as error:  # noqa: BLE001 —— healthz 绝不为推送解析失败而 500
        return {"enabled": False, "error": str(error)[:300],
                "quote": empty_status(), "trade": empty_status()}


def default_transport(url, *, ping_interval=DEFAULT_PING_INTERVAL,
                      ping_timeout=DEFAULT_PING_TIMEOUT, open_timeout=10.0,
                      **kwargs):
    """默认传输：``websockets`` 的 asyncio 客户端（协议层 ping/pong 即保活）。

    **普通函数**（不是协程）：返回异步上下文管理器，客户端用
    ``async with self._transport(url, ...) as conn`` 建连接——与假传输同一调用契约。
    ``**kwargs`` 忽略，便于两种传输共用签名。``websockets`` 惰性导入：离线测试注入
    transport 时不需要该依赖。
    """
    from websockets.asyncio.client import connect  # noqa: PLC0415
    return connect(url, ping_interval=ping_interval, ping_timeout=ping_timeout,
                   open_timeout=open_timeout)


class _PushClient:
    """推送客户端公共部分：鉴权 → 保活/刷新 → 断线指数退避重连。

    子类只差两点：``_dispatch(frame)``（行情→on_message / 交易→on_event）与
    ``_after_auth(conn)``（行情首次 flush 订阅；交易什么都不做——服务端自动订阅）。
    """

    def __init__(self, url, credential_provider, transport=None, *, name="push",
                 on_reconnect=None, now_ms=None, sleep=None, backoff_base=DEFAULT_BACKOFF_BASE,
                 backoff_max=DEFAULT_BACKOFF_MAX, refresh_interval=DEFAULT_REFRESH_SECONDS,
                 ping_interval=DEFAULT_PING_INTERVAL, ping_timeout=DEFAULT_PING_TIMEOUT,
                 open_timeout=DEFAULT_AUTH_TIMEOUT, auth_timeout=DEFAULT_AUTH_TIMEOUT,
                 max_auth_failures=DEFAULT_MAX_AUTH_FAILURES,
                 refresh_max_attempts=None):
        refresh_interval = float(refresh_interval)
        if not 0 < refresh_interval <= MAX_REFRESH_SECONDS:
            raise ValueError(f"refresh_interval 必须在 (0, {MAX_REFRESH_SECONDS}] 秒"
                             f"（官方：至少每 10 分钟刷新一次，超时服务端断开）")
        if backoff_base <= 0 or backoff_max < backoff_base:
            raise ValueError("退避参数非法：要求 0 < backoff_base <= backoff_max")
        self.url = url
        self.name = name
        self._credential_provider = credential_provider
        self._transport = transport if transport is not None else default_transport
        self._on_reconnect = on_reconnect
        self._now_ms = now_ms if now_ms is not None else _default_now_ms
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._backoff_base = float(backoff_base)
        self._backoff_max = float(backoff_max)
        self._refresh_interval = refresh_interval
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._open_timeout = open_timeout
        self._auth_timeout = auth_timeout
        self._max_auth_failures = max_auth_failures
        self._refresh_max_attempts = refresh_max_attempts

        self._lock = threading.Lock()
        self._intent = {}
        self._acked = {}
        self._inflight = {}
        self._pending = {}
        self._request_id = 0
        self._loop = None
        self._wake = None
        self._task = None
        self._conn = None
        self._stopping = False
        self._started = False
        self._connected = False
        self._authenticated = False
        self._session_id = None
        self._last_message_ms = None
        self._reconnects = 0
        self._last_error = None
        self._auth_failures = 0
        self._had_session = False

    # ---- 生命周期 ----
    async def start(self):
        """启动后台任务（幂等：已在跑则直接返回）。"""
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stopping = False
        self._started = True
        self._auth_failures = 0
        self._task = asyncio.create_task(self._run(), name=f"futu-push-{self.name}")

    async def stop(self):
        """取消后台任务并关闭连接（幂等；未启动时零副作用）。"""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 —— 停止路径不掩盖调用方
                pass
        conn, self._conn = self._conn, None
        self._started = False
        self._connected = False
        self._authenticated = False
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- 订阅意图（幂等；线程安全：HTTP/工具线程也可调用）----
    def subscribe(self, items):
        """把 items 并入本地订阅意图表；只对**新增**的部分发订阅帧（重复调用不重发）。"""
        changes = normalize_items(items)
        changed = False
        with self._lock:
            for channel, bucket in changes.items():
                target = self._intent.setdefault(channel, {})
                for key, payload in bucket.items():
                    if target.get(key) != payload:
                        target[key] = payload
                        changed = True
        if changed:
            self._notify()
        return self.snapshot_intent()

    def unsubscribe(self, items):
        """从本地订阅意图表移除 items；只对**已确认订阅**的部分发反订阅帧。"""
        changes = normalize_items(items)
        changed = False
        with self._lock:
            for channel, bucket in changes.items():
                target = self._intent.get(channel)
                if not target:
                    continue
                for key in bucket:
                    if target.pop(key, None) is not None:
                        changed = True
        if changed:
            self._notify()
        return self.snapshot_intent()

    def snapshot_intent(self):
        with self._lock:
            return intent_view(self._intent)

    def _notify(self):
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:  # 事件循环已关闭：下一次 start 会重建
            pass

    def _diff(self):
        """``(新增, 待反订阅)``：意图表与「已确认 + 已发出未应答」的差集。

        - 新增 = 意图 − 已确认 − 在途（重复 subscribe 调用不重发；失败应答会清掉在途，
          下一次 flush 才重试）；
        - 反订阅 = 已确认 − 意图（只对服务端确实订阅过的标的发反订阅）。
        """
        with self._lock:
            subscribe, unsubscribe = {}, {}
            for channel in SUBSCRIBE_CHANNELS:
                want = self._intent.get(channel) or {}
                have = self._acked.get(channel) or {}
                pending = self._inflight.get(channel) or {}
                added = {key: value for key, value in want.items()
                         if key not in have and key not in pending}
                removed = {key: value for key, value in have.items() if key not in want}
                if added:
                    subscribe[channel] = added
                if removed:
                    unsubscribe[channel] = removed
        return subscribe, unsubscribe

    def _next_request_id(self):
        with self._lock:
            self._request_id += 1
            return self._request_id

    # ---- 状态 ----
    def status(self):
        with self._lock:
            return {"connected": self._connected, "authenticated": self._authenticated,
                    "last_message_at": _iso_from_ms(self._last_message_ms),
                    "last_message_ms": self._last_message_ms,
                    "reconnects": self._reconnects,
                    "last_error": self._last_error,
                    "started": self._started,
                    "session_id": self._session_id,
                    "url": self.url,
                    "intent": intent_view(self._intent)}

    def _record_error(self, message):
        with self._lock:
            self._last_error = str(message)[:300]

    # ---- 会话 / 重连 ----
    async def _run(self):
        attempt = 0
        try:
            while not self._stopping:
                if self._max_auth_failures is not None \
                        and self._auth_failures >= self._max_auth_failures:
                    # 保留具体的鉴权失败原因（healthz 的 last_error 要看得到根因）
                    with self._lock:
                        if not self._last_error:
                            self._last_error = f"鉴权连续失败 {self._auth_failures} 次，已停止重连"
                    break
                try:
                    async with self._transport(
                            self.url, ping_interval=self._ping_interval,
                            ping_timeout=self._ping_timeout,
                            open_timeout=self._open_timeout) as conn:
                        with self._lock:
                            self._conn = conn
                            self._connected = True
                            self._authenticated = False
                            self._acked = {}
                            self._inflight = {}
                            self._pending = {}
                        try:
                            await self._open_session(conn)
                        finally:
                            with self._lock:
                                self._connected = False
                                self._authenticated = False
                                self._acked = {}
                                self._inflight = {}
                                self._pending = {}
                                self._conn = None
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except PushAuthError as error:
                    with self._lock:
                        self._auth_failures += 1
                    self._record_error(error)
                except Exception as error:  # noqa: BLE001 —— 任何断开都走重连（含假连接）
                    self._record_error(f"{type(error).__name__}: {error}")
                if self._stopping:
                    break
                with self._lock:
                    self._reconnects += 1
                delay = min(self._backoff_max, self._backoff_base * (2 ** attempt))
                attempt += 1
                await self._sleep(delay)
        finally:
            with self._lock:
                self._started = False
                self._connected = False
                self._authenticated = False
                self._conn = None
                self._task = None

    async def _open_session(self, conn):
        credential = resolve_credential(self._credential_provider)
        ack = await self._authenticate(conn, credential)
        with self._lock:
            self._authenticated = True
            self._auth_failures = 0
            self._session_id = ack.get("session_id")
            self._last_message_ms = self._now_ms()
            reconnected = self._had_session
            self._had_session = True
        await self._after_auth(conn)
        if reconnected:
            await self._call(self._on_reconnect, self.status())
        await self._serve(conn)

    async def _authenticate(self, conn, credential):
        timestamp_ms = self._now_ms()
        nonce = _new_nonce()
        frame = auth_frame(credential, timestamp_ms, nonce)
        await conn.send(_encode(frame))
        while True:
            try:
                raw = await asyncio.wait_for(conn.recv(), timeout=self._auth_timeout)
            except asyncio.TimeoutError as error:
                raise PushAuthError(f"等待鉴权应答超时（{self._auth_timeout}s）") from error
            reply = _decode(raw)
            if not isinstance(reply, dict):
                continue
            self._note_message()
            if reply.get("session_id"):
                return reply
            code = reply.get("code")
            if code is not None and int(code) != 0:
                message = str(reply.get("message") or "")
                raise PushAuthError(f"鉴权失败（code={code}）：{message}"[:300])

    async def _serve(self, conn):
        """一个会话的事件循环：收帧 + 保活刷新；任一侧异常/断开都会结束本会话。"""
        receiver = asyncio.create_task(self._recv_loop(conn))
        keepalive = asyncio.create_task(self._keepalive_loop(conn))
        try:
            done, _ = await asyncio.wait({receiver, keepalive},
                                         return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    raise error
        finally:
            for task in (receiver, keepalive):
                task.cancel()
            await asyncio.gather(receiver, keepalive, return_exceptions=True)

    async def _recv_loop(self, conn):
        while True:
            raw = await conn.recv()
            if raw is None:
                raise PushDisconnected("连接已关闭")
            frame = _decode(raw)
            if frame is None:
                continue
            self._note_message()
            if self._handle_reply(frame):
                continue
            await self._call(self._dispatch, frame)

    async def _keepalive_loop(self, conn):
        # 协议层 ping/pong 由库承担；这里只负责 5 分钟 refresh 帧与订阅差量补发。
        next_refresh = self._loop.time() + self._refresh_interval
        while True:
            timeout = max(0.0, next_refresh - self._loop.time())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                await self._flush(conn)
                await self._refresh(conn)
                next_refresh = self._loop.time() + self._refresh_interval
                continue
            self._wake.clear()
            await self._flush(conn)

    async def _refresh(self, conn):
        credential = resolve_credential(self._credential_provider)
        frame = refresh_frame(credential, self._now_ms(), _new_nonce())
        await conn.send(_encode(frame))

    async def _flush(self, conn):
        """把订阅意图差量发给服务端：新增 → subscribe，移除 → unsubscribe。"""
        subscribe, unsubscribe = self._diff()
        if subscribe:
            request_id = self._next_request_id()
            self._pending[request_id] = {"kind": "subscribe", "changes": subscribe}
            self._mark_inflight(subscribe)
            await conn.send(_encode(subscribe_frame(subscribe, request_id)))
        if unsubscribe:
            request_id = self._next_request_id()
            self._pending[request_id] = {"kind": "unsubscribe", "changes": unsubscribe}
            await conn.send(_encode(unsubscribe_frame(unsubscribe, request_id)))

    def _mark_inflight(self, changes):
        with self._lock:
            for channel, bucket in changes.items():
                self._inflight.setdefault(channel, {}).update(bucket)

    def _handle_reply(self, frame):
        """应答帧：鉴权成功后的 session 保活帧与订阅应答；返回 True 表示已消费。"""
        if frame.get("session_id"):
            with self._lock:
                self._session_id = frame.get("session_id")
            return True
        request_id = frame.get("id")
        code = frame.get("code")
        if code is None or request_id is None:
            return False
        pending = self._pending.pop(request_id, None)
        if int(code) == 0:
            if pending is not None:
                self._confirm(pending)
            return True
        message = str(frame.get("message") or "")
        self._record_error(f"订阅应答失败（id={request_id}, code={code}）：{message}"[:300])
        if pending is not None:
            # 失败应答：清掉在途标记，下一次 flush（refresh 周期）重试
            self._clear_inflight(pending["changes"])
        return True

    def _clear_inflight(self, changes):
        with self._lock:
            for channel, bucket in changes.items():
                target = self._inflight.get(channel)
                if not target:
                    continue
                for key in bucket:
                    target.pop(key, None)

    def _confirm(self, pending):
        """应答 code==0：订阅差异并入「已确认」，反订阅差异从「已确认」移除。"""
        with self._lock:
            for channel, bucket in pending["changes"].items():
                target = self._acked.setdefault(channel, {})
                inflight = self._inflight.setdefault(channel, {})
                if pending["kind"] == "subscribe":
                    target.update(bucket)
                else:
                    for key in bucket:
                        target.pop(key, None)
                for key in bucket:
                    inflight.pop(key, None)

    def _note_message(self):
        with self._lock:
            self._last_message_ms = self._now_ms()

    @staticmethod
    async def _call(callback, argument):
        """回调可同步可异步；回调异常不得打断会话。"""
        if callback is None:
            return None
        try:
            result = callback(argument)
            if inspect.isawaitable(result):
                return await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 上层回调的失败不拖垮连接
            return None
        return None

    async def _after_auth(self, conn):
        return None

    async def _dispatch(self, frame):
        return None


class QuotePushClient(_PushClient):
    """行情推送：连接 → 鉴权 → 按意图表订阅 → 保活/刷新 → 断线重连重订阅。"""

    def __init__(self, url, credential_provider, on_message, transport=None, **kwargs):
        super().__init__(url, credential_provider, transport, name="quote", **kwargs)
        self._on_message = on_message

    async def _after_auth(self, conn):
        # 鉴权成功前不订阅；成功后的首次订阅按本地意图表 diff（可能为空——工具侧可后加）
        await self._flush(conn)

    async def _dispatch(self, frame):
        await self._call(self._on_message, frame)


class TradePushClient(_PushClient):
    """交易推送：鉴权成功后**被动接收全部交易事件**（不发订阅帧），重连走 on_reconnect。

    硬约束（附录 A）：断线期间事件不补发、事件不保证顺序——因此本类**不做**状态判断，
    只负责把事件交给上层（``server/trading.py`` 的 TradeEventBridge）并在重连后回调
    ``on_reconnect`` 触发一次 REST 对账。
    """

    def __init__(self, url, credential_provider, on_event, transport=None, **kwargs):
        super().__init__(url, credential_provider, transport, name="trade", **kwargs)
        self._on_event = on_event

    async def _dispatch(self, frame):
        await self._call(self._on_event, frame)


class QuoteSnapshotCache:
    """进程内行情快照缓存：WS 推送的 QUOTE 帧 → ``rt_quote`` 之类工具直接命中。

    TTL 语义与 ``server/caches.py`` 一致（超过 ttl 视为未命中），命中方负责标注来源。
    只收 ``type == QUOTE`` 的帧；缺失任一请求标的也算未命中（不做部分命中拼接）。
    """

    DEFAULT_TTL_MS = 5000

    def __init__(self, ttl_ms=None, now_ms=None, max_symbols=500):
        self.ttl_ms = int(self.DEFAULT_TTL_MS if ttl_ms is None else ttl_ms)
        self._now_ms = now_ms if now_ms is not None else _default_now_ms
        self._max_symbols = int(max_symbols)
        self._lock = threading.Lock()
        self._entries = {}

    def record(self, frame):
        """记录一帧推送；返回本次写入的标的列表（非 QUOTE 帧返回空）。"""
        if not isinstance(frame, dict):
            return []
        data = frame.get("data")
        entries = data if isinstance(data, list) else [data if isinstance(data, dict)
                                                       else frame]
        kind = _push_kind(frame)
        if kind != "QUOTE":
            return []
        now = self._now_ms()
        recorded = []
        with self._lock:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                symbol = _entry_symbol(entry)
                if not symbol:
                    continue
                self._entries[symbol] = {"at": now, "data": entry}
                recorded.append(symbol)
            if len(self._entries) > self._max_symbols:
                oldest = sorted(self._entries, key=lambda key: self._entries[key]["at"])
                for symbol in oldest[:len(self._entries) - self._max_symbols]:
                    self._entries.pop(symbol, None)
        return recorded

    def lookup(self, codes, ttl_ms=None):
        """全部标的都命中且未过期时返回 ``{"entries", "at_ms"}``，否则 None。"""
        ttl = self.ttl_ms if ttl_ms is None else int(ttl_ms)
        now = self._now_ms()
        entries, at_ms = [], None
        with self._lock:
            for code in codes:
                entry = self._entries.get(code)
                if entry is None or now - entry["at"] > ttl:
                    return None
                entries.append(entry["data"])
                at_ms = entry["at"] if at_ms is None else max(at_ms, entry["at"])
        return {"entries": entries, "at_ms": at_ms}

    def snapshot(self):
        with self._lock:
            now = self._now_ms()
            return {"count": len(self._entries), "ttl_ms": self.ttl_ms,
                    "symbols": sorted(self._entries),
                    "freshest_ms": max((entry["at"] for entry in self._entries.values()),
                                       default=None),
                    "now_ms": now}


def _push_kind(frame):
    candidates = [frame]
    data = frame.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    for source in candidates:
        for key in ("type", "action", "push_type", "msg_type", "event_type"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, str) and value.upper() in PUSH_KINDS:
                return value.upper()
    return None


def _entry_symbol(entry):
    for key in ("code", "symbol", "stock_code", "security_code"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class PushRuntime:
    """服务侧推送装配：通道/凭据判定 + 两个客户端的启停 + healthz 状态 + 行情快照缓存。

    **不变式 2（零副作用）**：``futu_channel != openapi`` 或 OpenAPI 凭据不可用时，
    ``start()`` 不建连接、不报错（只记 ``enabled=False`` 与原因）；``stop()`` 同样幂等。
    """

    def __init__(self, home=None, *, transport=None, credential_provider=None,
                 credential_path=None, quote_url=QUOTE_WS_URL, trade_url=TRADE_WS_URL,
                 quote_cache=None, bridge=None, reconciler=None, gate=None,
                 quote_client_factory=None, trade_client_factory=None, now_ms=None,
                 sleep=None, refresh_interval=DEFAULT_REFRESH_SECONDS,
                 ping_interval=DEFAULT_PING_INTERVAL, ping_timeout=DEFAULT_PING_TIMEOUT,
                 auth_timeout=DEFAULT_AUTH_TIMEOUT, markets=None):
        self.home = None if home is None else str(home)
        self._transport = transport
        self._credential_provider = credential_provider
        self._credential_path = credential_path
        self._quote_url = quote_url
        self._trade_url = trade_url
        self._quote_cache = quote_cache if quote_cache is not None else QuoteSnapshotCache()
        self._bridge = bridge
        self._reconciler = reconciler
        self._gate = gate
        self._quote_factory = quote_client_factory
        self._trade_factory = trade_client_factory
        self._now_ms = now_ms
        self._sleep = sleep
        self._refresh_interval = refresh_interval
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._auth_timeout = auth_timeout
        self._markets = markets
        self._quote = None
        self._trade = None
        self._started = False
        self._enabled = None
        self._skip_reason = None
        self._last_error = None
        self._lock = threading.Lock()

    # ---- 可用性判定（与 futu_data/trading 同一份 openapi_ready 实现）----
    @property
    def quote_cache(self):
        return self._quote_cache

    def enabled(self):
        """``futu_channel=openapi`` 且凭据可用才启动（否则如实为 False，不报错）。"""
        from server import futu_data  # noqa: PLC0415 —— 保持本模块可独立导入
        if self.home is not None and futu_data.load_channel(self.home) != \
                futu_data.CHANNEL_OPENAPI:
            self._skip_reason = "futu_channel != openapi"
            return False
        if not futu_data.openapi_ready(self._credential_path):
            self._skip_reason = "OpenAPI 凭据不可用（~/.dsh/futu-openapi.json）"
            return False
        self._skip_reason = None
        return True

    # ---- 启停 ----
    async def start(self):
        with self._lock:
            if self._started:
                return True
            self._started = True
            self._enabled = self.enabled()
            if not self._enabled:
                self._started = False
                return False
        try:
            self._quote, self._trade = self._build_clients()
            for client in (self._quote, self._trade):
                await client.start()
        except Exception as error:  # noqa: BLE001 —— 推送启动失败不影响服务主路径
            self._last_error = f"{type(error).__name__}: {error}"[:300]
            await self.stop()
            return False
        return True

    async def stop(self):
        with self._lock:
            quote, trade = self._quote, self._trade
            self._quote = self._trade = None
            self._started = False
        for client in (quote, trade):
            if client is None:
                continue
            try:
                await client.stop()
            except Exception as error:  # noqa: BLE001
                self._last_error = f"{type(error).__name__}: {error}"[:300]

    def _build_clients(self):
        provider = self._credential_provider or self._credential_store_loader()
        common = {"transport": self._transport, "now_ms": self._now_ms, "sleep": self._sleep,
                  "refresh_interval": self._refresh_interval,
                  "ping_interval": self._ping_interval, "ping_timeout": self._ping_timeout,
                  "auth_timeout": self._auth_timeout}
        if self._quote_factory is not None:
            quote = self._quote_factory(self._quote_url, provider, common)
        else:
            quote = QuotePushClient(self._quote_url, provider,
                                    on_message=self._on_quote_message, **common)
        if self._trade_factory is not None:
            trade = self._trade_factory(self._trade_url, provider, common)
        else:
            trade = TradePushClient(self._trade_url, provider,
                                    on_event=self._on_trade_event,
                                    on_reconnect=self._on_trade_reconnect, **common)
        return quote, trade

    def _credential_store_loader(self):
        def load():
            from trading_datasource.futu_openapi import CredentialStore  # noqa: PLC0415
            return CredentialStore(self._credential_path).load()
        return load

    # ---- 消息回调 ----
    def _on_quote_message(self, frame):
        self._quote_cache.record(frame)

    async def _on_trade_event(self, frame):
        """事件 → OMS/告警在线程里做（SQLite 写不占事件循环）。"""
        bridge = self._bridge
        if bridge is None:
            from server import trading  # noqa: PLC0415
            bridge = self._bridge = trading.TradeEventBridge(self.home)
        await asyncio.to_thread(bridge.handle, frame)

    async def _on_trade_reconnect(self, status):
        """重连 → REST 对账补齐（事件不补发；对账仍是事实来源）。"""
        reconciler = self._reconciler
        if reconciler is None:
            from server import trading  # noqa: PLC0415
            kwargs = {} if self._markets is None else {"markets": self._markets}
            reconciler = self._reconciler = trading.PushReconciler(
                self.home, gate=self._gate, **kwargs)
        await asyncio.to_thread(reconciler.run, "ws-reconnect")

    # ---- healthz ----
    def status(self):
        return {"enabled": bool(self._enabled), "started": bool(self._started),
                "reason": self._skip_reason, "last_error": self._last_error,
                "quote": _status_of(self._quote), "trade": _status_of(self._trade)}
