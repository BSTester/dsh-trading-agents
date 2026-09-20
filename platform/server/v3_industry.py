"""行业映射与暴露：``GET /api/v3/risk/industry?tickers=&market=&limit_pct=20``。

契约（前端已按此实现）::

    {"ok":true,"as_of":"…","limitPct":20,"breach":false,
     "mapping":{"SH.600000":{"industry":"银行","plates":["SH.LIST0949"]}},
     "exposures":[{"industry":"银行","weightPct":12.5,"tickers":["SH.600000"],"value":125000.0}],
     "top":{"industry":"银行","weightPct":12.5},
     "sources":{"plate":"futu/info_owner_plate","weights":"platform/portfolio"},
     "missing":[{"ticker":"US.NVDA","reason":"富途未返回所属板块"}]}

行业口径（**不猜**）:

  1. 富途 ``info_owner_plate`` → ``sectors`` 里 ``plate_type=INDUSTRY`` 的板块；
     名称优先中文 ``plate_sc_name``，退化到 ``plate_name``；
  2. 上一步「没有 INDUSTRY 类型」时，用 ``plate_list(market, INDUSTRY)`` 的行业板块代码集合
     与 owner plates 求交集（一次调用/市场，结果在本次请求内缓存）；
  3. ``info_owner_plate`` **整体失败**（限频 -12006 / 权限）时，降级用 ``plate_stock`` 逐行业
     板块反查成分股（有扫描上限，命中即停；上限内没命中就进 ``missing`` 并写明是扫描上限）；
  4. 任一标的取不到行业 → 进 ``missing``，**绝不猜行业**。

权重口径：``plan`` 里首个 frozen 且标的多于 1 只的计划（与 ``/api/v3/risk/analytics`` 同一
口径）→ 否则自选池等权（``<home>/trading-platform.json`` 的 ``watchlist``）。
``value`` = 权重 × ``equity.current``（拿不到权益 → ``null``，不估算）。
``breach = top.weightPct > limit_pct``。

``?market=SH|HK|US``：标的集**复用统一解析** ``server/v3_universe.resolve_universe``
（配置 ``watchlists.<market>`` → 富途真实持仓）；显式 ``?tickers=`` 仍优先（只按市场过滤
请求的标的）；该市场既无配置池也无真实持仓 → ``industry/no-universe``（如实报错，不退回
全部市场）。平台组合里没有该市场标的时，权重改用该市场宇宙等权并在 ``sources.weights``
写明（否则暴露恒为 0%，等于给假象）。不传 ``market`` 时行为与历史一致。
"""
from __future__ import annotations

import asyncio
import json
import os
import time as _time
from datetime import datetime, timezone
from pathlib import Path

from server import v3_math, v3_universe

__all__ = [
    "INDUSTRY_PLATE_TYPE",
    "PLATE_SOURCE_ORDER_PLATE",
    "PLATE_SOURCE_PLATE_STOCK",
    "industry_exposure",
    "register",
]

#: 板块类型里代表「行业分类」的取值（富途 ``sectors[].plate_type``）。
INDUSTRY_PLATE_TYPE = "INDUSTRY"
PLATE_SOURCE_ORDER_PLATE = "futu/info_owner_plate"
PLATE_SOURCE_PLATE_STOCK = "futu/plate_stock"
#: plate_stock 反查的上限（每个行业板块一次调用；默认保守，避免一次请求打几十个上游调用）
DEFAULT_PLATE_SCAN_LIMIT = 12
#: 上游限频（-12006 / rate limit）时的重试等待（秒）与次数
RATE_LIMIT_MARKERS = ("-12006", "rate limit", "超出频率", "频率限制")
RATE_LIMIT_RETRIES = 1
RATE_LIMIT_DELAY = 0.8
#: 自选池等权的标的数上限（与 v3_analytics.WATCHLIST_LIMIT 同口径）
WATCHLIST_LIMIT = 8
#: 市场前缀（用于按 market 过滤标的）
MARKETS = ("SH", "SZ", "BJ", "HK", "US")


def _error(code, message, **extra):
    payload = {"ok": False, "error": {"code": str(code), "message": str(message)[:300]}}
    payload["error"].update(extra)
    return payload


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _home_path(home):
    if home in (None, ""):
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return Path(str(home)).expanduser()


