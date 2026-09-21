"""roe/roa 离线落库通道：把远端基本面（ROE/ROA 及同期可用科目）**提前**搬进本地交易库。

**这是什么、不是什么**（先说清楚，避免误用）::

    本模块 **不是矩阵取数路径**。``/api/v3/factors/matrix`` 仍然**只读**本地
    ``<home>/trading-data/trading.sqlite`` 的 ``fundamentals``（毫秒级、PIT 口径）；
    本模块是一条**离线**写库通道，把 Yahoo 通道的 roe/roa 提前落进同一张表，
    让「取得到」成为事实。矩阵快，是因为它永远不联网——联网的部分由本模块在
    作业时间里完成。

**为什么需要它**：``roe/roa`` 在矩阵里 0 覆盖——富途没有资产负债表接口，
唯一通道是 ``trading_datasource.fundamentals.load_returns``（Yahoo，实测单标的
~10-16s），不能放进矩阵取数路径；而 store 里也从未有人把 roe/roa 写进去。
本模块补的就是这条「提前搬」的通道。

**免密替代源（2026-09-21 加，Yahoo 缺 roe/roa 时兜底）**：

  Yahoo 对部分 A 股只给资产负债表侧科目（实测 ``SH.600028``：3 期
  ``equity``/``total_assets`` 有值、回报侧一行没有 → ``roe``/``roa`` 恒缺），
  因此本模块在 Yahoo 缺 roe/roa 时**按顺序**降级到 AKShare 财务指标类免密端点：

  1. ``akshare.stock_financial_analysis_indicator_em``（东财 ``RPT_F10_FINANCE_MAINFINADATA``，
     一次请求给全部报告期）→ ``roe=ROEJQ 加权净资产收益率(%)``、
     ``roa=ZZCJLL 总资产净利率(%)``；
  2. ``akshare.stock_financial_analysis_indicator``（新浪 ``vFD_FinancialGuideLine``，
     按年分页）→ ``roe=净资产收益率(%)``、``roa=总资产净利润率(%)``。

  **口径差异是事实，不掩饰**：替代源给的是**上游已算好的比率**（归母/平均余额口径），
  与 Yahoo 腿（净利润 ÷ 期末股东权益/总资产）**不是同一分母口径**；写入行的 ``source``
  逐行如实标 ``akshare/…``，与 ``yahoo/yfinance`` 行可逐行区分，**绝不冒充**。替代源
  只补 ``roe``/``roa`` 两个比率（``equity``/``total_assets``/``net_income`` 支撑科目它
  不提供，绝不编造）；Yahoo 已有值的期一律不覆盖（降级不是替换）。
  只对 **Yahoo 腿已锚定的报告期**兜底（锚点 = 该票已解析出的报告期，按 ``--periods``
  过滤）：Yahoo 整条通道失败时**没有报告期锚点**，不猜期、不拉全历史。

**数据口径（诚实性约束，全部可核验）**：

  * 数值：Yahoo（yfinance）季度 ``balance_sheet`` / ``income_stmt``，字段别名与
    ``trading_datasource.fundamentals`` 同一套；roe = 净利润/股东权益、
    roa = 净利润/总资产（分母非正不给值）。同期还落 ``equity``/``total_assets``/
    ``net_income`` 三个支撑科目，便于人工核对 roe/roa 的分子分母。
    Yahoo 缺 roe/roa 的期由上面的**免密替代源**补（口径不同，靠 ``source`` 区分）。
  * 披露时点（announced_at）：A 股用东财 ``RPT_LICO_FN_CPD`` 的 **NOTICE_DATE**
    （该期财报的真实公告日）。**注意**：akshare ``stock_yjbb_em`` 的
    「最新公告日期」列实际是 UPDATE_DATE（行刷新时间）——对**老报告期**它是
    「下一年同季财报的公告日」（2026-09-21 实测：600000 的 2025-06-30 期，
    UPDATE_DATE=2026-08-28 而 NOTICE_DATE=2025-08-28），把它当披露日落库就是
    错数据。本模块因此**只认 NOTICE_DATE**，并加**法定披露窗口闸门**
    （一季报 ≤04-30、中报 ≤08-31、三季报 ≤10-31、年报 ≤次年 04-30）：越界的
    披露日一律拒绝入库（``merge_announcements`` 的 20250630 等老期就是被这个
    陷阱污染的，修复入口见 ``fix_announced_dates``）。**替代源补的行走同一道闸门**
    （同一注册表、同一 ``announced_source``、同一法定窗口）：注册表没有该期公告日
    就**写不进**，原因如实进结果的 ``skipped``（不猜、不拿同步运行日顶替 A 股披露日）。
  * 非 A 股没有披露注册表：announced_at 落**同步运行日**（数据「已核实在手」的
    日期，保守方向——只会让数据晚可见，不会前视），并在 announced_source 里
    如实标注「非官方披露日」。本轮任务只跑 SH，此分支留作 HK/US 扩展。
  * 幂等：写入口就是 ``trading_core.store.upsert_fundamentals``（与
    gross_margin 同一入口），冲突键 ``(symbol, field, period_end)`` upsert，
    重跑不产生重复行；announced_at 一经落库不被覆盖（与 store 既有语义一致）。
  * 限速礼貌：**串行**逐标的（上游 ~10-16s/标的）+ 失败重试 1 次 + 失败清单
    如实进结果（不为覆盖率好看而隐瞒）。免密替代源只在 Yahoo 缺 roe/roa 时触发，
    每个标的**每源最多一次请求**，失败不重试（错误原文进 ``fallback_failures``）。

**建议调度**（详见 docs/e2e-and-data-gaps.md）：**手动/低频**，不挂每日收盘链。
理由：roe/roa 的上游是季报，披露节奏为每季一次（4 月底/8 月底/10 月底/次年 4 月底），
日更毫无增益；Yahoo 通道单标的 ~10-16s，日链每加 1 分钟都挤占 16:00-16:30 的
K 线/公告窗口；且财报季结束后数据不再变化。推荐在每季披露截止日（5/9/11/5 月）
后手动跑一次，或以低频（如每周）挂研究侧作业。

CLI::

    cd platform && ~/.dsh/trading-venv/bin/python -B -m server.v3_fundamentals_sync \\
        --market SH [--tickers SH.600000,...] [--periods 20260630,...] \\
        [--db PATH] [--dry-run] [--no-indicator-fallback] [--fix-announced-at]

只写 store 数据，不碰任何交易端点（只读纪律）。
"""
import argparse
import datetime as _dt
import json
import re
import sys
import time
from pathlib import Path

