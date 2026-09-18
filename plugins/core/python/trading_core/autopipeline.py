"""auto_pipeline 配置与作业表装配（WP9 拆分自 daemon.py，规格 §4.1/§4.4）。

职责单一：把「配置 → 作业表」这一件事做完——读配置、校验（fail-closed）、在既有
JOBS_DEFAULT 链尾派生自动作业（build_plan/auto_execute/reconcile）。

校验（``apply_overlay``）是**调度侧与设置页写入侧共用的唯一实现**：设置端点若另写
一套规则，两处迟早漂移（写页面放行的配置在作业侧报错，或反之）。

依赖方向：本模块 import ``alerts``（叶子）；``watchlist`` 与 ``daemon``（JOBS_DEFAULT、
platform_config）经**函数内延迟导入**——``daemon`` 模块级 import 本模块，反向只能延迟，
这是仓库对逆向依赖的既有手法（见 watchlist.py / strategies.py 的同款注释）。
"""
import copy
import re

from . import alerts

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
#: 执行窗口分钟数上界（2026-09-16 审查 N2）：窗口是「距 exec_at 多久内还可以自动执行」
#: 的守卫（规格 §4.3 守卫 9），越长越接近失效——240 分钟已跨过 A 股整个连续竞价时段，
#: 再大就不是窗口而是「当天任意时刻可执行」。设置页 InputNumber 的 max 镜像本常量
#: （前端镜像由 tests/test_wp10_locks.py 锁定，两侧漂移即测试失败）。
EXEC_WINDOW_MAX_MINUTES = 240
#: 策略项字段：未知键报错——拼错的键被静默忽略等于策略没生效，比报错更危险。
#: ``watchlist`` 为**池键名**（2026-09-16 修订 I1）：选择 ``trading-platform.json``
#: 里的命名池，缺省 ``watchlist``；它不决定市场范围（市场由 ``market`` 决定）。
_AUTO_STRATEGY_KEYS = ("market", "strategy", "watchlist")
_AUTO_STRATEGY_REQUIRED = ("market", "strategy")
_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")


def _hhmm(value, field):
    """HH:MM 校验：格式 + 00-23/00-59 范围（非法抛 ValueError，fail-closed）。"""
    if not isinstance(value, str) or not _HHMM_RE.match(value):
        raise ValueError(f"{field} 需为 HH:MM 格式，收到 {value!r}")
    hour, minute = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError(f"{field} 时刻越界：{value!r}")
    return value


