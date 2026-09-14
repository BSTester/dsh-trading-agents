"""数据质量：缺口检测（按日历）、新鲜度、announced_at 覆盖率、跨源交叉校验。

宁缺毋假（规格 §4.2 规则 3）：缺口如实列出，不自动补假数据。
"""
import datetime as _dt

from trading_datasource.market import load_bars

from . import store

CROSS_SOURCE_TOLERANCE = 0.005  # 收盘价交叉校验容差 0.5%（规格 §4.3）


def gap_report(conn, market, symbol, start, end):
    expected = store.trading_days(conn, market, start, end)
    have = {b["t"] for b in store.read_bars(conn, symbol, "1d", as_of=end)}
    return [d for d in expected if d not in have]


def freshness(conn, symbol, period, today=None):
    last = store.last_bar_date(conn, symbol, period)
    if last is None:
        return {"last": None, "days_behind": None}
    today = today or _dt.date.today().isoformat()
    days = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(last)).days
    return {"last": last, "days_behind": days}


def announced_coverage(conn):
    return store.announced_coverage(conn)


def cross_source_check(conn, ticker, period="1d", sample=5,
                       tolerance=CROSS_SOURCE_TOLERANCE, loader=None):
    """抽样比对：库内最后 N 根收盘 vs 现取同源收盘，超容差记为不一致。"""
    loader = loader or load_bars
    today = _dt.date.today().isoformat()
    stored = store.read_bars(conn, ticker, period, as_of=today, limit=sample)
    if not stored:
        return []
    fresh, _, _ = loader(ticker, period, sample)
    by_date = {b["t"]: b["c"] for b in fresh}
    out = []
    for b in stored:
        other = by_date.get(b["t"])
        if other is None:
            continue
        if abs(other - b["c"]) > tolerance * max(abs(other), abs(b["c"]), 1e-9):
            out.append({"date": b["t"], "stored": b["c"], "fresh": other})
    return out


def full_report(conn, market, symbols, start, end, today=None):
    return {
        "market": market,
        "range": [start, end],
        "freshness": {s: freshness(conn, s, "1d", today=today) for s in symbols},
        "gaps": {s: gap_report(conn, market, s, start, end) for s in symbols},
        "announced_coverage": announced_coverage(conn),
    }
