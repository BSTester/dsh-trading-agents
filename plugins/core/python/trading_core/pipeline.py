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

**状态与内容分开说**（情绪阶段，WP11 质量修复）：``sentiment.run`` 的软失败（池空/全源
失败/会话未收盘）**返回 ok 且退出 0**，tick 照常写 ran 标记——作业确实跑完了，状态仍是
``skipped``（不是 ``ok``——「今天什么都没做」不该显示成完成），原因进 ``summary``
（``_CONTENT_OUTCOMES``），页面因此看得见
内容结局而不牺牲状态语义。同理，情绪阶段的积累数字（已积累天数/连续交易日/最近日期）
**一律按市场**取，不把全市场累计混进单市场行。

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

#: 流程页阶段**展示顺序** = 逻辑闭环序（与链内执行序解耦，见 ``_ordered``）。
#: 市场链按 at 升序后 auto_execute 在链首（09:35，执行昨天冻结的计划）——那是执行序；
#: 人读流程页要的是「同步→质量→因子→…→计划→执行→对账→摘要」的因果序。
STAGE_ORDER = ("sync_bars", "sync_fundamentals", "merge_announcements", "quality",
               "factors_snapshot", "sentiment_snapshot", "research_snapshot",
               "build_plan", "plan", "auto_execute", "execute", "reconcile",
               "enqueue_research", "digest")

#: 告警标题 → 阶段状态（标题字面量与 emit 点一一对应，grep 可核）
_ALERT_STATUS = {
    # planner.plan_auto（build_plan 作业）
    "自动计划无策略": ("build_plan", "skipped"),
    "会话未收盘": ("build_plan", "skipped"),
    "关注池为空": ("build_plan", "skipped"),
    "数据未就绪": ("build_plan", "skipped"),
    "策略未注册": ("build_plan", "skipped"),
    # planner._resolve_strategy（WP14 任务 4）：规则存在但未人工批准 → 不消费
    "规则未启用": ("build_plan", "skipped"),
    "策略权重失败": ("build_plan", "failed"),
    "策略 universe 失败": ("build_plan", "failed"),
    "券商通道不可用": ("build_plan", "failed"),
    "权益不可用": ("build_plan", "failed"),
    # 计划期结构性结局（2026-09-17 现金封顶修订）：「没做成」不得显示成「已完成」
    "计划跳过：无可执行订单": ("build_plan", "skipped"),
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
    # sentiment.run（sentiment_snapshot 作业，WP11）——**仅作业没跑完**（tick 中断、
    # ran 标记未落）时的状态归因；作业跑完后的内容失败走摘要，见 _SENTIMENT_OUTCOMES
    "情绪快照跳过": ("sentiment_snapshot", "skipped"),
    "情绪源不可用": ("sentiment_snapshot", "failed"),
    "情绪快照全部失败": ("sentiment_snapshot", "failed"),
    # 预算耗尽（S-1，2026-09-17 实机加固）：**作业跑完了但没采完**——不判 failed（数据
    # 采到了一部分，把它显示成失败会让运维以为通道坏了），归 skipped。
    "情绪快照预算耗尽": ("sentiment_snapshot", "skipped"),
    # research_sync.run（research_snapshot 作业，WP12 任务 5）——仅作业**没跑完**时的状态
    # 归因；作业跑完（有 ran 标记）时软失败不改状态，结局在结算 failed/absent 与告警明细里
    # 可查（与情绪阶段同一分工：状态说「跑没跑」，内容说「采到什么」）。
    "研究快照跳过": ("research_snapshot", "skipped"),
    "研究数据面未配置": ("research_snapshot", "skipped"),
    "研究快照源不可用": ("research_snapshot", "failed"),
    "研究快照全部失败": ("research_snapshot", "failed"),
    # research_queue.enqueue（enqueue_research 作业，WP15 任务 2）——同样是「作业没跑完」
    # 才生效；跑完（有 ran 标记）时软跳过只留告警明细，阶段仍是 ok（状态说跑没跑）。
    "研究任务入队跳过": ("enqueue_research", "skipped"),
    "研究任务入队失败": ("enqueue_research", "failed"),
}

