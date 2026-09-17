# WP8：工作台富途数据直通（资金流/衍生品/实时报价/行情快照/K线/基本数据，17 端点/工具）。
#
# 职责边界：skills 需要而本地无缓存的数据，由服务端**实时**经富途获取后返回（技能不再
# 回退直连富途）。本模块做四件事：
#   1. 通道收敛 —— 唯一触达 ``trading_datasource.futu_mcp.call_tool`` 的直通层
#      （token 缺失/过期由 call_tool 的 auto_refresh 自动处理，这里不复制协议）；
#   2. 错误分类 —— call_tool 把业务错误包成 FutuUnavailable 文本（isError=false 信封
#      同源），这里按特征重新分流：``ret_code!=0`` / ``s==error`` → 业务错误
#      （trading/futu-error）；token 缺失/失效 → 通道不可用（trading/futu-unavailable，
#      消息指向 scripts/futu_auth.py）；网络/响应形状 → 通道不可用；
#   3. 参数面 —— 每个数据方法的参数白名单 + 必填/类型校验（code 归一同 series 的
#      ticker 规则，即 ``market.to_futu_symbol``），坏参数在触达通道前拒绝；
#   4. 通道路由（WP8 任务 2）—— ``~/.dsh/trading-platform.json`` 的 ``futu_channel``
#      （``"openapi"|"mcp"``，**默认 mcp**，既有行为零变化）：``channel=openapi`` 且
#      openapi 凭据可用 → ``trading_datasource.futu_openapi.OpenApiMarket``（REST 后端，
#      响应归一化为 MCP 通道形状）；否则 MCP 直通。无凭据 + channel=openapi →
#      ``trading/openapi-unavailable``（消息指向 scripts/futu_auth.py --openapi）。
#
# 通道路由事实（2026-09-16）：
#   * 两个通道打**同一官方网关**，``data`` 层形状一致（MCP tools/list 实测 101 工具与
#     官方 REST 文档逐键比对；归一化 fixture 见 tests/test_wp8_market.py）；
#   * 归一化规则：(a) 信封 ``d`` 透传；(b) 信封顶层 ``pagination`` 并入 d（三个分页
#     端点 capital_flow_history/option_screen/quote_history_kline_v2，与
#     ``futu_mcp._unwrap`` 同规则）；(c) ``info_search`` 的 MCP 侧 ``quote_news_search``
#     实测恒空（官方通道已知问题，见 docs/TOOL-LIMITS.md），**OpenAPI 侧同端点也可能回
#     空对象**（合法空结果）——两侧同规归一化为形状表要求的 ``{"news_list": []}``；
#   * 错误分类（2026-09-16 审查修正）：OpenAPI 侧 ``UnexpectedResponse``（5xx/429/非
#     JSON/非信封）按**通道不可用**（trading/futu-unavailable）而不是业务错误——它没有
#     业务结论；只有 ``s==error`` / ``ret_code!=0`` 才是 trading/futu-error；
#   * 已知残余差异如实登记（不强求对齐）：``capital_flow_history`` 的 ``next_time`` 键
#     MCP data 有而 REST d 无；``rt_order_book`` 元素级字段 REST 更全；A 股 -9 的替代
#     路径提示只在 MCP 通道追加（OpenAPI 通道 errcode 语义未实测，不冒充）。
#
# 通道事实（docs/TOOL-LIMITS.md，2026-09-13 体检；2026-09-16 tools/list 复核）：
#   * ``quote_option_expiration`` 已停用（"tool has been deactivated or does not exist"），
#     实名是 ``quote_option_expiration_date``（实测 HK.00700 返回 ret_code=0）——
#     FUTU_TOOLS 按通道实测事实登记，任务表里的旧名不再使用；
#   * ``quote_option_screen`` 必须带非空 ``field_filter``（省略只回 4 个默认字段、其余全
#     null），且 ``strategy`` 为上游必填（缺省 ret=-3 invalid_parameter）——两层都必须
#     在参数层拒绝，不浪费一次注定失败的往返；
#   * A 股实时报价/盘口统一 ``-9``（仅延时权限）：rt_quote/rt_order_book 对 A 股注定
#     失败，错误消息附可用替代路径（capital_flow / history-kline）——数据事实，非故障。
#
# 缓存口径：实时端点 TTL 0（不进 caches.CACHE_TTL_MS / ENDPOINT_SHAPE，与
# plan-execute / trade_* / account_* 同类）；WP8 任务 2 的 5 个基本类端点
# （info_basicinfo/info_trading_days/info_search/info_market_state/quote_history_kline_v2）
# 进 TTL 表并在 value 层缓存（缓存数据而非信封，错误信封透传不缓存，语义同
# caches.cached——由 app.py 分支直连本模块保证）。
#
# 错误码族（与既有 trading/* 一致，全部 {ok:false, error:{code, message≤300, details}}）：
#   trading/futu-unavailable   MCP 通道不可用（未授权/网络/响应形状）；OpenAPI 非信封
#                              （5xx/429/非 JSON）同码——都没拿到业务结论
#   trading/openapi-unavailable OpenAPI 通道不可用（channel=openapi 但无凭据）→ 指向授权
#   trading/futu-error         富途业务错误（MCP ret_code!=0 / s=error；OpenAPI errcode/
#                              ret_code!=0 信封）
#   trading/invalid-operation  参数白名单/必填/类型（与 handle 层载荷校验同码）
import inspect
import re
import time
from datetime import datetime, timezone


def _iso_from_ms(at_ms):
    """毫秒时间戳 → UTC ISO 串（与 caches.iso_from_ms 同格式；本模块不跨模块依赖私有助手）。"""
    moment = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# 错误码（直通层信封 + 参数拒绝沿用 invalid-operation）。
UNAVAILABLE_CODE = "trading/futu-unavailable"
OPENAPI_UNAVAILABLE_CODE = "trading/openapi-unavailable"
FUTU_ERROR_CODE = "trading/futu-error"
PARAM_CODE = "trading/invalid-operation"

# 通道路由（WP8 任务 2）：trading-platform.json 的 futu_channel 合法值。
CHANNEL_MCP = "mcp"
CHANNEL_OPENAPI = "openapi"

# 通道事实：A 股实时 -9 的固定提示（数据事实，逐字进错误消息，锁定测试钉住）。
A_SHARE_RT_HINT = "A 股实时无权限：可用 capital_flow / history-kline 替代"
# token 缺失/失效的固定指引：指向安装/授权脚本（scripts/futu_auth.py）。
AUTH_HINT = "（富途 token 缺失或已失效：运行 scripts/futu_auth.py 完成安装/授权）"
# openapi 凭据缺失的固定指引（trading/openapi-unavailable 的消息必须指向授权入口）。
OPENAPI_AUTH_HINT = "（运行 scripts/futu_auth.py --openapi 完成 OAuth/AppKey 配置）"

DEFAULT_TIMEOUT_SECONDS = 30

# 端点 → 富途 MCP 工具名（mcp 后端）。键集 ≡ store_access.FUTU_ENDPOINTS ∪
# store_access.WP8_MARKET_ENDPOINTS（锁定测试比对）。option_expiration 的上游实名与
# 新 9 键的实测依据见文件头通道事实。
FUTU_TOOLS = {
    "rt_quote": "quote_stock_quote",
    "rt_order_book": "quote_order_book",
    "capital_flow": "quote_capital_flow",
    "capital_flow_history": "quote_capital_flow_history",
    "capital_distribution": "quote_capital_distribution",
    "option_expiration": "quote_option_expiration_date",
    "option_chain": "quote_option_chain",
    "option_screen": "quote_option_screen",
    # WP8 任务 2：OpenAPI 行情接入新增端点（MCP 侧工具名 2026-09-16 tools/list 实测）
    "market_snapshot": "quote_market_snapshot",
    "cur_kline": "quote_cur_kline",
    "rt_data": "quote_rt_data",
    "rt_ticker": "quote_rt_ticker",
    "info_basicinfo": "quote_stock_basicinfo",
    "info_trading_days": "quote_trading_days",
    "info_search": "quote_news_search",
    "info_market_state": "quote_market_state",
    "quote_history_kline_v2": "quote_history_kline",
}

