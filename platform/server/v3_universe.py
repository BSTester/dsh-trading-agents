"""市场宇宙解析（A 股 ``SH`` / 港股 ``HK`` / 美股 ``US``）——``/api/v3/*`` 的唯一池子来源。

契约（调用方按此实现，见各端点的 ``market=`` 语义）::

    resolve_universe(wb_call, home, market) -> dict | None

    {"market": "SH|HK|US", "tickers": [...], "source": "...", "note": "..."}

优先级（都是**真实来源**，逐级降级；全失败 → ``None``，由调用方如实回
``market/no-universe``——**绝不发明自选池**）:

  1. **配置** ``<home>/trading-platform.json`` 的 ``watchlists.<SH|HK|US>``（存在即用；
     同时兼容旧的顶层 ``watchlist``＝SH）；
  2. **真实持仓** ``account_positions``（工具面 ``positions``，富途只读）：按账户市场
     过滤后取标的；
  3. 都没有 → ``None``。

口径与复用（**不另造一套**）:

  * 账户 → 市场：数值 ``market_id`` 走 ``trading_datasource.market_ids.SIM_MARKET_IDS``
    （与 ``plugins/workbench/python/positions.py``、``server/trading.py`` 同一份常量：
    港股 1 / A 股 3 / 美股 100，A 股三前缀同为 3）；市场链名走
    ``server.v3_quality.MARKET_TRD_CODES`` 的白名单（成交质量模块同一份市场口径）；
  * 标的归一：``trading_datasource.market.to_futu_symbol``（全仓库唯一实现）——裸代码
    必须归一，否则 A 股持仓里的深市代码会被错发成 ``SH.002475``（``series`` 取不到数）；
    该模块不可用时退化为同规则实现（A 股首位 6/9/5/1→SH、0/2/3→SZ、4/8→BJ，见
    ``server/v3_sources.py`` 的 ``A_SHARE_HEAD_MARKET``）。

市场口径：**A 股 = ``SH``**——``SH`` / ``SZ`` / ``BJ`` 三个前缀都归入 ``SH``。
``market`` 缺省 ``SH``（保持既有 A 股口径兼容）；非法取值 → ``None``
（调用方回 ``market/bad-market``，消息为「market 需为 SH / HK / US」）。

频控：进程内 TTL 缓存（缺省 60s，``QUOTE_UNIVERSE_TTL_MS`` 覆盖），失败结果按更短的
负面 TTL 缓存，避免密集调用触发富途 ``-12006``（HTTP 403）。缓存只影响**重复解析**，
不改变来源与条数。

同一模块还给出**市场基准**：``benchmark_for(market, wb_call)`` 按 ``BENCHMARKS`` 的候选
优先级用 ``series`` 逐个实测探测（同一份 TTL 缓存），返回实际可用的基准代码；**全部候选
不可用 → ``None``**，由调用方把 beta/alpha/IR 置空并如实写明候选——绝不拿 A 股基准去算
港/美股组合。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from server import v3_quality

__all__ = [
    "A_SHARE_PREFIXES",
    "BENCHMARKS",
    "CACHE_ENV",
    "DEFAULT_TTL_MS",
    "MARKETS",
    "NEGATIVE_TTL_MS",
    "benchmark_for",
    "benchmark_note",
    "canonical_ticker",
    "clear_cache",
    "market_of_account_label",
    "market_of_ticker",
    "normalize_market",
    "read_config",
    "resolve_universe",
    "universe_note",
]

#: 支持的三个市场口径（A 股统一记 ``SH``）
MARKETS = ("SH", "HK", "US")
#: A 股三个交易所前缀（全部归入 ``SH`` 口径）
A_SHARE_PREFIXES = ("SH", "SZ", "BJ")
#: 可被识别的规范前缀（含 A 股三所）
_KNOWN_PREFIXES = ("SH", "SZ", "BJ", "HK", "US")
#: TTL 缓存缺省 60s；``QUOTE_UNIVERSE_TTL_MS`` 覆盖（0 = 关闭缓存）
DEFAULT_TTL_MS = 60_000
#: 失败结果的负面 TTL（更短：既不空转打上游，又不长时间掩盖恢复）
NEGATIVE_TTL_MS = 15_000
CACHE_ENV = "QUOTE_UNIVERSE_TTL_MS"
#: 配置文件（与 compute.watchlist_symbols / v3_analytics.read_watchlist 同一份）
CONFIG_FILENAME = "trading-platform.json"

#: 各市场基准候选（**优先级从高到低**，逐个用 ``series`` 实测探测；全失败 → ``None``）。
#: 实测可用性（2026-09-20 真机，``futu/quote_history_kline``，30 根日 K）：
#:   ✅ ``SH.000300`` 沪深 300（A 股唯一候选）；``HK.800000`` 恒生指数；
#:   ``HK.800700`` 恒生科技指数；``HK.02800`` 盈富基金 ETF；
#:   ``US.SPY`` 标普 500 ETF（仓库既有代理口径 ``market.INDEX_SYMBOLS.sp500_proxy``）；
#:   ``US..IXIC`` 纳斯达克综合（注意是**双点**写法，与 ``INDEX_SYMBOLS.ixic`` 同源）；
#:   ``US.QQQ`` 纳指 100 ETF；``US..DJI`` 道琼斯（双点写法）。
#:   ❌ ``US.SPX``/``US.NDX``/``US.DJI`` → 富途 ``errcode=-7 invalid symbol``
#:   （仓库既有结论一致：美股指数要用 ETF 或双点代码，``US.SPX`` 不可用）。
#: 美股首选 ``US.SPY``（标普 500 的可用代理，与 A 股/港股首选同为宽基市场基准），
#: 其后依次降级到真实指数与纳指 ETF。
#: **不做跨市场兜底**：某市场候选全不可用时返回 ``None``，绝不用 A 股基准去算别的市场。
BENCHMARKS = {
    "SH": ("SH.000300",),
    "HK": ("HK.800000", "HK.800700", "HK.02800"),
    "US": ("US.SPY", "US..IXIC", "US.QQQ", "US..DJI"),
}

_SIM_IDS = None
_ACCOUNT_IDS = None
_TO_FUTU_SYMBOL = None
_A_SHARE_HEADS = None
_CACHE = {}
_LAST_NOTE = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 市场标识归一
# ---------------------------------------------------------------------------
def normalize_market(value):
    """``market`` 查询参数 → ``SH`` / ``HK`` / ``US``；缺省 ``SH``，非法 → ``None``。

    ``SH`` / ``SZ`` / ``BJ`` 都归一为 ``SH``（A 股同一口径，与
    ``v3_industry.MARKETS``、``trading.CALENDAR_MARKET`` 的合并口径一致）。
    """
    if value is None:
        return "SH"
    if isinstance(value, bool):
        return None
    text = str(value).strip().upper()
    if not text:
        return "SH"
    if text in A_SHARE_PREFIXES:
        return "SH"
    if text in ("HK", "US"):
        return text
    return None


def _collapse(prefix):
    """交易所前缀 → 市场口径（A 股三所 → SH）；认不出 → ``None``。"""
    text = str(prefix or "").strip().upper()
    if text in A_SHARE_PREFIXES:
        return "SH"
    if text in ("HK", "US"):
        return text
    return None


def market_of_ticker(value):
    """标的 → 市场口径（``SH.600000``/``600000.SH``/``SZ.002475`` → ``SH``）。

    纯**市场前缀**口径（与 ``execution``/``oms`` 过滤规则逐字一致：``SH.``/``SZ.``/``BJ.``
    → ``SH``、``HK.`` → ``HK``、``US.`` → ``US``）。裸代码没有市场信息 → ``None``
    （调用方应先用 :func:`canonical_ticker` 归一）。
    """
    text = str(value or "").strip().upper()
    if "." not in text:
        return None
    head, _, tail = text.partition(".")
    if head in _KNOWN_PREFIXES and tail:
        return _collapse(head)
    if tail in _KNOWN_PREFIXES and head:
        return _collapse(tail)
    return None


def _sim_market_ids():
    """模拟账户 ``市场链名 → market_id``（唯一实现：trading_datasource.market_ids）。"""
    global _SIM_IDS
    if _SIM_IDS is None:
        mapping = {}
        try:  # 纯常量模块，零依赖（market_ids 的模块 docstring 明确保证）
            from trading_datasource.market_ids import SIM_MARKET_IDS
            mapping = {str(key).upper(): value for key, value in dict(SIM_MARKET_IDS).items()}
        except Exception:  # noqa: BLE001 —— 插件路径不可用时退回 trading 的同源常量
            try:
                from server.trading import SIM_MARKET_CHAIN
                mapping = {str(name).upper(): int(mid) for mid, name in SIM_MARKET_CHAIN.items()}
            except Exception:  # noqa: BLE001 —— 两份都不可用：数字市场一律「未知」，不猜
                mapping = {}
        _SIM_IDS = mapping
    return _SIM_IDS


def _account_market_ids():
    """``market_id`` → 市场口径（1→HK / 3→SH / 100→US）；未登记的数字 → 不在表内。"""
    global _ACCOUNT_IDS
    if _ACCOUNT_IDS is None:
        reverse = {}
        for name, mid in _sim_market_ids().items():
            code = _collapse(name)
            if code and mid not in reverse:
                reverse[mid] = code
        _ACCOUNT_IDS = reverse
    return _ACCOUNT_IDS


def market_of_account_label(value):
    """账户/分组声明的市场标识 → 市场口径；认不出 → ``None``（不猜）。

    接受三种真实写法：数值 ``market_id``（``positions`` 的分组，1/3/100）、
    数字字符串（``orders_open``/``deals_today`` 里未登记的市场，如 ``"9"``）、
    市场链名（``"HK"``/``"SH"``/``"US"``；``"SZ"``/``"BJ"`` 归入 ``SH``）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not float(value).is_integer():
            return None
        return _account_market_ids().get(int(value))
    text = str(value).strip().upper()
    if not text:
        return None
    if text.isdigit():
        return _account_market_ids().get(int(text))
    if text in v3_quality.MARKET_TRD_CODES:  # 成交质量模块的同一份市场白名单
        return _collapse(text)
    return None


