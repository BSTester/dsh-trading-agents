# WP6 补遗任务 D：MCP 工具面（56 工具，WP8 任务 3 起）与通道分级（规格 §3.2 / §3.4 / §3.6）。
#
# 唯一事实来源：本文件的 ``TOOLS`` 清单（51 端点工具 + 5 维护工具；52 端点扣除有意
# 排除的 confirm-decide）。MCP 工具不复制任何业务逻辑：端点工具一律
# ``handle(endpoint, payload)``（app.create_handler 的产物，与 HTTP 面同一个实例），
# 维护工具一律 ``store_api.admin_*``——两条通道对同一 payload 因此必然同源
# （规格 §5.1 A3/A4 的结构保证）。
#
# **``confirm-decide`` 有意不进工具面**（规格 §5.1 A7，2026-09-15 业务确认修订）：
# HTTP 面 WP6 时 23 端点里有 22 个各有一个 MCP 工具（WP7 任务 3 起 29 端点 28 工具，
# WP8 富途直通起 37 端点 36 工具，WP8 任务 2 起 46 端点 45 工具，WP8 任务 3 起
# 52 端点 51 工具），
# 唯一被排除的始终是 ``confirm-decide``。
# 理由是通道分级：它是**唯一能批准实盘操作**的通道，必须只由独立 Web 上的用户点击触发。
# 若把它做成工具，模型就能"自己发起、自己批准"，业务确认会退化成模型自批实盘单——
# 与 A2 要守的"人回答这笔业务参数对不对"完全相反。``confirmation`` 是**只读**待确认列表，
# 进工具面没有这个风险（描述里也写明模型不应也不能自行批准），故保留为读工具。
# 常量 ``MCP_EXCLUDED_ENDPOINTS`` 是这条规则的唯一落点，R5/S1 都按它断言。
# WP7 任务 3 新增 trade_* 写工具后这条边界更关键：trade_* 能**发起**确认并阻塞等待，
# 但批准仍只有 Web 的 confirm-decide 可达——模型发起、人批准，两侧合起来才是完整闸门。
#
# 通道分级（规格 §3.2 ⚠，补遗 A 已锁定错误码）：``switch_mode`` 在本层封死 live——
# 模型可见的通道不得持有实盘切换能力，``mode == "live"`` 无论 ``confirmation`` 为何直接返回
# ``trading/live-switch-web-only``，**不触达 handle、不触达 store**（测试用记录型替身断言
# 零调用）。live 切换只能由用户在独立 Web 输入口令完成。``plan_execute`` 保留口令入口
# （规格 §8.3 的对话侧等价入口，口令仍在服务端 handler 内复核）。
#
# 错误语义（规格 §3.2 末段，与 Node 服务层原实现（已退役，见 git 历史 ``aaa5f42^``）同形）：
#   * 业务失败（``ok:false`` + ``trading/*``）是**正常工具结果**（isError=false，文本为 JSON）；
#   * handler 之外的程序异常才 isError=true + ``trading/tool-failed`` 信封；
#   * 维护工具的 store 同步抛错（数据文件损坏/不可写/未知 run）按业务失败处理（isError=false
#     + ``trading/invalid-operation``），与 Node 原实现（已退役）的 ``envelope(fn)`` 同义。
#
# SDK 适配结论（mcp 2.2.0 实测，非推测）：
#   * ``from mcp.server.mcpserver import MCPServer``（2.x 由 FastMCP 更名）；
#   * 注册面是 ``MCPServer.add_tool(fn, name=..., description=..., structured_output=...)``，
#     inputSchema **由 pydantic 从函数签名的类型注解生成**——因此 56 个工具共用一个
#     ``**kwargs`` 派发函数 + 每个工具自带的 ``__signature__`` 表达字段集，注解即契约；
#   * ``MCPServer.list_tools/call_tool`` 是 async；``streamable_http_app()`` 返回的 Starlette
#     app 自带 ``lifespan=session_manager.run()``，挂载时须并入主 app 的 lifespan；
#   * ``structured_output=False`` 走非结构化通道：工具函数直接返回 ``CallToolResult`` 时 SDK
#     原样透传（``FuncMetadata.convert_result`` 对 CallToolResult 短路），isError 由我们决定——
#     这是「业务失败 isError=false / 程序异常 isError=true」的落点；
#   * 可选字段的默认值用 ``UNSET`` 哨兵而不是 None：SDK 会把参模型的**所有**字段（含默认值）
#     展开成 kwargs，用 None 就无法区分「调用方没给」与「调用方给了 null」。哨兵让两者可分，
#     载荷里因此只出现调用方真正提供的字段——与 Node 侧 zod ``.optional()`` 解析后的载荷逐键
#     一致（例如 ``series(ticker=...)`` 不得带上 ``period: None``，否则 handle 会走
#     「period 存在但非法」分支）。代价是 pydantic 对不可序列化默认值发一条
#     ``non-serializable-default`` 警告；注册处定点静音，换来的是 schema 里可选字段没有伪造的
#     ``default: null``（与 z.optional() 同形）。显式传 null 会被当作「调用方给了 null」继续
#     下传（Node 侧 zod 的 ``.optional()`` 没有 null 分支，会在 schema 层拒绝）——差异只落在
#     非法输入的处理位置，合法调用的载荷逐键一致。
#
# 与 Node 侧的**有意差异**登记（上一条「哨兵默认值 / 显式 null」之外，此处登记 schema 形状）：
#   * **``anyOf`` 与 PTC / run_code 退化（补遗 D 审查实测）**：``Param.annotation()`` 用
#     ``base | None``，使每个可选字段的 inputSchema 变成 ``anyOf``（基类型分支 +
#     ``{"type": "null"}`` 分支）。dsh-tools 支持的 schema 子集**不含 anyOf**，故 27 个工具里
#     23 个（全部含可选字段者；只有 ``admin_status`` / ``admin_runs`` / ``admin_cancel_run``
#     三个无可选字段）在 **PTC / run_code 模式**下入参类型静默退化为 ``Any``。
#     **native 模式不受影响**：注册通过、schema 原样透传、调用正常（S1 与 R5 的
#     ``test_published_schemas_are_closed`` 都按原生 schema 断言，即其证据）。
#     若要根治需去掉 ``| None``（只动 ``Param.annotation()`` 一处）：实测（mcp 2.2.0）schema
#     随即变单分支，且 ``description``/``minimum``/``maximum`` 仍在分支上——代价不是「丢失
#     可选字段描述/区间」，而是 null 分支消失、显式 null 改由 schema 层拒绝（与 Node/zod
#     ``.optional()`` 趋同），因此要重新核对 27 个工具的载荷语义。本次补遗只披露，不改。
import inspect
import json
import warnings
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from pydantic.json_schema import PydanticJsonSchemaWarning

from server import store_access

SERVER_NAME = "quantwb"
SERVER_VERSION = "0.1.0"

# 工具面总数：54 端点工具（§3.2 + WP7 factors-history + WP7 任务 3 的 6 个受约束交易
# 工具 + WP8 富途实时直通 8 个 + WP8 任务 2 的 9 个行情工具 + WP8 任务 3 的 6 个
# OpenAPI 交易只读工具 + WP8 任务 6 的 3 个推送订阅管理工具；58 端点扣除有意排除的
# confirm-decide 与设置页 3 端点 openapi_config/openapi_test/openapi_oauth）
# + 5 维护工具（§3.4）+ WP10 任务 1 pipeline + WP11 任务 3 sentiment_history。
# 锁定测试断言 61 恒成立。
TOOL_COUNT = 61

