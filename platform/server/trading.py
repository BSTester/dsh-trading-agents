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
#   trading/order-rejected     参数/模式/风控/确认/OMS 查重等「拒单」类失败
#   trading/broker-unavailable 券商通道异常（适配器/网络/未接入）
#   trading/invalid-operation  载荷白名单（app.py 路由层）与查询模式非法
# 所有失败一律 {ok:false, error:{code, message≤300, details:{}}}，绝不抛到 500。
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    # venv 里 trading_core 已 pip install -e（与 server/scheduler.py 同口径）。
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
    """载荷字段校验（类型与范围）。返回 (clean, error_message)；error_message 为 None 表示合法。

    clean 只保留闸门与适配层需要的键：symbol/side/qty/price/order_id/client_order_id。
    撤单不需要 side/qty/price（撤的是已提交订单，不新增敞口），只认 order_id + symbol。
    """
    if not isinstance(payload, dict):
        return None, "订单参数必须是对象"
    clean = {}
    required = ["symbol"]
    if operation in ("place", "modify"):
        required += ["side", "qty", "price"]
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
        price = payload["price"]
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not (price > 0):
            return None, f"参数非法：price 必须是 >0 的数值，收到 {price!r}"
        clean["price"] = float(price)
    if "order_id" in required:
        order_id = payload["order_id"]
        if not isinstance(order_id, str) or not order_id.strip():
            return None, "参数非法：order_id 必须是非空字符串"
        clean["order_id"] = order_id.strip()
    cid = payload.get("client_order_id")
    if cid is not None:
        if not isinstance(cid, str) or not cid.strip():
            return None, "参数非法：client_order_id 必须是非空字符串"
        clean["client_order_id"] = cid.strip()
    return clean, None


def _side_code(side):
    """方向 → 券商代码（broker.py place 的同一线：1=BUY 2=SELL）。"""
    return 1 if side == "BUY" else 2


def _summary_args(operation, clean):
    """确认摘要的入参：键名走券商参数表（store_access.describe_order_args 的渲染口径），
    让确认卡片显示 账户/市场/标的/方向/数量/价格。acc_id 在批准后由适配层解析
    （确认前 broker 保持零调用），摘要里如实不含账户。撤单无方向可列（不新增敞口）。"""
    args = {"market": str(PREFIX_MARKET_ID[clean["symbol"].split(".", 1)[0]]),
            "symbol": clean["symbol"]}
    if "side" in clean:
        args["order_side"] = _side_code(clean["side"])
    if operation in ("place", "modify"):
        args.update({"qty": clean["qty"], "price": clean["price"],
                     "order_type": 1})  # 限价单（broker.place 默认口径）
    if operation in ("modify", "cancel"):
        args["order_id"] = clean["order_id"]
    return args


