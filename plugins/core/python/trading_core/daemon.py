"""调度守护进程（规格 §8.1）：无 LLM 单进程；按交易日历触发作业链；
心跳落 ~/.dsh/trading-daemon.json；指令目录轮询分派见 handle_command。

边界（规格 §8.4 恢复原则）：cancel_plan 只本地撤销未提交（draft/frozen）订单，
在途订单留给对账兜底，绝不自动清除 unknown 状态。
kill 文件约定 ~/.dsh/trading-kill（WP3 risk.py ctx["kill_path"] 同一事实）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

from . import alerts, commands, execute, indicators, store
# WP9 拆分（规格 §3.1）：配置/作业装配 → autopipeline；自动执行作业体 → autopilot；
# 统一时钟 → clock。以下为**向后兼容再导出**——既有调用方（cli/planner/reconcile/
# server.scheduler/各测试）继续以 ``daemon.X`` 取用，无需改动。
from .autopipeline import (AUTO_PIPELINE_DEFAULTS, EXEC_WINDOW_MAX_MINUTES,  # noqa: F401
                           GLOBAL_CHAIN, auto_pipeline_config, build_jobs)
from .autopilot import auto_execute
from .clock import FAKE_NOW_ENV, _real_now, now_fn, now_stamp, warn_fake_now  # noqa: F401

JOBS_DEFAULT = {
    # WP7：每个有作业的市场收盘链末尾追加 factors_snapshot（先让数据作业落库，
    # 快照再吃当日数据）；cmd 形式走既有 runner——@watchlist 替换 / 900s 超时 /
    # 失败告警语义与协议零改动。
    # WP11：链尾再追加 sentiment_snapshot（factors_snapshot + 10 分钟）。它是**基础链**
    # 作业而非 auto_pipeline 派生作业：情绪/资讯采集是攒 PIT 历史（250 交易日演进条款
    # 的地基），与交易开关无关——auto_pipeline 关闭时也应持续积累；缺席源如实标
    # absent 并退出 0，不阻塞链。
    # WP12 任务 5：链尾再追加 research_snapshot（sentiment_snapshot + 5 分钟）。同样是
    # **基础链**作业（研究数据积累不受交易开关控制）：F10 关键 section/做空/板块目录每日
    # 落 PIT 表，是 250 交易日演进条款的数据地基；数据面未配置凭据时软跳过退出 0。
    # 错峰 5 分钟：与情绪采集的浏览器/网络负载分开，避免同刻争用同一通道。
    "SH": [{"name": "sync_bars", "at": "16:00", "cmd": ["sync-bars", "--tickers", "@watchlist"]},
            {"name": "sync_fundamentals", "at": "16:00", "cmd": ["fundamentals", "--tickers", "@watchlist"]},
            {"name": "merge_announcements", "at": "16:05", "cmd": ["merge-announcements", "--period", "@latest-quarter"]},
            {"name": "quality", "at": "16:10", "cmd": ["quality", "--market", "SH"]},
            {"name": "factors_snapshot", "at": "16:15", "cmd": ["factors-snapshot", "--tickers", "@watchlist"]},
            {"name": "sentiment_snapshot", "at": "16:25", "cmd": ["sentiment-snapshot", "--market", "SH"]},
            {"name": "research_snapshot", "at": "16:30", "cmd": ["research-snapshot", "--market", "SH"]}],
    "HK": [{"name": "sync_bars", "at": "16:30", "cmd": ["sync-bars", "--tickers", "@watchlist"]},
            {"name": "factors_snapshot", "at": "16:35", "cmd": ["factors-snapshot", "--tickers", "@watchlist"]},
            {"name": "sentiment_snapshot", "at": "16:45", "cmd": ["sentiment-snapshot", "--market", "HK"]},
            {"name": "research_snapshot", "at": "16:50", "cmd": ["research-snapshot", "--market", "HK"]}],
    "US": [{"name": "sync_bars", "at": "05:30", "cmd": ["sync-bars", "--tickers", "@watchlist"]},
            {"name": "factors_snapshot", "at": "05:35", "cmd": ["factors-snapshot", "--tickers", "@watchlist"]},
            {"name": "sentiment_snapshot", "at": "05:45", "cmd": ["sentiment-snapshot", "--market", "US"]},
            {"name": "research_snapshot", "at": "05:50", "cmd": ["research-snapshot", "--market", "US"]}],
}

# 风控参数默认值：镜像 engine/python/risk_config.py 的 DEFAULTS（venv 只链接
# trading_core/trading_datasource，engine 模块不可直接 import，故此处镜像键名，
# 变更时必须两处同步）。~/.dsh/trading-risk.json 覆盖默认值，未知键直接报错。
RISK_DEFAULTS = {"risk_per_trade": 0.01, "stop_atr_mult": 2.0, "max_positions": 5,
                 "daily_loss_limit_pct": 0.03, "max_position_pct": 0.25}


def heartbeat_path(home):
    return Path(home) / "trading-daemon.json"


def commands_dir(home):
    return Path(home) / "trading-commands"


def kill_path(home):
    return Path(home) / "trading-kill"


def write_heartbeat(home, payload):
    p = heartbeat_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def platform_config(home):
    """~/.dsh/trading-platform.json（调度表/关注池，规格 §架构 config.py 口径）。
    缺失/损坏按空配置跑，不阻塞调度；关注池空时占位作业跳过并告警。"""
    p = Path(home) / "trading-platform.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def resolve_command(cmd, home):
    """替换 @watchlist 占位为配置关注池；关注池为空返回 None（调用方跳过该作业）。

    读取经 ``watchlist`` 模块的唯一实现（WP9 修订 I4）：同步作业要吃全量关注池，
    故此处不做市场分片——分片由 planner/strategies 按市场各取所需。
    """
    from . import watchlist as watchlist_mod
    cmd = list(cmd)
    if "@watchlist" in cmd:
        symbols = watchlist_mod.watchlist_symbols(home)
        if not symbols:
            return None
        cmd[cmd.index("@watchlist")] = ",".join(symbols)
    return cmd


def _subprocess_runner(cmd):
    """cmd 形式作业的默认执行体：venv 同解释器跑 CLI 子进程（单作业 15 分钟超时）。"""
    home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    resolved = resolve_command(cmd, home)
    if resolved is None:
        return {"skipped": "关注池为空"}
    return subprocess.run([sys.executable, "-m", "trading_core", *resolved],
                          timeout=900).returncode


def tick(conn, home, jobs=None, now=None):
    """一轮调度：对每个市场判断「今日为交易日 且 当前时间 ≥ at 且 今日未跑」，
    满足则执行并记录。now 注入便于假时钟测试。

    作业表缺省走 ``build_jobs(home, conn)``（规格 §4.1：JOBS_DEFAULT + auto_pipeline
    派生的自动作业）。``GLOBAL_CHAIN`` 链**不查市场日历**（全局作业与单一市场无关，
    规格 §4.4），只受时点与当日 ran 标记约束。

    时钟口径：``now=`` 显式注入 > ``DSH_FAKE_NOW`` > 真实时间（``now_fn`` 采样一次）。
    """
    now = now or now_fn()
    warn_fake_now(conn, home)
    jobs = jobs or build_jobs(home, conn)
    state = store.kv_get(conn, "daemon:state", default={"ran": {}})
    stamp = now()
    for market, chain in jobs.items():
        if market != GLOBAL_CHAIN:
            try:
                trading_day = store.is_trading_day(conn, market, stamp[:10])
            except RuntimeError as error:  # 日历未同步：跳过该市场并告警，不拖垮循环
                alerts.emit(conn, home=str(home), level="warn", title="日历未同步",
                            detail=str(error)[:160])
                continue
            if not trading_day:
                continue
        for job in chain:
            key = f"{market}:{job['name']}:{stamp[:10]}"
            if state["ran"].get(key) or stamp[11:16] < job["at"]:
                continue
            _run_job(conn, job, home)
            state["ran"][key] = stamp
    store.kv_set(conn, "daemon:state", state)
    write_heartbeat(home, {"heartbeat": stamp,
                           "last_job": store.kv_get(conn, "daemon:last_job", ""),
                           "next": "见 trading-platform.json"})
    return state


def _run_job(conn, job, home, runner=None):
    """fn 形式直接调用（测试注入）；cmd 形式经 runner 跑 CLI 子进程。
    conn 允许为 None（纯 runner 注入的探测式调用），此时跳过 kv 记账。"""
    if "fn" in job:
        job["fn"]({"conn": conn, "home": home})
    else:
        (runner or _subprocess_runner)(job["cmd"])
    if conn is not None:
        store.kv_set(conn, "daemon:last_job", job["name"])


def handle_command(conn, home, cmd, executor=None, runner=None):
    """commands.poll 的分派器：白名单 5 种指令（规格 §8.2）。永不抛出——
    错误以 result 返回，交由 poll 移入 processed/，避免毒丸指令每轮重试。"""
    home = str(home)
    type_ = cmd.get("type")
    try:
        if type_ == "kill":
            kill_path(home).touch()
            return {"ok": True}
        if type_ == "unkill":
            kill_path(home).unlink(missing_ok=True)
            return {"ok": True}
        if type_ == "run_job":
            name = cmd.get("job")
            # 作业表与 tick 同一装配口径：手工 run_job 也能寻址自动作业
            # （build_plan/auto_execute/reconcile）——否则「作业已入链但指令说未知」
            # 会让运维以为作业不存在。
            job = next((j for chain in build_jobs(home, conn).values() for j in chain
                        if j["name"] == name), None)
            if job is None:
                return {"ok": False, "error": f"未知作业 {name}"}
            _run_job(conn, job, home, runner=runner)
            return {"ok": True}
        if type_ == "execute_plan":
            return (executor or _default_executor)(conn, home, cmd)
        if type_ == "cancel_plan":
            return _cancel_plan(conn, cmd)
        return {"ok": False, "error": f"未知指令 {type_}"}
    except Exception as error:  # noqa: BLE001 —— 指令级失败不阻塞轮询循环
        return {"ok": False, "error": str(error)[:160]}


def _default_executor(conn, home, cmd):
    """execute_plan 默认执行体：注入券商通道（与 plan_auto / reconcile.daily 同一默认口径）。

    WP9 e2e 暴露的接线缺口：本函数原先不注入 ``broker_call``，``_execute_plan`` 因
    「未接入券商通道」fail-closed 拒绝——自动执行写下的指令永远停在拒绝态。默认口径
    与 planner/reconcile 一致（惰性导入 futu_mcp.call_tool）；导入失败保持 None，
    由 ``_execute_plan`` 如实拒绝（绝不在无券商事实的情况下下单）。
    """
    broker_call = None
    try:
        from trading_datasource.futu_mcp import call_tool
        broker_call = call_tool
    except Exception:  # noqa: BLE001 —— 通道不可用：交给 _execute_plan 拒绝并留痕
        broker_call = None
    return _execute_plan(conn, home, cmd, broker_call=broker_call)


def poll_commands(conn, home):
    """轮询指令目录并分派（服务内调度器与 daemon CLI 共用同一实现）。

    断点背景（WP9 任务 7）：commands.poll 原先只在 daemon CLI 常驻模式里跑，服务内
    调度器只跑作业链——独立 daemon 未启动时，plan-execute/auto_execute 写下的指令
    永远无人处理。本函数把轮询并入调度 tick，使单进程也能闭环。

    返回 commands.poll 的结果列表（每条含 ``result`` 或 ``error``）。**永不抛出**：
    poll 自身逐条 try/except，这里再兜一层目录/IO 级故障——轮询是调度循环的附加段，
    任何故障都不该拖垮作业链。失败逐条落 warn 告警（含文件名与错误摘要）；文件仍被
    移入 processed/，毒丸指令不每轮重试（与 handle_command 的收敛口径一致）。
    """
    home = str(home)
    try:
        results = commands.poll(home, lambda cmd: handle_command(conn, home, cmd))
    except Exception as error:  # noqa: BLE001 —— 目录/IO 级故障：告警后放行
        _alert_command_failure(conn, home, "poll", str(error))
        return []
    for item in results:
        failure = command_failure(item)
        if failure:
            _alert_command_failure(conn, home, failure[0], failure[1])
    return results


def command_failure(item):
    """从 poll 结果项提取失败事实：返回 ``(where, detail)``，成功项返回 None。

    两类失败形态（handle_command 永不抛，故失败都从这里收敛）：
      * poll 自身捕获的异常项 —— ``{"file": …, "error": …}``（如非法 JSON）；
      * 分派器返回的失败结果 —— ``{"type": …, "result": {"ok": False, "error": …}}``
        （如白名单外指令、执行被拒）。
    轮询告警（本模块）与 CLI ``--once`` 摘要（cli._daemon_round）共用本判定，
    避免两处各写一份而漏掉 ok:False 形态。
    """
    detail = item.get("error")
    if detail is None:
        result = item.get("result")
        if not (isinstance(result, dict) and result.get("ok") is False):
            return None
        detail = result.get("error") or "指令失败"
    return item.get("file") or item.get("type") or "?", detail


def _alert_command_failure(conn, home, where, detail):
    """指令失败的 warn 告警；告警自身失败不得反噬轮询（conn 为 None 时静默跳过）。"""
    if conn is None:
        return
    try:
        alerts.emit(conn, home=home, level="warn", title="指令处理失败",
                    detail=f"{where}: {detail}"[:300])
    except Exception:  # noqa: BLE001
        pass


def risk_config(home):
    """~/.dsh/trading-risk.json 覆盖 RISK_DEFAULTS；未知键报错（静默失效更危险）。"""
    cfg = dict(RISK_DEFAULTS)
    p = Path(home) / "trading-risk.json"
    if p.exists():
        overlay = json.loads(p.read_text(encoding="utf-8"))
        unknown = set(overlay) - set(RISK_DEFAULTS)
        if unknown:
            raise ValueError(f"未知风控字段：{', '.join(sorted(unknown))}")
        cfg.update(overlay)
    return cfg


def _last_close(conn, symbol, as_of):
    bars = store.read_bars(conn, symbol, "1d", as_of=as_of, limit=1)
    return bars[-1]["c"] if bars else None


def _execute_plan(conn, home, cmd, broker_call=None, equity=None, today=None,
                  calendar_ok=None, price_of=None, stop_dist_of=None):
    """execute_plan 默认实现：按 content_hash 找冻结/已批准计划 → execute.run。

    券商通道必须注入（broker_call = futu_mcp.call_tool 同签名的 callable）；
    未注入一律拒绝执行——宁可拒绝，也不在无券商事实的情况下下单。
    权益/持仓/日亏损为离线保守默认（对齐 cli plan-build 离线口径），真实通道
    接入由运维层注入 broker_call 与 equity 覆盖（WP5）。
    """
    plan_hash = cmd.get("plan_hash")
    if not plan_hash:
        return {"ok": False, "error": "缺少 plan_hash"}
    if broker_call is None:
        return {"ok": False, "error": "daemon 未接入券商通道（需注入 broker_call）"}
    if store.is_halted(conn):
        return {"ok": False, "error": "熔断生效：先查明原因并 clear_halt 后再执行"}
    row = conn.execute(
        "SELECT plan_id, mode FROM plans WHERE content_hash=?"
        " AND status IN ('frozen','approved') ORDER BY created_at DESC LIMIT 1",
        (plan_hash,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"无可执行计划 hash={plan_hash}"}
    home = str(home)
    stamp = now_stamp()
    day = today or stamp[:10]
    if calendar_ok is None:
        market_row = conn.execute(
            "SELECT market FROM orders WHERE plan_id=? ORDER BY rowid LIMIT 1",
            (row["plan_id"],)).fetchone()
        try:
            calendar_ok = store.is_trading_day(conn, (market_row or {"market": "SH"})["market"], day)
        except RuntimeError:
            calendar_ok = False  # 日历缺失：不放行（宁可不执行）
    cfg = risk_config(home)
    if price_of is None:
        price_of = lambda s: _last_close(conn, s, stamp)  # noqa: E731
    if stop_dist_of is None:
        # 与计划定量**同源**（indicators.stop_distance 唯一实现，规格 §4.2 第 5 点）：
        # planner 按该距离定量、规则 4 按该距离校验，两侧自洽——计划不再生成必被拦下
        # 的徒劳订单，规则 4 也不会被 0 止损静默废除。ATR 取不到 → None → 规则 4 退回
        # 「全额名义」保守口径（如实反映数据缺口，宁可拒绝）。
        stop_dist_of = lambda s: indicators.stop_distance(  # noqa: E731
            conn, s, stamp, cfg["stop_atr_mult"])
    ctx = {"mode": row["mode"], "kill_path": str(kill_path(home)),
           "equity": equity if equity is not None else 1_000_000.0,
           "day_pnl_pct": 0.0,
           "is_trading_day": bool(calendar_ok), "config": cfg}
    # 持仓口径（规格 §4.2 第 7 点）：能取到券商事实就用真值，取不到保留离线保守默认。
    # 取不到时规则 5 对 SELL 按「本单全额名义」判定 → 大仓位退出会被拒（宁可拒绝，
    # 也不用未知持仓放宽硬规则）；取到真值后退出单才可能通过（见 execute._order_ctx）。
    positions_value, positions_count, ctx_source = _positions_ctx(
        conn, broker_call, row["plan_id"], row["mode"], price_of)
    ctx.update({"positions_value": positions_value, "positions_count": positions_count,
                "ctx_source": ctx_source})
    result = execute.run(conn, row["plan_id"], plan_hash, ctx, broker_call,
                         price_of=price_of, stop_dist_of=stop_dist_of)
    return {"ok": True, "plan_id": row["plan_id"], "ctx_source": ctx_source, **result}


def _positions_ctx(conn, broker_call, plan_id, mode, price_of):
    """执行侧持仓市值/持仓数：只读券商事实，取不到退回离线保守默认。

    **台账二分原则**：一律券商查询，绝不读本地 OMS 台账冒充实持仓（本地台账可能滞后
    于券商，用它喂规则 5/6 会让上限判定建立在错误基础上）。

    返回 ``(positions_value, positions_count, source)``，``source`` ∈
    ``{"broker", "offline_default"}``——调用方放进 ``ctx["ctx_source"]``，供
    ``execute._order_ctx`` 区分「持仓已知」与「持仓未知」两种判定情境：
    未知时绝不做卖出方向折算（那等于用未知持仓放宽规则 5）。

    计划的订单可能跨市场（手工计划），按订单里出现的市场逐个查询后合并；任一市场
    查询失败即整体退回离线默认——半个持仓表比没有更危险（规则 5 会把缺失的存量
    当成 0，从而放行叠加超限的单）。

    **已知口径限制（如实披露）**：``positions_count`` 只统计**计划所属市场**的持仓，
    跨市场总持仓数不可得——规则 6（最大持仓数）对「A 股已持 5 只、计划再买港股」这类
    跨市场叠加会低估。不做三市场全查的理由：任一市场没有账户会抛错，而按上面的口径
    会把整个 ctx 打成离线默认，**反过来封死本修订要放行的退出单**——用「更严的规则 6」
    换「退不掉仓」不是好交易。跨市场计数需要各市场查询可独立降级的口径，登记为后续项。
    """
    from . import broker as core_broker

    markets = [r["market"] for r in conn.execute(
        "SELECT DISTINCT market FROM orders WHERE plan_id=?", (plan_id,)).fetchall()]
    if not markets:
        return {}, 0, "offline_default"  # 无订单可执行：未取得任何券商事实，如实标注
    merged = {}
    for market in markets:
        try:
            positions, _equity = core_broker.positions_and_equity(
                broker_call, mode=mode, market=market)
        except Exception:  # noqa: BLE001 —— 取不到就整体退回离线默认（既有行为不回退）
            return {}, 0, "offline_default"
        merged.update(positions or {})
    value, count = {}, 0
    for symbol, info in merged.items():
        qty = int(info.get("qty") or 0)
        if not qty:
            continue
        count += 1
        px = price_of(symbol)
        if px:
            value[symbol] = abs(qty) * float(px)
    return value, count, "broker"


def _cancel_plan(conn, cmd):
    """撤余单：只本地撤销 draft/frozen（未提交）；在途订单不动，留给对账兜底。

    实现经 ``oms.cancel_pending``（唯一实现，2026-09-16 修订 I2）：与跨日计划过期
    （``store.cancel_stale_auto_plans``）共用同一口径——两处各写一遍曾让「过期计划
    留下孤儿 draft 单」的缺口出现过。
    """
    from . import oms
    plan_hash = cmd.get("plan_hash")
    row = conn.execute(
        "SELECT plan_id FROM plans WHERE content_hash=? ORDER BY created_at DESC LIMIT 1",
        (plan_hash,)).fetchone() if plan_hash else None
    if row is None:
        return {"ok": False, "error": f"计划不存在 hash={plan_hash}"}
    cancelled, untouched = oms.cancel_pending(conn, row["plan_id"], err="cancel_plan")
    return {"ok": True, "cancelled": cancelled, "untouched": untouched}
