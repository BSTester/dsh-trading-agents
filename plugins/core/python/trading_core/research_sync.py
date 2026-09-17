"""研究数据 PIT 快照采集（WP12 任务 5，规格 §7.3）。

每日把**关注池**的富途数据面研究数据落 PIT 表（三表见 store 的 WP12 段注释）：

  * ``f10_snapshots``   —— F10 关键 section（分析师共识/评级汇总/机构持仓/内部交易/持股变动）。
    为什么是这 5 个：规格 §十一.10 的额度约定——关注池 ≤200 标的 × 5 次调用已是每日
    额度上限的量级，其余 21 个 section 留给**按需**抓取（同一条 ``pit_fields`` 口径可复用），
    全量铺开会让每日调用数随 section 数线性膨胀。
  * ``short_snapshots`` —— 做空 2 项（每日卖空成交/空头持仓），**仅 HK/US**（官方 -8：
    卖空数据只覆盖港美可卖空证券；SH 链前置跳过，不发注定被拒的调用）。
  * ``plate_snapshots`` —— 板块**目录**快照（``plate_class=INDUSTRY``，每市场每日 1 次调用）。
    额度权衡（规格 §7.3 允许二选一）：板块**成分股**不在此作业抓取——HK 行业板块上百个，
    逐个翻页是随板块数膨胀的开销，而它的消费方（行业中性化）尚未落地；目录快照本身就是
    「板块分类不被后来者改写」的防幸存者偏差地基，成分股待中性化实现时按需抓。

PIT 钥匙（规格 §7.3「按 section 语义取值，不得统一硬套」）：``pit_fields`` 只认**明示语义**
的字段名（pub/publ/披露 → ``announced_at``；period/报告期/日期 → ``period_end``），
认不出就回落观测日并写入 ``_observed_note`` 标注——**绝不猜字段、也绝不假装有披露时点**。

失败隔离与作业契约（与 ``sentiment.run`` / ``planner.plan_auto`` 同款）：单端点失败记入
``failed`` 不阻塞其余；``-10 no_data`` 是「合法但无数据」→ 空落库标记、不算失败；
**永不抛**——失败以信封返回并落告警，不拖垮调度链。

三层边界（规格 §3.1）：本模块是 L1 基础链作业，**零 LLM**；它只读数据面、只写本地 PIT 表，
不产生任何交易动作、不进入信号计算路径（F10/做空因子在攒数期内不得进规则 factors）。
"""
from pathlib import Path

from . import store

#: 合法市场链（与 store 日历/planner.CALENDAR_MARKET 的市场键同一集合）
CHAIN_MARKETS = ("SH", "HK", "US")

#: 每日抓取的 F10 关键 section（规格 §十一.10 的额度约定；其余按需抓）
F10_SECTIONS = ("analyst_consensus", "rating_summary", "institutional",
                "insider_trades", "holding_changes")

#: 做空端点（仅港美；官方 -8 对其它市场不支持）
SHORT_SECTIONS = ("short_daily_volume", "short_interest")
SHORT_MARKETS = frozenset({"HK", "US"})

#: 板块目录抓取的分类（规格 §7.3 行业中性化的分类维度）
PLATE_CLASSES = ("INDUSTRY",)

#: 观测口径标注（规格 §7.3 原文语义）：本行的可查询依据是观测时点，不是数据披露时点
OBSERVED_NOTE = "观测时点非数据时点"

#: 披露/公告语义字段（PIT 钥匙，优先级即元组顺序）——只认明示语义的名字，不做同义猜测
ANNOUNCED_FIELDS = ("pub_trading_day", "pub_date", "publ_date", "dir_deci_pub_date")

#: 数据期语义字段（``date`` 最泛化，放最后；仍认不出则回落观测日）
PERIOD_FIELDS = ("period_text", "date_time", "period", "fiscal_year", "ex_date",
                 "static_date", "holding_date", "min_trade_date", "end_date",
                 "begin_date", "period_id", "date")

#: 研究数据面的凭据文件名（与 ``trading_datasource.futu_openapi.default_credential_path``
#: 和 server.settings_api.credential_path 同一落点：``<home>/futu-openapi.json``）
CREDENTIAL_FILENAME = "futu-openapi.json"

_ERROR_CHARS = 160