# 有意排除在工具面之外的 HTTP 端点（规格 §5.1 A7，2026-09-15 业务确认修订；
# WP8 任务 7 增补；WP8 OAuth 集成再增 openapi_oauth）。
# ``confirm-decide`` 是唯一能批准实盘操作的通道，只由独立 Web 的用户点击触发；做成工具就等于
# 让模型自己批自己的实盘单。设置页三端点同理：``openapi_config``/``openapi_test``（凭据
# 写入与连通性测试）与 ``openapi_oauth``（OAuth 2.1+PKCE 授权流程的 start/status/cancel，
# 生命周期在 server/oauth_flow.py）都是**人工 Web 动作**——读写 AppKey/私钥落盘、发起
# 浏览器授权的工具面等于把凭据管理交给模型，绝不做。WP10 的 ``auto_pipeline`` 同理：
# 它是「自动流水线是否自动下单」的总开关，模型若能拨开关就等于能自己启动无人确认的
# 执行链——**模型不得自拨**，开关只在 Web 设置页。R5/S1 按本常量断言
# 「工具名集 ≡ 端点数 − 排除集」。
MCP_EXCLUDED_ENDPOINTS = frozenset({"confirm-decide", "openapi_config", "openapi_test",
                                    "openapi_oauth", "auto_pipeline"})

# 规格 §3.6 禁用名黑名单（与 Node 原实现（已退役）同表）。匹配语义是**整名或分段精确**：工具名按
# 非字母数字切段，任一段命中才算，所以 ``plan_execute`` 不因子串 "exec" 被误伤，而
# ``run_shell`` / ``exec_cmd`` / ``read_file`` 一律拦下（与 Node 原实现（已退役）的锁定测试
# 判定逐字一致）。
TOOL_NAME_BLACKLIST = ("exec", "shell", "file_read", "file_write", "read_file",
                       "write_file", "token")

# 通道分级错误码/文案（补遗 A 锁定，规格 §3.2 ⚠）：三处必须逐字一致。
LIVE_SWITCH_CODE = "trading/live-switch-web-only"
LIVE_SWITCH_MESSAGE = "实盘切换只能在独立 Web 由用户输入口令完成；模式切换不授权任何订单"
# 程序异常（handler 之外）与业务失败的两种信封码（规格 §3.2 错误语义）。
TOOL_FAILED_CODE = "trading/tool-failed"
INVALID_OPERATION_CODE = "trading/invalid-operation"
MESSAGE_LIMIT = 300


def _split_segments(name):
    """``name.split(/[^a-z0-9]+/)`` 的等价物（不引入 re：分段字符集就是 ASCII 字母数字）。"""
    segments = []
    current = []
    for char in name:
        if char.isascii() and char.isalnum():
            current.append(char)
        elif current:
            segments.append("".join(current))
            current = []
    if current:
        segments.append("".join(current))
    return segments


def is_blacklisted(name):
    """规格 §3.6 黑名单判定：整名相等，或分段后任一段相等（故 "exec" 不误伤 plan_execute）。"""
    lowered = str(name).lower()
    if lowered in TOOL_NAME_BLACKLIST:
        return True
    return any(segment in TOOL_NAME_BLACKLIST for segment in _split_segments(lowered))


class _Unset:
    """「调用方未提供该可选字段」的哨兵（见文件头 SDK 适配结论）。"""

    __slots__ = ()

    def __repr__(self):
        return "<unset>"


UNSET = _Unset()

# 输入字段的取值类型表（规格 §3.2 输入列的取值域）：键名进 Param.kind，值进 pydantic 注解。
_TYPES = {
    "str": str,
    "bool": bool,
    "int": int,
    "number": float,
    "str_list": list[str],
    "int_list": list[int],
    "number_list": list[float],
    "mode": Literal["sim", "live"],
    "period": Literal["1m", "5m", "15m", "30m", "60m", "1d"],
    "action": Literal["execute", "cancel", "kill", "unkill"],
    # WP7 任务 3：交易方向（broker.py place 的 side 口径，1=BUY 2=SELL 由闸门映射）
    "side": Literal["BUY", "SELL"],
    # WP8 任务 3：OpenAPI 交易枚举（naming-dictionary 原文；取值与
    # trading_datasource.futu_openapi.OpenApiTrade 的常量集合逐项一致，锁定测试比对）。
    # order_type 8 枚举（NONE=未知不列入，模型不得构造）；trd_market 8 枚举；
    # exchange 14 枚举（官方无北交所——北交所标的的撤单/改单在服务端如实拒绝）。
    "order_type": Literal["LIMIT", "MARKET", "AUCTION", "AUCTION_LIMIT", "STOP",
                          "STOP_LIMIT", "MARKET_IF_TOUCHED", "LIMIT_IF_TOUCHED"],
    "trd_market": Literal["HK", "US", "SG", "HKCC", "CA", "FUTURES", "JP", "KR"],
    "exchange": Literal["US", "SEHK", "SGX", "SSE", "SZSE", "JP", "CA", "CME", "CBOT",
                        "NYMEX", "COMEX", "CBOE", "HKFE", "KR"],
    # WP8 任务 6：place-order 的其余官方枚举（naming-dictionary 原文；取值与
    # trading.OpenApiBroker/OpenApiTrade 的常量一致，锁定测试比对）。
    "time_in_force": Literal["DAY", "GTC"],
    "session": Literal["RTH", "RTH+Pre/Post-Mkt", "OVERNIGHT", "ALL_DAY"],
    "lot_type": Literal["ODD", "ROUND"],
    "order_class": Literal["NORMAL", "MLEG"],
    # WP8 富途直通：对象入参（option_screen 的 filter；option_chain 的 field_filter）。
    # 顶层模型仍 additionalProperties:false（_forbid_extra_fields），对象**内部**的键
    # 由 server/futu_data.py 的内键白名单校验——嵌套 dict 不进封闭模型。
    "object": dict,
    # WP8 任务 6：对象列表（kline 订阅项 [{symbol,period,adjust}]；与 object 同理，
    # 元素内键由 server/futu_push.normalize_items 校验）。
    "object_list": list[dict],
}


@dataclass(frozen=True)
class Param:
    """一个输入字段：名字、取值类型、是否必填、中文描述（+ 可选数值区间）。"""

    name: str
    kind: str
    description: str
    required: bool = False
    minimum: int | None = None
    maximum: int | None = None

    def annotation(self):
        """pydantic 注解：必填是基类型，可选是 ``基类型 | None``（schema 里带 null 分支）。"""
        base = _TYPES[self.kind]
        if not self.required:
            base = base | None
        constraints = {}
        if self.minimum is not None:
            constraints["ge"] = self.minimum
        if self.maximum is not None:
            constraints["le"] = self.maximum
        return Annotated[base, Field(description=self.description, **constraints)]

    def parameter(self):
        """函数签名里的 ``Parameter``：可选字段的默认值是 UNSET 哨兵（不是 None）。"""
        return inspect.Parameter(
            self.name,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if self.required else UNSET,
            annotation=self.annotation(),
        )


def req(name, kind, description, minimum=None, maximum=None):
    """必填字段（规格 §3.2 输入列的 ``*``）；minimum/maximum 透传（WP7 交易工具数量下限）。"""
    return Param(name, kind, description, required=True, minimum=minimum, maximum=maximum)


def opt(name, kind, description, minimum=None, maximum=None):
    """可选字段。"""
    return Param(name, kind, description, required=False, minimum=minimum, maximum=maximum)


# ``refresh?: boolean`` → 载荷 ``_refresh``（显式旁路 TTL 缓存，规格 §3.2 表头）。
REFRESH = opt("refresh", "bool", "true 时绕过 TTL 缓存强制重取")


@dataclass(frozen=True)
class ToolDefinition:
    """一个工具的定义（不含 handle/store 绑定）：R5 锁定测试与 run.py 就绪行都读它。"""

    name: str
    description: str
    endpoint: str | None
    params: tuple[Param, ...]

    @property
    def kind(self):
        return "endpoint" if self.endpoint else "admin"

    @property
    def fields(self):
        return tuple(param.name for param in self.params)


