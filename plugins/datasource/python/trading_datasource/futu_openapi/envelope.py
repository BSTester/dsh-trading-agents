"""响应信封解析（s==ok/error、错误码映射、need_order_confirm 信封）。"""
import json
import urllib.request

from .errors import OpenApiError, OrderConfirmRequired, UnexpectedResponse
def json_body_bytes(json_body):
    """请求体字节（发送与签名哈希共用同一份，保证逐字节一致）。"""
    return json.dumps(json_body, ensure_ascii=False, separators=(",", ":")) \
        .encode("utf-8")


def query_string(query):
    """原始 query 串：dict 按插入序完全转义（safe=""，避免 + / 空格歧义）；str 原样。

    * **同名多值**必须展开：``period=["BEFORE","AFTER"]`` → ``period=BEFORE&period=AFTER``
      （官方 rt-ticker 多值语义）→ ``doseq=True``。缺了它，list 会被序列化成 Python repr
      （``period=%5B%27BEFORE%27...%5D``），网关按非法枚举拒绝（实测 -3 invalid_parameter）；
    * ``None`` = 调用方未提供 → **整条省略**（与请求体 ``_body`` 去 None 同规）。不省略会
      出站 ``start=None``，网关按日期 pattern 拒绝（capital-flow/history 实测 -3）；
    * ``str`` 值不受 doseq 影响（仍是单值）；URL 与 AppKey 签名原文共用本函数，
      保证逐字节一致。
    """
    if query is None:
        return ""
    if isinstance(query, str):
        return query
    pairs = [(key, value) for key, value in query.items() if value is not None]
    return urllib.parse.urlencode(pairs, doseq=True,
                                  quote_via=urllib.parse.quote, safe="")


def _safe_json_dict(body):
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_envelope(status, body):
    """官方响应信封 → d；业务错误信封 → OpenApiError（见 ``parse_envelope_meta``）。

    非 JSON / 非信封（含 5xx、限频页）：抛 ``UnexpectedResponse``（OpenApiError 子类）
    ——「没有业务结论」与「业务拒绝」是两回事，本层不自动重试。
    """
    return parse_envelope_meta(status, body)[0]


def parse_envelope_meta(status, body):
    """``parse_envelope`` 的 (d, 分页) 版本。

    三种形态（**不是同一信封**）：
      * 交易侧 ``{"s":"ok","d":...}`` → (d, None)；``{"s":"error",...}`` →
        OpenApiError / OrderConfirmRequired；
      * 行情侧网关 ``{"ret_code":0,"data":{...},"pagination":{...}}`` —— 分页在**信封
        顶层**而不在 data 内，OpenApiMarket 把它并入返回值以对齐 MCP 通道
        ``futu_mcp._unwrap`` 的形状（那里 pagination 同样被并入 data）；``ret_code!=0``
        → OpenApiError（errcode=ret_code）；无分页时第二个元素为 ``None``；
      * 其余（非 JSON/非信封/5xx/429 网关页）→ ``UnexpectedResponse``：**非业务错误**，
        写路径据此落 unknown「先查询、勿重放」，不得当业务拒绝。
    """
    data = _safe_json_dict(body)
    if isinstance(data, dict) and data.get("s") == "ok":
        return data.get("d"), None
    if isinstance(data, dict) and data.get("s") == "error":
        fields = dict(
            errmsg=data.get("errmsg") or "未知错误",
            errcode=data.get("errcode"),
            need_order_confirm=data.get("need_order_confirm"),
            confirm_id=data.get("confirm_id"),
            jump_url=data.get("jump_url"))
        # need_order_confirm=true：订单已挂起待确认——抛专用子类，供交易闸门
        # 显式 except 分流（该信封禁止盲重试，会重复下单）
        cls = OrderConfirmRequired if fields["need_order_confirm"] else OpenApiError
        raise cls(**fields)
    if isinstance(data, dict) and "ret_code" in data:
        # 行情类网关信封：ret_code!=0 → 业务错误；==0 → data + 顶层 pagination
        if data.get("ret_code") != 0:
            raise OpenApiError(data.get("ret_msg") or data.get("errmsg") or "未知错误",
                               errcode=data.get("ret_code"))
        pagination = data.get("pagination")
        return data.get("data"), pagination if isinstance(pagination, dict) else None
    raise UnexpectedResponse(
        errcode=status if isinstance(status, int) and status else -1,
        errmsg=f"非预期响应（HTTP {status}）：{(body or b'')[:200]!r}")

