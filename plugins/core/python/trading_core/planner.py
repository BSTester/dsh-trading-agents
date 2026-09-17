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

from . import indicators, oms, store

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


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def _risk_defaults():
    """风控默认值（镜像唯一源 ``daemon.RISK_DEFAULTS``；惰性导入避免模块级环依赖）。"""
    from . import daemon
    return dict(daemon.RISK_DEFAULTS)


def build_and_freeze(conn, mode, strategy_id, target, broker_positions, prices,
                     as_of, lot=100, origin="manual", market=None, risk_config=None,
                     managed=None):
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

    ``risk_config`` 缺省用 ``daemon.RISK_DEFAULTS``；``plan_auto`` 传
    ``daemon.risk_config(home)``（含 ``~/.dsh/trading-risk.json`` 覆盖）。部分字段的
    覆盖字典按「缺省补默认」合并——**不重写配置读取实现**。
    """
    positions, equity = broker_positions(mode)
    cfg = _risk_defaults()
    if risk_config:
        cfg.update(risk_config)
    orders, skipped = [], []
    plan_id = f"PLN-{as_of.replace('-', '')}-{mode}-{uuid.uuid4().hex[:4].upper()}"
    # target 原序在前、managed 缺席者按代码升序在后：managed=None 时与既有顺序逐字一致
    symbols = list(target) + [s for s in sorted(set(managed or ())) if s not in target]
    for symbol in symbols:
        px = prices.get(symbol)
        if not px:
            continue  # 无价（停牌/无行情）：跳过并在审计可见，不猜价
        want_qty = int(equity * target[symbol] / px // lot * lot) if symbol in target else 0
        have_qty = positions.get(symbol, {}).get("qty", 0)
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
    return {"plan_id": plan_id, "as_of": as_of, "mode": mode, "status": "frozen",
            "orders": orders, "content_hash": content_hash, "skipped": skipped}


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

    1. 时刻早于该会话收盘（``SESSION_CLOSE_BEIJING`` 的北京时间）→ 返回 ``None``：
       本次负责的会话尚未收盘，宁可不生成计划，也不把上一场的收盘当本场；
    2. 候选本地日 = 北京日 − 天数偏移；
    3. 取日历中 ≤ 候选日的**最近交易日**（周末/节假日回落到上一场——北京周一早上的
       补跑因此落到上周五，正是美股链需要的语义）。

    DST 无关：只依赖「收盘落在北京次日」这一事实（US 界 05:00 在 EDT/EST 两制下
    都已收盘），不做制度换算。日历未同步 → ``RuntimeError``（调用方按「宁可不跑」处理）。
    """
    market = str(market).upper()
    close = SESSION_CLOSE_BEIJING.get(market)
    if close is None:
        raise KeyError(f"未知市场链：{market}")
    offset, hhmm = close
    when = _stamp_parts(stamp)
    hour, minute = (int(part) for part in hhmm.split(":"))
    # close_at 落在 stamp 当日：表的天数偏移已把「会话本地日 → 北京日」折算进去
    # （US：候选日 + 1 = 北京日），因此这里只替换时刻、不挪日期。
    if when < when.replace(hour=hour, minute=minute, second=0, microsecond=0):
        return None
    candidate = (when.date() - timedelta(days=offset)).isoformat()
    calendar_market = CALENDAR_MARKET.get(market, market)  # SZ/BJ 用 SH 日历
    start = (date.fromisoformat(candidate)
             - timedelta(days=READINESS_LOOKBACK_DAYS)).isoformat()
    days = store.trading_days(conn, calendar_market, start, candidate)
    if not days:
        raise RuntimeError(f"日历无交易日：{calendar_market} {start}..{candidate}")
    return days[-1]


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


def plan_auto(conn, home, market, today=None, broker_call=None):
    """build_plan 作业体（规格 §4.2）：数据就绪 → 策略权重 → 冻结 auto 计划。

    返回契约（**永不抛**——作业失败不拖垮调度链，原因以信封返回并落告警）::

      {"ok": True,  "skipped": <原因>}                    软跳过，不产生计划
      {"ok": False, "error": <原因>}                      配置/模式非法（fail-closed）
      {"ok": True,  "plan": {...}, "expired": [...],
       "no_price": [...], "no_atr": [...], "equity": <float>}   成功冻结

    ``no_atr`` = 因算不出 ATR（止损距离）而未定量的标的（planner 的 ``skipped`` 原样
    透出）——它们既不产单也不静默：运维据此判断是数据缺口还是标的本身不可用。

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

    strategy = strategies.REGISTRY.get(entry["strategy"])
    if strategy is None:
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
        positions, equity = core_broker.positions_and_equity(broker_call, mode=mode,
                                                             market=market)
    except core_broker.EquityUnavailable as error:
        # 权益口径问题（官方字段缺失/非正）与通道故障分列：告警文案不得互相冒名
        return skip(f"券商权益不可用（缺失或非正）：{str(error)[:120]}", "warn", "权益不可用")
    except Exception as error:  # noqa: BLE001 —— 通道失败即跳过（不用本地台账）
        return skip(f"券商通道不可用：{str(error)[:120]}", "warn", "券商通道不可用")

    if not equity or equity <= 0:
        # 权益缺失若当 0 处理，目标数量全变 0 = 凭空生成清仓单；如实跳过
        return skip("券商权益不可用（缺失或非正）", "warn", "权益不可用")

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

    plan = build_and_freeze(conn, mode=mode, strategy_id=entry["strategy"], target=target,
                            broker_positions=lambda _mode: (positions, equity),
                            prices=prices, as_of=data_date, origin="auto", market=market,
                            risk_config=daemon.risk_config(home), managed=managed)
    return {"ok": True, "plan": plan, "expired": expired, "no_price": no_price,
            "no_atr": list(plan.get("skipped") or []), "equity": equity,
            "watchlist": len(symbols),
            "managed": len(managed) if managed is not None else "target-only"}
