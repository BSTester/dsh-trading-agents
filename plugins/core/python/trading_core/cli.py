"""python -m trading_core 单入口：全部子命令输出 JSON，便于 Harness 对话读取。

网络类子命令（calendar/sync-bars/backfill/adjustments/fundamentals/merge-announcements/universe）
直连真实渠道；quality 只读本地库。默认库路径 $DSH_HOME/trading-data/trading.sqlite。
"""
import argparse
import datetime as _dt
import json

from . import calendar as cal
from . import quality, store, sync


def _add_db(parser):
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 $DSH_HOME/trading-data/trading.sqlite）")


def build_parser():
    p = argparse.ArgumentParser(prog="trading_core", description="量化平台数据基座 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("calendar", help="同步交易日历")
    s.add_argument("--market", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    _add_db(s)

    s = sub.add_parser("sync-bars", help="增量同步日线（--tickers 逗号分隔）")
    s.add_argument("--tickers", required=True, help="逗号分隔")
    _add_db(s)

    s = sub.add_parser("backfill", help="全量回填日线（断点续传）")
    s.add_argument("--tickers", required=True, help="逗号分隔")
    s.add_argument("--limit", type=int, default=sync.BACKFILL_LIMIT)
    _add_db(s)

    s = sub.add_parser("adjustments", help="同步复权因子")
    s.add_argument("--tickers", required=True, help="逗号分隔")
    _add_db(s)

    s = sub.add_parser("fundamentals", help="同步财务报表（公告日留空）")
    s.add_argument("--tickers", required=True, help="逗号分隔")
    _add_db(s)

    s = sub.add_parser("merge-announcements", help="合并 A 股公告日（akshare/yjbb）")
    s.add_argument("--period", required=True, help="报告期，如 20260630")
    _add_db(s)

    s = sub.add_parser("universe", help="同步指数成分快照")
    s.add_argument("--index", default="SH.000300")
    s.add_argument("--as-of", default=_dt.date.today().isoformat())
    _add_db(s)

    s = sub.add_parser("quality", help="质量报告（只读本地库）")
    s.add_argument("--market", default="SH")
    s.add_argument("--symbols", required=True, help="逗号分隔")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    _add_db(s)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = store.connect(args.db)
    try:
        if args.cmd == "calendar":
            result = {"days": cal.sync_calendar(conn, args.market, args.start, args.end)}
        elif args.cmd == "sync-bars":
            result = {"symbols": [sync.sync_bars_incremental(conn, t.strip())
                                  for t in args.tickers.split(",") if t.strip()]}
        elif args.cmd == "backfill":
            result = sync.backfill_bars(conn, [t.strip() for t in args.tickers.split(",") if t.strip()],
                                        limit=args.limit)
        elif args.cmd == "adjustments":
            result = {t: sync.sync_adjustments(conn, t.strip())
                      for t in args.tickers.split(",") if t.strip()}
        elif args.cmd == "fundamentals":
            result = {t: sync.sync_fundamentals(conn, t.strip())
                      for t in args.tickers.split(",") if t.strip()}
        elif args.cmd == "merge-announcements":
            result = sync.merge_announcements_akshare(conn, args.period)
        elif args.cmd == "universe":
            result = {"symbols": sync.sync_universe(conn, args.index, args.as_of),
                      "as_of": args.as_of,
                      "bias_note": sync.UNIVERSE_BIAS_NOTE}
        elif args.cmd == "quality":
            result = quality.full_report(conn, args.market,
                                         [t.strip() for t in args.symbols.split(",") if t.strip()],
                                         args.start, args.end)
        else:  # pragma: no cover - argparse 已约束
            raise ValueError(f"未知子命令 {args.cmd}")
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0
