"""OpenAPI 异常族（信封错误 / 传输失败 / 非预期响应 / 券商二次确认）。"""
class OpenApiError(Exception):
    """富途 OpenAPI 错误（信封 s==error / token 刷新失败 / 非预期传输响应）。

    need_order_confirm=true 的信封抛专用子类 ``OrderConfirmRequired``；
    jump_url：部分错误附带的跳转地址；retry_after：429 时从响应头并入
    （原值不解析，供上层退避）；限频/5xx 也在本错误如实抛出（本层不自动重试）。
    """

    def __init__(self, errmsg, errcode=None, need_order_confirm=None,
                 confirm_id=None, jump_url=None, retry_after=None):
        super().__init__(errmsg)
        self.errcode = errcode
        self.errmsg = errmsg
        self.need_order_confirm = need_order_confirm
        self.confirm_id = confirm_id
        self.jump_url = jump_url
        self.retry_after = retry_after


class TransportError(OpenApiError):
    """传输层异常（DNS 解析失败 / 连接拒绝 / 超时 / 连接重置 / 响应中断）。

    由默认传输 ``_default_http`` 抛出：urllib 的 URLError/OSError 等不再裸穿，
    使 ``request()`` 的「传输异常 → OpenApiError」契约成立（errcode 为 None，
    与业务 errcode 语义区分）。注入式传输抛出的任意异常同样由 ``_transport_call``
    收敛为本类，契约对注入传输一样成立。
    """


class UnexpectedResponse(OpenApiError):
    """非信封响应（5xx / 429 / 非 JSON / 网关页）：**不是业务结论**。

    ``parse_envelope_meta`` 对既非 ``{"s":...}`` 也非 ``{"ret_code":...}`` 的响应抛本类
    （``errcode`` = HTTP status，``retry_after`` = 429 的响应头原值）。

    与业务错误信封（有 errcode/errmsg）严格区分：本类代表「服务端**没有**给出可判定的
    业务结论」，请求**可能已到达券商**——交易写路径必须落 ``unknown``（铁律：先查询订单，
    勿重放），绝不能当 ``rejected``（终态、无出边，会把可能已成交的单丢掉）。
    读路径同样按「通道不可用」而不是「业务错误」处置（见 server/futu_data.py）。
    """


class OrderConfirmRequired(OpenApiError):
    """下单二次确认信封（``need_order_confirm=true``）：订单**已挂起待确认**。

    该信封代表订单已挂起而不是失败——上层（交易闸门/Web 卡片）必须显式
    ``except OrderConfirmRequired`` 分流：向用户展示 confirm_id/jump_url，批准后
    调 order-confirm；**禁止对原请求盲重试**（会重复下单）。
    """