# WP8 任务 2：进 value 层 TTL 缓存的端点（TTL 值在 caches.CACHE_TTL_MS：基本类 5m、
# quote_history_kline_v2 10m）。实时类（rt_quote/rt_order_book/capital_flow/
# capital_distribution/option_*/market_snapshot/cur_kline/rt_data/rt_ticker）一律 TTL 0。
CACHED_FUTU_ENDPOINTS = frozenset({
    "info_basicinfo", "info_trading_days", "info_search", "info_market_state",
    "quote_history_kline_v2",
    # WP12 任务 4：数据面低频端点的 value 层缓存（TTL 值在 caches.CACHE_TTL_MS：
    # 板块列表/复权/所属板块 6h，经济日历/F10/衍生品/板块成分/IPO 30m，筛选/自选 5m，
    # 做空 1h）。**modify_user_security 不进**：它是写（非交易写），缓存住写结果没有意义
    # 且会掩盖后续读。所有被缓存的端点都走同一份「错误不进缓存」语义（_cached）。
    "economic_calendar_hot", "economic_calendar_search", "info_owner_plate", "info_rehab",
    "plate_list", "plate_stock", "stock_screen", "warrant_screen", "ipo_list",
    "short_daily_volume", "short_interest", "watchlist_list", "watchlist_groups",
    "f10_detail", "derivative_detail",
})

# 端点 → OpenApiMarket 方法登记（openapi 后端；与 FUTU_TOOLS 键集一致，锁定测试比对）。
OPENAPI_METHODS = {
    "rt_quote": "stock_quote",
    "rt_order_book": "order_book",
    "capital_flow": "capital_flow",
    "capital_flow_history": "capital_flow_history",
    "capital_distribution": "capital_distribution",
    "option_expiration": "option_expiration",
    "option_chain": "option_chain",
    "option_screen": "option_screen",
    "market_snapshot": "market_snapshot",
    "cur_kline": "cur_kline",
    "rt_data": "rt_data",
    "rt_ticker": "rt_ticker",
    "info_basicinfo": "stock_basicinfo",
    "info_trading_days": "trading_days",
    "info_search": "search_news",
    "info_market_state": "market_state",
    "quote_history_kline_v2": "history_kline",
}


def _oa_capital_flow_history(market, arguments):
    return market.capital_flow_history(arguments["symbol"], count=arguments["count"])


def _oa_option_chain(market, arguments):
    # REST 面没有 field_filter（上游 option_chain 对该参数本就不生效）——丢弃并登记。
    return market.option_chain(arguments["symbol"])


def _oa_info_trading_days(market, arguments):
    return market.trading_days(arguments["market"], arguments["start"],
                               arguments["end"])


# OpenAPI 参数适配：canonical arguments（MCP 工具入参形状）→ OpenApiMarket 方法调用。
# 两通道消费同一份 canonical arguments；MCP 侧键名 = 上游工具入参（FUTU_TOOLS）。
OPENAPI_ADAPTERS = {
    "rt_quote": lambda market, a: market.stock_quote(a["code_list"]),
    "rt_order_book": lambda market, a: market.order_book(a["code"]),
    "capital_flow": lambda market, a: market.capital_flow(a["symbol"]),
    "capital_flow_history": _oa_capital_flow_history,
    "capital_distribution": lambda market, a: market.capital_distribution(a["symbol"]),
    "option_expiration": lambda market, a: market.option_expiration(a["symbol"]),
    "option_chain": _oa_option_chain,
    "option_screen": lambda market, a: market.option_screen(**a),
    "market_snapshot": lambda market, a: market.market_snapshot(a["code_list"]),
    "cur_kline": lambda market, a: market.cur_kline(**a),
    "rt_data": lambda market, a: market.rt_data(**a),
    "rt_ticker": lambda market, a: market.rt_ticker(**a),
    "info_basicinfo": lambda market, a: market.stock_basicinfo(a["code_list"]),
    "info_trading_days": _oa_info_trading_days,
    "info_search": lambda market, a: market.search_news(**a),
    "info_market_state": lambda market, a: market.market_state(**a),
    "quote_history_kline_v2": lambda market, a: market.history_kline(**a),
}

# option_screen 的 filter 对象内键白名单（与上游 tools/list 的入参面逐项对应）。
_SCREEN_KEYS = ("strategy", "field_filter", "limit", "next_key", "request_exact_data",
                "sort_obj", "strategy_param")

# ---------------------------------------------------------------------------
# WP12 任务 4：富途数据面端点（锁定表 docs/superpowers/plans/wp12-endpoint-lock.md §C）
# ---------------------------------------------------------------------------
# 16 个端点 = 直通 14 + 聚合 2（``f10_detail`` 26 section / ``derivative_detail`` 4 section）。
# 其中 3 个是**有意的 HTTP-only**（进服务端点面、不进 MCP 工具面，见
# ``mcp_tools.MCP_EXCLUDED_ENDPOINTS``）：
#   * ``warrant_screen``——窝轮数据保持 API 面完整，但平台策略/风控/执行不引入该品类；
#   * ``modify_user_security``——**写用户富途侧自选**（非交易写），仅 Web 用户操作可达；
#   * ``info_rehab``——复权因子，同步作业内部取数用，不需要模型工具面。
#
# mcp 通道**不登记**本组端点：托管 MCP 的上游工具名与参数形状未逐项核对（锁定表只核对
# REST 面），按仓库纪律禁止猜名——``_fetch_mcp`` 对本组如实拒绝并指引切 openapi 通道。
DATAPLANE_ENDPOINTS = (
    "economic_calendar_hot", "economic_calendar_search", "info_owner_plate", "info_rehab",
    "plate_list", "plate_stock", "stock_screen", "warrant_screen", "ipo_list",
    "short_daily_volume", "short_interest", "watchlist_list", "watchlist_groups",
    "modify_user_security", "f10_detail", "derivative_detail",
)

# ``derivative_detail`` 的 section 白名单（锁定表 §C.7 四项）→ OpenApiDerivatives 方法名。
DERIVATIVE_SECTIONS = {
    "future_info": "future_info",
    "reference_future": "reference_future",
    "option_volatility": "option_volatility",
    "option_exercise_probability": "option_exercise_probability",
}

# ``f10_detail`` 的 section 白名单以传输层 ``OpenApiF10.SECTIONS`` 为**单一事实源**
# （锁定表 §C.5 的 26 项），此处不复制名单——复制一份迟早漂移（与 _market_class 同口径）。


def _f10_class():
    """``trading_datasource.futu_openapi.OpenApiF10``（惰性导入，section 名单单一源）。"""
    from trading_datasource.futu_openapi import OpenApiF10 as cls  # noqa: PLC0415
    return cls