# ---------------------------------------------------------------------------
# 标的归一
# ---------------------------------------------------------------------------
def _to_futu_symbol(text):
    """``trading_datasource.market.to_futu_symbol``（全仓库唯一实现）；不可用 → ``None``。"""
    global _TO_FUTU_SYMBOL
    if _TO_FUTU_SYMBOL is None:
        try:
            from trading_datasource.market import to_futu_symbol
            _TO_FUTU_SYMBOL = to_futu_symbol
        except Exception:  # noqa: BLE001 —— 离线/精简环境：走下面的同规则实现
            _TO_FUTU_SYMBOL = False
    if _TO_FUTU_SYMBOL is False:
        return None
    try:
        return str(_TO_FUTU_SYMBOL(text))
    except Exception:  # noqa: BLE001
        return None


def _a_share_heads():
    """裸 6 位 A 股代码首位 → 交易所（复用 ``server.v3_sources.A_SHARE_HEAD_MARKET``）。"""
    global _A_SHARE_HEADS
    if _A_SHARE_HEADS is None:
        try:
            from server.v3_sources import A_SHARE_HEAD_MARKET
            _A_SHARE_HEADS = {str(key): str(value) for key, value in A_SHARE_HEAD_MARKET.items()}
        except Exception:  # noqa: BLE001
            _A_SHARE_HEADS = {"6": "SH", "9": "SH", "5": "SH", "1": "SH",
                              "0": "SZ", "2": "SZ", "3": "SZ", "4": "BJ", "8": "BJ"}
    return _A_SHARE_HEADS


