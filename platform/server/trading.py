# WP7 任务 3：交易闸门 + 受约束交易工具（规格 §二-3 / §三-2）。
#
# 富途写路径唯一在工作台服务（WP7 决策）：下单/改单/撤单必须过**完整闸门链**——
# 模式文件 → 风控 8 规则（risk.pre_trade_checks，kill 是其中规则 1）→ 业务确认
# （store_access.request_confirmation，进程内阻塞、Web 卡片作答）→ broker。
# 账户查询（positions/orders/funds）只受模式约束，直通 broker。
#
# 依赖口径（全部复用既有实现，不另写富途协议、不另写风控规则）：
#   * broker 适配     → trading_core.broker（WP3 锁定的 sim_trade_* 工具名与参数）
#   * 风控            → trading_core.risk.pre_trade_checks（8 规则原样调用）
#   * OMS             → trading_core.oms.transition 状态机 + orders/risk_checks 表
#   * 业务确认        → server.store_access 确认三方法（进程内，TTL 超时=拒绝）
#   * 富途通道        → trading_datasource.futu_mcp.call_tool（惰性导入）
#
# WP8 任务 3（交易链路统一至富途 OpenAPI）：在**不改闸门链**的前提下给写路径接上 REST
# 交易面（trading_datasource.futu_openapi.OpenApiTrade，13 方法）：
#
#   * 通道路由：``trading-platform.json`` 的 ``futu_channel``（**默认 mcp**）为
#     ``openapi`` 且 OpenAPI 凭据可用时，live 写与 live 账户查询走 REST；否则
#     完全沿用 WP7 行为（live 写先置拒绝 trading/broker-unavailable、sim 走既有
#     sim_trade_* 工具）。默认（无凭据）行为零变化是硬约束（不变式 2）。
#   * 两层确认合一：live 下**闸门内的业务确认（Web 卡片）是唯一人工批准**；券商侧
#     ``need_order_confirm`` 由 broker 在人工批准之后**自动**调 order_confirm 完成
#     （用户已批准本单参数，不再二次打扰）。confirm 失败一律落 ``unknown``：
#     订单在券商侧已挂起、confirm 结果不确定，铁律「先查询不重放」——**绝不重发下单**。
#     本模块**不新增模型可调用的 trade_confirm 工具**（工具面里没有券商确认入口）。
#   * 改单策略：官方 ``PUT /orders/{order_id}`` 支持原生改单 → live 非 A 股直接改单
#     （不再「撤旧重下」）；官方明示**不支持改 A 股订单**（modify-order 页）→ A 股
#     改单如实拒绝并给出替代路径（先 trade_cancel 再 trade_place，两笔独立批准）。
#     sim 路径不变（TOOL-LIMITS：sim_trade_modify_order 间歇性 -5 → 撤单+重下）。
#   * 新增 6 个 OpenAPI 只读端点/工具（trade_max_qty/orders_open/orders_history/
#     orders_detail/deals_today/deals_history）：mode 约束 + REST 直通，**不进缓存**。
#
# 错误码族（与既有 trading/* 一致）：
#   trading/order-rejected     风控/确认/OMS 查重等「拒单」类失败
#   trading/broker-unavailable 券商通道异常（适配器/网络/未接入）、live 写未接入、sim 通道
#                              不支持的字段（如实拒绝，不静默丢弃）
#   trading/invalid-operation  载荷白名单（app.py 路由层）与**字段校验层**（类型/枚举/
#                              条件必填/互斥/结构，WP8 任务 6 起）与查询模式非法
# 所有失败一律 {ok:false, error:{code, message≤300, details:{}}}，绝不抛到 500。
import json
import re
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

try:
    # venv 里 trading_core 已 pip install -e（与 server/scheduler.py 同口径）。
    from trading_core import alerts as core_alerts
    from trading_core import broker as core_broker
    from trading_core import daemon as core_daemon
    from trading_core import oms as core_oms
    from trading_core import risk as core_risk
    from trading_core import store as core_store
except ImportError:
    # 未安装 trading_core 的直跑环境：回退仓库路径（既有做法）。
    _CORE_PYTHON = str(Path(__file__).resolve().parents[2] / "plugins" / "core" / "python")
    if _CORE_PYTHON not in sys.path:
        sys.path.insert(0, _CORE_PYTHON)
    from trading_core import alerts as core_alerts
    from trading_core import broker as core_broker
    from trading_core import daemon as core_daemon
    from trading_core import oms as core_oms
    from trading_core import risk as core_risk
    from trading_core import store as core_store

from server import futu_data
from server import store_access
from server.store_access import WorkbenchError

# ---------------------------------------------------------------------------
# 常量（对照源码，不猜）
# ---------------------------------------------------------------------------
# 确认记录里的工具名：store_access.order_operation 用正则 ``(input|modify|cancel)_order$``
# 从工具名取操作类型（store_access.CONFIRM_OPERATIONS → 下单/改单/撤单）。服务工具名
# trade_place/trade_modify/trade_cancel 不带 _order 后缀，映射到确认记录名：
#   trade_place  → trade_input_order   （operation=下单）
#   trade_modify → trade_modify_order  （operation=改单）
#   trade_cancel → trade_cancel_order  （operation=撤单）
CONFIRM_TOOL = {"place": "trade_input_order", "modify": "trade_modify_order",
                "cancel": "trade_cancel_order"}

# 临时订单在 OMS 里的 plan_id 槽位：计划订单带真实 plan_id，临时订单集中在这个空槽，
# OMS 的「同槽同标的同方向在途查重」（oms.register_order 的幂等三件套之二）照此生效。
ADHOC_PLAN_ID = ""

# 交易时段判断的日历市场：store.is_trading_day 按 market 查 calendar 表，服务同步的
# A 股日历挂在 "SH"（daemon JOBS_DEFAULT 的市场名），SZ/BJ 标的共用同一 A 股日历。
CALENDAR_MARKET = {"SH": "SH", "SZ": "SH", "BJ": "SH", "HK": "HK", "US": "US"}

# 市场前缀 → 模拟账户 market_id：数字口径来自 store_access.MARKET_HINT 的实测
# （港股 1 / A股 3 / 美股 100，sim_trade_account_list 实测返回）；SH./SZ./BJ. 是 A 股、
# HK. 是港股、US. 是美股（broker.py lstrip("SH.ZBJ.") 与仓库标的写法可证）。
PREFIX_MARKET_ID = {"SH": 3, "SZ": 3, "BJ": 3, "HK": 1, "US": 100}

_SYMBOL_RE = re.compile(r"^([A-Z]+)\.\S+$")

# live 写路径的如实拒绝（见 FutuBroker 的说明）：**只对 mcp 通道的 FutuBroker 成立**——
# futu_channel=openapi 且凭据可用时 live 写由 OpenApiBroker 走 REST（WP8 任务 3）。
LIVE_WRITE_REFUSAL = (
    "实盘下单/改单/撤单在当前通道未接入券商执行：FutuBroker 只做 sim 写与账户查询"
    "（WP3 锁定 trading_order_place 的 schema；live 写由 OpenApiBroker 承担，需"
    " futu_channel=openapi 且已配置 OpenAPI 凭据）"
)

# live 写的闸门前置拒绝（TradeGate._write 在业务确认**之前**快速失败）：适配器未声明
# supports_live_write 时，live 写走完确认也注定被 _live_guard 拒绝——先置拒绝让确认
# 不被花在注定被拒的单上：不发起确认、不产生待确认、不落任何 OMS/风控行。
LIVE_WRITE_UNAVAILABLE = ("live 写通道未接入券商执行协议（futu_channel=openapi 且已配置"
                          " OpenAPI 凭据后启用）；当前仅 sim 可交易")

# ---------------------------------------------------------------------------
# WP8 任务 3：OpenAPI 交易链（通道路由 + 6 个只读端点）常量
# ---------------------------------------------------------------------------
# OpenAPI 凭据缺失的固定指引（与 server/futu_data.py 同一条文案，指向授权脚本）。
OPENAPI_AUTH_HINT = "（运行 scripts/futu_auth.py --openapi 完成 OAuth/AppKey 配置）"
# OpenAPI 交易通道不可用的信封码（与 futu_data 行情通道同码族；写路径的通道失败仍是
# trading/broker-unavailable —— 闸门既有语义不变）。
OPENAPI_UNAVAILABLE_CODE = "trading/openapi-unavailable"

# 市场前缀 → OpenAPI 授权账户的 enable_market（naming-dictionary#enable-market：
# 1=HK 2=US 4=ChinaStock 5=Futures 6=SG 12=CA 15=JP 18=KR）。**与 sim 的
# PREFIX_MARKET_ID 数字口径不同**（那是券商模拟账户的 market_id：HK 1/A 股 3/US 100），
# 两套数字各自来自各自通道的实测/文档，不互相换算。
OPENAPI_ENABLE_MARKET = {"HK": 1, "US": 2, "SH": 4, "SZ": 4, "BJ": 4}

# 市场前缀 → OpenAPI exchange（naming-dictionary#exchange）。官方枚举没有北交所（BJ）：
# 北交所标的的撤单/改单据此如实拒绝（不伪造 exchange 值），下单不受影响（place 只用 code）。
OPENAPI_EXCHANGE = {"SH": "SSE", "SZ": "SZSE", "HK": "SEHK", "US": "US"}

# A 股前缀（官方 modify-order 页：Does not support modifying A-share orders）。
A_SHARE_PREFIXES = frozenset({"SH", "SZ", "BJ"})

# enable_market 数字 → trd_market（naming-dictionary#trd-market 的官方枚举覆盖到的那些）。
ENABLE_MARKET_TO_TRD = {1: "HK", 2: "US", 4: "HKCC", 5: "FUTURES", 6: "SG", 12: "CA",
                        15: "JP", 18: "KR"}
# 逆映射：trd_market → enable_market（6 个只读查询按市场挑账户）。
TRD_MARKET_TO_ENABLE = {market: code for code, market in ENABLE_MARKET_TO_TRD.items()}

# 6 个 OpenAPI 只读端点/工具（工具面与端点清单增量，锁定测试逐个比对）。
OPENAPI_TRADE_ENDPOINTS = ("trade_max_qty", "orders_open", "orders_history",
                           "orders_detail", "deals_today", "deals_history")

# 只读新工具在 mcp 通道/未配置凭据/sim 模式下的如实拒绝文案（同一份，避免两处漂移）。
OPENAPI_ONLY_HINT = ("该查询属于 OpenAPI 交易链（trading-platform.json 的 "
                     "futu_channel=openapi 且已配置凭据）" + OPENAPI_AUTH_HINT
                     + "；mcp 通道下可用 account_orders/account_positions/account_funds")

# ---------------------------------------------------------------------------
# WP8 任务 6：place-order 官方全字段（闸门字段校验层的唯一取值域）
# ---------------------------------------------------------------------------
# 官方依据（2026-09-16 web_fetch 实抓 place-order.md / modify-order.md / naming-dictionary.md）：
#   order_type / time_in_force / session（仅美股，市价单仅 RTH）/ aux_price（触发类必填，
#   证券 3 位小数）/ lot_type（仅港股）/ remark（UTF-8 ≤64 字节）/ order_class（MLEG 必带
#   multi_leg_info）/ multi_leg_info（键集照 naming-dictionary#multi-leg-info）。
# 取值域在**本文件**声明（闸门不在默认部署下拉起 datasource 模块），与
# ``trading_datasource.futu_openapi.OpenApiTrade`` 的同名常量由锁定测试逐项比对，防两侧漂移。
# 校验发生在**风控/确认之前**：坏参数零券商往返、零确认消耗、零 OMS 落行。
PLACE_ORDER_TYPES = ("LIMIT", "MARKET", "AUCTION", "AUCTION_LIMIT", "STOP",
                     "STOP_LIMIT", "MARKET_IF_TOUCHED", "LIMIT_IF_TOUCHED")
PLACE_TIME_IN_FORCE = ("DAY", "GTC")
PLACE_SESSIONS = ("RTH", "RTH+Pre/Post-Mkt", "OVERNIGHT", "ALL_DAY")
# 触发类：官方明示 aux_price 必填（place-order / modify-order 两页同规则）。
AUX_PRICE_ORDER_TYPES = ("STOP", "STOP_LIMIT", "MARKET_IF_TOUCHED", "LIMIT_IF_TOUCHED")
# 限价类需要 price（官方 price 为选填，但限价类没有价格就不是限价单）；市价类带 price 属
# 含义不明 → 互斥拒绝（不替调用方猜）。
PRICE_ORDER_TYPES = ("LIMIT", "AUCTION_LIMIT", "STOP_LIMIT", "LIMIT_IF_TOUCHED")
MARKET_ORDER_TYPES = ("MARKET", "AUCTION", "STOP", "MARKET_IF_TOUCHED")
PLACE_LOT_TYPES = ("ODD", "ROUND")
PLACE_ORDER_CLASSES = ("NORMAL", "MLEG")
# naming-dictionary#multi-leg-info / #order-leg-info 的键集与必填。
MULTI_LEG_KEYS = ("option_strategy", "underlying_symbol", "underlying_stock_name",
                  "leg_infos")
MULTI_LEG_REQUIRED = ("option_strategy", "underlying_symbol", "leg_infos")
MULTI_LEG_STRATEGIES = ("Covered", "VerticalSpread", "Straddle", "Strangle", "Collar",
                        "Butterfly", "Condor", "IronButterfly", "IronCondor",
                        "CalenderSpread", "DiagonalSpread", "Customize")
LEG_KEYS = ("leg_symbol", "leg_exchange", "leg_ratio_qty", "leg_side",
            "leg_security_type", "leg_stock_name", "leg_hp_multiplier",
            "leg_avg_fill_price")
LEG_REQUIRED = ("leg_symbol", "leg_exchange", "leg_ratio_qty", "leg_side",
                "leg_security_type")
LEG_EXCHANGES = ("US", "SEHK", "SGX", "SSE", "SZSE", "JP", "CA", "CME", "CBOT",
                 "NYMEX", "COMEX", "CBOE", "HKFE", "KR")