def _spread_params(method, params, supplied=()):
    """聚合端点 ``params`` 展开前的**签名校验**：载荷错误与通道故障必须分开。

    ``f10_detail``/``derivative_detail`` 把调用方 ``params`` 展开进传输层方法。若不做
    校验，一个拼错的键会变成 ``TypeError``，被 ``_fetch_openapi`` 的兜底 ``except`` 归成
    ``trading/futu-unavailable``——界面据此把用户引去「设置页配凭据」，而真实原因是参数
    错误（应为 ``trading/invalid-operation``）。

    实现选**签名内省前置校验**而非「捕获 TypeError 再归类」：后者要靠异常文本匹配才能
    不吞内部真 bug，且归类点在兜底分支里、离现场远。这里在展开处按目标方法签名判定，
    未知键 → ``_param_error``（与逐端点白名单同码族），内部真 bug 仍按原样浮出。

    ``supplied`` 是已按位置传入的参数名（如 ``symbol``），不得再出现在 params 里
    （否则会变成重复实参）。方法自带 ``**kwargs`` 时无键契约可校验，原样透传。
    """
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise _param_error(f"params 必须是对象（收到 {type(params).__name__}）")
    if not params:
        return {}
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):  # 无法内省（C 扩展/特殊 callable）：交调用点自然失败
        return params
    if any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
        return params
    allowed = {name for name, p in parameters.items()
               if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
               and name not in set(supplied)}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise _param_error(f"params 含不支持字段：{unknown}"
                           f"（允许：{sorted(allowed)}）")
    return params


def _oa_f10_detail(groups, arguments):
    """``f10_detail``：section 白名单 → OpenApiF10 专用方法（params 按签名校验）。"""
    section = arguments["section"]
    # section 名单以传输层常量为单一事实源（不复制名单，避免漂移）；校验用类常量而非
    # 注入实例的同名属性——替身无需复刻 SECTIONS 也能走通校验。
    sections = _f10_class().SECTIONS
    if section not in sections:
        raise _param_error(f"section 取值非法：{section!r}"
                           f"（允许：{sorted(sections)}）")
    method = getattr(groups.f10, section)
    params = _spread_params(method, arguments.get("params"), supplied=("symbol",))
    return method(arguments["symbol"], **params)


def _oa_derivative_detail(groups, arguments):
    """``derivative_detail``：section 白名单 → OpenApiDerivatives 专用方法（params 同上）。"""
    section = arguments["section"]
    method_name = DERIVATIVE_SECTIONS.get(section)
    if method_name is None:
        raise _param_error(f"section 取值非法：{section!r}"
                           f"（允许：{sorted(DERIVATIVE_SECTIONS)}）")
    method = getattr(groups.derivatives, method_name)
    params = _spread_params(method, arguments.get("params"), supplied=("symbol",))
    return method(arguments["symbol"], **params)


# 数据面方法组的公开方法清单（**锁定绑定的单一事实源**）：反射不变式测试逐组比对
# 「公开方法 == 本表」。新增公开方法必须在此登记——防未来用内联字面量路径绕过锁定表
# 与 DATAPLANE_ADAPTERS 注册（表外的路径既不进端点面、也不受 TTL/形状表约束）。
DATAPLANE_GROUP_METHODS = {
    "basic": ("economic_calendar_hot", "economic_calendar_search", "owner_plate", "rehab"),
    "plate": ("plate_list", "plate_stock"),
    "screen": ("stock_screen", "warrant_screen"),
    "ipo": ("ipo_list",),
    "short": ("short_daily_volume", "short_interest"),
    "watchlist": ("watchlist_list", "watchlist_groups", "modify_user_security"),
}

# 方法组属性名 → 传输层类名（不变式测试用；与 DataPlaneGroups.__slots__ 前六项一致）。
DATAPLANE_GROUP_CLASSES = {
    "basic": "OpenApiBasicData",
    "plate": "OpenApiPlate",
    "screen": "OpenApiScreen",
    "ipo": "OpenApiIpo",
    "short": "OpenApiShort",
    "watchlist": "OpenApiWatchlist",
}


# 端点 → 适配器：canonical arguments（服务载荷归一后的形状）→ 方法组调用。
# 与 OPENAPI_ADAPTERS 同构，区别只在注入的是**数据面八方法组**（共享同一 OpenApiClient）。
DATAPLANE_ADAPTERS = {
    "economic_calendar_hot": lambda g, a: g.basic.economic_calendar_hot(**a),
    "economic_calendar_search": lambda g, a: g.basic.economic_calendar_search(**a),
    "info_owner_plate": lambda g, a: g.basic.owner_plate(**a),
    "info_rehab": lambda g, a: g.basic.rehab(**a),
    "plate_list": lambda g, a: g.plate.plate_list(**a),
    "plate_stock": lambda g, a: g.plate.plate_stock(**a),
    "stock_screen": lambda g, a: g.screen.stock_screen(**a),
    "warrant_screen": lambda g, a: g.screen.warrant_screen(**a),
    "ipo_list": lambda g, a: g.ipo.ipo_list(**a),
    "short_daily_volume": lambda g, a: g.short.short_daily_volume(**a),
    "short_interest": lambda g, a: g.short.short_interest(**a),
    "watchlist_list": lambda g, a: g.watchlist.watchlist_list(**a),
    "watchlist_groups": lambda g, a: g.watchlist.watchlist_groups(**a),
    "modify_user_security": lambda g, a: g.watchlist.modify_user_security(**a),
    "f10_detail": _oa_f10_detail,
    "derivative_detail": _oa_derivative_detail,
}

# call_tool 把业务错误抛成 "……: ret=<code> <ret_msg>" / "……: s=<status> <detail>"，
# 两条特征各一份正则（futu_mcp._unwrap 的输出格式，锁定测试按此钉）。
_RET_CODE_RE = re.compile(r"\bret=(-?\d+)\b")
_STATUS_RE = re.compile(r"\bs=\S")
# token/凭证语义特征（含 internal error：实测 token 过期时服务端对所有工具返回它）。
_AUTH_SEMANTICS = ("token", "internal error", "unauthorized", "invalid_token", "缺少富途")
# code 归一后的合法形状（与 market.is_a_share 使用的 MARKET.CODE 形状一致）。
_SYMBOL_SHAPE_RE = re.compile(r"^(?:SH|SZ|BJ|HK|US)\.[A-Z0-9.]+$")
# 日期形状（日历有效性由 OpenApiMarket 复核；这里只挡明显坏值）。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_KIND_TO_CODE = {"unavailable": UNAVAILABLE_CODE, "business": FUTU_ERROR_CODE,
                 "parameter": PARAM_CODE,
                 "openapi-unavailable": OPENAPI_UNAVAILABLE_CODE}

# OpenApiMarket 枚举常量的惰性单例（避免在模块导入期拉起 cryptography）。
_MARKET_CLS = None


def _market_class():
    """``trading_datasource.futu_openapi.OpenApiMarket`` 类（惰性导入，枚举常量单一源）。"""
    global _MARKET_CLS
    if _MARKET_CLS is None:
        from trading_datasource.futu_openapi import OpenApiMarket as cls  # noqa: PLC0415
        _MARKET_CLS = cls
    return _MARKET_CLS


def load_channel(home=None):
    """读 ``~/.dsh/trading-platform.json`` 的 ``futu_channel``（openapi|mcp，默认 mcp）。

    读取语义复用 server/config.py（容错）：缺文件 → 默认值；坏 JSON → ValueError
    （与服务配置同一处理，不静默吞掉配置错误）；非法值/缺键 → 默认 mcp。

    WP13 任务 1：实现下沉到 ``trading_datasource.channel.channel_of``——core 与 workbench
    脚本同样要判通道，放在双方共同依赖的包里才只有一份实现。本函数保留为服务侧入口
    （调用方与既有测试按原签名使用，行为逐字不变）。
    """
    from trading_datasource import channel as channel_mod  # noqa: PLC0415
    return channel_mod.channel_of(home)