# ---------------------------------------------------------------------------
# 56 工具清单（规格 §3.2 表 1-20 + 20b / §3.4 表 21-25 + WP7 factors_history、6 个
# 受约束交易工具，逐项对应）
# ---------------------------------------------------------------------------
# 名称、描述、输入字段集与 Node 原实现（已退役）的 ENDPOINT_TOOLS/ADMIN_TOOLS 一一对应，
# 字段顺序也保持原实现顺序（inputSchema 的 properties 顺序因此稳定可比）。
# 唯一新增/排除（2026-09-15 业务确认修订）：新增 ``confirmation`` 读工具（规格 §3.2 表 20b），
# 排除 ``confirm-decide``（见文件头与 MCP_EXCLUDED_ENDPOINTS）；WP8 任务 7 起排除集再加
# 设置页三端点 openapi_config/openapi_test/openapi_oauth（凭据读写与授权是人工 Web 动作，
# 同一常量）。
TOOLS = (
    ToolDefinition(
        "snapshot",
        "工作台全量快照：账户模式、研报、研究 run、量化预览、交易响应、去重订单事实"
        "（trade_summary）、在途调用与缺失端点清单。无券商数据时明确为空。",
        "snapshot",
        (REFRESH,),
    ),
    ToolDefinition(
        "switch_mode",
        "切换账户模式，只接受切到 sim（live→sim 回模拟盘）；sim→live 一律拒绝：实盘切换只能"
        "由用户在独立 Web（默认 http://127.0.0.1:8397，端口随服务配置）输入口令「确认实盘」"
        "完成。切换模式不授权任何订单。",
        "switch-mode",
        (
            req("mode", "mode", "目标模式（本工具只接受切到 sim；sim→live 一律拒绝）"),
            req("expected_mode", "mode", "调用方所见当前模式，防过期数据误切换"),
            opt("confirmation", "str", "切到 sim 无需口令；本工具不接受 sim→live 切换"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "series", "K 线序列（富途优先、降级如实标注）。", "series",
        (
            req("ticker", "str", "标的代码，如 SH.600519"),
            opt("period", "period", "默认 5m"),
            opt("limit", "int", "20..2000，默认 300", minimum=20, maximum=2000),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "equity", "本地模拟台账权益曲线（不代表券商资产）。", "equity",
        (
            opt("mode", "mode", "账户模式，缺省为当前模式"),
            opt("window", "int", "窗口长度（根）"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "positions", "券商真实持仓（按账户小计，不跨币种合并；失败账户列入 errors）。",
        "positions",
        (
            opt("mode", "mode", "账户模式，缺省为当前模式"),
            opt("window", "int", "窗口长度（根）"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "correlation", "持仓间相关性矩阵。", "correlation",
        (
            req("tickers", "str_list", "标的列表"),
            opt("window", "int", "窗口长度（根）"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "sensitivity", "策略参数敏感性矩阵。", "sensitivity",
        (
            opt("ticker", "str", "标的代码，缺省用当前持仓标的"),
            opt("strategy", "str", "策略名，缺省用默认策略"),
            opt("metric", "str", "评估指标，缺省用默认指标"),
            opt("fast_grid", "int_list", "快线参数网格"),
            opt("slow_grid", "int_list", "慢线参数网格"),
            opt("buy_grid", "number_list", "买入阈值网格"),
            opt("sell_grid", "number_list", "卖出阈值网格"),
            opt("start", "str", "起始日期"),
            REFRESH,
        ),
    ),
    ToolDefinition("risk", "风控配置与当前账户风险指标。", "risk", (REFRESH,)),
    ToolDefinition(
        "trades", "本地模拟台账成交记录（sim 专属）。", "trades",
        (
            opt("mode", "mode", "账户模式，缺省为当前模式"),
            opt("limit", "int", "最多返回条数"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "events", "标的公告/事件时间线。", "events",
        (
            req("ticker", "str", "标的代码"),
            opt("days", "int", "回看天数"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "factors", "横截面因子打分表。", "factors",
        (
            req("tickers", "str_list", "标的列表"),
            opt("window", "int", "窗口长度（根）"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "ic", "因子 RankIC 序列。", "ic",
        (
            req("tickers", "str_list", "标的列表"),
            opt("factor", "str", "因子名，缺省用默认因子"),
            opt("forward", "int", "前瞻期数"),
            opt("window", "int", "窗口长度（根）"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "audit", "审计链：计划→订单→成交三级链路 + 信号/响应链路。", "audit", (REFRESH,),
    ),
    ToolDefinition(
        "sources", "数据源健康状态（渠道可用性与 as_of）。", "sources",
        (
            opt("no_probe", "bool", "true 时只读缓存不探测"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "instrument", "标的解析（名称/市场/整手等）。", "instrument",
        (
            req("ticker", "str", "标的代码"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "quality", "单标的数据质量报告（缺口/复权）。", "quality",
        (
            req("ticker", "str", "标的代码"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "plan",
        "当前/历史执行计划：目标 vs 实际 diff、逐单风控预检、状态时间线；附当前账户模式。"
        "plans 按创建时间倒序，第一条即当前计划（执行/取消请用该条的 plan_hash）。",
        "plan",
        (REFRESH,),
    ),
    ToolDefinition(
        "confirmation",
        "读取待用户确认的实盘写操作（下单/改单/撤单）：返回 pending（含编号、工具、中文订单摘要、"
        "创建与到期时间）与 ttl_ms；无待确认时为 null。**只读**：这笔确认由用户在独立 Web 上作答，"
        "模型不应也不能自行批准——工具参数无法自证已确认，批准只有 Web 的 confirm-decide 通道可达。"
        "另注意跨进程边界：该列表只看得到本服务进程发起的确认，Harness 会话内的确认请在 Harness 面板作答。",
        "confirmation",
        (REFRESH,),
    ),
    ToolDefinition(
        "plan_execute",
        "唯一受约束执行入口：execute=执行已冻结计划（live 需口令「确认执行」，用户须在对话中"
        "逐笔确认后由你携带）；cancel=取消计划；kill/unkill=风控总开关。返回 queued+nonce，"
        "状态用 plan 轮询。不等待执行结果。",
        "plan-execute",
        (
            opt("plan_hash", "str", "action=execute/cancel 时必填"),
            opt("expected_mode", "mode", "action=execute 时必填"),
            opt("confirmation", "str", "live 执行必须为「确认执行」"),
            opt("action", "action", "默认 execute"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "schedule",
        "调度快照：daemon 心跳（>5 分钟即失联）、作业历史、kill/halt 状态。",
        "schedule",
        (REFRESH,),
    ),
    ToolDefinition(
        "reconcile", "对账快照：最近差异、TCA 摘要、告警列表。", "reconcile", (REFRESH,),
    ),
    ToolDefinition(
        "pipeline",
        "流程快照：每市场今日闭环阶段状态（同步/质量/因子/计划生成/自动执行等）、"
        "全局阶段（对账/摘要）、auto_pipeline 配置摘要与当日告警。阶段状态只反映事实"
        "（已跑=ok、未跑且无告警=pending），不推断。",
        "pipeline",
        (REFRESH,),
    ),
    ToolDefinition(
        "factors_history",
        "因子快照历史：按日期倒序的横截面因子快照（payload.tickers 为 {代码:{因子:值}}），"
        "来自服务定时收集——每交易日收盘作业链自动落库，无需手动触发。",
        "factors-history",
        (
            opt("limit", "int", "最多返回快照条数（1..120，默认 30）", minimum=1, maximum=120),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "sentiment_history",
        "情绪/舆情快照（WP11）：给 symbol 返回该标的按日期倒序的渠道原文记录"
        "（source 如 fin_sentiment/fin_news/last30days，payload 为渠道原始 JSON）；"
        "不给 symbol 返回最近一日的采集摘要（date/symbols/records/sources）。"
        "数据来自服务每交易日定时采集落库，只作研究与并列参考，不参与信号计算。",
        "sentiment-history",
        (
            opt("symbol", "str", "标的代码，如 SH.600519；缺省返回最近一日采集摘要"),
            opt("limit", "int", "记录条数（1..120，默认 30）", minimum=1, maximum=120),
            REFRESH,
        ),
    ),
    # ---- WP7 任务 3：受约束交易工具（写三个走完整闸门链 + Web 确认卡片）----
    # 闸门链：模式文件 → 风控 8 规则（kill 是规则 1）→ 业务确认（唯一放行方式）→ broker。
    # 提交后阻塞等待用户在独立 Web 确认卡片作答；确认 TTL 120 秒，超时自动拒绝
    # （fail-closed）。工具描述必须把这条边界讲清楚：模型能发起、只有人能批准。
    # WP8 任务 3 起 live 写可落地：futu_channel=openapi 且已配置凭据时走 REST，券商侧的
    # need_order_confirm 由服务端在人工批准之后自动完成（两层确认合一；工具面**没有**券商
    # 确认入口，模型不能自批）。未配置 OpenAPI（默认部署）时 live 写仍在确认之前被闸门
    # 拒绝（trading/broker-unavailable），描述里如实写明，免得模型在 live 下发起一笔
    # 注定被拒、还要占用确认通道的提交。
    ToolDefinition(
        "trade_place",
        "受约束下单（临时订单）：过完整闸门链（模式文件→风控 8 规则→kill→业务确认）"
        "后提交券商。提交后需在独立 Web 确认卡片批准；TTL 120 秒超时自动拒绝（fail-closed）。"
        "订单类型官方 8 枚举（默认 LIMIT），价格字段按类型条件必填："
        "price 必填=LIMIT/AUCTION_LIMIT/STOP_LIMIT/LIMIT_IF_TOUCHED，"
        "禁止=MARKET/AUCTION/STOP/MARKET_IF_TOUCHED（市价类无价格语义）；"
        "aux_price 必填=STOP/STOP_LIMIT/MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED（证券 3 位小数），"
        "其余类型禁止。MARKET/AUCTION 不带 price，风险基准价取本地最近收盘（本地无数据则"
        "拒绝）。time_in_force=GTC 表示撤单前有效；session 仅美股、市价单仅 RTH；"
        "lot_type 仅港股；remark ≤64 字节；多腿下单需 order_class=MLEG 并给 multi_leg_info。"
        "live 写需 futu_channel=openapi 且已配置凭据（券商二次确认由服务端在人工批准后"
        "自动完成）；未配置时 live 提交会被拒绝。sim 通道仅支持限价当日单，其余字段如实"
        "拒绝（不会静默丢弃）。模式只认账户模式文件（载荷不带 mode）。",
        "trade_place",
        (
            req("symbol", "str", "标的代码，如 SH.600519（支持 SH/SZ/BJ/HK/US 前缀）"),
            req("side", "side", "方向：BUY=买入 / SELL=卖出"),
            req("qty", "int", "数量（股，>=1）", minimum=1),
            opt("order_type", "order_type",
                "订单类型（默认 LIMIT）：LIMIT/MARKET/AUCTION/AUCTION_LIMIT/STOP/"
                "STOP_LIMIT/MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED"),
            opt("price", "number",
                "限价类必填（LIMIT/AUCTION_LIMIT/STOP_LIMIT/LIMIT_IF_TOUCHED，>0）；"
                "市价类（MARKET/AUCTION/STOP/MARKET_IF_TOUCHED）禁止携带"),
            opt("time_in_force", "time_in_force", "有效期：DAY=当日（默认）/ GTC=撤单前有效"),
            opt("session", "session",
                "交易时段（仅美股）：RTH=常规（默认）/RTH+Pre/Post-Mkt=盘前盘后/"
                "OVERNIGHT=夜盘/ALL_DAY=全日；市价单仅支持 RTH"),
            opt("aux_price", "number",
                "触发价（触发类订单必填；证券 3 位小数）：STOP/STOP_LIMIT/"
                "MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED"),
            opt("lot_type", "lot_type", "手数类型（仅港股）：ODD=碎股 / ROUND=整手（默认）"),
            opt("remark", "str", "备注（UTF-8 编码后 ≤64 字节）"),
            opt("order_class", "order_class",
                "订单类别：NORMAL=普通单腿（默认）/ MLEG=多腿（必须同时给 multi_leg_info）"),
            opt("multi_leg_info", "object",
                "多腿订单信息（官方 MultiLegInfo：option_strategy/underlying_symbol/"
                "leg_infos[leg_symbol/leg_exchange/leg_ratio_qty/leg_side/"
                "leg_security_type]）；HTTP 面也接受同构对象列表"),
            opt("client_order_id", "str", "幂等编号：同一编号重复提交只执行一次，不重复下单"),
        ),
    ),
    ToolDefinition(
        "trade_modify",
        "受约束改单：过完整闸门链（模式→风控 8 规则→kill→业务确认）后执行，风控按新参数"
        "全额预检。提交后需在独立 Web 确认卡片批准；TTL 120 秒超时自动拒绝（fail-closed）。"
        "官方改单请求体只有 exchange/qty/price/aux_price（**没有 order_type**：订单类型由"
        "券商侧已有订单决定）；若被改订单是触发类（STOP/STOP_LIMIT/MARKET_IF_TOUCHED/"
        "LIMIT_IF_TOUCHED），官方要求带 aux_price（触发价，证券 3 位小数），否则触发价沿用"
        "旧值。sim 分支=撤旧单+按新参数重下（券商模拟改单接口不可靠）；live（OpenAPI）非 A 股"
        "直接原生改单，A 股官方不支持改单（如实拒绝，请改走撤单+重新下单）。"
        "live 写需 futu_channel=openapi 且已配置凭据；未配置时 live 提交会被拒绝。",
        "trade_modify",
        (
            req("order_id", "str", "要改的券商订单号"),
            req("symbol", "str", "标的代码，如 SH.600519"),
            req("side", "side", "新单方向：BUY/SELL"),
            req("qty", "int", "新单数量（股，>=1；不做「仅改价格」的部分语义）", minimum=1),
            req("price", "number", "新单限价（>0）"),
            opt("aux_price", "number",
                "触发价（证券 3 位小数；官方：被改订单为 STOP/STOP_LIMIT/"
                "MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED 时必填）"),
            opt("client_order_id", "str", "幂等编号（标识这次改单产生的新单登记）"),
        ),
    ),
    ToolDefinition(
        "trade_cancel",
        "受约束撤单：过闸门（kill/模式/交易日三规则真实约束；撤单不新增敞口）后向券商"
        "提交撤单。撤错单同样是业务错误，故同样需在独立 Web 确认卡片批准；TTL 120 秒"
        "超时自动拒绝（fail-closed）。live 写需 futu_channel=openapi 且已配置凭据；"
        "未配置时 live 提交会被拒绝，当前仅 sim 可交易。",
        "trade_cancel",
        (
            req("order_id", "str", "要撤销的券商订单号"),
            req("symbol", "str", "标的代码（用于定位市场与账户）"),
            opt("client_order_id", "str", "幂等编号（仅作调用方追踪）"),
        ),
    ),
    # ---- WP7 任务 3：账户查询（mode 约束直通 broker，实时查询不进缓存）----
    ToolDefinition(
        "account_positions",
        "券商真实持仓（按账户列出，不跨账户/币种合并；失败账户列入 errors）。"
        "mode 缺省读账户模式文件；实时查询不缓存。",
        "account_positions",
        (opt("mode", "mode", "账户模式，缺省读模式文件"),),
    ),
    ToolDefinition(
        "account_orders",
        "券商订单历史（按账户列出，固定最近 30 天窗口——该工具不带时间范围会静默返回"
        " no data，见 TOOL-LIMITS）。mode 缺省读账户模式文件；实时查询不缓存。",
        "account_orders",
        (opt("mode", "mode", "账户模式，缺省读模式文件"),),
    ),
    ToolDefinition(
        "account_funds",
        "券商资金（按账户列出：可用资金/总资产等原始字段）。mode 缺省读账户模式文件；"
        "实时查询不缓存。",
        "account_funds",
        (opt("mode", "mode", "账户模式，缺省读模式文件"),),
    ),
    # ---- WP8 任务 3：OpenAPI 交易只读端点（6 个；字段与 app.py 的
    # OPENAPI_TRADE_FIELDS 白名单逐键同形）----
    # 全部**实时直通**（TTL 0，不进缓存）、受模式约束（mode 缺省读模式文件）。通道要求：
    # trading-platform.json 的 futu_channel=openapi 且已配置凭据（scripts/futu_auth.py
    # --openapi）；mcp 通道下这些工具返回 trading/openapi-unavailable 并给出替代路径——
    # 描述里如实写明，免得模型在 mcp 部署下反复试错。sim 模式没有对应 REST 面（OpenAPI
    # 交易接口只覆盖实盘业务账户），sim 请用 account_orders/account_positions/account_funds。
    ToolDefinition(
        "trade_max_qty",
        "最大可交易量（OpenAPI 交易链，需 futu_channel=openapi）：按授权账户列出 "
        "max_cash_buy/max_cash_and_margin_buy/max_position_sell/max_sell_short/max_buy_back "
        "等原始字段；order_type 必填（LIMIT/MARKET/…），带 order_id 时查该订单的最大可改"
        "数量。mcp 通道下返回 openapi-unavailable（可用 account_* 替代）。",
        "trade_max_qty",
        (
            req("code", "str", "标的代码，如 US.AAPL / HK.00700 / SH.600519"),
            req("order_type", "order_type", "订单类型（LIMIT/MARKET/AUCTION/AUCTION_LIMIT/"
                                            "STOP/STOP_LIMIT/MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED）"),
            opt("price", "number", "非市价单的价格（3 位小数，超出截断）"),
            opt("order_id", "str", "要查最大可改数量的券商订单号（省略=查新单）"),
            opt("mode", "mode", "账户模式，缺省读模式文件（OpenAPI 只回实盘 live）"),
        ),
    ),
    ToolDefinition(
        "orders_open",
        "未完成订单（OpenAPI 交易链，需 futu_channel=openapi）：含最近 24 小时已成交/"
        "已撤单；分页用 page_flag，返回 completed=true 表示本批已取尽。"
        "mcp 通道下返回 openapi-unavailable（可用 account_orders 替代）。",
        "orders_open",
        (
            req("market", "trd_market", "交易市场：HK/US/SG/HKCC/CA/FUTURES/JP/KR"),
            opt("page_flag", "str", "分页游标，空串=从头开始（用响应里的 page_flag 续页）"),
            opt("page_size", "int", "每页条数 10..100（缺省 50）", minimum=10,
                maximum=100),
            opt("mode", "mode", "账户模式，缺省读模式文件（OpenAPI 只回实盘 live）"),
        ),
    ),
    ToolDefinition(
        "orders_history",
        "历史订单（OpenAPI 交易链，需 futu_channel=openapi）：start/end 是创建时间的"
        "**微秒**时间戳（都省略=官方 0/0 组合语义，近 90 天）；分页用 page_flag。"
        "mcp 通道下返回 openapi-unavailable（可用 account_orders 替代）。",
        "orders_history",
        (
            req("market", "trd_market", "交易市场：HK/US/SG/HKCC/CA/FUTURES/JP/KR"),
            opt("code", "str", "只返回该标的的订单（省略=全部）"),
            opt("start", "int", "创建时间起点（微秒时间戳）"),
            opt("end", "int", "创建时间终点（微秒时间戳，须晚于 start）"),
            opt("page_flag", "str", "分页游标，空串=从头开始"),
            opt("page_size", "int", "每页条数 10..100（缺省 50）", minimum=10,
                maximum=100),
            opt("mode", "mode", "账户模式，缺省读模式文件（OpenAPI 只回实盘 live）"),
        ),
    ),
    ToolDefinition(
        "orders_detail",
        "订单详情（OpenAPI 交易链，需 futu_channel=openapi）：同一批 order_ids 必须属于"
        "同一个 exchange（最多 49 个）；服务端对每个授权账户各查一次，订单出现在持有它的"
        "账户分组里。mcp 通道下返回 openapi-unavailable（可用 account_orders 替代）。",
        "orders_detail",
        (
            req("exchange", "exchange", "交易所：US/SEHK/SGX/SSE/SZSE/JP/CA/CME/CBOT/"
                                        "NYMEX/COMEX/CBOE/HKFE/KR"),
            req("order_ids", "str_list", "订单号列表，1..49 个（同一 exchange）"),
            opt("mode", "mode", "账户模式，缺省读模式文件（OpenAPI 只回实盘 live）"),
        ),
    ),
    ToolDefinition(
        "deals_today",
        "当日成交（OpenAPI 交易链，需 futu_channel=openapi）：order_fills 原始字段"
        "（trd_side/deal_id/order_id/qty/price/成交对手方等）；分页用 page_flag。"
        "mcp 通道下返回 openapi-unavailable（成交历史可用 account_orders 的订单状态"
        "间接核对）。",
        "deals_today",
        (
            req("market", "trd_market", "交易市场：HK/US/SG/HKCC/CA/FUTURES/JP/KR"),
            opt("page_flag", "str", "分页游标，空串=从头开始"),
            opt("page_size", "int", "每页条数 10..100（缺省 50）", minimum=10,
                maximum=100),
            opt("mode", "mode", "账户模式，缺省读模式文件（OpenAPI 只回实盘 live）"),
        ),
    ),
    ToolDefinition(
        "deals_history",
        "历史成交（OpenAPI 交易链，需 futu_channel=openapi）：start/end 是**更新**时间的"
        "微秒时间戳（都省略=近 90 天）；page_size 上界 50（与订单页的 100 不同）。"
        "mcp 通道下返回 openapi-unavailable（成交历史可用 account_orders 的订单状态"
        "间接核对）。",
        "deals_history",
        (
            req("market", "trd_market", "交易市场：HK/US/SG/HKCC/CA/FUTURES/JP/KR"),
            opt("code", "str", "只返回该标的的成交（省略=全部）"),
            opt("start", "int", "更新时间起点（微秒时间戳）"),
            opt("end", "int", "更新时间终点（微秒时间戳，须晚于 start）"),
            opt("page_flag", "str", "分页游标，空串=从头开始"),
            opt("page_size", "int", "每页条数 10..50（缺省 50）", minimum=10,
                maximum=50),
            opt("mode", "mode", "账户模式，缺省读模式文件（OpenAPI 只回实盘 live）"),
        ),
    ),
    # ---- WP8：富途实时数据直通（8 个；取数在 server/futu_data.py，实时零缓存）----
    # 数据由服务端**实时**经富途托管 MCP 通道获取（skills 不再回退直连富途）；全部实时
    # 不进缓存。描述统一注明「服务端经富途实时获取；A 股实时受限见错误消息」——A 股
    # 行情仅延时权限（quote_stock_quote/quote_order_book 对 A 股统一 -9），错误消息里
    # 带替代路径（capital_flow / history-kline），这是数据事实不是故障。
    ToolDefinition(
        "rt_quote",
        "实时报价快照（服务端经富途实时获取）：代码/名称/最新价/涨跌幅等原始字段，"
        "codes 1..10 个。A 股实时无权限（统一 -9，见错误消息里的替代路径提示）；"
        "港股/美股实时。",
        "rt_quote",
        (req("codes", "str_list", "标的代码列表，1..10 个，如 [\"HK.00700\", \"SH.600519\"]"),),
    ),
    ToolDefinition(
        "rt_order_book",
        "实时盘口买卖档（服务端经富途实时获取）：档数随行情权限不同（HK 10 / US 60），"
        "不要假定固定档数。A 股实时无权限（统一 -9，见错误消息里的替代路径提示）。",
        "rt_order_book",
        (req("code", "str", "标的代码，如 HK.00700"),),
    ),
    ToolDefinition(
        "capital_flow",
        "分钟级资金流入/流出（服务端经富途实时获取；A 股分钟级实测可用——A 股实时报价"
        "受限时的替代路径）。",
        "capital_flow",
        (req("code", "str", "标的代码，如 HK.00700 / SZ.000001"),),
    ),
    ToolDefinition(
        "capital_flow_history",
        "历史资金流入/流出（服务端经富途实时获取）：按日粒度回看 days 天（默认 30）。",
        "capital_flow_history",
        (
            req("code", "str", "标的代码，如 SH.600519"),
            opt("days", "int", "回看天数（映射上游 count），1..1000，默认 30",
                minimum=1, maximum=1000),
        ),
    ),
    ToolDefinition(
        "capital_distribution",
        "日内资金分布（服务端经富途实时获取）：大/中/小单累计净流入流出。",
        "capital_distribution",
        (req("code", "str", "标的代码，如 SZ.000001"),),
    ),
    ToolDefinition(
        "option_expiration",
        "期权到期日列表（服务端经富途实时获取；HK/US/JP，其他市场上游不支持）。",
        "option_expiration",
        (req("code", "str", "期权正股代码，如 HK.00700 / US.AAPL"),),
    ),
    ToolDefinition(
        "option_chain",
        "期权链（服务端经富途实时获取）：按正股代码返回到期范围内的 CALL/PUT 合约。"
        "field_filter 可选透传（上游当前对该参数不生效；需要字段裁剪用 option_screen，"
        "那里 field_filter 必填非空）。",
        "option_chain",
        (
            req("code", "str", "期权正股代码，如 US.AAPL"),
            opt("field_filter", "object", "可选透传对象；上游 option_chain 当前对该参数不生效"),
        ),
    ),
    ToolDefinition(
        "option_screen",
        "期权筛选器（服务端经富途实时获取）：filter 对象**必须**含非空 field_filter"
        "（省略时上游只返回 4 个默认字段、其余全 null，见 docs/TOOL-LIMITS.md）与非空"
        " strategy（如 {\"market_category_list\": [1]}；类别码是整数）；可选 limit/next_key/"
        "sort_obj。",
        "option_screen",
        (req("filter", "object",
             "筛选对象：必须含非空 field_filter 与非空 strategy；可选 limit/next_key/"
             "request_exact_data/sort_obj/strategy_param"),),
    ),
    # ---- WP8 任务 2：OpenAPI 行情接入（9 个；取数与通道路由在 server/futu_data.py，
    # futu_channel=openapi 时走 OpenAPI REST 后端，默认 mcp 直通）。实时四类 TTL 0；
    # 基本五类进 TTL 缓存（quote_history_kline_v2 带 refresh 旁路）。A 股实时统一 -9
    # （仅延时权限），错误消息附替代路径——数据事实，不是故障。
    ToolDefinition(
        "market_snapshot",
        "行情快照（服务端经富途实时获取）：代码/名称/最新价/涨跌幅/量额/估值/市值等"
        "按品类分组的全字段快照，codes 1..400 个。A 股实时无权限（统一 -9，见错误消息"
        "里的替代路径提示）；港股/美股实时。",
        "market_snapshot",
        (req("codes", "str_list", "标的代码列表，1..400 个，如 [\"HK.00700\", \"US.AAPL\"]"),),
    ),
    ToolDefinition(
        "cur_kline",
        "当前 K 线（服务端经富途实时获取）：距今最新 num 根（1..370，必填），支持"
        "分钟/日/周/月/年/季（ktype）与复权（autype，默认前复权）；extended_time 仅对"
        "美股 1 分 K 生效。A 股实时无权限（统一 -9）。",
        "cur_kline",
        (
            req("code", "str", "标的代码，如 HK.00700 / SH.600519"),
            req("num", "int", "K 线数量，1..370", minimum=1, maximum=370),
            opt("ktype", "int", "K 线类型（官方枚举）：2=日(默认) 1=1分 6=5分 7=15分 8=30分 9=60分 3=周 4=月"),
            opt("autype", "int", "复权：1=前复权(默认) 0=不复权 2=后复权 3/4=含股息前/后复权"),
            opt("extended_time", "int", "0=默认 1=含美股盘前盘后（仅 1 分 K） 2=含夜盘"),
        ),
    ),
    ToolDefinition(
        "rt_data",
        "分时数据（服务端经富途实时获取）：当日分钟级时间序列，按交易时段分节返回。"
        "request_section 可选时段：NORMAL=默认(港股自动含暗盘) / FULL / PREMARKET / "
        "AFTERHOURS（美股）/ HK_DARK / OVERNIGHT。A 股实时无权限（统一 -9）。",
        "rt_data",
        (
            req("code", "str", "标的代码，如 HK.00700"),
            opt("request_section", "str", "交易时段，默认 NORMAL（枚举见描述）"),
        ),
    ),
    ToolDefinition(
        "rt_ticker",
        "逐笔成交（服务端经富途实时获取）：最新 N 笔（num 1..750，默认 500），含"
        "买卖方向/成交类型/时段；不支持时间区间过滤。period 可按时段过滤（NORMAL/"
        "BEFORE/AFTER/OVERNIGHT）。A 股实时无权限（统一 -9）。",
        "rt_ticker",
        (
            req("code", "str", "标的代码，如 HK.00700"),
            opt("num", "int", "返回条数，1..750，默认 500", minimum=1, maximum=750),
            opt("period", "str_list", "按时段过滤：NORMAL/BEFORE/AFTER/OVERNIGHT"),
        ),
    ),
    ToolDefinition(
        "info_basicinfo",
        "标的静态信息（服务端经富途获取，基本数据类）：代码/名称/每手股数/标的类型/"
        "上市日期/内部 stock_id/停牌位/生命周期状态等，codes 1..400 个。",
        "info_basicinfo",
        (req("codes", "str_list", "标的代码列表，1..400 个"), REFRESH),
    ),
    ToolDefinition(
        "info_trading_days",
        "交易日历（服务端经富途获取，基本数据类）：指定市场在 [start,end] 内的交易日"
        "与当日交易总秒数（识别半日市）。market/start/end 全必填。",
        "info_trading_days",
        (
            req("market", "str", "市场：HK/US/SH/SZ/BJ/SG/JP/CA/AU/JP_FUTURE/SG_FUTURE"),
            req("start", "str", "起始日期 yyyy-MM-dd（含）"),
            req("end", "str", "结束日期 yyyy-MM-dd（含）"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "info_search",
        "资讯搜索（服务端经富途获取）：按关键词搜资讯/公告/研报，返回标题/发布时间/"
        "链接。注意：MCP 通道上游恒空（官方通道已知问题，见 docs/TOOL-LIMITS.md），"
        "本工具以 OpenAPI 通道为准；配置 futu_channel=openapi 后才有数据。",
        "info_search",
        (
            req("keyword", "str", "搜索关键词，如 腾讯 / AAPL / 新能源"),
            opt("size", "int", "返回条数，1..50，默认 10", minimum=1, maximum=50),
            opt("news_type", "int", "类型过滤：1=资讯 2=公告 3=研报；不传全部"),
            opt("sort_type", "int", "排序：1=按阅读量 2=按时间"),
            opt("lang", "str", "语言过滤：zh-CN/zh-HK/en/ja"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "info_market_state",
        "市场状态（服务端经富途获取，基本数据类）：代码所属市场当前交易状态"
        "（开盘/休市/盘前/盘后/夜盘等）+ 当日时段表；codes 1..400 个。",
        "info_market_state",
        (
            req("codes", "str_list", "标的代码列表，1..400 个，必须带市场前缀"),
            opt("is_contain_ba", "bool", "true 时含美股盘前盘后时段切换"),
            opt("is_contain_overnight", "bool", "true 时含美股夜盘时段切换"),
            REFRESH,
        ),
    ),
    ToolDefinition(
        "quote_history_kline_v2",
        "历史 K 线 v2（服务端经富途获取；REST history-kline 新端点，带复权参数与翻页，"
        "单次上限 370 根、pagination.has_more 向更早翻页。既有 series 为 WP6 契约，"
        "不受本工具影响）：end 必填，ktype/autype 语义同 cur_kline。",
        "quote_history_kline_v2",
        (
            req("code", "str", "标的代码，如 HK.00700 / SH.600519"),
            opt("start", "str", "起始日期 yyyy-MM-dd（含）；不传按 num 从 end 前推"),
            req("end", "str", "结束日期 yyyy-MM-dd（含）"),
            opt("ktype", "int", "K 线类型（官方枚举）：2=日(默认) 1=1分 6=5分 7=15分 8=30分 9=60分 3=周 4=月"),
            opt("autype", "int", "复权：1=前复权(默认) 0=不复权 2=后复权 3/4=含股息前/后复权"),
            opt("num", "int", "数量，1..370，默认 370", minimum=1, maximum=370),
            opt("extended_time", "int", "0=默认 1=含美股盘前盘后（日内 K） 2=含夜盘"),
            REFRESH,
        ),
    ),
    # ---- WP8 任务 6：推送订阅管理面（3 个；**非交易**：只改本地连接订阅意图，
    # 不改模式、不过风控、不产生订单；TTL 0 实时直通，不进缓存）----
    # push_status 是读（与 /healthz 的 push 字段同一实现 futu_push.safe_status）；
    # push_subscribe/push_unsubscribe 在推送未启用时如实返回 trading/push-unavailable
    # （futu_channel=openapi 且已配置凭据时才启用），坏载荷 → trading/invalid-operation。
    ToolDefinition(
        "push_status",
        "富途 WS 推送状态（只读，TTL 0）：enabled 是否启用，quote/trade 两条链各自的"
        "connected/authenticated/最后消息时间/重连次数/最后错误，以及当前订阅意图。"
        "与 /healthz 的 push 字段同一实现、同一事实。",
        "push_status",
        (),
    ),
    ToolDefinition(
        "push_subscribe",
        "追加订阅意图（**非交易**：只改本地连接订阅意图，不改模式/不过风控/不产生订单，"
        "因此不需要业务确认）。通道：quote/order_book/ticker 给标的列表；kline 给"
        "[{symbol,period,adjust}]。幂等：重复提交同一意图不会重复发订阅帧；推送未启用时"
        "返回 trading/push-unavailable（不假装成功）。",
        "push_subscribe",
        (
            opt("quote", "str_list", "实时报价标的列表"),
            opt("order_book", "str_list", "摆盘标的列表"),
            opt("ticker", "str_list", "逐笔标的列表"),
            opt("kline", "object_list", "K 线订阅项：[{symbol, period, adjust}]"),
        ),
    ),
    ToolDefinition(
        "push_unsubscribe",
        "移除订阅意图（**非交易**）：精确反订阅给定标的/周期；只对已确认订阅的部分发"
        "反订阅帧，未订阅的标的不报错（幂等）。推送未启用时返回 trading/push-unavailable。",
        "push_unsubscribe",
        (
            opt("quote", "str_list", "要取消的实时报价标的"),
            opt("order_book", "str_list", "要取消的摆盘标的"),
            opt("ticker", "str_list", "要取消的逐笔标的"),
            opt("kline", "object_list", "要取消的 K 线订阅项：[{symbol, period, adjust}]"),
        ),
    ),
    # ---- §3.4 维护工具（5 个，来自 workbench_admin.mjs 的能力提升）----
    # 不经 RPC handler，直调 store_access 的 admin_*（与全部端点同库同锁；WP6 口径 23
    # 端点、WP7 任务 3 起 29 端点——2026-09 修订：原文「与 22 端点同库同锁」计数未随
    # 业务确认修订同步，此处更正并注明维护点=端点清单变化时同步本注释）；Node 侧对应
    # WorkbenchStore 的 cancelRun/cancelStaleRuns/pruneAbandonedRuns。
    ToolDefinition(
        "admin_status", "工作台数据维护：数据文件路径与各类记录数量。", None, (),
    ),
    ToolDefinition(
        "admin_runs", "工作台数据维护：列出全部研究 run（状态/标的/模式/年龄分钟）。", None, (),
    ),
    ToolDefinition(
        "admin_cancel_run",
        "工作台数据维护：取消指定研究 run（标记 cancelled，保留记录）。先用 admin_runs 查 id。",
        None,
        (req("run_id", "str", "run id"),),
    ),
    ToolDefinition(
        "admin_cancel_stale", "工作台数据维护：批量取消超时仍 running 的 run（默认 2 小时）。",
        None,
        (opt("hours", "number", "阈值小时数，默认 2"),),
    ),
    ToolDefinition(
        "admin_prune_runs", "工作台数据维护：删除超时孤儿 run（无研报者；有研报的保留）。",
        None,
        (opt("hours", "number", "阈值小时数，默认 2"),),
    ),
)

if len(TOOLS) != TOOL_COUNT:  # pragma: no cover —— 常量与清单漂移时立即炸掉，不留隐患
    raise AssertionError(f"工具面清单应为 {TOOL_COUNT} 项，实际 {len(TOOLS)} 项")

# 本模块注册面的工具名集合：``_forbid_extra_fields`` 只遍历它，不碰同进程其他工具的 arg_model。
TOOL_NAMES = frozenset(definition.name for definition in TOOLS)

# 54 个端点工具 → 服务端端点名（R5 断言其值集 ≡ store_access.endpoints() − MCP_EXCLUDED_ENDPOINTS）。
ENDPOINT_TOOL_ENDPOINTS = {tool.name: tool.endpoint for tool in TOOLS if tool.endpoint}


class StoreApi:
    """``store_access`` 的 home 绑定门面：5 个维护工具的**唯一**入口。

    规格 §3.4：维护工具走 WorkbenchStore 直接方法，不经 RPC handler（同库同锁）。Python 侧
    对应物就是 ``store_access.admin_*``，这里只把 ``home`` 参数绑定掉，不复制任何逻辑。
    """

    def __init__(self, home, module=None):
        self.home = str(home)
        self._module = store_access if module is None else module

    def admin_status(self):
        return self._module.admin_status(self.home)

    def admin_runs(self):
        return self._module.admin_runs(self.home)

    def admin_cancel_run(self, run_id):
        return self._module.admin_cancel_run(self.home, run_id)

    def admin_cancel_stale(self, hours=None):
        return self._module.admin_cancel_stale(self.home, hours)

    def admin_prune_runs(self, hours=None):
        return self._module.admin_prune_runs(self.home, hours)


def _message(error):
    """``String(error?.message ?? error).slice(0, 300)``。"""
    text = str(error) if str(error) else error.__class__.__name__
    return text[:MESSAGE_LIMIT]


def failure(code, message):
    """``{ok:false, error:{code, message, details:{}}}``（Node 原实现（已退役）的信封同形）。"""
    return {"ok": False, "error": {"code": code, "message": message, "details": {}}}


def tool_result(envelope, is_error):
    """把 envelope 包成非结构化 ``CallToolResult``：文本是紧凑 JSON（与 Node 同形）。"""
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


def result_payload(result):
    """``CallToolResult`` → envelope dict（测试与调试读同一份线格式）。"""
    return json.loads(result.content[0].text)


def payload_of(arguments):
    """``refresh`` → 载荷 ``_refresh`` 的映射（Node endpointTool 的 ``run`` 前半段）。"""
    payload = {key: value for key, value in arguments.items() if key != "refresh"}
    if arguments.get("refresh") is True:
        payload["_refresh"] = True
    return payload


def _store_call(name, store_api, arguments):
    """维护工具 → store_access 调用（hours 原样透传，阈值语义只有 _hours_to_ms 一份实现）。

    有意差异：Node 原实现（已退役）的 ``hoursToMs`` 用 ``Math.max(1, hours ?? 2)``，``hours<=0`` 时给
    1 小时；store_access 移植的是 ``workbench_admin.mjs`` 的 ``hoursArg``（``<=0``/非法 →
    默认 2 小时）。规格 §3.4 把维护工具锚在 workbench_admin.mjs 的能力上，故以 store_access 为准。
    """
    if name == "admin_status":
        return store_api.admin_status()
    if name == "admin_runs":
        return store_api.admin_runs()
    if name == "admin_cancel_run":
        return store_api.admin_cancel_run(arguments["run_id"])
    if name == "admin_cancel_stale":
        return store_api.admin_cancel_stale(arguments.get("hours"))
    if name == "admin_prune_runs":
        return store_api.admin_prune_runs(arguments.get("hours"))
    raise AssertionError(f"unknown admin tool: {name}")  # pragma: no cover


def dispatch(definition, handle, store_api, arguments):
    """执行一件工具，返回 envelope（业务失败也在 envelope 里，绝不抛错）。"""
    if definition.kind == "admin":
        try:
            return {"ok": True, "value": _store_call(definition.name, store_api, arguments)}
        except Exception as error:  # noqa: BLE001 —— 与 Node 原实现 envelope() 的 catch 同宽
            return failure(INVALID_OPERATION_CODE, _message(error))
    if definition.name == "switch_mode" and arguments.get("mode") == "live":
        # 通道分级（规格 §3.2 ⚠）：模型可见的通道不得持有 live 切换能力。这里直接返回，绝不
        # 触达 handle/store——口令对不对都改变不了这条分支。
        return failure(LIVE_SWITCH_CODE, LIVE_SWITCH_MESSAGE)
    return handle(definition.endpoint, payload_of(arguments))


@dataclass
class BoundTool:
    """工具定义 + handle/store 绑定 + SDK 注册函数（三者的组合就是一个可调用工具）。

    ``fn`` 是注册进 MCPServer 的函数（签名由 ``definition.params`` 决定）；``call`` 是它的
    函数体，测试可直接调用而绕过协议层。
    """

    definition: ToolDefinition
    handle: Callable[[str, dict], dict]
    store_api: Any
    fn: Callable[..., CallToolResult] | None = None

    @property
    def name(self):
        return self.definition.name

    @property
    def description(self):
        return self.definition.description

    @property
    def endpoint(self):
        return self.definition.endpoint

    @property
    def kind(self):
        return self.definition.kind

    @property
    def fields(self):
        return self.definition.fields

    def run(self, arguments):
        return dispatch(self.definition, self.handle, self.store_api, arguments)

    def call(self, arguments=None):
        """SDK 工具函数体：业务失败/程序异常在这里分流（规格 §3.2 错误语义）。"""
        provided = {key: value for key, value in (arguments or {}).items() if value is not UNSET}
        try:
            envelope = self.run(provided)
        except Exception as error:  # noqa: BLE001 —— handler 之外的程序异常 → isError=true
            return tool_result(failure(TOOL_FAILED_CODE, _message(error)), is_error=True)
        return tool_result(envelope, is_error=False)


def build_tools(handle, store_api):
    """56 个工具（名称/描述/输入字段集来自 ``TOOLS``，行为绑定到 handle/store_api）。

    ``handle`` 必须是 ``app.create_handler`` 的产物——与 HTTP 面同一个实例（规格 §5.2 R6）。
    """
    return tuple(_bind(definition, handle, store_api) for definition in TOOLS)


def _bind(definition, handle, store_api):
    """造 SDK 注册函数：``**kwargs`` 收下校验后的实参，``__signature__`` 表达字段集。

    签名里参数名 = schema 字段名，值类型/必填/描述/区间全部来自 ``ToolDefinition.params``，
    因此「注解即契约」，没有第二处需要同步的地方。
    """
    tool = BoundTool(definition=definition, handle=handle, store_api=store_api)

    def fn(**kwargs):
        return tool.call(kwargs)

    fn.__name__ = definition.name
    fn.__doc__ = definition.description
    fn.__signature__ = inspect.Signature([param.parameter() for param in definition.params])
    tool.fn = fn
    return tool


def register(server: MCPServer, handle, store_api=None):
    """把 56 个工具注册进 ``MCPServer``，返回绑定后的工具清单（``app.state.mcp_tools``）。

    ``store_api`` 在生产路径上由 create_app 显式传入；缺省 None 只为单测里手搓 server 的便利
    （此时维护工具调用会抛 AttributeError，并按程序异常包成 tool-failed）。
    """
    bound = build_tools(handle, store_api)
    with warnings.catch_warnings():
        # UNSET 哨兵默认值不是 JSON 可序列化的：pydantic 生成 inputSchema 时会警告并把 default
        # 排除掉——排除正是我们想要的（可选字段与 z.optional() 同形，不出现伪造的 default:null），
        # 所以只在此处定点静音，不扩大到全局。
        warnings.simplefilter("ignore", PydanticJsonSchemaWarning)
        for tool in bound:
            server.add_tool(tool.fn, name=tool.name, description=tool.description,
                            structured_output=False)
        _forbid_extra_fields(server)
    return bound


def _forbid_extra_fields(server, own_names=TOOL_NAMES):
    """规格 §3.6：inputSchema 必须 ``additionalProperties:false``（超集字段在 schema 层拒绝）。

    SDK 的公开注册面用固定 config 的 ``ArgModelBase`` 建参模型，没有注入 ``extra=forbid`` 的
    口子，因此注册后按**同一个参模型**再算一次 schema：校验面（extra 拒绝）与发布面
    （additionalProperties:false）仍出自同一份 pydantic 模型，不会各说各话。

    只遍历**本模块注册面**（``own_names`` 缺省 = ``TOOL_NAMES``）而不是 ``_tools`` 的全量：
    同进程里还可能有别的 MCP 工具（其他行、测试内手搓的 server），它们的 ``arg_model`` 不属于
    本次注册，不能被这里顺手改写；``.get()`` 对缺失名宽容，因此与注册顺序/子集解耦。

    私有面依赖（mcp 2.2.0 实测）：``server._tool_manager._tools`` / ``tool.fn_metadata.arg_model``
    / ``tool.parameters``。**SDK 升级时必须由 R5 ``test_published_schemas_are_closed`` 兜住**——
    私有面改名或变形会让 ``additionalProperties`` 不再为 false，该用例（以及 S1 的同名断言）
    立刻红，不会静默放宽。
    """
    manager = server._tool_manager  # noqa: SLF001 —— SDK 没有公开口子
    for name in own_names:
        tool = manager._tools.get(name)
        if tool is None or tool.fn_metadata is None:
            continue  # 非本模块注册（无参模型）或被改名遮蔽：不动它，封闭性由上引用例兜底
        model = tool.fn_metadata.arg_model
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
        tool.parameters = model.model_json_schema(by_alias=True)


__all__ = [
    "ENDPOINT_TOOL_ENDPOINTS", "INVALID_OPERATION_CODE", "LIVE_SWITCH_CODE", "LIVE_SWITCH_MESSAGE",
    "MCP_EXCLUDED_ENDPOINTS", "SERVER_NAME", "SERVER_VERSION", "StoreApi", "TOOLS", "TOOL_COUNT",
    "TOOL_FAILED_CODE", "TOOL_NAME_BLACKLIST", "TOOL_NAMES", "BoundTool", "Param",
    "ToolDefinition", "build_tools", "dispatch", "failure", "is_blacklisted", "payload_of",
    "register", "result_payload", "tool_result",
]
