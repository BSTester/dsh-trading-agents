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

**实机加固（2026-09-17，最后一轮 e2e 发现）**：本作业是**全仓库最慢的作业**——20 只
关注池 × 三源（每个都要起浏览器/网络重试）实测 >7 分钟，表现为「静默推进、对外像挂死」，
且超过 `daemon._subprocess_runner` 的 900s 上限后会被判失败（数据实际采不到）。三条加固：

  * **总预算** ``budget_seconds``（配置 ``sentiment_budget_seconds``，默认 600，**必须小于
    作业上限 900**）：预算耗尽即停止采集剩余标的，把它们记进 ``skipped_symbols``（原因
    ``budget``），**照常输出结构化摘要 + 退出 0**（作业确实跑了）并 emit warn
    「情绪快照预算耗尽」——让调度器与运维看到「今天没采完」而不是「失败」；
  * **逐标的超时** ``per_symbol_timeout``（配置 ``sentiment_symbol_timeout_seconds``，
    默认 90）：单标的超时只判该标的 failed（原因含「超时」）并继续下一个，**不再无限等**；
  * **子进程清理**：真实子进程一律 ``start_new_session=True`` 建独立进程组，超时用
    ``os.killpg(SIGKILL)`` 整组回收；被 SIGTERM/SIGINT 打断时由 finally + 信号守卫回收
    ——不留 fin_sentiment/浏览器孤儿（实机实测过孤儿泄漏）。

**进度可见性（S-3）**：每个标的处理完立即往 **stderr** 写一行进度并 flush。为什么不是
stdout：`daemon._subprocess_runner` 用 `_last_json_object(proc.stdout)` 从 stdout 里取
作业摘要，进度若混进 stdout 会干扰该解析（多行 JSON 的「从后往前找 `{`」策略对夹杂的
分数/括号文本不健壮）；stderr 同样被服务捕获转写到日志，运维照样看得见，且**不触碰
摘要契约**。
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

#: 源名（落库 source 列）：只反映**实际取数实现**，不写渠道品牌——上游可能降级到
#: AKShare/Yahoo，用渠道名当源名会让数据说谎（真实上游留在 payload.sources_status）
SOURCE_FIN_SENTIMENT = "fin_sentiment"
SOURCE_NEWS = "fin_news"
SOURCE_LAST30DAYS = "last30days"
SOURCES = (SOURCE_FIN_SENTIMENT, SOURCE_NEWS, SOURCE_LAST30DAYS)

#: 合法市场链（与 store 交易日历/planner.CALENDAR_MARKET 的市场键同一集合）
CHAIN_MARKETS = ("SH", "HK", "US")

#: 单次子脚本超时（秒）：情绪/资讯脚本各自带浏览器与网络重试，90s 是实测降级路径上限。
#: 逐标的生效（超时只判该标的 failed，继续下一个），可由配置覆盖（S-2）。
PER_CALL_TIMEOUT = 90
#: 一轮采集的总时长预算（秒），可由配置覆盖（S-1）。**必须小于 daemon 的作业上限 900s**，
#: 否则作业会被外部杀掉、数据反而采不到（实机 >7 分钟即触顶）。
DEFAULT_BUDGET_SECONDS = 600
#: 配置键（``~/.dsh/trading-platform.json`` 顶层，与 ``futu_channel``/``watchlist`` 同级）
BUDGET_CONFIG_KEY = "sentiment_budget_seconds"
SYMBOL_TIMEOUT_CONFIG_KEY = "sentiment_symbol_timeout_seconds"
#: 作业上限（daemon._subprocess_runner 的 900s）：预算不得大于等于它（配置校验用）
JOB_TIMEOUT_LIMIT_SECONDS = 900
_ERROR_CHARS = 160
#: 预算耗尽告警标题（**稳定字面量**：pipeline 的 _ALERT_STATUS/_CONTENT_OUTCOMES 按标题
#: 精确匹配归因；变量信息（已处理/剩余数量）只能进 detail）
BUDGET_ALERT_TITLE = "情绪快照预算耗尽"

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


def config(home):
    """→ ``{"budget_seconds": int, "symbol_timeout_seconds": int}``（S-1/S-2 配置口径）。

    键缺省补默认；**非法值 fail-closed 报 ValueError**（沿用 ``risk_config`` /
    ``auto_pipeline_config`` 的既有纪律：缺失=默认，非法=报错，绝不静默降级）——
    预算被静默忽略会让作业重新撞上 900s 上限，正是本次要修的问题。
    """
    from .daemon import platform_config  # 延迟导入：daemon 模块级 import 本模块的作业体

    cfg = platform_config(Path(home)) or {}
    return {
        "budget_seconds": _positive_int(cfg.get(BUDGET_CONFIG_KEY),
                                        DEFAULT_BUDGET_SECONDS, BUDGET_CONFIG_KEY),
        "symbol_timeout_seconds": _positive_int(cfg.get(SYMBOL_TIMEOUT_CONFIG_KEY),
                                                PER_CALL_TIMEOUT,
                                                SYMBOL_TIMEOUT_CONFIG_KEY),
    }


