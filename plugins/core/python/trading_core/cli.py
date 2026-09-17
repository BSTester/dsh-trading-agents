"""python -m trading_core 单入口：全部子命令输出 JSON，便于 Harness 对话读取。

网络类子命令（calendar/sync-bars/backfill/adjustments/fundamentals/merge-announcements/universe）
直连真实渠道；quality/factors-snapshot/factors-history 只动本地库（WP7 因子快照按日收集与查询）。
默认库路径 $DSH_HOME/trading-data/trading.sqlite。
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

    s = sub.add_parser("ic", help="因子 RankIC 序列 + 验证门报告（t 检验/分层/单调）")
    s.add_argument("--factor", default="momentum_60")
    s.add_argument("--symbols", required=True)
    s.add_argument("--as-of", required=True)
    s.add_argument("--horizon", type=int, default=20)
    # WP14 任务 3：多期 IC 序列取样参数。--step 缺省 0 = 按 horizon 取步长
    # （非重叠窗口，避免重叠收益把 IC 序列自相关带进 t 检验）。
    s.add_argument("--lookback", type=int, default=60, help="IC 观测期数（默认 60）")
    s.add_argument("--step", type=int, default=0, help="取样步长（0=按 horizon 非重叠）")
    _add_db(s)

    s = sub.add_parser("backtest", help="walk-forward 组合回测")
    s.add_argument("--strategy", default="momentum_value_top5")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--train", type=int, default=504)
    s.add_argument("--test", type=int, default=63)
    s.add_argument("--step", type=int, default=63)
    _add_db(s)

    s = sub.add_parser(
        "plan-build",
        help="生成并冻结计划（目标权重 JSON 内联提供）",
        description="目标权重是**上限**：数量 = min(权重定量, 风险预算定量)，"
                    "风险预算 = 权益 × risk_per_trade ÷ 2×ATR 止损距离；加仓受单笔风险"
                    "约束、减仓/清仓不受限；算不出 ATR 的标的进结果 skipped（不生成"
                    "必被风控规则 4 拦下的无止损全额定量）。与自动 build_plan 共用同一"
                    "定量口径（planner.build_and_freeze），手工路径无旁路。")
    s.add_argument("--mode", default="SIM")
    s.add_argument("--strategy", default="momentum_value_top5")
    s.add_argument("--target", required=True, help='JSON，如 {"SH.600519": 0.5}（权重上限）')
    s.add_argument("--prices", required=True, help="JSON，标的→限价")
    s.add_argument("--as-of", required=True)
    _add_db(s)

    s = sub.add_parser("plan-auto", help="自动计划生成（auto_pipeline：策略权重→冻结；按市场链）")
    s.add_argument("--market", required=True, help="市场链：SH/HK/US（SH 链含 SZ/BJ）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    s.add_argument("--today", default=None, help="as_of 覆盖 YYYY-MM-DD（测试/补跑用）")
    _add_db(s)

    s = sub.add_parser("auto-execute", help="自动执行（auto_pipeline：九守卫→执行已冻结计划指令）")
    s.add_argument("--market", required=True, help="市场链：SH/HK/US（SH 链含 SZ/BJ）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    s.add_argument("--today", default=None, help="日期覆盖 YYYY-MM-DD（测试/补跑用）")
    s.add_argument("--now", default=None,
                   help="当前时刻覆盖 YYYY-MM-DD HH:MM:SS（测试用；执行窗口按它判定）")
    _add_db(s)

    s = sub.add_parser("sentiment-snapshot",
                       help="情绪/资讯快照（fin_sentiment/fin_news/last30days；落 sentiment_snapshots）")
    s.add_argument("--market", required=True, help="市场链：SH/HK/US（SH 链含 SZ/BJ）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    s.add_argument("--today", default=None,
                   help="观测日覆盖 YYYY-MM-DD 或完整时刻（测试/补跑用）")
    s.add_argument("--now", default=None,
                   help="当前时刻覆盖 YYYY-MM-DD HH:MM:SS（测试用）")
    _add_db(s)

    s = sub.add_parser("research-snapshot",
                       help="研究数据 PIT 快照（F10 关键 section/做空/板块目录；"
                            "落 f10_snapshots/short_snapshots/plate_snapshots）")
    s.add_argument("--market", default=None,
                   help="市场链：SH/HK/US（SH 链含 SZ/BJ；--stats 时可省）")
    s.add_argument("--stats", action="store_true",
                   help="只读输出研究数据覆盖统计（不采集、不写库）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    s.add_argument("--today", default=None,
                   help="观测日覆盖 YYYY-MM-DD 或完整时刻（测试/补跑用）")
    s.add_argument("--now", default=None,
                   help="当前时刻覆盖 YYYY-MM-DD HH:MM:SS（测试用）")
    _add_db(s)

    s = sub.add_parser("reconcile-diff", help="离线比对本地与券商持仓 JSON")
    s.add_argument("--local", required=True)
    s.add_argument("--broker", required=True)
    _add_db(s)

    s = sub.add_parser("reconcile-daily",
                       help="每日对账（订单/持仓 vs 券商 → 差异告警+暂停 → TCA → digest）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    s.add_argument("--mode", default=None, help="账户模式覆盖 sim|live（默认读模式文件）")
    s.add_argument("--today", default=None, help="日期覆盖 YYYY-MM-DD（测试/补跑用）")
    _add_db(s)

    s = sub.add_parser("daemon", help="调度守护进程（--once 跑一轮；默认常驻轮询）")
    s.add_argument("--once", action="store_true", help="只跑一轮调度 + 指令轮询后退出")
    s.add_argument("--interval", type=int, default=60, help="常驻轮询秒数（默认 60）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    _add_db(s)

    # ── WP4 只读快照：Host 工作台经 pycore 取数的唯一通道（Node 不直读 SQLite）──
    s = sub.add_parser("snapshot-plan", help="计划快照（plans+orders+预检+告警，只读）")
    s.add_argument("--limit", type=int, default=10, help="告警条数（默认 10）")
    _add_db(s)

    s = sub.add_parser("snapshot-schedule", help="调度快照（心跳/作业/kill/halt，只读）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    _add_db(s)

    s = sub.add_parser("snapshot-reconcile", help="对账快照（差异/TCA/计划→订单→成交链，只读）")
    s.add_argument("--limit", type=int, default=5, help="链路包含的计划数（默认 5）")
    _add_db(s)

    # WP10 任务 1：流程快照（每市场「今日闭环跑到哪一步」，只读；流程页数据源）
    s = sub.add_parser("snapshot-pipeline", help="流程快照（每市场阶段状态+全局阶段，只读）")
    s.add_argument("--home", default=None, help="DSH_HOME 覆盖（默认 $DSH_HOME 或 ~/.dsh）")
    s.add_argument("--date", default=None, help="日期 YYYY-MM-DD（默认时钟口径今天）")
    s.add_argument("--limit", type=int, default=10, help="告警条数（默认 10）")
    _add_db(s)

    # ── WP7：因子快照（收盘作业链定时收集 + 历史查询；只动本地库）──
    s = sub.add_parser("factors-snapshot", help="横截面因子快照（factors.REGISTRY 全量，落 factor_snapshots）")
    s.add_argument("--tickers", required=True, help="逗号分隔")
    s.add_argument("--date", default=_dt.date.today().isoformat(), help="as_of，YYYY-MM-DD（默认今天）")
    _add_db(s)

    s = sub.add_parser("factors-history", help="因子快照历史（按日期倒序，只读）")
    s.add_argument("--limit", type=int, default=30, help="最多返回条数（默认 30）")
    _add_db(s)

    s = sub.add_parser("sentiment-history", help="情绪快照历史/当日采集摘要（只读）")
    s.add_argument("--symbol", default=None, help="标的代码；缺省返回最近一日采集摘要")
    s.add_argument("--limit", type=int, default=30, help="记录条数 1..120（默认 30）")
    _add_db(s)

    # ── WP14 任务 4：规则候选池（读/提案验证/人工批准）──
    s = sub.add_parser("rules-list", help="规则候选池列表（只读，可按状态过滤）")
    s.add_argument("--status", default=None,
                   help="按状态过滤（candidate/validating/passed/failed/enabled/disabled）")
    _add_db(s)

    s = sub.add_parser("rules-validate",
                       help="规则提案验证（协议校验 + 验证门：IC t 检验/分层单调 → passed/failed）")
    s.add_argument("--spec", required=True, help="spec JSON 文件路径；``-`` 表示从 stdin 读")
    s.add_argument("--symbols", default=None,
                   help="逗号分隔样本标的；缺省按规则 universe 解析关注池")
    s.add_argument("--as-of", default=None, help="评估日（默认取库内样本标的最新行情日）")
    s.add_argument("--horizon", type=int, default=20, help="前向收益窗口（默认 20）")
    s.add_argument("--lookback", type=int, default=60, help="IC 观测期数（默认 60）")
    s.add_argument("--step", type=int, default=0, help="取样步长（0=按 horizon 非重叠）")
    s.add_argument("--walkforward", action="store_true",
                   help="附跑 walk-forward OOS 摘要（补充证据；需同时给 --start/--end）")
    s.add_argument("--start", default=None, help="walk-forward 起点（--walkforward 时必填）")
    s.add_argument("--end", default=None, help="walk-forward 终点（--walkforward 时必填）")
    s.add_argument("--train", type=int, default=504, help="walk-forward 训练窗（默认 504）")
    s.add_argument("--test", type=int, default=63, help="walk-forward 测试窗（默认 63）")
    s.add_argument("--walk-step", type=int, default=63, help="walk-forward 步长（默认 63）")
    s.add_argument("--home", default=None,
                   help="DSH_HOME（读关注池/风控配置；默认 $DSH_HOME 或 ~/.dsh）")
    _add_db(s)

    return p


def _daemon_round(conn, home):
    """常驻循环的一轮：调度 + 指令轮询；失败指令进本轮摘要。

    轮询、告警与失败判定全部走 ``daemon`` 的单一实现（poll_commands/command_failure）——
    原先这里自带一份轮询与告警，与新增的服务内调度器重复且漏判 ok:False 形态。
    """
    from . import daemon as daemon_mod
    daemon_mod.tick(conn, home)
    issues = []
    for item in daemon_mod.poll_commands(conn, home):
        failure = daemon_mod.command_failure(item)
        if failure:
            issues.append({"file": failure[0], "error": failure[1]})
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


def _factors_snapshot(conn, tickers, date):
    """WP7：横截面因子快照——factors.REGISTRY 全量因子逐标的计算，payload 落库。

    因子计算唯一入口是 trading_core/factors.py 的既有实现（fn(conn, symbol, as_of)
    -> float|None，输入只有 PIT store 与 as_of），此处只做编排不另写计算；测试经替换
    REGISTRY 注入替身。任一计算异常或全部因子无数据时抛错（不落库），由调用方落
    {ok:false, error} 信封。
    """
    from . import factors
    tickers = [t for t in tickers if t]
    if not tickers:
        raise ValueError("tickers 为空")
    registry = factors.REGISTRY
    per_ticker = {t: {name: fn(conn, t, date) for name, fn in registry.items()}
                  for t in tickers}
    if all(value is None for row in per_ticker.values() for value in row.values()):
        raise RuntimeError(f"无可用因子数据（{len(tickers)} 标的，as_of={date}）")
    payload = {"date": date, "tickers": per_ticker, "computed_at": store._now(),
               "note": f"registry={','.join(registry)}"}
    store.save_factor_snapshot(conn, date, payload)
    return {"ok": True, "date": date, "tickers": len(per_ticker)}


def _ic_sample_dates(conn, symbols, as_of, lookback, step):
    """多期 IC 的取样日期：≤ as_of 的并集交易日后，从末尾每隔 step 取一个（升序返回）。

    步长缺省为 horizon（非重叠窗口）——重叠收益会把自相关带进 IC 序列、
    让 t 统计虚高，这是刻意的口径选择而非精度损失。
    """
    dates = set()
    for symbol in symbols:
        for bar in store.read_bars(conn, symbol, "1d", as_of=as_of, limit=lookback * step + 1):
            if bar["t"] <= as_of:
                dates.add(bar["t"])
    ordered = sorted(dates)
    if not ordered:
        return []
    window = ordered[-lookback * step - 1:]
    return window[::-1][::step][::-1]


def _ic_result(conn, args):
    """`ic` 子命令实现：as_of 当期 RankIC（既有键不动）+ 多期验证门报告（新增键）。

    键集与 WP2 一致（``factor``/``rank_ic``/``samples``）；``samples`` 按**因子值**
    样本数计，与 ``rank_ic`` 是否算得出无关。``rank_ic`` 依赖前向收益——阶段 B 起
    统一走 ``factors.forward_return``（精确窗口），因此在**最新 bar 日**（前向窗口
    尚不存在）如实为 ``null``，不再用近似窗口编造一个值（旧口径在此处返回过 1.0）；
    要看有前向窗口的日期，把 ``--as-of`` 指到历史日期即可。
    新增 ``rank_ic_mean``/``t_stat``/``p_value``/``n``/``layers``/``monotonic``
    与门槛结论 ``passes_gate``/``gate_reasons``。
    """
    from . import factors
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    factor_fn = factors.REGISTRY[args.factor]
    step = args.step if args.step and args.step > 0 else max(args.horizon, 1)
    lookback = max(args.lookback, 1)
    # ① 当期（as_of）截面：既有输出键的来源
    vals = {s: factor_fn(conn, s, args.as_of) for s in symbols}
    vals = {k: v for k, v in vals.items() if v is not None}
    fwd = {}
    for symbol in vals:
        value = factors.forward_return(conn, symbol, args.as_of, args.horizon)
        if value is not None:
            fwd[symbol] = value
    # ② 多期面板：逐取样日算因子值与前向收益 → ic_report
    factor_panel, forward_panel = {}, {}
    for date in _ic_sample_dates(conn, symbols, args.as_of, lookback, step):
        day_factors = {s: factor_fn(conn, s, date) for s in symbols}
        day_factors = {k: v for k, v in day_factors.items() if v is not None}
        day_forward = {}
        for symbol in day_factors:
            value = factors.forward_return(conn, symbol, date, args.horizon)
            if value is not None:
                day_forward[symbol] = value
        if day_factors:
            factor_panel[date], forward_panel[date] = day_factors, day_forward
    report = factors.ic_report(factor_panel, forward_panel)
    ok, reasons = factors.passes_gate(report)
    return {"factor": args.factor, "rank_ic": factors.rank_ic(vals, fwd),
            "samples": len(vals), **report,
            "passes_gate": ok, "gate_reasons": reasons,
            "lookback": lookback, "step": step, "horizon": args.horizon}


# ---------------------------------------------------------------------------
# WP14 任务 4：规则候选池 CLI（列表 / 提案验证）
# ---------------------------------------------------------------------------
# 两条子命令的分工与退出码口径：
#   * rules-list     只读列表（候选池 UI 与运维查看同一出口）；
#   * rules-validate 提案入口 + 验证门：**协议非法 = 非零退出**（提案写错了，是操作
#     错误）；**验证门不通过 = 退出 0 + status="failed"**（研究结论，不是故障——
#     与 plan-auto 的软跳过同一分级，脚本据 status 字段分流而不是据退出码）。
# **批准/停用刻意没有 CLI 子命令**（`rules-decide` 已删除）：批准只能由人在独立 Web
# 的研究页候选池点击（服务进程内动作端点 → `rule_engine.decide_rule`）。历史缺陷是
# CLI 与 Web 走同一条路径、操作来源靠 `--by` 自报，于是 shell 也能写出
# `approved_by='web'`——「批准只在 Web」形同虚设（规格 §9.4/§9.5）。离线无批准路径
# 不是遗漏，是设计：任何能被脚本调用的批准入口都是模型自批的入口。
# 验证门统计一律复用 ``factors.ic_report``/``passes_gate``（阈值唯一实现在 factors），
# 本模块**不重写任何阈值**；取样口径与 ``ic`` 子命令逐字一致（``_ic_sample_dates`` +
# ``factors.forward_return``——前向收益只有一份实现，与 ``ic_weighted`` 运行时同源，
# 否则「批准时的 IC」与「运行时的 IC」按不同前向收益计算），保证验证报告能被
# ``ic`` 子命令手工复核。


def _home_of(args):
    """``--home`` > ``$DSH_HOME`` > ``~/.dsh``（与 daemon/watchlist 同一口径）。"""
    import os
    return args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")


def _rule_public(row):
    """rules 表行 → 候选池公开形状（spec 摊平 + provenance 保留，不重复塞原文）。"""
    spec = row["spec"] or {}
    return {"rule_id": row["rule_id"], "hypothesis": spec.get("hypothesis"),
            "factors": spec.get("factors"), "combine": spec.get("combine"),
            "universe": spec.get("universe"), "top_n": spec.get("top_n"),
            "rebalance": spec.get("rebalance"), "provenance": spec.get("provenance"),
            "status": row["status"], "validation": row["validation"],
            "approved_by": row["approved_by"], "approved_at": row["approved_at"],
            "created_at": row["created_at"]}


def _rules_list(conn, args):
    """候选池列表（可按状态过滤）。未知状态 fail-closed——不静默返回全量冒充「过滤后」。"""
    from . import rule_engine
    status = args.status
    if status is not None and status not in rule_engine.RULE_STATUSES:
        return {"ok": False,
                "error": (f"未知规则状态 {status!r}"
                          f"（允许：{'/'.join(rule_engine.RULE_STATUSES)}）")}
    return {"ok": True,
            "rules": [_rule_public(row) for row in store.get_rules(conn, status=status)]}


def _latest_bar_date(conn, symbols):
    """样本标的里最新的日线日期（评估日缺省值）；全无行情返回 None。"""
    dates = [store.last_bar_date(conn, symbol, "1d") for symbol in symbols]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def _rule_panels(conn, spec, symbols, as_of, horizon, lookback, step, registry=None):
    """多期因子面板 + 共享前向收益面板（口径 = ``ic`` 子命令的取样实现）。

    返回 ``(factor_panel, forward_panel)``：``factor_panel`` 形如
    ``{因子名: {日期: {标的: 值}}}``，``forward_panel`` 形如 ``{日期: {标的: 前向收益}}``
    ——与 ``factors.ic_report`` 的入参形状逐项一致。因子函数异常按缺值处理
    （单因子失败不中止整轮，宁缺毋假）。
    """
    from . import factors
    registry = factors.REGISTRY if registry is None else registry
    step = step if step and step > 0 else max(horizon, 1)
    names = list(spec["factors"])
    factor_panel = {name: {} for name in names}
    forward_panel = {}
    for day in _ic_sample_dates(conn, symbols, as_of, lookback, step):
        forward = {}
        for symbol in symbols:
            value = factors.forward_return(conn, symbol, day, horizon)
            if value is not None:
                forward[symbol] = value
        if not forward:
            continue
        forward_panel[day] = forward
        for name in names:
            values = {}
            for symbol in symbols:
                try:
                    value = registry[name](conn, symbol, day)
                except Exception:  # noqa: BLE001 —— 单因子异常按缺值处理
                    value = None
                if value is not None:
                    values[symbol] = value
            if values:
                factor_panel[name][day] = values
    return factor_panel, forward_panel


def _walkforward_summary(conn, spec, args):
    """walk-forward OOS 摘要（**补充证据**，不参与 ``passes_gate`` 门槛）。

    口径：把规则按固定参数（``top_n`` 取 spec 值 → 单组合网格，无参数搜索）注册成
    策略后复用既有 ``walkforward.run``；``--walkforward`` 需同时给 ``--start/--end``
    （与 ``backtest`` 子命令同口径，不凭空造时间窗）。任何跑不起来的情形都如实记录
    ``status``/``reason``——**绝不假装跑过**（规格 §9.3 的诚实要求）。
    """
    from . import strategies, walkforward
    if not args.start or not args.end:
        return {"status": "not_run",
                "reason": "--walkforward 需同时给 --start 与 --end（与 backtest 同口径）"}
    try:
        strategies.register_rule(spec)  # 回测按 strategy_id 取实例
    except ValueError as error:
        return {"status": "failed", "reason": f"规则注册失败：{str(error)[:120]}"}
    try:
        result = walkforward.run(conn, spec["rule_id"], train=args.train, test=args.test,
                                 step=args.walk_step,
                                 grid={"top_n": [int(spec["top_n"])]},
                                 start=args.start, end=args.end)
    except Exception as error:  # noqa: BLE001 —— 数据/日历缺口即「跑不了」，如实记录
        return {"status": "failed", "reason": f"walk-forward 未跑通：{str(error)[:120]}"}
    folds = result.get("folds") or []
    if not folds:
        return {"status": "no_folds",
                "reason": "数据窗口不足，未产出 OOS 折（未参与门槛判定）",
                "param_groups": result.get("summary", {}).get("param_groups")}
    return {"status": "ok", "summary": result.get("summary"), "folds": len(folds)}


def _rule_gate(conn, spec, symbols=None, as_of=None, horizon=20, lookback=60, step=0,
               home=None, registry=None, walkforward_args=None):
    """验证门：逐因子产出 IC 报告 + 门槛结论，落成可入 ``rules.validation`` 的报告。

    判定口径（**不重写阈值**）：
      * 机械门槛 = ``factors.passes_gate(ic_report(...))`` **逐因子**全过才算通过
        （规则由多个因子合成，任一因子没有可验证的预测力就不该上岗）；
      * ``half_life``/``turnover`` 是**补充证据**：半衰期取各期 RankIC 序列的 lag-1
        自相关估计（单位=采样间隔），换手率取该因子 Top-N（N=规则 ``top_n``）成员
        的新进比例均值——两者都**不参与**门槛判定，报告里显式标注；
      * 样本不足（关注池为空 / 库内无行情 / 无可取样日期）→ 直接 ``failed`` 并给出
        原因，**不伪造通过**。
    """
    from . import factors, rule_engine
    validation = {"checked_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "universe": spec.get("universe"), "top_n": spec.get("top_n"),
                  "horizon": horizon, "lookback": lookback,
                  "step": step if step and step > 0 else max(horizon, 1),
                  "factors": {}, "passed": False, "gate_reasons": [],
                  "note": "半衰期/换手率为补充证据，不参与 passes_gate 门槛"}
    try:
        if symbols is None:
            symbols = rule_engine.resolve_universe(spec, home=home)
    except ValueError as error:
        validation["gate_reasons"] = [f"关注池不可用：{error}"]
        return validation
    symbols = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    validation["symbols"] = len(symbols)
    if not symbols:
        validation["gate_reasons"] = ["样本不足：关注池为空（无标的可验证）"]
        return validation
    as_of = as_of or _latest_bar_date(conn, symbols)
    if as_of is None:
        validation["gate_reasons"] = ["样本不足：库内无该批标的的日线（先跑 sync-bars）"]
        return validation
    validation["as_of"] = as_of
    factor_panel, forward_panel = _rule_panels(conn, spec, symbols, as_of, horizon,
                                               lookback, step, registry=registry)
    if not forward_panel:
        validation["gate_reasons"] = [
            f"样本不足：{as_of} 起的取样窗口内算不出前向收益（历史长度不够）"]
        return validation
    top_n = int(spec.get("top_n") or 1)
    for name in spec["factors"]:
        panel = factor_panel.get(name) or {}
        report = factors.ic_report(panel, forward_panel)
        passed, reasons = factors.passes_gate(report)
        ic_series = [factors.rank_ic(panel[day], forward_panel[day])
                     for day in sorted(set(panel) & set(forward_panel))]
        top_sets = [set(sorted(panel[day], key=panel[day].get, reverse=True)[:top_n])
                    for day in sorted(panel)]
        validation["factors"][name] = {
            **report,
            "half_life": factors.factor_half_life([x for x in ic_series if x is not None]),
            "turnover": factors.turnover(top_sets),
            "passes_gate": passed, "gate_reasons": reasons}
        if not passed:
            validation["gate_reasons"].append(f"{name}：" + "；".join(reasons))
    validation["passed"] = not validation["gate_reasons"]
    validation["walkforward"] = (_walkforward_summary(conn, spec, walkforward_args)
                                if walkforward_args is not None
                                else {"status": "not_run", "reason": "未请求（--walkforward 启用）"})
    return validation


def _rules_validate(conn, args):
    """提案入口 + 验证门（见本节顶部退出码口径）。"""
    import sys as _sys
    from . import rule_engine
    try:
        text = (_sys.stdin.read() if args.spec == "-"
                else Path(args.spec).read_text(encoding="utf-8"))
        spec = json.loads(text)
    except (OSError, ValueError) as error:
        return {"ok": False, "error": f"spec 读取失败：{error}"}
    ok, errors = rule_engine.validate_spec(spec)
    if not ok:
        return {"ok": False, "error": "规则协议校验失败", "errors": errors}
    rule_id = spec["rule_id"]
    existing = store.find_rule(conn, rule_id)
    if existing is None:
        store.upsert_rule(conn, rule_id, spec, status="candidate")  # 提案入库
        current = "candidate"
    else:
        current = existing["status"]
        if current not in ("candidate", "validating"):
            return {"ok": False,
                    "error": (f"规则已存在且状态为 {current}：重新验证需修改 rule_id 后"
                              "重新提案（不静默重置已通过/已启用的规则）")}
    if current == "candidate":
        rule_engine.set_rule_status(conn, rule_id, "validating")
    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else None)
    validation = _rule_gate(conn, spec, symbols=symbols, as_of=args.as_of,
                            horizon=args.horizon, lookback=args.lookback, step=args.step,
                            home=_home_of(args), walkforward_args=args if args.walkforward
                            else None)
    status = "passed" if validation["passed"] else "failed"
    rule_engine.set_rule_status(conn, rule_id, status, validation=validation)
    return {"ok": True, "rule_id": rule_id, "status": status, "validation": validation}


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
            result = _ic_result(conn, args)
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
        elif args.cmd == "plan-auto":
            # auto_pipeline 的 build_plan 作业体（WP9）。软跳过=退出 0（当日不生成计划
            # 是正常结论）；配置/模式非法=fail-closed 非零退出，让调度链与运维看得到。
            import os
            from . import planner
            home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
            result = planner.plan_auto(conn, home, args.market, today=args.today)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False, indent=1))
                return 1
        elif args.cmd == "auto-execute":
            # auto_pipeline 的 auto_execute 作业体（WP9）。软跳过=退出 0（当日不执行是
            # 正常结论——守卫拦截/已执行/无计划/超出执行窗口）；配置/模式非法=fail-closed
            # 非零退出，让调度链与运维看得到（计划与规格 §4.3 守卫 1/2 的分级一致）。
            # 假时钟入口：--now 优先，其次 DSH_FAKE_NOW（跨进程注入，见 daemon.now_stamp），
            # 最后真实时间。格式非法一律 fail-closed——不让写错的时间戳静默变成
            # 「总是超窗」或「总是命中」的判定。**时钟解析只有 daemon.now_fn 一处实现。**
            import os
            from . import daemon
            home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
            try:
                now = daemon.now_fn(args.now)
            except ValueError as error:
                print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
                return 1
            result = daemon.auto_execute(conn, home, args.market, today=args.today, now=now)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
                return 1
        elif args.cmd == "sentiment-snapshot":
            # 情绪/资讯 PIT 采集作业体（WP11）。软跳过（会话未收盘/池空/通道缺席）=
            # 退出 0：采集是攒历史，不是当日依赖，缺源不阻塞调度链；市场链/时钟非法
            # =fail-closed 非零退出（与 plan-auto 同一分级）。
            import os
            from . import sentiment
            home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
            result = sentiment.run(home, args.market, conn=conn, today=args.today,
                                   now=args.now)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
                return 1
        elif args.cmd == "research-snapshot":
            # 研究数据 PIT 采集作业体（WP12 任务 5）。与 sentiment-snapshot 同一分级：
            # 软跳过（会话未收盘/池空/数据面未配置）=退出 0——研究数据是攒 PIT 历史
            # （250 交易日演进条款的地基），是**基础链**作业，与交易开关无关，未配置
            # 凭据也不阻塞调度链；市场链/时钟非法=fail-closed 非零退出。
            # --stats 只读覆盖统计（不采集、不写库）。
            import os
            from . import research_sync
            home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
            if args.stats:
                result = {"ok": True, **store.research_stats(conn)}
            elif not args.market:
                print(json.dumps(
                    {"ok": False, "error": "research-snapshot 需要 --market（或用 --stats）"},
                    ensure_ascii=False))
                return 1
            else:
                result = research_sync.run(home, args.market, conn=conn,
                                           today=args.today, now=args.now)
                if not result.get("ok"):
                    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
                    return 1
        elif args.cmd == "reconcile-diff":
            from . import reconcile
            diffs = reconcile.compare(
                conn, json.loads(Path(args.local).read_text()),
                json.loads(Path(args.broker).read_text()))
            # 最新差异落 kv：snapshot-reconcile 的工作台取数口径（对账差异持久化）
            store.kv_set(conn, "reconcile:latest",
                         {"diffs": diffs, "at": _dt.datetime.now().isoformat(timespec="seconds")})
            result = {"diffs": diffs}
        elif args.cmd == "reconcile-daily":
            # WP9 任务 6b：reconcile 作业体（规格 §4.4）。软跳过（live/无账户）=退出 0
            # （跳过是正常结论）；通道/模式失败=fail-closed 非零退出，让调度链与运维
            # 看得到——对账失败绝不静默成「无差异」。
            import os
            from . import reconcile
            home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
            result = reconcile.daily(conn, home, mode=args.mode, today=args.today)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
                return 1
        elif args.cmd == "snapshot-plan":
            from . import snapshots
            result = snapshots.plan_snapshot(conn, alert_limit=args.limit)
        elif args.cmd == "snapshot-schedule":
            import os
            from . import snapshots
            home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
            result = snapshots.schedule_snapshot(conn, home)
        elif args.cmd == "snapshot-reconcile":
            from . import snapshots
            result = snapshots.reconcile_snapshot(conn, chain_limit=args.limit)
        elif args.cmd == "snapshot-pipeline":
            import os
            from . import pipeline
            home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
            result = pipeline.pipeline_snapshot(conn, home, date=args.date,
                                                alert_limit=args.limit)
        elif args.cmd == "factors-snapshot":
            # 无数据/计算失败：{ok:false, error} + 非零退出（daemon runner 只看退出码，
            # 干净 JSON 让调用方拿得到原因，而不是一屏 traceback）。
            try:
                result = _factors_snapshot(
                    conn, [t.strip() for t in args.tickers.split(",") if t.strip()], args.date)
            except Exception as error:  # noqa: BLE001 —— 失败信封即本子命令的输出契约
                print(json.dumps({"ok": False, "error": str(error)[:300]},
                                 ensure_ascii=False, indent=1))
                return 1
        elif args.cmd == "factors-history":
            result = {"ok": True, "snapshots": store.list_factor_snapshots(conn,
                                                                          limit=args.limit)}
        elif args.cmd == "sentiment-history":
            # 两种形态共用同一出口（records/summary 两键恒在，服务端形状校验单一）：
            #   给 --symbol → 该标的倒序记录；不给 → 最近一日的采集摘要。
            if args.symbol:
                result = {"ok": True, "symbol": args.symbol, "summary": None,
                          "records": store.read_sentiments(conn, args.symbol,
                                                           limit=args.limit)}
            else:
                result = {"ok": True, "symbol": None, "records": [],
                          "summary": store.sentiment_summary(conn)}
        elif args.cmd == "rules-list":
            result = _rules_list(conn, args)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False, indent=1))
                return 1
        elif args.cmd == "rules-validate":
            result = _rules_validate(conn, args)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False, indent=1))
                return 1
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
