"""情绪/资讯 PIT 快照采集（WP11 任务 2，规格 §6.1）。

三源（全部只存**原始事实**，不打分——打分算法会漂移，原始数据不会）：

  * ``fin_sentiment`` —— fin-data 的统一情绪脚本（X + Reddit + AKShare 千股千评）；
  * ``fin_news``      —— fin-data 的统一快讯脚本（富途快讯 → AKShare(A股) → Yahoo RSS）。
    **为什么不是 quote_news_search / find-news**：该上游实测恒空（docs/TOOL-LIMITS.md
    「取新闻」行三种参数均 ``data: []``；docs/HANDOVER.md 明确「取新闻用 fin_news」），
    接一个恒空通道等于造一个永远空的源。落库 payload 保留 fin_news 的
    ``sources_status``，真实上游（futu/akshare/yahoo）在数据里自证，不靠源名猜测。
    WP12 接入 OpenAPI 数据面后若 find-news 实测有数据，可经 ``news_call`` 注入接回
    （本函数的注入缝即为此预留）。
  * ``last30days``    —— 可选社媒广度引擎（未安装即缺席；平台侧只落原始输出，
    证据三要素纪律见 skills/last30days-bridge）。

失败隔离到「单源单标的」：单标的失败不影响其余标的与其余源；某源**全标的失败**才
升级为源级缺席（absent + warn）。**永不抛**（与 ``planner.plan_auto`` /
``autopilot.auto_execute`` 同一作业契约）——失败以信封返回并落告警，不拖垮调度链。

日期口径（规格未言明处的实现期决策）：观测日 = 该市场本次作业负责的**会话本地日**
（``planner.data_date_for`` 单一实现，与 bars/数据就绪门同空间，美股链因此不会
错记到北京次日）；会话尚未收盘 → info 软跳过；日历未同步 → 退化北京日但**显式标
``date_source="beijing-fallback"`` 并 info 告警**，绝不静默改口径。
"""
import json
import os
import subprocess
from pathlib import Path

#: 源名（落库 source 列）：只反映**实际取数实现**，不写渠道品牌——上游可能降级到
#: AKShare/Yahoo，用渠道名当源名会让数据说谎（真实上游留在 payload.sources_status）
SOURCE_FIN_SENTIMENT = "fin_sentiment"
SOURCE_NEWS = "fin_news"
SOURCE_LAST30DAYS = "last30days"
SOURCES = (SOURCE_FIN_SENTIMENT, SOURCE_NEWS, SOURCE_LAST30DAYS)

#: 合法市场链（与 store 交易日历/planner.CALENDAR_MARKET 的市场键同一集合）
CHAIN_MARKETS = ("SH", "HK", "US")

#: 单次子脚本超时（秒）：情绪/资讯脚本各自带浏览器与网络重试，90s 是实测降级路径上限
PER_CALL_TIMEOUT = 90
_ERROR_CHARS = 160

#: fin-data 安装落位（install_plugins.py 的 UNIFIED_PYTHON 口径）
FIN_DATA_DIR = Path("trading-python") / "fin-data"
_FIN_DATA_SCRIPTS = {SOURCE_FIN_SENTIMENT: "fin_sentiment.py",
                     SOURCE_NEWS: "fin_news.py"}
#: last30days 可选引擎落位（scripts/install_last30days.py 口径）
LAST30DAYS_SCRIPT = (Path("last30days-skill") / "skills" / "last30days"
                     / "scripts" / "last30days.py")


def scripts_for(home):
    """→ ``({source: Path}, [缺席源])``：只认**已安装**的脚本，缺一个就缺席一个源。

    缺席是正常状态（可选组件未装/未配置），不是错误：调用方据此标 absent 并继续。
    """
    paths, absent = {}, []
    fin_data = Path(home) / FIN_DATA_DIR
    for source, name in _FIN_DATA_SCRIPTS.items():
        path = fin_data / name
        if path.exists():
            paths[source] = path
        else:
            absent.append(source)
    last30 = Path(home) / LAST30DAYS_SCRIPT
    if last30.exists():
        paths[SOURCE_LAST30DAYS] = last30
    else:
        absent.append(SOURCE_LAST30DAYS)
    return paths, absent


