#!/usr/bin/env python3
"""基本面备用源：ROE / ROA（富途给不了）。

**为什么需要备用源**：实测富途 91 个工具里没有任何资产负债表接口，
`quote_financials_statements` 的所有报表类型返回的都是利润表科目，
因此 ROE/ROA 无法从富途得到。按渠道优先级（富途优先、其余备用）落到这里。

两条备用通道（2026-09-13 实测均可用于三市场）：
  1. Yahoo Finance（yfinance）：`balance_sheet` + `income_stmt`，美股/港股/A股都可用
  2. AKShare（东方财富）：仅 A 股，作为第二道备用

诚实性约束：
  * 返回里必须带 `source` 与**两个报表期**（资产负债表期、利润表期），
    期数不一致时如实标出，不假装同口径。
  * 两条通道都拿不到时返回 `available: False` 且带上已尝试的原因，
    **不估算、不用行业均值顶替**。
"""
import os
import re

SOURCE_YAHOO = "yahoo/yfinance"
SOURCE_AKSHARE = "akshare/东方财富"

# Yahoo 的 A 股后缀
_A_SUFFIX = {"SH": "SS", "SZ": "SZ", "BJ": "BJ"}


def to_yahoo_symbol(ticker):
    """把内部写法归一为 Yahoo 代码；无法映射返回 None。"""
    text = str(ticker).strip().upper()
    if not text:
        return None
    # 已是 MARKET.CODE
    match = re.fullmatch(r"(SH|SZ|BJ|HK|US)\.([A-Z0-9.]+)", text)
    if match:
        market, code = match.group(1), match.group(2)
    else:
        # CODE.MARKET
        match = re.fullmatch(r"([A-Z0-9.]+)\.(SH|SZ|BJ|HK|US)", text)
        if match:
            code, market = match.group(1), match.group(2)
        elif re.fullmatch(r"\d{6}", text):
            market, code = ("SH" if text[0] in "69" else "BJ" if text[0] in "48" else "SZ"), text
        elif re.fullmatch(r"\d{1,5}", text):
            market, code = "HK", text
        else:
            market, code = "US", text
    if market == "HK":
        # Yahoo 港股用 4 位代码：700 / 0700 / 00700 / 09988 → 0700.HK / 9988.HK
        return f"{code.lstrip('0').zfill(4)}.HK"
    if market in _A_SUFFIX:
        return f"{code}.{_A_SUFFIX[market]}"
    return code


def _first_present(frame, names):
    for name in names:
        if frame is not None and not frame.empty and name in frame.index:
            series = frame.loc[name]
            for value in series:
                if value == value and value is not None:   # 跳过 NaN
                    return float(value)
    return None


def _column_date(frame, name):
    try:
        return str(frame.columns[0])[:10]
    except (IndexError, AttributeError):
        return None


def from_yahoo(ticker):
    """从 Yahoo 取股东权益、总资产、净利润并计算 ROE/ROA。失败返回 None。"""
    symbol = to_yahoo_symbol(ticker)
    if symbol is None:
        return None
    try:
        import yfinance as yf
        handle = yf.Ticker(symbol)
        balance = handle.balance_sheet
        income = handle.income_stmt
    except Exception:  # noqa: BLE001 - 备用源失败即降级
        return None
    if balance is None or balance.empty:
        return None
    equity = _first_present(balance, ["Stockholders Equity", "Common Stock Equity",
                                      "Total Equity Gross Minority Interest"])
    assets = _first_present(balance, ["Total Assets"])
    net_income = _first_present(income, [
        "Net Income From Continuing Operation Net Minority Interest",
        "Net Income From Continuing And Discontinued Operation",
        "Net Income", "Net Income Common Stockholders"])
    if equity is None and assets is None:
        return None
    return {
        "available": True,
        "source": SOURCE_YAHOO,
        "symbol": symbol,
        "equity": equity,
        "total_assets": assets,
        "net_income": net_income,
        "currency": None,   # yfinance 未在行列里给出币种，不猜
        "balance_period": _column_date(balance, "balance"),
        "income_period": _column_date(income, "income"),
        **returns_of(net_income, equity, assets),
    }


def from_akshare(ticker):
    """A 股的第二道备用（东方财富）。非 A 股返回 None。"""
    text = str(ticker).strip().upper()
    code = text.split(".")[0] if "." in text else text
    if not re.fullmatch(r"\d{6}", code):
        return None
    prefix = "SH" if code[0] in "69" else "BJ" if code[0] in "48" else "SZ"
    try:
        import akshare as ak
        balance = ak.stock_balance_sheet_by_report_em(symbol=f"{prefix}{code}")
        income = ak.stock_profit_sheet_by_report_em(symbol=f"{prefix}{code}")
    except Exception:  # noqa: BLE001
        return None
    if balance is None or balance.empty or income is None or income.empty:
        return None

    def cell(frame, column):
        if column not in frame.columns:
            return None
        value = frame.iloc[0][column]
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    equity = cell(balance, "TOTAL_PARENT_EQUITY") or cell(balance, "TOTAL_EQUITY")
    assets = cell(balance, "TOTAL_ASSETS")
    net_income = cell(income, "PARENT_NETPROFIT") or cell(income, "NETPROFIT")
    if equity is None and assets is None:
        return None
    balance_period = str(balance.iloc[0].get("REPORT_DATE"))[:10] if "REPORT_DATE" in balance.columns else None
    income_period = str(income.iloc[0].get("REPORT_DATE"))[:10] if "REPORT_DATE" in income.columns else None
    return {
        "available": True,
        "source": SOURCE_AKSHARE,
        "symbol": f"{prefix}{code}",
        "equity": equity,
        "total_assets": assets,
        "net_income": net_income,
        "currency": "CNY",
        "balance_period": balance_period,
        "income_period": income_period,
        **returns_of(net_income, equity, assets),
    }


def returns_of(net_income, equity, assets):
    """ROE = 净利润/股东权益；ROA = 净利润/总资产。分母非正时给 None。"""
    def ratio(numerator, denominator):
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return round(numerator / denominator * 100, 2)

    return {"roe": ratio(net_income, equity), "roa": ratio(net_income, assets)}


def load_returns(ticker):
    """按优先级尝试备用源。全部失败时 available=False 并列出已尝试的通道。"""
    tried = []
    for name, loader in (("yfinance", from_yahoo), ("akshare", from_akshare)):
        try:
            result = loader(ticker)
        except Exception as error:  # noqa: BLE001
            tried.append({"source": name, "reason": str(error)[:120]})
            continue
        if result:
            result["note"] = (
                "ROE/ROA 来自富途之外的备用源（富途无资产负债表接口）；"
                "口径为「最近报告期净利润 / 最近期末股东权益」，两个报表期已分别列出，"
                "期数不一致时不要跨期比较。")
            return result
        tried.append({"source": name, "reason": "未返回可用数据"})
    return {
        "available": False,
        "roe": None, "roa": None,
        "tried": tried,
        "reason": "富途无资产负债表接口；备用源（Yahoo Finance / AKShare）也未取到",
    }