def _check_order(operation, clean, mode):
    """送进 pre_trade_checks 的订单：改单按**新参数**全额过闸（改单经济上=撤旧+下新，
    见 FutuBroker.modify 与 TOOL-LIMITS「一律撤单+重下」）；撤单不新增敞口，
    以 qty=0/price=0 过闸（规则 4-7 自然中性，真正约束是 1/2/3——披露于 default_ctx_builder）。
    stop_dist 恒 None：风险额按全额名义计（daemon stop_dist_of 的保守口径）。
    risk.pre_trade_checks 不读 side（8 规则均不用），撤单缺方向无影响。"""
    order = {"symbol": clean["symbol"], "mode": mode, "stop_dist": None}
    if "side" in clean:
        order["side"] = clean["side"]
    if operation == "cancel":
        order.update({"qty": 0, "price": 0.0})
    else:
        order.update({"qty": clean["qty"], "price": clean["price"]})
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

    # ---- 写路径 ----
    def place(self, order, mode):
        self._live_guard(mode)
        call = self._tool()
        account = self._sim_account(call, order["symbol"])
        # market 传账户的 market_id（数字口径，与 sim_trade_position_list 实测一致）；
        # execute.run 传的是交易所前缀——两条路径的差异在此披露，适配层以账户事实为准。
        return core_broker.place(call, acc_id=account["acc_id"],
                                 market=account["market_id"], symbol=order["symbol"],
                                 side=order["side"], qty=order["qty"],
                                 price=order["price"])

    def modify(self, order, mode):
        """改单 = 撤旧单 + 按新参数重新下单（TOOL-LIMITS：sim_trade_modify_order
        间歇性 -5，一律不用）。旧单已撤而新单超时时返回 unknown（先查询，不重放）。"""
        self._live_guard(mode)
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
      * ``OpenApiError`` → ``rejected``：业务错误信封，券商未接受本单。
    """
    from trading_datasource.futu_openapi import (  # noqa: PLC0415
        OpenApiError, OrderConfirmRequired, TransportError)
    if isinstance(error, OrderConfirmRequired):
        return {"need_confirm": error}
    if isinstance(error, TransportError):
        return {"status": "unknown",
                "err": f"{label}传输异常：{_err_text(error)}（先查询，不重放）"}
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
        try:
            data = trade.place_order(acc_id=acc_id, code=order["symbol"], qty=order["qty"],
                                     price=order["price"], side=order["side"],
                                     order_type="LIMIT", time_in_force="DAY")
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
        try:
            data = trade.modify_order(acc_id=acc_id, order_id=order["order_id"],
                                      exchange=exchange, qty=order["qty"],
                                      price=order["price"])
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
                # 官方撤单页未列 need_order_confirm；若券商如此返回，仍按「用户已在卡片批准
                # 本操作」自动确认（同一批准覆盖同一操作，不额外打扰用户）。
                return self._confirm(trade, acc_id, outcome["need_confirm"], None, "cancel",
                                     fallback_order_id=order["order_id"])
            return {"order_id": order["order_id"], **outcome}
        return {"status": "cancelled", "order_id": order["order_id"], "acc_id": acc_id,
                "raw": data}

    def _confirm(self, trade, acc_id, need, placed, operation, fallback_order_id=None):
        """券商侧二次确认（两层确认合一的第二层）——**只调 order_confirm，绝不重发下单**。

        用户已在 Web 卡片批准本单参数（业务确认），因此这里自动完成券商确认。失败一律
        落 ``unknown``：订单在券商侧**已挂起**（place/modify）且 confirm 结果不确定，
        铁律「先查询不重放」；confirm_id 缺失同样 unknown（不猜、不重发）。
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
        except Exception as error:  # noqa: BLE001
            from trading_datasource.futu_openapi import (  # noqa: PLC0415
                OpenApiError, TransportError)
            if not isinstance(error, (OpenApiError, TransportError)):
                raise
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
        # 1) 字段校验（broker/确认零接触）
        clean, message = validate_order(payload, operation)
        if message is not None:
            return _envelope_fail("trading/order-rejected", message)
        # 2) 模式：只认模式文件（载荷不带 mode，杜绝声明模式与账户模式不一致的旁路；
        #    风控规则 2 在闸门内的形态即「模式文件合法且与订单一致」——订单模式由这里
        #    统一赋值，二者恒等）。
        try:
            mode = store_access.read_mode(self.home)
        except WorkbenchError as error:
            return _envelope_fail("trading/order-rejected", f"账户模式非法：{error}")
        # 2.5) live 写前置拒绝：适配器未声明 supports_live_write 时，确认通过后也注定
        #      被 FutuBroker._live_guard 拒绝——在确认之前快速失败，不发起确认、不产生
        #      待确认、不落 OMS/风控行。getattr 对未声明该属性的鸭子类型适配器按
        #      「不支持」处理（fail-safe：没显式声明支持 live 写，就不得写 live）。
        if mode == "live" and not getattr(self.broker, "supports_live_write", False):
            return _envelope_fail("trading/broker-unavailable", LIVE_WRITE_UNAVAILABLE)
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
        order = _check_order(operation, clean, mode)
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
                                          mode="live", args=_summary_args(operation, clean),
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
        broker_order = {key: clean[key] for key in
                        ("symbol", "side", "qty", "price", "order_id") if key in clean}
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