def _infer_symbol(text):
    """裸代码 → ``MARKET.CODE``；无法判定 → ``None``（不猜市场）。"""
    canonical = _to_futu_symbol(text)
    if canonical and "." in canonical:
        return canonical
    if text.isdigit():
        if len(text) == 6:  # A 股（公开编码规则）
            return f"{_a_share_heads().get(text[0], 'SZ')}.{text}"
        if len(text) <= 5:  # 港股（1~5 位，补零到 5 位）
            return f"HK.{text.zfill(5)}"
        return None
    if re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", text):  # 美股（字母开头的代码）
        return f"US.{text}"
    return None


def canonical_ticker(value, group_market=None):
    """标的 → 规范的富途写法 ``MARKET.CODE``；无法归一 → ``None``。

    * ``SH.600000`` / ``600000.SH`` 原样或翻正；
    * 裸代码按 ``_infer_symbol`` 推断（A 股按首位分 SH/SZ/BJ，港股补零到 5 位，其余按美股）；
    * ``group_market`` 是账户/分组声明的市场（``"HK"`` 等），仅用于**消歧**：
      分组明确是港股而代码是数字时按港股补零，避免 6 位以下数字被误判。
    """
    text = str(value or "").strip().upper()
    if not text:
        return None
    if "." in text:
        head, _, tail = text.partition(".")
        if head in _KNOWN_PREFIXES and tail:
            return f"{head}.{tail}"
        if tail in _KNOWN_PREFIXES and head:
            return f"{tail}.{head}"
        return None
    if group_market == "HK" and text.isdigit() and len(text) <= 5:
        return f"HK.{text.zfill(5)}"
    if group_market == "US" and not text.isdigit():
        return _infer_symbol(text)
    return _infer_symbol(text)


