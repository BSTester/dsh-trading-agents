# WP8：工作台富途实时数据直通（资金流/衍生品/实时报价，8 端点/工具）。
#
# 职责边界：skills 需要而本地无缓存的数据，由服务端**实时**经富途托管 MCP 通道获取后
# 返回（技能不再回退直连富途）。本模块只做三件事：
#   1. 通道收敛 —— 唯一触达 ``trading_datasource.futu_mcp.call_tool`` 的直通层
#      （token 缺失/过期由 call_tool 的 auto_refresh 自动处理，这里不复制协议）；
#   2. 错误分类 —— call_tool 把业务错误包成 FutuUnavailable 文本（isError=false 信封
#      同源），这里按特征重新分流：``ret_code!=0`` / ``s==error`` → 业务错误
#      （trading/futu-error）；token 缺失/失效 → 通道不可用（trading/futu-unavailable，
#      消息指向 scripts/futu_auth.py）；网络/响应形状 → 通道不可用；
#   3. 参数面 —— 每个数据方法的参数白名单 + 必填/类型校验（code 归一同 series 的
#      ticker 规则，即 ``market.to_futu_symbol``），坏参数在触达通道前拒绝。
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
# 缓存口径：8 个端点全部实时（TTL 0），**不进** caches.CACHE_TTL_MS / ENDPOINT_SHAPE，
# 与 plan-execute / trade_* / account_* 同类；零缓存由 app.py 分支直连本模块保证。
#
# 错误码族（与既有 trading/* 一致，全部 {ok:false, error:{code, message≤300, details}}）：
#   trading/futu-unavailable   通道不可用（未授权/网络/响应形状）→ 指向安装/授权
#   trading/futu-error         富途业务错误（ret_code!=0 / s=error）
#   trading/invalid-operation  参数白名单/必填/类型（与 handle 层载荷校验同码）
import re

# 错误码（直通层两种信封 + 参数拒绝沿用 invalid-operation）。
UNAVAILABLE_CODE = "trading/futu-unavailable"
FUTU_ERROR_CODE = "trading/futu-error"
PARAM_CODE = "trading/invalid-operation"

# 通道事实：A 股实时 -9 的固定提示（数据事实，逐字进错误消息，锁定测试钉住）。
A_SHARE_RT_HINT = "A 股实时无权限：可用 capital_flow / history-kline 替代"
# token 缺失/失效的固定指引：指向安装/授权脚本（scripts/futu_auth.py）。
AUTH_HINT = "（富途 token 缺失或已失效：运行 scripts/futu_auth.py 完成安装/授权）"

DEFAULT_TIMEOUT_SECONDS = 30

# 端点 → 富途 MCP 工具名。键集 ≡ store_access.FUTU_ENDPOINTS（锁定测试比对）。
# option_expiration 的上游实名见文件头通道事实。
FUTU_TOOLS = {
    "rt_quote": "quote_stock_quote",
    "rt_order_book": "quote_order_book",
    "capital_flow": "quote_capital_flow",
    "capital_flow_history": "quote_capital_flow_history",
    "capital_distribution": "quote_capital_distribution",
    "option_expiration": "quote_option_expiration_date",
    "option_chain": "quote_option_chain",
    "option_screen": "quote_option_screen",
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

_KIND_TO_CODE = {"unavailable": UNAVAILABLE_CODE, "business": FUTU_ERROR_CODE,
                 "parameter": PARAM_CODE}


class FutuDataError(RuntimeError):
    """直通层失败：``kind`` ∈ unavailable/business/parameter 决定信封码。"""

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
    from trading_datasource.market import to_futu_symbol
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


class FutuData:
    """富途实时直通：8 个数据方法 + ``handle(endpoint, payload)`` 信封分发。

    ``call`` 注入口（测试替身同 FutuBroker 口径）；缺省惰性导入
    ``trading_datasource.futu_mcp.call_tool``（token 自动续期由其 auto_refresh 承担）。
    """

    def __init__(self, call=None, timeout=DEFAULT_TIMEOUT_SECONDS):
        self._call = call
        self.timeout = timeout

    # ---- 通道层 ----
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

    def _fetch(self, endpoint, arguments):
        """调一次富途工具；任何通道异常分类成 FutuDataError，绝不冒充成功。"""
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
        return data

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
    }

    def dispatch(self, endpoint, payload):
        if endpoint not in FUTU_TOOLS:
            raise _param_error(f"未知富途直通端点：{endpoint}")
        payload = payload if isinstance(payload, dict) else {}
        return getattr(self, self._METHODS[endpoint])(payload)

    def handle(self, endpoint, payload):
        """→ {ok:true, value} / {ok:false, error:{code, message≤300, details}}。

        业务失败是正常信封（绝不抛到 500 / isError=true）；通道层意外也落 unavailable，
        与 caches.cached 的兜底同口径。
        """
        try:
            return {"ok": True, "value": self.dispatch(endpoint, payload)}
        except FutuDataError as error:
            return {"ok": False, "error": {"code": error.code, "message": str(error)[:300],
                                           "details": error.details}}
        except Exception as error:  # noqa: BLE001 —— 直通层不留未分类异常
            return {"ok": False, "error": {"code": UNAVAILABLE_CODE,
                                           "message": f"富途通道不可用：{str(error)[:200]}",
                                           "details": {}}}
