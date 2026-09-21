"""V3 扩展数据源（**免密钥公开端点**）：A 股实时行情/盘口公开降级链、北向资金、宏观经济。

数据源政策（2026-09-21，与仓库「数据诚实」纪律一致）
====================================================

除富途（授权使用）外，本模块引用的每一个数据渠道都是**免密钥公开端点**：

  * A 股实时行情/盘口降级链：东方财富 ``push2`` → 腾讯 ``qt.gtimg.cn`` → 新浪 ``hq.sinajs.cn``
    （接进既有 ``rt_quote`` / ``rt_order_book`` 链路：**只在富途返回 -9（A 股无实时权限）**
    时启用；HK/US 行为一字不变——降级不是替换）。三家 2026-09-21 实测全部可用：
    单请求 62~210ms；突发 >2~3 req/s 东财会以 ``RemoteDisconnected`` 断连（腾讯/新浪
    8 连发全通过）；**python-urllib 的客户端指纹会被东财 WAF 直接拒连**（requests/httpx
    正常），因此文本/JSON 取数一律走 httpx（缺库才退 urllib，东财源失败不重试）。
  * 北向资金：AKShare ``stock_hsgt_fund_flow_summary_em``（当日四方板块）+
    ``stock_hsgt_hist_em``（沪/深股通历史序列）。**披露事实**：沪深港交易所自
    2024-08-19 起不再披露北向当日净买入（实测历史序列最后非空日期 2024-08-16），
    summary 里北向板块的 0 是上游占位值——响应里以 ``net_buy_disclosed=false`` 如实
    标注，**绝不把 0 当真实净买额**。
  * 宏观经济：AKShare macro 五个核心指标（CPI/PPI/PMI/社融/M2，函数名以本机
    akshare 1.18.96 实测存在为准），数值**原样透传**，不加工、不平滑。

设计约束（与 ``v3_sources`` 同一套纪律）：

  * **绝不伪造**：盘口公开源只有五档，就如实给五档并写明「该源无十档/逐笔能力」；
    取不到的档位省略（上游给 ``"-"`` 时绝不填 0）；
  * **所有外部调用都带超时**；东财源失败**不重试**（重试只会加重断连），立即换下一家；
  * **失败走既有降级链语义**（``v3_fallback.run_chain``：每一级 ``{source,ok,ms,error}``），
    公开链也全失败时把**原始 -9 错误原样上抛**（``futu_error`` 保留在成功响应里）；
  * **持久化/惰性**：``register()`` 不 import akshare/pandas；一切三方 import 都在
    请求路径内按需发生，且可注入假模块/假 fetch（测试封网络）。

本模块由 ``v3_sources.register`` 末尾**追加装配**（app.py 的模块接线清单是封闭的），
共享同一份 ``deps``；另向 ``v3_fallback`` 的状态探测登记扩展链（``rt_quote`` /
``rt_order_book`` / ``northbound`` / ``macro``，仅 ``?keys=` 显式请求时探测，缺省
探测集保持既有 8 条不变），从而复用 ``DataSourceChainUnavailable`` 等既有告警语义。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

from server import v3_sources
from server.v3_sources import (
    Deps,
    _akshare_call,
    _akshare_failure_message,
    _error_text,
    #: 2026-09-21 修：``_head`` 此前漏在 import 清单外 → ``/api/v3/northbound`` 整条
    #: 路由 NameError 500（北向历史序列截断用）。定义在 ``v3_sources.py``。
    _head,
    _rows_from_frame,
    akshare_module,
    as_number,
    as_text,
    envelope_error,
    now_iso,
    to_int,
)

# ── A 股公开行情：三源定义 ──────────────────────────────────────────────────────

#: 单源超时（秒）：降级链的意义是快，单源挂了尽快换下一家。
QUOTE_TIMEOUT = 5.0
#: 源失败后的进程内冷却（秒）：东财对突发请求会断连，冷却避免平台流量把它打死。
QUOTE_COOLDOWN_S = 60.0
#: 公开行情降级链的墙钟预算（秒）：三家各一次，超预算的剩余源记 skipped。
QUOTE_CHAIN_TIMEOUT = 20.0

#: 公开源的延时口径（实测口径见模块 docstring；逐字进响应 ``delay`` 字段）。
DELAY_EASTMONEY = "实时（免费 L1 快照；东财 push2，响应含行情时间戳 f86）"
DELAY_TENCENT = "实时（免费 L1 快照；腾讯 qt.gtimg.cn，字段内含行情时间）"
DELAY_SINA = "实时（免费 L1 快照；新浪 hq.sinajs.cn，字段内含行情时间）"

#: A 股公开行情链（顺序 = 先东财后腾讯后新浪）。降级不是替换：只服务富途 -9 的 A 股请求。
PUBLIC_QUOTE_SOURCES = ("eastmoney/push2", "tencent/qt.gtimg.cn", "sina/hq.sinajs.cn")

#: 公开源只覆盖沪/深（北交所前缀未在三家实测核实，不猜映射 → 如实拒绝）。
A_SHARE_PUBLIC_MARKETS = ("SH", "SZ")

_CNST = timezone(timedelta(hours=8))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# 进程内冷却表：{source_name: cooldown_deadline(monotonic)}。测试可直接清空/注入。
_COOLDOWN: dict = {}


def _cooling(source):
    deadline = _COOLDOWN.get(source)
    return deadline is not None and time.monotonic() < deadline


def _mark_cool(source):
    _COOLDOWN[source] = time.monotonic() + QUOTE_COOLDOWN_S


def a_share_public_code(ticker):
    """``SH.600519`` / ``600519.SH`` / ``sh600519`` → ``("SH", "600519")``；公开链不支持 → ``None``。

    2026-09-21 修：docstring 一直声称支持紧凑形态 ``sh600519``，但 ``v3_sources.detect_market``
    对它返回 ``""``（实测）→ 此前统一回 ``None``（路由报 ``quote/unsupported-market``，不会写错
    数据，但能力与文档不符）。这里**先补紧凑前缀映射**（``sh``/``sz`` + 6 位数字，大小写不敏感），
    其余形态仍交 ``detect_market``，行为对既有形态零变化。
    """
    text = v3_sources.as_text(ticker).strip().lower()
    if len(text) == 8 and text[:2] in ("sh", "sz") and text[2:].isdigit():
        return text[:2].upper(), text[2:]
    market = v3_sources.detect_market(ticker)
    if market not in A_SHARE_PUBLIC_MARKETS:
        return None
    code = v3_sources._bare_code(ticker)
    if not (code.isdigit() and len(code) == 6):
        return None
    return market, code


def _iso_cn_from_epoch(seconds):
    """东财 ``f86``（秒级 Unix）→ 北京时区 ISO；非法返回 ``None``。"""
    number = as_number(seconds)
    if number is None or number <= 0:
        return None
    try:
        return (_EPOCH + timedelta(seconds=number)).astimezone(_CNST).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _iso_cn_from_compact(text):
    """腾讯 ``YYYYMMDDHHMMSS`` → 北京时区 ISO；非法返回 ``None``。"""
    raw = as_text(text).strip()
    if not re.fullmatch(r"\d{14}", raw):
        return None
    try:
        moment = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=_CNST)
    except ValueError:
        return None
    return moment.isoformat()


def _iso_cn_from_date_time(date_text, time_text):
    """新浪 ``2026-09-21`` + ``15:34:59`` → 北京时区 ISO；非法返回 ``None``。"""
    raw = f"{as_text(date_text).strip()} {as_text(time_text).strip()}"
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CNST).isoformat()
    except ValueError:
        return None


def _book(levels, *, from_shares=False):
    """``[(price, volume), ...]`` → ``[[price, volume], ...]``；非数值档位**整档省略**（不填 0）。"""
    out = []
    for price, volume in levels:
        price_n = as_number(price)
        volume_n = as_number(volume)
        if price_n is None or volume_n is None:
            continue  # 上游 ``"-"``/空档：省略，不伪造
        out.append([price_n, volume_n / 100.0 if from_shares else volume_n])
    return out


def _payload(source, delay, quote_time, code, market, name, quote, bid, ask, volume_unit="手"):
    """三源归一化的成功载荷（``book`` 允许为空档——盘口请求方按 ``depth`` 判断）。"""
    return {
        "ok": True,
        "source": source,
        "delay": delay,
        "as_of": now_iso(),
        "quote_time": quote_time,
        "code": code,
        "market": market,
        "name": name,
        "quote": quote,
        "book": {"bid": bid, "ask": ask, "depth": min(len(bid), len(ask)),
                 "volume_unit": volume_unit},
    }


# ── HTTP（文本口：腾讯/新浪返回 GBK 文本，不是 JSON）────────────────────────────


def _http_text(url, headers=None, encoding="gbk", timeout=None):
    """GET → 文本（``{ok,value}`` / ``{ok:false,error}``，**永不抛异常**）。"""
    headers = dict(headers or {})
    timeout = float(timeout or v3_sources.DEFAULT_TIMEOUT)
    try:
        try:
            import httpx
        except ImportError:
            import urllib.request

            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
            return {"ok": True, "value": raw.decode(encoding, "replace")}
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
            response = client.get(url, headers=headers)
        if response.status_code != 200:
            return envelope_error("quote/http", f"{url} → HTTP {response.status_code}")
        return {"ok": True, "value": response.content.decode(encoding, "replace")}
    except Exception as error:  # noqa: BLE001 —— 失败进 error 信封（与 v3_sources 同口径）
        return envelope_error("quote/network", f"{url} → {_error_text(error)}")


class ExtDeps:
    """``v3_sources.Deps`` + **文本**取数口（腾讯/新浪是 GBK 文本端点）。

    ``base`` 是共享的 ``v3_sources.Deps``（akshare/JSON/env 全走它）；
    ``text_get`` 注入即替掉真实网络（测试封网络口径）。
    """

    def __init__(self, base, text_get=None):
        self.base = base
        self._text_get = text_get

    # 透传 base 的既有能力（akshare 模块、json_get、env、timeout…）
    def __getattr__(self, name):
        return getattr(self.base, name)

    def text_get(self, url, headers=None, encoding="gbk", timeout=None):
        if self._text_get is not None:
            try:
                return self._text_get(url, headers, encoding, timeout or self.base.timeout)
            except Exception as error:  # noqa: BLE001 —— 注入的假 fetch 抛错也走信封
                return envelope_error("quote/network", f"{url} → {_error_text(error)}")
        return _http_text(url, headers=headers, encoding=encoding,
                          timeout=timeout or self.base.timeout)


def _json_get(deps, url, headers=None):
    return deps.base.json_get(url, headers=headers, timeout=QUOTE_TIMEOUT)


def _text_get(deps, url, headers=None):
    return deps.text_get(url, headers=headers, encoding="gbk", timeout=QUOTE_TIMEOUT)


# ── 三源实现（每源一次请求，失败不重试，真实错误原样带回）───────────────────────

_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
#: 东财 push2 字段：报价（f43 现价 / f60 昨收 / f46 开 / f44 高 / f45 低 / f47 量(手) /
#: f48 额 / f50 量比 / f169 涨跌额 / f170 涨跌幅 / f86 行情时间戳）+ 五档盘口
#: （买一~买五 = f19/f20, f17/f18, f15/f16, f13/f14, f11/f12；卖一~卖五 = f39/f40 …
#: f31/f32；量单位手）。字段语义与 ``akshare.stock_bid_ask_em`` 的映射一致。
_EM_FIELDS = ("f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f169,f170,f171,"
              "f11,f12,f13,f14,f15,f16,f17,f18,f19,f20,"
              "f31,f32,f33,f34,f35,f36,f37,f38,f39,f40")


def _quote_eastmoney(deps, market, code):
    """东方财富 ``push2``：单请求取报价 + 五档。**失败不重试**（重试会加重断连）。"""
    secid_market = {"SH": "1", "SZ": "0"}.get(market)
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid_market}.{code}"
           f"&fields={_EM_FIELDS}&invt=2&fltt=2")
    result = _json_get(deps, url, headers=_EM_HEADERS)
    if not result.get("ok"):
        _mark_cool("eastmoney/push2")
        return result
    data = result.get("value")
    data = data.get("data") if isinstance(data, dict) else None
    if not isinstance(data, dict) or as_number(data.get("f43")) is None:
        # 上游对缺档/无权限标的会回 ``data:null`` 或空对象：如实当失败（不猜字段）
        _mark_cool("eastmoney/push2")
        return envelope_error("eastmoney/push2", f"push2 未返回 {market}.{code} 的报价数据（data={data!r}）")
    quote = {
        "price": as_number(data.get("f43")),
        "prev_close": as_number(data.get("f60")),
        "open": as_number(data.get("f46")),
        "high": as_number(data.get("f44")),
        "low": as_number(data.get("f45")),
        "volume": as_number(data.get("f47")),        # 手
        "turnover": as_number(data.get("f48")),      # 元
        "volume_ratio": as_number(data.get("f50")),
        "change": as_number(data.get("f169")),
        "change_pct": as_number(data.get("f170")),
        "amplitude_pct": as_number(data.get("f171")),
    }
    bid = _book([(data.get("f19"), data.get("f20")), (data.get("f17"), data.get("f18")),
                 (data.get("f15"), data.get("f16")), (data.get("f13"), data.get("f14")),
                 (data.get("f11"), data.get("f12"))])
    ask = _book([(data.get("f39"), data.get("f40")), (data.get("f37"), data.get("f38")),
                 (data.get("f35"), data.get("f36")), (data.get("f33"), data.get("f34")),
                 (data.get("f31"), data.get("f32"))])
    return _payload("eastmoney/push2", DELAY_EASTMONEY,
                    _iso_cn_from_epoch(data.get("f86")), code, market,
                    as_text(data.get("f58")), quote, bid, ask)


def _quote_tencent(deps, market, code):
    """腾讯 ``qt.gtimg.cn``：GBK 文本 + ``~`` 分隔；盘口字段 9~28（价,量 交替，量单位手）。"""
    prefix = market.lower()
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    result = _text_get(deps, url)
    if not result.get("ok"):
        _mark_cool("tencent/qt.gtimg.cn")
        return result
    text = as_text(result.get("value"))
    match = re.search(r'="([^"]*)"', text)
    if not match:
        _mark_cool("tencent/qt.gtimg.cn")
        return envelope_error("tencent/qt.gtimg.cn", f"腾讯响应无法解析：{text[:120]!r}")
    parts = match.group(1).split("~")
    if len(parts) < 50 or not as_number(parts[3] if len(parts) > 3 else None):
        _mark_cool("tencent/qt.gtimg.cn")
        return envelope_error("tencent/qt.gtimg.cn",
                              f"腾讯响应字段不足（{len(parts)} 段），无法解析 {market}.{code}")
    quote = {
        "price": as_number(parts[3]),
        "prev_close": as_number(parts[4]),
        "open": as_number(parts[5]),
        "volume": as_number(parts[6]),               # 手
        "turnover": (as_number(parts[37]) or 0.0) * 10000.0 if as_number(parts[37]) is not None else None,
        "change": as_number(parts[31]),
        "change_pct": as_number(parts[32]),
        "high": as_number(parts[33]),
        "low": as_number(parts[34]),
        "turnover_rate_pct": as_number(parts[38]),
        "pe_ttm": as_number(parts[39]),
        "amplitude_pct": as_number(parts[43]),
        "float_market_cap": as_number(parts[44]),    # 亿
        "market_cap": as_number(parts[45]),          # 亿
        "pb": as_number(parts[46]),
        "volume_ratio": as_number(parts[49]),
    }
    bid = _book([(parts[9], parts[10]), (parts[11], parts[12]), (parts[13], parts[14]),
                 (parts[15], parts[16]), (parts[17], parts[18])])
    ask = _book([(parts[19], parts[20]), (parts[21], parts[22]), (parts[23], parts[24]),
                 (parts[25], parts[26]), (parts[27], parts[28])])
    return _payload("tencent/qt.gtimg.cn", DELAY_TENCENT,
                    _iso_cn_from_compact(parts[30]), code, market,
                    as_text(parts[1]), quote, bid, ask)


def _quote_sina(deps, market, code):
    """新浪 ``hq.sinajs.cn``：GBK 文本 + 逗号分隔（**必须带 Referer**，否则 403）。

    新浪的量字段单位是**股**（成交量与盘口档量），这里换算成手（/100），
    使三源的 ``volume``/档量同单位可对照。
    """
    prefix = market.lower()
    url = f"https://hq.sinajs.cn/list={prefix}{code}"
    headers = {"User-Agent": _EM_HEADERS["User-Agent"],
               "Referer": "https://finance.sina.com.cn/"}
    result = _text_get(deps, url, headers=headers)
    if not result.get("ok"):
        _mark_cool("sina/hq.sinajs.cn")
        return result
    match = re.search(r'="([^"]*)"', as_text(result.get("value")))
    if not match:
        _mark_cool("sina/hq.sinajs.cn")
        return envelope_error("sina/hq.sinajs.cn",
                              f"新浪响应无法解析：{as_text(result.get('value'))[:120]!r}")
    parts = match.group(1).split(",")
    if len(parts) < 32 or not as_number(parts[3]):
        _mark_cool("sina/hq.sinajs.cn")
        return envelope_error("sina/hq.sinajs.cn",
                              f"新浪响应字段不足（{len(parts)} 段），无法解析 {market}.{code}")
    volume_hand = as_number(parts[8])
    quote = {
        "price": as_number(parts[3]),
        "prev_close": as_number(parts[2]),
        "open": as_number(parts[1]),
        "high": as_number(parts[4]),
        "low": as_number(parts[5]),
        "volume": volume_hand / 100.0 if volume_hand is not None else None,  # 股 → 手
        "turnover": as_number(parts[9]),
        "change": None,  # 新浪该形态无涨跌额字段：不给就是不给（不从价差推算）
        "change_pct": None,
    }
    bid = _book([(parts[11], parts[10]), (parts[13], parts[12]), (parts[15], parts[14]),
                 (parts[17], parts[16]), (parts[19], parts[18])], from_shares=True)
    ask = _book([(parts[21], parts[20]), (parts[23], parts[22]), (parts[25], parts[24]),
                 (parts[27], parts[26]), (parts[29], parts[28])], from_shares=True)
    return _payload("sina/hq.sinajs.cn", DELAY_SINA,
                    _iso_cn_from_date_time(parts[30], parts[31]), code, market,
                    as_text(parts[0]), quote, bid, ask)


#: 源名 → 实现（``PUBLIC_QUOTE_SOURCES`` 是链顺序的唯一事实来源）。
_QUOTE_FETCHERS = {
    "eastmoney/push2": _quote_eastmoney,
    "tencent/qt.gtimg.cn": _quote_tencent,
    "sina/hq.sinajs.cn": _quote_sina,
}


def _quote_chain_attempts(deps, market, code, want_book):
    """构造 ``(name, callable)`` 链：冷却中的源跳过（记 skipped，不假装试过）。"""
    chain = []
    for source in PUBLIC_QUOTE_SOURCES:
        fetcher = _QUOTE_FETCHERS[source]

        def link(fetcher=fetcher, source=source):
            payload = fetcher(deps, market, code)
            if want_book and payload.get("ok") and payload.get("book", {}).get("depth", 0) == 0:
                # 盘口请求遇到「无档位」：算该源失败（对盘口语义不可用），继续降级
                return envelope_error(source, "该源本次未返回任何有效档位")
            return payload

        if _cooling(source):
            # 冷却中：显式记 skipped（与 run_chain 的 timeout-skipped 同语义），不发请求
            chain.append((source, lambda source=source: envelope_error(
                "quote/cooldown",
                f"{source} 刚失败过，冷却 {QUOTE_COOLDOWN_S:.0f}s 内跳过（避免加重上游断连）")))
        else:
            chain.append((source, link))
    return chain


def fetch_a_share_quote(deps, ticker, *, want_book=False):
    """A 股实时快照（公开降级链，东财 → 腾讯 → 新浪）。

    返回命源的正常载荷（``source``/``delay``/``as_of`` 如实）或
    ``{ok:false, error:{code,message,chain,tried}}``——错误里保留每级真实错误原文。
    公开链不覆盖的标的（HK/US/北交所）由调用方决定去向；本函数直接报
    ``quote/unsupported-market``（不猜前缀映射）。
    """
    from server import v3_fallback  # 局部导入：与 financials_with_chain 同一口径

    resolved = a_share_public_code(ticker)
    if resolved is None:
        return envelope_error(
            "quote/unsupported-market",
            f"A 股公开行情链只覆盖沪/深（SH/SZ 六位数字），收到 {ticker!r}；"
            "北交所/港股/美股不在免密公开链覆盖内")
    market, code = resolved
    chain = _quote_chain_attempts(deps, market, code, want_book)
    value, used, attempts = v3_fallback.run_chain(chain, timeout=QUOTE_CHAIN_TIMEOUT)
    attempts = v3_fallback.attempts_chain(attempts)
    if value is None:
        error = v3_fallback.last_error(attempts)
        payload = envelope_error(
            error.get("code") or "quote/all-sources-failed",
            f"{market}.{code} 公开行情链（东财 → 腾讯 → 新浪）全部失败。"
            f"降级链：{v3_fallback.describe_attempts(attempts)}")
        payload["chain"] = attempts
        payload["tried"] = [item.get("source") for item in attempts]
        payload["as_of"] = now_iso()
        return payload
    value = dict(value)
    value["chain"] = attempts
    value["used_source"] = used
    return value


def fetch_a_share_order_book(deps, ticker):
    """A 股盘口（公开降级链）。公开源**只有五档**——如实给五档，不伪造十档/逐笔。"""
    result = fetch_a_share_quote(deps, ticker, want_book=True)
    if not result.get("ok"):
        return result
    book = result.get("book") or {}
    result["depth_note"] = (
        f"公开源（{result.get('source')}）仅提供五档买卖盘（档量单位：手）；"
        "富途 HK 10 档 / US 60 档与逐笔成交本源**无此能力**，不伪造档位")
    result["book_depth"] = book.get("depth", 0)
    return result


# ── 富途 -9 钩子：接进既有 rt_quote / rt_order_book 链路 ───────────────────────

#: 富途 -9 的固定语义（与 futu_data.A_SHARE_RT_HINT 同一口径的数据事实）。
_FUTU_FALLBACK_NOTE = ("降级不是替换：仅当富途返回 -9（A 股无实时权限）且请求全是沪/深标的时"
                       "才启用公开源；HK/US 行为一字不变")


def _make_futu_fallback(deps):
    """构造 ``futu_data.install_public_fallback`` 的 handler。

    返回 ``None`` 表示「公开链也没拿到」→ 调用方把**原始 -9 错误**原样上抛；
    成功时返回的 value 里保留 ``futu_error`` 与 ``futu_fallback`` 说明（可核对）。
    """
    def handler(endpoint, codes, error):
        if endpoint not in ("rt_quote", "rt_order_book"):
            return None
        error_dict = {"code": getattr(error, "code", ""), "message": str(error)[:200]}
        results, errors = [], []
        used_source = None
        for ticker in codes:
            # 2026-09-21 修：盘口端点必须走 ``fetch_a_share_order_book``——``book_depth`` /
            # ``depth_note`` 两个字段只在它里面补（此前误调 ``fetch_a_share_quote``，
            # 降级响应的这两个字段实测恒为 null）。
            if endpoint == "rt_order_book":
                result = fetch_a_share_order_book(deps, ticker)
            else:
                result = fetch_a_share_quote(deps, ticker)
            if not result.get("ok"):
                errors.append({"code": ticker, "error": result.get("error")})
                continue
            used_source = used_source or result.get("source")
            results.append(result)
        if not results:
            return None  # 公开链全失败：让原始 -9 错误如实上抛（降级失败不编造）
        payload = {
            "ok": True,
            "source": used_source,
            "delay": results[0].get("delay"),
            "as_of": now_iso(),
            "futu_fallback": {
                "reason": "futu 返回 -9（A 股无实时权限）→ 已降级到免密公开源",
                "policy": _FUTU_FALLBACK_NOTE,
                "upstream_error": error_dict,
            },
            "errors": errors,
        }
        if endpoint == "rt_quote":
            payload["quotes"] = [
                {"code": item.get("code"), "market": item.get("market"),
                 "name": item.get("name"), "quote_time": item.get("quote_time"),
                 "quote": item.get("quote"), "source": item.get("source"),
                 "delay": item.get("delay")}
                for item in results]
        else:
            book = results[0]
            payload.update({
                "code": book.get("code"), "market": book.get("market"),
                "name": book.get("name"), "quote_time": book.get("quote_time"),
                "book": book.get("book"), "book_depth": book.get("book_depth"),
                "depth_note": book.get("depth_note"),
                "quote": book.get("quote"),
            })
        if len(results) == 1:
            payload["chain"] = results[0].get("chain")
        return payload

    return handler


# ── 北向资金 ───────────────────────────────────────────────────────────────────

NORTHBOUND_DISCLOSURE_NOTE = (
    "沪深港交易所自 2024-08-19 起不再披露北向当日净买入（实测 AKShare 沪/深股通历史序列"
    "的最后非空净买额日期为 2024-08-16）；summary 里北向板块的 0 是上游占位值而非真实"
    "成交净买额，本响应以 net_buy_disclosed=false 标注，南向（港股通）不受影响")

NORTHBOUND_HIST_COLUMNS = {"日期": "date", "当日成交净买额": "net_buy", "买入成交额": "buy",
                           "卖出成交额": "sell", "历史累计净买额": "cumulative_net_buy",
                           "当日资金流入": "inflow", "当日余额": "balance",
                           "持股市值": "holding_mv"}


def _northbound_summary(deps):
    """当日四方板块摘要（``stock_hsgt_fund_flow_summary_em``）。"""
    try:
        ak = akshare_module(deps)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/missing", f"akshare 不可用：{_error_text(error)}")
    func = getattr(ak, "stock_hsgt_fund_flow_summary_em", None)
    if not callable(func):
        return envelope_error("akshare/missing-func",
                              "akshare.stock_hsgt_fund_flow_summary_em 不存在（版本不兼容？）")
    frame, attempts, error = _akshare_call(deps, func)
    if frame is None:
        payload = envelope_error("akshare/stock_hsgt_fund_flow_summary_em",
                                 _akshare_failure_message("stock_hsgt_fund_flow_summary_em",
                                                          error, attempts))
        payload["error"]["attempts"] = attempts
        return payload
    rows = []
    for row in _rows_from_frame(frame):
        direction = as_text(row.get("资金方向"))
        rows.append({
            "trade_date": as_text(row.get("交易日")),
            "board": as_text(row.get("板块")),
            "type": as_text(row.get("类型")),
            "direction": direction,
            "net_buy": as_number(row.get("成交净买额")),      # 亿元
            # 北向（沪/深股通）2024-08 起不再披露当日净买入 → 上游 0 是占位
            "net_buy_disclosed": direction != "北向",
            "upflow": as_number(row.get("资金净流入")),
            "up_count": as_number(row.get("上涨数")),
            "down_count": as_number(row.get("下跌数")),
            "index": as_text(row.get("相关指数")),
            "index_change_pct": as_number(row.get("指数涨跌幅")),
        })
    if not rows:
        return envelope_error("akshare/no-rows", "stock_hsgt_fund_flow_summary_em 未返回任何行")
    return {"ok": True, "source": "akshare/stock_hsgt_fund_flow_summary_em",
            "as_of": now_iso(), "boards": rows, "attempts": attempts}


def _northbound_hist(deps, channel, limit):
    """沪/深股通历史序列（``stock_hsgt_hist_em``，升序取最后 ``limit`` 条）。"""
    try:
        ak = akshare_module(deps)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/missing", f"akshare 不可用：{_error_text(error)}")
    func = getattr(ak, "stock_hsgt_hist_em", None)
    if not callable(func):
        return envelope_error("akshare/missing-func", "akshare.stock_hsgt_hist_em 不存在（版本不兼容？）")
    frame, attempts, error = _akshare_call(deps, func, {"symbol": channel})
    if frame is None:
        payload = envelope_error("akshare/stock_hsgt_hist_em",
                                 _akshare_failure_message("stock_hsgt_hist_em", error, attempts))
        payload["error"]["attempts"] = attempts
        return payload
    rows = []
    for row in _head(_rows_from_frame(frame), 100000):
        entry = {}
        for cn, en in NORTHBOUND_HIST_COLUMNS.items():
            entry[en] = as_number(row.get(cn)) if cn != "日期" else as_text(row.get(cn))
        rows.append(entry)
    rows = [row for row in rows if row.get("date")]
    rows.sort(key=lambda row: row["date"])
    disclosed = [row for row in rows if row.get("net_buy") is not None]
    if not rows:
        return envelope_error("akshare/no-rows", f"stock_hsgt_hist_em({channel}) 未返回任何行")
    want = max(1, int(limit))
    return {"ok": True, "source": "akshare/stock_hsgt_hist_em", "as_of": now_iso(),
            "channel": channel, "rows": rows[-want:],
            "total_rows": len(rows),
            "last_net_buy_date": disclosed[-1]["date"] if disclosed else None,
            "attempts": attempts}


def fetch_northbound(deps, limit=60):
    """北向资金：当日四方板块 + 沪/深股通历史序列（两部件独立取数、独立如实报错）。"""
    limit_n = to_int(limit, 60, 1, 3000)
    summary = _northbound_summary(deps)
    hist_sh = _northbound_hist(deps, "沪股通", limit_n)
    hist_sz = _northbound_hist(deps, "深股通", limit_n)
    parts_ok = [part.get("ok") for part in (summary, hist_sh, hist_sz)]
    errors = []
    for name, part in (("summary", summary), ("hist_sh", hist_sh), ("hist_sz", hist_sz)):
        if not part.get("ok"):
            error = part.get("error") or {}
            errors.append({"part": name, "code": error.get("code"), "message": error.get("message")})
    if not any(parts_ok):
        payload = envelope_error("northbound/all-parts-failed",
                                 "北向资金三个部件（当日摘要/沪股通历史/深股通历史）全部失败；"
                                 "真实错误见 errors（不返回占位数据）")
        payload["errors"] = errors
        payload["as_of"] = now_iso()
        return payload
    last_dates = [part.get("last_net_buy_date") for part in (hist_sh, hist_sz) if part.get("ok")]
    return {
        "ok": True,
        "as_of": now_iso(),
        "source": ("akshare/stock_hsgt_fund_flow_summary_em + akshare/stock_hsgt_hist_em"),
        "boards": summary.get("boards") if summary.get("ok") else None,
        "history": {
            "SH": {"rows": hist_sh.get("rows"), "total_rows": hist_sh.get("total_rows"),
                   "last_net_buy_date": hist_sh.get("last_net_buy_date")} if hist_sh.get("ok") else None,
            "SZ": {"rows": hist_sz.get("rows"), "total_rows": hist_sz.get("total_rows"),
                   "last_net_buy_date": hist_sz.get("last_net_buy_date")} if hist_sz.get("ok") else None,
        },
        "net_buy_disclosure": {
            "northbound_daily_net_buy": False,
            "last_disclosed_date": max((d for d in last_dates if d), default=None),
            "note": NORTHBOUND_DISCLOSURE_NOTE,
        },
        "errors": errors,
        "note": "净买额/成交额单位：亿元；数值原样透传 AKShare（未加工）",
    }


# ── 宏观经济 ───────────────────────────────────────────────────────────────────

def _macro_period(raw):
    """``2026年08月份`` / ``201501`` → ``2026-08`` / ``2015-01``；识别不出返回 ``None``。"""
    text = as_text(raw).strip()
    match = re.fullmatch(r"(\d{4})年(\d{2})月份?", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    match = re.fullmatch(r"(\d{4})-(\d{2})", text[:7])
    if match:
        return text[:7]
    match = re.fullmatch(r"(\d{6})", text)
    if match:
        return f"{text[:4]}-{text[4:6]}"
    return None


#: 指标白名单（函数名以本机 akshare 1.18.96 实测存在为准；2026-09-21 逐个真跑通过）。
MACRO_INDICATORS = {
    "cpi": {"func": "macro_china_cpi", "name": "居民消费价格指数（CPI）", "unit": "同比 %",
            "value": "全国-同比增长", "extras": {"index": "全国-当月"}},
    "ppi": {"func": "macro_china_ppi", "name": "工业生产者出厂价格指数（PPI）", "unit": "同比 %",
            "value": "当月同比增长", "extras": {"index": "当月"}},
    "pmi": {"func": "macro_china_pmi", "name": "采购经理指数（PMI）", "unit": "指数（50 荣枯线）",
            "value": "制造业-指数", "extras": {"non_manufacturing": "非制造业-指数"}},
    "shrzgm": {"func": "macro_china_shrzgm", "name": "社会融资规模增量（社融）", "unit": "亿元",
               "value": "社会融资规模增量", "extras": {"rmb_loans": "其中-人民币贷款"}},
    "m2": {"func": "macro_china_money_supply", "name": "货币供应量（M2/M1/M0）",
           "unit": "数量亿元 / 同比 %", "value": "货币和准货币(M2)-同比增长",
           "extras": {"m2": "货币和准货币(M2)-数量(亿元)", "m1": "货币(M1)-数量(亿元)",
                      "m1_yoy": "货币(M1)-同比增长", "m0": "流通中的现金(M0)-数量(亿元)",
                      "m0_yoy": "流通中的现金(M0)-同比增长"}},
}
#: 中文别名（工具面调用者不必记拼音键）。
MACRO_ALIASES = {"社融": "shrzgm", "社融增量": "shrzgm", "货币供应量": "m2", "货币供应": "m2"}

#: OpenBB 免密 provider 的 OECD 标准化口径覆盖（OpenBB 4.7.2 + openbb-oecd 实测）：
#: 只有 CPI 有中国的同口径序列；``money_measures`` 仅 federal_reserve（美国），
#: CLI（composite_leading_indicator）不是 PMI 同口径——不映射、不冒充。
MACRO_OECD_CALIBER = {
    "cpi": "OECD 标准化口径（economy.cpi，中国，月度；value 为小数，×100 = 同比 %；"
           "2026-09-21 与 NBS 同比逐月核对一致：0.008↔0.8%、0.005↔0.5%）",
}
MACRO_OECD_NO_SERIES = (
    "免密 OECD/IMF 面（OpenBB 4.7.2 实测）没有该指标的中国同口径序列："
    "money_measures 仅支持 federal_reserve（美国）、composite_leading_indicator 是 OECD CLI"
    "（非 PMI 口径，不冒充）——该指标无第二源")

#: OpenBB 冷启动 import 实测 32~57s：宏观 OECD 腿的 import 超时（``Deps.module`` 的
#: SIGALRM 防护口径；导入成功后进程内缓存，后续请求毫秒级）。环境变量可放宽。
MACRO_OPENBB_IMPORT_TIMEOUT_ENV = "QUANT_MACRO_OPENBB_IMPORT_TIMEOUT"
MACRO_OPENBB_IMPORT_TIMEOUT = 75.0


def macro_openbb_import_timeout(deps):
    """OECD 腿的 openbb import 超时（秒）：环境变量 ``QUANT_MACRO_OPENBB_IMPORT_TIMEOUT``。"""
    raw = None
    try:
        raw = deps.base.get_env(MACRO_OPENBB_IMPORT_TIMEOUT_ENV)
    except Exception:  # noqa: BLE001 —— 假 deps 形态不保证
        raw = None
    if raw in (None, ""):
        return MACRO_OPENBB_IMPORT_TIMEOUT
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return MACRO_OPENBB_IMPORT_TIMEOUT
    return value if value > 0 else MACRO_OPENBB_IMPORT_TIMEOUT


def _oecd_period(raw):
    """pandas Timestamp / ``2026-08-01 00:00:00`` / date → ``2026-08``；识别不出返回 ``None``。"""
    text = as_text(raw).strip()
    match = re.match(r"(\d{4})-(\d{2})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return _macro_period(text)


def _macro_nbs(deps, key, meta, want):
    """AKShare 腿（国家统计局/央行口径，经金十数据）：序列 + 最新值。"""
    try:
        ak = akshare_module(deps)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/missing", f"akshare 不可用：{_error_text(error)}")
    func = getattr(ak, meta["func"], None)
    if not callable(func):
        return envelope_error("akshare/missing-func", f"akshare.{meta['func']} 不存在（版本不兼容？）")
    frame, attempts, error = _akshare_call(deps, func)
    if frame is None:
        payload = envelope_error("akshare/" + meta["func"],
                                 _macro_failure_message(meta["func"], error, attempts))
        payload["error"]["attempts"] = attempts
        return payload
    rows = []
    for raw in _rows_from_frame(frame):
        period = _macro_period(raw.get("月份"))
        if period is None:
            continue  # 无法定位期间的行不入序列（宁缺毋假）
        entry = {"period": period, "value": as_number(raw.get(meta["value"]))}
        for name, column in meta["extras"].items():
            entry[name] = as_number(raw.get(column))
        rows.append(entry)
    if not rows:
        return envelope_error("akshare/no-rows", f"akshare.{meta['func']} 未返回可解析的报告期")
    rows.sort(key=lambda row: row["period"])
    tail = rows[-want:]
    return {
        "ok": True,
        "indicator": key,
        "caliber": "国家统计局/央行口径（经金十数据，AKShare 免密钥公开端点）",
        "name": meta["name"],
        "unit": meta["unit"],
        "source": f"akshare/{meta['func']}",
        "as_of": now_iso(),
        "latest": tail[-1],
        "rows": tail,
        "total_periods": len(rows),
        "note": "数值原样透传（未加工、不平滑）；attempt 明细见 attempts",
        "attempts": attempts,
    }


def _macro_oecd(deps, key, want):
    """OpenBB OECD 腿（对照面/降级第 2 源）：仅 CPI 有中国同口径序列（实测）。"""
    if key not in MACRO_OECD_CALIBER:
        return envelope_error("oecd/no-series", MACRO_OECD_NO_SERIES)
    try:
        module = deps.base.module("openbb", deps.base._openbb,
                                  timeout=macro_openbb_import_timeout(deps))
    except ImportError as error:
        return envelope_error("openbb/unavailable", f"openbb 未安装：{_error_text(error)}")
    except Exception as error:  # noqa: BLE001 —— import 期其它异常（含 SIGALRM 超时）
        return envelope_error("openbb/import-failed", f"openbb 导入失败：{_error_text(error)}")
    obb = getattr(module, "obb", None) or module
    cpi = getattr(getattr(obb, "economy", None), "cpi", None)
    if not callable(cpi):
        return envelope_error("openbb/capability-missing",
                              "openbb.obb.economy.cpi 不存在（版本不兼容？）")
    frame, attempts, error = _akshare_call(deps, lambda: cpi(country="china", provider="oecd"))
    if frame is None:
        payload = envelope_error("openbb/economy.cpi",
                                 _macro_failure_message("economy.cpi(oecd, china)", error, attempts))
        payload["error"]["attempts"] = attempts
        return payload
    rows_raw = []
    to_df = getattr(frame, "to_dataframe", None)
    if callable(to_df):
        # 2026-09-21 修：OpenBB/OECD 把**报告期放在 DataFrame 的 index**（``index.name="date"``），
        # 而 ``_rows_from_frame`` 走 ``to_dict("records")`` 会丢 index → 下面 ``raw.get("date")``
        # 恒 None → 全行跳过、第二源恒回 openbb/no-rows。这里先 ``reset_index()`` 把报告期
        # 落回列（reset 失败/无该方法则原样，不吞异常语义）。
        data_frame = to_df()
        reset = getattr(data_frame, "reset_index", None)
        if callable(reset):
            try:
                data_frame = reset()
            except Exception:  # noqa: BLE001 —— 非 pandas 帧没有 index 语义，保持原样
                pass
        rows_raw = _rows_from_frame(data_frame)
    else:
        rows_raw = _rows_from_frame(v3_sources.jsonable(frame))
    rows = []
    for raw in rows_raw:
        if isinstance(raw, dict) and "expenditure" in raw and as_text(raw.get("expenditure")) != "total":
            continue  # 只取总量口径，避免分项与总量混排
        period = _oecd_period(raw.get("date") if isinstance(raw, dict) else None)
        if period is None:
            continue
        value = as_number(raw.get("value")) if isinstance(raw, dict) else None
        rows.append({"period": period, "value": value, "value_pct": None if value is None else value * 100.0})
    rows = [row for row in rows if row["value"] is not None]
    if not rows:
        return envelope_error("openbb/no-rows", "economy.cpi(oecd, china) 未返回可解析的月度序列")
    rows.sort(key=lambda row: row["period"])
    tail = rows[-want:]
    return {
        "ok": True,
        "indicator": key,
        "caliber": MACRO_OECD_CALIBER[key],
        "source": "openbb/economy.cpi(provider=oecd)",
        "as_of": now_iso(),
        "latest": tail[-1],
        "rows": tail,
        "total_periods": len(rows),
        "note": "OECD 免密 provider（openbb-oecd）；value_pct = value×100（显式换算并标注，不冒充 NBS 口径）",
        "attempts": attempts,
    }


def fetch_macro(deps, indicator, limit=24, compare=""):
    """宏观指标：主源 AKShare（NBS 口径）；OpenBB OECD 为**第二源/对照面**。

    * ``compare`` 留空（缺省）：只出 NBS 口径，响应快（不触发 openbb 冷启动 import）；
      AKShare 失败且指标为 CPI 时**自动**降到 OECD 腿（降级链语义，``source``/``caliber``
      如实换标，``nbs_error`` 保留主源真实错误）。
    * ``compare="oecd"``：成功响应里并列 ``oecd`` 块（OECD 标准化口径），两口径各自带
      ``caliber`` 标注，**不互相冒充**；该指标无 OECD 序列时 ``oecd`` 块如实给
      ``oecd/no-series`` 及原因。openbb 冷启动 import 实测 32~57s，首次显式对照请求
      可能变慢（SIGALRM 防护 + 进程内缓存），首屏路径不受影响。
    """
    key = MACRO_ALIASES.get(as_text(indicator).strip(), as_text(indicator).strip().lower())
    meta = MACRO_INDICATORS.get(key)
    if meta is None:
        return envelope_error(
            "macro/unknown-indicator",
            f"indicator 需为 {sorted(MACRO_INDICATORS)}（或中文别名 {sorted(MACRO_ALIASES)}），收到 {indicator!r}")
    want = to_int(limit, 24, 1, 500)
    nbs = _macro_nbs(deps, key, meta, want)
    want_oecd = (as_text(compare).strip().lower() == "oecd") or (not nbs.get("ok") and key == "cpi")
    oecd = _macro_oecd(deps, key, want) if want_oecd else None
    if nbs.get("ok"):
        if want_oecd:
            nbs["oecd"] = oecd
            nbs["note"] = (nbs.get("note") or "")
            nbs["calibers"] = {"nbs": nbs.get("caliber"), "oecd": (oecd or {}).get("caliber")}
        return nbs
    if oecd is not None and oecd.get("ok"):
        # 主源失败 → OECD 腿顶上（口径换标：响应的 source/caliber 是 OECD，不是 NBS）
        oecd["nbs_error"] = nbs.get("error")
        oecd["indicator"] = key
        oecd["name"] = meta["name"]
        oecd["unit"] = (meta["unit"] + "；OECD 腿为 value_pct（%）")
        oecd["note"] = ("主源 AKShare（NBS 口径）失败，本响应来自 OECD 标准化口径降级腿"
                        "（两口径不互相冒充）")
        return oecd
    payload = envelope_error(
        nbs.get("error", {}).get("code") or "macro/all-sources-failed",
        (nbs.get("error", {}).get("message") or "宏观取数失败")
        + (f"；OECD 腿也不可用：{oecd.get('error', {}).get('message')}" if oecd is not None else ""))
    payload["nbs_error"] = nbs.get("error")
    if oecd is not None:
        payload["oecd_error"] = oecd.get("error")
    return payload


def _macro_failure_message(func, error, attempts):
    detail = (error or {}).get("message") or "无错误明细"
    return f"{func} 失败（尝试 {len(attempts or [])} 次）：{detail}"


# ── /api/v3/sources/status 扩展链（仅 keys= 显式请求时探测）────────────────────

#: 扩展链静态描述（字段与 ``v3_fallback.CHAIN_SPECS`` 同形；不进缺省探测集——
#: 那个集合被既有单测锁定为 8 条）。
EXT_CHAIN_SPECS = (
    {"key": "rt_quote", "label": "A 股实时报价（富途 -9 → 公开降级）",
     "primary": "futu/rt_quote",
     "fallback": "eastmoney/push2 → tencent/qt.gtimg.cn → sina/hq.sinajs.cn"},
    {"key": "rt_order_book", "label": "A 股盘口（富途 -9 → 公开五档）",
     "primary": "futu/rt_order_book",
     "fallback": "eastmoney/push2 → tencent/qt.gtimg.cn → sina/hq.sinajs.cn"},
    {"key": "northbound", "label": "沪深港通北向资金（AKShare）",
     "primary": "akshare/stock_hsgt_fund_flow_summary_em",
     "fallback": "akshare/stock_hsgt_hist_em"},
    {"key": "macro", "label": "宏观经济 CPI/PPI/PMI/社融/M2（AKShare）",
     "primary": "akshare/macro_china_cpi",
     "fallback": ""},
)

EXT_SPEC_BY_KEY = {spec["key"]: spec for spec in EXT_CHAIN_SPECS}

_PROBE_TICKER = "SH.600000"


def _tool_probe(v3_run, name, payload, expect, describe):
    """富途工具面的一次只读探测（与 ``v3_fallback._tool_probe`` 同形的本地实现，
    不依赖私有函数；返回统一信封 ``{ok, as_of, source, rows, probe}``）。"""
    envelope = v3_run(name, payload)
    if not isinstance(envelope, dict):
        return envelope_error("probe/bad-envelope", f"{name} 返回非信封对象：{type(envelope).__name__}")
    if not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        return envelope_error(error.get("code") or f"probe/{name}-failed",
                              error.get("message") or f"{name} 返回 ok=false")
    value = envelope.get("value")
    try:
        detail = expect(value)
    except Exception as error:  # noqa: BLE001 —— 形状不符也是真实失败
        return envelope_error("probe/unexpected-shape", f"{name} 返回形状不符：{_error_text(error)}")
    if not detail:
        return envelope_error("probe/no-data", f"{name} 未返回可用数据（{describe}）")
    return {
        "ok": True,
        "as_of": (value or {}).get("as_of") if isinstance(value, dict) else None,
        "source": (value or {}).get("source") if isinstance(value, dict) else None,
        "rows": detail,
        "probe": describe,
    }


def build_ext_probes(v3_run, home, *, deps=None):
    """``{key: [(source_name, callable), ...]}``——形状与 ``v3_fallback.build_probes`` 一致。"""
    if deps is None:
        deps = ExtDeps(Deps(home=home))

    def quote_rows(value):
        if isinstance(value, dict):
            for key in ("quotes", "code_list"):
                rows = value.get(key)
                if isinstance(rows, list) and rows:
                    return len(rows)
        return 0

    def book_rows(value):
        book = (value or {}).get("book") if isinstance(value, dict) else None
        if isinstance(book, dict):
            return len(book.get("bid") or []) + len(book.get("ask") or [])
        return 0

    def board_rows(value):
        rows = (value or {}).get("boards") if isinstance(value, dict) else None
        return len(rows) if isinstance(rows, list) else 0

    def macro_rows(value):
        rows = (value or {}).get("rows") if isinstance(value, dict) else None
        return len(rows) if isinstance(rows, list) else 0

    def northbound_summary_probe():
        return _northbound_summary(deps)

    def northbound_hist_probe():
        return _northbound_hist(deps, "沪股通", 1)

    def macro_probe():
        return fetch_macro(deps, "cpi", 1)

    return {
        "rt_quote": [
            ("futu/rt_quote", lambda: _tool_probe(v3_run, "rt_quote",
                                                  {"codes": [_PROBE_TICKER]}, quote_rows,
                                                  "rt_quote SH.600000（富途 -9 属权限缺口，如实记）")),
            ("eastmoney/push2→tencent→sina", lambda: fetch_a_share_quote(deps, _PROBE_TICKER)),
        ],
        "rt_order_book": [
            ("futu/rt_order_book", lambda: _tool_probe(v3_run, "rt_order_book",
                                                       {"code": _PROBE_TICKER}, book_rows,
                                                       "rt_order_book SH.600000（富途 -9 属权限缺口）")),
            ("eastmoney/push2→tencent→sina", lambda: fetch_a_share_order_book(deps, _PROBE_TICKER)),
        ],
        "northbound": [
            ("akshare/stock_hsgt_fund_flow_summary_em", northbound_summary_probe),
            ("akshare/stock_hsgt_hist_em", northbound_hist_probe),
        ],
        "macro": [
            ("akshare/macro_china_cpi", macro_probe),
        ],
    }


# ── 路由注册 ───────────────────────────────────────────────────────────────────


def register(app, v3_run, home, deps=None):
    """挂 ``GET /api/v3/northbound``、``GET /api/v3/macro``，并安装富途 -9 公开降级钩子。

    ``deps`` 仅供测试注入（``v3_sources.Deps`` 或其子集）；缺省按 ``home`` 建一份真实依赖。
    """
    from server import futu_data

    base = deps if deps is not None else Deps(home=home)
    ext_deps = base if isinstance(base, ExtDeps) else ExtDeps(base)
    futu_data.install_public_fallback(_make_futu_fallback(ext_deps))

    @app.get("/api/v3/northbound")
    async def v3_northbound(limit: int = 60):
        """北向资金（AKShare 免密钥）：当日四方板块 + 沪/深股通历史净买额序列。

        北向当日净买入自 2024-08-19 起交易所不再披露（响应 ``net_buy_disclosure`` 写明），
        **绝不上游 0 占位当真实值**；部件失败在 ``errors`` 里逐条留真实错误。
        """
        try:
            return await asyncio.to_thread(fetch_northbound, ext_deps, limit)
        except Exception as error:  # noqa: BLE001 —— 统一信封，不把栈透给前端
            return envelope_error("northbound/internal", _error_text(error))

    @app.get("/api/v3/macro")
    async def v3_macro(indicator: str = "cpi", limit: int = 24, compare: str = ""):
        """宏观经济序列：主源 AKShare（国家统计局/央行口径，免密钥），OpenBB OECD 为第二源。

        indicator = cpi / ppi / pmi / shrzgm(社融) / m2；序列（升序尾部 limit 条）+ 最新值 +
        来源。``compare=oecd`` 时响应并列 ``oecd`` 块（OECD 标准化口径，仅 CPI 有中国同口径
        序列，其余如实 oecd/no-series）；AKShare 失败且为 CPI 时自动降到 OECD 腿（口径换标）。
        openbb 冷启动 import 实测 32~57s（SIGALRM 防护 + 进程内缓存），首屏路径默认不触发。
        """
        try:
            return await asyncio.to_thread(fetch_macro, ext_deps, indicator, limit, compare)
        except Exception as error:  # noqa: BLE001
            return envelope_error("macro/internal", _error_text(error))

    app.state.v3_sources_ext = {
        "deps": ext_deps,
        "v3_run": v3_run,
        "routes": ("/api/v3/northbound", "/api/v3/macro"),
        "ext_chains": tuple(spec["key"] for spec in EXT_CHAIN_SPECS),
    }
    return ext_deps