def _positive_int(value, default, label):
    """正整数校验：缺省（None）取默认；bool/非整数/非正一律 ValueError。"""
    if value is None:
        return int(default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} 必须是正整数，收到 {value!r}")
    return int(value)


def _budget_guard(budget_seconds, symbol_timeout, label=BUDGET_CONFIG_KEY):
    """预算 + 最坏超窗必须留在作业上限内，否则配置期就拒绝。

    预算检查在每个标的**开始前**，所以最坏多跑「一个标的的三源各超时一次」=
    ``len(SOURCES) * symbol_timeout``。默认 600 + 3×90 = 870 < 900 ✓；若用户把预算调到
    700，最坏 970 ≥ 900，作业会先被 `daemon._subprocess_runner` 杀掉——正是本次要修的
    失败模式，故 fail-closed 报错并给出可执行的调整方向。
    """
    worst = budget_seconds + len(SOURCES) * symbol_timeout
    if worst >= JOB_TIMEOUT_LIMIT_SECONDS:
        raise ValueError(
            f"{label}（{budget_seconds}s）+ 最坏超窗（{len(SOURCES)}×{symbol_timeout}s）"
            f"= {worst}s 已达到作业上限 {JOB_TIMEOUT_LIMIT_SECONDS}s；"
            f"请调小 {BUDGET_CONFIG_KEY} 或 {SYMBOL_TIMEOUT_CONFIG_KEY}")
    return budget_seconds


# ---------------------------------------------------------------------------
# 子进程清理（S-2）：独立进程组 + 超时/中断回收，绝不泄漏 fin_sentiment/浏览器子进程
# ---------------------------------------------------------------------------
#: 当前存活的子进程组 id（真实子进程模式才用；测试注入的 runner 不经过这里）
_LIVE_PGIDS = set()
_SIGNAL_GUARD_INSTALLED = False


def _kill_group(pgid):
    """整组 SIGKILL；组已消失视为成功（幂等）。"""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _LIVE_PGIDS.discard(pgid)


def _kill_live_children():
    """回收全部受管子进程组（超时、异常、外部信号三条路径共用）。"""
    for pgid in list(_LIVE_PGIDS):
        _kill_group(pgid)


def _install_signal_guard():
    """SIGTERM 清理守卫（幂等、只在主线程生效）。

    为什么需要：SIGTERM 的**默认动作直接终止进程**，`finally` 不会执行 → 子进程组被
    留下（实机观察到孤儿 fin_sentiment + 浏览器进程，需手工按 PID 清理）。这里装一个
    处理函数：先回收子进程组，再恢复默认动作并重新发信号（保持「被信号杀死」的语义与
    退出码，不把外部停止变成正常退出）。SIGINT 不必拦：Python 抛 KeyboardInterrupt，
    由 runner 的 finally 回收，且 `run()` 不吞 BaseException。
    """
    global _SIGNAL_GUARD_INSTALLED
    if _SIGNAL_GUARD_INSTALLED:
        return
    try:
        previous = signal.getsignal(signal.SIGTERM)

        def _handler(signum, frame):  # noqa: ARG001 —— 信号处理签名固定
            _kill_live_children()
            signal.signal(signum, previous if callable(previous) else signal.SIG_DFL)
            if callable(previous):
                previous(signum, frame)
            else:
                os.kill(os.getpid(), signum)

        signal.signal(signal.SIGTERM, _handler)
        _SIGNAL_GUARD_INSTALLED = True
    except (ValueError, OSError, AttributeError):
        # 非主线程/平台不支持：静默放弃（超时路径与 finally 仍在，只是 SIGTERM 路径退化）
        pass


