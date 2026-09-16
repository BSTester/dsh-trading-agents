"""流程快照（WP10，规格 §5.1）：每市场「今日闭环跑到哪一步」的只读聚合。

只读且不造状态——事实来源只有既有四处：

  * kv ``daemon:state`` 的 ran 标记（键 ``{market}:{job}:{日期}``，值=执行时刻）；
  * ``plans`` 表（该市场最新的 auto 计划）；
  * ``orders`` 表（该计划的订单状态分布）；
  * ``alerts`` 表（无 ran 标记时解释「为什么没跑」）+ kv ``daily:digest``。

阶段集合来自 ``autopipeline.build_jobs`` 的**真实作业链**（作业缺席即阶段缺席，不凭空
列出还没实现的阶段），另加三个表驱动阶段：``plan``（计划）、``execute``（执行）、
``digest``（摘要）。

阶段状态口径：

  * ``ok``      —— 当日有 ran 标记（``at`` = 实际执行时刻）；
  * ``skipped`` —— 无 ran 标记，但当日有归因到该阶段的告警（``summary`` = 告警标题）；
  * ``failed``  —— 同上，但告警归因结论是故障（见 ``_ALERT_STATUS``）；
  * ``pending`` —— 两者都没有（今天还没到点/没跑）。

**不做推断**：没有证据就是 ``pending``；已跑成功的阶段不会因为别处的告警被改写。

告警归因（保守，只在无 ran 标记时生效）：标题取 ``_ALERT_STATUS`` 的**字面量**——它们
是各 emit 点的稳定常量（``planner.plan_auto`` / ``autopilot.auto_execute`` /
``reconcile.daily`` / ``daemon.tick``），不是关键词猜测；跨市场的归属靠告警 detail 里的
显式市场标记（``market=SH`` 或 ``SH.600519`` 形态）消歧，detail 无市场标记的（如
日历未同步）才作用于该市场链上的全部数据作业。

**链域（2026-09-16 修复 K1/I1）**：GLOBAL 链的作业不绑市场日历，因此归因时**不做市场
消歧**，也不消费市场链层告警（``_CHAIN_ALERT_STATUS``）。两条都是实测错态的直接后果：
对账差异的 detail 里是**差异标的**（``SH.600519``），拿它当归属市场会把告警甩给 SH，
GLOBAL 的 reconcile 于是永远停在 pending（critical 差异在页面上不可见）；而「日历未同步」
作用于市场链数据作业，对账不依赖交易日历，把它当成 GLOBAL 阶段的原因是拿别处故障冒充
本阶段结论。

时钟口径：``date`` 缺省取 ``clock.now_stamp()[:10]``（``DSH_FAKE_NOW`` 生效），与调度链
同一时间线。已知限制：``alerts.emit`` 的 ``created_at`` 取真实时间，演练（假时钟）下
当日告警可能落在真实日期上——因此归因只看最近 N 条并按日期比对，取不到就退化为
``pending``（宁可少说，不编）。
"""
import json
from pathlib import Path

from . import clock, store

#: 与作业链一致的市场集合（GLOBAL 链单列，不绑市场日历）
MARKETS = ("SH", "HK", "US")
#: 全局链键（与 autopipeline.GLOBAL_CHAIN 同一事实，避免 import 环）
GLOBAL_CHAIN = "GLOBAL"

#: 阶段中文标签（页面直接展示，映射只此一处）。未登记的新作业回退为作业名本身——
#: 不猜中文名，界面看到英文键就是「这里还没登记标签」。
_JOB_LABELS = {
    "sync_bars": "行情同步",
    "sync_fundamentals": "财报同步",
    "merge_announcements": "公告日合并",
    "quality": "数据质量",
    "factors_snapshot": "因子快照",
    "sentiment_snapshot": "情绪快照",
    "research_snapshot": "研究数据快照",
    "build_plan": "计划生成",
    "auto_execute": "自动执行",
    "enqueue_research": "研究任务入队",
    "reconcile": "对账",
    "plan": "计划",
    "execute": "执行",
    "digest": "摘要",
}