def openapi_ready(credential_path=None):
    """OpenAPI 凭据是否可用（**行情与交易两条链共用的唯一判定**）。

    oauth → access_token/refresh_token 至少有一个（可刷新）；
    appkey → app_key 与 private_key_path 齐备**且私钥文件可加载**
    （``AppKeySigner.from_path``：文件缺失/坏 PEM/算法不支持/私钥类型不符 → 不可用）；
    其余（文件缺失/坏 JSON/mode 未配置）→ False。

    「私钥可加载」是凭据就绪的一部分：只看路径存在会让 live 写穿过闸门（**人工确认被消耗**）
    后才在签名时失败；判在这里则确认零消耗、零 HTTP 调用、错误码 ``trading/broker-unavailable``
    （见 tests/test_wp8_trading.py 的私钥缺失用例）。

    WP13 任务 1：实现下沉到 ``trading_datasource.channel.openapi_ready``（同一函数），
    本入口与 ``trading_core.research_sync.openapi_ready`` 从此是同一个答案，
    ``tests/test_wp12_ready_equivalence.py`` 的等价矩阵继续成立（且不再需要人工同步）。
    """
    from trading_datasource import channel as channel_mod  # noqa: PLC0415
    return channel_mod.openapi_ready(credential_path)


class FutuDataError(RuntimeError):
    """直通层失败：``kind`` ∈ unavailable/business/parameter/openapi-unavailable 决定信封码。"""

    def __init__(self, message, kind="business", details=None):
        super().__init__(message)
        self.kind = kind
        self.code = _KIND_TO_CODE[kind]
        self.details = details or {}


def _param_error(message):
    return FutuDataError(message, kind="parameter")


def _normalize_code(value, field):
    """code 归一：同 series 的 ticker 规则（to_futu_symbol），归不出合法形状即拒绝。

    ``000001.HK`` 是港股长和、裸 6 位数字按 A 股——这些既有约定全部由 to_futu_symbol
    一份实现承担，这里只做形状复核，不另写第二份归一。
    """
    if not isinstance(value, str) or not value.strip():
        raise _param_error(f"{field} 必须是非空字符串（如 HK.00700 / SH.600519）")
    from trading_datasource.market import to_futu_symbol  # noqa: PLC0415
    symbol = to_futu_symbol(value)
    if not _SYMBOL_SHAPE_RE.fullmatch(symbol):
        raise _param_error(f"{field} 不是合法标的代码：{str(value)[:40]}（如 HK.00700 / SH.600519）")
    return symbol


def _required_code(payload, field="code"):
    value = payload.get(field)
    if value is None:
        raise _param_error(f"缺少必填字段 {field}")
    return _normalize_code(value, field)


def _require_dict(value, message):
    if not isinstance(value, dict) or not value:
        raise _param_error(message)
    return value


def _codes_list(payload, field, count_max):
    """批量 code 白名单：1..count_max 个标的代码，逐个归一（同 rt_quote 规则）。"""
    codes = payload.get(field)
    if not isinstance(codes, list) or not 1 <= len(codes) <= count_max:
        raise _param_error(f"{field} 必须是 1..{count_max} 个标的代码的列表")
    return [_normalize_code(code, field) for code in codes]


def _int_range(payload, field, low, high, default=None, required=False):
    """可选整数区间校验：缺省走 default；必填缺失/越界/类型错拒绝（bool 排除）。"""
    value = payload.get(field)
    if value is None:
        if required and default is None:
            raise _param_error(f"缺少必填字段 {field}（{low}..{high} 的整数）")
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _param_error(f"{field} 必须是 {low}..{high} 的整数")
    return value


def _enum(payload, field, allowed, default):
    """可选枚举校验：缺省走 default；非法值拒绝（单一事实源 = OpenApiMarket 的枚举表）。"""
    value = payload.get(field)
    if value is None:
        return default
    if value not in allowed:
        raise _param_error(f"{field} 取值非法：{value!r}（允许：{sorted(allowed)}）")
    return value


def _optional_bool(payload, field):
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _param_error(f"{field} 必须是布尔值")
    return value


def _required_date(payload, field):
    value = payload.get(field)
    if value is None:
        raise _param_error(f"缺少必填字段 {field}（yyyy-MM-dd）")
    return _optional_date({field: value}, field, required=True)


def _optional_date(payload, field, required=False):
    value = payload.get(field)
    if value is None:
        if required:
            raise _param_error(f"缺少必填字段 {field}（yyyy-MM-dd）")
        return None
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise _param_error(f"{field} 必须是 yyyy-MM-dd 字符串")
    try:
        import datetime as _dt
        _dt.date.fromisoformat(value)
    except ValueError:
        raise _param_error(f"{field} 不是合法日期：{value!r}（yyyy-MM-dd）") from None
    return value


def _optional_str(payload, field):
    """可选非空字符串（分页游标/分组名等）：非字符串或空白一律拒绝。"""
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _param_error(f"{field} 必须是非空字符串")
    return value


def _optional_int(payload, field, minimum=1):
    """可选整数下界校验（**不做上界发明**：上界由传输层方法按其常量判，单一事实源）。"""
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _param_error(f"{field} 必须是不小于 {minimum} 的整数")
    return value


def _optional_scalar(payload, field):
    """可选标量直通（官方未给区间/枚举的参数，不发明规则）。"""
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        raise _param_error(f"{field} 必须是标量（字符串或数字）")
    return value


def _optional_list(payload, field):
    """可选非空数组（code_list/screen_queries 等）：元素形状由传输层方法校验。"""
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise _param_error(f"{field} 必须是非空数组")
    return value


def _optional_dict(payload, field):
    """可选非空对象（section 参数等）：键集合由传输层方法校验。"""
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _param_error(f"{field} 必须是对象")
    return value


def _required_str(payload, field):
    value = payload.get(field)
    if value is None:
        raise _param_error(f"缺少必填字段 {field}")
    if not isinstance(value, str) or not value.strip():
        raise _param_error(f"{field} 必须是非空字符串")
    return value.strip()


def _required_scalar(payload, field):
    """必填标量（官方必填但未给区间/枚举的参数，如 search_type）。"""
    value = payload.get(field)
    if value is None:
        raise _param_error(f"缺少必填字段 {field}")
    if isinstance(value, (dict, list)):
        raise _param_error(f"{field} 必须是标量（字符串或数字）")
    return value


def _required_list(payload, field):
    """必填非空数组（官方必填的 code_list/screen_queries 等）。"""
    value = payload.get(field)
    if value is None:
        raise _param_error(f"缺少必填字段 {field}")
    if not isinstance(value, list) or not value:
        raise _param_error(f"{field} 必须是非空数组")
    return value


class DataPlaneGroups:
    """WP12 数据面八方法组的实例集合（**共享同一个 OpenApiClient**）。

    方法与 WP8 的 ``OpenApiMarket`` 同源：每个方法组只是一个 REST 面（筛选用 screen、
    板块用 plate……），凭据/续期/限频/错误码映射都在共用的 client 上，因此这里必须
    复用 ``FutuData._client()`` 建出的**同一个** client 实例，不能各建一个。
    """

    __slots__ = ("screen", "plate", "short", "basic", "ipo", "watchlist",
                 "derivatives", "f10")

    def __init__(self, client):
        from trading_datasource.futu_openapi import (  # noqa: PLC0415 —— 避免导入期拉起 cryptography
            OpenApiBasicData, OpenApiDerivatives, OpenApiF10, OpenApiIpo, OpenApiPlate,
            OpenApiScreen, OpenApiShort, OpenApiWatchlist)
        self.screen = OpenApiScreen(client)
        self.plate = OpenApiPlate(client)
        self.short = OpenApiShort(client)
        self.basic = OpenApiBasicData(client)
        self.ipo = OpenApiIpo(client)
        self.watchlist = OpenApiWatchlist(client)
        self.derivatives = OpenApiDerivatives(client)
        self.f10 = OpenApiF10(client)


