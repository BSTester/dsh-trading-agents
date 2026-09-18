"""交易时段事实源（规格 §4.2 数据就绪门 / §4.3 守卫表；唯一实现，两组共用）。

本模块回答两个问题，别处不再各写一份：

  1. **某市场某本地日的真实收盘**（``session_close`` / ``session_close_beijing``）——
     供数据就绪门（``planner.data_date_for``）判定「本次负责的会话是否已收盘」；
  2. **某市场某本地日的可委托时段**（``windows_for`` / ``in_window``）——供平台层人工
     下单闸门（``platform/server/trading.py``）判定「现在能不能下单」。

时区一律用 ``zoneinfo``（**不手算 DST**）：SH/SZ/BJ = Asia/Shanghai、HK = Asia/Hong_Kong、
US = America/New_York。入库/日志口径仍沿既有约定（北京时间戳 + 市场本地日期）——本模块
只做「本地日 ↔ 时刻」的折算，不改变落库口径。

------------------------------------------------------------------------------
收盘时刻的算法（**这段推理就是实现口径，勿改成朴素版本**）
------------------------------------------------------------------------------
* **全天**（``trade_second`` 等于该市场全天秒数，或该行缺失 ``trade_second``）→ 用该市场
  **已知的全天收盘**（SH/SZ/BJ 15:00、HK 16:00、US 16:00 本地）。**刻意不用**
  「开盘 + trade_second」算全天：港股全天 5.5h 含 1h 午休，09:30 + 5.5h = 15:00 ≠ 真实的
  16:00；美股 09:30 + 6.5h = 16:00 恰好对，但那是巧合，不能依赖。
* **半日/提前收盘**（``trade_second`` 小于全天秒数）→ 收盘 = **开盘 + trade_second**，
  即按**单段（不含午休）**计算。

为什么不能用「全天收盘 − 缺口秒数」这个看起来更直观的算法：港股全天 5.5h 里含 1h 午休
（09:30–12:00 + 13:00–16:00），而**港股半日市只有上午**（09:30–12:00 = 2.5h）。
``16:00 − (5.5 − 2.5)h = 13:00`` 是**错的**（真实 12:00）；``09:30 + 2:30 = 12:00`` 才对。
美股同理：半日 09:30–13:00 = 3.5h，``09:30 + 3.5h = 13:00`` 正确。
**半日市在两地实践里都是单段（只有上午），所以午休不适用**——这就是本算法成立的理由；
全天仍走已知收盘表，避免用「开盘 + trade_second」把港股算成 15:00。

------------------------------------------------------------------------------
可委托窗口（人工下单闸门）的取向：**宁可放过、不可错杀**
------------------------------------------------------------------------------
闸门只挡**明确闭市**的时段，边界取宽松超集：错杀的代价是错过一次交易（且用户无从纠正），
放过的代价只是券商按自己的规则拒单（平台如实回报拒单）。逐市场：

* SH/SZ/BJ：``09:15–15:00`` 单段——含开盘集合竞价与**午间报单窗口**。午休**刻意算在内**：
  券商普遍接受午间报单并排队，用「非连续竞价」当理由挡掉属于错杀；
* HK：``09:00–16:10`` 单段——含 09:00 开市前竞价与 16:00–16:10 收市竞价；
* US：``session`` 为常规（``RTH`` 或缺省）→ ``09:30–16:00``；请求扩展时段（官方
  ``PLACE_SESSIONS`` 里除 RTH 外的取值：``RTH+Pre/Post-Mkt`` / ``OVERNIGHT`` /
  ``ALL_DAY``）→ ``04:00–20:00``（盘前 + 盘后的并集）。**已知近似**：券商的独立夜盘时段
  可能延伸到 20:00 ET 之后，本仓库**没有**该时刻表的权威来源，故按「盘前/盘后」口径
  处理——夜盘请求在本地 20:00 之后仍会被判闭市（如实登记为近似，不是精确的夜盘表；要
  精确覆盖得先拿到券商的夜盘时段来源）；
* **半日/提前收盘缩短该窗口**：窗口上界 = ``min(全天窗口上界, 真实收盘)``（HK 半日 →
  12:00 而非 16:10；US 半日 → 13:00）。**但只在缩短日取 min**——全天日的窗口上界保留
  窗口定义本身：HK 16:10 收市竞价、US 盘前盘后 04:00–20:00 都是全天日窗口的一部分，
  对它们取 min（16:00）会把真实可交易的时段挡掉，与「宁可放过」相反；
* 窗口是**左闭右开** ``[open, close)``：上界那一刻起已不可委托，与守卫 9 的执行窗口
  同一约定；
* 未知市场 / 未知 ``session`` 取值 → **fail-closed**（``windows_for`` 返回空列表，
  ``in_window`` 记不允许 + 明确原因）。下界晚于上界（极端缩短日）→ 同样视为
  **无可委托时段**（不是负长度窗口）。

``moment`` 的认读口径：**aware datetime 按其自身时区换算**；**naive datetime 或
``"%Y-%m-%d %H:%M:%S"`` 字符串一律按北京时间解释**（与 ``planner``/``daemon`` 的
北京时间戳口径一致）。时钟由调用方注入（本模块不读系统时间、不联网、无副作用）。
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

#: 北京时间（固定 +08:00；与 planner/daemon/trading 的既有口径一致）
BEIJING_TZ = timezone(timedelta(hours=8))

#: 市场 → IANA 时区名（DST 交给 zoneinfo）
MARKET_TZ = {"SH": "Asia/Shanghai", "SZ": "Asia/Shanghai", "BJ": "Asia/Shanghai",
             "HK": "Asia/Hong_Kong", "US": "America/New_York"}

#: 市场 → 全天交易秒数（实测：SH 4h / HK 5.5h / US 6.5h）
FULL_DAY_SECONDS = {"SH": 14400, "SZ": 14400, "BJ": 14400, "HK": 19800, "US": 23400}

#: 市场 → 已知全天收盘（本地 ``(时, 分)``）。全天行**只认这张表**，不按秒数反推。
FULL_DAY_CLOSE = {"SH": (15, 0), "SZ": (15, 0), "BJ": (15, 0), "HK": (16, 0), "US": (16, 0)}

#: 市场 → 开盘（本地 ``(时, 分)``）。各市场均为 09:30；半日收盘 = 开盘 + trade_second。
MARKET_OPEN = {"SH": (9, 30), "SZ": (9, 30), "BJ": (9, 30), "HK": (9, 30), "US": (9, 30)}

#: 市场 → 全天日可委托窗口 ``(下界, 上界)``（本地；左闭右开）
ORDER_WINDOWS = {"SH": ((9, 15), (15, 0)), "SZ": ((9, 15), (15, 0)),
                 "BJ": ((9, 15), (15, 0)), "HK": ((9, 0), (16, 10))}
#: 美股：常规时段（``session`` 缺省或 ``RTH``）与扩展时段（其余官方取值）
US_RTH_WINDOW = ((9, 30), (16, 0))
US_EXTENDED_WINDOW = ((4, 0), (20, 0))
#: 官方 ``PLACE_SESSIONS`` 的**常规**取值；其余已知取值按扩展时段处理（见模块 docstring）。
US_REGULAR_SESSIONS = (None, "RTH")

_KNOWN_MARKETS = tuple(MARKET_TZ)


def market_tz(market):
    """市场 → ``ZoneInfo``；未知市场 → ``None``（调用方 fail-closed）。"""
    name = MARKET_TZ.get(str(market or "").upper())
    return ZoneInfo(name) if name is not None else None


def full_day_seconds(market):
    """市场全天交易秒数；未知市场 → ``None``。"""
    return FULL_DAY_SECONDS.get(str(market or "").upper())


def is_short_day(market, trade_second):
    """该行是否**缩短日**（半日/提前收盘）：``0 < trade_second < 全天秒数``。

    ``None``（缺失）、非正整数、等于全天秒数 → ``False``（走已知全天收盘表）。非正整数
    按缺失处理是防御性读法：真出现 0/负数说明上游写坏了，回落全天口径比凭空把收盘
    算成开盘时刻更保守（后者会让数据就绪门当天提前放行）。
    """
    seconds = _positive_int(trade_second)
    full = full_day_seconds(market)
    return bool(seconds and full and seconds < full)


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _at(market, day, hhmm, tz):
    parsed = day if isinstance(day, date) else date.fromisoformat(str(day))
    hour, minute = hhmm
    return datetime(parsed.year, parsed.month, parsed.day, hour, minute, tzinfo=tz)


def session_close(market, local_date, trade_second=None):
    """该市场该本地日的**真实收盘**（tz-aware，市场本地时区）。

    全天（``trade_second`` 缺失/等于全天秒数）→ 已知收盘表；缩短日 → 开盘 + 秒数。
    未知市场 → ``KeyError``（调用方先判市场，不猜）。
    """
    market = str(market or "").upper()
    tz = market_tz(market)
    if tz is None:
        raise KeyError(f"未知市场：{market!r}（支持 {'/'.join(_KNOWN_MARKETS)}）")
    if is_short_day(market, trade_second):
        open_dt = _at(market, local_date, MARKET_OPEN[market], tz)
        return open_dt + timedelta(seconds=_positive_int(trade_second))
    return _at(market, local_date, FULL_DAY_CLOSE[market], tz)


def session_close_beijing(market, local_date, trade_second=None):
    """真实收盘的**北京时间**（naive，与 ``planner`` 的北京时刻戳可直接比较）。"""
    return session_close(market, local_date, trade_second).astimezone(
        BEIJING_TZ).replace(tzinfo=None)


def parse_moment(moment):
    """``moment`` → tz-aware datetime：aware 原样，naive/字符串按**北京时间**解释。"""
    if isinstance(moment, datetime):
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=BEIJING_TZ)
    text = str(moment).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise ValueError(f"时刻需为 YYYY-MM-DD HH:MM:SS（北京时间），收到 {text!r}") from error
    return parsed.replace(tzinfo=BEIJING_TZ)


def market_local(market, moment):
    """``moment`` → 该市场本地时区的时刻（未知市场 → ``KeyError``）。"""
    tz = market_tz(market)
    if tz is None:
        raise KeyError(f"未知市场：{market!r}（支持 {'/'.join(_KNOWN_MARKETS)}）")
    return parse_moment(moment).astimezone(tz)


#: 官方 ``PLACE_SESSIONS`` 里按「扩展时段」处理的取值（与
#: ``platform/server/trading.py`` 的 ``PLACE_SESSIONS`` 同源；由锁定测试守漂移）。
US_EXTENDED_SESSIONS = ("RTH+Pre/Post-Mkt", "OVERNIGHT", "ALL_DAY")


def _us_window(us_session):
    """美股窗口：常规（``None``/``RTH``）→ 09:30–16:00；已知扩展取值 → 04:00–20:00；
    未知取值 → ``None``（fail-closed）。"""
    if us_session in US_REGULAR_SESSIONS:
        return US_RTH_WINDOW
    if isinstance(us_session, str) and us_session in US_EXTENDED_SESSIONS:
        return US_EXTENDED_WINDOW
    return None


def _window_for(market, us_session):
    if market == "US":
        return _us_window(us_session)
    return ORDER_WINDOWS.get(market)


def windows_for(market, local_date, trade_second=None, us_session=None):
    """该市场该本地日的**可委托窗口**列表（tz-aware，本地时区）。

    正常情况返回单个 ``(open_dt, close_dt)``（左闭右开）；未知市场/未知 ``session``/
    下界晚于上界 → 空列表（fail-closed，调用方按「无可委托时段」拒绝）。
    """
    market = str(market or "").upper()
    tz = market_tz(market)
    span = _window_for(market, us_session)
    if tz is None or span is None:
        return []
    lower = _at(market, local_date, span[0], tz)
    upper = _at(market, local_date, span[1], tz)
    if is_short_day(market, trade_second):
        # 缩短日：上界取**真实收盘**（HK 半日 12:00 而非 16:10；US 半日 13:00）
        upper = min(upper, session_close(market, local_date, trade_second))
    if lower >= upper:
        return []
    return [(lower, upper)]


def format_windows(windows):
    """窗口的可读文案（错误消息与排障共用同一格式）。"""
    if not windows:
        return "无（当日已无可委托时段）"
    return "、".join(f"{open_dt:%H:%M}–{close_dt:%H:%M}" for open_dt, close_dt in windows)


def in_window(market, moment, us_session=None, trade_second=None):
    """``(allowed, reason)``：``moment`` 是否落在该市场当日可委托窗口内。

    ``reason`` 在拒绝时携带排障所需的全部事实（市场、当前市场本地时间、当日窗口、
    以及「平台前置校验、券商同样会拒」的定性），便于 ``trading`` 层直接信封化。
    """
    market = str(market or "").upper()
    if market_tz(market) is None:
        return False, (f"未知市场 {market or moment!r}：无时段口径，按 fail-closed 拒绝"
                       f"（支持 {'/'.join(_KNOWN_MARKETS)}）")
    if market == "US" and _us_window(us_session) is None:
        return False, (f"未知的 session 取值 {us_session!r}：无时段口径，按 fail-closed 拒绝"
                       f"（常规 RTH；扩展 RTH+Pre/Post-Mkt/OVERNIGHT/ALL_DAY）")
    local = market_local(market, moment)
    windows = windows_for(market, local.date().isoformat(), trade_second=trade_second,
                          us_session=us_session)
    stamp = f"{market} 当前市场本地时间 {local:%Y-%m-%d %H:%M:%S}（{local.tzname()}）"
    tail = ("本闸门是平台前置校验（未触达券商），券商同样会以非交易时段拒单")
    if windows and any(open_dt <= local < close_dt for open_dt, close_dt in windows):
        return True, f"在可委托时段内：{stamp}，当日窗口 {format_windows(windows)}"
    return False, (f"{stamp} 不在当日可委托时段 {format_windows(windows)} 内；"
                   f"{tail}（口径：宁可放过、不可错杀——只挡明确闭市）")