from trading_core import store
from trading_datasource.market import is_a_share, to_futu_symbol, to_yahoo_symbol

SOURCE_YAHOO = "yahoo/yfinance"
#: A 股披露日来源：东财业绩报表底表的 NOTICE_DATE 字段（该期财报真实公告日）。
SOURCE_NOTICE_DATE = "eastmoney/yjbb:NOTICE_DATE"
#: 非 A 股的替代披露时点：同步运行日（已核实「数据在手」的日期，保守方向）。
SOURCE_RUN_DATE = "sync-run-date(非官方披露日,保守)"

#: 免密替代源（AKShare 财务指标类）：Yahoo 缺 roe/roa 时的兜底。**来源名逐字进 ``source`` 列**。
SOURCE_AK_EM_INDICATOR = "akshare/stock_financial_analysis_indicator_em"
SOURCE_AK_SINA_INDICATOR = "akshare/stock_financial_analysis_indicator"

#: roe/roa 的分子分母科目一并落库，便于人工核对（字段名不与 futu 口径冲突）。
FIELDS = ("roe", "roa", "equity", "total_assets", "net_income")

#: 替代源**只补**这两个比率（``equity``/``total_assets``/``net_income`` 支撑科目替代源
#: 不提供——不编造分母）。
INDICATOR_FIELDS = ("roe", "roa")

#: 替代源链（顺序 = 先东财后新浪，两家都免密钥）。列名以本机 akshare 1.18.96 实测为准：
#:   * 东财 ``RPT_F10_FINANCE_MAINFINADATA``（``APP_F10_MAINFINADATA``）：``REPORT_DATE`` +
#:     ``ROEJQ``（加权净资产收益率 %）+ ``ZZCJLL``（总资产净利率 %），一次请求给全部报告期；
#:   * 新浪 ``vFD_FinancialGuideLine``：``日期`` + ``净资产收益率(%)`` + ``总资产净利润率(%)``，
#:     按年分页（``start_year`` 从所需最早年份起，少翻页）。
#: **口径是上游已算好的比率**（归母/平均余额口径），与 Yahoo 腿（净利润 ÷ 期末股东权益/
#: 总资产）不同——因此写入行逐行标 ``source``，绝不冒充 ``yahoo/yfinance``。
INDICATOR_SOURCES = (
    {
        "source": SOURCE_AK_EM_INDICATOR,
        "func": "stock_financial_analysis_indicator_em",
        "period": ("REPORT_DATE",),
        "roe": ("ROEJQ",),
        "roa": ("ZZCJLL",),
        "caliber": ("东财 F10 主要指标（RPT_F10_FINANCE_MAINFINADATA）：roe=ROEJQ 加权净资产"
                    "收益率(%)、roa=ZZCJLL 总资产净利率(%)；归母/平均余额口径，"
                    "与 Yahoo 腿（净利润/期末股东权益）口径不同，靠 source 列区分"),
    },
    {
        "source": SOURCE_AK_SINA_INDICATOR,
        "func": "stock_financial_analysis_indicator",
        "period": ("日期",),
        "roe": ("净资产收益率(%)",),
        "roa": ("总资产净利润率(%)",),
        "caliber": ("新浪财务指标（vFD_FinancialGuideLine）：roe=净资产收益率(%)、"
                    "roa=总资产净利润率(%)；同为上游已算好的比率，非 Yahoo 分母口径"),
    },
)

