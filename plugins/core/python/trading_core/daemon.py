"""调度守护进程（规格 §8.1）：无 LLM 单进程；按交易日历触发作业链；
心跳落 ~/.dsh/trading-daemon.json；指令目录轮询分派见 handle_command。

边界（规格 §8.4 恢复原则）：cancel_plan 只本地撤销未提交（draft/frozen）订单，
在途订单留给对账兜底，绝不自动清除 unknown 状态。
kill 文件约定 ~/.dsh/trading-kill（WP3 risk.py ctx["kill_path"] 同一事实）。"""
import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import alerts, commands, execute, store

JOBS_DEFAULT = {
    # WP7：每个有作业的市场收盘链末尾追加 factors_snapshot（先让数据作业落库，
    # 快照再吃当日数据）；cmd 形式走既有 runner——@watchlist 替换 / 900s 超时 /
    # 失败告警语义与协议零改动。
    "SH": [{"name": "sync_bars", "at": "16:00", "cmd": ["sync-bars", "--tickers", "@watchlist"]},
            {"name": "sync_fundamentals", "at": "16:00", "cmd": ["fundamentals", "--tickers", "@watchlist"]},
            {"name": "merge_announcements", "at": "16:05", "cmd": ["merge-announcements", "--period", "@latest-quarter"]},
            {"name": "quality", "at": "16:10", "cmd": ["quality", "--market", "SH"]},
            {"name": "factors_snapshot", "at": "16:15", "cmd": ["factors-snapshot", "--tickers", "@watchlist"]}],
    "HK": [{"name": "sync_bars", "at": "16:30", "cmd": ["sync-bars", "--tickers", "@watchlist"]},
            {"name": "factors_snapshot", "at": "16:35", "cmd": ["factors-snapshot", "--tickers", "@watchlist"]}],
    "US": [{"name": "sync_bars", "at": "05:30", "cmd": ["sync-bars", "--tickers", "@watchlist"]},
            {"name": "factors_snapshot", "at": "05:35", "cmd": ["factors-snapshot", "--tickers", "@watchlist"]}],
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


#: auto_pipeline 默认值（规格 §4.1）：默认关闭是硬约束——enabled=False 时调用方
#: （plan-auto / auto-execute 作业）必须与「功能未实现」逐字节等价。exec_at 为北京
#: 时间，美股按夏令时写（冬令时需人工调配置，不做自动 DST 换算）。
#: exec_window_minutes = 执行窗口分钟数（规格 §4.3 守卫 9）：调度器 tick-first——服务
#: 启动即补跑当日到期作业，没有窗口就会在收盘后补执行 09:35 的计划（被风控规则 3
#: 逐单拒单并消耗当日计划）。超窗一律不执行、留待人工。
AUTO_PIPELINE_DEFAULTS = {
    "enabled": False,
    "strategies": [],
    "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
    "exec_window_minutes": 30,
    "reconcile_at": "19:00",
}
#: 合法市场（与 store 交易日历的市场键同一集合）
AUTO_PIPELINE_MARKETS = ("SH", "HK", "US")
#: 策略项字段：未知键报错——拼错的键被静默忽略等于策略没生效，比报错更危险
_AUTO_STRATEGY_KEYS = ("market", "strategy", "watchlist")
_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")


def _hhmm(value, field):
    """HH:MM 校验：格式 + 00-23/00-59 范围（非法抛 ValueError，fail-closed）。"""
    if not isinstance(value, str) or not _HHMM_RE.match(value):
        raise ValueError(f"{field} 需为 HH:MM 格式，收到 {value!r}")
    hour, minute = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError(f"{field} 时刻越界：{value!r}")
    return value


def _positive_int(value, field):
    """正整数校验（fail-closed）。

    bool 必须显式排除：Python 里 ``isinstance(True, int)`` 为真，而 ``exec_window_minutes:
    true`` 是写错的配置——当 1 分钟用会把窗口缩到几乎不可用，静默接受比报错更危险。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 需为正整数，收到 {value!r}")
    return value


def _auto_strategies(items):
    """策略项校验：结构/字段/市场/非空串；返回只含白名单键的新列表。"""
    if not isinstance(items, list):
        raise ValueError("auto_pipeline.strategies 需为列表")
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"auto_pipeline.strategies 每项需为对象，收到 {item!r}")
        unknown = set(item) - set(_AUTO_STRATEGY_KEYS)
        if unknown:
            raise ValueError(f"未知策略字段：{', '.join(sorted(unknown))}")
        missing = [key for key in _AUTO_STRATEGY_KEYS if key not in item]
        if missing:
            raise ValueError(f"策略项缺少字段：{', '.join(missing)}")
        if item["market"] not in AUTO_PIPELINE_MARKETS:
            raise ValueError(f"策略项 market 非法：{item['market']!r}")
        for key in ("strategy", "watchlist"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ValueError(f"策略项 {key} 需为非空字符串")
        out.append({key: item[key] for key in _AUTO_STRATEGY_KEYS})
    return out


def _auto_exec_at(overlay, defaults):
    """执行时刻表：给到的市场覆盖、未给到的市场补默认；未知市场报错。"""
    if not isinstance(overlay, dict):
        raise ValueError("auto_pipeline.exec_at 需为对象")
    unknown = set(overlay) - set(AUTO_PIPELINE_MARKETS)
    if unknown:
        raise ValueError(f"exec_at 未知市场：{', '.join(sorted(unknown))}")
    merged = dict(defaults)
    for market, at in overlay.items():
        merged[market] = _hhmm(at, f"auto_pipeline.exec_at.{market}")
    return merged


def auto_pipeline_config(home):
    """auto_pipeline 配置（规格 §4.1）：键缺省补默认；非法报错（不静默降级）。

    缺失与非法是两回事：缺失=用默认值（功能关闭是默认态），非法=ValueError——
    把非法静默降级成默认会让「配置写错了」伪装成「功能没开」。返回值是全新对象，
    调用方修改不会污染后续读取。"""
    cfg = copy.deepcopy(AUTO_PIPELINE_DEFAULTS)
    overlay = platform_config(home).get("auto_pipeline")
    if overlay is None:
        return cfg
    if not isinstance(overlay, dict):
        raise ValueError("auto_pipeline 需为对象")
    unknown = set(overlay) - set(AUTO_PIPELINE_DEFAULTS)
    if unknown:
        raise ValueError(f"未知 auto_pipeline 字段：{', '.join(sorted(unknown))}")
    if "enabled" in overlay:
        if not isinstance(overlay["enabled"], bool):
            raise ValueError("auto_pipeline.enabled 需为布尔值")
        cfg["enabled"] = overlay["enabled"]
    if "strategies" in overlay:
        cfg["strategies"] = _auto_strategies(overlay["strategies"])
    if "exec_at" in overlay:
        cfg["exec_at"] = _auto_exec_at(overlay["exec_at"], cfg["exec_at"])
    if "exec_window_minutes" in overlay:
        cfg["exec_window_minutes"] = _positive_int(
            overlay["exec_window_minutes"], "auto_pipeline.exec_window_minutes")
    if "reconcile_at" in overlay:
        cfg["reconcile_at"] = _hhmm(overlay["reconcile_at"], "auto_pipeline.reconcile_at")
    return cfg


#: 全局作业链的键（规格 §4.4）：承载不绑市场日历的作业（reconcile）。
GLOBAL_CHAIN = "GLOBAL"
#: build_plan 相对该市场 factors_snapshot 的偏移（分钟，规格 §4.1）——先让数据与
#: 因子快照落库，自动计划再吃当日数据。
BUILD_PLAN_OFFSET_MINUTES = 5


def _plus_minutes(hhmm, minutes):
    """HH:MM + 分钟（跨日回绕）；输入已由 _hhmm/常量保证格式合法。"""
    hour, minute = (int(part) for part in hhmm.split(":"))
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _minutes_of_day(hhmm):
    """HH:MM → 当日分钟数（0..1439）。格式非法抛 ValueError（调用方 fail-closed）。"""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


def _within_exec_window(stamp_hhmm, exec_at, window_minutes):
    """守卫 9 判定（规格 §4.3 第 9 条）：当前时刻是否落在执行窗口内。

    窗口是**半开区间** ``[exec_at, exec_at + window_minutes)``：配置值是「从 exec_at 起算
    的分钟数」——30 分钟窗口（09:35 起）覆盖 09:35..10:04，10:05 已在窗外。用半开而非
    闭区间，是为了让「窗口长度」与配置值逐分钟对齐（闭区间会得到 window_minutes+1 个
    分钟位）。

    **跨日**：``exec_at + window`` 越过当日 23:59 时按当日末分钟截断，不跨到次日——
    判定只在同一交易日内成立（tick 按当日 ran 标记去重，次日是另一条标记），截断方向
    保守（宁可少执行）。默认时刻表（SH 09:35 / HK 09:45 / US 22:35）都不会触发截断。
    """
    start = _minutes_of_day(exec_at)
    end = min(start + window_minutes, 24 * 60)
    return start <= _minutes_of_day(stamp_hhmm) < end


def _factors_at(chain):
    """该市场链上 factors_snapshot 的时点；链上没有则 None（不装配 build_plan）。"""
    for job in chain:
        if job["name"] == "factors_snapshot":
            return job["at"]
    return None


def build_jobs(home, conn=None):
    """作业表装配（规格 §4.1）：JOBS_DEFAULT + auto_pipeline 派生的自动作业。

    关闭态契约（硬约束）：enabled=False（或未配置）时返回值与 JOBS_DEFAULT
    **逐键逐值相等**——关闭功能不改变任何现有调度行为。

    开启态在既有链**链尾追加**（不改既有作业的时点与顺序）：

      * 各市场：build_plan（= 该市场 factors_snapshot + ``BUILD_PLAN_OFFSET_MINUTES``）、
        auto_execute（= ``auto_pipeline.exec_at[market]``）；
      * ``GLOBAL_CHAIN``：reconcile（= ``auto_pipeline.reconcile_at``）——**不查交易日历**
        （见 tick），只受时点与当日 ran 标记约束。

    作业体自身的软跳过（未启用/无匹配策略/数据未就绪/守卫拦截）由 ``plan-auto`` 与
    ``auto-execute`` 实现并留痕；这里不重复做关注池或策略门槛——避免两处判定漂移，
    作业「缺席」与「跳过」的原因都只有一处可见（告警表）。

    配置非法（ValueError）→ warn 告警 + 退化为纯 JOBS_DEFAULT：调度链不能因为一个
    写错的配置整体停摆（fail-soft 只在装配层；作业体内仍是 fail-closed）。
    """
    jobs = copy.deepcopy(JOBS_DEFAULT)
    try:
        cfg = auto_pipeline_config(home)
    except ValueError as error:
        if conn is not None:
            alerts.emit(conn, home=str(home), level="warn", title="auto_pipeline 配置非法",
                        detail=str(error)[:160])
        return jobs
    if not cfg["enabled"]:
        return jobs
    for market in AUTO_PIPELINE_MARKETS:
        chain = jobs.get(market)
        if chain is None:
            continue
        factors_at = _factors_at(chain)
        if factors_at is not None:
            chain.append({"name": "build_plan",
                          "at": _plus_minutes(factors_at, BUILD_PLAN_OFFSET_MINUTES),
                          "cmd": ["plan-auto", "--market", market]})
        chain.append({"name": "auto_execute", "at": cfg["exec_at"][market],
                      "cmd": ["auto-execute", "--market", market]})
    jobs[GLOBAL_CHAIN] = [{"name": "reconcile", "at": cfg["reconcile_at"],
                           "cmd": ["reconcile-daily"]}]
    return jobs


def resolve_command(cmd, home):
    """替换 @watchlist 占位为配置关注池；关注池为空返回 None（调用方跳过该作业）。"""
    cmd = list(cmd)
    if "@watchlist" in cmd:
        watchlist = ",".join(platform_config(home).get("watchlist") or [])
        if not watchlist:
            return None
        cmd[cmd.index("@watchlist")] = watchlist
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
    if price_of is None:
        price_of = lambda s: _last_close(conn, s, stamp)  # noqa: E731
    if stop_dist_of is None:
        stop_dist_of = lambda s: None  # noqa: E731 —— 保守口径：风险额按全额名义计
    ctx = {"mode": row["mode"], "kill_path": str(kill_path(home)),
           "equity": equity if equity is not None else 1_000_000.0,
           "positions_value": {}, "positions_count": 0, "day_pnl_pct": 0.0,
           "is_trading_day": bool(calendar_ok), "config": risk_config(home)}
    result = execute.run(conn, row["plan_id"], plan_hash, ctx, broker_call,
                         price_of=price_of, stop_dist_of=stop_dist_of)
    return {"ok": True, "plan_id": row["plan_id"], **result}


def auto_execute(conn, home, market, today=None, now=None):
    """auto_execute 作业体（规格 §4.3）：九守卫 → 写 execute_plan 指令。

    **与人工点击落完全相同的指令文件**（commands.write_command 同一实现），由指令轮询
    消费后走 handle_command → execute.run 的既有窄门（逐单风控 8 规则）；本函数不做任何
    旁路，也不直接触达券商。

    守卫 9 = **执行窗口**（规格 §4.3 第 9 条）：调度器 tick-first——启动即补跑当日已到期
    作业，没有窗口就会在收盘后补执行 09:35 的计划（风控规则 3 逐单拒单 + 当日计划被消耗）。
    窗口外一律不执行、留待人工（info 告警）。

    返回契约（**永不抛**——作业失败不拖垮调度链）::

      {"ok": True,  "skipped": <原因>}                 守卫未过/当日已执行（软跳过，退出 0）
      {"ok": False, "error": <原因>}                   配置/模式非法（fail-closed，退出 1）
      {"ok": True,  "nonce":…, "as_of":…, "plan":…}    指令已落盘

    守卫分级：总开关关闭=静默（默认态不是故障）；其余跳过=info（当日不执行是正常结论）；
    配置/模式非法=warn + ok=False（无人值守时必须让运维看得见）。

    时刻口径：``now()`` 只采样一次（today 与窗口判定共用同一样本），避免跨分钟抖动。
    """
    from . import commands, planner

    home = str(home)
    market = str(market).upper()
    now = now or now_fn()
    stamp = now()
    today = today or stamp[:10]

    def skip(reason, level="info", title=None):
        alerts.emit(conn, home=home, level=level, title=(title or reason)[:40],
                    detail=reason[:160])
        return {"ok": True, "skipped": reason}

    # 守卫 1：总开关（关闭=默认态，静默；非法=fail-closed）
    try:
        cfg = auto_pipeline_config(home)
    except ValueError as error:
        reason = f"auto_pipeline 配置非法：{error}"
        alerts.emit(conn, home=home, level="warn", title="auto_pipeline 配置非法",
                    detail=reason[:160])
        return {"ok": False, "error": reason}
    if not cfg["enabled"]:
        return {"ok": True, "skipped": "auto_pipeline 未启用"}

    # 守卫 2：只有 sim 自动执行；live 永远等人工（自动生成 ≠ 自动执行）
    try:
        mode = planner.read_mode(home)
    except ValueError as error:
        reason = f"账户模式非法：{error}"
        alerts.emit(conn, home=home, level="warn", title="账户模式非法", detail=reason[:160])
        return {"ok": False, "error": reason}
    if mode != "sim":
        return skip(f"{mode} 模式：计划等待人工执行（自动生成≠自动执行）",
                    "info", "计划等待人工执行")

    # 守卫 3：kill switch
    if kill_path(home).exists():
        return skip("kill switch 生效：拒绝自动执行", "info", "kill switch 生效")

    # 守卫 4：日内熔断
    if store.is_halted(conn):
        return skip("熔断生效：先查明原因并 clear_halt 后再执行", "info", "熔断生效")

    # 守卫 5+6：计划存在（auto+frozen，最近窗口内）且计划自身 mode=sim、market 一致
    # 窗口 2 的依据见 planner.recent_trading_days（跨市场日期空间容差）
    try:
        candidates = planner.recent_trading_days(conn, market, today, window=2)
    except RuntimeError as error:
        return skip(f"日历未同步：{error}", "warn", "日历未同步")
    plan = None
    for as_of in candidates:
        plan = store.get_latest_auto_plan(conn, market, as_of)
        if plan is not None:
            break
    if plan is None:
        return skip(f"无 auto 冻结计划（{market} 最近交易日 {'/'.join(candidates)}）",
                    "info", "无待执行计划")
    if str(plan.get("mode") or "").lower() != "sim":
        # 加固：live 期生成的计划不会被自动执行（防跨模式挂单）
        return skip(f"计划 {plan['plan_id']} 为 {plan.get('mode')} 模式：等待人工执行",
                    "info", "计划等待人工执行")
    if plan.get("market") != market:
        return skip(f"计划市场不符：{plan.get('market')} ≠ {market}", "warn", "计划市场不符")

    # 守卫 7：当日幂等（kv 标记；指令文件另有 nonce 幂等兜底）
    mark_key = f"auto_exec:{market}:{today}"
    if store.kv_get(conn, mark_key):
        return skip(f"当日已执行：{market} {today}", "info", "当日已执行")

    # 守卫 9：执行窗口（规格 §4.3 第 9 条）——写指令前的最后一道判定。
    # 位置说明：规格把「写指令（携带 expected_mode）」记为守卫 8，本函数把窗口判定放在
    # 写动作之前，编号沿用规格（8 = 写指令，9 = 执行窗口）。
    exec_at = cfg["exec_at"].get(market)
    if exec_at is None:
        # exec_at 表只覆盖 AUTO_PIPELINE_MARKETS（SH/HK/US）；作业市场传入表外键时
        # fail-closed 跳过，不 KeyError（永不抛契约）
        return skip(f"exec_at 未配置市场 {market}：拒绝自动执行", "warn", "exec_at 缺市场")
    try:
        in_window = _within_exec_window(stamp[11:16], exec_at, cfg["exec_window_minutes"])
    except ValueError as error:
        return skip(f"当前时刻非法：{error}", "warn", "时刻非法")
    if not in_window:
        return skip(f"已超执行窗口（{exec_at} 起 {cfg['exec_window_minutes']} 分钟，"
                    f"当前 {stamp[11:16]}）：计划等待人工执行", "info", "已超执行窗口")

    # 守卫 8：写指令（携带 expected_mode；指令处理侧既有复核兜底）
    nonce = commands.write_command(home, "execute_plan",
                                  {"plan_hash": plan["content_hash"],
                                   "expected_mode": "sim"})
    store.kv_set(conn, mark_key, {"plan_id": plan["plan_id"], "nonce": nonce,
                                  "as_of": plan["as_of"], "at": stamp})
    return {"ok": True, "nonce": nonce, "as_of": plan["as_of"], "plan": plan}


def _cancel_plan(conn, cmd):
    """撤余单：只本地撤销 draft/frozen（未提交）；在途订单不动，留给对账兜底。"""
    plan_hash = cmd.get("plan_hash")
    row = conn.execute(
        "SELECT plan_id FROM plans WHERE content_hash=? ORDER BY created_at DESC LIMIT 1",
        (plan_hash,)).fetchone() if plan_hash else None
    if row is None:
        return {"ok": False, "error": f"计划不存在 hash={plan_hash}"}
    cancelled, untouched = [], []
    for o in store.get_open_orders(conn, row["plan_id"]):
        if o["status"] in ("draft", "frozen"):
            oms_cancel(conn, o["client_order_id"])
            cancelled.append(o["client_order_id"])
        else:
            untouched.append({"id": o["client_order_id"], "status": o["status"]})
    return {"ok": True, "cancelled": cancelled, "untouched": untouched}


def oms_cancel(conn, client_order_id):
    from . import oms
    oms.transition(conn, client_order_id, "cancelled", err="cancel_plan")


def _real_now():
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 统一时钟口径（唯一实现）：tick 的 now= 注入传不进 cmd 形式作业的 CLI 子进程
# （调度链的真实结构是「父进程 tick → 子进程 CLI」），跨进程演练/测试靠
# DSH_FAKE_NOW 环境变量对齐时间线。
# ---------------------------------------------------------------------------
FAKE_NOW_ENV = "DSH_FAKE_NOW"

_fake_now_alerted = False  # 每进程一次（warn_fake_now 的去重位）


def now_stamp(explicit=None):
    """解析当前时刻字符串：显式注入 > ``DSH_FAKE_NOW`` > 真实时间。

    **两个来源都校验格式**（显式注入同样可能写错——CLI ``--now`` 是人工输入）：
    非法格式如实抛 ValueError，绝不静默回落真实时间。写错的时间戳静默回落会让演练
    结论失真（宁可让演练当场炸掉，也不给出一份看起来正常的假报告）。
    """
    raw = str(explicit) if explicit is not None else os.environ.get(FAKE_NOW_ENV)
    if raw is None:
        return _real_now()
    import datetime as dt
    try:
        dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        source = "--now" if explicit is not None else FAKE_NOW_ENV
        raise ValueError(f"{source} 需为 YYYY-MM-DD HH:MM:SS，收到 {raw!r}") from error
    return raw


def now_fn(explicit=None):
    """``now=`` 参数用的零参 callable：**采样一次**固定，避免跨分钟抖动。"""
    stamp = now_stamp(explicit)
    return lambda: stamp


def warn_fake_now(conn, home):
    """``DSH_FAKE_NOW`` 生效时告警一次（每进程一次；未设置/已告警/无 conn 均 no-op）。

    生产防误用：演练变量忘了清理会让整个调度时间线错乱（作业提前/滞后触发），
    因此必须让工作台告警列表可见，而不是只写在文档里。
    """
    global _fake_now_alerted
    raw = os.environ.get(FAKE_NOW_ENV)
    if not raw or _fake_now_alerted or conn is None:
        return False
    _fake_now_alerted = True
    alerts.emit(conn, home=str(home), level="warn", title="假时钟生效",
                detail=f"{FAKE_NOW_ENV}={raw}（演练/测试专用；用后 unset）"[:160])
    return True
