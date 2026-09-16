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
import re
import time

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
    """
    from server.config import config_path  # noqa: PLC0415 —— 保持本模块可独立导入
    path = config_path(home)
    import json
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CHANNEL_MCP
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"trading-platform.json 解析失败：{error}") from error
    channel = raw.get("futu_channel") if isinstance(raw, dict) else None
    return channel if channel in (CHANNEL_MCP, CHANNEL_OPENAPI) else CHANNEL_MCP


def openapi_ready(credential_path=None):
    """OpenAPI 凭据是否可用（**行情与交易两条链共用的唯一判定**）。

    oauth → access_token/refresh_token 至少有一个（可刷新）；
    appkey → app_key 与 private_key_path 齐备**且私钥文件可加载**
    （``AppKeySigner.from_path``：文件缺失/坏 PEM/算法不支持/私钥类型不符 → 不可用）；
    其余（文件缺失/坏 JSON/mode 未配置）→ False。

    「私钥可加载」是凭据就绪的一部分：只看路径存在会让 live 写穿过闸门（**人工确认被消耗**）
    后才在签名时失败；判在这里则确认零消耗、零 HTTP 调用、错误码 ``trading/broker-unavailable``
    （见 tests/test_wp8_trading.py 的私钥缺失用例）。
    """
    from trading_datasource.futu_openapi import CredentialStore  # noqa: PLC0415
    try:
        cred = CredentialStore(credential_path).load()
    except (OSError, ValueError):
        return False
    mode = cred.get("mode")
    if mode == "oauth":
        return bool(cred.get("access_token") or cred.get("refresh_token"))
    if mode == "appkey":
        if not (cred.get("app_key") and cred.get("private_key_path")):
            return False
        from trading_datasource.futu_openapi import AppKeySigner  # noqa: PLC0415
        try:
            AppKeySigner.from_path(cred["private_key_path"],
                                   cred.get("algorithm", "Ed25519"))
        except Exception:  # noqa: BLE001 —— 私钥缺失/坏 PEM/算法不支持 → 凭据不可用
            return False
        return True
    return False


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
                 channel=None, market=None, credential_path=None):
        self._call = call
        self.timeout = timeout
        self.home = home
        self._credential_path = credential_path
        self._market_override = market
        if channel is not None:
            self._channel = channel
        elif call is not None:
            self._channel = CHANNEL_MCP  # 注入 MCP 替身即钉住 MCP 通道（测试确定性）
        elif market is not None:
            self._channel = CHANNEL_OPENAPI
        else:
            self._channel = None  # 惰性读 futu_channel 配置

    # ---- 通道层 ----
    @property
    def channel(self):
        if self._channel is None:
            self._channel = load_channel(self.home)
        return self._channel

    @property
    def market(self):
        """OpenAPI 后端（注入替身优先；缺省 OpenApiMarket + 默认凭据路径）。"""
        if self._market_override is not None:
            return self._market_override
        if getattr(self, "_market_built", None) is None:
            from trading_datasource.futu_openapi import (  # noqa: PLC0415
                CredentialStore, OpenApiClient, OpenApiMarket)
            self._market_built = OpenApiMarket(
                OpenApiClient(CredentialStore(self._credential_path)))
        return self._market_built

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
        """实时报价快照：codes 1..10 个 → 上游 code_list。"""
        codes = payload.get("codes")
        if not isinstance(codes, list) or not 1 <= len(codes) <= 10:
            raise _param_error("rt_quote 的 codes 必须是 1..10 个标的代码的列表")
        normalized = [_normalize_code(code, "codes") for code in codes]
        return self._fetch("rt_quote", {"code_list": normalized})

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
    }

    def dispatch(self, endpoint, payload):
        if endpoint not in FUTU_TOOLS:
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
