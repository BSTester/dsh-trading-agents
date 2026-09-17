"""参数校验基类（枚举/区间/日期/正文白名单，坏参数零网络往返）。"""
import re

from .errors import OpenApiError
# ---------------------------------------------------------------------------
class _RestValidators:
    """REST 方法组共用参数校验器（``OpenApiMarket`` / ``OpenApiTrade`` 唯一实现）。

    约定：``None`` = 调用方未提供；必填字段缺省即拒（本地 ``ValueError``，消息面向
    调用方）；枚举/区间在此拒绝，坏参数**不触达网络**；请求体经 ``_body`` 去 None
    （官方接口按缺省处理省略字段）。严格度对齐官方文档：未知枚举值一律拒绝。
    """

    _REQUIRED = object()  # 「必填枚举」哨兵：default=None 表示可省略（请求体去 None）

    def _codes(self, codes, count_max):
        """批量 code_list 校验：1..count_max 个非空字符串（官方 invalid_parameter 面）。"""
        if not isinstance(codes, list) or not 1 <= len(codes) <= count_max:
            raise ValueError(
                f"code_list 必须是 1..{count_max} 个标的代码的列表（如 HK.00700）")
        for code in codes:
            if not isinstance(code, str) or not code.strip():
                raise ValueError(f"code_list 元素必须是非空字符串，得到：{code!r}")
        return list(codes)

    def _int_in(self, value, low, high, name, default=None):
        """整数区间校验：None 走 default；越界/类型错拒绝（bool 是 int 的子类，排除）。"""
        if value is None:
            if default is None:
                raise ValueError(f"{name} 必填（{low}..{high} 的整数）")
            return default
        if isinstance(value, bool) or not isinstance(value, int) \
                or not low <= value <= high:
            raise ValueError(f"{name} 必须是 {low}..{high} 的整数")
        return value

    def _enum_in(self, value, allowed, name, default=_REQUIRED):
        """枚举校验：None 走 default；default 为哨兵时视为必填。"""
        if value is None:
            if default is self._REQUIRED:
                raise ValueError(f"{name} 必填，取值之一：{sorted(allowed)}")
            return default
        if value not in allowed:
            raise ValueError(f"{name} 取值非法：{value!r}（允许：{sorted(allowed)}）")
        return value

    def _csv_enum(self, value, allowed, name, default=None):
        """逗号分隔字符串枚举（官方 ``filter_expiration_cycles`` 的形态）。

        官方类型是**字符串**（网关 pattern 逐字：``^(A|B)(,(A|B))*$``，2026-09-16 实测），
        **不是数组**——传 list 会被出站序列化成 Python repr 并被网关 -3 拒绝，因此这里只
        接受字符串（list 等非字符串本地拒绝，坏参数零网络往返）；``""`` 视为未提供。
        元素两端空白容忍并归一（去掉后按原文逗号拼接，保证与网关 pattern 一致）。
        """
        if value is None or value == "":
            return default
        if not isinstance(value, str):
            raise ValueError(
                f"{name} 必须是逗号分隔的字符串（官方类型 string，如 'WEEK,MONTH'），"
                f"不接受 {type(value).__name__}：{value!r}；允许：{sorted(allowed)}")
        parts = [part.strip() for part in value.split(",")]
        if any(part not in allowed for part in parts):
            raise ValueError(f"{name} 取值非法：{value!r}"
                             f"（允许：{sorted(allowed)}，逗号分隔）")
        return ",".join(parts)

    def _date(self, value, name, required=False):
        """yyyy-MM-dd 日期校验（含日历有效性）。"""
        if value is None or value == "":
            if required:
                raise ValueError(f"{name} 必填（yyyy-MM-dd）")
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是 yyyy-MM-dd 字符串")
        try:
            import datetime as _dt
            _dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} 不是合法日期：{value!r}（yyyy-MM-dd）") from None
        return value

    def _body(self, mapping):
        """去掉 None 值的请求体（官方接口按缺省处理省略字段）。"""
        return {key: value for key, value in mapping.items() if value is not None}

    # ------------------------------------------------------------ 文本/标量/路径
    # WP12 任务 2 起，以下助手由 OpenApiMarket/OpenApiTrade/数据面七组**共用一份实现**
    # （原先只存在于 OpenApiTrade；提升到基类避免第二份实现漂移）。

    def _path_token(self, value, name):
        """路径片段：非空字符串且不含路径分隔符/空白（避免拼出意外路径）。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空字符串")
        text = value.strip()
        if any(char in text for char in "/?#") or any(char.isspace() for char in text):
            raise ValueError(f"{name} 含非法字符（不得含 / ? # 或空白）：{text[:40]!r}")
        return text

    def _acc_id(self, value):
        """acc_id 进路径：非空字符串且不含路径分隔符/空白（避免拼出意外路径）。

        WP13 审查次要项：原先 ``OpenApiTrade`` 与 ``OpenApiSimTrade`` 各有一份**逐字
        相同**的实现（提升到基类，单一实现）。
        """
        return self._path_token(value, "acc_id")

    def _symbol(self, code):
        """标的代码校验（官方 exchange.symbol 形状；大小写归一为大写）。"""
        if not isinstance(code, str) or not self._SYMBOL_RE.fullmatch(code.strip().upper()):
            raise ValueError(f"code 形如 US.AAPL/HK.00700/SH.600519，得到：{code!r}")
        return code.strip().upper()

    def _symbol_in_path(self, symbol):
        """进路径的标的代码：``_symbol`` 形状 + 官方长度上限 32（锁定表 §C.1/§C.7）。"""
        code = self._symbol(symbol)
        if len(code) > self.SYMBOL_PATH_MAX:
            raise ValueError(f"symbol 长度不得超过 {self.SYMBOL_PATH_MAX}：{code[:40]!r}")
        return code

    def _text(self, value, name, required=True, default=None):
        """数量/价格/编号：官方声明为 string（本层接受数值并转字符串）。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise ValueError(f"{name} 必填")
            return default
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"{name} 必须是字符串或数值，得到：{value!r}")
        return str(value).strip()

    def _text_max(self, value, name, max_len, required=True):
        """有官方长度上限的文本字段（超长本地拒绝，零网络往返）。"""
        text = self._text(value, name, required=required)
        if text is not None and len(text) > max_len:
            raise ValueError(f"{name} 长度不得超过 {max_len}：{text[:40]!r}")
        return text

    def _micros(self, value, name):
        """微秒时间戳：非负整数（官方单位微秒，本层不换算）。

        WP13 任务 2 从 ``OpenApiTrade`` 提升到基类（模拟交易历史订单同口径使用）。
        实测（2026-09-16）：``/sim-trade/{acc_id}/history-orders`` 的 ``time_begin``
        必须为整数，传 ``YYYY-MM-DD`` 字符串报 ``parameter 'time_begin' must be an
        integer``；响应 ``create_time`` 亦为微秒（``1789351326000000``）。
        """
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} 必须是非负整数（微秒时间戳）")
        return value

    def _page_flag(self, value):
        """page_flag 必填但可为空串（空串=从头开始，官方原文）。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("page_flag 必须是字符串（空串=从头开始）")
        return value

    def _page_size(self, value, bounds):
        """分页大小：``bounds`` 由各端点给出（官方各页上界不同，各自钉死）。"""
        if value is None:
            return None
        return self._int_in(value, bounds[0], bounds[1], "page_size")

    def _scalar(self, value, name, required=False, default=None):
        """标量直通（str/int/float，排除 bool/对象/数组）。

        用于锁定表**只给出参数名、未给出枚举或区间**的字段：本层只挡明显的类型错误
        （会拼出 Python repr 的 dict/list），枚举语义由服务端判定并如实抛出——
        不自行发明枚举（「禁止猜测」纪律，见锁定表 §D）。
        """
        if value is None or value == "":
            if required:
                raise ValueError(f"{name} 必填")
            return default
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"{name} 必须是标量（字符串或数值），得到：{value!r}")
        return value

    def _opt_int(self, value, low, high, name):
        """可选整数区间：``None`` 原样通过（不触发 ``_int_in`` 的必填判定）。"""
        if value is None:
            return None
        return self._int_in(value, low, high, name)

    def _merge_pagination(self, d, pagination):
        """信封顶层 pagination 并入 d（对齐 futu_mcp._unwrap；无分页原样返回）。

        数据层是**数组**的端点（实测 ``economic-calendar/search`` 的 data 就是数组，
        分页只在信封顶层）无法 ``{**d}``——数组没有键可并入，旧写法直接 TypeError
        （真机 ``--dataplane`` 自检抓到）。这里按官方 screen 一族使用的词汇归一为
        ``{"items": [...], "pagination": {...}}``：不丢官方游标（``has_more``/
        ``next_key``），也不假装数组是对象；无分页时数组原样返回（不包壳）。
        """
        if not pagination:
            return d
        if isinstance(d, dict):
            return {**d, "pagination": pagination}
        return {"items": d, "pagination": pagination}

    # ------------------------------------------------------------ 错误码语义
    # 锁定表 §A「无数据语义」与 §C.7/§C.8/§D.3 的权限项在此机械落地——两个语义是
    # 官方明示的，不属于「业务解释」：`-10` 是「合法但无数据」，`-9` 是「无权限/身份无效」。

    def _tolerate_no_data(self, call):
        """``-10 no_data`` → 空而非错：返回 ``{"no_data": True}``。

        **不伪造 items/字段**：官方只说「无数据」，本层就不替它编造空数组或零值——
        调用方据 ``no_data`` 如实展示「该标的当前无此数据」。
        """
        try:
            return call()
        except OpenApiError as error:
            if error.errcode == self.NO_DATA_ERRCODE:
                return {"no_data": True}
            raise

    def _permission_note(self, call, note):
        """``-9`` → 如实抛出且可读：消息 = 本层说明 + 官方原文；**不重试、不当空数据**。"""
        try:
            return call()
        except OpenApiError as error:
            if error.errcode == self.PERMISSION_ERRCODE:
                raise OpenApiError(f"{note}：{error.errmsg}",
                                   errcode=error.errcode,
                                   need_order_confirm=error.need_order_confirm,
                                   confirm_id=error.confirm_id,
                                   jump_url=error.jump_url,
                                   retry_after=error.retry_after) from error
            raise

    #: 标的代码形状（官方 exchange.symbol，如 HK.00700/US.AAPL/SH.600519）
    _SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,6}\.[A-Za-z0-9._]+$")
    #: 标的代码进路径时的官方长度上限（锁定表 §C.1/§C.7：symbol ≤32 字符）
    SYMBOL_PATH_MAX = 32
    #: 官方「无数据」错误码（锁定表 §A 无数据语义）
    NO_DATA_ERRCODE = -10
    #: 官方「权限/用户身份」错误码（锁定表 §C.7/C.8/§D.3）
    PERMISSION_ERRCODE = -9


# ---------------------------------------------------------------------------
# OpenApiMarket：行情 REST 方法组（WP8 任务 2）
# ---------------------------------------------------------------------------
# 路径与参数逐项对照官方文档（2026-09-16 web_fetch 实抓，前缀 /api/v1.0/quote）：
#   realtime/market-snapshot … basic-data/search、capital-flow/*、derivatives/*、
#   screening/option-screen（各方法 docstring 里带精确路径与参数）。
# 约定：每个方法做参数白名单/类型/区间校验（本地 ValueError，消息面向调用方）→
# client.request(request_meta) → 返回信封 d。符号归一**不在这里**做（futu_data 层统一
# 走 market.to_futu_symbol，全仓库一份归一），这里只做形状校验。
# 三个分页端点（capital_flow_history/option_screen/history_kline）用 request_meta 把
# 信封顶层 pagination 并入返回值——与 MCP 通道 futu_mcp._unwrap 的形状一致，使
# futu_data 的双通道路由可以产出同形状响应（归一化 fixture 见 tests/test_wp8_market.py）。
