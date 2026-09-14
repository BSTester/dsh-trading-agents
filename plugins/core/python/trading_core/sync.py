"""同步层：bars 增量/回填、复权因子、财务报表、公告日合并、成分股快照。

原则（规格 §四）：取数一律走 trading_datasource（唯一实现）；失败如实上报，
不写占位行；回填进度落 kv 游标，可断点续传；富途调用间节流（复用 futu_mcp 退避，
此处只控制调用频率）。
"""
import datetime as _dt
import time

from trading_datasource.futu_mcp import call_tool
from trading_datasource.market import load_bars, to_futu_symbol

from . import store

_TZ8 = _dt.timezone(_dt.timedelta(hours=8))  # 富途毫秒时间戳按 UTC+8 零点对齐

SLEEP_SECONDS = 0.5            # 富途通道调用间隔；测试注入 0
PROGRESS_KEY_BACKFILL = "backfill:bars:1d"
BACKFILL_LIMIT = 2000          # market.MAX_BARS 上限内（路由层自动换源满足长历史）


def _sleep(seconds):
    if seconds:
        time.sleep(seconds)


def sync_bars_incremental(conn, ticker, period="1d", loader=None):
    """仅支持 1d：增量按自然日折算根数，分钟级请用 backfill_bars（period 透传）。"""
    if period != "1d":
        raise ValueError("增量同步仅支持 1d（分钟级根数无法按自然日折算）")
    loader = loader or load_bars
    last = store.last_bar_date(conn, ticker, period)
    if last is None:
        needed = 370                      # 首次增量 = 富途单次上限；更长历史交给 backfill
    else:
        # 自然日 +7 缓冲折算根数；last 异常落在未来时 since 为负，max(…,5) 兜底
        # （注：market.MIN_BARS=20 会把 <20 的请求抬到 20，此处 5 只是下限防御）；
        # 落后超过 370 根的存量缺口由 backfill 补，增量永不静默追平
        since = (_dt.date.today() - _dt.date.fromisoformat(last)).days + 7
        needed = min(370, max(since, 5))
    bars, source, stale = loader(ticker, period, needed)
    fresh = [b for b in bars if last is None or b["t"] > last]
    rows = store.upsert_bars(conn, ticker, period, fresh, source)
    return {"ticker": ticker, "rows": rows, "source": source, "stale": stale,
            "last": store.last_bar_date(conn, ticker, period)}


def backfill_bars(conn, tickers, period="1d", limit=BACKFILL_LIMIT,
                  loader=None, sleep_seconds=None, progress_key=PROGRESS_KEY_BACKFILL):
    """全量回填：每标的一次 load_bars（路由层自动满足长历史）；失败记录不中断；
    kv 游标（done/failed）支持断点续传——重复调用只处理未完成标的。
    前置：存量长历史缺口先 backfill 一次；增量入口（sync_bars_incremental）只覆盖近期窗口。"""
    loader = loader or load_bars
    sleep_seconds = SLEEP_SECONDS if sleep_seconds is None else sleep_seconds
    progress = store.kv_get(conn, progress_key, default={"done": [], "failed": {}})
    done = list(progress.get("done", []))
    failed = dict(progress.get("failed", {}))
    ok = []
    for ticker in tickers:
        if ticker in done:
            continue
        try:
            bars, source, stale = loader(ticker, period, limit)
            store.upsert_bars(conn, ticker, period, bars, source)
            failed.pop(ticker, None)
            done.append(ticker)
            ok.append(ticker)
        except Exception as error:  # noqa: BLE001 —— 单标的失败不中断回填
            failed[ticker] = str(error)[:160]
        store.kv_set(conn, progress_key, {"done": done, "failed": failed})
        _sleep(sleep_seconds)
    return {"ok": ok, "failed": failed,
            "done_total": len(done), "pending": [t for t in tickers if t not in done]}


# 富途科目名按市场不同（实测口径沿用 workbench/python/quality.py 的 alias 表，只取 4 键）。
FIELD_ALIASES = {
    "revenue": ["Total Revenue as Reported", "Total Revenue", "Total Operating Revenue",
                "Operating Revenue"],
    "net_profit": ["Net Profit", "Net Income to Parent Company",
                   "Net Profit of Parent Company Owners"],
    "gross_profit": ["Gross Profit"],
    "diluted_eps": ["Diluted EPS"],
}


def ms_to_date(ms):
    """富途报告期毫秒时间戳 → YYYY-MM-DD（按 UTC+8 零点对齐，实测口径）。"""
    return _dt.datetime.fromtimestamp(ms / 1000, _TZ8).strftime("%Y-%m-%d")


def sync_adjustments(conn, ticker, divi_mode="include_divi", fetcher=None):
    """复权因子：quote_corporate_actions_rehab（炸弹工具，单标的调用；divi_mode
    默认 include_divi = A股/富途口径，schema 实测确认）。"""
    fetcher = fetcher or call_tool
    data = fetcher("quote_corporate_actions_rehab",
                   {"symbol": to_futu_symbol(ticker), "divi_mode": divi_mode}) or {}
    rows = [{"ex_date": r["ex_div_date"],
             "cum_forward": r.get("cum_forward_adj_factorA"),
             "cum_backward": r.get("cum_backward_adj_factorA"),
             "actions": r.get("action_types") or []}
            for r in (data.get("rehabs") or []) if r.get("ex_div_date")]
    return store.upsert_adjustments(conn, to_futu_symbol(ticker), rows, "futu/rehab")


def sync_fundamentals(conn, ticker, fetcher=None):
    """财务报表：quote_financials_statements → 4 个核心字段。
    富途不含公告日，announced_at 落 NULL，由 merge_announcements_akshare 补齐。"""
    fetcher = fetcher or call_tool
    futu_symbol = to_futu_symbol(ticker)
    data = fetcher("quote_financials_statements", {"symbol": futu_symbol}) or {}
    rows = []
    for report in data.get("report_list") or []:
        period_end = ms_to_date(report["date_time"])
        items = {i.get("display_name"): i.get("data") for i in report.get("item_list") or []}
        for field, aliases in FIELD_ALIASES.items():
            value = next((items[a] for a in aliases if items.get(a) is not None), None)
            if value is not None:
                rows.append({"field": field, "period_end": period_end, "value": float(value)})
    return store.upsert_fundamentals(conn, futu_symbol, rows, "futu/statements")