LEG_SIDES = ("BUY", "SELL", "SELL_SHORT", "BUY_BACK")
LEG_SECURITY_TYPES = ("STOCK", "OPTION", "FUTURES", "MULTILEG_OPTION")
# 官方 place-order：remark UTF-8 编码后最大长度 64 字节。
REMARK_MAX_BYTES = 64
# 证券账户的触发价小数位（官方 place-order/modify-order：aux_price 精确到 3 位小数；
# 期货 9 位不在本期 symbol 前缀覆盖内）。超出即本地拒绝，**不四舍五入**——静默改价
# 等于替调用方决定成交条件（拒绝比篡改安全）。
AUX_PRICE_MAX_DECIMALS = 3

# 市价类里**请求体没有价格**的两类（MARKET/AUCTION）：官方 price 选填且与它们互斥，
# aux_price 也不适用。闸门的风控规则 4/5 需要 `qty × price` —— 基准价取**本地日线最近
# 收盘**（daemon._execute_plan 的 price_of 同款兜底口径，真事实而非编造）；本地取不到就
# fail-closed 拒绝：用 0 兜底会让两条规则静默失效（fail-open），编造价等于伪造风险事实。
NO_PRICE_ORDER_TYPES = ("MARKET", "AUCTION")
NO_PRICE_BASIS_REFUSAL = (
    "参数非法：市价类订单（{order_type}）没有价格/触发价，且本地没有 {symbol} 的日线数据"
    "可供风控基准（规则 4/5 需要 qty×price；用 0 兜底会让风控静默失效）——请先同步行情"
    "数据，或改用 LIMIT/AUCTION_LIMIT/STOP_LIMIT/LIMIT_IF_TOUCHED（带 price）或 "
    "STOP/MARKET_IF_TOUCHED（带 aux_price）")

# sim 通道能力边界（WP3 锁定的 sim_trade_place_order：core_broker.place 的 order_type=1
# 限价 + price）。显式给出的官方默认值（LIMIT/DAY/NORMAL）仍可用；其余字段一律如实拒绝，
# **绝不静默丢弃**（不变式 2）。gate 与适配层共用同一条文案。
SIM_LIMIT_ONLY_FIELDS = ("session", "aux_price", "lot_type", "remark", "multi_leg_info")
SIM_LIMIT_REFUSAL = ("sim 仅支持限价当日单（order_type=LIMIT / time_in_force=DAY / "
                     "order_class=NORMAL，不支持 session/aux_price/lot_type/remark/"
                     "multi_leg_info）；这些字段不会被静默丢弃——请改用 live 通道"
                     "（futu_channel=openapi 且已配置 OpenAPI 凭据）")


def _sim_unsupported(clean):
    """sim 通道不支持的字段名（空列表 = 限价当日单，sim 可原样执行）。"""
    unsupported = []
    if clean.get("order_type", "LIMIT") != "LIMIT":
        unsupported.append("order_type")
    if clean.get("time_in_force", "DAY") != "DAY":
        unsupported.append("time_in_force")
    if clean.get("order_class", "NORMAL") != "NORMAL":
        unsupported.append("order_class")
    unsupported += [name for name in SIM_LIMIT_ONLY_FIELDS if name in clean]
    return unsupported


def _risk_price(clean):
    """请求体里的风险基准价：限价类用 price；触发类（无 price）用 aux_price。"""
    price = clean.get("price")
    return clean.get("aux_price") if price is None else price


def _local_close(conn, symbol, today):
    """本地日线最近收盘价（PIT：``ts <= today``）；没有数据返回 None。

    口径与 ``daemon._last_close`` 逐字一致——市价类订单的风险基准价从这里取，属于**已同步
    的真事实**，而不是闸门凭空编造的价格。
    """
    try:
        bars = core_store.read_bars(conn, symbol, "1d", today, limit=1)
    except Exception:  # noqa: BLE001 —— 日历/库表缺失 → 视为「本地无基准」（fail-closed）
        return None
    if not bars:
        return None
    close = bars[-1].get("c")
    if isinstance(close, bool) or not isinstance(close, (int, float)) or not (close > 0):
        return None
    return float(close)


def _too_many_decimals(value, places):
    """数值的小数位是否超过 ``places``（用 Decimal(str(v))，不受二进制浮点影响）。"""
    exponent = Decimal(str(value)).normalize().as_tuple().exponent
    return isinstance(exponent, int) and exponent < 0 and -exponent > places


def _enum(value, allowed, name):
    """枚举取值域校验（错误消息带官方允许值，便于模型自我纠正）。"""
    if value not in allowed:
        return f"参数非法：{name} 只接受 {'/'.join(allowed)}，收到 {value!r}"
    return None


