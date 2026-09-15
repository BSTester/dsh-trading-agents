"""python -m trading_core 单入口：全部子命令输出 JSON，便于 Harness 对话读取。

网络类子命令（calendar/sync-bars/backfill/adjustments/fundamentals/merge-announcements/universe）
直连真实渠道；quality 只读本地库。默认库路径 $DSH_HOME/trading-data/trading.sqlite。
"""
import argparse
import datetime as _dt
import json
from pathlib import Path

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

    s = sub.add_parser("ic", help="因子 RankIC 序列")
    s.add_argument("--factor", default="momentum_60")
    s.add_argument("--symbols", required=True)
    s.add_argument("--as-of", required=True)
    s.add_argument("--horizon", type=int, default=20)
    _add_db(s)

    s = sub.add_parser("backtest", help="walk-forward 组合回测")
    s.add_argument("--strategy", default="momentum_value_top5")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--train", type=int, default=504)
    s.add_argument("--test", type=int, default=63)
    s.add_argument("--step", type=int, default=63)
    _add_db(s)

    s = sub.add_parser("plan-build", help="生成并冻结计划（目标权重 JSON 内联提供）")
    s.add_argument("--mode", default="SIM")
    s.add_argument("--strategy", default="momentum_value_top5")
    s.add_argument("--target", required=True, help='JSON，如 {"SH.600519": 0.5}')
    s.add_argument("--prices", required=True, help="JSON，标的→限价")
    s.add_argument("--as-of", required=True)
    _add_db(s)

    s = sub.add_parser("reconcile-diff", help="离线比对本地与券商持仓 JSON")
    s.add_argument("--local", required=True)
    s.add_argument("--broker", required=True)
    _add_db(s)

    s = sub.add_parser("daemon", help="调度守护进程（--once 跑一轮；默认常驻轮询）")
    s.add_argument("--once", action="store_true", help="只跑一轮调度 + 指令轮询后退出")
    s.add_argument("--interval", type=int, default=60, help="常驻轮询秒数（默认 60）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    _add_db(s)
    return p


def _daemon_round(conn, home):
    """常驻循环的一轮：调度 + 指令轮询；处理失败的指令告警（文件仍移入 processed/）。"""
    from . import alerts, commands
    from . import daemon as daemon_mod
    daemon_mod.tick(conn, home)
    issues = []
    for item in commands.poll(home, handler=lambda cmd: daemon_mod.handle_command(conn, home, cmd)):
        if item.get("error"):
            issues.append({"file": item.get("file"), "error": item["error"]})
            alerts.emit(conn, home=str(home), level="warn",
                        title="指令处理失败", detail=item["error"])
    return issues


def _daemon_loop(conn, home, once=False, interval=60):
    """--once 跑一轮返回摘要；默认常驻 interval 秒轮询，Ctrl-C 优雅退出。"""
    import time
    while True:
        issues = _daemon_round(conn, home)
        if once:
            return {"once": True, "issues": issues}
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return {"stopped": True, "issues": issues}


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
        elif args.cmd == "ic":
            from . import factors
            vals = {s.strip(): factors.REGISTRY[args.factor](conn, s.strip(), args.as_of)
                    for s in args.symbols.split(",")}
            vals = {k: v for k, v in vals.items() if v is not None}
            # 前向收益：取 as_of 之后的窗口（近似口径——交易日对齐由 bars 本身保证）
            as_of2 = (_dt.date.fromisoformat(args.as_of)
                      + _dt.timedelta(days=args.horizon * 2)).isoformat()
            fwd = {}
            for s in vals:
                bars = store.read_bars(conn, s, "1d", as_of=as_of2, limit=args.horizon)
                if len(bars) >= 2:
                    fwd[s] = bars[-1]["c"] / bars[0]["c"] - 1
            result = {"factor": args.factor, "rank_ic": factors.rank_ic(vals, fwd),
                      "samples": len(vals)}
        elif args.cmd == "backtest":
            from . import walkforward
            result = walkforward.run(conn, strategy_id=args.strategy, train=args.train,
                                     test=args.test, step=args.step,
                                     grid={"top_n": [3, 5]},
                                     start=args.start, end=args.end)
        elif args.cmd == "plan-build":
            from . import planner
            target = json.loads(args.target)
            prices = json.loads(args.prices)

            def broker_positions(mode):
                return {}, 1_000_000.0  # 无券商通道时的离线口径；真实通道走 daemon（WP4）

            result = planner.build_and_freeze(conn, mode=args.mode, strategy_id=args.strategy,
                                              target=target, broker_positions=broker_positions,
                                              prices=prices, as_of=args.as_of)
        elif args.cmd == "reconcile-diff":
            from . import reconcile
            result = {"diffs": reconcile.compare(
                conn, json.loads(Path(args.local).read_text()),
                json.loads(Path(args.broker).read_text()))}
        elif args.cmd == "daemon":
            import os
            home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
            result = _daemon_loop(conn, home, once=args.once, interval=args.interval)
        else:  # pragma: no cover - argparse 已约束
            raise ValueError(f"未知子命令 {args.cmd}")
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0
