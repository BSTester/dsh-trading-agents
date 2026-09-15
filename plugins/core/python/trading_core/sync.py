"""同步层：bars 增量/回填、复权因子、财务报表、公告日合并、成分股快照。

原则（规格 §四）：取数一律走 trading_datasource（唯一实现）；失败如实上报，
不写占位行；回填进度落 kv 游标，可断点续传；富途调用间节流（复用 futu_mcp 退避，
此处只控制调用频率）。
"""
import datetime as _dt
import re
import time

from trading_datasource.futu_mcp import call_tool
from trading_datasource.market import load_bars, load_raw_bars, to_futu_symbol

from . import store

_TZ8 = _dt.timezone(_dt.timedelta(hours=8))  # 富途毫秒时间戳按 UTC+8 零点对齐

SLEEP_SECONDS = 0.5            # 富途通道调用间隔；测试注入 0
PROGRESS_KEY_BACKFILL = "backfill:bars:1d"
BACKFILL_LIMIT = 2000          # market.MAX_BARS 上限内（load_raw_bars 分块拼满长历史）


def _sleep(seconds):
    if seconds:
        time.sleep(seconds)


def sync_bars_incremental(conn, ticker, period="1d", loader=None):
    """仅支持 1d：增量按自然日折算根数，分钟级请用 backfill_bars（period 透传）。
    增量与回填统一富途原始价（autype=0），复权由 adjustments 派生。
    """
    if period != "1d":
        raise ValueError("增量同步仅支持 1d（分钟级根数无法按自然日折算）")
    loader = loader or load_raw_bars
    symbol = to_futu_symbol(ticker)   # 落库统一 futu 格式；loader 仍收原 ticker
    last = store.last_bar_date(conn, symbol, period)
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
    rows = store.upsert_bars(conn, symbol, period, fresh, source)
    return {"ticker": symbol, "rows": rows, "source": source, "stale": stale,
            "last": store.last_bar_date(conn, symbol, period)}


def backfill_bars(conn, tickers, period="1d", limit=BACKFILL_LIMIT,
                  loader=None, sleep_seconds=None, progress_key=PROGRESS_KEY_BACKFILL):
    """全量回填：每标的一次 load_raw_bars——富途原始价分块（≤370 根/页向后翻页），
    复权口径由 adjustments 表派生（规格 §4.2 规则 2：落库一律原始价 + 因子表）；
    失败记录不中断；kv 游标（done/failed）支持断点续传——重复调用只处理未完成标的。
    前置：存量长历史缺口先 backfill 一次；增量入口（sync_bars_incremental）只覆盖近期窗口。"""
    loader = loader or load_raw_bars
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
            symbol = to_futu_symbol(ticker)   # 落库统一 futu 格式；loader/游标仍用原 ticker
            store.upsert_bars(conn, symbol, period, bars, source)
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
    futu_symbol = to_futu_symbol(ticker)
    data = fetcher("quote_corporate_actions_rehab",
                   {"symbol": futu_symbol, "divi_mode": divi_mode}) or {}
    rows = [{"ex_date": r["ex_div_date"],
             "cum_forward": r.get("cum_forward_adj_factorA"),
             "cum_backward": r.get("cum_backward_adj_factorA"),
             "actions": r.get("action_types") or []}
            for r in (data.get("rehabs") or []) if r.get("ex_div_date")]
    return store.upsert_adjustments(conn, futu_symbol, rows, "futu/rehab")


def sync_fundamentals(conn, ticker, fetcher=None):
    """财务报表：quote_financials_statements → 4 个核心字段。
    富途不含公告日，announced_at 落 NULL，由 merge_announcements_akshare 补齐。"""
    fetcher = fetcher or call_tool
    futu_symbol = to_futu_symbol(ticker)
    data = fetcher("quote_financials_statements", {"symbol": futu_symbol}) or {}
    rows = []
    for report in data.get("report_list") or []:
        stamp = report.get("date_time")
        if not stamp:
            continue  # 无报告期：无法定位 PIT，宁缺毋假
        period_end = ms_to_date(stamp)
        items = {i.get("display_name"): i.get("data") for i in report.get("item_list") or []}
        for field, aliases in FIELD_ALIASES.items():
            present = next((a for a in aliases if a in items), None)
            if present is None:
                continue
            raw = items[present]
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                continue  # 非数值科目 → 缺指标（宁缺毋假，quality.py 同口径）
            rows.append({"field": field, "period_end": period_end, "value": float(raw)})
    return store.upsert_fundamentals(conn, futu_symbol, rows, "futu/statements")


UNIVERSE_BIAS_NOTE = "当前成分快照，未含历史成分，含幸存者偏差（规格 §13.4 缺口②降级）"


def merge_announcements_akshare(conn, period, akshare_module=None):
    """缺口①（规格 §13.4）：A股公告日双源合并。

    period 形如 "20260630"（报告期）。一次调用覆盖全市场当期业绩表，
    逐行把「股票代码+报告期」匹配到的 fundamentals 行补上公告日。
    只补 announced_at IS NULL 的行，不覆盖已合并的来源。
    """
    ak = akshare_module
    if ak is None:
        import akshare as ak
    df = ak.stock_yjbb_em(date=period)
    if df is None or len(df) == 0:
        return {"matched": 0, "rows": 0}
    period_end = f"{period[:4]}-{period[4:6]}-{period[6:8]}"
    code_col = next((c for c in df.columns if "股票代码" in c), None)
    date_col = next((c for c in df.columns if "公告" in c), None)
    if code_col is None or date_col is None:
        raise ValueError(f"yjbb 列缺失（实际列：{sorted(df.columns)}）")
    matched = skipped = 0
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        symbol = to_futu_symbol(code)
        announced = str(row[date_col]).strip()[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", announced):
            skipped += 1  # 脏日期（如 "nan"）入库即 PIT 永久不可见，宁缺毋假
            continue
        matched += store.set_announced_at(conn, symbol, period_end, announced, "akshare/yjbb")
    return {"matched": matched, "rows": int(len(df)), "skipped": skipped}


def sync_universe(conn, index_symbol, as_of, fetcher=None, bias_note=UNIVERSE_BIAS_NOTE,
                  limit=50):
    """指数成分快照：quote_valuation_index_component_stock_list 键集分页（limit≤50；
    实测协议 2026-09-14：游标在响应的 pagination.next_key，stop 于 pagination.has_more=false，
    tools/list schema 同口径）。bias_note 承载缺口②的幸存者偏差标注。"""
    fetcher = fetcher or call_tool
    symbols, next_key, pages = [], None, 0
    while pages < 40:  # 页数上限：2000 成分 / 50 每页，兼防服务端游标异常循环
        args = {"symbol": index_symbol, "limit": limit}
        if next_key is not None:
            args["next_key"] = next_key
        data = fetcher("quote_valuation_index_component_stock_list", args) or {}
        page = data.get("stock_list") or []
        symbols.extend(r.get("symbol") for r in page if r.get("symbol"))
        pagination = data.get("pagination") or {}
        next_key = pagination.get("next_key")
        pages += 1
        if not pagination.get("has_more") or not page or next_key is None:
            break
    # 成分返回的是 futu 格式（SH.600519）；universe 统一存 6 位裸码（is_a_share 约定）
    bare = sorted({s.split(".")[-1] if "." in s else s for s in symbols})
    store.store_universe(conn, as_of, index_symbol, bare, "futu/component_stock_list", bias_note)
    return len(bare)