def _check_number(value, name, positive=True):
    """数值校验（bool 不算数值）；positive=True 时要求 > 0。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"参数非法：{name} 必须是数值，收到 {value!r}"
    if positive and not (value > 0):
        return f"参数非法：{name} 必须是 >0 的数值，收到 {value!r}"
    return None


def _multi_leg_error(value):
    """multi_leg_info 的结构校验（对象或对象列表；键集/必填/枚举照官方命名词典）。"""
    if value is None or not isinstance(value, (dict, list)):
        return "参数非法：multi_leg_info 必须是对象或对象列表"
    items = value if isinstance(value, list) else [value]
    if not items:
        return "参数非法：multi_leg_info 不能为空"
    for item in items:
        if not isinstance(item, dict):
            return "参数非法：multi_leg_info 必须是对象或对象列表"
        unknown = sorted(set(item) - set(MULTI_LEG_KEYS))
        if unknown:
            return f"参数非法：multi_leg_info 含未支持字段 {unknown}"
        for field in MULTI_LEG_REQUIRED:
            if item.get(field) in (None, ""):
                return f"参数非法：multi_leg_info.{field} 必填"
        message = _enum(item["option_strategy"], MULTI_LEG_STRATEGIES,
                        "multi_leg_info.option_strategy")
        if message:
            return message
        legs = item["leg_infos"]
        if not isinstance(legs, list) or not legs:
            return "参数非法：multi_leg_info.leg_infos 必须是至少一条腿的列表"
        for leg in legs:
            if not isinstance(leg, dict):
                return "参数非法：multi_leg_info.leg_infos 元素必须是对象"
            unknown = sorted(set(leg) - set(LEG_KEYS))
            if unknown:
                return f"参数非法：multi_leg_info.leg_infos 含未支持字段 {unknown}"
            for field in LEG_REQUIRED:
                if leg.get(field) in (None, ""):
                    return f"参数非法：multi_leg_info.leg_infos.{field} 必填"
            for field, allowed in (("leg_exchange", LEG_EXCHANGES),
                                   ("leg_side", LEG_SIDES),
                                   ("leg_security_type", LEG_SECURITY_TYPES)):
                message = _enum(leg[field], allowed,
                                f"multi_leg_info.leg_infos.{field}")
                if message:
                    return message
    return None


class BrokerUnavailable(RuntimeError):
    """券商通道不可用/未接入：适配层「确定未发出」的故障才抛这一类。"""


class OpenApiUnavailable(BrokerUnavailable):
    """OpenAPI 交易通道不可用（channel≠openapi / 凭据缺失 / sim 模式无 REST 面）。

    单独子类是为了让**只读** OpenAPI 端点/工具的失败信封用
    ``trading/openapi-unavailable``（与 server/futu_data.py 的行情通道同码族，指引用户
    去 ``scripts/futu_auth.py --openapi``）；写路径的通道失败仍是
    ``trading/broker-unavailable``（闸门既有语义，不变）。
    """


def _now():
    """trading_core 侧的本地时间口径（东八区、秒精度；oms/execute 同形）。"""
    tz8 = timezone(timedelta(hours=8))
    return datetime.now(tz8).strftime("%Y-%m-%d %H:%M:%S")


def _iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope_ok(value):
    return {"ok": True, "value": value}


def _envelope_fail(code, message, **details):
    return {"ok": False,
            "error": {"code": code, "message": str(message)[:300], "details": details}}


# ---------------------------------------------------------------------------
# 业务确认绑定（store_access 三方法；WP6 的跨进程限制对交易路径消解：
# 发起与作答同在本服务进程，Web 卡片经 confirmation/confirm-decide 端点作答）
# ---------------------------------------------------------------------------
class StoreConfirm:
    """``store_access`` 确认三方法的绑定门面。测试可注入同形假件。"""

    def __init__(self, module=None):
        self._module = store_access if module is None else module

    def request(self, home, tool, mode, args, session_id, ttl_ms):
        return self._module.request_confirmation(home, tool=tool, mode=mode, args=args,
                                                 session_id=session_id, ttl_ms=ttl_ms)

    def view(self, home):
        return self._module.confirmation_view(home)

    def decide(self, home, confirmation_id, decision):
        return self._module.decide_confirmation(home, confirmation_id=confirmation_id,
                                                decision=decision)


# ---------------------------------------------------------------------------
# 风控 ctx 构造（逐字段对照 risk.pre_trade_checks 需要什么，披露见行内注释）
# ---------------------------------------------------------------------------
# risk.py 的 8 条规则需要的 ctx 键：mode / kill_path / is_trading_day / equity /
# positions_value / positions_count / day_pnl_pct / config / plan_hash / plan_status。
# 其中服务进程**能取真值**的：mode（模式文件）、kill_path（home/trading-kill 文件）、
# is_trading_day（本地日历库）、config（daemon.risk_config，含 trading-risk.json 覆盖）。
# **取不到真值**的（见 default_ctx_builder 行内披露）：权益/持仓市值/持仓数/日内盈亏
# ——闸门在确认通过前对 broker 保持零调用（闸门失败路径不得触达 broker），因此敞口
# 字段沿用 daemon._execute_plan 的离线保守口径，不为此发起券商查询。
def default_ctx_builder(conn, mode, order, operation, home, today):
    """默认 risk ctx：真值尽量取，取不到用保守默认（逐字段披露）。"""
    prefix = order["symbol"].split(".", 1)[0]
    calendar_market = CALENDAR_MARKET.get(prefix)
    if calendar_market is None:
        is_trading_day = False  # 未知市场：不放行（宁可不执行）
    else:
        try:
            is_trading_day = core_store.is_trading_day(conn, calendar_market, today)
        except RuntimeError:
            # 日历未同步：daemon._execute_plan 同口径——不放行（宁可不执行）
            is_trading_day = False
    ctx = {
        "mode": mode,
        # kill 文件约定与 WP3 risk.py ctx["kill_path"] / daemon.kill_path 同一事实
        "kill_path": str(core_daemon.kill_path(home)),
        # 保守默认（daemon 离线口径 1_000_000）：闸门确认前不查券商，真实权益口径
        # 需要盯市数据，服务侧暂无该作业链——规则 4/5 以此口径拦截超限单，可能
        # 拒掉权益远大于默认值的合法单（宁可错拒，注释披露给后续任务接真值）。
        "equity": 1_000_000.0,
        # 持仓市值表/持仓数：同上取不到真值，空表意味着规则 5/6 只对「本单全额名义」
        # 生效——低估持仓的后果是**拦不住叠加超限**（存量持仓不可见，规则 5/6 看不到
        # 已有市值）；单笔名义被规则 4 封顶（对照 risk.py：stop_dist=None 时风险额按
        # 全额名义对 equity×risk_per_trade 封顶），故 fail-open 幅度有界（如实披露）。
        "positions_value": {},
        "positions_count": 0,
        # 日内盈亏：取不到真值给 0.0（不触发规则 7 熔断口径）；熔断落库（store.set_halt）
        # 由 daemon 执行链负责，闸门不重复实现。
        "day_pnl_pct": 0.0,
        "is_trading_day": bool(is_trading_day),
        # 风控参数：trading-risk.json 覆盖 + RISK_DEFAULTS（真值）
        "config": core_daemon.risk_config(home),
        # 规则 8（计划一致性）对**临时订单**的口径，如实披露（对照 risk.py:52-54）：
        # risk.py 要求 order.plan_hash == ctx.plan_hash 且 plan_status ∈
        # {frozen, approved, executing}。交易工具的载荷白名单里没有 plan_hash
        # （杜绝「自称属于某计划」的旁路），order 与 ctx 两侧恒为 None → 哈希子句恒真；
        # plan_status 标 "approved" 表示该单的授权形态是「随后的人工业务确认」——
        # 确认未通过则整单拒绝（fail-closed），规则 8 的放行不会单独放行任何订单。
        # 需要计划哈希校验的执行路径仍是 plan-execute → daemon → execute.run
        # （content_hash 与状态在那里逐一核实），本闸门不重复也不伪造它。
        "plan_hash": None,
        "plan_status": "approved",
    }
    if operation == "cancel":
        # 撤单不新增风险敞口：规则 4（单笔风险）以 qty=0 自然为 0；规则 5/6 以空持仓
        # 表自然通过；规则 7 的熔断语义本身就是「撤单」（execute.run 熔断即撤余单），
        # 用真实日亏会拦下熔断中的撤单、与既有语义相反——故给中性 0.0（披露）。
        # 撤单真正受约束的是规则 1（kill）/2（模式）/3（交易日）。
        ctx.update({"positions_value": {}, "positions_count": 0, "day_pnl_pct": 0.0})
    return ctx


def validate_order(payload, operation):
    """载荷字段校验（类型/范围/枚举/条件必填/互斥）。返回 (clean, error_message)。

    clean 只保留闸门与适配层需要的键：symbol/side/qty/price/order_id/client_order_id
    （WP8 任务 6 起另含官方 place 扩展字段，见下）。撤单不需要 side/qty/price
    （撤的是已提交订单，不新增敞口），只认 order_id + symbol。

    WP8 任务 6 的官方字段校验（逐条对照 place-order.md / naming-dictionary.md）：
      * order_type 8 枚举（默认 LIMIT）、time_in_force{DAY,GTC}（默认 DAY）；
      * session 4 枚举（仅美股；市价单仅支持 RTH）；
      * price 限价类必填、市价类互斥；
      * aux_price 触发类必填、非触发类互斥、证券 3 位小数；
      * lot_type{ODD,ROUND} 仅港股；remark UTF-8 ≤64 字节；
      * order_class{NORMAL,MLEG}：MLEG 必带 multi_leg_info，反之亦然；
      * multi_leg_info 结构照 naming-dictionary#multi-leg-info。
    全部错误在**风控/确认之前**返回（调用方信封成 trading/invalid-operation），
    消息带官方允许值/字段名，坏参数零券商往返。
    """
    if not isinstance(payload, dict):
        return None, "订单参数必须是对象"
    clean = {}
    required = ["symbol"]
    if operation in ("place", "modify"):
        required += ["side", "qty"]
    if operation == "modify":
        required += ["price"]  # 官方改单 price 必填（place 的 price 按 order_type 条件必填）
    if operation in ("modify", "cancel"):
        required += ["order_id"]
    for field in required:
        if field not in payload or payload[field] is None:
            return None, f"参数非法：缺少 {field}"
    symbol = payload["symbol"]
    if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol.strip().upper()):
        return None, f"参数非法：symbol 形如 SH.600519，收到 {symbol!r}"
    prefix = symbol.strip().upper().split(".", 1)[0]
    if prefix not in PREFIX_MARKET_ID:
        return None, f"参数非法：无法识别的市场前缀 {prefix}（支持 SH/SZ/BJ/HK/US）"
    clean["symbol"] = symbol.strip().upper()
    if "side" in required:
        side = payload["side"]
        if side not in ("BUY", "SELL"):
            return None, f"参数非法：side 只接受 BUY/SELL，收到 {side!r}"
        clean["side"] = side
    if "qty" in required:
        qty = payload["qty"]
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or \
                not float(qty).is_integer() or int(qty) < 1:
            return None, f"参数非法：qty 必须是 >=1 的整数，收到 {qty!r}"
        clean["qty"] = int(qty)
    if "order_id" in required:
        order_id = payload["order_id"]
        if not isinstance(order_id, str) or not order_id.strip():
            return None, "参数非法：order_id 必须是非空字符串"
        clean["order_id"] = order_id.strip()

    if operation == "place":
        message = _validate_place_extras(payload, clean)
        if message is not None:
            return None, message
    elif operation == "modify":
        # 官方改单 price 必填（请求体里没有 order_type：订单类型由券商侧已有订单决定，
        # 故这里只额外透传触发价 aux_price，不引入类型语义）。
        message = _check_number(payload["price"], "price")
        if message is not None:
            return None, message
        clean["price"] = float(payload["price"])
        if payload.get("aux_price") is not None:
            message = _check_number(payload["aux_price"], "aux_price")
            if message is None and _too_many_decimals(payload["aux_price"],
                                                      AUX_PRICE_MAX_DECIMALS):
                message = (f"参数非法：aux_price 按官方证券账户精确到 "
                           f"{AUX_PRICE_MAX_DECIMALS} 位小数，收到 {payload['aux_price']!r}")
            if message is not None:
                return None, message
            clean["aux_price"] = float(payload["aux_price"])
    elif "price" in payload and payload["price"] is not None:
        # 撤单带 price 无意义（载荷白名单已挡在 HTTP/MCP 层；直调闸门时如实拒绝）
        message = _check_number(payload["price"], "price")
        if message is not None:
            return None, message

    cid = payload.get("client_order_id")
    if cid is not None:
        if not isinstance(cid, str) or not cid.strip():
            return None, "参数非法：client_order_id 必须是非空字符串"
        clean["client_order_id"] = cid.strip()
    return clean, None


def _validate_place_extras(payload, clean):
    """place 的官方扩展字段校验；返回错误消息或 None（就地写入 clean）。

    只有调用方**显式给出**的扩展字段才写进 clean（进而透传给券商）——缺省值不改写适配层
    的默认口径（LIMIT/DAY 由 OpenApiBroker 在出站时补），因此既有的 WP7 券商入参逐键不变。
    """
    order_type = payload.get("order_type")
    if order_type is None:
        order_type = "LIMIT"
    message = _enum(order_type, PLACE_ORDER_TYPES, "order_type")
    if message is not None:
        return message
    if "order_type" in payload:
        clean["order_type"] = order_type

    time_in_force = payload.get("time_in_force")
    if time_in_force is None:
        time_in_force = "DAY"
    message = _enum(time_in_force, PLACE_TIME_IN_FORCE, "time_in_force")
    if message is not None:
        return message
    if "time_in_force" in payload:
        clean["time_in_force"] = time_in_force

    # price：限价类必填、市价类互斥；无条件先校验它给出的值本身合法。
    price = payload.get("price")
    if price is not None:
        message = _check_number(price, "price")
        if message is not None:
            return message
        if order_type in MARKET_ORDER_TYPES:
            return (f"参数非法：order_type={order_type} 是市价类订单，不带 price"
                    f"（官方 price 选填且市价类无价格语义）")
        clean["price"] = float(price)
    elif order_type in PRICE_ORDER_TYPES:
        return (f"参数非法：order_type={order_type} 需要 price（限价类订单必填；"
                f"允许的价格基准见 PRICE_ORDER_TYPES={PRICE_ORDER_TYPES}）")

    # aux_price：触发类必填、非触发类互斥、证券 3 位小数。
    aux_price = payload.get("aux_price")
    if aux_price is not None:
        message = _check_number(aux_price, "aux_price")
        if message is not None:
            return message
        if order_type not in AUX_PRICE_ORDER_TYPES:
            return (f"参数非法：aux_price 只用于触发类订单 "
                    f"{'/'.join(AUX_PRICE_ORDER_TYPES)}，收到 order_type={order_type}")
        if _too_many_decimals(aux_price, AUX_PRICE_MAX_DECIMALS):
            return (f"参数非法：aux_price 按官方证券账户精确到 "
                    f"{AUX_PRICE_MAX_DECIMALS} 位小数，收到 {aux_price!r}")
        clean["aux_price"] = float(aux_price)
    elif order_type in AUX_PRICE_ORDER_TYPES:
        return (f"参数非法：order_type={order_type} 时 aux_price 必填"
                f"（官方触发价规则；证券 3 位小数）")

    # 注意 MARKET/AUCTION（NO_PRICE_ORDER_TYPES）：字段层放行（price 互斥已在上方校验），
    # 风险基准价由闸门用**本地日线最近收盘**补齐（TradeGate._risk_basis）；取不到即 fail-closed。

    session = payload.get("session")
    if session is not None:
        message = _enum(session, PLACE_SESSIONS, "session")
        if message is not None:
            return message
        if clean["symbol"].split(".", 1)[0] != "US":
            return (f"参数非法：session 仅适用于美股（{clean['symbol']} 不是美股）；"
                    f"允许值 {'/'.join(PLACE_SESSIONS)}")
        if order_type == "MARKET" and session != "RTH":
            return (f"参数非法：市价单仅支持 session=RTH（官方原文），收到 {session!r}；"
                    f"允许值 {'/'.join(PLACE_SESSIONS)}")
        clean["session"] = session

    lot_type = payload.get("lot_type")
    if lot_type is not None:
        message = _enum(lot_type, PLACE_LOT_TYPES, "lot_type")
        if message is not None:
            return message
        if clean["symbol"].split(".", 1)[0] != "HK":
            return (f"参数非法：lot_type 仅适用于港股（{clean['symbol']} 不是港股）；"
                    f"允许值 {'/'.join(PLACE_LOT_TYPES)}")
        clean["lot_type"] = lot_type

    remark = payload.get("remark")
    if remark is not None:
        if not isinstance(remark, str):
            return f"参数非法：remark 必须是字符串，收到 {remark!r}"
        if len(remark.encode("utf-8")) > REMARK_MAX_BYTES:
            return (f"参数非法：remark 的 UTF-8 长度不得超过 {REMARK_MAX_BYTES} 字节"
                    f"（官方 place-order），收到 {len(remark.encode('utf-8'))} 字节")
        clean["remark"] = remark

    order_class = payload.get("order_class")
    if order_class is not None:
        message = _enum(order_class, PLACE_ORDER_CLASSES, "order_class")
        if message is not None:
            return message
        clean["order_class"] = order_class

    multi_leg_info = payload.get("multi_leg_info")
    if multi_leg_info is not None:
        if order_class != "MLEG":
            return ("参数非法：multi_leg_info 需要与 order_class=MLEG 同时给出"
                    "（官方：多腿订单需指定为 MLEG）")
        message = _multi_leg_error(multi_leg_info)
        if message is not None:
            return message
        clean["multi_leg_info"] = multi_leg_info
    elif order_class == "MLEG":
        return ("参数非法：order_class=MLEG 时 multi_leg_info 必填"
                "（官方多腿订单规则；结构见 naming-dictionary#multi-leg-info）")
    return None


def _side_code(side):
    """方向 → 券商代码（broker.py place 的同一线：1=BUY 2=SELL）。"""
    return 1 if side == "BUY" else 2


def _summary_args(operation, clean, risk_price=None):
    """确认摘要的入参：键名走券商参数表（store_access.describe_order_args 的渲染口径），
    让确认卡片显示 账户/市场/标的/方向/数量/价格（WP8 任务 6 起含订单类型/触发价/时段/
    手数类型/订单类别/多腿信息——人批准的就是这些参数）。acc_id 在批准后由适配层解析
    （确认前 broker 保持零调用），摘要里如实不含账户。撤单无方向可列（不新增敞口）。
    市价类没有下单价：把闸门实际使用的**风控基准价**如实列进卡片（本地最近收盘），
    让人知道自己批准的是什么口径。"""
    args = {"market": str(PREFIX_MARKET_ID[clean["symbol"].split(".", 1)[0]]),
            "symbol": clean["symbol"]}
    if "side" in clean:
        args["order_side"] = _side_code(clean["side"])
    if operation in ("place", "modify"):
        args["qty"] = clean["qty"]
        if "price" in clean:
            args["price"] = clean["price"]
        elif "aux_price" not in clean:
            # 市价类（MARKET/AUCTION）既无下单价也无触发价：把闸门实际使用的风控基准价
            # （本地最近收盘）如实列进卡片，让人知道自己批准的是什么口径。
            args["risk_price"] = risk_price
        # 订单类型：显式给的是官方枚举字符串；缺省沿用券商代码 1=限价（WP7 口径不变）。
        args["order_type"] = clean.get("order_type", 1)
        for name in ("time_in_force", "session", "aux_price", "lot_type", "remark",
                     "order_class"):
            if name in clean:
                args[name] = clean[name]
        if "multi_leg_info" in clean:
            # 对象原样交给卡片渲染会退化成 "[object Object]"（_js.template 的同 JS 语义），
            # 这里先序列化成可读 JSON——人要在卡片上核对每一条腿。
            args["multi_leg_info"] = json.dumps(clean["multi_leg_info"],
                                                ensure_ascii=False, separators=(",", ":"))
    if operation in ("modify", "cancel"):
        args["order_id"] = clean["order_id"]
    return args


def _check_order(operation, clean, mode, risk_price=None):
    """送进 pre_trade_checks 的订单：改单按**新参数**全额过闸（改单经济上=撤旧+下新，
    见 FutuBroker.modify 与 TOOL-LIMITS「一律撤单+重下」）；撤单不新增敞口，
    以 qty=0/price=0 过闸（规则 4-7 自然中性，真正约束是 1/2/3——披露于 default_ctx_builder）。
    stop_dist 恒 None：风险额按全额名义计（daemon stop_dist_of 的保守口径）。
    risk.pre_trade_checks 不读 side（8 规则均不用），撤单缺方向无影响。
    WP8 任务 6：``risk_price`` 是闸门解析出的风险基准价（price / aux_price / 市价类的本地
    最近收盘，见 TradeGate._risk_price）——规则 4/5 的 `qty × price` 因此恒有真值。"""
    order = {"symbol": clean["symbol"], "mode": mode, "stop_dist": None}
    if "side" in clean:
        order["side"] = clean["side"]
    if operation == "cancel":
        order.update({"qty": 0, "price": 0.0})
    else:
        basis = _risk_price(clean)
        order.update({"qty": clean["qty"],
                      "price": float(basis if basis is not None else risk_price)})
    return order


# ---------------------------------------------------------------------------
# broker 适配（唯一的富途协议接触面）
# ---------------------------------------------------------------------------
class FutuBroker:
    """gate→broker 接口（place/modify/cancel/positions/orders/funds）的 WP7 实现。

    * sim 分支复用 trading_core.broker 的锁定工具名（WP3 依赖锁定表，实测口径）；
      账户按 sim_trade_account_list 的 market_id 解析（P4 教训：market 缺省会报错）。
    * live 查询复用仓库内的实测协议：account_authorized_trd_accs + account_positions
      （plugins/workbench/python/positions.py live_groups）、account_orders_history 与
      account_funds（docs/TOOL-LIMITS.md 实测：订单历史必须带 start/end，否则静默
      返回纯文本 no data）。
    * live 写路径：本类**不实现**（WP3 只锁定 trading_order_place 的 schema）；
      ``supports_live_write`` 恒 False，闸门在业务确认之前就拒绝 live 写。
      WP8 任务 3 起 live 写由 ``OpenApiBroker``（channel=openapi + 凭据）承担——
      原样的 FutuBroker 仍是 mcp 通道与 sim 路径的实现（不变式 2：默认行为零变化）。
    * **适配器契约**（闸门正确性依赖它）：调用层超时必须在本层消化成
      ``{"status": "unknown"}``（铁律：超时→查询不重放），只有「确定未发出」的故障
      才抛 BrokerUnavailable——闸门据此决定 OMS 落 unknown（在途待查询）还是 rejected。
    """

    # 适配器契约：是否实现 live 写路径。默认 False（本类只做 sim 写 + live/sim 查询）；
    # TradeGate._write 据此在业务确认之前拒绝 live 写（不发起确认、不产生待确认）。
    # 接入了 live 写的适配器必须显式声明 True，才会走完整确认流到达 broker。
    supports_live_write = False

    def __init__(self, call=None):
        self._call = call  # None → 首次调用时惰性导入 futu_mcp.call_tool

    def _tool(self):
        if self._call is None:
            from trading_datasource.futu_mcp import call_tool  # noqa: PLC0415
            self._call = call_tool
        return self._call

    # ---- sim 账户解析 ----
    def _sim_account(self, call, symbol):
        prefix = symbol.split(".", 1)[0]
        want = PREFIX_MARKET_ID.get(prefix)
        accounts = (core_broker.accounts(call) or {}).get("accounts") or []
        for account in accounts:
            if str(account.get("market_id")) == str(want):
                return {"acc_id": str(account["account_id"]),
                        "market_id": account.get("market_id")}
        raise BrokerUnavailable(f"模拟账户中没有 {prefix}（market_id={want}）的账户，"
                                "先在富途开通对应市场模拟账户")

    def _live_guard(self, mode):
        if mode == "live":
            raise BrokerUnavailable(LIVE_WRITE_REFUSAL)

    def _sim_options_guard(self, order):
        """sim 通道的字段能力边界（WP8 任务 6）：sim_trade_place_order/modify 只支持限价
        当日单，扩展字段在这里**如实拒绝**（抛 BrokerUnavailable → 闸门信封
        trading/broker-unavailable），绝不静默丢弃。闸门字段层已先置拒绝；本守卫是适配层的
        第二道（防绕道直调适配器）。"""
        unsupported = _sim_unsupported(order)
        if unsupported:
            raise BrokerUnavailable(f"{SIM_LIMIT_REFUSAL}（不支持的字段：{unsupported}）")

    # ---- 写路径 ----
    def place(self, order, mode):
        self._live_guard(mode)
        self._sim_options_guard(order)
        call = self._tool()
        account = self._sim_account(call, order["symbol"])
        # market 传账户的 market_id（数字口径，与 sim_trade_position_list 实测一致）；
        # execute.run 传的是交易所前缀——两条路径的差异在此披露，适配层以账户事实为准。
        return core_broker.place(call, acc_id=account["acc_id"],
                                 market=account["market_id"], symbol=order["symbol"],
                                 side=order["side"], qty=order["qty"],
                                 price=order.get("price"))

    def modify(self, order, mode):
        """改单 = 撤旧单 + 按新参数重新下单（TOOL-LIMITS：sim_trade_modify_order
        间歇性 -5，一律不用）。旧单已撤而新单超时时返回 unknown（先查询，不重放）。
        WP8 任务 6：官方改单的 aux_price 在 sim 路径无法表达（撤旧重下只带 price）——
        如实拒绝而不是丢弃触发价（见 _sim_options_guard）。"""
        self._live_guard(mode)
        self._sim_options_guard(order)
        call = self._tool()
        account = self._sim_account(call, order["symbol"])
        try:
            cancel_out = core_broker.cancel(call, acc_id=account["acc_id"],
                                            market=account["market_id"],
                                            order_id=order["order_id"])
        except TimeoutError as error:
            return {"status": "unknown", "broker_order_id": None,
                    "err": f"改单撤旧超时：{str(error)[:120]}（先查询，不重放）"}
        except Exception as error:  # noqa: BLE001 —— 拒单/业务失败如实上抛信息
            return {"status": "rejected", "broker_order_id": None,
                    "err": f"改单撤旧失败：{str(error)[:160]}"}
        placed = core_broker.place(call, acc_id=account["acc_id"],
                                   market=account["market_id"], symbol=order["symbol"],
                                   side=order["side"], qty=order["qty"],
                                   price=order["price"])
        return {"status": placed["status"],
                "broker_order_id": placed.get("broker_order_id"),
                "cancel": cancel_out, "place": placed, "err": placed.get("err")}

    def cancel(self, order, mode):
        self._live_guard(mode)
        call = self._tool()
        account = self._sim_account(call, order["symbol"])
        try:
            raw = core_broker.cancel(call, acc_id=account["acc_id"],
                                     market=account["market_id"],
                                     order_id=order["order_id"])
        except TimeoutError as error:
            return {"status": "unknown", "order_id": order["order_id"],
                    "err": f"撤单超时：{str(error)[:120]}（先查询，不重放）"}
        except Exception as error:  # noqa: BLE001
            return {"status": "rejected", "order_id": order["order_id"],
                    "err": str(error)[:160]}
        # raw 原样附上：isError=false 不代表业务成功（铁律：仍需查业务码），
        # 业务码枚举未公开，本层不解读，留给调用方/对账核对。
        return {"status": "cancelled", "order_id": order["order_id"], "raw": raw}

    # ---- 查询路径（直通，模式约束在闸门）----
    def positions(self, mode):
        call = self._tool()
        groups, errors = [], []
        if mode == "sim":
            accounts = [a for a in ((core_broker.accounts(call) or {}).get("accounts") or [])
                        if a.get("account_id")]
            for account in accounts:
                acc_id = str(account["account_id"])
                try:
                    rows = core_broker.positions(call, acc_id=acc_id,
                                                 market_id=account.get("market_id"))
                except Exception as error:  # noqa: BLE001 —— 单账户失败不掩盖
                    errors.append({"acc_id": acc_id, "reason": str(error)[:160]})
                    continue
                if rows:
                    groups.append({"acc_id": acc_id, "market": account.get("market_id"),
                                   "positions": rows})
            source = "futu/sim_trade_position_list"
        else:
            accounts = (call("account_authorized_trd_accs", {}) or {}).get("accounts") or []
            for account in accounts:
                acc_id = str(account.get("account_id") or "")
                if not acc_id:
                    continue
                try:
                    rows = call("account_positions", {"acc_id": acc_id}, timeout=30)
                except Exception as error:  # noqa: BLE001
                    errors.append({"acc_id": acc_id, "reason": str(error)[:160]})
                    continue
                rows = rows if isinstance(rows, list) else (rows.get("positions") or [])
                if rows:
                    groups.append({"acc_id": acc_id, "positions": rows})
            source = "futu/account_positions"
        return {"mode": mode, "source": source, "as_of": _iso_now(),
                "groups": groups, "errors": errors,
                "note": "券商返回的真实持仓（按账户列出，不跨账户/币种合并）；"
                        "失败账户列入 errors，不掩盖。"}

    def orders(self, mode):
        """订单历史。实测约束（TOOL-LIMITS）：不带 start/end 会静默返回纯文本 no data，
        因此这里固定带最近 30 天窗口；日期文本格式未离线验证，sim 冒烟时校准。"""
        call = self._tool()
        end = date.today()
        start = end - timedelta(days=30)
        window = {"start": start.isoformat(), "end": end.isoformat()}
        groups, errors = [], []
        if mode == "sim":
            accounts = [a for a in ((core_broker.accounts(call) or {}).get("accounts") or [])
                        if a.get("account_id")]
            for account in accounts:
                acc_id = str(account["account_id"])
                try:
                    rows = call("sim_trade_history_order_list",
                                dict({"acc_id": acc_id}, **window), timeout=30)
                except Exception as error:  # noqa: BLE001
                    errors.append({"acc_id": acc_id, "reason": str(error)[:160]})
                    continue
                groups.append({"acc_id": acc_id, "rows": rows})
            tool = "sim_trade_history_order_list"
        else:
            accounts = (call("account_authorized_trd_accs", {}) or {}).get("accounts") or []
            for account in accounts:
                acc_id = str(account.get("account_id") or "")
                if not acc_id:
                    continue
                try:
                    rows = call("account_orders_history",
                                dict({"acc_id": acc_id}, **window), timeout=30)
                except Exception as error:  # noqa: BLE001
                    errors.append({"acc_id": acc_id, "reason": str(error)[:160]})
                    continue
                groups.append({"acc_id": acc_id, "rows": rows})
            tool = "account_orders_history"
        return {"mode": mode, "source": f"futu/{tool}", "as_of": _iso_now(),
                "window": window, "groups": groups, "errors": errors,
                "note": "订单历史按账户列出（固定 30 天窗口；该工具不带时间范围会静默返回"
                        " no data，见 TOOL-LIMITS）。"}

    def funds(self, mode):
        call = self._tool()
        groups, errors = [], []
        if mode == "sim":
            accounts = [a for a in ((core_broker.accounts(call) or {}).get("accounts") or [])
                        if a.get("account_id")]
            for account in accounts:
                acc_id = str(account["account_id"])
                try:
                    rows = call("sim_trade_cash_info", {"acc_id": acc_id}, timeout=30)
                except Exception as error:  # noqa: BLE001
                    errors.append({"acc_id": acc_id, "reason": str(error)[:160]})
                    continue
                groups.append({"acc_id": acc_id, "market": account.get("market_id"),
                               "cash": rows})
            tool = "sim_trade_cash_info"
        else:
            accounts = (call("account_authorized_trd_accs", {}) or {}).get("accounts") or []
            for account in accounts:
                acc_id = str(account.get("account_id") or "")
                if not acc_id:
                    continue
                try:
                    rows = call("account_funds", {"acc_id": acc_id}, timeout=30)
                except Exception as error:  # noqa: BLE001
                    errors.append({"acc_id": acc_id, "reason": str(error)[:160]})
                    continue
                groups.append({"acc_id": acc_id, "cash": rows})
            tool = "account_funds"
        return {"mode": mode, "source": f"futu/{tool}", "as_of": _iso_now(),
                "groups": groups, "errors": errors}

    # ---- WP8 任务 3：OpenAPI 交易只读端点（mcp 通道没有对应数据面 → 如实拒绝）----
    # 这 6 个方法只在 OpenApiBroker（channel=openapi）里有真实实现；这里保留同名方法，
    # 使「未配置 OpenAPI 的默认部署」拿到 trading/openapi-unavailable 的**可执行指引**，
    # 而不是 AttributeError 被信封化成一句含糊的通道异常。
    def trade_max_qty(self, payload, mode=None):
        """见 OpenApiBroker.trade_max_qty（需要 futu_channel=openapi）。"""
        raise OpenApiUnavailable(f"trade_max_qty 需要 OpenAPI 交易通道；{OPENAPI_ONLY_HINT}")

    def orders_open(self, payload, mode=None):
        """见 OpenApiBroker.orders_open（需要 futu_channel=openapi）。"""
        raise OpenApiUnavailable(f"orders_open 需要 OpenAPI 交易通道；{OPENAPI_ONLY_HINT}")

    def orders_history(self, payload, mode=None):
        """见 OpenApiBroker.orders_history（需要 futu_channel=openapi）。"""
        raise OpenApiUnavailable(f"orders_history 需要 OpenAPI 交易通道；{OPENAPI_ONLY_HINT}")

    def orders_detail(self, payload, mode=None):
        """见 OpenApiBroker.orders_detail（需要 futu_channel=openapi）。"""
        raise OpenApiUnavailable(f"orders_detail 需要 OpenAPI 交易通道；{OPENAPI_ONLY_HINT}")

    def deals_today(self, payload, mode=None):
        """见 OpenApiBroker.deals_today（需要 futu_channel=openapi）。"""
        raise OpenApiUnavailable(f"deals_today 需要 OpenAPI 交易通道；{OPENAPI_ONLY_HINT}")

    def deals_history(self, payload, mode=None):
        """见 OpenApiBroker.deals_history（需要 futu_channel=openapi）。"""
        raise OpenApiUnavailable(f"deals_history 需要 OpenAPI 交易通道；{OPENAPI_ONLY_HINT}")


# ---------------------------------------------------------------------------
# WP8 任务 3：OpenAPI 交易适配器（live 写 + live 查询走 REST；sim/MCP 委托 FutuBroker）
# ---------------------------------------------------------------------------
# OpenApiTrade 类的惰性单例（只读枚举常量的单一源；避免在模块导入期拉起 cryptography）。
_TRADE_CLS = None


def _trade_class():
    """``trading_datasource.futu_openapi.OpenApiTrade`` 类（惰性导入）。"""
    global _TRADE_CLS
    if _TRADE_CLS is None:
        from trading_datasource.futu_openapi import OpenApiTrade as cls  # noqa: PLC0415
        _TRADE_CLS = cls
    return _TRADE_CLS


def _order_id(value):
    """从信封 d 里取订单号（place_order / order_confirm 都返回 {order_id: ...}）。"""
    if isinstance(value, dict):
        order_id = value.get("order_id")
        return None if order_id in (None, "") else str(order_id)
    return None


def _err_text(error, limit=160):
    """OpenAPI 错误 → 面向调用方的一句话（errcode 有值就带上）。"""
    errcode = getattr(error, "errcode", None)
    errmsg = getattr(error, "errmsg", None) or str(error)
    return f"errcode={errcode}：{str(errmsg)[:limit]}" if errcode is not None \
        else str(errmsg)[:limit]


def _classify_openapi_error(error, label):
    """OpenAPI 写路径异常 → 结果 dict；非 OpenAPI 异常返回 ``None``（调用方原样上抛）。

    分类顺序与官方语义一一对应（惰性导入异常类：platform 服务默认不拉 cryptography）：
      * ``OrderConfirmRequired``（need_order_confirm=true）→ ``{"need_confirm": exc}``：
        订单在券商侧**已挂起**，必须走 order_confirm，**绝不重发下单**；
      * ``TransportError`` → ``unknown``：请求可能已到达券商（铁律：先查询，不重放）；
      * ``UnexpectedResponse``（5xx/429/非 JSON/非信封）→ **``unknown``**：服务端没给出
        业务结论，请求同样可能已到达券商——若当 ``rejected``（终态、无出边）会把可能已
        成交的单直接丢掉，与「先查询不重放」冲突；429 的 ``Retry-After`` 原值同时进
        消息与返回（信封 value.retry_after），供上层退避；
      * 其余 ``OpenApiError``（``s==error`` / ``ret_code!=0`` 的业务信封）→ ``rejected``：
        券商明确未接受本单——这是唯一可以落终态的情形。
    """
    from trading_datasource.futu_openapi import (  # noqa: PLC0415
        OpenApiError, OrderConfirmRequired, TransportError, UnexpectedResponse)
    if isinstance(error, OrderConfirmRequired):
        return {"need_confirm": error}
    if isinstance(error, TransportError):
        return {"status": "unknown",
                "err": f"{label}传输异常：{_err_text(error)}（先查询，不重放）"}
    if isinstance(error, UnexpectedResponse):
        retry_after = getattr(error, "retry_after", None)
        status = getattr(error, "errcode", None)
        detail = _err_text(error)
        if retry_after:
            detail = f"{detail}；Retry-After={retry_after}"
        return {"status": "unknown",
                "err": f"{label}响应状态未知（HTTP {status if status is not None else 'n/a'}）："
                       f"未知状态：先查询订单，勿重放（{detail}）",
                **({"retry_after": str(retry_after)} if retry_after else {})}
    if isinstance(error, OpenApiError):
        return {"status": "rejected", "err": f"{label}被拒：{_err_text(error)}"}
    return None


class OpenApiBroker:
    """WP8 任务 3 的通道路由适配器：OpenAPI REST（live）+ WP7 FutuBroker（sim/MCP）。

    ``FutuBroker`` 接口的子集超集，闸门（TradeGate）看到的契约完全一致（place/modify/
    cancel/positions/orders/funds），另加 6 个 OpenAPI 只读方法。分流规则（逐条披露）：

    * ``channel != openapi`` → 一切照旧走 FutuBroker（含 live 写的 _live_guard 拒绝）；
    * ``channel == openapi`` 且 ``mode == live`` → live 写与 live 账户查询走 REST；
    * ``mode == sim`` → **始终**走 FutuBroker 的 sim_trade_* 路径：官方 REST 交易面
      只覆盖实盘业务账户（``/accounts/authorized_trd_accs`` 返回实盘账户；模拟账户在
      另一套 sim-trade 端点里，本任务不接），因此 sim 语义不因通道切换而改变；
    * ``channel == openapi`` 但凭据缺失 → ``supports_live_write`` 为 False，live 写在
      **确认之前**被闸门拒绝（WP7 的 trading/broker-unavailable，确认零调用）；
      live 只读查询则抛 OpenApiUnavailable（trading/openapi-unavailable，指引授权），
      **不静默回退 MCP**（与 server/futu_data.py 的行情通道同一取舍）。

    注入口径与 futu_data.FutuData 一致：注入 ``trade`` 替身即钉住 openapi 通道（测试
    确定性），注入 ``call`` 给委托的 FutuBroker（MCP 替身）。
    """

    def __init__(self, home=None, call=None, trade=None, channel=None,
                 credential_path=None, legacy=None):
        self.home = home
        self._trade_override = trade
        self._credential_path = credential_path
        self._legacy = legacy if legacy is not None else FutuBroker(call=call)
        self._trade_built = None
        # 通道钉死口径与 futu_data.FutuData 一致：注入 trade 替身 → openapi，
        # 注入 call 替身（MCP）→ mcp，二者都没有才惰性读 futu_channel 配置。
        if channel is not None:
            self._channel = channel
        elif trade is not None:
            self._channel = futu_data.CHANNEL_OPENAPI
        elif call is not None:
            self._channel = futu_data.CHANNEL_MCP
        else:
            self._channel = None

    # ---- 通道层 ----
    @property
    def channel(self):
        if self._channel is None:
            self._channel = futu_data.load_channel(self.home)
        return self._channel

    @property
    def trade(self):
        """OpenAPI 交易后端（注入替身优先；缺省 OpenApiTrade + 默认凭据路径）。"""
        if self._trade_override is not None:
            return self._trade_override
        if self._trade_built is None:
            from trading_datasource.futu_openapi import (  # noqa: PLC0415
                CredentialStore, OpenApiClient, OpenApiTrade)
            self._trade_built = OpenApiTrade(
                OpenApiClient(CredentialStore(self._credential_path)))
        return self._trade_built

    def _openapi_ready(self):
        """channel=openapi 时凭据是否可用（注入 trade 替身视为可用）。"""
        if self._trade_override is not None:
            return True
        return futu_data.openapi_ready(self._credential_path)

    @property
    def supports_live_write(self):
        """动态能力：凭据可能后配/失效，因此是属性而不是类常量（闸门每次现读）。"""
        return self.channel == futu_data.CHANNEL_OPENAPI and self._openapi_ready()

    def _route(self, mode):
        """返回 "openapi" | "legacy"；openapi 通道缺凭据时抛 OpenApiUnavailable。"""
        if self.channel != futu_data.CHANNEL_OPENAPI or mode != "live":
            return "legacy"
        if not self._openapi_ready():
            raise OpenApiUnavailable(
                f"OpenAPI 凭据缺失（~/.dsh/futu-openapi.json）{OPENAPI_AUTH_HINT}")
        return "openapi"

    def _read_route(self, name, mode):
        """6 个只读新工具的通道判定（mcp/sim/无凭据 → 如实拒绝，不静默回退 MCP）。"""
        if self.channel != futu_data.CHANNEL_OPENAPI:
            raise OpenApiUnavailable(f"{name} 需要 OpenAPI 交易通道；{OPENAPI_ONLY_HINT}")
        if mode != "live":
            raise OpenApiUnavailable(
                f"{name} 只覆盖实盘业务账户（OpenAPI 交易接口不含模拟账户）；"
                "sim 模式请用 account_positions/account_orders/account_funds")
        if not self._openapi_ready():
            raise OpenApiUnavailable(
                f"OpenAPI 凭据缺失（~/.dsh/futu-openapi.json）{OPENAPI_AUTH_HINT}")
        return True

    # ---- 账户解析（OpenAPI 面）----
    def _accounts(self):
        """授权交易账户列表（Account 对象，含 account_id/enable_market）。"""
        data = self.trade.authorized_accounts()
        accounts = data.get("accounts") if isinstance(data, dict) else data
        return [account for account in (accounts or [])
                if isinstance(account, dict) and account.get("account_id")]

    @staticmethod
    def _enable_codes(account):
        """账户的 enable_market 归一成 int 集合（通道可能给 int 或数字字符串）。"""
        codes = set()
        for value in account.get("enable_market") or []:
            try:
                codes.add(int(value))
            except (TypeError, ValueError):
                continue
        return codes

    def _markets_of(self, account):
        """账户可交易的 trd_market 列表（enable_market 数字 → 官方 trd_market 名）。"""
        return [ENABLE_MARKET_TO_TRD[code] for code in sorted(self._enable_codes(account))
                if code in ENABLE_MARKET_TO_TRD]

    def _enable_of_symbol(self, symbol):
        """标的 → 所需 enable_market 数字（前缀无法识别即拒绝，零网络往返）。"""
        prefix = str(symbol or "").strip().upper().split(".", 1)[0]
        want = OPENAPI_ENABLE_MARKET.get(prefix)
        if want is None:
            raise ValueError(f"标的市场前缀无法识别：{symbol!r}（支持 SH/SZ/BJ/HK/US）")
        return prefix, want

    def _account_for(self, symbol):
        """按标的市场挑授权账户（第一个 enable_market 命中的账户；多账户口径见 note）。"""
        _prefix, want = self._enable_of_symbol(symbol)
        for account in self._accounts():
            if want in self._enable_codes(account):
                return str(account["account_id"])
        raise BrokerUnavailable(
            f"没有可交易 {symbol} 市场的授权账户（enable_market 未含 {want}）；"
            "先在富途开通对应市场的交易账户")

    def _per_account(self, route):
        """逐账户直通（单账户失败不掩盖）：groups 与 errors 各归其位。"""
        groups, errors = [], []
        for account in self._accounts():
            acc_id = str(account["account_id"])
            try:
                groups.append({"acc_id": acc_id, **route(acc_id, account)})
            except ValueError:
                raise  # 参数类错误对所有账户一致 → 由闸门信封成 invalid-operation
            except Exception as error:  # noqa: BLE001 —— 单账户失败不掩盖
                errors.append({"acc_id": acc_id, "reason": str(error)[:160]})
        return groups, errors

    def _per_market(self, payload, name, route):
        """按 payload['market']（trd_market）挑账户并逐账户调 route(acc_id, market)。"""
        market = payload.get("market")
        if not isinstance(market, str) or not market.strip():
            raise ValueError(f"{name} 需要 market（trd_market 枚举，如 HK/US/HKCC）")
        market = market.strip().upper()
        if market not in _trade_class().TRD_MARKETS:
            raise ValueError(f"market 取值非法：{market!r}"
                             f"（允许：{sorted(_trade_class().TRD_MARKETS)}）")
        groups, errors = [], []
        for account in self._accounts():
            if market not in self._markets_of(account):
                continue
            acc_id = str(account["account_id"])
            try:
                groups.append({"acc_id": acc_id, "market": market, **route(acc_id, market)})
            except ValueError:
                raise
            except Exception as error:  # noqa: BLE001
                errors.append({"acc_id": acc_id, "market": market,
                               "reason": str(error)[:160]})
        if not groups and not errors:
            errors.append({"acc_id": None, "market": market,
                           "reason": f"没有启用 {market} 市场的授权账户"})
        return groups, errors

    # ---- 写路径（live → REST；sim/mcp → FutuBroker）----
    def place(self, order, mode):
        if self._route(mode) == "legacy":
            return self._legacy.place(order, mode)
        trade = self.trade
        acc_id = self._account_for(order["symbol"])
        # 官方扩展字段整体透传（WP8 任务 6）：order_type/time_in_force 恒有值（缺省
        # LIMIT/DAY），其余只在调用方给了才下发——不凭默认值改写券商语义，也不丢字段。
        kwargs = {"acc_id": acc_id, "code": order["symbol"], "qty": order["qty"],
                  "side": order["side"],
                  "order_type": order.get("order_type", "LIMIT"),
                  "time_in_force": order.get("time_in_force", "DAY")}
        if "price" in order:
            kwargs["price"] = order["price"]
        for name in ("session", "aux_price", "lot_type", "remark", "order_class",
                     "multi_leg_info"):
            if name in order:
                kwargs[name] = order[name]
        try:
            data = trade.place_order(**kwargs)
        except Exception as error:  # noqa: BLE001 —— 分类见 _classify_openapi_error
            outcome = _classify_openapi_error(error, "下单")
            if outcome is None:
                raise
            if "need_confirm" in outcome:
                return self._confirm(trade, acc_id, outcome["need_confirm"], None, "place")
            return {"acc_id": acc_id, **outcome}
        return {"status": "submitted", "broker_order_id": _order_id(data),
                "acc_id": acc_id, "confirmed": False, "raw": data}

    def modify(self, order, mode):
        if self._route(mode) == "legacy":
            return self._legacy.modify(order, mode)
        symbol = order["symbol"]
        prefix = str(symbol).strip().upper().split(".", 1)[0]
        exchange = OPENAPI_EXCHANGE.get(prefix)
        if exchange is None:
            return {"status": "rejected", "broker_order_id": None,
                    "err": f"官方 OpenAPI exchange 枚举不含 {prefix}（{symbol}）："
                           "撤单/改单暂不支持该市场"}
        if prefix in A_SHARE_PREFIXES:
            # 官方 modify-order 页明示不支持改 A 股订单 → 如实拒绝 + 给替代路径（不静默
            # 换成「撤旧重下」：那会把一次批准变成两笔真实委托，超出用户批准的语义）。
            return {"status": "rejected", "broker_order_id": None,
                    "err": "官方 OpenAPI 改单不支持 A 股（SSE/SZSE 沪深港通）；"
                           "请先 trade_cancel 再 trade_place（两笔独立批准）"}
        trade = self.trade
        acc_id = self._account_for(symbol)
        # WP8 任务 6：官方 PUT 改单的辅助字段 aux_price（触发价）整体透传——对在富途客户端
        # 建的非限价单（STOP/STOP_LIMIT/MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED）必须能改触发价；
        # 官方改单请求体**没有** order_type 字段，故这里不做类型语义。
        kwargs = {"acc_id": acc_id, "order_id": order["order_id"], "exchange": exchange,
                  "qty": order["qty"], "price": order["price"]}
        if "aux_price" in order:
            kwargs["aux_price"] = order["aux_price"]
        try:
            data = trade.modify_order(**kwargs)
        except Exception as error:  # noqa: BLE001
            outcome = _classify_openapi_error(error, "改单")
            if outcome is None:
                raise
            if "need_confirm" in outcome:
                return self._confirm(trade, acc_id, outcome["need_confirm"], None, "modify",
                                     fallback_order_id=order["order_id"])
            return {"acc_id": acc_id, **outcome}
        return {"status": "submitted", "broker_order_id": order["order_id"],
                "acc_id": acc_id, "confirmed": False, "raw": data}

    def cancel(self, order, mode):
        if self._route(mode) == "legacy":
            return self._legacy.cancel(order, mode)
        symbol = order["symbol"]
        prefix = str(symbol).strip().upper().split(".", 1)[0]
        exchange = OPENAPI_EXCHANGE.get(prefix)
        if exchange is None:
            return {"status": "rejected", "order_id": order["order_id"],
                    "err": f"官方 OpenAPI exchange 枚举不含 {prefix}（{symbol}）："
                           "撤单暂不支持该市场"}
        trade = self.trade
        acc_id = self._account_for(symbol)
        try:
            data = trade.cancel_order(acc_id=acc_id, order_id=order["order_id"],
                                      exchange=exchange)
        except Exception as error:  # noqa: BLE001
            outcome = _classify_openapi_error(error, "撤单")
            if outcome is None:
                raise
            if "need_confirm" in outcome:
                # 官方撤单页未列 need_order_confirm（**未实测的防御分支**）；若券商如此返回，
                # 仍按「用户已在卡片批准本操作」自动确认（同一批准覆盖同一操作，不额外打扰
                # 用户）。真实通道若出现该分支，先以查单复核再定策略。
                return self._confirm(trade, acc_id, outcome["need_confirm"], None, "cancel",
                                     fallback_order_id=order["order_id"])
            return {"order_id": order["order_id"], **outcome}
        return {"status": "cancelled", "order_id": order["order_id"], "acc_id": acc_id,
                "raw": data}

    def _confirm(self, trade, acc_id, need, placed, operation, fallback_order_id=None):
        """券商侧二次确认（两层确认合一的第二层）——**只调 order_confirm，绝不重发下单**。

        用户已在 Web 卡片批准本单参数（业务确认），因此这里自动完成券商确认。**任何**异常
        （业务信封/传输/非信封/意外异常）一律落 ``unknown``：订单在券商侧**已挂起**
        （place/modify）且 confirm 结果不确定，铁律「先查询不重放」；对 confirm 而言
        ``unknown`` 永远比 ``rejected`` 诚实（后者会让 OMS 丢掉一张可能已生效的单）。
        confirm_id 缺失同样 unknown（不猜、不重发）。
        """
        confirm_id = getattr(need, "confirm_id", None)
        base = {"acc_id": acc_id, "broker_order_id": _order_id(placed)
                or fallback_order_id, "confirm_id": confirm_id, "need_order_confirm": True}
        if not confirm_id:
            return {**base, "status": "unknown",
                    "err": f"{operation} 需券商二次确认但信封未带 confirm_id："
                           "订单在券商侧挂起待确认（先查单，勿重发下单）"}
        try:
            confirmed = trade.order_confirm(acc_id=acc_id, confirm_id=confirm_id)
        except Exception as error:  # noqa: BLE001 —— confirm 任何异常一律 unknown（更诚实）
            from trading_datasource.futu_openapi import TransportError  # noqa: PLC0415
            kind = "传输异常" if isinstance(error, TransportError) else "失败"
            return {**base, "status": "unknown",
                    "err": f"券商二次确认{kind}：{_err_text(error)}"
                           "（订单在券商侧挂起/可能已生效；先查单，勿重发下单）"}
        return {**base, "status": "submitted",
                "broker_order_id": _order_id(confirmed) or base["broker_order_id"],
                "confirmed": True, "raw": confirmed}

    # ---- 账户查询（live → REST；sim/mcp → FutuBroker）----
    def positions(self, mode):
        if self._route(mode) == "legacy":
            return self._legacy.positions(mode)
        groups, errors = self._per_account(
            lambda acc_id, account: {
                "market": account.get("enable_market"),
                "positions": self.trade.positions(acc_id) or []})
        groups = [group for group in groups if group["positions"]]
        return {"mode": mode, "source": "futu/openapi:positions", "as_of": _iso_now(),
                "groups": groups, "errors": errors,
                "note": "券商返回的真实持仓（OpenAPI REST；按账户列出，不跨账户/币种合并）；"
                        "失败账户列入 errors，不掩盖。"}

    def orders(self, mode):
        """订单历史：OpenAPI 的 ``/orders_history`` 必填 trd_market → 按「账户×市场」列。

        不带 start/end（官方组合语义 0/0 = 近 90 天）；每账户每市场取第一页（page_size=100），
        并把 page_flag/completed 一并给出，需要翻页用 orders_history 工具。
        """
        if self._route(mode) == "legacy":
            return self._legacy.orders(mode)
        groups, errors = [], []
        for account in self._accounts():
            acc_id = str(account["account_id"])
            markets = self._markets_of(account)
            if not markets:
                errors.append({"acc_id": acc_id,
                               "reason": "账户未声明可交易市场（enable_market）"})
                continue
            for market in markets:
                try:
                    data = self.trade.history_orders(acc_id, market, page_flag="",
                                                    page_size=100) or {}
                except Exception as error:  # noqa: BLE001 —— 单账户单市场失败不掩盖
                    errors.append({"acc_id": acc_id, "market": market,
                                   "reason": str(error)[:160]})
                    continue
                groups.append({"acc_id": acc_id, "market": market,
                               "rows": data.get("orders") or [],
                               "page_flag": data.get("page_flag"),
                               "completed": data.get("completed")})
        return {"mode": mode, "source": "futu/openapi:orders_history",
                "as_of": _iso_now(), "window": {"start": None, "end": None},
                "groups": groups, "errors": errors,
                "note": "订单历史按账户×市场列出（OpenAPI 默认窗口：服务端 90 天；"
                        "不带 start/end 即官方 0/0 组合语义）。"}

    def funds(self, mode):
        if self._route(mode) == "legacy":
            return self._legacy.funds(mode)
        groups, errors = self._per_account(
            lambda acc_id, account: {
                "market": account.get("enable_market"),
                "cash": self.trade.account_funds(acc_id)})
        return {"mode": mode, "source": "futu/openapi:funds", "as_of": _iso_now(),
                "groups": groups, "errors": errors,
                "note": "账户资金（OpenAPI REST；currency 未指定时官方按账户默认币种返回）"}

    # ---- WP8 任务 3：6 个只读端点（OpenAPI REST 直通）----
    def _read_envelope(self, mode, source, groups, errors, note):
        return {"mode": mode, "source": source, "as_of": _iso_now(),
                "groups": groups, "errors": errors, "note": note}

    def trade_max_qty(self, payload, mode):
        """GET /accounts/{acc_id}/acctradinginfo —— 最大可交易量（逐账户列出）。"""
        self._read_route("trade_max_qty", mode)
        symbol = payload.get("code")
        prefix, want = self._enable_of_symbol(symbol)
        code = str(symbol).strip().upper()
        groups, errors = [], []
        for account in self._accounts():
            if want not in self._enable_codes(account):
                continue
            acc_id = str(account["account_id"])
            try:
                data = self.trade.max_trade_qty(acc_id, code, payload.get("order_type"),
                                                price=payload.get("price"),
                                                order_id=payload.get("order_id"))
            except ValueError:
                raise
            except Exception as error:  # noqa: BLE001
                errors.append({"acc_id": acc_id, "reason": str(error)[:160]})
                continue
            groups.append({"acc_id": acc_id, "market": prefix, "code": code,
                           "max": data or {}})
        if not groups and not errors:
            errors.append({"acc_id": None,
                           "reason": f"没有可交易 {prefix} 市场的授权账户"})
        return self._read_envelope(
            mode, "futu/openapi:acctradinginfo", groups, errors,
            "最大可交易量（OpenAPI REST；max_cash_buy/max_position_sell 等原始字段）；"
            "带 order_id 时查该订单的最大可改数量（官方要求两次查询间隔 > 0.5s）。")

    def orders_open(self, payload, mode):
        """GET /accounts/{acc_id}/orders —— 未完成订单（含最近 24h 已成交/已撤）。"""
        self._read_route("orders_open", mode)
        groups, errors = self._per_market(
            payload, "orders_open",
            lambda acc_id, market: self._paged(self.trade.open_orders, acc_id, market,
                                               payload, "orders"))
        return self._read_envelope(
            mode, "futu/openapi:orders", groups, errors,
            "未完成订单（OpenAPI REST；含最近 24 小时已成交/已撤单）；分页游标 page_flag，"
            "completed=true 表示本批已取尽。")

    def orders_history(self, payload, mode):
        """GET /accounts/{acc_id}/orders_history —— 历史订单（start/end 微秒）。"""
        self._read_route("orders_history", mode)
        groups, errors = self._per_market(
            payload, "orders_history",
            lambda acc_id, market: self._paged(self.trade.history_orders, acc_id, market,
                                               payload, "orders",
                                               filters=("code", "start", "end")))
        return self._read_envelope(
            mode, "futu/openapi:orders_history", groups, errors,
            "历史订单（OpenAPI REST；start/end 为创建时间微秒时间戳，官方 0/0 组合=近 90 天）"
            "；分页游标 page_flag。")

    def orders_detail(self, payload, mode):
        """POST /accounts/{acc_id}/orders/detail —— 订单详情（逐账户问同一批订单号）。"""
        self._read_route("orders_detail", mode)
        groups, errors = self._per_account(
            lambda acc_id, account: {
                "rows": self.trade.order_details(
                    acc_id, payload.get("exchange"), payload.get("order_ids")) or []})
        return self._read_envelope(
            mode, "futu/openapi:orders_detail", groups, errors,
            "订单详情（OpenAPI REST；同一批 order_ids 需属于同一 exchange）；"
            "授权账户逐个查询，订单出现在持有它的那个账户分组里。")

    def deals_today(self, payload, mode):
        """GET /accounts/{acc_id}/order_fills —— 当日成交。"""
        self._read_route("deals_today", mode)
        groups, errors = self._per_market(
            payload, "deals_today",
            lambda acc_id, market: self._paged(self.trade.today_deals, acc_id, market,
                                               payload, "order_fills"))
        return self._read_envelope(
            mode, "futu/openapi:order_fills", groups, errors,
            "当日成交（OpenAPI REST；order_fills 原始字段 trd_side/deal_id/qty/price 等）。")

    def deals_history(self, payload, mode):
        """GET /accounts/{acc_id}/fills_history —— 历史成交（page_size 上界 50）。"""
        self._read_route("deals_history", mode)
        groups, errors = self._per_market(
            payload, "deals_history",
            lambda acc_id, market: self._paged(self.trade.history_deals, acc_id, market,
                                               payload, "order_fills",
                                               filters=("code", "start", "end")))
        return self._read_envelope(
            mode, "futu/openapi:fills_history", groups, errors,
            "历史成交（OpenAPI REST；start/end 为更新时间微秒时间戳，page_size 上界 50）。")

    def _paged(self, call, acc_id, market, payload, rows_key, filters=()):
        """分页查询的公共取数形状：rows + page_flag + completed（缺一不伪造）。

        ``filters`` 是该端点支持的官方可选过滤字段（orders_history/fills_history 的
        code/start/end）；只在调用方真的给了值时才下传，省略字段由官方按缺省处理。
        """
        kwargs = {"page_flag": payload.get("page_flag", ""),
                  "page_size": payload.get("page_size")}
        for name in filters:
            if payload.get(name) is not None:
                kwargs[name] = payload[name]
        data = call(acc_id, market, **kwargs) or {}
        return {"rows": data.get(rows_key) or [],
                "page_flag": data.get("page_flag"),
                "completed": data.get("completed")}


def default_broker(home=None):
    """TradeGate 的默认适配器：``futu_channel=openapi`` → OpenApiBroker，否则 WP7 FutuBroker。

    默认（未配置 openapi 通道/凭据）返回的仍是 **原样的 FutuBroker**：live 写先置拒绝、
    sim 走 sim_trade_* 工具、live 查询走 account_* 工具——WP7 行为逐字不变（不变式 2）。
    """
    if futu_data.load_channel(home) == futu_data.CHANNEL_OPENAPI:
        return OpenApiBroker(home=home)
    return FutuBroker()



# ---------------------------------------------------------------------------
# 交易闸门
# ---------------------------------------------------------------------------
class TradeGate:
    """受约束交易闸门：模式 → 风控 8 规则（含 kill）→ 业务确认 → broker。

    依赖注入（构造参数，测试全部可替换）：
      * broker       —— place/modify/cancel/positions/orders/funds 接口
                        （缺省 ``default_broker(home)``：futu_channel=openapi 时是
                        OpenApiBroker（live 走 REST），否则是 WP7 的 FutuBroker——
                        trading_core.broker + futu_mcp.call_tool）；
      * confirm      —— 确认三方法绑定（缺省 StoreConfirm = store_access）；
      * risk_fn      —— 缺省 trading_core.risk.pre_trade_checks（8 规则唯一实现）；
      * ctx_builder  —— risk ctx 构造（缺省 default_ctx_builder，披露见其注释）；
      * conn_factory —— OMS 连接（缺省 home/trading-data/trading.sqlite）；
      * confirm_ttl_ms —— 业务确认 TTL（缺省 store_access.CONFIRM_TTL_MS=120_000，
                        与 agent.cordis.yml 的 quant-platform-mcp 行 toolCallTimeoutMs
                        同值：MCP 调用在 preset 层 120s 先超时、闸门继续等到 TTL 到期
                        按拒绝收尾，模型侧看到超时但订单**不会**在无批准下提交；
                        preset 行已调为 180000（WP7 任务 5），作答余量充足）。
    """

    def __init__(self, home, broker=None, confirm=None, risk_fn=None, ctx_builder=None,
                 conn_factory=None, confirm_ttl_ms=None, today=None):
        self.home = str(home)
        self.broker = broker if broker is not None else default_broker(self.home)
        self.confirm = confirm if confirm is not None else StoreConfirm()
        self.risk_fn = risk_fn if risk_fn is not None else core_risk.pre_trade_checks
        self.ctx_builder = ctx_builder if ctx_builder is not None else default_ctx_builder
        self._conn_factory = conn_factory
        self.confirm_ttl_ms = (store_access.CONFIRM_TTL_MS if confirm_ttl_ms is None
                               else confirm_ttl_ms)
        self.today = today

    # ---- 内部 ----
    def _conn(self):
        if self._conn_factory is not None:
            return self._conn_factory()
        return core_store.connect(core_store.db_path(self.home))

    def _today(self):
        return self.today or date.today().isoformat()

    def _risk_basis(self, conn, clean):
        """风险基准价 → ``(price, error_message)``（WP8 任务 6）。

        优先级与依据：
          1. ``price``（限价类）/ ``aux_price``（触发类）——调用方给出的成交或触发价；
          2. 市价类（MARKET/AUCTION，请求体没有价格）→ **本地日线最近收盘**
             （``daemon._execute_plan`` 的 ``price_of`` 同款口径：已同步的真事实）；
          3. 都取不到 → fail-closed：返回可读错误（``NO_PRICE_BASIS_REFUSAL``），
             绝不用 0 让风控规则 4/5 静默失效，也绝不编造价。
        """
        basis = _risk_price(clean)
        if basis is not None:
            return float(basis), None
        close = _local_close(conn, clean["symbol"], self._today())
        if close is not None:
            return close, None
        if clean.get("order_type") in NO_PRICE_ORDER_TYPES:
            return None, NO_PRICE_BASIS_REFUSAL.format(
                order_type=clean.get("order_type"), symbol=clean["symbol"])
        # 理论上不可达（限价类/触发类的基准价在字段校验层已强制存在）：字段层若被绕过，
        # 这里同样 fail-closed，绝不让风控拿到 None/0。
        return None, (f"参数非法：order_type={clean.get('order_type')} 缺少风险基准价"
                      f"（price/aux_price），闸门无法评估单笔风险与单票市值")

    # ---- 查询（mode 约束直通，不进任何缓存）----
    def positions(self, mode=None):
        return self._query("positions", mode)

    def orders(self, mode=None):
        return self._query("orders", mode)

    def funds(self, mode=None):
        return self._query("funds", mode)

    def _query(self, name, mode):
        try:
            if mode is None:
                mode = store_access.read_mode(self.home)
            else:
                store_access.mode_value(mode)
        except WorkbenchError as error:
            return _envelope_fail("trading/invalid-operation", f"账户模式非法：{error}")
        try:
            value = getattr(self.broker, name)(mode)
        except OpenApiUnavailable as error:
            # OpenAPI 通道缺失（channel=openapi 但没凭据）：指引去授权，不冒充通道异常
            return _envelope_fail(OPENAPI_UNAVAILABLE_CODE, str(error))
        except Exception as error:  # noqa: BLE001 —— 信封化，绝不 500
            return _envelope_fail("trading/broker-unavailable", f"券商通道异常：{error}")
        return _envelope_ok(value)

    # ---- WP8 任务 3：OpenAPI 交易只读路径（mode 约束 + REST 直通，不进任何缓存）----
    # 端点名 = 工具名 = 本类方法名 = broker 方法名（同一字符串，便于逐项对照锁定测试）；
    # 载荷里的业务参数（code/market/exchange/page_flag/...）整体下传，mode 单独走模式约束。
    def trade_max_qty(self, payload=None):
        return self._read("trade_max_qty", payload)

    def orders_open(self, payload=None):
        return self._read("orders_open", payload)

    def orders_history(self, payload=None):
        return self._read("orders_history", payload)

    def orders_detail(self, payload=None):
        return self._read("orders_detail", payload)

    def deals_today(self, payload=None):
        return self._read("deals_today", payload)

    def deals_history(self, payload=None):
        return self._read("deals_history", payload)

    def _read(self, name, payload):
        """OpenAPI 只读工具的闸门：模式约束（模式文件或载荷 mode）→ broker 只读方法。

        与 ``_query`` 的差别只有两点：载荷带业务参数（整体下传），以及三类失败分别映射
        为 ``trading/openapi-unavailable``（通道缺失/需授权）、``trading/invalid-operation``
        （broker 侧参数白名单，坏参数零网络往返）与 ``trading/broker-unavailable``（其余）。
        读操作不落 OMS、不做幂等、不进缓存（与 account_* 同类）。
        """
        payload = payload if isinstance(payload, dict) else {}
        mode = payload.get("mode")
        try:
            if mode is None:
                mode = store_access.read_mode(self.home)
            else:
                store_access.mode_value(mode)
        except WorkbenchError as error:
            return _envelope_fail("trading/invalid-operation", f"账户模式非法：{error}")
        args = {key: value for key, value in payload.items() if key != "mode"}
        try:
            value = getattr(self.broker, name)(args, mode)
        except OpenApiUnavailable as error:
            return _envelope_fail(OPENAPI_UNAVAILABLE_CODE, str(error))
        except ValueError as error:
            return _envelope_fail("trading/invalid-operation", str(error))
        except Exception as error:  # noqa: BLE001 —— 信封化，绝不 500
            return _envelope_fail("trading/broker-unavailable", f"券商通道异常：{error}")
        return _envelope_ok(value)

    # ---- 写路径 ----
    def place(self, order, session_id="mcp"):
        return self._write("place", order, session_id)

    def modify(self, order, session_id="mcp"):
        return self._write("modify", order, session_id)

    def cancel(self, order, session_id="mcp"):
        return self._write("cancel", order, session_id)

    def _write(self, operation, payload, session_id):
        # 1) 字段校验（broker/确认零接触）。WP8 任务 6：字段/枚举/条件必填/互斥属于
        #    **载荷非法**（与 HTTP/MCP 层白名单同码族）→ trading/invalid-operation，
        #    消息带官方允许值；风控/确认/券商拒绝仍是 trading/order-rejected。
        clean, message = validate_order(payload, operation)
        if message is not None:
            return _envelope_fail("trading/invalid-operation", message)
        # 2) 模式：只认模式文件（载荷不带 mode，杜绝声明模式与账户模式不一致的旁路；
        #    风控规则 2 在闸门内的形态即「模式文件合法且与订单一致」——订单模式由这里
        #    统一赋值，二者恒等）。
        try:
            mode = store_access.read_mode(self.home)
        except WorkbenchError as error:
            return _envelope_fail("trading/order-rejected", f"账户模式非法：{error}")
        # 2.5) live 写前置拒绝：适配器未声明 supports_live_write 时，确认通过后也注定
        #      被 FutuBroker._live_guard 拒绝——在确认之前快速失败，不发起确认、不产生
        #      待确认、不落 OMS/风控行。getattr 对未声明该属性的适配器按「不支持」处理
        #      （fail-safe：没显式声明支持 live 写，就不得写 live）。
        if mode == "live" and not getattr(self.broker, "supports_live_write", False):
            return _envelope_fail("trading/broker-unavailable", LIVE_WRITE_UNAVAILABLE)
        # 2.6) sim 通道能力边界（WP8 任务 6）：sim_trade_place_order/modify 只支持限价当日单
        #      （core_broker.place 的 order_type=1 锁定口径），扩展字段在 sim 下**如实拒绝**
        #      ——不静默丢弃、不落 OMS/风控行、零券商调用（适配层还有第二道守卫）。
        if mode == "sim":
            unsupported = _sim_unsupported(clean)
            if unsupported:
                return _envelope_fail("trading/broker-unavailable", SIM_LIMIT_REFUSAL,
                                      fields=unsupported)
        # 3) 幂等编号：调用方提供则复用（重复提交不重复下单），否则生成（OMS uuid 口径）
        cid = clean.get("client_order_id") or uuid.uuid4().hex
        conn = self._conn()
        try:
            return self._write_gated(conn, operation, clean, mode, cid, session_id)
        finally:
            conn.close()

    def _write_gated(self, conn, operation, clean, mode, cid, session_id):
        # 幂等（OMS：client_order_id 主键）：已登记的编号直接回放登记结论，
        # 不再过确认、不再触达 broker。
        existing = conn.execute(
            "SELECT status, broker_order_id FROM orders WHERE client_order_id=?",
            (cid,)).fetchone()
        if existing is not None:
            return _envelope_ok({"client_order_id": cid, "status": existing["status"],
                                 "broker_order_id": existing["broker_order_id"],
                                 "duplicate": True})
        # cancel 不产生新订单（无可执行物），无 OMS 行可登记——其重复提交由券商侧
        # 「撤已撤单」语义兜底，这里不伪造幂等（如实披露）。
        has_row = operation in ("place", "modify")
        # 3.5) 风险基准价（WP8 任务 6）：限价类=price、触发类=aux_price、市价类
        #      （MARKET/AUCTION）=本地日线最近收盘（daemon._execute_plan 的 price_of 同款）。
        #      取不到就 fail-closed 拒绝——**不落 OMS/风控行、零券商调用**；绝不用 0 让
        #      规则 4/5 静默失效，也绝不编造价。放在重复提交回放之后（幂等优先）。
        risk_price = None
        if operation != "cancel":
            risk_price, message = self._risk_basis(conn, clean)
            if message is not None:
                return _envelope_fail("trading/invalid-operation", message)
        if has_row:
            # OMS 幂等三件套之二（oms.register_order 同款查重，adhoc 槽）：同标的同方向
            # 在途临时单拒绝重复登记。
            dup = conn.execute(
                "SELECT 1 FROM orders WHERE plan_id=? AND symbol=? AND side=? AND "
                f"status IN ({','.join('?' * len(core_oms.OPEN_STATES))})",
                (ADHOC_PLAN_ID, clean["symbol"], clean["side"], *core_oms.OPEN_STATES)
            ).fetchone()
            if dup is not None:
                return _envelope_fail(
                    "trading/order-rejected",
                    f"{clean['symbol']} {clean['side']} 已有在途临时单（OMS 在途查重）",
                    client_order_id=cid)
            # 登记 draft（列集与 oms.register_order 一致；cid 支持调用方指定——
            # register_order 只生成 uuid，调用方幂等编号需要主键直插）。
            now = _now()
            conn.execute(
                "INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,"
                "status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cid, ADHOC_PLAN_ID, clean["symbol"], clean["symbol"].split(".", 1)[0],
                 clean["side"], clean.get("qty", 0), clean.get("price"), "draft", mode,
                 now, now))
            conn.commit()

        # 4) 风控 8 规则（kill 是规则 1）——拒绝即跳过并留痕，无覆盖按钮
        order = _check_order(operation, clean, mode, risk_price)
        ctx = self.ctx_builder(conn, mode, order, operation, self.home, self._today())
        verdict = self.risk_fn(order, ctx)
        core_store.insert_risk_check(conn, ADHOC_PLAN_ID, clean["symbol"], verdict.rule,
                                     verdict.allowed, verdict.reason)
        if not verdict.allowed:
            if has_row:
                core_oms.transition(conn, cid, "cancelled", err="risk:" + verdict.reason)
            return _envelope_fail("trading/order-rejected",
                                  f"风控规则 {verdict.rule}：{verdict.reason}",
                                  client_order_id=cid)

        # 5) 业务确认：唯一放行方式。live 写操作必须经 Web 卡片批准；TTL 到期由
        #    store_access 按拒绝收尾（fail-closed）。sim 写操作不发起确认
        #    （store_access：只有实盘写操作需要业务确认）。
        if has_row:
            core_oms.transition(conn, cid, "frozen")
        if mode == "live":
            answer = self.confirm.request(self.home, tool=CONFIRM_TOOL[operation],
                                          mode="live",
                                          args=_summary_args(operation, clean, risk_price),
                                          session_id=session_id or "mcp",
                                          ttl_ms=self.confirm_ttl_ms)
            if answer.get("decision") != "approved":
                reason = str(answer.get("reason") or "确认未通过")
                if has_row:
                    core_oms.transition(conn, cid, "cancelled", err=reason)
                return _envelope_fail("trading/order-rejected", f"业务确认未通过：{reason}",
                                      client_order_id=cid)
        if has_row:
            # frozen → submitting：此后订单进入「可能已触达券商」的在途语义
            core_oms.transition(conn, cid, "submitting")

        # 6) broker（唯一触达点）。适配器契约：调用超时在适配层消化成 unknown，
        #    只有「确定未发出」的故障才抛 BrokerUnavailable（此时 OMS 落 rejected，
        #    不谎称已提交）。
        # WP8 任务 6：官方扩展字段整体透传（只在调用方给了才带上——不改写适配层的默认值）；
        # 市价类的 price 缺失在这里自然不带（字段校验层已保证风险基准价的处置）。
        broker_order = {key: clean[key] for key in
                        ("symbol", "side", "qty", "price", "order_id", "order_type",
                         "time_in_force", "session", "aux_price", "lot_type", "remark",
                         "order_class", "multi_leg_info") if key in clean}
        try:
            result = getattr(self.broker, operation)(broker_order, mode)
        except Exception as error:  # noqa: BLE001 —— 信封化，绝不 500
            message = f"券商通道异常：{error}"
            if has_row:
                core_oms.transition(conn, cid, "rejected", err=f"broker: {str(error)[:140]}")
            return _envelope_fail("trading/broker-unavailable", message,
                                  client_order_id=cid)
        if has_row:
            status = result.get("status")
            if status == "submitted":
                core_oms.transition(conn, cid, "submitted",
                                    broker_order_id=result.get("broker_order_id"))
            elif status in ("unknown", "rejected"):
                # unknown：铁律「只查询不重放」——迁出仅凭查询结果（oms.TRANSITIONS）
                core_oms.transition(conn, cid, status, err=result.get("err"))
                if status == "rejected":
                    core_store.kv_set(conn, "last_reject",
                                      {"order": cid, "err": result.get("err")})
            else:  # pragma: no cover —— 适配器契约外的状态立即暴露
                raise ValueError(f"broker 适配返回未知状态：{status!r}")
        return _envelope_ok({**result, "client_order_id": cid})


# ---------------------------------------------------------------------------
# WP8 任务 4：WS 交易事件 → OMS / 告警（推送只作加速，REST/对账仍是事实来源）
# ---------------------------------------------------------------------------
# 事件 → OMS 目标状态（附录 A 的 10 类）。``None`` = 该事件不改变订单状态：
#   * EVENT_REPLACED：改单成功，订单仍在途（状态由数量事实决定，推送不改）；
#   * EVENT_FILL：目标由累计成交量决定（partial/filled，见 _fill_target）；
#   * *_REJECTED / EVENT_FILL_CORRECT / EVENT_FILL_CANCEL：**保守处理**——只告警不猜
#     状态（撤单被拒时订单仍有效；成交修正/撤成交需要 REST 事实才能判定，绝不凭推送
#     把已成交的订单降级）。
EVENT_TARGET_STATE = {
    "EVENT_NEW": "submitted",
    "EVENT_REPLACED": None,
    "EVENT_CANCELED": "cancelled",
    "EVENT_EXPIRED": "cancelled",
    "EVENT_FILL": None,
    "EVENT_NEW_REJECTED": "rejected",
    "EVENT_REPLACE_REJECTED": None,
    "EVENT_CANCEL_REJECTED": None,
    "EVENT_FILL_CORRECT": None,
    "EVENT_FILL_CANCEL": None,
}
# 告警级别：拒单类/事实修正类必须引人注意（warn）；正常生命周期留 info 痕迹。
EVENT_ALERT_LEVEL = {
    "EVENT_NEW": "info",
    "EVENT_REPLACED": "info",
    "EVENT_CANCELED": "info",
    "EVENT_EXPIRED": "info",
    "EVENT_FILL": "info",
    "EVENT_NEW_REJECTED": "warn",
    "EVENT_REPLACE_REJECTED": "warn",
    "EVENT_CANCEL_REJECTED": "warn",
    "EVENT_FILL_CORRECT": "warn",
    "EVENT_FILL_CANCEL": "warn",
}
# 事件里承载订单号/数量/价格的候选字段（官方未在附录 A 给出事件体字段名，这里按
# 「读叶子字段、大小写变体、缺失即跳过」的防御口径取值，绝不猜不存在的语义）。
_EVENT_TYPE_KEYS = ("event_type", "event", "type", "action", "push_type", "msg_type")
_ORDER_ID_KEYS = ("order_id", "orderId", "order_id_ex", "broker_order_id")
_CLIENT_ORDER_ID_KEYS = ("client_order_id", "clientOrderId")
_DEALT_KEYS = ("dealt_qty", "dealtQty", "filled_qty", "filledQty", "traded_qty")
_TOTAL_QTY_KEYS = ("qty", "quantity", "order_qty", "total_qty")
_FILL_ID_KEYS = ("fill_id", "deal_id", "fillId", "dealId")
_PRICE_KEYS = ("price", "dealt_avg_price", "avg_price", "fill_price")
_FILL_QTY_KEYS = ("fill_qty", "deal_qty", "last_qty", "qty")
_TRADED_AT_KEYS = ("traded_at", "deal_time", "fill_time", "update_time", "create_time")
_REASON_KEYS = ("reason", "err_msg", "message", "remark", "status")


def _leaf(frame, keys):
    """从帧或其 ``data`` 子对象里取第一个非空叶子字段（不递归、不序列化活对象）。"""
    sources = [frame]
    data = frame.get("data")
    if isinstance(data, dict):
        sources.append(data)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, (str, int, float, bool)):
                return value
    return None


def _number(value):
    """数量/价格叶子 → float（非数值返回 None；bool 不算数值）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def event_type_of(frame):
    """事件类型（10 类白名单内的大写名）；未知/缺失返回 None（不崩）。"""
    if not isinstance(frame, dict):
        return None
    value = _leaf(frame, _EVENT_TYPE_KEYS)
    if isinstance(value, str) and value.upper() in EVENT_TARGET_STATE:
        return value.upper()
    return None