#: 告警标题 → 阶段状态（标题字面量与 emit 点一一对应，grep 可核）
_ALERT_STATUS = {
    # planner.plan_auto（build_plan 作业）
    "自动计划无策略": ("build_plan", "skipped"),
    "会话未收盘": ("build_plan", "skipped"),
    "关注池为空": ("build_plan", "skipped"),
    "数据未就绪": ("build_plan", "skipped"),
    "策略未注册": ("build_plan", "skipped"),
    "策略权重失败": ("build_plan", "failed"),
    "策略 universe 失败": ("build_plan", "failed"),
    "券商通道不可用": ("build_plan", "failed"),
    "权益不可用": ("build_plan", "failed"),
    # autopilot.auto_execute（auto_execute 作业）
    "账户模式非法": ("auto_execute", "failed"),
    "计划等待人工执行": ("auto_execute", "skipped"),
    "kill switch 生效": ("auto_execute", "skipped"),
    "熔断生效": ("auto_execute", "skipped"),
    "无待执行计划": ("auto_execute", "skipped"),
    "计划市场不符": ("auto_execute", "failed"),
    "当日已执行": ("auto_execute", "skipped"),
    "exec_at 缺市场": ("auto_execute", "failed"),
    "时刻非法": ("auto_execute", "failed"),
    "已超执行窗口": ("auto_execute", "skipped"),
    # reconcile.daily（reconcile 作业）
    "对账跳过": ("reconcile", "skipped"),
    "对账通道不可用": ("reconcile", "failed"),
    "对账差异": ("reconcile", "failed"),
    "对账无差异": ("reconcile", "ok"),
    # sentiment.run（sentiment_snapshot 作业，WP11）
    "情绪快照跳过": ("sentiment_snapshot", "skipped"),
    "情绪源不可用": ("sentiment_snapshot", "failed"),
    "情绪快照全部失败": ("sentiment_snapshot", "failed"),
}
#: 市场链层告警（daemon.tick 在整条链层面发出）→ 作用于该市场所有数据作业
_CHAIN_ALERT_STATUS = {"日历未同步": "skipped"}
#: 配置非法会同时阻断 build_plan 与 auto_execute（两处 emit 同名标题）
_CONFIG_ALERT_STATUS = {"auto_pipeline 配置非法": "skipped"}

#: 归因扫描的告警条数上限（与 plan/reconcile 快照同量级，够覆盖一个交易日的作业链）
ALERT_SCAN_LIMIT = 100


def _labels(job_name):
    return _JOB_LABELS.get(job_name, job_name)


def _ran_at(state, market, job_name, date):
    """该市场该作业当日的 ran 标记值（执行时刻字符串）；未跑为 None。"""
    ran = state.get("ran") or {}
    return ran.get(f"{market}:{job_name}:{date}")


def _alerts_of(conn, date):
    """当日告警（按 detail 消歧用；``alerts.emit`` 用真实时间，演练下可能取不到）。"""
    rows = conn.execute(
        "SELECT level,title,detail,created_at FROM alerts ORDER BY id DESC LIMIT ?",
        (ALERT_SCAN_LIMIT,)).fetchall()
    return [dict(row) for row in rows if str(row["created_at"])[:10] == date]


def _alert_market(alert):
    """告警归属市场：detail 显式 ``market=XX`` 优先，其次扫 ``XX.`` 证券前缀。

    两者都没有 → None（市场无关：如「日历未同步」作用于该链全部数据作业）。
    """
    detail = str(alert.get("detail") or "")
    for market in MARKETS:
        if f"market={market}" in detail:
            return market
    for market in MARKETS:
        if f"{market}." in detail:
            return market
    return None


def _applies(alert, market):
    """告警是否作用于该市场：detail 无市场标记 → 市场无关（如日历未同步），作用于任意市场。"""
    owner = _alert_market(alert)
    return owner is None or owner == market