#: 情绪采集的**内容结局**（作业跑完后的软失败）→ 摘要片段。软失败（池空/全源失败/会话
#: 未收盘）在采集侧是「返回 ok 且退出 0」，tick 照常写 ran 标记——阶段状态因此恒为 ok。
#: 状态不改（作业确实跑完了，改状态会让流程页说谎），结局并进 **summary**，否则页面上
#: 「今天什么都没采到」毫无痕迹。与 _ALERT_STATUS 的三条同源同标题，但路径不同：
#: 那三条管「没跑完」，这里管「跑完了但内容失败」，两者不重复也不冲突。
#: **作业跑完后的内容结局**（E2E 缺陷 6 泛化，2026-09-17）：``标题 → (作业名或 None,
#: 状态覆盖或 None, 摘要片段)``。作业名 ``None`` 表示归属看 detail 里的 ``job=<name>``。
#: 状态覆盖 ``None`` = 只补摘要不改状态（部分源缺失时作业确实采到了东西，降级会说谎）；
#: ``skipped`` = 跑完了但什么都没做（关注池为空/会话未收盘/数据面未配置）——**此前这类
#: 结局只在摘要里出现、状态恒为绿色「已完成」**，与「闭环是否正常」相悖。
#: （标题一律用**字面量**：pipeline 为断环而惰性 import daemon，模块级取不到其常量。
#: 字面量与 emit 点的漂移由 `tests/test_e2e_pipeline_honesty.py` 的标题锁兜住。）
_CONTENT_OUTCOMES = {
    "作业跳过：关注池为空": (None, "skipped", "当日未执行：关注池为空"),
    "情绪快照跳过": ("sentiment_snapshot", "skipped", "当日未采集"),
    "情绪快照预算耗尽": ("sentiment_snapshot", "skipped", "当日未采完：预算耗尽"),
    "情绪源不可用": ("sentiment_snapshot", None, "部分源不可用"),
    "情绪快照全部失败": ("sentiment_snapshot", "failed", "当日全部失败"),
    "研究快照跳过": ("research_snapshot", "skipped", "当日未采集"),
    "研究数据面未配置": ("research_snapshot", "skipped", "数据面未配置"),
    "研究快照源不可用": ("research_snapshot", None, "部分源不可用"),
    "研究快照全部失败": ("research_snapshot", "failed", "当日全部失败"),
    "研究任务入队跳过": ("enqueue_research", "skipped", "当日未入队"),
    # 计划期结构性预警（2026-09-17）：作业跑了、计划也生成了，但买入被现金/持仓上限约束
    # ——状态仍是 ok（确实产出计划），摘要如实写明约束（不在页面上假装「一切都买到了」）。
    "计划预警：现金不可得": ("build_plan", None, "现金不可得：本次未生成买单"),
    "计划预警：现金封顶": ("build_plan", None, "买入量按可用现金封顶"),
    "计划预警：持仓数超限": ("build_plan", None, "持仓数已达上限：新建仓会被规则 6 拦"),
    "计划跳过：无可执行订单": ("build_plan", "skipped", "当日无可执行订单"),
    # F-a（2026-09-17）：**作业失败**此前完全不可见——`_subprocess_runner` 的整数 returncode
    # 被 `_run_job` 忽略、tick 仍写 ran 标记 → 触发了 content 路径（ran 已存在）。三条标题
    # 由 daemon 发出，归属看 detail 里的 ``job=<name>``：
    #   * 作业失败   → 阶段 failed（跑了但非零退出，绝不能再显示「已完成」）
    #   * 作业异常   → 阶段 failed（fn 形式抛异常）
    #   * 作业部分失败 → 只补摘要不改状态：作业确实跑了（部分标的成功），降级会说谎，
    #                    但「19 成功 1 失败」必须在页面上看得见
    "作业失败": (None, "failed", "当日失败"),
    "作业异常": (None, "failed", "作业异常"),
    "作业部分失败": (None, None, "部分失败"),
}

#: 状态优先级（内容结局与既有归因同时命中时取更强的一个）
_STATUS_RANK = {"ok": 0, "skipped": 1, "pending": 1, "failed": 2}

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
        status, summary = _content_outcome(job_name, market, alerts, chain)
        return {"label": _labels(job_name), "status": status or "ok",
                "at": ran, "scheduled": None, "summary": summary}
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