class TradeEventBridge:
    """交易推送事件 → OMS 白名单迁移 + 告警。**永不抛出**（推送不得拖垮会话）。

    硬约束落地（附录 A③）：
      * 用 ``order_id`` 定位订单（``broker_order_id`` 或调用方给的 ``client_order_id``）；
      * 状态迁移只走 ``trading_core.oms.TRANSITIONS`` 白名单——乱序（撤单先于成交回报）、
        重复、未知类型都不崩：非法迁移落 ``warn`` 告警并保留原状态；
      * 目标状态 == 当前状态 → ``state-unchanged``（正常重复回报，不产生告警噪音）；
      * 事件**不是**唯一事实源：这里只做「能确定的迁移」，其余交给对账兜底。
    """

    def __init__(self, home, conn_factory=None, alert_fn=None):
        self.home = str(home)
        self._conn_factory = conn_factory
        self._alert_fn = alert_fn
        self._lock = threading.Lock()
        self._counts = {"received": 0, "applied": 0, "alerts": 0, "unknown_events": 0,
                        "unmatched": 0, "errors": 0, "fills": 0}

    def _conn(self):
        if self._conn_factory is not None:
            return self._conn_factory()
        return core_store.connect(core_store.db_path(self.home))

    def _bump(self, key, amount=1):
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + amount

    def status(self):
        with self._lock:
            return dict(self._counts)

    def _alert(self, conn, level, title, detail):
        self._bump("alerts")
        try:
            if self._alert_fn is not None:
                self._alert_fn(conn, self.home, level, title, detail)
            else:
                core_alerts.emit(conn, self.home, level, title, detail)
        except Exception:  # noqa: BLE001 —— 告警失败不能影响状态迁移结论
            self._bump("errors")

    def handle(self, frame):
        """处理一帧推送 → ``{"event", "applied", "reason", ...}``（永不抛出）。"""
        self._bump("received")
        if not isinstance(frame, dict):
            return self._result(None, False, "not-an-object")
        event = event_type_of(frame)
        if event is None:
            self._bump("unknown_events")
            return self._result(None, False, "unknown-event")
        try:
            return self._apply(event, frame)
        except Exception as error:  # noqa: BLE001 —— 任何异常都收敛为「未应用」
            self._bump("errors")
            return self._result(event, False, "error", detail=str(error)[:200])

    # ---- 内部 ----
    def _result(self, event, applied, reason, **extra):
        return {"event": event, "applied": applied, "reason": reason, **extra}

    def _apply(self, event, frame):
        conn = self._conn()
        try:
            row = self._locate(conn, frame)
            if row is None:
                self._bump("unmatched")
                self._alert(conn, "warn", f"推送事件无法定位订单：{event}",
                            self._detail(frame))
                return self._result(event, False, "order-not-found")
            current = row["status"]
            target = self._target(event, frame, row)
            if target is None:
                level = EVENT_ALERT_LEVEL.get(event, "warn")
                if event == "EVENT_REPLACED":
                    self._record_fill(conn, frame, row)
                    return self._result(event, False, "state-unchanged")
                self._alert(conn, level, f"推送事件需人工关注：{event}",
                            self._detail(frame, row))
                return self._result(event, False, "no-state-change")
            if target == current:
                self._record_fill(conn, frame, row)
                return self._result(event, False, "state-unchanged")
            try:
                core_oms.transition(conn, row["client_order_id"], target,
                                    broker_order_id=_text_of(frame, _ORDER_ID_KEYS))
            except ValueError as error:
                # 乱序/重复/非法迁移：记录告警，状态保持原样（对账兜底会收敛）
                self._alert(conn, "warn", f"推送事件迁移被拒：{event}",
                            f"{self._detail(frame, row)} / {str(error)[:120]}")
                return self._result(event, False, "illegal-transition", to=target)
            self._bump("applied")
            self._record_fill(conn, frame, row)
            self._alert(conn, EVENT_ALERT_LEVEL.get(event, "info"),
                        f"推送事件 {event}：{row['symbol']} {current} → {target}",
                        self._detail(frame, row))
            return self._result(event, True, "applied", to=target)
        finally:
            conn.close()

    def _locate(self, conn, frame):
        broker_id = _text_of(frame, _ORDER_ID_KEYS)
        client_id = _text_of(frame, _CLIENT_ORDER_ID_KEYS)
        if client_id:
            row = conn.execute("SELECT client_order_id, symbol, status, qty, side"
                               " FROM orders WHERE client_order_id=?",
                               (client_id,)).fetchone()
            if row is not None:
                return row
        if broker_id:
            rows = conn.execute("SELECT client_order_id, symbol, status, qty, side"
                                " FROM orders WHERE broker_order_id=?",
                                (broker_id,)).fetchall()
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1:
                # 同一券商单号对应多行：取最近更新的在途行（推送只作加速，不猜）
                return rows[-1]
        return None

    def _target(self, event, frame, row):
        if event == "EVENT_FILL":
            return self._fill_target(frame, row)
        return EVENT_TARGET_STATE.get(event)

    @staticmethod
    def _fill_target(frame, row):
        """成交事件的目标状态：成交量够 → filled；有部分成交 → partial；无数量 → partial。

        推送不携带累计量时按 partial 保守处理（若当前是 submitted，这是一次合法迁移；
        后续事件/对账会收敛到 filled）。
        """
        dealt = _number(_leaf(frame, _DEALT_KEYS))
        total = _number(_leaf(frame, _TOTAL_QTY_KEYS))
        if total is None:
            total = _number(row["qty"])
        if dealt is None:
            return "partial"
        if total is not None and dealt >= total:
            return "filled"
        return "partial" if dealt > 0 else None

    def _record_fill(self, conn, frame, row):
        """显式携带成交编号/数量/价格的 EVENT_FILL 落 ``fills``（按 fill_id 去重）。

        官方事件体字段未在附录 A 给出：只有「编号 + 数量 + 价格」都拿得到才落库，
        否则跳过（绝不凭猜测合成成交记录）。
        """
        if event_type_of(frame) != "EVENT_FILL":
            return
        fill_id = _text_of(frame, _FILL_ID_KEYS)
        qty = _number(_leaf(frame, _FILL_QTY_KEYS))
        price = _number(_leaf(frame, _PRICE_KEYS))
        if not fill_id or not qty or price is None:
            return
        existing = conn.execute("SELECT 1 FROM fills WHERE fill_id=?",
                                (str(fill_id),)).fetchone()
        if existing is not None:
            return  # 重复推送（含乱序重发）不重复落成交
        try:
            core_store.insert_fill(conn, str(fill_id), row["client_order_id"], price,
                                   int(qty), traded_at=_text_of(frame, _TRADED_AT_KEYS))
        except Exception:  # noqa: BLE001 —— 落成交失败如实计数，不影响状态迁移结论
            self._bump("errors")
            return
        self._bump("fills")

    @staticmethod
    def _detail(frame, row=None):
        """告警明细：只取叶子字段，长度受限（不序列化推送原始帧）。"""
        parts = []
        event = _leaf(frame, _EVENT_TYPE_KEYS)
        if event is not None:
            parts.append(f"event={event}")
        order_id = _leaf(frame, _ORDER_ID_KEYS)
        if order_id is not None:
            parts.append(f"order_id={order_id}")
        if row is not None:
            parts.append(f"client_order_id={row['client_order_id']}")
            parts.append(f"status={row['status']}")
        reason = _leaf(frame, _REASON_KEYS)
        if reason is not None:
            parts.append(f"detail={str(reason)[:120]}")
        return " ".join(parts)[:300]