def _venv_python(home):
    """trading-venv 解释器（fin-data 脚本的既有运行口径：见 fin_sentiment.py 自释放逻辑）。"""
    base = Path(home) / "trading-venv"
    name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return base / name


def _topic(symbol):
    """社媒检索主题：裸代码（``US.AAPL``→``AAPL``）比带市场前缀更可搜。"""
    return symbol.split(".", 1)[-1]


def _command(home, source, path, symbol):
    python = _venv_python(home)
    if source == SOURCE_LAST30DAYS:
        return [str(python), str(path), _topic(symbol), "--emit=json"]
    return [str(python), str(path), "--ticker", symbol]


def _default_runner(cmd, timeout=PER_CALL_TIMEOUT):
    """默认子进程执行体：返回 stdout；非零退出即为失败（stderr 摘要进异常）。"""
    proc = subprocess.run([str(part) for part in cmd], capture_output=True,
                          text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"退出码 {proc.returncode}：{detail[:120]}")
    return proc.stdout


def _payload_of(text, source, symbol):
    """脚本输出 → 原始事实（dict/list）。非法 JSON 如实抛错——**不写占位行**。"""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ValueError(f"{source} 输出非 JSON：{str(error)[:80]}") from error
    if not isinstance(data, (dict, list)):
        raise ValueError(f"{source} 输出既非对象也非数组（{type(data).__name__}）")
    return data


def _observation_date(conn, market, stamp):
    """→ ``(date | None, date_source)``；``None`` = 本次负责的会话尚未收盘。"""
    from . import planner
    try:
        local = planner.data_date_for(conn, market, stamp)
    except (RuntimeError, KeyError):
        # 日历未同步/市场缺表：退化北京日，但**显式标源**（调用方落 info 告警）
        return str(stamp)[:10], "beijing-fallback"
    if local is None:
        return None, "session-open"
    return local, "session"


def _fetched_at(stamp):
    """观测时点：完整时刻直接用它（演练/补跑可复现），纯日期则取真实当前时刻。"""
    from . import clock
    text = str(stamp)
    return text if len(text) > 10 else clock.now_stamp()


def run(home, market, conn=None, runner=None, news_call=None, now=None, today=None):
    """采集一轮 → 结果信封（**永不抛**）。

    返回契约::

      {"ok": True, "market", "date", "date_source", "symbols", "saved",
       "absent": [...], "failed": [{"symbol","source","error"}], "sources": {源: 成功数}}
      {"ok": True, "skipped": <原因>}                       软跳过（会话未收盘/池空）
      {"ok": False, "error": <原因>}                        市场链/时钟非法（fail-closed）

    ``runner``：子进程执行体 ``runner(cmd, timeout) -> stdout``（缺省真实 subprocess）；
    ``news_call``：资讯源注入缝 ``news_call(symbol) -> dict``（缺省走 fin_news 脚本）。
    两者都是测试与 WP12 通道替换的注入点。
    """
    from . import clock, store as store_mod

    home = str(home)
    market = str(market).upper()
    if market not in CHAIN_MARKETS:
        return {"ok": False,
                "error": f"未知市场链：{market}（应为 {'/'.join(CHAIN_MARKETS)}）"}
    try:
        # today(可纯日期，语义「该日已过完」) > now > DSH_FAKE_NOW > 真实时间；
        # 纯日期的解析交给 planner._stamp_parts（唯一实现），这里不做二次口径。
        stamp = today or clock.now_stamp(now)
    except ValueError as error:
        return {"ok": False, "error": str(error)}

    owns_conn = conn is None
    if owns_conn:
        conn = store_mod.connect(store_mod.db_path(home))
    try:
        return _collect(conn, home, market, str(stamp),
                        runner or _default_runner, news_call)
    finally:
        if owns_conn:
            conn.close()