#: 字段别名与 trading_datasource.fundamentals 同一套（股东权益优先普通股东权益）。
EQUITY_ALIASES = ("Stockholders Equity", "Common Stock Equity",
                  "Total Equity Gross Minority Interest")
ASSETS_ALIASES = ("Total Assets",)
INCOME_ALIASES = ("Net Income From Continuing Operation Net Minority Interest",
                  "Net Income From Continuing And Discontinued Operation",
                  "Net Income", "Net Income Common Stockholders")

#: A 股法定披露截止（月, 日），年报截止为次年 04-30。披露日越界 = 上游给的不是
#: 本期披露日（yjbb UPDATE_DATE 陷阱），拒绝入库。
STATUTORY_DEADLINE = {"03-31": (4, 30), "06-30": (8, 31),
                      "09-30": (10, 31), "12-31": (4, 30)}

SLEEP_BETWEEN_TICKERS = 1.0  # 串行限速：标的之间的间隔秒数（上游 ~10-16s/标的）

_REGISTRIES: dict = {}  # {period_iso: {code6: notice_date}}，进程内缓存


# ---------------------------------------------------------------------------
# 取数：Yahoo 季度通道（数值）
# ---------------------------------------------------------------------------
def _first_value(frame, aliases, column):
    """frame[column] 里按别名顺序取第一个非 NaN 值（trading_datasource 同口径）。"""
    if frame is None or frame.empty or column not in frame.columns:
        return None
    for name in aliases:
        if name in frame.index:
            for value in frame.loc[name, [column]]:
                if value == value and value is not None:  # NaN 过滤
                    return float(value)
    return None


