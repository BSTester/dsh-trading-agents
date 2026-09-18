"""计划生成与冻结（规格 §6.1）：diff 只生成必要的整手订单；冻结后不可变。

``plan_auto`` 是 WP9 的 build_plan 作业体（规格 §4.2）：策略权重 → 冻结 auto 计划。
它是**机械作业**——输入只有 PIT store、配置与券商只读查询，零 LLM 参与。
"""
import hashlib
import inspect
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import indicators, oms, sessions, store

_TZ8 = timezone(timedelta(hours=8))

# 标的市场前缀 → 交易日历市场（数据就绪门按日历判定，规格 §4.2）。
# 与 platform/server/trading.py 的 CALENDAR_MARKET 同口径：core 不能 import server
# 包，因此这里是镜像；漂移由 tests/test_wp9_plan_auto.py 的锁定用例炸掉。
CALENDAR_MARKET = {"SH": "SH", "SZ": "SH", "BJ": "SH", "HK": "HK", "US": "US"}
#: 作业链市场（配置 exec_at / 关注池分片以此为准；SZ/BJ 归 SH 链）
CHAIN_MARKETS = ("SH", "HK", "US")
#: 关注池新鲜度回看窗口（够覆盖长假即可）
READINESS_LOOKBACK_DAYS = 40
#: 市场会话收盘的**北京时刻**（相对会话本地日）：``(天数偏移, "HH:MM")``。
#: 作业按北京时间触发，bars 与交易日历按**市场本地日期**落库——美股链两者相差一天
#: （ET 会话收盘落在北京次日）。US 取 EST 最晚界 05:00（EDT 实为 04:00）：两制下
#: 都已收盘，全年成立且**偏保守**（宁可晚 1 小时判定就绪，不拿未收盘的会话当已收盘）。
SESSION_CLOSE_BEIJING = {"SH": (0, "15:00"), "SZ": (0, "15:00"), "BJ": (0, "15:00"),
                         "HK": (0, "16:00"), "US": (1, "05:00")}

#: 目标外持仓自动清出的开关键（``~/.dsh/trading-platform.json`` **顶层**，与
#: ``futu_channel``/``watchlist``/``sentiment_budget_seconds`` 同级；命名与读取范式照
#: ``sentiment.BUDGET_CONFIG_KEY``：模块级常量命名键 + 从平台配置读）。**默认 False**：
#: 键缺失即关闭，任何既有行为不受影响。
EXIT_OUTSIDE_TARGET_CONFIG_KEY = "exit_outside_target"
#: 三条新告警标题都是**稳定字面量**（变量信息只进 detail）：``pipeline`` 的
#: ``_CONTENT_OUTCOMES`` 按标题精确匹配归因，标题里掺变量等于让归因悄悄失效。
EXIT_ALERT_TITLE = "计划预警：目标外持仓清出"
EXIT_EMPTY_TARGET_ALERT_TITLE = "计划预警：目标为空未清出"
EXIT_CONFIG_ALERT_TITLE = "exit_outside_target 配置非法"
#: 清出标的的 skipped 后缀（``build_and_freeze`` 的 ``skipped`` 与告警 detail 共用字面量）
EXIT_NO_PRICE_REASON = "(无价,无法清出)"
EXIT_UNSELLABLE_REASON = "(T+N不可卖)"


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def _risk_defaults():
    """风控默认值（镜像唯一源 ``daemon.RISK_DEFAULTS``；惰性导入避免模块级环依赖）。"""
    from . import daemon
    return dict(daemon.RISK_DEFAULTS)