def _stage(market, job_name, state, alerts, date, chain="market"):
    """作业阶段：ran 标记 → ok；否则按当日告警归因 → skipped/failed；否则 pending。

    ``chain`` 区分链域，两条差异都只在 ``"global"`` 下生效（K1/I1，见文件头「链域」）：

      * **不做市场消歧**——GLOBAL 链作业不绑市场，detail 里的证券代码是差异内容而非归属；
      * **不消费市场链层告警**（``_CHAIN_ALERT_STATUS``）——那是市场链作业的失败原因。

    市场链（``chain="market"``）保持既有口径：告警按 detail 消歧归属，链层告警作用于
    该市场全部数据作业，配置类告警只影响 build_plan/auto_execute。
    """
    ran = _ran_at(state, market, job_name, date)
    if ran:
        return {"label": _labels(job_name), "status": "ok",
                "at": ran, "scheduled": None, "summary": ""}
    for alert in alerts:
        title = str(alert.get("title") or "")
        owner = _ALERT_STATUS.get(title)
        if owner is not None and owner[0] == job_name:
            if chain == "global" or _applies(alert, market):
                status = "failed" if _is_failure(alert, owner[1]) else owner[1]
                return {"label": _labels(job_name), "status": status,
                        "at": None, "scheduled": None, "summary": title}
    if chain == "market":
        for title, status in _CHAIN_ALERT_STATUS.items():
            if any(str(a.get("title") or "") == title and _applies(a, market) for a in alerts):
                return {"label": _labels(job_name), "status": status,
                        "at": None, "scheduled": None, "summary": title}
        if any(str(a.get("title") or "") in _CONFIG_ALERT_STATUS and _applies(a, market)
               for a in alerts) and job_name in ("build_plan", "auto_execute"):
            return {"label": _labels(job_name),
                    "status": _CONFIG_ALERT_STATUS["auto_pipeline 配置非法"],
                    "at": None, "scheduled": None, "summary": "auto_pipeline 配置非法"}
    return {"label": _labels(job_name), "status": "pending",
            "at": None, "scheduled": None, "summary": ""}


def _is_failure(alert, fallback):
    """critical 一律升级为 failed（本仓库 critical 只用于真实故障，如对账差异）。"""
    if str(alert.get("level") or "") == "critical":
        return True
    return fallback == "failed"


def _latest_auto_plan(conn, market):
    """该市场最新的 auto 计划（任意状态）——「今天该执行的那份计划」。"""
    for plan in store.list_plans_newest_first(conn):
        if plan.get("origin") == "auto" and plan.get("market") == market:
            return plan
    return None


def _plan_stage(conn, market):
    plan = _latest_auto_plan(conn, market)
    if plan is None:
        return {"label": _labels("plan"), "status": "pending", "at": None,
                "scheduled": None, "summary": "无自动计划"}
    orders = store.get_orders_by_plan(conn, plan["plan_id"])
    summary = f"{plan['plan_id']} {plan['status']} {len(orders)} 单"
    if plan["status"] == "cancelled":
        # 过期语义（规格 §4.2）：跨日未执行的 auto 计划被置 cancelled——如实标注
        return {"label": _labels("plan"), "status": "skipped", "at": plan["created_at"],
                "scheduled": None, "summary": summary}
    return {"label": _labels("plan"), "status": "ok", "at": plan["created_at"],
            "scheduled": None, "summary": summary}


#: 「执行发生过」的状态集（I2）：只有这些代表订单真的到达了券商（``unknown`` = 可能已
#: 触达、先查不重放，故算到达）。其余（``cancelled``/``rejected``）意味着一单未成——
#: 自动链下风控拒整批是可达路径，把它显示成绿色「已完成」与「闭环是否正常」相悖。
_EXECUTED_STATES = frozenset({"submitted", "partial", "filled", "unknown"})


def _execute_stage(conn, market):
    """执行阶段：有订单则按**结局**派生状态（摘要始终是事实口径，不粉饰）。"""
    plan = _latest_auto_plan(conn, market)
    if plan is None:
        return {"label": _labels("execute"), "status": "pending", "at": None,
                "scheduled": None, "summary": "无计划"}
    orders = store.get_orders_by_plan(conn, plan["plan_id"])
    counts = {}
    for order in orders:
        counts[order["status"]] = counts.get(order["status"], 0) + 1
    if not counts:
        return {"label": _labels("execute"), "status": "pending", "at": None,
                "scheduled": None, "summary": "无订单"}
    summary = ", ".join(f"{key} {counts[key]}" for key in sorted(counts))
    status = "ok" if any(o["status"] in _EXECUTED_STATES for o in orders) else "failed"
    return {"label": _labels("execute"), "status": status, "at": None,
            "scheduled": None, "summary": summary}