def _ratio(numerator, denominator):
    """ROE/ROA（%）：分子分母任一缺失或分母非正 → None（不用 0 假冒）。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator * 100.0, 2)


def fetch_quarterly_returns(ticker, fetcher=None):
    """Yahoo 季度通道：``{period: {字段: 值}}``。通道失败抛 ``RuntimeError``（可读）。

    ``fetcher`` 注入即完全绕过 yfinance（离线测试口径），返回
    ``(balance_frame, income_frame)`` 形状的替代由测试自拟——本函数只依赖
    ``.columns``（报告期）与 ``.index/.loc``（科目行）两个 pandas 面。
    """
    symbol = to_yahoo_symbol(ticker)
    if symbol is None:
        raise RuntimeError(f"{ticker} 无法映射 Yahoo 代码（不支持的标的格式）")
    if fetcher is not None:
        balance, income = fetcher(ticker)
    else:
        try:
            import yfinance as yf
            handle = yf.Ticker(symbol)
            balance = handle.quarterly_balance_sheet
            income = handle.quarterly_income_stmt
        except Exception as error:  # noqa: BLE001 —— 上游什么异常都可能抛
            raise RuntimeError(f"{ticker} Yahoo 通道异常：{type(error).__name__}: {error}") from error
    if balance is None or balance.empty:
        raise RuntimeError(f"{ticker}（{symbol}）Yahoo 季度资产负债表为空（no-data）")
    periods = {}
    for column in balance.columns:
        period = str(column)[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", period):
            continue  # 脏报告期无法定位 PIT，宁缺毋假
        equity = _first_value(balance, EQUITY_ALIASES, column)
        assets = _first_value(balance, ASSETS_ALIASES, column)
        net_income = _first_value(income, INCOME_ALIASES, column)
        entry = {"equity": equity, "total_assets": assets, "net_income": net_income,
                 "roe": _ratio(net_income, equity), "roa": _ratio(net_income, assets)}
        if all(entry[key] is None for key in FIELDS):
            continue  # 一科目都取不到 → 该期没有可用数据，不造假
        periods[period] = entry
    if not periods:
        raise RuntimeError(f"{ticker}（{symbol}）Yahoo 返回里没有可解析的报告期（no-data）")
    return {"symbol": symbol, "source": SOURCE_YAHOO, "periods": periods}


# ---------------------------------------------------------------------------
# 取数：免密替代源（AKShare 财务指标类）——Yahoo 缺 roe/roa 时兜底
# ---------------------------------------------------------------------------
def _number(value):
    """单元格 → float / None。非数值（NaN/空串/``"-"``/``"--"``）→ None，**不返回 0 冒充**。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    text = str(value).strip()
    if not text or text in ("nan", "None", "NaT", "<NA>", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _frame_rows(frame):
    """DataFrame → list[dict]。只做一次浅拷贝，不与 pandas 对象保持引用。

    优先 ``to_dict('records')``（pandas 路径），退化路径支持 list[dict] 与 ``iterrows()``，
    这样离线测试注入的假表不必真的安装 pandas（与 ``v3_sources._rows_from_frame`` 同口径）。
    """
    if frame is None:
        return []
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            return list(to_dict("records"))
        except TypeError:
            pass
    if isinstance(frame, list):
        return [row for row in frame if isinstance(row, dict)]
    iterrows = getattr(frame, "iterrows", None)
    if callable(iterrows):
        return [dict(row) for _, row in iterrows()]
    return []


def _report_period_iso(raw):
    """报告期单元格 → ``YYYY-MM-DD``；识别不出 → None（脏期无法定位 PIT，宁缺毋假）。"""
    if raw is None:
        return None
    text = str(raw).strip()[:10]
    return text if re.match(r"^\d{4}-\d{2}-\d{2}$", text) else None


def _first_cell(row, names):
    """按优先级取第一个存在的列（两个替代源的列名不同）。"""
    for name in names:
        if name in row:
            return row.get(name)
    return None


def _indicator_entries(frame, spec, wanted):
    """替代源原始表 → ``{period_iso: {roe, roa}}``（只收所需报告期；比率不做二次换算）。

    上游比率一律**四舍五入到两位小数**（与 Yahoo 腿 ``_ratio`` 的落库精度一致），
    但**不做任何口径换算**：值就是上游给的百分数，口径差异靠 ``source`` 列如实区分。
    """
    entries = {}
    for row in _frame_rows(frame):
        if not isinstance(row, dict):
            continue
        period = _report_period_iso(_first_cell(row, spec["period"]))
        if period is None:
            continue
        if wanted is not None and period not in wanted:
            continue
        roe = _number(_first_cell(row, spec["roe"]))
        roa = _number(_first_cell(row, spec["roa"]))
        if roe is None and roa is None:
            continue  # 该期上游没给可用比率 → 不写（缺就是缺）
        entries[period] = {
            "roe": None if roe is None else round(roe, 2),
            "roa": None if roa is None else round(roa, 2),
        }
    return entries


def _call_indicator(spec, arg, start_year):
    """真网络口径：**惰性** import akshare（模块 import 时不碰三方库）并调一次。"""
    import akshare  # noqa: PLC0415 —— 与 v3_sources 同口径：请求路径内才 import
    func = getattr(akshare, spec["func"], None)
    if not callable(func):
        raise RuntimeError(f"akshare.{spec['func']} 不存在（版本不兼容？）")
    if spec["func"].endswith("_em"):
        return func(symbol=arg, indicator="按报告期")
    return func(symbol=arg, start_year=str(start_year))


def fetch_indicator_returns(ticker, fetcher=None, periods=None):
    """免密替代源链（东财 → 新浪）：``{period: {roe, roa}}`` + **如实来源/口径标注**。

    * 只对 ``periods``（Yahoo 腿已锚定的报告期）取数；缺省 = 该源返回的全部期；
    * ``fetcher(func_name, arg) -> frame`` 注入即完全绕过 akshare/网络（离线测试口径）；
    * 某源一期都没给出所需报告期 → 视为该源失败（不把空表当成功），继续下一源；
    * 全部来源失败 → ``RuntimeError``，文本里带**每个源的真实错误**（不编造成功）。
    """
    symbol = to_futu_symbol(ticker)
    if not is_a_share(symbol):
        raise RuntimeError(f"{ticker} 不是 A 股标的：AKShare 财务指标兜底源只覆盖 A 股（不猜映射）")
    market, code = symbol.split(".")[0], symbol.split(".")[1]
    wanted = None if periods is None else {str(period) for period in periods}
    start_year = str(min(period[:4] for period in wanted)) if wanted else "1900"
    attempts = []
    for spec in INDICATOR_SOURCES:
        arg = f"{code}.{market}" if spec["func"].endswith("_em") else code
        began = time.time()
        try:
            frame = (fetcher(spec["func"], arg) if fetcher is not None
                     else _call_indicator(spec, arg, start_year))
            entries = _indicator_entries(frame, spec, wanted)
        except Exception as error:  # noqa: BLE001 —— 上游/解析异常都算该源失败，留真实原文
            attempts.append({"source": spec["source"], "ok": False,
                             "ms": int((time.time() - began) * 1000),
                             "error": f"{type(error).__name__}: {error}"[:300]})
            continue
        elapsed = int((time.time() - began) * 1000)
        if not entries:
            attempts.append({
                "source": spec["source"], "ok": False, "ms": elapsed,
                "error": (f"该源未给出所需报告期（{','.join(sorted(wanted)) if wanted else '任意期'}）"
                          "的 roe/roa：空表不当作成功")})
            continue
        attempts.append({"source": spec["source"], "ok": True, "ms": elapsed})
        return {"symbol": symbol, "source": spec["source"], "caliber": spec["caliber"],
                "periods": entries, "attempts": attempts}
    detail = "；".join(f"{item['source']}：{item['error']}" for item in attempts)
    raise RuntimeError(f"{ticker} 免密替代源（AKShare 财务指标）全部失败——{detail}")


def _missing_ratios(data, periods):
    """这些报告期里 Yahoo 缺的 roe/roa（Yahoo 已有值的期**不在**清单里：降级不是替换）。"""
    missing = []
    for period in periods:
        entry = data.get("periods", {}).get(period) or {}
        if any(entry.get(field) is None for field in INDICATOR_FIELDS):
            missing.append(period)
    return missing


# ---------------------------------------------------------------------------
# 披露注册表：东财 RPT_LICO_FN_CPD 的 NOTICE_DATE（真实公告日）
# ---------------------------------------------------------------------------
def announcement_registry(period_iso, fetcher=None, needed_codes=None):
    """该报告期全市场公告日注册表 ``{code6: 'YYYY-MM-DD'}``（NOTICE_DATE，非 UPDATE_DATE）。

    ``fetcher`` 注入返回 ``(params) -> dict``（离线测试口径，即 API 的 JSON 响应）。
    网络路径串行分页（pageSize=500），收集齐 ``needed_codes`` 即提前停（礼貌分页）。
    上游异常 → ``RuntimeError``（可读，含报告期与原始异常摘要）。
    """
    cached = _REGISTRIES.get(period_iso)
    if cached is not None:
        return cached
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    base = {"sortColumns": "UPDATE_DATE,SECURITY_CODE", "sortTypes": "-1,-1",
            "pageSize": "500", "pageNumber": "1", "reportName": "RPT_LICO_FN_CPD",
            "columns": "ALL",
            "filter": f"(REPORTDATE='{period_iso}')"}
    wanted = {str(code).zfill(6) for code in (needed_codes or [])}

    def _call_once(params):
        if fetcher is not None:
            return fetcher(params)
        import requests
        response = requests.get(url, params=params, timeout=30)
        return response.json()

    try:
        first = _call_once(base)
        result = first.get("result") or {}
        pages = int(result.get("pages") or 0)
        registry = {}

        def _absorb(batch):
            for row in batch:
                code = str(row.get("SECURITY_CODE") or "").zfill(6)
                notice = row.get("NOTICE_DATE")
                if not code or not notice:
                    continue  # 未披露/脏行：注册表里就是没有，不猜
                registry[code] = str(notice)[:10]

        _absorb((result.get("data") or []))  # 第一页已随 ``first`` 取回，不重复请求
        page = 1
        while page <= pages:
            if wanted and wanted <= registry.keys():
                break  # 需要的代码都拿到了，不再翻页（礼貌分页）
            page += 1
            batch = (_call_once(dict(base, pageNumber=str(page))).get("result")
                     or {}).get("data") or []
            _absorb(batch)
    except RuntimeError:
        raise
    except Exception as error:  # noqa: BLE001 —— 网络/形状/解析异常统一可读化
        raise RuntimeError(
            f"披露注册表获取失败（RPT_LICO_FN_CPD REPORTDATE={period_iso}）："
            f"{type(error).__name__}: {error}") from error
    _REGISTRIES[period_iso] = registry
    return registry


def _statutory_deadline(period_iso):
    """A 股法定披露截止日（YYYY-MM-DD）；非标准报告期 → None（无闸门）。"""
    month_day = period_iso[5:]
    offset = STATUTORY_DEADLINE.get(month_day)
    if offset is None:
        return None
    year = int(period_iso[:4]) + (1 if month_day == "12-31" else 0)
    return f"{year}-{offset[0]:02d}-{offset[1]:02d}"


def resolve_announced_at(futu_symbol, period_iso, registries, run_date):
    """披露时点解析 → ``(announced_at, announced_source, None)`` 或 ``(None, None, 拒因)``。

    A 股：注册表 NOTICE_DATE + 法定窗口闸门（越界 = 上游给的不是本期披露日，拒绝）；
    注册表没有该期 → 拒绝（宁缺毋假，不写没有披露时点的行）。
    非 A 股：同步运行日（保守替代，如实标注）。
    """
    if is_a_share(futu_symbol):
        registry = registries.get(period_iso)
        if registry is None:
            return None, None, f"披露注册表不可用（报告期 {period_iso}）——宁缺毋假不写入"
        code = futu_symbol.split(".")[1] if "." in futu_symbol else futu_symbol
        notice = registry.get(str(code).zfill(6))
        if not notice:
            return None, None, (f"上游披露注册表无 {futu_symbol} 报告期 {period_iso} "
                                "的公告日（未披露或非 A 股口径）——宁缺毋假不写入")
        deadline = _statutory_deadline(period_iso)
        if not (period_iso < notice <= deadline):
            return None, None, (f"{futu_symbol} 报告期 {period_iso} 的 NOTICE_DATE={notice} "
                                f"越出法定披露窗口（{period_iso}, {deadline}］——"
                                "疑似上游行刷新时间（UPDATE_DATE）而非披露日，拒绝入库")
        return notice, SOURCE_NOTICE_DATE, None
    return run_date, SOURCE_RUN_DATE, None


# ---------------------------------------------------------------------------
# 主流程：串行拉取 → 披露闸门 → 同一写入口 upsert
# ---------------------------------------------------------------------------
def _watchlist_tickers(home, market):
    from trading_core.watchlist import watchlist_symbols
    return list(watchlist_symbols(home, market=market))


def sync(conn, market="SH", tickers=None, periods=None, home=None, dry_run=False,
         fetcher=None, registry_fetcher=None, sleep_seconds=SLEEP_BETWEEN_TICKERS,
         today=None, indicator_fetcher=None, indicator_fallback=True):
    """离线落库主入口。返回可 JSON 化的摘要（含失败清单），绝不为覆盖率隐瞒失败。

    ``indicator_fetcher`` 注入免密替代源的取数口（``fetcher(func_name, arg) -> frame``，
    离线测试口径）；``indicator_fallback=False`` 可整体关掉兜底（只跑 Yahoo 腿）。
    """
    started = time.time()
    run_date = (today or _dt.date.today().isoformat())
    if tickers is None:
        tickers = _watchlist_tickers(home, market)
    tickers = [t.strip() for t in (tickers or []) if t and t.strip()]
    requested = None
    if periods:
        requested = []
        for period in periods:
            text = str(period).strip()
            if not re.match(r"^\d{8}$", text):
                return {"ok": False, "error": f"--periods 需为 YYYYMMDD（收到：{period}）"}
            requested.append(f"{text[:4]}-{text[4:6]}-{text[6:]}")
    if not tickers:
        return {"ok": False, "error": f"标的清单为空（market={market} 且关注池无该市场标的）"}

    fetched, failures = {}, []
    for index, ticker in enumerate(tickers):
        try:
            fetched[ticker] = fetch_quarterly_returns(ticker, fetcher=fetcher)
        except (RuntimeError, Exception) as error:  # noqa: BLE001 —— 失败也是结果
            if isinstance(error, KeyboardInterrupt):
                raise
            failures.append({"ticker": ticker, "reason": str(error)[:200]})
        if sleep_seconds and index < len(tickers) - 1:
            time.sleep(sleep_seconds)

    # 免密替代源（AKShare 财务指标）：只补 Yahoo 缺 roe/roa 的**已锚定报告期**。
    # Yahoo 整条通道失败的票没有报告期锚点 → 不猜期（原因如实进 fallback_skipped）。
    indicator_data, fallback_failures, fallback_skipped = {}, [], []
    if indicator_fallback:
        for ticker, data in fetched.items():
            symbol = to_futu_symbol(ticker)
            anchored = [period for period in sorted(data["periods"])
                        if requested is None or period in requested]
            missing = _missing_ratios(data, anchored)
            if not missing:
                continue
            if not is_a_share(symbol):
                fallback_skipped.append({
                    "ticker": ticker, "periods": missing,
                    "reason": "非 A 股：AKShare 财务指标兜底源只覆盖 A 股（不猜映射）"})
                continue
            try:
                indicator_data[ticker] = fetch_indicator_returns(
                    ticker, fetcher=indicator_fetcher, periods=missing)
            except RuntimeError as error:
                fallback_failures.append({"ticker": ticker, "periods": missing,
                                          "reason": str(error)[:300]})
        for item in failures:
            fallback_skipped.append({
                "ticker": item["ticker"], "periods": [],
                "reason": "Yahoo 通道失败，没有报告期锚点 → 未触发免密替代源（不猜期）"})

    # 只对「真取到了数据」的报告期取披露注册表（一次/期，跨标的复用）
    needed = sorted({period for data in fetched.values()
                     for period in data["periods"]
                     if requested is None or period in requested})
    registries = {}
    for period in needed:
        codes = set()
        for ticker, data in fetched.items():
            if period in data["periods"]:
                symbol = to_futu_symbol(ticker)
                code = symbol.split(".")[1] if "." in symbol else symbol
                if is_a_share(symbol):
                    codes.add(code)
        if codes:
            try:
                registries[period] = announcement_registry(
                    period, fetcher=registry_fetcher, needed_codes=codes)
            except RuntimeError as error:
                registries[period] = None
                failures.append({"ticker": "*", "reason": str(error)[:200]})

    written_detail, skipped, rows_total = [], [], 0
    for ticker, data in fetched.items():
        symbol = to_futu_symbol(ticker)
        indicator = indicator_data.get(ticker)
        # 按来源分批：``source`` 列逐行如实（upsert 一个来源一批，不混标）
        batches = {SOURCE_YAHOO: []}
        if indicator:
            batches[indicator["source"]] = []
        for period, entry in sorted(data["periods"].items()):
            if requested is not None and period not in requested:
                continue
            announced, announced_source, refusal = resolve_announced_at(
                symbol, period, registries, run_date)
            if refusal:
                skipped.append({"ticker": ticker, "period": period, "reason": refusal})
                continue
            for field in FIELDS:
                value = entry.get(field)
                if value is None:
                    continue  # 取不到的字段不写（数据诚实：缺就是缺）
                batches[SOURCE_YAHOO].append({
                    "field": field, "period_end": period, "value": float(value),
                    "announced_at": announced, "announced_source": announced_source})
            # 替代源只补 Yahoo 缺的 roe/roa：同一披露闸门、同一 announced_at（既有口径）
            fallback_entry = (indicator or {}).get("periods", {}).get(period) or {}
            for field in INDICATOR_FIELDS:
                if entry.get(field) is not None or fallback_entry.get(field) is None:
                    continue  # Yahoo 有值就不覆盖（降级不是替换）；上游没给就不写
                batches[indicator["source"]].append({
                    "field": field, "period_end": period,
                    "value": float(fallback_entry[field]),
                    "announced_at": announced, "announced_source": announced_source})
        periods_written, rows_here, per_source = [], 0, {}
        for source, rows in batches.items():
            if not rows:
                continue
            count = len(rows) if dry_run else store.upsert_fundamentals(conn, symbol, rows, source)
            rows_here += count
            per_source[source] = count
            periods_written.extend(row["period_end"] for row in rows)
        if rows_here:
            rows_total += rows_here
            written_detail.append({"ticker": ticker, "symbol": symbol,
                                   "periods": sorted(set(periods_written)),
                                   "rows": rows_here, "sources": per_source})

    return {"ok": True, "market": market, "dry_run": bool(dry_run), "run_date": run_date,
            "tickers": tickers, "periods": needed,
            "rows_written": rows_total, "written": written_detail,
            "skipped": skipped, "failures": failures,
            "fallback": [
                {"ticker": ticker, "symbol": data["symbol"], "source": data["source"],
                 "caliber": data["caliber"], "periods": sorted(data["periods"]),
                 "attempts": data["attempts"]}
                for ticker, data in indicator_data.items()],
            "fallback_failures": fallback_failures,
            "fallback_skipped": fallback_skipped,
            "elapsed_s": round(time.time() - started, 2),
            "note": ("本模块是**离线落库通道**，不是矩阵取数路径：矩阵仍只读 store（快），"
                     "本模块把远端数据提前搬进 store；roe/roa 的 store 读取接线在 "
                     "v3_analytics 质量因子读数（未接线前矩阵 roe/roa 覆盖不变）；"
                     "Yahoo 缺 roe/roa 的期由免密替代源（AKShare 财务指标）补，"
                     "口径不同、source 逐行如实标注（见 fallback[].caliber）")}


# ---------------------------------------------------------------------------
# 修复通道：yjbb UPDATE_DATE 污染的 announced_at 一键对齐 NOTICE_DATE
# ---------------------------------------------------------------------------
def fix_announced_dates(conn, periods=None, dry_run=False, registry_fetcher=None):
    """把 ``announced_source='akshare/yjbb'``（实为 UPDATE_DATE）的行对齐到 NOTICE_DATE。

    背景：``trading_core.sync.merge_announcements_akshare`` 读的是 yjbb 的
    「最新公告日期」列——它是东财的**行刷新时间**（UPDATE_DATE），对老报告期是
    「下一年同季财报的公告日」（2026-09-21 实测：600000/2025-06-30 被写成
    2026-08-28，真实披露日 2025-08-28）。本修复把差异行改成 NOTICE_DATE 并改标
    ``announced_source='eastmoney/yjbb:NOTICE_DATE'``；NOTICE_DATE 越出法定窗口的
    （上游仍不可信）拒绝并如实列出。
    """
    started = time.time()
    rows = conn.execute(
        "SELECT DISTINCT symbol, period_end FROM fundamentals WHERE announced_at IS NOT NULL"
        + ("" if not periods else " AND period_end IN (%s)" %
           ",".join("?" for _ in periods)),
        list(periods or [])).fetchall()
    targets = [(str(row["symbol"]), str(row["period_end"])) for row in rows]
    # 先按报告期聚合**全部** A 股代码再取注册表：``announcement_registry`` 带进程内
    # 缓存 + 「集齐即提前翻页停止」——若逐代码取，首个代码就会把注册表**截断**在
    # 第一页并缓存，后续代码全部误报「注册表无该期公告日」（2026-09-21 实测缺陷：
    # 160 行只修出 42 行）。一次/期、带全量 wanted，提前停止才安全。
    codes_by_period: dict = {}
    for symbol, period in targets:
        if not is_a_share(symbol):
            continue  # yjbb 只覆盖 A 股；HK/US 本就没有公告日（futu 口径）
        code = symbol.split(".")[1] if "." in symbol else symbol
        codes_by_period.setdefault(period, set()).add(code)
    registries: dict = {}
    refused = []
    for period, codes in codes_by_period.items():
        try:
            registries[period] = announcement_registry(
                period, fetcher=registry_fetcher, needed_codes=codes)
        except RuntimeError as error:
            registries[period] = None
            refused.append({"symbol": "*", "period": period, "reason": str(error)[:160]})
    corrections, unchanged, missing = [], 0, []
    for symbol, period in targets:
        registry = registries.get(period)
        if not is_a_share(symbol) or registry is None:
            continue
        code = symbol.split(".")[1] if "." in symbol else symbol
        notice = registry.get(code)
        current = conn.execute(
            "SELECT announced_at, announced_source FROM fundamentals"
            " WHERE symbol=? AND period_end=? AND announced_at IS NOT NULL LIMIT 1",
            (symbol, period)).fetchone()
        if current is None:
            continue
        if notice is None:
            missing.append({"symbol": symbol, "period": period,
                            "reason": "注册表无该期公告日，保留现值不动"})
            continue
        deadline = _statutory_deadline(period)
        if not (period < notice <= deadline):
            refused.append({"symbol": symbol, "period": period,
                            "reason": f"NOTICE_DATE={notice} 越出窗口（{period},{deadline}］，保留现值"})
            continue
        if str(current["announced_at"]) == notice:
            unchanged += 1
            continue
        corrections.append({"symbol": symbol, "period": period,
                            "from": str(current["announced_at"]), "to": notice})
        if not dry_run:
            conn.execute(
                "UPDATE fundamentals SET announced_at=?, announced_source=?"
                " WHERE symbol=? AND period_end=? AND announced_at IS NOT NULL",
                (notice, SOURCE_NOTICE_DATE, symbol, period))
    if not dry_run:
        conn.commit()
    return {"ok": True, "dry_run": bool(dry_run), "targets": len(targets),
            "corrected": len(corrections), "detail": corrections,
            "unchanged": unchanged, "refused": refused, "missing": missing,
            "elapsed_s": round(time.time() - started, 2)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="server.v3_fundamentals_sync",
        description="roe/roa 离线落库通道（Yahoo 数值 + NOTICE_DATE 披露时点 → trading store；"
                    "不是矩阵取数路径，矩阵仍只读 store）")
    parser.add_argument("--market", required=True, choices=("SH", "HK", "US"),
                        help="市场标注 + 默认标的清单（关注池按市场过滤）")
    parser.add_argument("--tickers", default=None,
                        help="逗号分隔（缺省=关注池里该市场的标的）")
    parser.add_argument("--periods", default=None,
                        help="逗号分隔报告期 YYYYMMDD（缺省=通道返回的全部已披露期）")
    parser.add_argument("--db", default=None,
                        help="SQLite 路径（默认 $DSH_HOME/trading-data/trading.sqlite）")
    parser.add_argument("--home", default=None, help="DSH_HOME 覆盖（关注池读取用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只拉取与解析，不写库（结果里 rows 为计划写入数）")
    parser.add_argument("--fix-announced-at", action="store_true",
                        help="修复模式：把 akshare/yjbb（UPDATE_DATE 污染）的公告日"
                             "对齐到 NOTICE_DATE，不做 Yahoo 拉取")
    parser.add_argument("--no-indicator-fallback", action="store_true",
                        help="关掉免密替代源（AKShare 财务指标）：只跑 Yahoo 腿，"
                             "Yahoo 缺 roe/roa 的期就不写（缺就是缺）")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    periods = [p for p in (str(args.periods or "").split(",") if args.periods else []) if p]
    tickers = [t for t in (str(args.tickers or "").split(",") if args.tickers else []) if t]
    import os
    home = args.home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    conn = store.connect(args.db)
    try:
        if args.fix_announced_at:
            result = fix_announced_dates(conn, periods=periods or None,
                                         dry_run=args.dry_run)
        else:
            result = sync(conn, market=args.market,
                          tickers=tickers or None, periods=periods or None,
                          home=home, dry_run=args.dry_run,
                          indicator_fallback=not args.no_indicator_fallback)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if not result.get("ok"):
        return 1
    if result.get("failures") and not result.get("rows_written") and not result.get("corrected"):
        return 1  # 零成功且有失败：让调用方（作业链）看见
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
