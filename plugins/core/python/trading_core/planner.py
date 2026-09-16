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


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def _risk_defaults():
    """风控默认值（镜像唯一源 ``daemon.RISK_DEFAULTS``；惰性导入避免模块级环依赖）。"""
    from . import daemon
    return dict(daemon.RISK_DEFAULTS)


def build_and_freeze(conn, mode, strategy_id, target, broker_positions, prices,
                     as_of, lot=100, origin="manual", market=None, risk_config=None):
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
    for symbol, weight in target.items():
        px = prices.get(symbol)
        if not px:
            continue  # 无价（停牌/无行情）：跳过并在审计可见，不猜价
        want_qty = int(equity * weight / px // lot * lot)
        have_qty = positions.get(symbol, {}).get("qty", 0)
        delta = want_qty - have_qty
        if delta == 0:
            continue
        if delta > 0:  # 加仓：受单笔风险预算约束（减仓不受限，见 docstring）
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

    关注池可能是字符串（逗号分隔）——字符串按字符迭代会静默产出垃圾标的，显式切开。
    """
    from . import daemon
    watchlist = daemon.platform_config(home).get("watchlist") or []
    if isinstance(watchlist, str):
        watchlist = watchlist.split(",")
    out = []
    for raw in watchlist:
        symbol = str(raw).strip().upper()
        if symbol and CALENDAR_MARKET.get(symbol.split(".", 1)[0]) == market:
            out.append(symbol)
    return out


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
    """
    from . import alerts
    from . import broker as core_broker
    from . import daemon, quality, strategies

    home = str(home)
    market = str(market).upper()
    try:
        # 时钟口径唯一实现在 daemon（显式 today > DSH_FAKE_NOW > 真实时间）；
        # 非法假时钟 fail-closed，不静默回落——否则演练会按真实日期生成计划。
        today = today or daemon.now_stamp()[:10]
    except ValueError as error:
        return {"ok": False, "error": str(error)}

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
        is_trading_day = store.is_trading_day(conn, market, today)
    except RuntimeError as error:
        return skip(f"日历未同步：{error}", "warn", "日历未同步")
    if not is_trading_day:
        return skip(f"{today} 非 {market} 交易日")  # 休市不是故障：不告警

    # 数据就绪门期望的「最近已收盘交易日」：与 auto_execute 共用同一 helper
    # （窗口 1 = 交易日维度最近一天，不含日期空间容差）
    try:
        expected = recent_trading_days(conn, market, today, window=1)[0]
    except RuntimeError as error:
        return skip(f"日历未同步：{error}", "warn", "日历未同步")

    symbols = symbols_for_market(home, market)
    if not symbols:
        return skip(f"关注池为空：market={market}", "warn", "关注池为空")
    stale = [s for s in symbols
             if quality.freshness(conn, s, "1d", today)["last"] != expected]
    if stale:
        return skip(f"数据未就绪（应有最后交易日 {expected}）：{','.join(stale)}",
                    "warn", "数据未就绪")

    strategy = strategies.REGISTRY.get(entry["strategy"])
    if strategy is None:
        return skip(f"策略未注册：{entry['strategy']}", "warn", "策略未注册")

    # 过期语义：先作废跨日的 auto 计划，再生成当日计划（只动 auto，手工计划不碰）
    expired = store.cancel_stale_auto_plans(conn, today)

    # 组合策略需要 home 读配置，单标的策略没有该参数——按签名显式分派，不靠猜
    params = inspect.signature(strategy.target_weights).parameters
    try:
        weights = strategy.target_weights(conn, today, **({"home": home} if "home" in params else {}))
    except ValueError as error:
        # 组合策略要读风控配置（权重上限）；trading-risk.json 非法时 risk_config 抛
        # ValueError——作业契约是「永不抛」，这里按 fail-closed 软跳过并告警
        # （静默回退默认值会掩盖配置错误：配置非法时宁可当日不生成计划）
        return skip(f"策略权重计算失败：{str(error)[:120]}", "warn", "策略权重失败")
    target = {s: w for s, w in (weights or {}).items()
              if CALENDAR_MARKET.get(str(s).split(".", 1)[0]) == market}

    if broker_call is None:
        try:
            from trading_datasource.futu_mcp import call_tool
            broker_call = call_tool
        except Exception as error:  # noqa: BLE001 —— 导入失败即通道不可用
            return skip(f"券商通道不可用：{str(error)[:120]}", "warn", "券商通道不可用")

    prices, no_price = {}, []
    for symbol in target:
        px = daemon._last_close(conn, symbol, today)  # PIT 最近收盘，不盘中取数
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

    plan = build_and_freeze(conn, mode=mode, strategy_id=entry["strategy"], target=target,
                            broker_positions=lambda _mode: (positions, equity),
                            prices=prices, as_of=today, origin="auto", market=market,
                            risk_config=daemon.risk_config(home))
    return {"ok": True, "plan": plan, "expired": expired, "no_price": no_price,
            "no_atr": list(plan.get("skipped") or []), "equity": equity,
            "watchlist": len(symbols)}