def credential_file(home):
    """``<home>/futu-openapi.json``（服务端同一落点；显式 home 时优于 DSH_HOME 环境）。"""
    return Path(home) / CREDENTIAL_FILENAME


def openapi_ready(path=None):
    """数据面凭据是否可用（WP13 任务 1 起 = ``trading_datasource.channel.openapi_ready``）。

    为什么不 import ``server``：core 不得依赖服务进程包（依赖方向固定）。此前是服务端实现的
    **刻意镜像**（判据变更需两处同步）；WP13 任务 1 把判定下沉到双方共同依赖的
    ``trading_datasource``，这里改为委托——**同一份实现、同一个答案**，人工同步的负担消除。
    只看文件存在会让作业在签名期才失败，白烧一次链路，故此处必须与调用路径同源。
    """
    from trading_datasource import channel as channel_mod  # noqa: PLC0415
    return channel_mod.openapi_ready(path)


def default_client(path=None):
    """数据面只读方法组（缺省构造；凭据/续期/限频在共用的 OpenApiClient 上）。

    ``path`` 为凭据路径（与就绪判定同一路径——否则会出现「按 A 判就绪、按 B 取凭据」的
    错配）。只实例化本作业用到的三组：用不到的方法组不进对象图，也避免 import 期
    拉起 cryptography（惰性导入与 server 侧同一口径）。
    """
    from trading_datasource.futu_openapi import (  # noqa: PLC0415
        CredentialStore, OpenApiClient, OpenApiF10, OpenApiPlate, OpenApiShort)
    client = OpenApiClient(CredentialStore(path))

    class _Groups:
        __slots__ = ("f10", "short", "plate")

        def __init__(self):
            self.f10 = OpenApiF10(client)
            self.short = OpenApiShort(client)
            self.plate = OpenApiPlate(client)

    return _Groups()


def _candidate_objects(payload):
    """PIT 字段的候选载体（确定性顺序）：对象自身 → 其列表容器里的首个对象。

    只降**一层**：深递归会把嵌套的业务数组（如逐笔成交）也当载体，认出的「期」未必是
    本行的期。列表容器按**键名排序**遍历，保证同一 payload 每次结果一致（不靠字典序偶然）。
    """
    if isinstance(payload, list):
        first = next((item for item in payload if isinstance(item, dict)), None)
        return [first] if first else []
    if not isinstance(payload, dict):
        return []
    objects = [payload]
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, list):
            first = next((item for item in value if isinstance(item, dict)), None)
            if first is not None:
                objects.append(first)
    return objects


def _first_value(objects, fields):
    """按字段优先级在候选载体里取首个非空标量；取不到返回 None（不猜、不造）。"""
    for field in fields:
        for obj in objects:
            value = obj.get(field)
            if value is None or isinstance(value, (dict, list)):
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def pit_fields(payload, observation_date):
    """``payload`` → ``(period_end, announced_at)``（规格 §7.3 的取值口径，唯一实现）。

    ``period_end`` **必得**：认不出数据期就回落观测日（保证键非空 + 同日幂等）；
    ``announced_at`` **只在认得出披露时点**时给，否则 None——由读侧按观测时点过滤，
    采集侧在 payload 附 ``_observed_note`` 标注（谁都不许替上游编一个披露日）。
    """
    objects = _candidate_objects(payload)
    return (_first_value(objects, PERIOD_FIELDS) or str(observation_date),
            _first_value(objects, ANNOUNCED_FIELDS))


def _note_payload(payload):
    """给观测口径的 payload 附标注（对象直接加键；数组包装为 ``{"items": [...]}``）。

    数组包装是刻意的形状变化：既不丢原文，也不让标注混进业务数组的元素里
    （读侧拿到 ``items`` 就知道原文是数组，拿到对象就是对象）。
    """
    note = {store.OBSERVED_NOTE_KEY: OBSERVED_NOTE}
    if isinstance(payload, dict):
        return {**payload, **note}
    if isinstance(payload, list):
        return {"items": payload, **note}
    return {"items": [payload], **note}