def build_and_freeze(conn, mode, strategy_id, target, broker_positions, prices,
                     as_of, lot=100, origin="manual", market=None, risk_config=None,
                     managed=None, broker_cash=None, exit_symbols=None):
    """冻结一份计划。``origin``/``market`` 是**来源与归属元数据**（规格 §4.6）：

    * ``origin="auto"`` 的计划供自动执行链识别（``store.get_latest_auto_plan`` /
      ``cancel_stale_auto_plans``）；``"manual"`` 保持既有语义（``market`` 为 NULL）；
    * 二者**不参与 content_hash**——hash 是计划内容的指纹，来源不是内容；既有 hash
      口径不变，历史计划不需重算，也让「同一内容同一 hash」的校验继续成立。

    **冻结即登记**（WP9 任务 4 修订）：订单 diff 同时写入 OMS ``orders`` 表（状态
    ``draft``）——``execute.run`` 只认 ``store.get_orders_by_plan``，不登记就等于计划
    永远执行不到任何单（人工 plan-execute 与 WP9 auto_execute 都走这条链路）。
    幂等由 ``oms.register_order`` 的在途单查重把守：同一 plan_id 重复登记会抛
    ``DuplicateOpenOrder``，**如实传播**，不静默吞。

    **定量口径（规格 §4.2 第 5 点，2026-09-16 修订）**：权重是**上限**，实际下单量
    受单笔风险预算约束，两者自洽于同一止损距离（``indicators.stop_distance`` 唯一实现，
    与执行侧规则 4 同源）::

        权重定量  = 权益 × 权重 ÷ 价（整手向下取整）
        风险预算  = floor(权益 × risk_per_trade ÷ 止损距离 ÷ lot) × lot
        加仓量    = min(增量, 风险预算)          # 增量 = 权重定量 − 当前持仓

    「上限只约束**增量**」是刻意的读法（``qty = min(权重定量, 风险预算)`` 的直译只在
    空仓时等价）：若把上限套在**目标持仓**上，已持仓 5000 股、目标 5000 股的标的会被
    压成 1700 股目标 → 凭空产生 3300 股**非预期卖出**；同理减仓（delta<0）不受风险预算
    限制——减少敞口不是新增风险。因此：加仓量取 min 且不为负；减仓量照常全额执行。

    ``跳过``（返回 ``skipped`` 列表，**不静默**）：
      * ``"SYM(无ATR)"``：bar 不足或 ATR 为 0 → 算不出止损距离 → 跳过。**不退回
        「无止损全额定量」**：那必然被规则 4 拦下（徒劳计划），也绝不用 0 止损把
        规则 4 静默废除；
      * ``"SYM(风险预算不足一手)"``：预算连一手都买不起 → 跳过（如实列出，不生成 0 单）。

    **受管集合与退出路径（规格 §4.2 第 6 点，2026-09-16 修订）**：``managed`` 给出该
    策略**负责的全部标的**（关注池 ∩ 策略 universe）。diff 在 ``managed ∪ target`` 上做：
    ``managed`` 中缺席者目标权重为 0 → 券商实际持有则**全额卖出**（清仓）。没有这条，
    策略不再返回的已持仓标的永远不进 diff，自动流水线就**只买不退**。

    * ``managed=None``（缺省）保持既有语义：只遍历 ``target`` 的键——既有调用方与
      测试行为逐字不变；
    * ``managed`` **之外**的持仓不进 diff（不清理用户手工持仓）；
    * 遍历顺序 = ``target`` 原序 + ``managed`` 缺席者的代码升序（确定性，不依赖 dict
      序），且 ``managed=None`` 时与既有顺序完全一致；
    * 清仓量不受风险预算约束（减少敞口不是新增风险，与减仓同口径）。

    **目标外清出（规格 §4.2 第 6 点修订，2026-09-17）**：``exit_symbols`` 是调用方显式给出
    的**清出集合**，语义 = 「这些标的的目标权重按 0 处理」。它与 ``managed`` 的区别是来源：
    ``managed`` 是「策略负责范围」的**自动**推导（关注池 ∩ 策略 universe），而
    ``exit_symbols`` 是 ``plan_auto`` 在开关 ``exit_outside_target`` 打开时算出的
    「券商持仓 − 当日 target 键」——**能清掉 ``managed`` 之外的存量持仓**，正是受管集合
    机制刻意不碰、又会让账户与策略组合长期不收敛的那部分::

        diff 符号集 = list(target) + [s for s in sorted(set(managed or ()) | set(exit_symbols or ()))
                                      if s not in target]

    * ``exit_symbols=None``（缺省）与既有行为**逐字一致**：返回体**无** ``exits`` 键、
      符号集与顺序不变（``set() | set()`` 为空）；
    * 传了该参数时返回体带 ``exits``（**排序去重后的清出集合**）供审计——它是调用方的
      请求，不等于最终产单（无价/不可卖的标的会进 ``skipped``）；
    * **无价即跳过并如实记账**：清出标的取不到价格时记 ``"SYM(无价,无法清出)"``（既有
      ``target`` 路径的无价静默行为不变——那里由调用方的 ``no_price`` 清单负责）；
    * **可卖数量封顶**：持仓条目带 ``available`` 时（``with_marks=True`` 的券商快照），
      卖出量取 ``min(需卖量, available)``；``available=0`` 记 ``"SYM(T+N不可卖)"``，
      **不生成注定被券商拒绝的单**；字段缺失（``None``）按既有口径用全部 qty；
    * 清出标的一旦进入 diff，与 ``managed`` 缺席者同权：目标权重 0、全额卖出、不受风险
      预算与现金约束。

    ``risk_config`` 缺省用 ``daemon.RISK_DEFAULTS``；``plan_auto`` 传
    ``daemon.risk_config(home)``（含 ``~/.dsh/trading-risk.json`` 覆盖）。部分字段的
    覆盖字典按「缺省补默认」合并——**不重写配置读取实现**。

    **买入按可用现金封顶（2026-09-17 实机修订：模拟盘全自动的结构性阻塞）**：权重定量
    与风险预算都只看**权益**，不看现金；实机 A 股模拟账户「权益 81 万 / 可用现金 5.5 万 /
    已持 8 只」时，任何买单都会被券商以**资金不足**拒绝（或先被规则 6 拦），自动闭环
    结构性跑不起来。因此多一条现金约束::

        可买量 = floor(剩余现金 ÷ (价 × lot)) × lot
        加仓量 = min(权重定量, 风险预算, 可买量)

    * ``broker_cash``：``fn(mode) -> float | None`` 的券商现金查询（**与权益同源：
      券商事实，不读本地台账**）。缺省 ``None`` = 调用方未提供现金事实 → 保持既有语义
      （``plan-build`` 等离线/诊断路径与既有测试不受影响）；
    * **多买单共享同一笔现金**：按计划内买单顺序（``target`` 原序 + managed 缺席者代码
      升序，确定性）**逐单累计扣减**，绝不让每单都按全额现金定量。选择顺序分配而非按比例
      分配，是为了保留策略给出的优先级顺序，且结果可逐单复算；
    * **卖单不受现金约束**（减少敞口）、**卖出所得不计入可买现金**——成交与到账时点不
      保证先于买单，把未成交的卖出款当可用资金是无根据的假设（宁可少买，不可凭空多买）；
    * ``broker_cash`` 返回 ``None``（字段全缺）→ **一笔买单都不生成**，逐个记入
      ``skipped`` 的 ``"(现金不可得)"``，并在返回值的 ``cash.unavailable`` 如实标注；
      **绝不用权益冒充现金**；
    * 现金连一手都买不起 → 该标的记 ``"(现金不足一手)"``（不生成 0 单）；被现金压低
      数量的标的列入 ``cash.capped``（如实告诉调用方「想买多少、实际能买多少」）。
    """
    positions, equity = broker_positions(mode)
    cfg = _risk_defaults()
    if risk_config:
        cfg.update(risk_config)
    orders, skipped = [], []
    plan_id = f"PLN-{as_of.replace('-', '')}-{mode}-{uuid.uuid4().hex[:4].upper()}"
    # 现金约束（见 docstring）：调用方未提供现金事实时 cash_remaining 保持 None（既有语义）
    cash_available = cash_remaining = None
    cash_unavailable, cash_capped = False, []
    if broker_cash is not None:
        cash_available = broker_cash(mode)
        if cash_available is None:
            cash_unavailable = True
        else:
            cash_remaining = max(0.0, float(cash_available))
    # target 原序在前、managed 缺席者按代码升序在后：managed=None 时与既有顺序逐字一致；
    # exit_symbols 只在**显式给出**时参与并集（缺省 = 空集，符号集与顺序不变）。
    exit_set = set(exit_symbols or ())
    symbols = list(target) + [s for s in sorted(set(managed or ()) | exit_set)
                              if s not in target]
    for symbol in symbols:
        px = prices.get(symbol)
        if not px:
            if symbol in exit_set:
                # 清出标的无价：**记 skipped**（宁缺毋假，不猜价、不生成假单）。既有 target
                # 路径的无价静默行为刻意不动——那里由调用方的 no_price 清单如实承接。
                skipped.append(f"{symbol}{EXIT_NO_PRICE_REASON}")
            continue  # 无价（停牌/无行情）：跳过并在审计可见，不猜价
        want_qty = int(equity * target[symbol] / px // lot * lot) if symbol in target else 0
        have = positions.get(symbol) or {}
        have_qty = have.get("qty", 0)
        delta = want_qty - have_qty
        if delta == 0:
            continue
        if delta > 0:  # 加仓：受单笔风险预算约束（减仓/清仓不受限，见 docstring）
            stop_dist = indicators.stop_distance(conn, symbol, as_of, cfg["stop_atr_mult"])
            if stop_dist is None:
                skipped.append(f"{symbol}(无ATR)")
                continue
            budget_qty = int(equity * cfg["risk_per_trade"] / stop_dist // lot * lot)
            delta = min(delta, budget_qty)
            if delta <= 0:
                skipped.append(f"{symbol}(风险预算不足一手)")
                continue
            if cash_unavailable:
                skipped.append(f"{symbol}(现金不可得)")
                continue
            if cash_remaining is not None:
                # 可买量按剩余现金（**共享**：每单扣减，见 docstring）
                affordable = int(cash_remaining // (px * lot)) * lot
                if affordable < delta:
                    cash_capped.append(symbol)
                delta = min(delta, affordable)
                if delta <= 0:
                    skipped.append(f"{symbol}(现金不足一手)")
                    continue
                cash_remaining -= delta * px
        else:
            # 减仓/清仓：券商给了可卖数量就按它封顶（T+N：当日买入的股份不可卖），
            # 否则生成的是**注定被拒**的单。字段缺失（None）→ 既有口径：卖全部 qty。
            available = have.get("available")
            if available is not None:
                sellable = int(available)
                if sellable <= 0:
                    skipped.append(f"{symbol}{EXIT_UNSELLABLE_REASON}")
                    continue
                delta = -min(-delta, sellable)
        orders.append({"symbol": symbol, "market": symbol.split(".")[0],
                       "side": "BUY" if delta > 0 else "SELL", "qty": abs(delta),
                       "price": px})
    content = json.dumps({"plan_id": plan_id, "as_of": as_of, "mode": mode,
                          "strategy_id": strategy_id, "target": target,
                          "orders": orders}, sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
                 "status,created_at,origin,market) VALUES(?,?,?,?,?,?,'frozen',?,?,?)",
                 (plan_id, as_of, mode, strategy_id, json.dumps(target, ensure_ascii=False),
                  content_hash, _now(), origin, market))
    conn.commit()
    # 冻结即登记：订单进 OMS（draft），execute.run 据此取单。plan_hash 参数传 content_hash
    # ——与 execute.run 校验用的计划指纹同源，登记与执行两侧口径一致。
    for order in orders:
        oms.register_order(conn, plan_id, order["symbol"], order["market"],
                           order["side"], order["qty"], order["price"], mode, content_hash)
    result = {"plan_id": plan_id, "as_of": as_of, "mode": mode, "status": "frozen",
              "orders": orders, "content_hash": content_hash, "skipped": skipped}
    if broker_cash is not None:
        # 只在调用方提供了现金事实时才带该键：缺省路径的返回字典逐字不变（既有调用方/测试）
        result["cash"] = {"available": cash_available, "remaining": cash_remaining,
                          "unavailable": cash_unavailable,
                          "capped": sorted(cash_capped)}
    if exit_symbols is not None:
        # 同上：缺省路径返回字典逐字不变；显式给出清出集合时如实回报（审计用）
        result["exits"] = sorted(exit_set)
    return result


def read_mode(home):
    """账户模式真源（与 platform/server/store_access.read_mode 同语义）：
    文件缺失=sim，内容非法抛 ValueError（不静默回退）。"""
    try:
        text = (Path(home) / "trading-account-mode").read_text(encoding="utf-8")
    except FileNotFoundError:
        return "sim"
    value = text.strip().lower()
    if value not in ("sim", "live"):
        raise ValueError(f"账户模式非法：{value!r}")
    return value


def exit_outside_target_enabled(platform_cfg):
    """读「目标外持仓自动清出」开关（``~/.dsh/trading-platform.json`` 顶层键）。

    取值口径（fail-closed，与本仓库既有配置纪律一致——配置写错**宁可拒绝**，不静默降级）：

    * 键**缺失** → ``False``：默认关闭，不改变任何既有行为（默认态不是异常）；
    * 键为**真布尔**（``True``/``False``）→ 原样返回；
    * 其余（字符串 ``"true"``/数字 ``1``/``None``/容器/……）→ ``ValueError``：把 ``"true"``
      静默当假会让「配置写了但没生效」伪装成「功能没开」；``1`` 则相反地可能被当成开，
      两种误读都不可接受。**调用方（``plan_auto``）按软跳过处理**，绝不把异常抛给作业层
      （作业契约「永不抛」）。

    ``bool`` 判定必须用 ``isinstance(value, bool)``：Python 里 ``True`` 也是 ``int``，
    ``isinstance(1, bool)`` 为假而 ``isinstance(True, int)`` 为真——顺序写反就漏掉数字。
    """
    value = (platform_cfg or {}).get(EXIT_OUTSIDE_TARGET_CONFIG_KEY, False)
    if not isinstance(value, bool):
        raise ValueError(f"{EXIT_OUTSIDE_TARGET_CONFIG_KEY} 需为布尔值（true/false），"
                         f"收到 {value!r}")
    return value


def symbols_for_market(home, market):
    """关注池里属于该市场链的标的（SH/SZ/BJ 同属 SH 链；大写归一）。

    委托 ``watchlist`` 模块的唯一实现（WP9 修订 I4）：读取/切分/市场分片只有一份，
    原先 planner 与 strategies 各写一遍已漂移出「有/无市场过滤」两种口径。
    """
    from . import watchlist
    return watchlist.watchlist_symbols(home, market=market)


def _stamp_parts(stamp):
    """解析作业时刻：完整时刻 ``YYYY-MM-DD HH:MM:SS`` 或纯日期 ``YYYY-MM-DD``。

    纯日期按**当日 23:59:59** 解释（「这一天已经过完」）：``plan_auto`` 的 ``today``
    注入口径与既有测试都传日期，语义是「该日收盘后」；把纯日期当 00:00 会让按日注入的
    调用一律落在收盘前而被保守跳过——那不是它们的本意。非法格式如实抛 ``ValueError``
    （不静默回落：写错的时间戳会让演练结论失真，沿用 ``daemon.now_stamp`` 的口径）。
    """
    text = str(stamp).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            return parsed.replace(hour=23, minute=59, second=59)
        return parsed
    raise ValueError(f"作业时刻需为 YYYY-MM-DD[ HH:MM:SS]，收到 {text!r}")


def data_date_for(conn, market, stamp):
    """本次作业负责的**市场本地会话日期**（规格 §4.2 数据就绪门）。

    作业按北京时间触发，bars 与交易日历按市场本地日期落库——美股链两者相差一天
    （ET 会话收盘 = 北京次日 04:00/05:00）。北京日直接当本地日用会让美股链的
    「应有最后交易日」永远超前一天，数据就绪门每天静默跳过（实现期发现，2026-09-16）。
    本函数把北京时刻折算到该市场本次作业负责的会话本地日：

    1. **先解析会话本地日**：候选本地日 = 北京日 − 天数偏移，取日历中 ≤ 候选日的
       **最近交易日**（周末/节假日回落到上一场——北京周一早上的补跑因此落到上周五，
       正是美股链需要的语义）；日历未同步 → ``RuntimeError``；
    2. **再用该日（回落后的交易日）的真实收盘判「是否已收盘」**：尚未收盘 → 返回
       ``None``（调用方软跳过，宁可不生成计划，也不把上一场的收盘当本场）。

    第 2 步的收盘口径分两路（半日市纳入判定，实现期修订 2026-09-18）：

    * **全天行**（``trade_second`` 缺失 / 等于该市场全天秒数）→ **逐字沿用**
      ``SESSION_CLOSE_BEIJING`` 表。该表对美股取 EST 最晚界 05:00（EDT 实为 04:00），
      是**刻意偏保守**的 DST 无关口径；换成按 zoneinfo 精算的真实收盘会让 EDT 期间
      04:00–05:00 的补跑提前放行，属行为变更，故不动；
    * **半日/提前收盘行**（``trade_second`` 小于全天秒数）→ 由 ``sessions`` 算出的
      **真实收盘**（开盘 + trade_second，单段口径：HK 半日 12:00、US 半日 13:00）折算成
      北京时间后比较。旧口径按全天界判定，会把已收盘的半日市误判成「尚未收盘」而
      整天不产出计划。

    DST 无关性对全天行依旧成立（只依赖「收盘落在北京次日」这一事实，不做制度换算）；
    ``trade_second`` 的非正整数按缺失处理（回落全天口径，见 ``sessions.is_short_day``）。
    """
    market = str(market).upper()
    close = SESSION_CLOSE_BEIJING.get(market)
    if close is None:
        raise KeyError(f"未知市场链：{market}")
    offset, hhmm = close
    when = _stamp_parts(stamp)
    # 会话日先解析（回落最近交易日），再判是否已收盘——顺序是修订要点：判定要用
    # **该日**的 trade_second，而不是「北京日」的。
    candidate = (when.date() - timedelta(days=offset)).isoformat()
    calendar_market = CALENDAR_MARKET.get(market, market)  # SZ/BJ 用 SH 日历
    start = (date.fromisoformat(candidate)
             - timedelta(days=READINESS_LOOKBACK_DAYS)).isoformat()
    days = store.trading_days(conn, calendar_market, start, candidate)
    if not days:
        raise RuntimeError(f"日历无交易日：{calendar_market} {start}..{candidate}")
    local_day = days[-1]
    row = store.calendar_row(conn, calendar_market, local_day) or {}
    trade_second = row.get("trade_second")
    if sessions.is_short_day(market, trade_second):
        if when < sessions.session_close_beijing(market, local_day, trade_second):
            return None
        return local_day
    hour, minute = (int(part) for part in hhmm.split(":"))
    # close_at 落在 stamp 当日：表的天数偏移已把「会话本地日 → 北京日」折算进去
    # （US：候选日 + 1 = 北京日），因此这里只替换时刻、不挪日期。
    if when < when.replace(hour=hour, minute=minute, second=0, microsecond=0):
        return None
    return local_day


def observation_date(conn, market, stamp):
    """本次作业负责的**会话本地观测日** → ``(date | None, date_source)``。

    ``None`` = 本次负责的会话尚未收盘（调用方按软跳过处理，规格 §4.2）。
    ``date_source`` 三值：``session``（正常折算）/ ``session-open``（未收盘）/
    ``beijing-fallback``（日历未同步或市场缺表 → 退化北京日，调用方**必须**为此发
    info 告警：口径退化要可见，不能静默改语义）。

    与 ``data_date_for`` 的关系：后者是「折算 + 拿最近交易日」的唯一实现，本函数只是把
    它的三种结局整理成调用方需要的二元组——采集链（sentiment/research_sync）与值班
    研究员入队（research_queue）共用同一处，避免各写一份导致口径漂移。
    """
    try:
        local = data_date_for(conn, market, stamp)
    except (RuntimeError, KeyError):
        return str(stamp)[:10], "beijing-fallback"
    if local is None:
        return None, "session-open"
    return local, "session"


def recent_trading_days(conn, market, today, window=1):
    """最近 window 个交易日（≤ today，降序，最近在前）。

    日历按**市场本地日期**落库（quote_trading_days 口径），作业按北京时间触发：
    同一个「最近已收盘交易日」在两种日期空间解释下可能相差一天，容差由 window 决定——
    plan_auto（数据就绪门）取 1，auto_execute（自动执行守卫）取 2。
    日历未同步 → RuntimeError（调用方按「宁可不执行」处理）。
    """
    start = (date.fromisoformat(today) - timedelta(days=READINESS_LOOKBACK_DAYS)).isoformat()
    days = store.trading_days(conn, market, start, today)
    if not days:
        raise RuntimeError(f"日历无交易日：{market} {start}..{today}")
    return list(reversed(days[-window:]))


def _resolve_strategy(conn, name):
    """按名字取策略实例，返回 ``(instance | None, error | None)``。

    两类名字，**待遇不同**（WP14 任务 6 e2e 修正后的口径）：

    1. **内置策略**（``watchlist_rsi`` 等，模块级 ``@strategy`` 注册）：注册表命中即用；
    2. **规则名**（``strategies.register_rule`` 动态注册，见 ``strategies.is_rule``）：
       **每次都回查 DB 状态**，进程内实例不作数——``candidate``/``passed``/``failed``/
       ``disabled`` 一律不得被消费，只有 ``enabled``（经 ``decide_rule`` 记录批准人）
       才注册进注册表。

    为什么规则不能只信注册表：旧实现把 REGISTRY 当一级事实来源，规则被解析过一次就
    常驻进程内；此后用户在 Web 停用该规则，长驻服务进程的下一次计划生成仍会命中陈旧
    实例继续下单——``disabled`` 是终态，等于「停用随时可停」在进程内失效（fail-open）。
    状态不再匹配时由 ``strategies.unregister_rule`` 立刻摘除实例。

    规则加载失败（spec 被改坏/因子被摘）按 fail-closed 返回 error，不做静默回退。
    """
    from . import strategies  # 模块级 import 在 plan_auto 内是惰性的，这里自带一份
    instance = strategies.REGISTRY.get(name)
    if instance is not None and not strategies.is_rule(name):
        return instance, None  # 内置策略：注册表是权威
    row = store.find_rule(conn, name)
    if row is None:
        if instance is not None:
            # 动态注册过但库内无记录：没有批准痕迹 → 不得消费（fail-closed）
            strategies.unregister_rule(name)
            return None, f"规则未启用：{name}（库内无该规则记录，需人工批准后启用）"
        return None, None  # 不是规则名：交由调用方按「策略未注册」处理
    if row["status"] != "enabled":
        strategies.unregister_rule(name)  # 停用/状态变化：立刻摘掉进程内实例
        return None, f"规则未启用：{name}（当前 {row['status']}，需人工批准后启用）"
    try:
        return strategies.register_rule(row["spec"]), None
    except ValueError as error:
        strategies.unregister_rule(name)
        return None, f"规则加载失败：{name}（{str(error)[:100]}）"


def plan_auto(conn, home, market, today=None, broker_call=None):
    """build_plan 作业体（规格 §4.2）：数据就绪 → 策略权重 → 冻结 auto 计划。

    返回契约（**永不抛**——作业失败不拖垮调度链，原因以信封返回并落告警）::

      {"ok": True,  "skipped": <原因>}                    软跳过，不产生计划
      {"ok": False, "error": <原因>}                      配置/模式非法（fail-closed）
      {"ok": True,  "plan": {...}, "expired": [...],
       "no_price": [...], "no_atr": [...], "equity": <float>,
       "converge": {...}}                                 成功冻结

    ``no_atr`` = 因算不出 ATR（止损距离）而未定量的标的（planner 的 ``skipped`` 原样
    透出）——它们既不产单也不静默：运维据此判断是数据缺口还是标的本身不可用。

    **目标外清出（规格 §4.2 第 6 点修订，2026-09-17）**：``~/.dsh/trading-platform.json``
    顶层开关 ``exit_outside_target``（默认 False）打开时，本次计划把「券商持仓 − 当日
    ``target`` 的键」一并按目标权重 0 处理（清出），让账户收敛到策略组合。``converge``
    如实回告这次收敛的四个事实::

        {"enabled": bool,            # 开关值（缺失=false）
         "symbols": [...],           # 收敛集合（排序；target 为空时为空）
         "prices": {sym: {"price": <float>, "source": "close"|"broker_mark",
                          "field": <券商字段名|None>}},
         "skipped": [...],           # "SYM(无价,无法清出)" / "SYM(T+N不可卖)"
         "empty_target": bool}       # 因 target 为空而被硬守卫拦下

    口径与边界（详见 ``build_and_freeze`` 的 ``exit_symbols`` 段）：

    * **只在自动路径生效**：手工 ``plan-build`` 不经本函数，语义不变；
    * **target 为空一律不收敛**（硬守卫）并发 warn「目标为空，未执行目标外清出」——
      「策略今日选不出标的」绝不能变成「清空全部持仓」；
    * 价格：本地最近收盘优先；拿不到则用券商持仓**标记价**（sim ``cur_price`` / live
      ``nominal_price``），来源在 ``converge.prices`` 里如实标注（**绝不伪装成本地收盘**）；
      两处都拿不到 → 记 ``skipped`` 且**不生成订单**（不猜价）；
    * 可卖数量：券商给了可用数量（sim ``qty_avbl`` / live ``can_sell_qty``，缺字段时按
      既有口径用全部 qty）→ 卖出量取 ``min(qty, available)``；``available=0`` 记
      ``skipped``，**不生成注定被拒的单**（T+N 持仓当日不可卖）；
    * 开关值不是真布尔 → **fail-closed 软跳过** + warn（作业契约「永不抛」）；关闭/缺失时
      除多读一次平台配置外逐字不改变既有行为。

    软跳过的告警分级：关闭功能=静默（默认态不是异常）；其余原因=info/warn
    （warn 用于「本可运行但条件不满足」：数据未就绪/日历缺失/券商不可用）。

    **日期空间（实现期修订，2026-09-16）**：``today`` 既接受完整时刻也接受纯日期
    （纯日期 = 当日已过完）。数据就绪门与下游一律用 ``data_date_for`` 折算出的
    **市场本地会话日**（美股 = 北京日前一天），不使用北京日——北京日只用于
    「休市不生成」判定与告警文案。
    """
    from . import alerts
    from . import broker as core_broker
    from . import daemon, quality, strategies

    home = str(home)
    market = str(market).upper()
    try:
        # 时钟口径唯一实现在 daemon（显式 today > DSH_FAKE_NOW > 真实时间）；
        # 非法假时钟 fail-closed，不静默回落——否则演练会按真实日期生成计划。
        stamp = today or daemon.now_stamp()
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    beijing_today = str(stamp)[:10]  # 北京日：只用于休市判定与告警文案（见 docstring）

    def skip(reason, level=None, title=None):
        if level:
            alerts.emit(conn, home=home, level=level, title=title or reason[:40],
                        detail=reason[:160])
        return {"ok": True, "skipped": reason}

    if market not in CHAIN_MARKETS:
        return {"ok": False, "error": f"未知市场链：{market}（应为 {'/'.join(CHAIN_MARKETS)}）"}

    try:
        cfg = daemon.auto_pipeline_config(home)
    except ValueError as error:
        reason = f"auto_pipeline 配置非法：{error}"
        alerts.emit(conn, home=home, level="warn", title="auto_pipeline 配置非法",
                    detail=reason[:160])
        return {"ok": False, "error": reason}

    if not cfg["enabled"]:
        return skip("auto_pipeline 未启用")  # 静默：关闭是默认态，不是故障

    entry = next((item for item in cfg["strategies"] if item.get("market") == market), None)
    if entry is None:
        return skip(f"auto_pipeline 无匹配策略：market={market}", "info", "自动计划无策略")

    try:
        mode = read_mode(home)
    except ValueError as error:
        return {"ok": False, "error": str(error)}

    try:
        is_trading_day = store.is_trading_day(conn, market, beijing_today)
    except RuntimeError as error:
        return skip(f"日历未同步：{error}", "warn", "日历未同步")
    if not is_trading_day:
        return skip(f"{beijing_today} 非 {market} 交易日")  # 休市不是故障：不告警

    # 数据就绪门按**市场本地会话日**判定（北京日 ≠ 美股会话日，见 data_date_for）。
    # 本次负责的会话尚未收盘 → info 软跳过（保守：宁可当日不生成计划，也不拿上一场
    # 的收盘当本场，否则会把当日 kv ran 标记消耗掉、真到收盘后不再补跑）。
    try:
        data_date = data_date_for(conn, market, stamp)
    except RuntimeError as error:
        return skip(f"日历未同步：{error}", "warn", "日历未同步")
    except ValueError as error:
        # 注入口径非法（CLI --today 是人工输入）：fail-closed 非零退出，与上面
        # DSH_FAKE_NOW 非法同一分级——作业契约「永不抛」仍然成立。
        return {"ok": False, "error": str(error)}
    if data_date is None:
        return skip(f"{market} 本次负责的会话尚未收盘（北京 {stamp}）", "info", "会话未收盘")

    symbols = symbols_for_market(home, market)
    if not symbols:
        return skip(f"关注池为空：market={market}", "warn", "关注池为空")
    stale = [s for s in symbols
             if quality.freshness(conn, s, "1d", data_date)["last"] != data_date]
    if stale:
        return skip(f"数据未就绪（应有最后交易日 {data_date}）：{','.join(stale)}",
                    "warn", "数据未就绪")

    strategy, error = _resolve_strategy(conn, entry["strategy"])
    if strategy is None:
        if error:
            return skip(error, "info", "规则未启用")
        return skip(f"策略未注册：{entry['strategy']}", "warn", "策略未注册")

    # 过期语义：先作废跨日的 auto 计划，再生成当日计划（只动 auto，手工计划不碰）
    expired = store.cancel_stale_auto_plans(conn, data_date)

    # 组合策略需要 home/market/池键，单标的策略没有这些参数——按签名显式分派，不靠猜。
    # market 必须传（K1）：策略的分母与 max_positions 截断都在**市场过滤之后**，
    # 跨市场合并计数会让先排序的市场吃光名额（实测美股恒为 0）。
    def _strategy_kwargs(fn):
        params = inspect.signature(fn).parameters
        kwargs = {}
        if "home" in params:
            kwargs["home"] = home
        if "market" in params:
            kwargs["market"] = market
        if "watchlist" in params:
            kwargs["watchlist"] = entry["watchlist"]
        return kwargs

    try:
        weights = strategy.target_weights(conn, data_date,
                                         **_strategy_kwargs(strategy.target_weights))
    except ValueError as error:
        # 组合策略要读风控配置（权重上限）与命名池（键不存在即配置错误）；
        # trading-risk.json 非法时 risk_config 抛 ValueError——作业契约是「永不抛」，
        # 这里按 fail-closed 软跳过并告警（静默回退默认值会掩盖配置错误：
        # 配置非法时宁可当日不生成计划）
        return skip(f"策略权重计算失败：{str(error)[:120]}", "warn", "策略权重失败")
    target = {s: w for s, w in (weights or {}).items()
              if CALENDAR_MARKET.get(str(s).split(".", 1)[0]) == market}

    # 受管集合（规格 §4.2 第 6 点）= 该市场关注池 ∩ 策略 universe。
    # 策略 universe 为空（单标的策略、或策略不声明负责范围）→ managed=None：退化为
    # 只遍历 target（与既有行为一致），结果标注 managed="target-only" 供运维分辨。
    # universe 读取失败按 fail-closed 软跳过：宁可不生成计划，也不假装「无受管标的」
    # （后者会让已持仓标的继续逃过 diff，正是本修订要消除的缺口）。
    try:
        universe = strategy.universe(conn, data_date, **_strategy_kwargs(strategy.universe))
    except Exception as error:  # noqa: BLE001 —— 作业契约「永不抛」
        return skip(f"策略 universe 读取失败：{str(error)[:120]}", "warn", "策略 universe 失败")
    universe_set = {str(s).strip().upper() for s in (universe or []) if str(s).strip()}
    managed = [s for s in symbols if s in universe_set] if universe_set else None

    # 目标外持仓自动清出开关（规格 §4.2 第 6 点修订，2026-09-17）：默认关闭。读取放在这里
    # （策略已解析、券商查询之前）——关闭态下除了多读一次平台配置，逐字不改变任何行为；
    # 非法值 fail-closed 软跳过 + 告警（与上面 risk_config 的 ValueError 同一分级：
    # 配置非法时宁可当日不生成计划，也绝不静默按默认值跑）。
    try:
        exit_enabled = exit_outside_target_enabled(daemon.platform_config(home))
    except ValueError as error:
        return skip(f"{EXIT_OUTSIDE_TARGET_CONFIG_KEY} 配置非法：{error}", "warn",
                    EXIT_CONFIG_ALERT_TITLE)

    if broker_call is None:
        # WP13 任务 3：默认通道走 channel 分派（``futu_channel: openapi`` 且凭据就绪 →
        # REST；否则回退 mcp）——与执行链 ``daemon._default_executor`` **同一口径**。
        # 曾经此处硬编码 ``futu_mcp.call_tool``：openapi 通道下「下单执行走 REST、
        # 取持仓算权重走 MCP」的通道分裂由本任务实测暴露，在此收口。
        # ``core_broker.sim_call`` 只接管模拟交易 7 个工具，live 工具（account_*/
        # trading_*）原样交给 MCP，故 live 行为逐字不变。
        try:
            broker_call = core_broker.sim_call(home)
        except Exception as error:  # noqa: BLE001 —— 导入失败即通道不可用
            return skip(f"券商通道不可用：{str(error)[:120]}", "warn", "券商通道不可用")

    prices, no_price = {}, []
    for symbol in target:
        px = daemon._last_close(conn, symbol, data_date)  # PIT 最近收盘，不盘中取数
        if px:
            prices[symbol] = px
        else:
            no_price.append(symbol)

    try:
        positions, equity, cash = core_broker.positions_equity_cash(
            broker_call, mode=mode, market=market, with_marks=exit_enabled)
    except core_broker.EquityUnavailable as error:
        # 权益口径问题（官方字段缺失/非正）与通道故障分列：告警文案不得互相冒名
        return skip(f"券商权益不可用（缺失或非正）：{str(error)[:120]}", "warn", "权益不可用")
    except Exception as error:  # noqa: BLE001 —— 通道失败即跳过（不用本地台账）
        return skip(f"券商通道不可用：{str(error)[:120]}", "warn", "券商通道不可用")

    if not equity or equity <= 0:
        # 权益缺失若当 0 处理，目标数量全变 0 = 凭空生成清仓单；如实跳过
        return skip("券商权益不可用（缺失或非正）", "warn", "权益不可用")

    # 可用现金（2026-09-17 实机修订）：权重/风险预算只看权益，不看现金——本机 A 股模拟
    # 账户「权益 81 万 / 现金 5.5 万 / 已持 8 只」时买单必被券商资金不足拒。现金与权益
    # 同源于一次账户查询（`positions_equity_cash`，不重复查账户列表）；取不到就不生成
    # 买单（**绝不用权益冒充现金**），卖单与清仓照常。
    warnings = []
    if cash is None:
        alerts.emit(conn, home=home, level="warn", title="计划预警：现金不可得",
                    detail=f"{market} 券商可用现金字段缺失：本次不生成买单"
                           "（卖单与清仓不受影响）")
    # 结构性预警：持仓数已达上限时新增建仓注定被规则 6 拦——计划期就说清，别让页面显示
    # 「已完成」而订单全被拦（实机：已持 8 只 > max_positions=5）。
    max_positions = int(daemon.risk_config(home).get("max_positions") or 0)
    new_symbols = [s for s in target if (positions.get(s, {}).get("qty") or 0) == 0]
    if max_positions and len(positions) >= max_positions and new_symbols:
        warnings.append(f"持仓 {len(positions)} 只 ≥ 上限 {max_positions}："
                        f"新增建仓将被规则 6 拦（{len(new_symbols)} 只）")
        alerts.emit(conn, home=home, level="warn", title="计划预警：持仓数超限",
                    detail=f"{market} 持仓 {len(positions)} ≥ max_positions={max_positions}，"
                           f"计划含 {len(new_symbols)} 只新建仓，执行时会被规则 6 拒绝")

    # 受管集合缺席者若券商实际持有 → 要生成清仓单 → 需要价格。只为**实际持有**的缺席
    # 标的补价：无持仓的标的既不需要价格，也不该污染 no_price（那是数据缺口的清单）。
    for symbol in (managed or ()):
        if symbol in prices or (positions.get(symbol, {}).get("qty") or 0) == 0:
            continue
        px = daemon._last_close(conn, symbol, data_date)
        if px:
            prices[symbol] = px
        else:
            no_price.append(symbol)

    # ---- 目标外清出（规格 §4.2 第 6 点修订，2026-09-17）----
    # 收敛集合 = {券商持仓} − {当日策略 target 的键}：受管集合**之外**的存量持仓只有这条
    # 路径能进 diff（managed 机制刻意不碰它们），不修就永远是「策略不买也不卖」的死锁。
    # **硬守卫：target 为空一律不收敛**——「策略今日选不出标的」与「清空全部持仓」是两件
    # 事，后者不是任何人的意图；此时发 warn 说明「目标为空，未执行目标外清出」。
    exits, exit_price_meta, empty_target = [], {}, False
    if exit_enabled:
        if not target:
            empty_target = True
            alerts.emit(conn, home=home, level="warn", title=EXIT_EMPTY_TARGET_ALERT_TITLE,
                        detail=f"{market} 策略目标为空：未执行目标外清出（券商持仓 "
                               f"{len(positions)} 只保持不变）")
        else:
            exits = sorted(s for s, row in positions.items()
                           if (row.get("qty") or 0) > 0 and s not in target)
            for symbol in exits:
                if symbol in prices:
                    # 受管集合补价时已经取到本地收盘（exit ∩ managed 的重叠标的）：复用，
                    # 不重复查库、也不让它二次进 no_price 清单。
                    exit_price_meta[symbol] = {"price": prices[symbol], "source": "close",
                                               "field": None}
                    continue
                # 价格：本地最近收盘优先（与 target 同一 PIT 口径）→ 券商标记价回退。
                # 来源**如实标注**，绝不把券商标记价伪装成本地收盘价。
                px = daemon._last_close(conn, symbol, data_date)
                if px:
                    exit_price_meta[symbol] = {"price": px, "source": "close", "field": None}
                    prices[symbol] = px
                    continue
                row = positions.get(symbol) or {}
                mark = row.get("price")
                if mark:
                    exit_price_meta[symbol] = {"price": mark, "source": "broker_mark",
                                               "field": row.get("mark_field")}
                    prices[symbol] = mark
                    continue
                # 两处都拿不到 → 真价格缺口（如实进 no_price），清出集合仍下传，由
                # build_and_freeze 统一记 skipped（"SYM(无价,无法清出)"）——不猜价、不生成假单
                if symbol not in no_price:  # 去重：managed 补价失败时可能已记过一次
                    no_price.append(symbol)

    plan = build_and_freeze(conn, mode=mode, strategy_id=entry["strategy"], target=target,
                            broker_positions=lambda _mode: (positions, equity),
                            prices=prices, as_of=data_date, origin="auto", market=market,
                            risk_config=daemon.risk_config(home), managed=managed,
                            broker_cash=lambda _mode: cash,
                            exit_symbols=exits if exit_enabled else None)
    # 计划期现金结局如实回告（买了多少、被现金压低哪些）
    cash_info = plan.get("cash") or {}
    if cash_info.get("capped"):
        warnings.append(f"现金封顶：{len(cash_info['capped'])} 只买入量被可用现金压低"
                        f"（{'、'.join(cash_info['capped'][:5])}）")
        alerts.emit(conn, home=home, level="warn", title="计划预警：现金封顶",
                    detail=f"{market} 可用现金 {cash}：{','.join(cash_info['capped'])[:120]}"
                           " 的买入量按现金封顶")

    # 清出结局如实回告：清出集合、价格来源（本地收盘 / 券商标记价分别几只）、以及**没有**
    # 变成订单的标的与原因（无价 / T+N 不可卖）。一条 warn，标题是稳定字面量，变量只进 detail
    # ——页面与运维据此分辨「真的清了」与「想清但清不动」。
    converge = {"enabled": bool(exit_enabled), "symbols": list(exits), "prices": exit_price_meta,
                "skipped": [], "empty_target": empty_target}
    if exit_enabled and exits:
        exit_set = set(exits)
        exit_skips = [row for row in (plan.get("skipped") or [])
                      if str(row).split("(", 1)[0] in exit_set]
        converge["skipped"] = exit_skips
        marked = sorted(s for s, meta in exit_price_meta.items()
                        if meta["source"] == "broker_mark")
        warnings.append(f"目标外清出 {len(exits)} 只（券商标记价 {len(marked)} 只、"
                        f"跳过 {len(exit_skips)} 只）")
        detail = (f"{market} 目标外持仓 {len(exits)} 只清出；价格来源：本地收盘 "
                  f"{len(exit_price_meta) - len(marked)} 只、券商持仓标记价 {len(marked)} 只")
        if marked:
            detail += "（" + "、".join(
                f"{s}@{exit_price_meta[s]['field'] or 'mark'}" for s in marked[:5]) + "）"
        if exit_skips:
            detail += "；未生成订单：" + "、".join(exit_skips[:5])
        alerts.emit(conn, home=home, level="warn", title=EXIT_ALERT_TITLE,
                    detail=detail[:300])
    if not plan["orders"]:
        # 「没做成」不能显示成「已完成」：零订单计划在流程页按跳过口径呈现
        alerts.emit(conn, home=home, level="warn", title="计划跳过：无可执行订单",
                    detail=f"{market} 计划 {plan['plan_id']} 无订单："
                           + ("；".join(warnings)[:120] or "策略目标与当前持仓一致"))
    return {"ok": True, "plan": plan, "expired": expired, "no_price": no_price,
            "no_atr": list(plan.get("skipped") or []), "equity": equity,
            "cash": cash, "warnings": warnings,
            "watchlist": len(symbols),
            "managed": len(managed) if managed is not None else "target-only",
            "converge": converge}