class FutuData:
    """富途实时直通：17 个数据方法 + ``handle(endpoint, payload)`` 信封分发 + 通道路由。

    ``call`` 注入口（MCP 通道替身，同 FutuBroker 口径）；缺省惰性导入
    ``trading_datasource.futu_mcp.call_tool``（token 自动续期由其 auto_refresh 承担）。
    ``market`` 注入口（OpenAPI 通道替身）；缺省惰性构造
    ``trading_datasource.futu_openapi.OpenApiMarket(OpenApiClient(CredentialStore(...)))``。

    通道选择：显式 ``channel`` 优先；注入 ``call`` 替身钉住 mcp、注入 ``market`` 替身
    钉住 openapi（测试的确定性）；否则惰性读 ``futu_channel`` 配置（默认 mcp）。
    """

    def __init__(self, call=None, timeout=DEFAULT_TIMEOUT_SECONDS, home=None,
                 channel=None, market=None, credential_path=None, push=None,
                 dataplane=None):
        self._call = call
        self.timeout = timeout
        self.home = home
        self._credential_path = credential_path
        self._market_override = market
        # WP12 任务 4：数据面八方法组替身注入口（与 market 同口径——注入即钉住 openapi）
        self._dataplane_override = dataplane
        self._client_built = None
        # WP8 任务 4：WS 行情推送的进程内快照缓存（None = 未接线，rt_quote 走通道）
        self._push = push
        if channel is not None:
            self._channel = channel
        elif call is not None:
            self._channel = CHANNEL_MCP  # 注入 MCP 替身即钉住 MCP 通道（测试确定性）
        elif market is not None or dataplane is not None:
            self._channel = CHANNEL_OPENAPI
        else:
            self._channel = None  # 惰性读 futu_channel 配置

    # ---- 通道层 ----
    @property
    def channel(self):
        if self._channel is None:
            self._channel = load_channel(self.home)
        return self._channel

    def _client(self):
        """OpenAPI 客户端单例：行情组与 WP12 数据面八组**共用**（凭据/续期/限频只有一份）。"""
        if self._client_built is None:
            from trading_datasource.futu_openapi import (  # noqa: PLC0415
                CredentialStore, OpenApiClient)
            self._client_built = OpenApiClient(CredentialStore(self._credential_path))
        return self._client_built

    @property
    def market(self):
        """OpenAPI 行情后端（注入替身优先；缺省 OpenApiMarket + 默认凭据路径）。"""
        if self._market_override is not None:
            return self._market_override
        if getattr(self, "_market_built", None) is None:
            from trading_datasource.futu_openapi import OpenApiMarket  # noqa: PLC0415
            self._market_built = OpenApiMarket(self._client())
        return self._market_built

    @property
    def dataplane(self):
        """WP12 数据面八方法组（注入替身优先；缺省与 ``market`` 共用同一 client）。"""
        if self._dataplane_override is not None:
            return self._dataplane_override
        if getattr(self, "_dataplane_built", None) is None:
            self._dataplane_built = DataPlaneGroups(self._client())
        return self._dataplane_built

    def _openapi_ready(self):
        """channel=openapi 时凭据是否可用（注入 market 替身视为可用）。

        判定逻辑在模块级 ``openapi_ready``（与 server/trading.py 的 OpenAPI 交易链共用
        同一份实现，避免两条链对「凭据可用」给出两种答案）。
        """
        if self._market_override is not None:
            return True
        return openapi_ready(self._credential_path)

    def _tool(self):
        if self._call is None:
            from trading_datasource.futu_mcp import call_tool  # noqa: PLC0415
            self._call = call_tool
        return self._call

    @staticmethod
    def _classify(error):
        """FutuUnavailable 文本 → FutuDataError（业务/凭证/网络三分，见文件头）。"""
        message = str(error) or error.__class__.__name__
        ret = _RET_CODE_RE.search(message)
        if ret or _STATUS_RE.search(message):
            # 业务错误：消息含 ret_code/errmsg 截断；A 股 -9 附替代路径（数据事实）。
            ret_code = int(ret.group(1)) if ret else None
            errmsg = message.split(": ", 1)[-1]
            text = f"富途业务错误（ret_code={ret_code if ret_code is not None else 'n/a'}）：{errmsg[:200]}"
            details = {"ret_code": ret_code} if ret_code is not None else {}
            if ret_code == -9:
                text = f"{text}（{A_SHARE_RT_HINT}）"
            return FutuDataError(text, kind="business", details=details)
        lowered = message.lower()
        if any(semantics in lowered or semantics in message for semantics in _AUTH_SEMANTICS):
            return FutuDataError(f"富途通道不可用：{message[:200]}{AUTH_HINT}", kind="unavailable")
        return FutuDataError(f"富途通道不可用：{message[:200]}", kind="unavailable")

    # ---- 取数路由（WP8 任务 2）：channel 决定后端 ----
    def _fetch(self, endpoint, arguments):
        """按通道取一次数据；任何通道异常分类成 FutuDataError，绝不冒充成功。"""
        if self.channel == CHANNEL_OPENAPI:
            if not self._openapi_ready():
                raise FutuDataError(
                    f"OpenAPI 通道不可用：未配置 openapi 凭据"
                    f"（~/.dsh/futu-openapi.json）{OPENAPI_AUTH_HINT}",
                    kind="openapi-unavailable")
            return self._fetch_openapi(endpoint, arguments)
        return self._fetch_mcp(endpoint, arguments)

    def _fetch_mcp(self, endpoint, arguments):
        """MCP 直通（既有行为，零变化）：call_tool → 分类 → data 透传。"""
        if endpoint in DATAPLANE_ENDPOINTS:
            # WP12 数据面端点在 mcp 通道**无登记上游**：托管 MCP 的工具名与参数形状
            # 未逐项核对（锁定表只核对 REST 面），不猜名、不静默降级——如实拒绝并给出
            # 可行路径（fail-closed，与「通道不可用如实报错」同一纪律）。
            raise FutuDataError(
                f"{endpoint} 仅支持 openapi 通道：mcp 通道未登记该端点的上游工具"
                f"（上游工具名与参数形状未核对，禁止猜名）。请配置 openapi 凭据"
                f"（~/.dsh/futu-openapi.json）并把 futu_channel 设为 openapi。",
                kind="unavailable")
        # None = 未提供（与 OpenApiMarket._body 同规）：可选参数缺省不显式传 null
        arguments = {key: value for key, value in arguments.items() if value is not None}
        try:
            data = self._tool()(FUTU_TOOLS[endpoint], arguments,
                                timeout=self.timeout, auto_refresh=True)
        except FutuDataError:
            raise
        except Exception as error:  # noqa: BLE001 —— 通道层一切失败按特征分流（文件头）
            raise self._classify(error) from error
        if not isinstance(data, (dict, list)):
            # 通道实测：quote_order_book 的 data 是数组（[{books: …}]），list 同样合法透传
            raise FutuDataError(f"{endpoint}: 富途返回不是对象或数组", kind="unavailable")
        if endpoint == "info_search" and data == {}:
            # 通道事实：quote_news_search 实测恒空（TOOL-LIMITS）；归一化为形状表
            # 要求的 {"news_list": []}，使两通道（及 TTL 缓存形状校验）同规。
            return {"news_list": []}
        return data

    def _fetch_openapi(self, endpoint, arguments):
        """OpenAPI 后端：参数适配 → OpenApiMarket → 归一化（d 已由方法层并好 pagination）。

        错误分流（与交易写路径同一份错误分类，措辞按读路径）：
          * ``UnexpectedResponse``（5xx/429/非 JSON/非信封）→ ``trading/futu-unavailable``
            （「通道没给出业务结论」不是业务错误；429 的 Retry-After 进 details）；
          * 其余 ``OpenApiError``（``s==error`` / ``ret_code!=0``）→ ``trading/futu-error``；
          * 传输异常 → ``trading/futu-unavailable``。
        """
        from trading_datasource.futu_openapi import (  # noqa: PLC0415
            OpenApiError, UnexpectedResponse)
        try:
            if endpoint in DATAPLANE_ADAPTERS:
                # WP12 任务 4：数据面八方法组（与 market 共用 client，注入替身走同一入口）
                value = DATAPLANE_ADAPTERS[endpoint](self.dataplane, arguments)
            else:
                value = OPENAPI_ADAPTERS[endpoint](self.market, arguments)
        except FutuDataError:
            raise
        except ValueError as error:  # noqa: B901 —— OpenApiMarket 参数白名单（本地校验）
            raise _param_error(str(error)) from error
        except UnexpectedResponse as error:  # noqa: B901 —— 非信封/5xx/429：无业务结论
            status = error.errcode if error.errcode is not None else "n/a"
            details = {"retry_after": str(error.retry_after)} if error.retry_after else {}
            raise FutuDataError(
                f"OpenAPI 通道不可用（HTTP {status}）：{str(error.errmsg)[:200]}",
                kind="unavailable", details=details) from error
        except OpenApiError as error:  # noqa: B901 —— 信封 s==error / ret_code!=0
            errcode = error.errcode if error.errcode is not None else "n/a"
            raise FutuDataError(
                f"富途业务错误（errcode={errcode}）：{str(error.errmsg)[:200]}",
                kind="business",
                details={"errcode": error.errcode}
                if error.errcode is not None else {}) from error
        except Exception as error:  # noqa: BLE001 —— 网络/传输失败按通道不可用
            raise FutuDataError(f"OpenAPI 通道不可用：{str(error)[:200]}",
                                kind="unavailable") from error
        if not isinstance(value, (dict, list)):
            raise FutuDataError(f"{endpoint}: OpenAPI 返回不是对象或数组",
                                kind="unavailable")
        if endpoint == "info_search" and value == {}:
            # 通道事实：MCP 侧 quote_news_search 实测恒空；OpenAPI 侧同端点也可能回空对象
            # （合法空结果）。两侧同规归一化为形状表要求的 {"news_list": []}，避免
            # ENDPOINT_SHAPE 把合法空结果判成「载荷不完整」。
            return {"news_list": []}
        return value

    # ---- 数据方法（参数白名单 + 必填/类型校验，坏参数零通道调用）----
    def rt_quote(self, payload):
        """实时报价快照：codes 1..10 个 → 上游 code_list。

        WP8 任务 4：``futu_channel=openapi`` 且行情 WS 在跑时，先看进程内推送快照
        （TTL 语义由 ``QuoteSnapshotCache`` 承担）；命中即返回推送数据并标注
        ``source: "push"``（与 REST 取的形状不同——推送帧的字段面由上游决定，
        调用方可据 ``source`` 判别）。未命中一律走通道取数，行为与既有逐字一致。
        """
        codes = payload.get("codes")
        if not isinstance(codes, list) or not 1 <= len(codes) <= 10:
            raise _param_error("rt_quote 的 codes 必须是 1..10 个标的代码的列表")
        normalized = [_normalize_code(code, "codes") for code in codes]
        hit = self._push_lookup(normalized)
        if hit is not None:
            return hit
        return self._fetch("rt_quote", {"code_list": normalized})

    def _push_lookup(self, codes):
        """推送快照命中 → value（``source: "push"``）；未接线/未命中/过期 → None。"""
        if self._push is None:
            return None
        lookup = getattr(self._push, "lookup", None)
        if lookup is None:
            return None
        try:
            hit = lookup(codes)
        except Exception:  # noqa: BLE001 —— 推送缓存只是加速，坏缓存不许打断取数
            return None
        if not hit:
            return None
        return {"source": "push", "code_list": hit["entries"],
                "pushed_at": _iso_from_ms(hit["at_ms"]),
                "ttl_ms": getattr(self._push, "ttl_ms", None)}

    def rt_order_book(self, payload):
        """实时盘口：档数随行情权限（HK 10 / US 60 / A 股不可用），不假定固定档数。"""
        return self._fetch("rt_order_book", {"code": _required_code(payload)})

    def capital_flow(self, payload):
        """分钟级资金流（A 股实测可用——A 股实时报价的替代路径之一）。"""
        return self._fetch("capital_flow", {"symbol": _required_code(payload)})

    def capital_flow_history(self, payload):
        """历史资金流：days（默认 30，1..1000）→ 上游 count。"""
        code = _required_code(payload)
        days = payload.get("days", 30)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 1000:
            raise _param_error("capital_flow_history 的 days 必须是 1..1000 的整数")
        return self._fetch("capital_flow_history", {"symbol": code, "count": days})

    def capital_distribution(self, payload):
        """日内资金分布：大中小单累计净流入/流出。"""
        return self._fetch("capital_distribution", {"symbol": _required_code(payload)})

    def option_expiration(self, payload):
        """期权到期日列表（上游实名 quote_option_expiration_date，见文件头）。"""
        return self._fetch("option_expiration", {"symbol": _required_code(payload)})

    def option_chain(self, payload):
        """期权链：field_filter 可选透传（上游当前对该参数不生效，字段裁剪用 option_screen）。"""
        code = _required_code(payload)
        arguments = {"symbol": code}
        if "field_filter" in payload and payload.get("field_filter") is not None:
            arguments["field_filter"] = _require_dict(
                payload["field_filter"], "option_chain 的 field_filter 给了就必须是非空对象")
        return self._fetch("option_chain", arguments)

    def option_screen(self, payload):
        """期权筛选：filter 必须带非空 field_filter（上游陷阱，见 TOOL-LIMITS）与非空 strategy。"""
        screen = payload.get("filter")
        screen = _require_dict(screen, "option_screen 需要 filter 对象（必须含非空 field_filter）")
        unknown = sorted(set(screen) - set(_SCREEN_KEYS))
        if unknown:
            raise _param_error(f"option_screen filter 含不支持的字段：{unknown}")
        _require_dict(screen.get("field_filter"),
                      "option_screen 必须带非空 field_filter（省略时上游只返回 4 个默认字段、"
                      "其余全 null，见 docs/TOOL-LIMITS.md）")
        _require_dict(screen.get("strategy"),
                      'option_screen 必须带非空 strategy（上游必填，如 {"market_category_list": [1]}）')
        limit = screen.get("limit")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)
                                  or not 0 <= limit <= 1000):
            raise _param_error("option_screen 的 limit 必须是 0..1000 的整数")
        next_key = screen.get("next_key")
        if next_key is not None and not isinstance(next_key, str):
            raise _param_error("option_screen 的 next_key 必须是字符串（分页游标）")
        exact = screen.get("request_exact_data")
        if exact is not None and (isinstance(exact, bool) or not isinstance(exact, int)):
            raise _param_error("option_screen 的 request_exact_data 必须是整数")
        for key in ("sort_obj", "strategy_param"):
            if screen.get(key) is not None and not isinstance(screen[key], dict):
                raise _param_error(f"option_screen 的 {key} 必须是对象")
        arguments = {key: screen[key] for key in _SCREEN_KEYS if key in screen}
        return self._fetch("option_screen", arguments)

    # ---- WP8 任务 2：行情/基本数据 9 方法（官方参数名 → 两通道适配）----
    def market_snapshot(self, payload):
        """行情快照：codes 1..400（官方 snapshot 上限 400/次）。"""
        return self._fetch("market_snapshot",
                           {"code_list": _codes_list(payload, "codes",
                                                     _market_class().MAX_CODE_LIST)})

    def cur_kline(self, payload):
        """当前 K 线：code + num 必填（1..370）；ktype/autype/extended_time 枚举。"""
        market = _market_class()
        return self._fetch("cur_kline", {
            "symbol": _required_code(payload),
            "num": _int_range(payload, "num", 1, market.MAX_KLINE_NUM, required=True),
            "ktype": _enum(payload, "ktype", market.KTYPE_VALUES, 2),
            "autype": _enum(payload, "autype", market.AUTYPE_VALUES, 1),
            "extended_time": _enum(payload, "extended_time",
                                   market.EXTENDED_TIME_VALUES, 0),
        })

    def rt_data(self, payload):
        """分时数据：code + 可选 request_section（时段枚举）。"""
        return self._fetch("rt_data", {
            "symbol": _required_code(payload),
            "request_section": _enum(payload, "request_section",
                                     _market_class().RT_SECTIONS, "NORMAL"),
        })

    def rt_ticker(self, payload):
        """逐笔成交：code + 可选 num（1..750，默认 500）与 period 列表。"""
        market = _market_class()
        period = payload.get("period")
        if period is not None:
            if not isinstance(period, list) or \
                    any(item not in market.TICKER_PERIODS for item in period):
                raise _param_error(
                    f"period 必须是 {sorted(market.TICKER_PERIODS)} 的列表")
        return self._fetch("rt_ticker", {
            "symbol": _required_code(payload),
            "num": _int_range(payload, "num", 1, market.MAX_TICKER_NUM,
                              default=500),
            "period": period,
        })

    def info_basicinfo(self, payload):
        """标的静态信息：codes 1..400。"""
        return self._fetch("info_basicinfo",
                           {"code_list": _codes_list(payload, "codes",
                                                     _market_class().MAX_CODE_LIST)})

    def info_trading_days(self, payload):
        """交易日历：market/start/end 全必填（market 大写枚举；start<=end）。"""
        market_name = payload.get("market")
        if not isinstance(market_name, str) or not market_name.strip():
            raise _param_error("缺少必填字段 market")
        market_name = market_name.strip().upper()
        start = _required_date(payload, "start")
        end = _required_date(payload, "end")
        if market_name not in _market_class().TRADING_MARKETS:
            raise _param_error(
                f"market 取值非法：{market_name}（允许：{sorted(_market_class().TRADING_MARKETS)}）")
        if start > end:
            raise _param_error("start 不能晚于 end")
        return self._fetch("info_trading_days",
                           {"market": market_name, "start": start, "end": end})

    def info_search(self, payload):
        """资讯搜索：keyword 必填；size 1..50（默认 10）；news_type/sort_type/lang 枚举。"""
        market = _market_class()
        keyword = payload.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            raise _param_error("keyword 必须是非空字符串")
        return self._fetch("info_search", {
            "symbol": keyword,
            "size": _int_range(payload, "size", 1, 50, default=10),
            "news_type": _enum(payload, "news_type", market.NEWS_TYPES, None),
            "sort_type": _enum(payload, "sort_type", market.SEARCH_SORT_TYPES, None),
            "lang": _enum(payload, "lang", market.SEARCH_LANGS, None),
        })

    def info_market_state(self, payload):
        """市场状态：codes 1..400 + 可选美股盘前盘后/夜盘布尔开关。"""
        market = _market_class()
        arguments = {"code_list": _codes_list(payload, "codes", market.MAX_CODE_LIST)}
        for field in ("is_contain_ba", "is_contain_overnight"):
            value = _optional_bool(payload, field)
            if value is not None:
                arguments[field] = value
        return self._fetch("info_market_state", arguments)

    def quote_history_kline_v2(self, payload):
        """历史 K 线 v2（REST history-kline 新端点；既有 series 为 WP6 契约不动）：
        code + end 必填；num 1..370（默认 370）；ktype/autype/extended_time 枚举。"""
        market = _market_class()
        return self._fetch("quote_history_kline_v2", {
            "symbol": _required_code(payload),
            "start": _optional_date(payload, "start"),
            "end": _required_date(payload, "end"),
            "ktype": _enum(payload, "ktype", market.KTYPE_VALUES, 2),
            "autype": _enum(payload, "autype", market.AUTYPE_VALUES, 1),
            "num": _int_range(payload, "num", 1, market.MAX_KLINE_NUM, default=370),
            "extended_time": _enum(payload, "extended_time",
                                   market.EXTENDED_TIME_VALUES, 0),
        })

    # ---- WP12 任务 4：富途数据面端点（锁定表 §C）----
    # 校验分层：本层只做**必填/类型/形状**（坏参数零通道调用）；取值上界与枚举由传输层
    # 方法组按锁定表常量判（同样是本地校验，零网络往返）——区间/枚举只有一份事实源。
    def economic_calendar_hot(self, payload):
        """热门经济事件：limit 1..20；next_key/date/timezone 选填。"""
        return self._fetch("economic_calendar_hot", {
            "limit": _int_range(payload, "limit", 1, 20),
            "next_key": _optional_str(payload, "next_key"),
            "date": _optional_date(payload, "date"),
            "timezone": _optional_scalar(payload, "timezone"),
        })

    def economic_calendar_search(self, payload):
        """经济事件搜索：keyword 与 search_type 必填（官方必填）；limit/time_order_type
        官方未给区间 → 标量直通（不发明）。"""
        return self._fetch("economic_calendar_search", {
            "keyword": _required_str(payload, "keyword"),
            "search_type": _required_scalar(payload, "search_type"),
            "limit": _optional_scalar(payload, "limit"),
            "next_key": _optional_str(payload, "next_key"),
            "time_order_type": _optional_scalar(payload, "time_order_type"),
        })

    def info_owner_plate(self, payload):
        """所属板块：code 必填（板块归属，行业中性化的前提数据）。"""
        return self._fetch("info_owner_plate", {"symbol": _required_code(payload)})

    def info_rehab(self, payload):
        """复权因子：code 必填；divi_mode 选填（枚举由传输层判）。"""
        return self._fetch("info_rehab", {
            "symbol": _required_code(payload),
            "divi_mode": _optional_scalar(payload, "divi_mode"),
        })

    def plate_list(self, payload):
        """板块列表：market 与 plate_class 必填（枚举与 REGION 市场限制由传输层判）。"""
        return self._fetch("plate_list", {
            "market": _required_str(payload, "market"),
            "plate_class": _required_str(payload, "plate_class"),
        })

    def plate_stock(self, payload):
        """板块成分股：plate_code 必填；排序/分页参数选填。"""
        return self._fetch("plate_stock", {
            "plate_code": _required_str(payload, "plate_code"),
            "sort_field": _optional_scalar(payload, "sort_field"),
            "ascend": _optional_bool(payload, "ascend"),
            "price_type": _optional_scalar(payload, "price_type"),
            "leverage_direction": _optional_scalar(payload, "leverage_direction"),
            "leverage_multiple": _optional_scalar(payload, "leverage_multiple"),
            "next_key": _optional_str(payload, "next_key"),
            "limit": _optional_int(payload, "limit"),
        })

    def stock_screen(self, payload):
        """股票筛选：screen_queries 必填（11 选 1 查询类型，形状由传输层判）；
        retrieve_queries/sort/sorts/分页/自选持仓范围选填。"""
        user_mode = payload.get("user_stock_list_mode")
        if user_mode is not None and (isinstance(user_mode, bool)
                                      or user_mode not in (0, 1, 2)):
            raise _param_error("user_stock_list_mode 取值必须是 0/1/2")
        return self._fetch("stock_screen", {
            "screen_queries": _required_list(payload, "screen_queries"),
            "retrieve_queries": _optional_list(payload, "retrieve_queries"),
            "sort": _optional_dict(payload, "sort"),
            "sorts": _optional_list(payload, "sorts"),
            "next_key": _optional_str(payload, "next_key"),
            "limit": _optional_int(payload, "limit"),
            "watchlist_stock_ids": _optional_list(payload, "watchlist_stock_ids"),
            "holding_stock_ids": _optional_list(payload, "holding_stock_ids"),
            "user_stock_list_mode": user_mode,
        })

    def warrant_screen(self, payload):
        """窝轮筛选（HTTP-only：数据面完整，平台不策略化窝轮）：全参数选填。"""
        return self._fetch("warrant_screen", {
            "market_type": _optional_scalar(payload, "market_type"),
            "is_delay": _optional_bool(payload, "is_delay"),
            "only_count": _optional_bool(payload, "only_count"),
            "stock_owner": _optional_str(payload, "stock_owner"),
            "screen_groups": _optional_list(payload, "screen_groups"),
            "sorts": _optional_list(payload, "sorts"),
            "next_key": _optional_str(payload, "next_key"),
            "limit": _optional_int(payload, "limit"),
        })

    def ipo_list(self, payload):
        """新股列表：market 必填（官方按市场独立端点，小写枚举）；request_type 选填。"""
        return self._fetch("ipo_list", {
            "market": _required_str(payload, "market"),
            "request_type": _optional_int(payload, "request_type"),
        })

    def short_daily_volume(self, payload):
        """每日卖空成交：code 必填；count 选填（上界由传输层判）。"""
        return self._fetch("short_daily_volume", {
            "symbol": _required_code(payload),
            "count": _optional_int(payload, "count"),
        })

    def short_interest(self, payload):
        """空头持仓：code 必填；count 选填（上界由传输层判）。"""
        return self._fetch("short_interest", {
            "symbol": _required_code(payload),
            "count": _optional_int(payload, "count"),
        })

    def watchlist_list(self, payload):
        """自选股列表：group_name 必填（用户身份缺失时上游 -9，按权限语义如实呈现）。"""
        return self._fetch("watchlist_list", {"group_name": _required_str(payload, "group_name")})

    def watchlist_groups(self, payload):
        """自选股分组：group_type 选填（ALL/CUSTOM/SYSTEM，大小写敏感由传输层判）。"""
        return self._fetch("watchlist_groups",
                           {"group_type": _optional_str(payload, "group_type")})

    def modify_user_security(self, payload):
        """修改自选（HTTP-only，仅 Web 用户操作，不进 MCP 工具面）：op 与 code_list 必填。"""
        return self._fetch("modify_user_security", {
            "op": _required_str(payload, "op"),
            "code_list": _required_list(payload, "code_list"),
            "group_name": _optional_str(payload, "group_name"),
        })

    def f10_detail(self, payload):
        """个股深度数据聚合：code + section 必填（section 白名单 = 传输层 SECTIONS 26 项，
        单一事实源）；section 参数经 params 对象下传（键集合由传输层方法判）。"""
        section = _required_str(payload, "section")
        if section not in _f10_class().SECTIONS:
            raise _param_error(f"section 取值非法：{section!r}"
                               f"（允许：{sorted(_f10_class().SECTIONS)}）")
        return self._fetch("f10_detail", {
            "symbol": _required_code(payload),
            "section": section,
            "params": _optional_dict(payload, "params"),
        })

    def derivative_detail(self, payload):
        """衍生品聚合：code + section 必填（section 白名单 = 锁定表 §C.7 四项）。"""
        section = _required_str(payload, "section")
        if section not in DERIVATIVE_SECTIONS:
            raise _param_error(f"section 取值非法：{section!r}"
                               f"（允许：{sorted(DERIVATIVE_SECTIONS)}）")
        return self._fetch("derivative_detail", {
            "symbol": _required_code(payload),
            "section": section,
            "params": _optional_dict(payload, "params"),
        })

    # ---- 信封分发 ----
    _METHODS = {
        "rt_quote": "rt_quote",
        "rt_order_book": "rt_order_book",
        "capital_flow": "capital_flow",
        "capital_flow_history": "capital_flow_history",
        "capital_distribution": "capital_distribution",
        "option_expiration": "option_expiration",
        "option_chain": "option_chain",
        "option_screen": "option_screen",
        "market_snapshot": "market_snapshot",
        "cur_kline": "cur_kline",
        "rt_data": "rt_data",
        "rt_ticker": "rt_ticker",
        "info_basicinfo": "info_basicinfo",
        "info_trading_days": "info_trading_days",
        "info_search": "info_search",
        "info_market_state": "info_market_state",
        "quote_history_kline_v2": "quote_history_kline_v2",
        # WP12 任务 4：数据面端点（方法名与端点名同形）
        "economic_calendar_hot": "economic_calendar_hot",
        "economic_calendar_search": "economic_calendar_search",
        "info_owner_plate": "info_owner_plate",
        "info_rehab": "info_rehab",
        "plate_list": "plate_list",
        "plate_stock": "plate_stock",
        "stock_screen": "stock_screen",
        "warrant_screen": "warrant_screen",
        "ipo_list": "ipo_list",
        "short_daily_volume": "short_daily_volume",
        "short_interest": "short_interest",
        "watchlist_list": "watchlist_list",
        "watchlist_groups": "watchlist_groups",
        "modify_user_security": "modify_user_security",
        "f10_detail": "f10_detail",
        "derivative_detail": "derivative_detail",
    }

    def dispatch(self, endpoint, payload):
        if endpoint not in FUTU_TOOLS and endpoint not in DATAPLANE_ENDPOINTS:
            raise _param_error(f"未知富途直通端点：{endpoint}")
        payload = payload if isinstance(payload, dict) else {}
        return getattr(self, self._METHODS[endpoint])(payload)

    def handle(self, endpoint, payload, force=False):
        """→ {ok:true, value} / {ok:false, error:{code, message≤300, details}}。

        TTL 端点（CACHED_FUTU_ENDPOINTS）在 **value 层**缓存（缓存数据而非信封；
        ``force``/载荷 ``_refresh`` 旁路 TTL，与 caches.cached 同语义）；错误信封
        原样透传、绝不进缓存。业务失败是正常信封（绝不抛到 500 / isError=true）；
        通道层意外也落 unavailable。
        """
        payload = payload if isinstance(payload, dict) else {}
        if force or payload.get("_refresh") is True:
            force = True
        if endpoint in CACHED_FUTU_ENDPOINTS:
            return self._cached(endpoint, payload, force)
        return self._envelope(endpoint, payload)

    def _envelope(self, endpoint, payload):
        try:
            return {"ok": True, "value": self.dispatch(endpoint, payload)}
        except FutuDataError as error:
            return {"ok": False, "error": {"code": error.code, "message": str(error)[:300],
                                           "details": error.details}}
        except Exception as error:  # noqa: BLE001 —— 直通层不留未分类异常
            return {"ok": False, "error": {"code": UNAVAILABLE_CODE,
                                           "message": f"富途通道不可用：{str(error)[:200]}",
                                           "details": {}}}

    def _cached(self, endpoint, payload, force):
        """value 层 TTL 缓存（语义同 caches.cached：命中→cached:true；错误不缓存）。"""
        from server import caches  # noqa: PLC0415 —— 保持本模块可独立导入
        ttl = caches.CACHE_TTL_MS.get(endpoint, 0)
        if not force and ttl > 0:
            hit = caches.read(endpoint, payload, ttl)
            if hit is not None:
                at, value = hit
                if caches.matches_shape(endpoint, value):
                    return {"ok": True, "value": value, "cached": True,
                            "cached_at": caches.iso_from_ms(at)}
        envelope = self._envelope(endpoint, payload)
        if not envelope.get("ok"):
            return envelope
        value = envelope["value"]
        if not caches.matches_shape(endpoint, value):
            return {"ok": False, "error": {
                "code": UNAVAILABLE_CODE,
                "message": f"{endpoint} 返回的载荷不完整，已按失败处理",
                "details": {}}}
        entry = caches.write(endpoint, payload, value) if ttl > 0 \
            else {"at": time.time() * 1000}
        return {"ok": True, "value": value, "cached": False,
                "cached_at": caches.iso_from_ms(entry["at"])}