def run(home, market, conn=None, client=None, now=None, today=None,
        credential_path=None, sleep_seconds=None):
    """采集一轮 → 结果信封（**永不抛**）。

    返回契约::

      {"ok": True, "market", "date", "date_source", "symbols", "saved",
       "absent": [...],        # 通道缺席（数据面未配置）或某 section 全标的失败
       "unsupported": [...],   # 市场/品类不支持（如 SH 的做空端点）——不是失败
       "no_data": [...],       # 端点如实返回「合法但无数据」（-10），空落库不算失败
       "failed": [{"symbol", "section", "error"}],
       "sections": {"f10": n, "short": n, "plate": n}}   # n = **落库行数**
                                                         # （short 按日合并，一标的 1 行）
      {"ok": True, "skipped": <原因>}                       软跳过（会话未收盘/池空/未配置）
      {"ok": False, "error": <原因>}                        市场链/时钟非法（fail-closed）

    ``client`` 注入即钉住通道（测试替身）；缺省按 ``credential_path`` 判凭据就绪后才构造
    ——未配置凭据时**零调用**软跳过（不假装采到了空数据）。
    ``sleep_seconds`` 缺省沿用 sync 的富途调用间隔（测试注入 0）。
    """
    from . import clock, store as store_mod

    home = str(home)
    market = str(market).upper()
    if market not in CHAIN_MARKETS:
        return {"ok": False,
                "error": f"未知市场链：{market}（应为 {'/'.join(CHAIN_MARKETS)}）"}
    try:
        # 与 sentiment.run 同一口径：today（可纯日期）> now > DSH_FAKE_NOW > 真实时间
        stamp = today or clock.now_stamp(now)
    except ValueError as error:
        return {"ok": False, "error": str(error)}

    owns_conn = conn is None
    if owns_conn:
        conn = store_mod.connect(store_mod.db_path(home))
    try:
        return _collect(conn, home, market, str(stamp), client, credential_path,
                        sleep_seconds)
    finally:
        if owns_conn:
            conn.close()


