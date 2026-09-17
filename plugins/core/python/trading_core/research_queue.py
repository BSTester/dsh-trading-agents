"""值班研究员（L3）队列业务层（规格 §十）。

分工（与 ``store`` / ``sentiment`` 同一分层纪律）：

  * ``store`` —— 纯状态机（表、入队/领取/完成/回收的迁移规则，零告警零网络）；
  * 本模块 —— 需要 ``home`` 与告警的那部分：任务载荷组装（``enqueue``）与超时回收的
    warn 告警（``reclaim``）。二者都不调用任何 LLM、不开子进程。

**队列即攻击面**（规格 §十一.13）：``kind`` 与载荷键都是白名单（``store.TASK_KINDS`` /
``store.TASK_PAYLOAD_KEYS``），入队侧当场拒绝自由文本——队列里永远只有结构化引用，
「把一句话塞进队列让值班研究员去执行」这条路在落库前就被堵住。

告警归属：``reclaim`` 的「研究任务失败」**不登记**进 ``pipeline._ALERT_STATUS``——它描述的
是某条任务的执行结局，不是某个作业阶段的没跑完；流程页的阶段归因不该被它改写，任务明细
由队列列表端点与告警列表各自呈现（状态说阶段，明细说任务）。
"""
import datetime as _dt

from . import alerts, store

#: 因子巡检窗口（交易日）：足够覆盖一条因子的中周期衰减，也不至于拉太长历史。
FACTOR_WINDOW_DAYS = 120
#: 每日摘要的 kv 引用（reconcile 链的 ``daily:digest``，值班研究员据此读到当日口径）
DIGEST_REF = "kv:daily:digest"
_CHARS = 300


def enqueue(home, market, conn=None, now=None, today=None):
    """入队一轮 → 结果信封（**永不抛**）。

    返回契约::

      {"ok": True, "market", "date", "date_source",
       "tasks": [{"kind", "task_id", "created"}, ...]}
      {"ok": True, "market", "skipped": <原因>}   软跳过（会话未收盘/关注池为空）
      {"ok": False, "error": <原因>}              市场链/时钟非法（fail-closed）

    入队是**纯本地动作**：只读配置与本地库、只写本地队列表——不调 LLM、不开子进程、
    不触达任何外部通道（「零 LLM」是基础链作业的硬约束，规格 §10.5）。
    """
    from . import clock, planner

    home = str(home)
    market = str(market).upper()
    if market not in planner.CHAIN_MARKETS:
        return {"ok": False,
                "error": f"未知市场链：{market}（应为 {'/'.join(planner.CHAIN_MARKETS)}）"}
    try:
        # 与采集链同一时钟口径：today（可纯日期）> now > DSH_FAKE_NOW > 真实时间
        stamp = today or clock.now_stamp(now)
    except ValueError as error:
        return {"ok": False, "error": str(error)}

    owns_conn = conn is None
    if owns_conn:
        conn = store.connect(store.db_path(home))
    try:
        return _enqueue_round(conn, home, market, str(stamp))
    finally:
        if owns_conn:
            conn.close()


def _enqueue_round(conn, home, market, stamp):
    from . import factors, planner, watchlist

    def emit(level, title, detail):
        # detail 一律带 market=XX：pipeline 的阶段归因按该标记归属市场。
        alerts.emit(conn, home=home, level=level, title=title,
                    detail=f"market={market} {detail}"[:_CHARS])

    try:
        date, date_source = planner.observation_date(conn, market, stamp)
    except ValueError as error:
        # 注入时刻非法（--today/--now 是人工输入）：fail-closed，不静默回落真实时间
        return {"ok": False, "error": str(error)}
    if date_source == "beijing-fallback":
        emit("info", "日期口径退化",
             f"日历未同步，观测日退化为北京日 {date}（date_source=beijing-fallback）")
    if date is None:
        emit("info", "研究任务入队跳过", f"本次负责的会话尚未收盘（{stamp}）")
        return {"ok": True, "market": market,
                "skipped": f"{market} 本次负责的会话尚未收盘"}

    symbols = watchlist.watchlist_symbols(home, market=market)
    if not symbols:
        emit("info", "研究任务入队跳过", "关注池为空（无标的可研究）")
        return {"ok": True, "market": market, "skipped": f"关注池为空：market={market}"}

    try:
        refs = _refs(conn, date)
        tasks = [
            _put(conn, "daily_brief", date, market,
                 {"as_of": date, "market": market, "digest_ref": DIGEST_REF,
                  "symbols": symbols, "refs": refs}),
            _put(conn, "factor_patrol", date, market,
                 {"as_of": date, "market": market,
                  "factor_list": sorted(factors.REGISTRY),
                  "window": FACTOR_WINDOW_DAYS}),
        ]
        # 周度挖掘轮：本周（ISO 周，周一起算）该市场尚未入队才补一条——周一休市时
        # 本周第一个交易日照样会入队，不做「必须周一」的硬判定。
        if not store.task_exists_since(conn, "mining_round", market, _week_start(date)):
            tasks.append(_put(conn, "mining_round", date, market,
                              {"as_of": date, "market": market, "refs": refs}))
    except ValueError as error:
        # 载荷/参数非法（本模块自建载荷，正常不该发生）：暴露为告警，不静默丢任务
        emit("warn", "研究任务入队失败", str(error))
        return {"ok": False, "error": str(error)}
    return {"ok": True, "market": market, "date": date, "date_source": date_source,
            "tasks": tasks}