def _default_runner(cmd, timeout=PER_CALL_TIMEOUT):
    """默认子进程执行体：**独立进程组** + 超时/中断整组回收（S-2）。

    返回 stdout；非零退出即为失败（stderr 摘要进异常）。超时抛 ``TimeoutError``
    （消息含「超时」，调用方据此把该标的记 failed 并继续下一个）。
    """
    _install_signal_guard()
    proc = subprocess.Popen([str(part) for part in cmd], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:  # noqa: SIM105 —— 进程组登记必须在 wait 之前，故不用 suppress
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = proc.pid
    _LIVE_PGIDS.add(pgid)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _kill_group(pgid)          # 整组：脚本还会拉起浏览器子进程
            proc.communicate()          # 回收僵尸
            raise TimeoutError(
                f"超时（>{timeout}s）已回收进程组 {pgid}") from error
        if proc.returncode != 0:
            detail = (stderr or stdout or "").strip()
            raise RuntimeError(f"退出码 {proc.returncode}：{detail[:120]}")
        return stdout
    finally:
        # 正常/异常/KeyboardInterrupt 三条路径都走这里：进程若还在就整组回收
        _LIVE_PGIDS.discard(pgid)
        if proc.poll() is None:
            _kill_group(pgid)
            proc.wait()


def _payload_of(text, source, symbol):
    """脚本输出 → 原始事实（dict/list）。非法 JSON 如实抛错——**不写占位行**。"""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ValueError(f"{source} 输出非 JSON：{str(error)[:80]}") from error
    if not isinstance(data, (dict, list)):
        raise ValueError(f"{source} 输出既非对象也非数组（{type(data).__name__}）")
    return data


def _fetched_at(stamp):
    """观测时点：完整时刻直接用它（演练/补跑可复现），纯日期则取真实当前时刻。"""
    from . import clock
    text = str(stamp)
    return text if len(text) > 10 else clock.now_stamp()


def run(home, market, conn=None, runner=None, news_call=None, now=None, today=None,
        budget_seconds=None, per_symbol_timeout=None, symbols=None, limit=None,
        monotonic=None, progress_stream=None):
    """采集一轮 → 结果信封（**永不抛**；KeyboardInterrupt/SystemExit 除外，见下）。

    返回契约::

      {"ok": True, "market", "date", "date_source", "symbols", "saved",
       "processed", "ok_symbols", "elapsed_seconds", "budget_exhausted",
       "absent": [...], "failed": [{"symbol","source","error"}],
       "skipped_symbols": [{"symbol","reason"}], "sources": {源: 成功数}}
      {"ok": True, "skipped": <原因>}                       软跳过（会话未收盘/池空/无标的）
      {"ok": False, "error": <原因>}                        市场链/时钟/配置非法（fail-closed）

    ``runner``：子进程执行体 ``runner(cmd, timeout) -> stdout``（缺省真实 subprocess，
    带进程组回收）；``news_call``：资讯源注入缝。两者都是测试与 WP12 通道替换的注入点。
    ``budget_seconds``/``per_symbol_timeout``：缺省从配置读（见 ``config``）；
    ``symbols``（list 或逗号串）与 ``limit`` 是运维分批跑的覆盖（``--symbols``/``--limit``）；
    ``monotonic`` 是预算计时的注入点（测试用假时钟，不真等）；``progress_stream``
    缺省 stderr（S-3，见模块 docstring 的「进度可见性」）。

    **永不抛的边界**：``BaseException``（KeyboardInterrupt/SystemExit）不吞——外部中断
    应当终止作业，但子进程会在 runner 的 finally 里被整组回收后才传播。
    """
    from . import clock, store as store_mod

    home = str(home)
    market = str(market).upper()
    if market not in CHAIN_MARKETS:
        return {"ok": False,
                "error": f"未知市场链：{market}（应为 {'/'.join(CHAIN_MARKETS)}）"}
    try:
        settings = config(home)
        symbol_timeout = _positive_int(per_symbol_timeout,
                                       settings["symbol_timeout_seconds"],
                                       "per_symbol_timeout")
        budget = _budget_guard(_positive_int(budget_seconds, settings["budget_seconds"],
                                            "budget_seconds"), symbol_timeout)
    except ValueError as error:
        # 配置非法（人工手写配置文件）：fail-closed 信封，绝不静默用默认值跑
        return {"ok": False, "error": str(error)}
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
        return _collect(conn, home, market, str(stamp), runner or _default_runner,
                        news_call, budget=budget, symbol_timeout=symbol_timeout,
                        symbols=symbols, limit=limit,
                        monotonic=monotonic or time.monotonic,
                        progress_stream=progress_stream or sys.stderr)
    finally:
        if owns_conn:
            conn.close()


def _symbol_override(value):
    """``--symbols`` 覆盖：list 或逗号串 → 大写去空白列表；空串返回 None（视为未覆盖）。

    非法（非 list/str）→ ValueError（调用方按 fail-closed 处理）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = [str(part).split(",") for part in value]
        items = [text for group in items for text in group]
    else:
        raise ValueError(f"symbols 必须是列表或逗号串，收到 {type(value).__name__}")
    cleaned = [str(text).strip().upper() for text in items if str(text).strip()]
    return cleaned or None


def _progress(stream, index, total, symbol, status):
    """逐标的进度行（S-3）：写 stderr 并立即 flush——不污染 stdout 的作业摘要。"""
    try:
        stream.write(f"[sentiment] {index}/{total} {symbol} {status}\n")
        stream.flush()
    except (ValueError, OSError):
        pass  # 流已关闭/不可写：进度可见性不该让采集失败


def _collect(conn, home, market, stamp, runner, news_call, *, budget, symbol_timeout,
             symbols, limit, monotonic, progress_stream):
    from . import alerts, planner, store as store_mod, watchlist

    def emit(level, title, detail):
        # detail 一律带 market=XX：pipeline 的阶段归因按该标记归属市场（_alert_market），
        # 标题则是状态映射的**精确键**（源名等变量信息只能进 detail，不能进标题）。
        alerts.emit(conn, home=home, level=level, title=title,
                    detail=detail[:_ERROR_CHARS])

    try:
        date, date_source = planner.observation_date(conn, market, stamp)
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
        # 标题刻意不进 pipeline 的**状态**映射：采集照常进行，作业跑完就有 ran 标记，
        # 阶段状态以 ran 标记为准，把「口径退化」显示成 skipped/failed 会让流程页说谎。
        # （内容失败是另一回事：软失败同样不改状态，但结局进阶段摘要，见 pipeline
        #  的 _SENTIMENT_OUTCOMES。）
        emit("info", "情绪快照日期退化",
             f"market={market} 日历未同步，观测日取北京日 {date}（作业时刻 {stamp}）")

    try:
        override = _symbol_override(symbols)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    if override is not None:
        targets = override          # 运维分批跑：显式覆盖关注池（不再读配置）
    else:
        targets = watchlist.watchlist_symbols(home, market=market)
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return {"ok": False, "error": f"limit 必须是正整数，收到 {limit!r}"}
        targets = targets[:limit]
    if not targets:
        emit("warn", "情绪快照跳过", f"market={market} 关注池为空")
        return {"ok": True, "market": market, "date": date,
                "date_source": date_source, "skipped": f"关注池为空：market={market}"}

    paths, absent = scripts_for(home)
    if news_call is not None:
        # 注入通道可用即视为该源在场（不要求脚本存在），并从缺席清单里移除
        paths.setdefault(SOURCE_NEWS, None)
        absent = [source for source in absent if source != SOURCE_NEWS]

    fetched = _fetched_at(stamp)
    started = monotonic()
    saved, failed, skipped_symbols = 0, [], []
    processed, ok_symbols = 0, 0
    per_source = {source: 0 for source in SOURCES}
    total = len(targets)
    for index, symbol in enumerate(targets, start=1):
        if monotonic() - started >= budget:
            # S-1：预算耗尽——停止剩余标的（不是失败：作业确实跑了，只是没采完）
            skipped_symbols.extend({"symbol": rest, "reason": "budget"}
                                   for rest in targets[index - 1:])
            emit("warn", BUDGET_ALERT_TITLE,
                 f"market={market} 已处理 {processed}/{total} 个标的"
                 f"（预算 {budget}s 耗尽），剩余 {total - processed} 个未采集")
            break
        symbol_saved = 0
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
                        runner(_command(home, source, path, symbol), symbol_timeout),
                        source, symbol)
                store_mod.insert_sentiment(conn, date, symbol, source, payload,
                                           fetched_at=fetched)
                saved += 1
                symbol_saved += 1
                per_source[source] += 1
            except Exception as error:  # noqa: BLE001 —— 失败隔离到单源单标的
                failed.append({"symbol": symbol, "source": source,
                               "error": str(error)[:_ERROR_CHARS]})
        processed += 1
        if symbol_saved:
            ok_symbols += 1
        _progress(progress_stream, index, total, symbol,
                  "ok" if symbol_saved else "failed")

    absent_out = list(absent)
    for source in SOURCES:
        if per_source[source] or source in absent_out:
            continue
        first = next((item for item in failed if item["source"] == source), None)
        if first is not None:  # 该源每个标的都失败 → 源级缺席 + warn
            absent_out.append(source)
            emit("warn", "情绪源不可用",
                 f"market={market} 源 {source}：{processed} 个标的全部失败：{first['error']}")
    if saved == 0 and failed:
        emit("warn", "情绪快照全部失败",
             f"market={market} {date}：{len(failed)} 次调用无一成功")

    return {"ok": True, "market": market, "date": date, "date_source": date_source,
            "symbols": targets, "saved": saved, "absent": sorted(set(absent_out)),
            "failed": failed, "sources": per_source,
            "processed": processed, "ok_symbols": ok_symbols,
            "skipped_symbols": skipped_symbols,
            "budget_exhausted": bool(skipped_symbols),
            "elapsed_seconds": round(monotonic() - started, 1)}