def read_watchlist(home):
    """自选池（``<home>/trading-platform.json`` 的 ``watchlist``）；读不到 → 空池（不臆造）。"""
    path = _home_path(home) / "trading-platform.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("watchlist"), list):
        return []
    names = []
    for item in raw["watchlist"]:
        text = str(item or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _value_of(envelope):
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return None
    value = envelope.get("value")
    return value if isinstance(value, dict) else None


def _error_of(envelope):
    if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
        error = envelope["error"]
        return str(error.get("code") or "industry/upstream"), str(error.get("message") or "上游失败")
    return "industry/upstream", "上游工具调用失败（无 error 明细）"


def _is_rate_limited(code, message):
    text = f"{code} {message}".lower()
    return any(marker.lower() in text for marker in RATE_LIMIT_MARKERS)


def market_of(ticker):
    """``SH.600000`` → ``SH``；非 ``MARKET.CODE`` 形态 → ``""``。"""
    text = str(ticker or "").strip().upper()
    if "." not in text:
        return ""
    head = text.split(".", 1)[0]
    return head if head in MARKETS else ""


def _call_tool(v3_run, name, payload, *, sleep=None, retries=RATE_LIMIT_RETRIES):
    """工具面调用 + 限频重试（只重试限频这一类**可重试**失败，其余原样返回）。"""
    envelope = v3_run(name, payload)
    if not isinstance(envelope, dict) or envelope.get("ok"):
        return envelope if isinstance(envelope, dict) else {
            "ok": False, "error": {"code": "industry/bad-envelope", "message": f"{name} 返回非信封对象"}}
    code, message = _error_of(envelope)
    if retries > 0 and _is_rate_limited(code, message):
        waiter = sleep or _time.sleep
        waiter(RATE_LIMIT_DELAY)
        return _call_tool(v3_run, name, payload, sleep=waiter, retries=retries - 1)
    return envelope


def _plate_name(sector):
    """板块中文名优先（``plate_sc_name``），退化到英文名。"""
    for key in ("plate_sc_name", "sc_name", "plate_name"):
        text = str(sector.get(key) or "").strip()
        if text:
            return text
    return ""


class IndustryResolver:
    """按请求构造：缓存「市场 → 行业板块代码集」与逐板块成分股，避免重复打上游。"""

    def __init__(self, v3_run, *, sleep=None, plate_scan_limit=DEFAULT_PLATE_SCAN_LIMIT):
        self.v3_run = v3_run
        self.sleep = sleep
        self.plate_scan_limit = max(0, int(plate_scan_limit))
        self._industry_plates = {}
        self._plate_members = {}
        self.sources_used = []
        self.notes = []

    # ── 行业板块清单（每个市场一次）────────────────────────────────────────────
    def industry_plates(self, market):
        if market in self._industry_plates:
            return self._industry_plates[market]
        plates = {}
        error = None
        if market:
            envelope = _call_tool(self.v3_run, "plate_list",
                                  {"market": market, "plate_class": INDUSTRY_PLATE_TYPE},
                                  sleep=self.sleep)
            value = _value_of(envelope)
            if value is None:
                error = _error_of(envelope)
            else:
                for row in value.get("plate_list") or []:
                    if not isinstance(row, dict):
                        continue
                    code = str(row.get("code") or "").strip().upper()
                    if not code:
                        continue
                    name = ""
                    for key in ("sc_name", "plate_name"):
                        name = str(row.get(key) or "").strip()
                        if name:
                            break
                    plates[code] = name or code
        self._industry_plates[market] = (plates, error)
        return self._industry_plates[market]

    def _sector(self, ticker):
        """``info_owner_plate`` → ``(sectors, error)``。"""
        envelope = _call_tool(self.v3_run, "info_owner_plate", {"code": ticker}, sleep=self.sleep)
        value = _value_of(envelope)
        if value is None:
            return None, _error_of(envelope)
        sectors = [row for row in (value.get("sectors") or []) if isinstance(row, dict)]
        return sectors, None

    # ── 单标的行业 ────────────────────────────────────────────────────────────
    def industry_of(self, ticker):
        """``(mapping_entry | None, reason | None)``。取不到行业时 reason 是**真实**原因。"""
        market = market_of(ticker)
        sectors, error = self._sector(ticker)
        if sectors is None:
            # 主源整体失败 → 降级用 plate_stock 反查（有上限；上限内没命中就如实说明）
            found, reason = self._reverse_lookup(ticker, market)
            if found is not None:
                return found, None
            return None, (
                f"{PLATE_SOURCE_ORDER_PLATE} 不可用（{error[0]}：{error[1]}）；"
                f"{PLATE_SOURCE_PLATE_STOCK} 反查{reason}"
            )
        industry_plates = [row for row in sectors
                           if str(row.get("plate_type") or "").strip().upper() == INDUSTRY_PLATE_TYPE]
        used = PLATE_SOURCE_ORDER_PLATE
        if not industry_plates and market:
            # 兜底 A：owner plates 与「行业板块代码集」求交集（owner 的 plate_type 可能不全）
            plates, _error = self.industry_plates(market)
            industry_plates = [row for row in sectors
                               if str(row.get("plate_code") or "").strip().upper() in plates]
            if industry_plates:
                note = (f"部分标的的行业由 {PLATE_SOURCE_ORDER_PLATE} 的 owner plates 与 "
                        f"plate_list({INDUSTRY_PLATE_TYPE}) 行业板块清单求交集得到"
                        f"（上游未把该板块标成 plate_type={INDUSTRY_PLATE_TYPE}）")
                if note not in self.notes:
                    self.notes.append(note)
        if not industry_plates:
            # 兜底 B（最后一招）：plate_stock 逐行业板块反查成分股。有扫描上限、命中即停；
            # 上限内没命中就把**真实原因**（含上游错误/扫描上限）写进 missing。
            found, reverse_reason = self._reverse_lookup(ticker, market)
            if found is not None:
                return found, None
            own = [str(row.get("plate_code") or "") for row in sectors][:5]
            return None, (
                f"富途未返回所属行业板块（owner plates={own or '空'}，"
                f"无 plate_type={INDUSTRY_PLATE_TYPE} 且与行业板块清单无交集）；"
                f"{PLATE_SOURCE_PLATE_STOCK} 反查{reverse_reason}"
            )
        primary = industry_plates[0]
        name = _plate_name(primary)
        if not name:
            return None, "富途返回的行业板块没有名称（sc_name/plate_name 均为空），不猜行业"
        if used not in self.sources_used:
            self.sources_used.append(used)
        return {
            "industry": name,
            "plates": [str(row.get("plate_code") or "").strip().upper()
                       for row in industry_plates if row.get("plate_code")],
            "allPlates": [str(row.get("plate_code") or "").strip().upper()
                          for row in sectors if row.get("plate_code")],
        }, None

    # ── plate_stock 反查 ──────────────────────────────────────────────────────
    def _plate_members_of(self, plate_code):
        if plate_code in self._plate_members:
            return self._plate_members[plate_code]
        envelope = _call_tool(self.v3_run, "plate_stock",
                              {"plate_code": plate_code, "limit": 200}, sleep=self.sleep)
        value = _value_of(envelope)
        members = []
        if value is not None:
            for row in value.get("stock_list") or []:
                if isinstance(row, dict) and row.get("code"):
                    members.append(str(row["code"]).strip().upper())
        self._plate_members[plate_code] = members
        return members

    def _reverse_lookup(self, ticker, market):
        """行业板块成分股反查；返回 ``(entry | None, reason)``。"""
        if not market:
            return None, "（无法反查：标的缺少可识别的市场前缀）"
        if self.plate_scan_limit <= 0:
            return None, "（反查已禁用：plate_scan_limit=0）"
        plates, error = self.industry_plates(market)
        if error is not None:
            return None, f"（无法反查：plate_list 不可用 {error[0]}）"
        wanted = str(ticker).strip().upper()
        scanned = 0
        for code, name in list(plates.items())[: self.plate_scan_limit]:
            scanned += 1
            members = self._plate_members_of(code)
            if wanted in members:
                if PLATE_SOURCE_PLATE_STOCK not in self.sources_used:
                    self.sources_used.append(PLATE_SOURCE_PLATE_STOCK)
                return {"industry": name or code, "plates": [code], "allPlates": [code]}, None
        return None, (f"未在扫描上限内命中（已扫描 {scanned}/{len(plates)} 个行业板块，"
                      f"上限 {self.plate_scan_limit}）")


# ── 组合权重 ───────────────────────────────────────────────────────────────────


def resolve_weights(v3_run, home):
    """组合权重：frozen 计划 → 自选池等权。返回 ``(weights, source)``。"""
    envelope = _call_tool(v3_run, "plan", {})
    value = _value_of(envelope) or {}
    plans = value.get("plans") if isinstance(value.get("plans"), list) else []
    weights, source = v3_math.pick_frozen_plan_weights(plans)
    if weights:
        return weights, f"platform/portfolio（{source}）"
    weights, source = v3_math.watchlist_equal_weights(read_watchlist(home), WATCHLIST_LIMIT)
    if weights:
        return weights, f"platform/portfolio（{source}）"
    return None, source


def _canonical_ticker(value):
    """标的 → 规范键 ``MARKET.CODE``（``600000.SH`` 与 ``SH.600000`` 归一；其余大写原样）。"""
    text = str(value or "").strip().upper()
    if "." in text:
        head, tail = text.split(".", 1)
        if head in MARKETS:
            return f"{head}.{tail}"
        if tail in MARKETS:
            return f"{tail}.{head}"
    return text


def _lookup_weight(weights, ticker):
    """权重表里找标的：精确 → 大小写不敏感 → 规范键（``SH.600000`` ↔ ``600000.SH``）。"""
    text = str(ticker or "").strip()
    if text in weights:
        return weights[text]
    upper = text.upper()
    for key, value in weights.items():
        if str(key).strip().upper() == upper:
            return value
    target = _canonical_ticker(text)
    for key, value in weights.items():
        if _canonical_ticker(key) == target:
            return value
    return None


def _tickers_of(raw):
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = str(raw or "").split(",")
    out = []
    for item in items:
        text = item.strip().upper()
        if text and text not in out:
            out.append(text)
    return out


def industry_exposure(v3_run, home, tickers_raw=None, market="", limit_pct=20, *,
                      nav=None, sleep=None, plate_scan_limit=DEFAULT_PLATE_SCAN_LIMIT,
                      resolver=None):
    """行业暴露（可注入 ``v3_run``；路由只是它的异步外壳）。契约见模块 docstring。"""
    try:
        limit_value = float(limit_pct)
    except (TypeError, ValueError):
        return _error("industry/bad-limit", f"limit_pct 需为数值，收到 {limit_pct!r}")
    if limit_value < 0 or limit_value > 100:
        return _error("industry/bad-limit", f"limit_pct 需在 0..100，收到 {limit_value}")

    weights, weight_source = resolve_weights(v3_run, home)

    requested = _tickers_of(tickers_raw)
    market_code = ""
    if str(market or "").strip():
        market_code = v3_universe.normalize_market(market)
        if market_code is None:
            return _error("industry/bad-market", "market 需为 SH / HK / US",
                          supported=list(MARKETS))
    universe_source = None
    if requested:
        if market_code:
            requested = [ticker for ticker in requested
                         if v3_universe.market_of_ticker(ticker) == market_code]
        universe = requested
        notes = [f"标的由 ?tickers= 指定（{len(universe)} 只），权重仍取平台组合口径"]
        if not weights:
            return _error("industry/no-portfolio",
                          f"无可用组合定义（{weight_source}）——用 ?tickers= 显式指定标的，"
                          f"或先配置自选池")
    elif market_code:
        # 统一解析（配置 watchlists.<market> → 富途真实持仓）；无宇宙 → 沿用 industry/no-universe
        resolved = v3_universe.resolve_universe(v3_run, home, market_code)
        if resolved is None:
            detail = v3_universe.universe_note(home, market_code) or ""
            return _error("industry/no-universe",
                          f"market={market_code} 既没有配置自选池、也没有真实持仓"
                          + (f"（{detail}）" if detail else "")
                          + "——这不是取数失败，而是该市场没有可分析的标的",
                          market=market_code)
        universe = [ticker for ticker in (resolved.get("tickers") or [])]
        universe_source = resolved.get("source")
        notes = [f"标的取统一解析的 {market_code} 宇宙（{universe_source}），"
                 f"共 {len(universe)} 只"]
        if universe and (not weights
                         or not any(_lookup_weight(weights, ticker) is not None
                                    for ticker in universe)):
            # 平台组合里没有该市场标的 → 用该市场宇宙等权（否则暴露恒为 0%，等于给假象）
            weights = {ticker: 1.0 / len(universe) for ticker in universe}
            weight_source = (f"{market_code} 市场宇宙等权（{len(universe)} 只）"
                             f"（平台组合口径：{weight_source}）")
    else:
        if not weights:
            return _error("industry/no-portfolio",
                          f"无可用组合定义（{weight_source}）——用 ?tickers= 显式指定标的，"
                          f"或先配置自选池")
        universe = [ticker for ticker in weights]
        notes = []
    if market_code:
        notes.append(f"已按 market={market_code} 过滤（不跨市场合并暴露）")
    if not universe:
        # 空集**不伪装成成功**：没有标的就没有暴露，返回错误并说明组合的市场分布
        # （前端读到 ok=false 会显示「无数据源 · 原因」，而不是给一个绿色「未超限」的假象）。
        mix = {}
        for ticker in weights:
            mix[market_of(ticker) or "未知"] = mix.get(market_of(ticker) or "未知", 0) + 1
        mix_text = "、".join(f"{key}×{value}" for key, value in sorted(mix.items()))
        return _error("industry/no-universe",
                      f"过滤后没有可分析的标的（market={market_code or '未指定'}；"
                      f"平台组合 {len(weights)} 只，市场分布 {mix_text}）——"
                      f"这不是取数失败，而是该市场在组合里没有持仓/自选")

    if nav is None:
        equity_value = _value_of(_call_tool(v3_run, "equity", {"window": 30})) or {}
        current = v3_math.to_float(equity_value.get("current"))
        nav = current if current is not None and current > 0 else None
    nav_note = (f"value = 权重 × equity.current（{nav}）" if nav is not None
                else "value=null：equity.current 取不到（未用估算值顶替）")

    resolver = resolver or IndustryResolver(v3_run, sleep=sleep, plate_scan_limit=plate_scan_limit)
    mapping = {}
    missing = []
    zero_weight = []
    for ticker in universe:
        weight = _lookup_weight(weights, ticker)
        if weight is None:
            weight = 0.0
            zero_weight.append(ticker)
        entry, reason = resolver.industry_of(ticker)
        if entry is None:
            missing.append({"ticker": ticker, "reason": reason})
            continue
        mapping[ticker] = entry
        entry["weight"] = float(weight)
    if zero_weight:
        notes.append(
            f"{len(zero_weight)} 只标的在平台组合权重里没有对应的权重（{zero_weight[:3]}…）→ weightPct 按 0 计，不臆造权重"
            if len(zero_weight) > 3 else
            f"标的 {zero_weight} 在平台组合权重里没有对应项 → weightPct 按 0 计，不臆造权重"
        )

    if not mapping:
        reason_text = "；".join(f"{item['ticker']}：{item['reason']}" for item in missing[:3])
        return _error("industry/no-mapping",
                      f"没有任何标的取到行业分类（{reason_text or '未知原因'}）",
                      missing=missing, sources={"plate": None, "weights": weight_source})

    aggregated = {}
    for ticker, entry in mapping.items():
        bucket = aggregated.setdefault(entry["industry"], {"industry": entry["industry"],
                                                          "weight": 0.0, "tickers": [], "plates": []})
        bucket["weight"] += entry["weight"]
        bucket["tickers"].append(ticker)
        for plate in entry.get("plates") or []:
            if plate not in bucket["plates"]:
                bucket["plates"].append(plate)
    exposures = []
    for bucket in aggregated.values():
        weight_pct = bucket["weight"] * 100.0
        exposures.append({
            "industry": bucket["industry"],
            "weightPct": round(weight_pct, 4),
            "tickers": sorted(bucket["tickers"]),
            "value": round(bucket["weight"] * nav, 2) if nav is not None else None,
            "plates": bucket["plates"],
        })
    exposures.sort(key=lambda row: (-row["weightPct"], row["industry"]))
    top = {"industry": exposures[0]["industry"], "weightPct": exposures[0]["weightPct"]} if exposures else None

    sources_used = resolver.sources_used or [PLATE_SOURCE_ORDER_PLATE]
    return {
        "ok": True,
        "as_of": now_iso(),
        "market": market_code or None,
        "universe_source": universe_source,
        "limitPct": limit_value,
        "breach": bool(top and top["weightPct"] > limit_value),
        "mapping": mapping,
        "exposures": exposures,
        "top": top,
        "sources": {
            "plate": "+".join(sources_used),
            "weights": weight_source,
            "nav": "sim-ledger(equity.current)" if nav is not None else None,
            "universe": universe_source,
        },
        "missing": missing,
        "notes": notes + list(resolver.notes) + [nav_note],
        "universe": universe,
    }


def register(app, v3_run, home, deps=None):
    """挂 ``GET /api/v3/risk/industry``。``deps`` 仅供测试注入（``nav``/``sleep``/``plate_scan_limit``）。"""
    deps = deps or {}

    @app.get("/api/v3/risk/industry")
    async def v3_risk_industry(tickers: str = "", market: str = "", limit_pct: float = 20):
        """行业映射与暴露（只读 GET）：板块归属来自富途，权重来自平台组合。"""

        def build():
            return industry_exposure(
                v3_run, home, tickers_raw=tickers, market=market, limit_pct=limit_pct,
                nav=deps.get("nav"), sleep=deps.get("sleep"),
                plate_scan_limit=deps.get("plate_scan_limit", DEFAULT_PLATE_SCAN_LIMIT),
            )

        try:
            return await asyncio.to_thread(build)
        except Exception as error:  # noqa: BLE001 —— 统一信封，不抛 500
            return _error("industry/internal", f"{type(error).__name__}: {error}")

    app.state.v3_industry = {"routes": ("/api/v3/risk/industry",)}
    return industry_exposure