# ---------------------------------------------------------------------------
# 配置 / 持仓两级来源
# ---------------------------------------------------------------------------
def home_path(home):
    """``home`` 归一成路径（与 ``v3_analytics._home_path`` 同口径）。"""
    if home in (None, ""):
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return Path(str(home)).expanduser()


def read_config(home):
    """``<home>/trading-platform.json`` → dict；缺失/坏 JSON → ``None``（不抛错）。"""
    try:
        raw = json.loads((home_path(home) / CONFIG_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _config_pool(raw, market):
    """配置里该市场的原始池子 → ``(items, source)``；没有该市场的配置 → ``(None, None)``。"""
    if not isinstance(raw, dict):
        return None, None
    watchlists = raw.get("watchlists")
    if isinstance(watchlists, dict):
        for key, value in watchlists.items():
            if str(key).strip().upper() == market and isinstance(value, list):
                return value, f"config/{CONFIG_FILENAME}#watchlists.{market}"
    if market == "SH" and isinstance(raw.get("watchlist"), list):
        # 旧顶层键＝A 股口径（历史兼容）；仍按市场过滤，不把港股/美股条目混进 SH
        return raw["watchlist"], f"config/{CONFIG_FILENAME}#watchlist"
    return None, None


def _clean_pool(items, market, group_market=None):
    """原始条目 → ``(tickers, dropped, unresolved)``：按市场过滤 + 归一 + 去重保序。"""
    tickers, dropped, unresolved = [], [], []
    for item in items or []:
        raw = str(item or "").strip()
        if not raw:
            continue
        ticker = canonical_ticker(raw, group_market)
        if ticker is None:
            unresolved.append(raw)
            continue
        if market_of_ticker(ticker) != market:
            dropped.append(ticker)
            continue
        if ticker not in tickers:
            tickers.append(ticker)
    return tickers, dropped, unresolved


def _positions_universe(wb_call, home, market):
    """第 2 级：真实持仓（工具面 ``positions``，富途只读）→ ``(tickers, source, note)``。"""
    try:
        envelope = wb_call("positions", {})
    except Exception as error:  # noqa: BLE001 —— 与工具面口径一致：失败进原因，不抛
        return None, None, f"真实持仓不可读（positions 调用异常：{type(error).__name__}: {error}）"
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        code = message = None
        if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
            code = envelope["error"].get("code")
            message = envelope["error"].get("message")
        return None, None, (f"真实持仓不可读（positions 取数失败：{code or 'unknown'}："
                            f"{str(message or '无 error 明细')[:160]}）")
    value = envelope.get("value") if isinstance(envelope.get("value"), dict) else {}
    source = value.get("source") if isinstance(value.get("source"), str) and value.get("source") \
        else "futu/account_positions"
    groups = [group for group in (value.get("groups") or []) if isinstance(group, dict)]
    if not groups:
        return None, None, f"真实持仓为空（{source}：无账户分组）"
    tickers, other_accounts, unknown_accounts = [], [], []
    for group in groups:
        raw_label = group.get("market")
        declared = raw_label not in (None, "")
        label = market_of_account_label(raw_label)
        rows = [row for row in (group.get("positions") or []) if isinstance(row, dict)]
        if declared and label is None:
            # 上游声明了市场但不在已知口径内（如 market_id 9/10/11/12/13/16）：
            # **不猜**，如实排除并计数（父任务：过滤时如实排除并计数）。
            unknown_accounts.append(str(raw_label))
            continue
        if label is None:
            # 账户**没有**市场声明（实盘账户 market=null）：退化为逐行看标的自身的市场前缀
            for row in rows:
                symbol = row.get("code") or row.get("symbol") or row.get("ticker")
                ticker = canonical_ticker(symbol)
                if ticker is None:
                    unknown_accounts.append(str(group.get("acc_id") or group.get("account") or "?"))
                    continue
                if market_of_ticker(ticker) == market and ticker not in tickers:
                    tickers.append(ticker)
            continue
        if label != market:
            other_accounts.append(label)
            continue
        for row in rows:
            symbol = row.get("code") or row.get("symbol") or row.get("ticker")
            ticker = canonical_ticker(symbol, label)
            if ticker is None or market_of_ticker(ticker) != market:
                continue
            if ticker not in tickers:
                tickers.append(ticker)
    bits = [f"真实持仓（{source}）：{market} 账户 {len(tickers)} 只标的"]
    if other_accounts:
        bits.append(f"另有 {len(other_accounts)} 个账户属于其它市场（{sorted(set(other_accounts))}）")
    if unknown_accounts:
        bits.append(f"{len(unknown_accounts)} 个账户的市场标识无法归入 SH/HK/US（如 "
                    f"{sorted(set(unknown_accounts))[:3]}）→ 未计入，不猜")
    if not tickers:
        bits.append(f"该市场（{market}）账户没有可取的真实持仓")
    return tickers or None, f"{source}#{market}", "；".join(bits)


# ---------------------------------------------------------------------------
# TTL 缓存
# ---------------------------------------------------------------------------
def _ttl_ms(override=None):
    if override is not None:
        try:
            return max(0, int(override))
        except (TypeError, ValueError):
            return DEFAULT_TTL_MS
    raw = os.environ.get(CACHE_ENV)
    if raw in (None, ""):
        return DEFAULT_TTL_MS
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return DEFAULT_TTL_MS


def _now_ms(clock=None):
    return float(clock() if clock else time.monotonic()) * 1000.0


def clear_cache(home=None, market=None):
    """清 TTL 缓存（测试/运维用）。不给参数 → 全清。"""
    with _LOCK:
        if home is None and market is None:
            _CACHE.clear()
            _LAST_NOTE.clear()
            return
        key = (str(home_path(home)), normalize_market(market))
        _CACHE.pop(key, None)
        _LAST_NOTE.pop(key, None)


def _cache_get(key, ttl, now):
    """命中未过期的缓存条目 → ``entry``；否则 ``None``。"""
    if ttl <= 0:
        return None
    with _LOCK:
        entry = _CACHE.get(key)
    if entry is None or now >= entry["expires"]:
        return None
    return entry


def _cache_put(key, ttl, now, payload, note):
    """写缓存（失败结果用更短的负面 TTL）并记下最后说明。"""
    if ttl > 0:
        window = ttl if payload is not None else min(ttl, NEGATIVE_TTL_MS)
        with _LOCK:
            _CACHE[key] = {"expires": now + window, "payload": payload, "note": note}
    with _LOCK:
        _LAST_NOTE[key] = note


def _cache_hit_note(note, ttl):
    return f"{note}；命中进程内 TTL 缓存（{CACHE_ENV}＝{ttl}ms）"


def universe_note(home, market=None):
    """最近一次解析该市场的说明（含失败原因）；从未解析过 → ``None``。

    调用方用它给出 ``market/no-universe`` 的 ``detail``——错误消息按契约固定，
    真实原因（配置缺失 / positions 上游报错原文）不能丢。
    """
    key = (str(home_path(home)), normalize_market(market))
    with _LOCK:
        return _LAST_NOTE.get(key)


def benchmark_note(market):
    """最近一次解析该市场基准的说明（含候选探测的真实结果）；未解析过 → ``None``。"""
    key = ("benchmark", normalize_market(market))
    with _LOCK:
        return _LAST_NOTE.get(key)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _resolve_uncached(wb_call, home, market):
    """真正解析一次：``(payload|None, note)``。"""
    raw = read_config(home)
    items, source = _config_pool(raw, market)
    if items is not None:
        tickers, dropped, unresolved = _clean_pool(items, market)
        bits = [f"自选池取自 {source}，共 {len(tickers)} 只"]
        if dropped:
            bits.append(f"已剔除 {len(dropped)} 只非本市场标的（{dropped[:3]}）")
        if unresolved:
            bits.append(f"{len(unresolved)} 条无法识别市场（{unresolved[:3]}）→ 未计入，不猜")
        if tickers:
            return {"market": market, "tickers": tickers, "source": source,
                    "note": "；".join(bits)}, "；".join(bits)
        bits.append("该键存在但过滤后为空 → 继续看真实持仓")
        config_note = "；".join(bits)
    else:
        reason = (f"配置里没有 watchlists.{market}"
                  + ("（也无旧的顶层 watchlist 键）" if market == "SH" else ""))
        config_note = f"未使用配置池：{reason}"

    tickers, source, positions_note = _positions_universe(wb_call, home, market)
    if tickers:
        note = f"{config_note}；{positions_note}"
        return {"market": market, "tickers": tickers, "source": source,
                "note": note}, note
    note = f"{config_note}；{positions_note}"
    return None, note


def resolve_universe(wb_call, home, market=None, *, ttl_ms=None, clock=None):
    """该市场的标的宇宙（契约见模块 docstring）；无真实来源 → ``None``。

    ``wb_call(name, payload)`` 是注入的工具面调用器（同步，返回原始信封）。
    ``ttl_ms`` / ``clock`` 仅供测试注入（``clock`` 返回秒，缺省 ``time.monotonic``）。
    """
    code = normalize_market(market)
    if code is None:
        return None
    key = (str(home_path(home)), code)
    ttl = _ttl_ms(ttl_ms)
    now = _now_ms(clock)
    entry = _cache_get(key, ttl, now)
    if entry is not None:
        payload = entry["payload"]
        note = _cache_hit_note(entry["note"], ttl)
        with _LOCK:
            _LAST_NOTE[key] = note
        if payload is None:
            return None
        return {**payload, "note": note}

    payload, note = _resolve_uncached(wb_call, home, code)
    _cache_put(key, ttl, now, payload, note)
    return payload


def benchmark_for(market, wb_call, *, ttl_ms=None, clock=None, limit=250):
    """该市场**实测可用**的基准：``{"market","ticker","source","note"} | None``。

    按 ``BENCHMARKS[market]`` 的优先级逐个用工具面 ``series`` 探测（日 K ≥2 根才算可用），
    结果进程内 TTL 缓存（与 :func:`resolve_universe` 同一份缓存与 ``QUOTE_UNIVERSE_TTL_MS``）。
    **全部候选都取不到 → ``None``**（调用方应把 beta/alpha/IR 置空并如实写明候选，
    绝不用别的市场的基准硬算）。探测失败的真实原因（含富途 ``-12006`` 频控原文）进 ``note``。
    """
    code = normalize_market(market)
    if code is None or code not in BENCHMARKS:
        return None
    key = ("benchmark", code)
    ttl = _ttl_ms(ttl_ms)
    now = _now_ms(clock)
    entry = _cache_get(key, ttl, now)
    if entry is not None:
        payload = entry["payload"]
        note = _cache_hit_note(entry["note"], ttl)
        with _LOCK:
            _LAST_NOTE[key] = note
        if payload is None:
            return None
        return {**payload, "note": note}

    candidates = BENCHMARKS[code]
    attempts = []
    found = None
    for ticker in candidates:
        try:
            envelope = wb_call("series", {"ticker": ticker, "period": "1d", "limit": limit})
        except Exception as error:  # noqa: BLE001 —— 与工具面口径一致：失败进 attempts
            attempts.append(f"{ticker}✗({type(error).__name__}: {error})")
            continue
        if isinstance(envelope, dict) and envelope.get("ok"):
            value = envelope.get("value") if isinstance(envelope.get("value"), dict) else {}
            bars = value.get("bars") if isinstance(value.get("bars"), list) else []
            if len(bars) >= 2:
                found = {"market": code, "ticker": ticker,
                         "source": value.get("source") or "futu/quote_history_kline",
                         "note": ""}
                break
            attempts.append(f"{ticker}✗(日 K 仅 {len(bars)} 根)")
            continue
        error = (envelope or {}).get("error") if isinstance(envelope, dict) else None
        code_text = (error or {}).get("code") or "unknown"
        message = str((error or {}).get("message") or "上游返回 ok=false")[:120]
        attempts.append(f"{ticker}✗({code_text}：{message})")

    if found is None:
        note = (f"该市场（{code}）基准不可用：候选 {'/'.join(candidates)} 逐个探测均失败（"
                f"{'；'.join(attempts)}）→ beta/alpha/ir 置空，不拿别的市场基准硬算")
        _cache_put(key, ttl, now, None, note)
        return None
    note = (f"基准 {found['ticker']}（{code} 市场首选/降级命中，来源 {found['source']}）"
            + (f"；此前失败候选：{'；'.join(attempts)}" if attempts else ""))
    found["note"] = note
    _cache_put(key, ttl, now, found, note)
    return found
