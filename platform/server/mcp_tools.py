# WP6 补遗任务 D：MCP 工具面（26 工具）与通道分级（规格 §3.2 / §3.4 / §3.6）。
#
# 唯一事实来源：本文件的 ``TOOLS`` 清单（21 端点工具 + 5 维护工具）。MCP 工具不复制任何
# 业务逻辑：端点工具一律 ``handle(endpoint, payload)``（app.create_handler 的产物，与 HTTP
# 面同一个实例），维护工具一律 ``store_api.admin_*``——两条通道对同一 payload 因此必然同源
# （规格 §5.1 A3/A4 的结构保证）。
#
# **``confirm-decide`` 有意不进工具面**（规格 §5.1 A7，2026-09-15 业务确认修订）：
# HTTP 面 22 端点里有 21 个各有一个 MCP 工具，唯一被排除的就是 ``confirm-decide``。
# 理由是通道分级：它是**唯一能批准实盘操作**的通道，必须只由独立 Web 上的用户点击触发。
# 若把它做成工具，模型就能"自己发起、自己批准"，业务确认会退化成模型自批实盘单——
# 与 A2 要守的"人回答这笔业务参数对不对"完全相反。``confirmation`` 是**只读**待确认列表，
# 进工具面没有这个风险（描述里也写明模型不应也不能自行批准），故保留为读工具。
# 常量 ``MCP_EXCLUDED_ENDPOINTS`` 是这条规则的唯一落点，R5/S1 都按它断言。
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
#     inputSchema **由 pydantic 从函数签名的类型注解生成**——因此 26 个工具共用一个
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
#     ``{"type": "null"}`` 分支）。dsh-tools 支持的 schema 子集**不含 anyOf**，故 26 个工具里
#     23 个（全部含可选字段者；只有 ``admin_status`` / ``admin_runs`` / ``admin_cancel_run``
#     三个无可选字段）在 **PTC / run_code 模式**下入参类型静默退化为 ``Any``。
#     **native 模式不受影响**：注册通过、schema 原样透传、调用正常（S1 与 R5 的
#     ``test_published_schemas_are_closed`` 都按原生 schema 断言，即其证据）。
#     若要根治需去掉 ``| None``（只动 ``Param.annotation()`` 一处）：实测（mcp 2.2.0）schema
#     随即变单分支，且 ``description``/``minimum``/``maximum`` 仍在分支上——代价不是「丢失
#     可选字段描述/区间」，而是 null 分支消失、显式 null 改由 schema 层拒绝（与 Node/zod
#     ``.optional()`` 趋同），因此要重新核对 26 个工具的载荷语义。本次补遗只披露，不改。
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

# 工具面总数：21 端点工具（§3.2，22 端点扣除有意排除的 confirm-decide）+ 5 维护工具（§3.4）。
# 锁定测试断言 26 恒成立。
TOOL_COUNT = 26

# 有意排除在工具面之外的 HTTP 端点（规格 §5.1 A7，2026-09-15 业务确认修订）。
# ``confirm-decide`` 是唯一能批准实盘操作的通道，只由独立 Web 的用户点击触发；做成工具就等于
# 让模型自己批自己的实盘单。R5/S1 按本常量断言「工具名集 ≡ 端点数 − 排除集」。
MCP_EXCLUDED_ENDPOINTS = frozenset({"confirm-decide"})

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


def req(name, kind, description):
    """必填字段（规格 §3.2 输入列的 ``*``）。"""
    return Param(name, kind, description, required=True)


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
# 26 工具清单（规格 §3.2 表 1-20 + 20b / §3.4 表 21-25，逐项对应）
# ---------------------------------------------------------------------------
# 名称、描述、输入字段集与 Node 原实现（已退役）的 ENDPOINT_TOOLS/ADMIN_TOOLS 一一对应，
# 字段顺序也保持原实现顺序（inputSchema 的 properties 顺序因此稳定可比）。
# 唯一新增/排除（2026-09-15 业务确认修订）：新增 ``confirmation`` 读工具（规格 §3.2 表 20b），
# 排除 ``confirm-decide``（见文件头与 MCP_EXCLUDED_ENDPOINTS）。
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
    # ---- §3.4 维护工具（5 个，来自 workbench_admin.mjs 的能力提升）----
    # 不经 RPC handler，直调 store_access 的 admin_*（与 20 端点同库同锁）；Node 侧对应
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

# 21 个端点工具 → 服务端端点名（R5 断言其值集 ≡ store_access.endpoints() − MCP_EXCLUDED_ENDPOINTS）。
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
    """26 个工具（名称/描述/输入字段集来自 ``TOOLS``，行为绑定到 handle/store_api）。

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
    """把 26 个工具注册进 ``MCPServer``，返回绑定后的工具清单（``app.state.mcp_tools``）。

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