def _content_outcome(job_name, market, alerts, chain):
    """作业**跑完了**之后的内容结局 → ``(状态覆盖或 None, 摘要)``。

    与 ``_ALERT_STATUS`` 的分工：那张表管「没跑完」（无 ran 标记时的归因），这张表管
    「跑完了但没产出」。归属规则：
      * 表里显式给了作业名 → 必须相等；
      * 作业名为 ``None``（如 daemon 的通用跳过告警）→ 看 detail 里的 ``job=<name>``，
        避免一条告警污染同市场其它作业的阶段；
      * 市场消歧沿用 ``_applies``；GLOBAL 链不做消歧（与 ``_stage`` 同口径）。
    多条命中取**最强状态**（failed > skipped > ok），摘要按出现顺序去重拼接。
    """
    status, parts = None, []
    for alert in alerts:
        title = str(alert.get("title") or "")
        entry = _CONTENT_OUTCOMES.get(title)
        if entry is None:
            continue
        owner, override, text = entry
        if owner is not None:
            if owner != job_name:
                continue
        elif f"job={job_name}" not in str(alert.get("detail") or ""):
            continue
        if chain != "global" and not _applies(alert, market):
            continue
        if override and _STATUS_RANK.get(override, 0) > _STATUS_RANK.get(status or "ok", 0):
            status = override
        detail = _strip_market_prefix(alert.get("detail"), market)
        # detail 里的 job=/market= 是归属信息，对上页面是噪声（原因已由 text 表达）
        detail = " ".join(piece for piece in detail.split()
                          if not piece.startswith(("job=", "market=")))
        label = _merge_reason(text, detail)
        if label not in parts:
            parts.append(label)
    return status, "；".join(parts)


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


def _merge_reason(text, detail):
    """把告警 detail 合并进摘要文本，**避免同一原因重复**（F-c，2026-09-17）。

    实机证据：`sync_bars` 的摘要曾显示「当日未执行：关注池为空：关注池为空」——告警标题与
    detail 各说了一遍同样的原因。规则：
      * detail 为空 → 只用 text；
      * detail 已被 text 包含 → 只用 text（不重复）；
      * detail 以 text 里的原因开头（如 text=「当日未执行：关注池为空」、
        detail=「关注池为空（或全部停牌）」）→ 只补增量部分（「（或全部停牌）」）；
      * 其余情况 → 正常拼接（去重不得吃掉 detail 里 text 未表达的事实）。
    """
    if not detail:
        return text
    if detail in text:
        return text
    reason = text.split("：", 1)[1] if "：" in text else text
    if reason and detail.startswith(reason):
        rest = detail[len(reason):]
        return f"{text}{rest}" if rest else text
    return f"{text}：{detail}"


def _strip_market_prefix(detail, market):
    """告警 detail 的 ``market=XX`` 前缀去掉（摘要里已是该市场行，前缀是重复噪声）。

    只剥**该市场**的前缀；detail 里其它位置的市场标记原样保留（它们是事实内容）。
    """
    prefix = f"market={market}"
    text = str(detail or "").strip()
    return text[len(prefix):].strip() if text.startswith(prefix) else text


def _sentiment_outcome(alerts, market):
    """当日该市场情绪采集的内容结局 → 摘要片段；全成功（无相关告警）→ ``""``。

    纳入范围只有 ``_SENTIMENT_OUTCOMES`` 的字面量标题（emit 点稳定常量，非关键词猜测），
    归属沿用 ``_applies`` 的市场消歧；同一结局去重，多个用「；」并列。
    """
    parts = []
    for alert in alerts:
        label = _SENTIMENT_OUTCOMES.get(str(alert.get("title") or ""))
        if label is None or not _applies(alert, market):
            continue
        detail = _strip_market_prefix(alert.get("detail"), market)
        text = f"{label}：{detail}" if detail else label
        if text not in parts:
            parts.append(text)
    return "；".join(parts)