def _digest_stage(conn, date):
    digest = store.kv_get(conn, "daily:digest", default=None)
    if not isinstance(digest, dict) or str(digest.get("as_of") or "") != date:
        return {"label": _labels("digest"), "status": "pending", "at": None,
                "scheduled": None, "summary": ""}
    orders = digest.get("orders") or {}
    summary = ", ".join(f"{key} {orders[key]}" for key in sorted(orders))
    return {"label": _labels("digest"), "status": "ok", "at": digest.get("at"),
            "scheduled": None, "summary": summary}


def _market_stages(conn, market, jobs, state, alerts, date):
    """该市场阶段表：真实作业链顺序 + 在 build_plan/auto_execute 之后插入派生阶段。"""
    stages = {}
    inserted = set()
    chain = jobs.get(market) or []
    for job in chain:
        name = job["name"]
        stage = _stage(market, name, state, alerts, date)
        stage["scheduled"] = job.get("at")
        stages[name] = stage
        if name == "build_plan":
            stages["plan"] = _plan_stage(conn, market)
            inserted.add("plan")
        elif name == "auto_execute":
            stages["execute"] = _execute_stage(conn, market)
            inserted.add("execute")
    if "plan" not in inserted:
        stages["plan"] = _plan_stage(conn, market)
    if "execute" not in inserted:
        stages["execute"] = _execute_stage(conn, market)
    return stages


def _global_stages(conn, jobs, state, alerts, date):
    """GLOBAL 链阶段（不绑市场日历）：归因不做市场消歧、不消费市场链层告警。"""
    stages = {}
    for job in jobs.get(GLOBAL_CHAIN) or []:
        stage = _stage(GLOBAL_CHAIN, job["name"], state, alerts, date, chain="global")
        stage["scheduled"] = job.get("at")
        stages[job["name"]] = stage
    stages["digest"] = _digest_stage(conn, date)
    return stages


def _config_unparsable(home):
    """``trading-platform.json`` 存在但 JSON 不可解析（I3）。

    ``platform_config`` 对损坏文件按空配置容错（不阻塞调度，这是有意的）；但那样一来
    「配置坏了」与「功能没开」在摘要里长得一模一样。此处只**判可解析性**，不改变容错语义。
    """
    path = Path(home) / "trading-platform.json"
    if not path.exists():
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return False


def _auto_pipeline_summary(home):
    """auto_pipeline 配置摘要：非法配置如实报错（端点本身不失败）。

    两种「非法」分开报：**语义非法**（``apply_overlay`` 的 ValueError）走 ``error``；
    **文件级不可解析**（JSON 坏）补 ``config_error`` 并保留默认值字段（I3）——两条路径
    互斥，语义错优先（文件已能解析时才可能语义错）。
    """
    from . import autopipeline
    try:
        summary = autopipeline.auto_pipeline_config(home)
    except ValueError as error:
        return {"enabled": False, "error": str(error)[:160]}
    if _config_unparsable(home):
        summary["config_error"] = "配置文件无法解析"
    return summary


def pipeline_snapshot(conn, home, date=None, alert_limit=10):
    """流程快照（规格 §5.1）：每市场阶段状态 + 全局阶段 + 配置摘要 + 当日告警。

    只读：只查库/读模式文件与心跳文件，不写库、不调券商（``build_jobs`` 传 conn=None
    正是为了不触发它内部的配置非法告警写入）。
    """
    from . import alerts as core_alerts
    from . import daemon
    date = date or clock.now_stamp()[:10]
    jobs = daemon.build_jobs(home)  # 不传 conn：装配期的告警写入不属于只读快照
    state = store.kv_get(conn, "daemon:state", default={"ran": {}}) or {"ran": {}}
    today_alerts = _alerts_of(conn, date)
    markets = {market: {"stages": _market_stages(conn, market, jobs, state,
                                                today_alerts, date)}
               for market in MARKETS}
    return {"date": date, "markets": markets,
            "global": {"stages": _global_stages(conn, jobs, state, today_alerts, date)},
            "auto_pipeline": _auto_pipeline_summary(home),
            "kill": daemon.kill_path(home).exists(),
            "halt": store.halt_summary(conn),
            "alerts": core_alerts.list_recent(conn, limit=alert_limit)}
