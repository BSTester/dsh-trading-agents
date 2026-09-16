"""TTL 缓存与载荷形状校验（WP6 补遗 C）。

移植源（逐行为准）：
  * ``plugins/workbench/src/rpc.js:9-37`` —— CACHE_TTL_MS 常量表 + stableKey 稳定序列化；
  * ``rpc.js:73-95`` —— cached() 包装：命中（TTL 内且形状合法）→ cached:true，
    未命中 → 取数 → 形状校验 → 写缓存（仅 TTL>0）→ cached:false；
    形状不符抛「{endpoint} 返回的载荷不完整，已按失败处理」，一切异常落成失败信封；
  * ``plugins/workbench/src/cache.js:26-118`` —— createTtlCache：**内存 + 磁盘两级**缓存
    （目录 ``$DSH_HOME/trading-workbench-cache``、条目 ``{value, at}``、TTL 过期删文件、
    ``maxFiles``/``maxBytes``、临时文件唯一名 + rename 原子落盘）；
  * ``plugins/workbench/src/endpoints.js:46-65`` —— ENDPOINT_SHAPE 表与 matchesShape。

关于磁盘这一级（更正补遗 A/B 的说明）：Node 侧 rpc.js **不是**只内存缓存。``rpc.js:65``
用 ``createTtlCache(deps)``，而 ``cache.js`` 明确实现了磁盘持久化——目的正是避免「每次重启
进程缓存全丢，用户重启后第一次点每个页签都要重新等一遍」的冷启动回归（cache.js:1-11）。
本模块因此同样实现两级：内存命中零开销；内存未命中再读磁盘并回填内存；写盘只在
JSON 可序列化且不超过 ``maxBytes`` 时进行（大结果只留内存，cache.js:11/103）。

键与 payload 的稳定序列化照抄 rpc.js:34-37（键排序后成对序列化，键顺序不影响命中）；
磁盘条目名照抄 cache.js:20-24 safeName（``<消毒后的端点>-<sha1(其余段) 前 16 位>.json``）。
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

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
    # WP7：因子快照按日收集，5 分钟 TTL 足够面板新鲜度（legacy rpc.js 不回写）
    "factors-history": 5 * 60_000,
    # WP11 任务 3：情绪快照同为按日采集，5 分钟 TTL 与因子快照同量级
    "sentiment-history": 5 * 60_000,
    # WP8 任务 2：OpenAPI 行情接入的基本类端点（基本类 5m；历史 K 线 v2 10m）。
    # 实时四类（market_snapshot/cur_kline/rt_data/rt_ticker）与其余富途实时端点
    # 一律 TTL 0，**不进**本表（与 WP8 富途实时直通 8 端点同口径）。
    "info_basicinfo": 5 * 60_000,
    "info_trading_days": 5 * 60_000,
    "info_search": 5 * 60_000,
    "info_market_state": 5 * 60_000,
    "quote_history_kline_v2": 10 * 60_000,
    # WP10 任务 1：流程快照（每市场阶段状态推导，读当日 ran 标记与表事实）——30 秒，
    # 与「调度」页同量级：页面轮询要能看到作业刚跑完的状态变化。
    "pipeline": 30_000,
}

# endpoints.js:46-65 的端点最小字段（plan-execute 是动作端点，不进缓存形状表）
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
    # 业务确认：``confirmation`` 有形状但 TTL=0（直读内存态，永不进缓存）；形状仍登记，
    # 与 endpoints.js 的 ENDPOINT_SHAPE 逐项一致（test_wp6_tables_lock 比对两侧表）。
    "confirmation": ["pending"],
    "schedule": ["heartbeat", "jobs"],
    "reconcile": ["diffs", "tca"],
    # WP10 任务 1：流程快照最小字段（每市场阶段表 + 全局阶段 + 配置摘要）
    "pipeline": ["date", "markets", "global", "auto_pipeline"],
    # WP7：因子快照历史（服务定时收集），最小字段只有一个快照数组
    "factors-history": ["snapshots"],
    # WP11 任务 3：情绪快照历史/摘要——两种形态共用同一出口，records 与 summary 两键恒在
    # （给 symbol 时 summary 为 None，不给时 records 为空数组），形状校验因此单一。
    "sentiment-history": ["records", "summary"],
    # WP8 任务 2：OpenAPI 行情接入进缓存端点的最小字段（官方文档 data 顶层键，与
    # MCP 通道 data 同形）。注意 info_search：MCP 侧 quote_news_search 恒空，归一化为
    # {"news_list": []}（futu_data._fetch_mcp），两通道/形状校验因此同规。
    "info_basicinfo": ["basic_list"],
    "info_trading_days": ["trading_days"],
    "info_search": ["news_list"],
    "info_market_state": ["market_state_list"],
    "quote_history_kline_v2": ["kline_list"],
}

# cache.js:17-18 的两个默认上限
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_FILES = 120

_SAFE_ENDPOINT = re.compile(r"[^a-zA-Z0-9_-]")


def default_dir(home=None):
    """cache.js:29 的目录：``$DSH_HOME/trading-workbench-cache``（``~/.dsh`` 兜底）。

    ``home`` 参数用于测试隔离与嵌入式调用（Node 侧等价物是 ``createTtlCache({dir})``）。
    """
    base = home if home is not None else os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return os.path.join(str(base), "trading-workbench-cache")


def safe_name(key):
    """cache.js:20-24 safeName：端点消毒 + 其余段 sha1 前 16 位。"""
    endpoint, _, rest = str(key).partition("|")
    digest = hashlib.sha1(rest.encode("utf-8")).hexdigest()[:16]
    return f"{_SAFE_ENDPOINT.sub('_', endpoint)}-{digest}.json"


class TtlCache:
    """``createTtlCache`` 的 Python 等价物：内存 + 磁盘两级，任何异常都当未命中。

    与 cache.js 的对应关系：
      * ``read(key, ttl)``  —— cache.js:81-90（内存命中 → 磁盘命中并回填内存）；
      * ``write(key, value, now=None)`` —— cache.js:93-114（内存总是写；可序列化且不超限才落盘）；
      * ``prune()`` —— cache.js:61-77（清 ``.tmp`` 残骸 + 超过 maxFiles 的按 mtime 淘汰最旧）；
      * ``max_bytes`` 是**单条目**上限（cache.js:103 ``Buffer.byteLength(payload) > maxBytes``）：
        超过就只留内存不落盘。cache.js 的磁盘淘汰只按**文件数**（``maxFiles``），这里保持一致。
    """

    def __init__(self, home=None, directory=None, max_files=None, max_bytes=None, now=None):
        self._home = home
        self._directory = directory
        self.max_files = int(max_files) if max_files else DEFAULT_MAX_FILES
        self.max_bytes = int(max_bytes) if max_bytes else DEFAULT_MAX_BYTES
        self._now = now if now is not None else (lambda: time.time() * 1000)
        self._memory = {}
        self._lock = threading.Lock()
        self._disk_ready = False

    @property
    def directory(self):
        return self._directory or default_dir(self._home)

    def _ensure_dir(self):
        """cache.js:35-44 ensureDir：建目录失败后不再反复尝试，一律当未命中。"""
        if self._disk_ready:
            return True
        try:
            os.makedirs(self.directory, exist_ok=True)
            self._disk_ready = True
        except OSError:
            self._disk_ready = False
        return self._disk_ready

    def _read_disk(self, name, ttl_ms, current):
        """cache.js:46-59 readDisk：缺文件/坏 JSON 当未命中；过期删文件后当未命中。"""
        path = os.path.join(self.directory, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError, UnicodeDecodeError):  # UnicodeDecodeError ⊂ ValueError
            return None
        if not isinstance(entry, dict):
            return None
        at = entry.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        if ttl_ms > 0 and current - at >= ttl_ms:
            try:  # 过期文件删不掉也无所谓（cache.js:52）
                os.unlink(path)
            except OSError:
                pass
            return None
        return entry

    def prune(self):
        """cache.js:61-77：清临时残骸，再按 mtime 从旧到新淘汰到 ``max_files`` 以内。"""
        try:
            rows = []
            with os.scandir(self.directory) as scan:
                for item in scan:
                    try:
                        rows.append((item.name, item.stat().st_mtime))
                    except OSError:
                        continue
        except OSError:
            return
        rows.sort(key=lambda row: row[1], reverse=True)
        doomed = [name for name, _ in rows if name.endswith(".tmp")]
        doomed += [name for name, _ in rows if not name.endswith(".tmp")][self.max_files:]
        for name in doomed:
            try:
                os.unlink(os.path.join(self.directory, name))
            except OSError:
                continue

    def read(self, key, ttl_ms, now=None):
        """命中返回条目 ``{value, at}``，否则 ``None``（cache.js:81-90）。ttl<=0 = 不缓存。"""
        if ttl_ms <= 0:
            return None
        current = self._now() if now is None else now
        with self._lock:
            hit = self._memory.get(key)
        if hit is not None:
            if current - hit["at"] < ttl_ms:
                return hit
            with self._lock:
                self._memory.pop(key, None)
        if not self._ensure_dir():
            return None
        entry = self._read_disk(safe_name(key), ttl_ms, current)
        if entry is not None:
            with self._lock:
                self._memory[key] = entry
        return entry

    def write(self, key, value, now=None):
        """写两级缓存并返回条目 ``{value, at}``（cache.js:93-114）。"""
        at = self._now() if now is None else now
        entry = {"value": value, "at": at}
        with self._lock:
            self._memory[key] = entry
        if not self._ensure_dir():
            return entry
        try:
            payload = json.dumps(entry, ensure_ascii=False)
        except (TypeError, ValueError):  # 不可序列化的值只留内存（cache.js:99-101）
            return entry
        if len(payload.encode("utf-8")) > self.max_bytes:  # 大结果只留内存（cache.js:103）
            return entry
        target = os.path.join(self.directory, safe_name(key))
        try:
            # 临时名必须唯一：Host 进程与 CLI/测试共用缓存目录，同名 tmp 会互相截断
            # （cache.js:106-108 的 process.pid + randomBytes）。
            temp = f"{target}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp, target)
            self.prune()
        except OSError:  # 写盘失败不影响返回（cache.js:112）
            pass
        return entry

    def clear_memory(self):
        """cache.js:116 clearMemory。"""
        with self._lock:
            self._memory.clear()


_CACHE = TtlCache()


def configure(home=None, max_files=None, max_bytes=None, now=None):
    """测试/嵌入式：替换模块级缓存实例并返回它（Node 侧对应 ``deps.cache`` 注入）。"""
    global _CACHE
    _CACHE = TtlCache(home=home, max_files=max_files, max_bytes=max_bytes, now=now)
    return _CACHE


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
    entry = _CACHE.read(cache_key(endpoint, payload), ttl_ms, now)
    if entry is None:
        return None
    return entry["at"], entry.get("value")


def write(endpoint, payload, value, now=None):
    """写缓存并返回 ``{"at": ms}``（rpc.js:89 ``cache.write``）。"""
    entry = _CACHE.write(cache_key(endpoint, payload), value, now)
    return {"at": entry["at"]}


def clear():
    """测试/诊断用：清空**内存**层（Node 侧对应 cache.js:116 clearMemory）。

    磁盘条目刻意不清：它们的 TTL 与形状校验仍然生效，且跨进程共享正是磁盘层的目的。
    """
    _CACHE.clear_memory()


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