def _sentiment_stage(conn, market, date, stage, alerts=()):
    """情绪快照阶段：内容结局（当日，仅作业跑完时）+ 积累事实（本市场）+ 既有摘要。

    只读事实、不改状态：作业没跑（pending/skipped/failed）时也照样展示「攒了多少」——
    采集是长期积累，与当日是否成功是两件事。**口径一律按市场**：days/latest/streak
    都带 market，避免把全市场累计与逐市场连续拼成一句话（同一句复现在每个市场行的旧态）。

    内容结局只在 ``stage["at"]`` 存在（= 当日有 ran 标记，作业跑完了）时并入：没跑完的
    阶段已由 ``_ALERT_STATUS`` 的路由给出同名归因摘要，再拼一遍就是复读。
    无任何内容结局且无任何记录时原样返回（空库不产生噪声）。
    """
    parts = []
    days = store.sentiment_days(conn, market=market)
    if days:
        latest = store.sentiment_latest(conn, market=market)
        streak = store.sentiment_streak(conn, market=market, today=date)
        parts.append(f"已积累 {days} 天（连续 {streak} 个交易日，最近 {latest}）")
    if not parts:
        return stage
    base = str(stage.get("summary") or "")
    enriched = dict(stage)
    enriched["summary"] = "；".join(([base] if base else []) + parts)
    return enriched


def _ordered(stages):
    """按**逻辑闭环顺序**排阶段（R3 修订，2026-09-16 审查）。

    展示顺序与链内执行顺序是两件事：市场链按 ``at`` 升序后 ``auto_execute``（09:35）排在
    当日链首——那是**执行序**（补跑时先执行昨天冻结的计划），而流程页要呈现的是
    「同步 → 质量 → 因子 → … → 计划 → 执行」的**因果序**。此前展示序直接沿用链序，因此
    链序一变页面就跟着变；现在展示序由本常量固定，链序怎么排都不影响读数。
    未知作业名（上游新增而此处未登记）追加在末尾——不丢阶段，也不打乱已知顺序。
    """
    known = [name for name in STAGE_ORDER if name in stages]
    extra = [name for name in stages if name not in STAGE_ORDER]
    return {name: stages[name] for name in known + extra}


def _market_stages(conn, market, jobs, state, alerts, date):
    """该市场阶段表：真实作业链**发现**阶段 + 在 build_plan/auto_execute 之后插入派生阶段。"""
    stages = {}
    inserted = set()
    chain = jobs.get(market) or []
    for job in chain:
        name = job["name"]
        stage = _stage(market, name, state, alerts, date)
        stage["scheduled"] = job.get("at")
        if name == "sentiment_snapshot":
            stage = _sentiment_stage(conn, market, date, stage, alerts)
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
    return _ordered(stages)


def _global_stages(conn, jobs, state, alerts, date):
    """GLOBAL 链阶段（不绑市场日历）：归因不做市场消歧、不消费市场链层告警。"""
    stages = {}
    for job in jobs.get(GLOBAL_CHAIN) or []:
        stage = _stage(GLOBAL_CHAIN, job["name"], state, alerts, date, chain="global")
        stage["scheduled"] = job.get("at")
        stages[job["name"]] = stage
    stages["digest"] = _digest_stage(conn, date)
    return _ordered(stages)


#: 首启必须知道的配置缺口（E2E 缺陷 7）：只读、如实，不替用户改配置。
def _config_warnings(home):
    warnings = []
    try:
        from . import watchlist as watchlist_mod
        if not watchlist_mod.watchlist_symbols(home):
            warnings.append({
                "code": "watchlist_empty",
                "message": "关注池未配置：数据作业不会采集、研究队列不会入队",
                "hint": "初始化：trading_core watchlist-init --from-index SH.000300"
                        "（或在 trading-platform.json 配置 watchlist）",
            })
    except Exception as error:  # noqa: BLE001 —— 读不到配置不能拖垮快照
        warnings.append({"code": "watchlist_unreadable",
                         "message": f"关注池读取失败：{str(error)[:160]}", "hint": ""})
    return warnings


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
            # 首启配置缺口（关注池未配置等）：只读如实提示，前端流程页显眼展示
            "config_warnings": _config_warnings(home),
            "kill": daemon.kill_path(home).exists(),
            "halt": store.halt_summary(conn),
            "alerts": core_alerts.list_recent(conn, limit=alert_limit)}