def _collect(conn, home, market, stamp, client, credential_path, sleep_seconds):
    from . import alerts, planner, sync as sync_mod, watchlist

    def emit(level, title, detail):
        # detail 一律带 market=XX：pipeline 的阶段归因按该标记归属市场（_alert_market），
        # 标题则是状态映射的精确键（源名等变量信息只能进 detail）。
        alerts.emit(conn, home=home, level=level, title=title,
                    detail=detail[:_ERROR_CHARS])

    try:
        date, date_source = planner.observation_date(conn, market, stamp)
    except ValueError as error:
        # 注入时刻非法（--today/--now 是人工输入）：fail-closed 信封，**永不抛**
        return {"ok": False, "error": str(error)}
    if date is None:
        emit("info", "研究快照跳过",
             f"market={market} 本次负责的会话尚未收盘（{stamp}）")
        return {"ok": True, "market": market,
                "skipped": f"{market} 本次负责的会话尚未收盘"}
    if date_source == "beijing-fallback":
        # 口径退化不进状态映射：采集照常进行（ran 标记为准），把退化显示成
        # skipped/failed 会让流程页说谎。内容失败是另一回事（见 failed/absent）。
        emit("info", "研究快照日期退化",
             f"market={market} 日历未同步，观测日取北京日 {date}（作业时刻 {stamp}）")

    symbols = watchlist.watchlist_symbols(home, market=market)
    if not symbols:
        emit("warn", "研究快照跳过", f"market={market} 关注池为空")
        return {"ok": True, "market": market, "date": date, "date_source": date_source,
                "skipped": f"关注池为空：market={market}"}

    path = credential_path or credential_file(home)
    if client is None and not openapi_ready(path):
        # 未配置凭据是正常状态（数据面可选），不是错误：软跳过 + info 留痕
        emit("info", "研究数据面未配置",
             f"market={market} 无可用 OpenAPI 凭据（{path}），本轮不采集")
        return {"ok": True, "market": market, "date": date,
                "date_source": date_source, "symbols": symbols,
                "absent": ["openapi"], "skipped": "研究数据面未配置"}

    sleep_seconds = sync_mod.SLEEP_SECONDS if sleep_seconds is None else sleep_seconds
    client = client or default_client(path)
    #: 观测时点取**日粒度**（作业每日一次）：同日重跑覆盖自身（幂等），跨日各自留痕。
    #: 这正是 F10 主键纳入 fetched_at 的用意——观测不被后来的观测抹掉，同日重跑不堆积。
    observed = str(date)

    saved, failed = 0, []
    counts = {"f10": 0, "short": 0, "plate": 0}
    no_data, unsupported = [], []
    section_ok = {section: 0 for section in F10_SECTIONS}
    section_failed = {section: 0 for section in F10_SECTIONS}

    for symbol in symbols:
        for section in F10_SECTIONS:
            ok, outcome = _call(client.f10.f10, symbol, section)
            _sleep(sleep_seconds)  # 节流对成败一视同仁（失败同样消耗额度/触发风控）
            if not ok:
                failed.append({"symbol": symbol, "section": section, "error": outcome})
                section_failed[section] += 1
                continue
            if _is_no_data(outcome):
                no_data.append(f"{symbol}/{section}")
            _save_f10(conn, symbol, section, outcome, observed)
            saved += 1
            counts["f10"] += 1
            section_ok[section] += 1

        if market in SHORT_MARKETS:
            # 两端点合并为**当日一行**：表主键是 (symbol,date)，逐端点落库会互相覆盖
            # （实现期测试暴露）。payload 以端点为键自描述；单端点失败时保留成功的那个。
            short_payload = {}
            for section in SHORT_SECTIONS:
                ok, outcome = _call(getattr(client.short, section), symbol)
                _sleep(sleep_seconds)
                if not ok:
                    failed.append({"symbol": symbol, "section": section, "error": outcome})
                    continue
                if _is_no_data(outcome):
                    no_data.append(f"{symbol}/{section}")
                short_payload[section] = outcome
            if short_payload:
                store.insert_short(conn, symbol, str(date), short_payload,
                                   fetched_at=observed)
                saved += 1
                counts["short"] += 1
        elif "short" not in unsupported:
            # 官方 -8：卖空数据只覆盖港美——前置跳过，不发注定被拒的调用（零额度浪费）
            unsupported.append("short")

    for plate_class in PLATE_CLASSES:
        ok, outcome = _call(client.plate.plate_list, market=market,
                            plate_class=plate_class)
        _sleep(sleep_seconds)
        if not ok:
            failed.append({"symbol": None, "section": f"plate_list:{plate_class}",
                           "error": outcome})
            continue
        store.insert_plate(conn, str(date), market, plate_class, outcome,
                           fetched_at=observed)
        saved += 1
        counts["plate"] += 1

    absent_out = []
    for section in F10_SECTIONS:
        if section_failed[section] and not section_ok[section]:
            absent_out.append(section)
            first = next(item for item in failed if item["section"] == section)
            emit("warn", "研究快照源不可用",
                 f"market={market} section {section}：{len(symbols)} 个标的全部失败："
                 f"{first['error']}")
    if saved == 0 and failed:
        emit("warn", "研究快照全部失败",
             f"market={market} {date}：{len(failed)} 次调用无一成功")

    return {"ok": True, "market": market, "date": date, "date_source": date_source,
            "symbols": symbols, "saved": saved, "absent": sorted(set(absent_out)),
            "unsupported": unsupported, "no_data": no_data, "failed": failed,
            "sections": counts}


def _call(fn, *args, **kwargs):
    """调用一个数据面只读端点 → ``(ok, payload | error_text)``（失败隔离到单端点）。

    作业契约要求**永不抛**：这里的异常一律收敛成 ``(False, 错误文本)``，交由调用方记
    ``failed`` 并继续。``BaseException``（KeyboardInterrupt 等）不在此吞掉——那是进程级信号。
    """
    try:
        return True, fn(*args, **kwargs)
    except Exception as error:  # noqa: BLE001 —— 单端点失败不得拖垮整轮采集
        return False, str(error)[:_ERROR_CHARS]


def _is_no_data(payload):
    """上游「合法但无数据」标记（``-10`` → ``{"no_data": True}``，见传输层容错口径）。"""
    return isinstance(payload, dict) and payload.get("no_data") is True


def _save_f10(conn, symbol, section, payload, observed):
    """按 section 语义取 PIT 钥匙后落库；未拿到披露时点则在 payload 附观测口径标注。"""
    period_end, announced_at = pit_fields(payload, observed)
    if announced_at is None:
        payload = _note_payload(payload)
    store.insert_f10(conn, symbol, section, payload, period_end=period_end,
                     announced_at=announced_at, fetched_at=observed)


def _sleep(seconds):
    """调用间隔（复用 sync 的节流口径：常量同源，测试注入 0）。"""
    if seconds:
        import time
        time.sleep(seconds)
