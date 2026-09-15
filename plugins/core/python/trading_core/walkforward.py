"""walk-forward（规格 §5.3）：每折仅用训练窗选参，测试窗拼接为 OOS 曲线；
报告强制自曝测试过的参数组数（多重检验）。"""
from . import portfolio, store


def _instance(base):
    """REGISTRY 存的是实例；策略无状态，按参数组合浅拷贝即可。"""
    import copy
    return copy.copy(base)


def run(conn, strategy_id, train, test, step, grid, start, end, benchmark="SH.000300"):
    from . import strategies
    base = strategies.REGISTRY[strategy_id]
    all_days = store.trading_days(conn, "SH", start, end)
    folds, oos_curve = [], []
    combos = _expand(grid)
    i = 0
    while i + train + test <= len(all_days):
        tr_end = all_days[i + train - 1]
        te_end = all_days[min(i + train + test - 1, len(all_days) - 1)]
        best, best_metric = None, -1e9
        for combo in combos:
            inst = _instance(base)
            _apply(inst, combo)
            r = _eval_window(conn, inst, all_days[i], tr_end, benchmark)
            if r["summary"].get("sharpe", -1e9) > best_metric:
                best, best_metric = combo, r["summary"].get("sharpe", -1e9)
        best_inst = _instance(base)
        _apply(best_inst, best)
        r = _eval_window(conn, best_inst, all_days[i], te_end, benchmark)
        seg = r["equity"][-test:]
        for p in seg:
            oos_curve.append(p)
        folds.append({"train_end": tr_end, "test_end": te_end, "params": best,
                      "oos_sharpe": r["summary"].get("sharpe")})
        i += step
    sharpes = [f["oos_sharpe"] for f in folds if f["oos_sharpe"] is not None]
    return {"folds": folds, "oos_curve": oos_curve,
            "summary": {"param_groups": len(combos),
                        "folds": len(folds),
                        "oos_sharpe": round(sum(sharpes) / len(sharpes), 3) if sharpes else None,
                        "train": train, "test": test, "step": step}}


def _expand(grid):
    keys = list(grid)
    out = [{}]
    for k in keys:
        out = [dict(c, **{k: v}) for c in out for v in grid[k]]
    return out or [{}]


def _apply(inst, combo):
    for k, v in combo.items():
        setattr(inst, k, v)


def _eval_window(conn, inst, start, end, benchmark):
    """窗口内直接按月调 target_weights 生成持仓并估值（复用 portfolio.run 的
    约束引擎，但策略实例已带参）。"""
    return portfolio.run(conn, inst, start=start, end=end, benchmark=benchmark)