def _collect(conn, home, market, stamp, runner, news_call):
    from . import alerts, clock, store as store_mod, watchlist

    def emit(level, title, detail):
        # detail 一律带 market=XX：pipeline 的阶段归因按该标记归属市场（_alert_market），
        # 标题则是状态映射的**精确键**（源名等变量信息只能进 detail，不能进标题）。
        alerts.emit(conn, home=home, level=level, title=title, detail=detail[:160])

    try:
        date, date_source = _observation_date(conn, market, stamp)
    except ValueError as error:
        # 注入口径非法（--today/--now 是人工输入）：fail-closed 信封，**永不抛**——
        # 非法时刻若静默回落真实时间，演练结论会失真（沿用 planner/daemon 同一分级）。
        return {"ok": False, "error": str(error)}
    if date is None:
        emit("info", "情绪快照跳过",
             f"market={market} 本次负责的会话尚未收盘（{stamp}）")
        return {"ok": True, "market": market,
                "skipped": f"{market} 本次负责的会话尚未收盘"}
    if date_source == "beijing-fallback":
        # 标题刻意不进 pipeline 的阶段状态映射：采集照常进行，阶段状态以 ran 标记为准，
        # 把「口径退化」显示成 skipped/failed 会让流程页说谎。
        emit("info", "情绪快照日期退化",
             f"market={market} 日历未同步，观测日取北京日 {date}（作业时刻 {stamp}）")

    symbols = watchlist.watchlist_symbols(home, market=market)
    if not symbols:
        emit("warn", "情绪快照跳过", f"market={market} 关注池为空")
        return {"ok": True, "market": market, "date": date,
                "date_source": date_source, "skipped": f"关注池为空：market={market}"}

    paths, absent = scripts_for(home)
    if news_call is not None:
        # 注入通道可用即视为该源在场（不要求脚本存在），并从缺席清单里移除
        paths.setdefault(SOURCE_NEWS, None)
        absent = [source for source in absent if source != SOURCE_NEWS]

    fetched = _fetched_at(stamp)
    saved, failed = 0, []
    per_source = {source: 0 for source in SOURCES}
    for symbol in symbols:
        for source in SOURCES:
            if source not in paths:
                continue  # 缺席源：不逐标的记 failed，最终仍进 absent
            try:
                if source == SOURCE_NEWS and news_call is not None:
                    payload = news_call(symbol)
                else:
                    path = paths[source]
                    if path is None:
                        continue
                    payload = _payload_of(
                        runner(_command(home, source, path, symbol), PER_CALL_TIMEOUT),
                        source, symbol)
                store_mod.insert_sentiment(conn, date, symbol, source, payload,
                                           fetched_at=fetched)
                saved += 1
                per_source[source] += 1
            except Exception as error:  # noqa: BLE001 —— 失败隔离到单源单标的
                failed.append({"symbol": symbol, "source": source,
                               "error": str(error)[:_ERROR_CHARS]})

    absent_out = list(absent)
    for source in SOURCES:
        if per_source[source] or source in absent_out:
            continue
        first = next((item for item in failed if item["source"] == source), None)
        if first is not None:  # 该源每个标的都失败 → 源级缺席 + warn
            absent_out.append(source)
            emit("warn", "情绪源不可用",
                 f"market={market} 源 {source}：{len(symbols)} 个标的全部失败：{first['error']}")
    if saved == 0 and failed:
        emit("warn", "情绪快照全部失败",
             f"market={market} {date}：{len(failed)} 次调用无一成功")

    return {"ok": True, "market": market, "date": date, "date_source": date_source,
            "symbols": symbols, "saved": saved, "absent": sorted(set(absent_out)),
            "failed": failed, "sources": per_source}