def _positive_int(value, field, maximum=None):
    """正整数校验（fail-closed）；给了 ``maximum`` 就一并查上界。

    bool 必须显式排除：Python 里 ``isinstance(True, int)`` 为真，而 ``exec_window_minutes:
    true`` 是写错的配置——当 1 分钟用会把窗口缩到几乎不可用，静默接受比报错更危险。
    上界同理：超出 ``EXEC_WINDOW_MAX_MINUTES`` 的窗口让守卫名存实亡，宁可拒绝。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 需为正整数，收到 {value!r}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} 不得超过 {maximum}，收到 {value!r}")
    return value


def _auto_strategies(items):
    """策略项校验：结构/字段/市场/非空串；返回只含白名单键的新列表。

    ``watchlist`` 可省略（缺省池 ``watchlist``）——它选池子，不选市场；显式给出的
    池键若在配置里不存在，由策略层按 fail-closed 抛错并由作业入口软跳过告警
    （见 ``strategies.WatchlistRsiStrategy`` 与 ``watchlist.watchlist_symbols``）。
    省略时该键仍会以缺省值补全：下游（planner/策略）无需各自处理 None。
    """
    from . import watchlist as watchlist_mod
    if not isinstance(items, list):
        raise ValueError("auto_pipeline.strategies 需为列表")
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"auto_pipeline.strategies 每项需为对象，收到 {item!r}")
        unknown = set(item) - set(_AUTO_STRATEGY_KEYS)
        if unknown:
            raise ValueError(f"未知策略字段：{', '.join(sorted(unknown))}")
        missing = [key for key in _AUTO_STRATEGY_REQUIRED if key not in item]
        if missing:
            raise ValueError(f"策略项缺少字段：{', '.join(missing)}")
        if item["market"] not in AUTO_PIPELINE_MARKETS:
            raise ValueError(f"策略项 market 非法：{item['market']!r}")
        for key in ("strategy", "watchlist"):
            if key in item and (not isinstance(item[key], str) or not item[key].strip()):
                raise ValueError(f"策略项 {key} 需为非空字符串")
        out.append({"market": item["market"], "strategy": item["strategy"],
                    "watchlist": item.get("watchlist") or watchlist_mod.DEFAULT_POOL_KEY})
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


def apply_overlay(cfg, overlay):
    """把 overlay 校验并合并进 cfg，返回**新对象**；非法抛 ``ValueError``。

    校验只有这一份实现：调度侧（``auto_pipeline_config``）与设置页写入校验
    （platform 的 ``settings_api.save_auto_pipeline``）都调用本函数——两处各写一套
    必然漂移，届时「写页面放行的配置」会在作业侧报错，反之亦然。

    校验顺序即报错顺序（未知字段 → enabled → strategies → exec_at →
    exec_window_minutes → reconcile_at）：一个配置有多个错时先报结构错，便于定位。
    """
    if not isinstance(overlay, dict):
        raise ValueError("auto_pipeline 需为对象")
    unknown = set(overlay) - set(AUTO_PIPELINE_DEFAULTS)
    if unknown:
        raise ValueError(f"未知 auto_pipeline 字段：{', '.join(sorted(unknown))}")
    merged = copy.deepcopy(cfg)
    if "enabled" in overlay:
        if not isinstance(overlay["enabled"], bool):
            raise ValueError("auto_pipeline.enabled 需为布尔值")
        merged["enabled"] = overlay["enabled"]
    if "strategies" in overlay:
        merged["strategies"] = _auto_strategies(overlay["strategies"])
    if "exec_at" in overlay:
        merged["exec_at"] = _auto_exec_at(overlay["exec_at"], merged["exec_at"])
    if "exec_window_minutes" in overlay:
        merged["exec_window_minutes"] = _positive_int(
            overlay["exec_window_minutes"], "auto_pipeline.exec_window_minutes",
            maximum=EXEC_WINDOW_MAX_MINUTES)
    if "reconcile_at" in overlay:
        merged["reconcile_at"] = _hhmm(overlay["reconcile_at"], "auto_pipeline.reconcile_at")
        # 派生校验共用同一实现：过晚会跨日回绕（入队排到对账之前）→ 当场拒绝配置
        enqueue_time_after_reconcile(merged["reconcile_at"])
    return merged


def auto_pipeline_config(home):
    """auto_pipeline 配置（规格 §4.1）：键缺省补默认；非法报错（不静默降级）。

    缺失与非法是两回事：缺失=用默认值（功能关闭是默认态），非法=ValueError——
    把非法静默降级成默认会让「配置写错了」伪装成「功能没开」。返回值是全新对象，
    调用方修改不会污染后续读取。"""
    from .daemon import platform_config  # 延迟导入：daemon 模块级 import 本模块（逆向依赖）
    cfg = copy.deepcopy(AUTO_PIPELINE_DEFAULTS)
    overlay = platform_config(home).get("auto_pipeline")
    if overlay is None:
        return cfg
    return apply_overlay(cfg, overlay)


#: 全局作业链的键（规格 §4.4）：承载不绑市场日历的作业（reconcile、enqueue_research）。
GLOBAL_CHAIN = "GLOBAL"
#: build_plan 相对该市场 factors_snapshot 的偏移（分钟，规格 §4.1）——先让数据与
#: 因子快照落库，自动计划再吃当日数据。
BUILD_PLAN_OFFSET_MINUTES = 5
#: 研究任务入队（基础链）默认时刻与命令：**关闭态**（无 reconcile 作业）用这个固定点；
#: **开启态**由 ``build_jobs`` 按 ``reconcile_at`` + ``ENQUEUE_RESEARCH_AFTER_RECONCILE_MINUTES``
#: 派生，保证入队严格晚于当日 digest 落库（规格 §10.2/§10.3；审查 A-2）。
#: 命令与 daemon.JOBS_DEFAULT 共用（单一事实源），避免两处时刻/参数漂移。
ENQUEUE_RESEARCH_BASE_AT = "19:05"
ENQUEUE_RESEARCH_AFTER_RECONCILE_MINUTES = 5
ENQUEUE_RESEARCH_CMD = ["enqueue-research", "--market", "SH,HK,US"]

#: 日历同步作业（基础 GLOBAL 链，WP18）的时刻与命令。**18:50 = 早于对账（19:00）与
#: 研究入队（19:05）**：日历必须在当日对账/计划之前就位——这样「日历刚用尽」（当日
#: 市场链因超出覆盖被跳过）也能在**今晚同一轮**自愈，次日市场链照常跑。放在入队之后
#: 或更晚都不行：那样当日晚些时候的作业仍按旧日历判定，缺口要多挂一天。
#: 命令与 daemon.JOBS_DEFAULT 共用（单一事实源），避免两处命令/参数漂移。
CALENDAR_SYNC_AT = "18:50"
CALENDAR_SYNC_CMD = ["calendar-sync", "--market", "SH,HK,US"]


def _plus_minutes(hhmm, minutes):
    """HH:MM + 分钟（跨日回绕）；输入已由 _hhmm/常量保证格式合法。"""
    hour, minute = (int(part) for part in hhmm.split(":"))
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _factors_at(chain):
    """该市场链上 factors_snapshot 的时点；链上没有则 None（不装配 build_plan）。"""
    for job in chain:
        if job["name"] == "factors_snapshot":
            return job["at"]
    return None


def enqueue_time_after_reconcile(reconcile_at):
    """由 ``reconcile_at`` 派生研究入队时刻（+``ENQUEUE_RESEARCH_AFTER_RECONCILE_MINUTES``）。

    **单一实现**：配置校验（``apply_overlay``）与作业装配（``build_jobs``）共用。
    跨日回绕一律 ValueError（fail-closed）：``reconcile_at`` 过晚时派生时刻会回到次日
    凌晨，按时刻排序会把入队排到对账之前——「digest 先落库、入队紧随其后」的时序假设
    不成立。宁可拒绝配置，也不让 daily_brief 悄悄引用上一份摘要（审查 A-2）。
    """
    derived = _plus_minutes(reconcile_at, ENQUEUE_RESEARCH_AFTER_RECONCILE_MINUTES)
    if derived <= reconcile_at:
        raise ValueError(
            f"auto_pipeline.reconcile_at 过晚：{reconcile_at!r} + "
            f"{ENQUEUE_RESEARCH_AFTER_RECONCILE_MINUTES} 分钟跨日，研究入队会排到对账之前")
    return derived


def build_jobs(home, conn=None):
    """作业表装配（规格 §4.1）：JOBS_DEFAULT + auto_pipeline 派生的自动作业。

    关闭态契约（硬约束）：enabled=False（或未配置）时返回值与 JOBS_DEFAULT
    **逐键逐值相等**——关闭功能不改变任何现有调度行为。

    开启态在既有链**链尾追加**（不改既有作业的时点与顺序）：

      * 各市场：build_plan（= 该市场 factors_snapshot + ``BUILD_PLAN_OFFSET_MINUTES``）、
        auto_execute（= ``auto_pipeline.exec_at[market]``）；
      * ``GLOBAL_CHAIN``：reconcile（= ``auto_pipeline.reconcile_at``），其后是
        enqueue_research（= ``reconcile_at`` + ``ENQUEUE_RESEARCH_AFTER_RECONCILE_MINUTES``，
        **入队时刻跟随配置**，保证严格晚于当日 digest 落库——审查 A-2）。两者都**不查
        交易日历**（见 tick），只受时点与当日 ran 标记约束。

    **全链时间序不变式（R3，2026-09-16 审查）**：每条链（市场链与 GLOBAL 链）都按 ``at``
    升序排列。理由不是好看，而是 tick-first 补跑时**同一轮里可能同时到期多个作业，
    执行顺序就是链内顺序**——链若不按时间序，补跑那一刻的顺序就与真实的因果顺序不一致
    （最要命的一例：build_plan 排在 factors_snapshot 之前，计划会吃到旧快照）。
    关闭态直接返回 JOBS_DEFAULT（其各链本就时间有序），因此该不变式对两种状态都成立。
    相等时点由稳定排序保持插入顺序（如同刻的 sync_bars 与 sync_fundamentals）。

    作业体自身的软跳过（未启用/无匹配策略/数据未就绪/守卫拦截）由 ``plan-auto`` 与
    ``auto-execute`` 实现并留痕；这里不重复做关注池或策略门槛——避免两处判定漂移，
    作业「缺席」与「跳过」的原因都只有一处可见（告警表）。

    配置非法（ValueError）→ warn 告警 + 退化为纯 JOBS_DEFAULT：调度链不能因为一个
    写错的配置整体停摆（fail-soft 只在装配层；作业体内仍是 fail-closed）。
    """
    from .daemon import JOBS_DEFAULT  # 延迟导入：基础作业表归 daemon（逆向依赖只能延迟）
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
        # R3：追加完按 at 升序重排（稳定排序，同刻保持插入顺序）——补跑同轮多作业时，
        # 链内顺序就是执行顺序，必须与时间顺序一致（见 docstring 的不变式）。
        jobs[market] = sorted(chain, key=lambda job: job["at"])
    # GLOBAL 链：reconcile（对账 → 写 digest）在前，enqueue_research（基础链作业）**按
    # reconcile_at 派生**紧随其后——开启态下入队时刻跟随配置，保证严格晚于当日 digest；
    # 关闭态则保持 JOBS_DEFAULT 的固定点（ENQUEUE_RESEARCH_BASE_AT，无 reconcile 作业）。
    # **基础链作业原样保留**（sync_calendar 18:50 与关闭态的 enqueue_research）：这里只
    # 摘掉固定点的 enqueue_research 再按配置时刻重排，日历同步与交易开关无关，不参与重排。
    # 整条链按 at 升序排序：tick-first 补跑时同一轮可能同时到期多个作业，顺序必须与
    # 时间顺序一致，否则 enqueue 会先于 reconcile 执行（审查 A-2），或日历同步被排到
    # 对账之后（那样「今晚自愈」就不成立）。
    global_chain = [job for job in jobs.setdefault(GLOBAL_CHAIN, [])
                    if job["name"] != "enqueue_research"]
    global_chain.append({"name": "reconcile", "at": cfg["reconcile_at"],
                         "cmd": ["reconcile-daily"]})
    global_chain.append({"name": "enqueue_research",
                         "at": enqueue_time_after_reconcile(cfg["reconcile_at"]),
                         "cmd": list(ENQUEUE_RESEARCH_CMD)})
    jobs[GLOBAL_CHAIN] = sorted(global_chain, key=lambda job: job["at"])
    return jobs
