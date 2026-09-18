"""auto_execute 作业体（WP9 拆分自 daemon.py，规格 §4.3）：九守卫 → 写 execute_plan 指令。

与 daemon 的分工：本模块只做「自动执行」这一件事的判定与落指令；作业表的装配在
``autopipeline``，调度循环与指令分派在 ``daemon``。``kill_path`` 归 daemon
（kill 文件约定的唯一实现），经函数内延迟导入取用——daemon 模块级 import 本模块，
反向只能延迟（仓库对逆向依赖的既有手法，见 watchlist.py 同款注释）。
"""
from . import alerts, store
from .autopipeline import auto_pipeline_config
from .clock import now_fn

#: 交易日守卫的告警标题（**稳定字面量**：``pipeline._ALERT_STATUS`` 按标题精确匹配，
#: 变量只进 detail）。分级 **info**：真实休市不是故障——与 ``planner.plan_auto`` 的非
#: 交易日软跳过同一语义（那里同样不写 warn）。
TRADING_DAY_ALERT_TITLE = "非交易日"


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


def auto_execute(conn, home, market, today=None, now=None):
    """auto_execute 作业体（规格 §4.3）：九守卫 → 写 execute_plan 指令。

    **与人工点击落完全相同的指令文件**（commands.write_command 同一实现），由指令轮询
    消费后走 handle_command → execute.run 的既有窄门（逐单风控 8 规则）；本函数不做任何
    旁路，也不直接触达券商。

    守卫 9 = **执行窗口**（规格 §4.3 第 9 条）：调度器 tick-first——启动即补跑当日已到期
    作业，没有窗口就会在收盘后补执行 09:35 的计划（风控规则 3 逐单拒单 + 当日计划被消耗）。
    窗口外一律不执行、留待人工（info 告警）。

    守卫 4b = **交易日**（WP18 新增的**纵深防御**）：规格 §4.3 的九守卫里没有交易日判定，
    因为线上调度器在链外已挡（``daemon.tick`` 非交易日不跑市场链）。但 CLI / MCP / E2E
    **直接调用**本函数时没有这道闸——实测 2026-05-01（法定假日）09:35 直调照样写出了
    ``execute_plan`` 指令，只靠下游 ``risk.pre_trade_checks`` 的规则 3 逐单拒单兜底。
    纵深防御要求同一道闸在作业体内也存在：日历未同步 → warn（与守卫 5+6 同分级）；
    非交易日 → info（真实休市不是故障，与 ``plan_auto`` 一致）。编号用 4b 而非重排，
    是为了不打乱规格 §4.3 已登记编号（5/6/7/8/9 与文档一一对应）。

    返回契约（**永不抛**——作业失败不拖垮调度链）::

      {"ok": True,  "skipped": <原因>}                 守卫未过/当日已执行（软跳过，退出 0）
      {"ok": False, "error": <原因>}                   配置/模式非法（fail-closed，退出 1）
      {"ok": True,  "nonce":…, "as_of":…, "plan":…}    指令已落盘

    守卫分级：总开关关闭=静默（默认态不是故障）；其余跳过=info（当日不执行是正常结论）；
    配置/模式非法=warn + ok=False（无人值守时必须让运维看得见）。

    时刻口径：``now()`` 只采样一次（today 与窗口判定共用同一样本），避免跨分钟抖动。
    """
    from . import commands, planner
    from .daemon import kill_path  # 延迟导入：kill 文件约定归 daemon（逆向依赖只能延迟）

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

    # 守卫 4：日内熔断。
    # 分级与可见性（2026-09-16 修订 I3）：halt 生效是**需要人介入**的状态，用 warn 并
    # 带上原因与设置时间——info 级会让自动链路静默停摆（实测：差异熔断后每天只是
    # info 跳过，页面上看不到任何异常）。每市场每日至多一条，不构成刷屏。
    # **不自动恢复**：清除 halt 永远由人工 clear_halt 决定（先查明原因）。
    halt = store.halt_state(conn)
    if halt and halt.get("active"):
        reason = halt.get("reason") or "未标注原因"
        return skip(f"熔断生效（{reason}，{halt.get('set_at') or '时间未知'}）："
                    f"先查明原因并人工 clear_halt 后再执行", "warn", "熔断生效")

    # 守卫 4b：交易日（**纵深防御**，见 docstring 与 wp18 用例）。
    # 线上调度器已在链外挡（非交易日不跑市场链），但 CLI/MCP/E2E 直调没有这道闸：
    # 实测法定假日 2026-05-01 09:35 直调本函数照样写出了 execute_plan 指令。
    # 分级与 plan_auto 一致：真实休市不是故障 → info；日历未同步 → warn（守卫 5+6 同款）。
    try:
        trading_day = store.is_trading_day(conn, market, today)
    except RuntimeError as error:
        return skip(f"日历未同步：{error}", "warn", "日历未同步")
    if not trading_day:
        return skip(f"{today} 非 {market} 交易日：不自动执行", "info",
                    TRADING_DAY_ALERT_TITLE)

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