def _text_of(frame, keys):
    value = _leaf(frame, keys)
    if value is None:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# WP8 任务 4：重连后 REST 对账（事件不补发 → 用查询补齐）
# ---------------------------------------------------------------------------
#: 对账默认覆盖的市场（OpenApiTrade.TRD_MARKETS 的子集；A 股不在官方 OpenAPI 交易面）
PUSH_RECONCILE_MARKETS = ("HK", "US")
#: 状态文本的粗判子串（**不解读未公开的状态枚举**）：先看数量口径，再看这些子串
_STATUS_CANCEL = ("CANCEL", "EXPIRE")
_STATUS_REJECT = ("REJECT",)
_STATUS_FILLED = ("FILLED", "FILL_ALL")


class PushReconciler:
    """重连后的 REST 对账兜底：拉券商未结订单，与本地 OMS 在途订单比对。

    事实来源优先级（不变式 1）：REST 查询 > 推送事件。本类只做三件事：
      1. 用数量口径（``dealt_qty`` vs ``qty``）判定 filled/partial；
      2. 数量不足以判定时，用状态文本的粗粒度子串（CANCEL/EXPIRE/REJECT/FILLED）判定；
      3. 其余一律记差异（``local_only``/``broker_only``/``illegal-transition``），
         **绝不猜终态**——迁移仍走 ``oms.TRANSITIONS`` 白名单。

    记录落 kv ``reconcile:push``（``snapshot-reconcile`` 的 ``push`` 字段即它），并落一条
    告警；REST 失败如实记录，**永不抛出**（推送重连的回调里抛异常会打断客户端会话）。
    """

    def __init__(self, home, gate=None, conn_factory=None, alert_fn=None,
                 markets=PUSH_RECONCILE_MARKETS, now=None):
        self.home = str(home)
        self._gate = gate
        self._conn_factory = conn_factory
        self._alert_fn = alert_fn
        self._markets = tuple(markets)
        self._now = now
        self._lock = threading.Lock()
        self.last_error = None

    def _conn(self):
        if self._conn_factory is not None:
            return self._conn_factory()
        return core_store.connect(core_store.db_path(self.home))

    def _gateway(self):
        if self._gate is None:
            self._gate = TradeGate(self.home)
        return self._gate

    @staticmethod
    def _stamp():
        return _iso_now()

    def run(self, reason="ws-reconnect"):
        """执行一次对账 → 记录（``{"at","reason","checked","updated","diffs","errors"}``）。"""
        record = {"at": self._stamp(), "reason": reason, "markets": list(self._markets),
                  "checked": 0, "updated": [], "diffs": [], "errors": []}
        rows = {}
        try:
            rows = self._broker_rows(record)
        except Exception as error:  # noqa: BLE001 —— 对账失败不影响推送会话
            self._record(record, f"{type(error).__name__}: {error}"[:200])
        try:
            self._reconcile(record, rows)
        except Exception as error:  # noqa: BLE001
            self._record(record, f"{type(error).__name__}: {error}"[:200])
        self._persist(record)
        return record

    def _record(self, record, message):
        record["errors"].append(message)
        with self._lock:
            self.last_error = message

    def _broker_rows(self, record):
        """逐市场拉未结订单（REST）；单市场失败不掩盖其余市场。"""
        rows = {}
        gate = self._gateway()
        for market in self._markets:
            try:
                envelope = gate.orders_open({"mode": "live", "market": market})
            except Exception as error:  # noqa: BLE001 —— 单市场通道失败继续下一个
                self._record(record, f"{market}: {type(error).__name__}: {error}"[:160])
                continue
            if not isinstance(envelope, dict) or not envelope.get("ok"):
                error = (envelope or {}).get("error") or {}
                self._record(record,
                             f"{market}: {str(error.get('message') or envelope)[:160]}")
                continue
            value = envelope.get("value") or {}
            for group in value.get("groups") or []:
                group_market = group.get("market") or market
                for row in group.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    order_id = _text_of(row, _ORDER_ID_KEYS)
                    if order_id:
                        rows[order_id] = {"market": group_market, "row": row}
        return rows

    def _reconcile(self, record, broker_rows):
        conn = self._conn()
        try:
            local = conn.execute(
                "SELECT client_order_id, broker_order_id, symbol, status, qty, side"
                f" FROM orders WHERE broker_order_id IS NOT NULL AND status IN"
                f" ({','.join('?' * len(core_oms.OPEN_STATES))})",
                tuple(core_oms.OPEN_STATES)).fetchall()
            seen = set()
            for order in local:
                record["checked"] += 1
                broker_id = str(order["broker_order_id"])
                seen.add(broker_id)
                entry = broker_rows.get(broker_id)
                if entry is None:
                    record["diffs"].append({"kind": "local_only", "order_id": broker_id,
                                            "client_order_id": order["client_order_id"],
                                            "status": order["status"]})
                    continue
                target = self._target_state(entry["row"], order)
                if target is None or target == order["status"]:
                    continue
                try:
                    core_oms.transition(conn, order["client_order_id"], target,
                                        broker_order_id=broker_id)
                except ValueError as error:
                    record["diffs"].append({"kind": "illegal-transition",
                                            "order_id": broker_id,
                                            "client_order_id": order["client_order_id"],
                                            "from": order["status"], "to": target,
                                            "detail": str(error)[:120]})
                    continue
                record["updated"].append({"order_id": broker_id,
                                          "client_order_id": order["client_order_id"],
                                          "from": order["status"], "to": target,
                                          "source": "rest:orders"})
            for broker_id in sorted(set(broker_rows) - seen):
                record["diffs"].append({"kind": "broker_only", "order_id": broker_id,
                                        "market": broker_rows[broker_id]["market"]})
        finally:
            conn.close()

    @staticmethod
    def _target_state(row, order):
        """由 REST 行推导 OMS 目标状态：数量口径优先，其次状态文本子串，最后 None。"""
        dealt = _number(_leaf(row, _DEALT_KEYS))
        total = _number(_leaf(row, _TOTAL_QTY_KEYS)) or _number(order["qty"])
        if dealt is not None:
            if total is not None and dealt >= total:
                return "filled"
            if dealt > 0:
                return "partial"
        status_text = str(_leaf(row, ("status", "order_status", "status_name")) or "").upper()
        if any(mark in status_text for mark in _STATUS_REJECT):
            return "rejected"
        if any(mark in status_text for mark in _STATUS_CANCEL):
            return "cancelled"
        if "PART" in status_text:
            return "partial"
        if any(mark in status_text for mark in _STATUS_FILLED):
            return "filled"
        return None

    def _persist(self, record):
        try:
            conn = self._conn()
        except Exception as error:  # noqa: BLE001
            self._record(record, f"对账记录落库失败：{error}")
            return
        try:
            core_store.kv_set(conn, "reconcile:push", record)
            level = "warn" if (record["diffs"] or record["errors"]) else "info"
            title = "推送重连对账"
            detail = (f"市场={','.join(record['markets'])} 核对={record['checked']} "
                      f"迁移={len(record['updated'])} 差异={len(record['diffs'])} "
                      f"错误={len(record['errors'])}")
            if self._alert_fn is not None:
                self._alert_fn(conn, self.home, level, title, detail)
            else:
                core_alerts.emit(conn, self.home, level, title, detail)
        except Exception as error:  # noqa: BLE001
            self._record(record, f"对账告警落库失败：{error}")
        finally:
            conn.close()
