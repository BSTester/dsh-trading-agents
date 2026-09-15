"""多标的组合回测（规格 §5.3）：月度再平衡 + 成本 + A股现实约束 + 基准对比。

成本：佣金 0.03% 双边 + A股卖出印花税 0.1% + 滑点 0.1% 双边（与
trading_datasource.backtest 常数一致）。约束：T+1（当日买入不可卖）、
涨停不买/跌停不卖（±10% 判定）、停牌跳过、整手 100。
"""
import math

from . import store

COMMISSION, STAMP, SLIPPAGE = 0.0003, 0.001, 0.001
LOT = 100


def _is_a(code):
    return code.startswith(("SH.", "SZ.", "BJ."))


def run(conn, strategy, start, end, benchmark=None, rebalance="monthly"):
    days = store.trading_days(conn, "SH", start, end) if benchmark else []
    if benchmark and days:
        universe_dates = days
    else:
        row = conn.execute("SELECT MIN(ts),MAX(ts) FROM bars WHERE period='1d'").fetchone()
        if not row or not row[0]:
            raise RuntimeError("库内无 bars，先回填")
        universe_dates = store.trading_days(conn, "SH", row[0], row[1]) or []
    cash, shares, last_buy_day = 1_000_000.0, {}, {}
    curve, turnover_sum, rebal_months = [], 0.0, set()
    target = {}
    for day in universe_dates:
        # 月度再平衡：每月第一个交易日重算目标权重
        if day[5:7] not in rebal_months:
            rebal_months.add(day[5:7])
            target = strategy.target_weights(conn, _prev_as_of(conn, day))
        # 持仓估值用最近可用收盘（停牌当日无 bar，mark 不中断）
        day_equity = cash + sum(shares.get(s, 0) * _mark_close(conn, s, day) for s in shares)
        # 卖出（先卖后买，T+1 约束：当日买入不卖）
        for s in list(shares):
            px, lim = _close(conn, s, day), _limit_ok(conn, s, day)
            if px is None or not lim:
                continue
            want = target.get(s, 0.0) * day_equity
            have = shares[s] * px
            if have > want * 1.02 and day not in last_buy_day.get(s, set()):
                lots = int(min((have - want), shares[s] * px) / px / LOT) * LOT
                if lots >= LOT:
                    shares[s] -= lots
                    cash += lots * px * (1 - COMMISSION - STAMP - SLIPPAGE)
                    turnover_sum += lots * px
        # 买入
        for s, w in target.items():
            px, lim = _close(conn, s, day), _limit_ok(conn, s, day)
            if px is None or not lim:
                continue
            have = shares.get(s, 0) * px
            want = w * day_equity
            if want > have * 1.02:
                lots = int(min(want - have, cash) / px / LOT) * LOT
                if lots >= LOT:
                    cost = lots * px * (1 + COMMISSION + SLIPPAGE)
                    if cost <= cash:
                        shares[s] = shares.get(s, 0) + lots
                        cash -= cost
                        turnover_sum += lots * px
                        last_buy_day.setdefault(s, set()).add(day)
        curve.append({"t": day, "equity": day_equity})
    summary = _metrics(curve, turnover_sum,
                       _benchmark_curve(conn, benchmark, universe_dates))
    return {"summary": summary, "equity": curve, "target_last": target}


def _close(conn, symbol, day):
    rows = store.read_bars(conn, symbol, "1d", as_of=day, limit=1)
    return rows[-1]["c"] if rows and rows[-1]["t"] == day else None  # 停牌：当日无 bar


def _mark_close(conn, symbol, day):
    rows = store.read_bars(conn, symbol, "1d", as_of=day, limit=1)
    return rows[-1]["c"] if rows else 0.0  # 最近可用收盘（PIT）；从未有 bar 视为 0


def _limit_ok(conn, symbol, day):
    rows = store.read_bars(conn, symbol, "1d", as_of=day, limit=2)
    if len(rows) < 2 or rows[-1]["t"] != day:
        return False
    chg = rows[-1]["c"] / rows[0]["c"] - 1.0
    if _is_a(symbol) and abs(chg) >= 0.0995:  # 涨停不买/跌停不卖（近似判定）
        return False
    return True


def _prev_as_of(conn, day):
    days = store.trading_days(conn, "SH", "2000-01-01", day)
    idx = days.index(day)
    return days[idx - 1] if idx > 0 else day


def _benchmark_curve(conn, benchmark, dates):
    if not benchmark:
        return []
    return [(_close(conn, benchmark, d) or 0.0) for d in dates]


def _metrics(curve, turnover, bench):
    vals = [p["equity"] for p in curve]
    if len(vals) < 2:
        return {"param_groups": 1}
    rets = [vals[i + 1] / vals[i] - 1 for i in range(len(vals) - 1)]
    total = vals[-1] / vals[0] - 1
    years = len(vals) / 250
    annual = (1 + total) ** (1 / years) - 1
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(250)
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    br = [b for b in bench if b]
    b_total = (br[-1] / br[0] - 1) if len(br) > 1 else 0.0
    b_annual = (1 + b_total) ** (1 / years) - 1 if br else 0.0
    excess = [r - (b_annual / 250) for r in rets] if br else rets
    ir = 0.0
    if len(excess) > 2:
        ex_std = math.sqrt(sum((e - sum(excess) / len(excess)) ** 2
                               for e in excess) / (len(excess) - 1)) * math.sqrt(250)
        ir = (sum(excess) / len(excess)) / ex_std if ex_std else 0.0  # 零方差（空仓等）如实报 0
    return {"total_return": round(total, 4), "annual": round(annual, 4),
            "volatility": round(vol, 4),
            "sharpe": round(annual / vol, 3) if vol else 0.0,
            "max_drawdown": round(mdd, 4),
            "excess_annual": round(annual - b_annual, 4) if br else None,
            "information_ratio": round(ir, 3) if br else None,
            "turnover": round(turnover / (sum(vals) / len(vals)) / years, 2),
            "param_groups": 1}
