"""同步层：bars 增量/回填、复权因子、财务报表、公告日合并、成分股快照。

原则（规格 §四）：取数一律走 trading_datasource（唯一实现）；失败如实上报，
不写占位行；回填进度落 kv 游标，可断点续传；富途调用间节流（复用 futu_mcp 退避，
此处只控制调用频率）。
"""
import datetime as _dt
import time

from trading_datasource.market import load_bars

from . import store

SLEEP_SECONDS = 0.5            # 富途通道调用间隔；测试注入 0
PROGRESS_KEY_BACKFILL = "backfill:bars:1d"
BACKFILL_LIMIT = 2000          # market.MAX_BARS 上限内（路由层自动换源满足长历史）


def _sleep(seconds):
    if seconds:
        time.sleep(seconds)


def sync_bars_incremental(conn, ticker, period="1d", loader=None):
    loader = loader or load_bars
    last = store.last_bar_date(conn, ticker, period)
    if last is None:
        needed = 370                      # 首次增量 = 富途单次上限；更长历史交给 backfill
    else:
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
    kv 游标（done/failed）支持断点续传——重复调用只处理未完成标的。"""
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