def _refs(conn, date):
    """当日研究数据引用（结构化，只带行数——值班研究员据此决定读哪张表）。"""
    counts = store.snapshot_counts(conn, date)
    return [{"source": "sentiment_snapshots", "date": date,
             "records": counts["sentiment"]},
            {"source": "f10_snapshots", "date": date, "rows": counts["f10"]},
            {"source": "short_snapshots", "date": date, "rows": counts["short"]},
            {"source": "plate_snapshots", "date": date, "rows": counts["plate"]},
            {"source": "factor_registry", "date": date, "rows": len(_fresh_factors())}]


def _fresh_factors():
    from . import factors
    return factors.REGISTRY


def _week_start(date_text):
    """该日期所属 ISO 周（周一起算）的周一日期字符串。"""
    day = _dt.date.fromisoformat(str(date_text)[:10])
    return (day - _dt.timedelta(days=day.isoweekday() - 1)).isoformat()


def _put(conn, kind, date, market, payload):
    """入队一条并回报是否为本次新建（幂等复用时 created=False）。"""
    existed = conn.execute(
        "SELECT 1 FROM research_tasks WHERE kind=? AND as_of=? AND market=? LIMIT 1",
        (kind, date, market)).fetchone() is not None
    task_id = store.enqueue_task(conn, kind, date, market, payload)
    return {"kind": kind, "task_id": task_id, "created": not existed}


def report(conn, home, task_id, ok, result_ref=None, err=None):
    """回报任务结果（业务层：状态机在 store，**升级为 failed 的告警在此发**）。

    与 ``reclaim`` 同一分工：``store.finish_task`` 只管状态迁移，告警需要 ``home``，
    因此留在业务层。failed 是**结局**（attempts 到上限，不再重试），与 reclaim 的
    超时判 failed 一样必须留痕——否则一条任务默默消失，没人知道它为什么没产出。
    """
    task = store.finish_task(conn, task_id, ok, result_ref=result_ref, err=err)
    if task["status"] == "failed":
        alerts.emit(
            conn, home=str(home), level="warn", title="研究任务失败",
            detail=(f"task={task['task_id']} kind={task['kind']} market={task['market']} "
                    f"as_of={task['as_of']} attempts={task['attempts']} "
                    f"err={task.get('err') or ''}")[:300])
    return task


def reclaim(conn, home, now, timeout_minutes=store.TASK_TIMEOUT_MINUTES):
    """回收超时任务；升级为 failed 的逐个发 warn 告警。返回状态机结果。

    告警逐条发（不是汇总一条）：任务是独立的研究单元，汇总会让「哪条失败了、重试过几次」
    淹没在摘要里——而这两个数字正是运维判断执行体是否健康所必需的。
    """
    result = store.reclaim_tasks(conn, now, timeout_minutes=timeout_minutes)
    for task in result["failed"]:
        alerts.emit(
            conn, home=str(home), level="warn", title="研究任务失败",
            detail=(f"task={task['task_id']} kind={task['kind']} market={task['market']} "
                    f"as_of={task['as_of']} attempts={task['attempts']} "
                    f"err={task.get('err') or ''}")[:300])
    return result
