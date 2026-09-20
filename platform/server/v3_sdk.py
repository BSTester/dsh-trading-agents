"""V3.0 FR-GATEWAY-002：**SDK JSON-RPC 会话客户端**（``/api/v3/sdk/*``）。

本模块补上 ``docs/v3-spec.md`` 里 FR-GATEWAY-002 + §5.2.2 + §6.3 的实现。规格原文是
唯一尺子，逐条对应如下（**先读规格再读代码**）：

    §FR-GATEWAY-002  「通过 stdio 服务 SDK 协议格式，使平台能够驱动 Harness Agent：为每个
                      sessionId 打开一个会话、把用户提示词排入队列，并把每个会话事件与
                      Agent 状态转换流式发回平台。」      → SdkRuntime / enqueue_prompt / 事件流
    §FR-GATEWAY-002  「initialize 是运行时就绪边界…握手返回协议稳定标识
                      deepseek-harness-sdk-runtime。」      → handshake() 如实比对，不符报不符
    §5.2.2           「SDK 启动捆绑的 dsh CLI，使用 --profile sdk；每次启动都需要显式的
                      Harness home。」                     → profile=dsh_home 双显式
    §6.3             「$DSH_HOME/profiles/sdk/cordis.patch.yml 挂
                      @deepseek-ai/dsh-sdk-jsonrpc-server + 平台 MCP 客户端行」
                                                          → platform/install/quant-sdk/

为什么**不**用 ``deepseek-harness-sdk``（Python SDK）
----------------------------------------------------
实测（2026-09-20）：``~/.dsh/trading-venv`` 里 ``import deepseek_harness`` = False，包未安装。
规格 §5.2.2 的示例代码把会话管理交给了那个包，但 SDK 协议本身是**换行分帧 JSON-RPC over
stdio**（``@deepseek-ai/dsh-sdk-protocol`` README：「Wire one JSON-RPC 2.0 message per
``\\n``-terminated line over byte streams you own」），三方法 + 四通知全部是自描述 JSON。
因此本模块**自己实现帧协议客户端**：不引入新依赖、不装 Python 包、不依赖厂商客户端的版本
节奏，且能在离线单测里用假进程把分帧/半包/粘包/超大行/握手超时/异常退出逐条钉死。

协议契约（来自 ``@deepseek-ai/dsh-sdk-protocol@0.1.5-rc.2`` 的 ``lib/types/types.d.ts``）
--------------------------------------------------------------------------------------
    client→server  initialize     InitializeParams{cwd,provider,model,reasoningEffort?,maxTokens?}
                                  → InitializeResult{serverInfo{name,version}}
    client→server  session/prompt SessionPromptParams{sessionId,contentBlocks[]}
                                  → SessionPromptResult{messageId}
    client→server  shutdown       → {}
    server→client  session.event       SessionEventNotification{sessionId,event}
    server→client  session.status      SessionStatusNotification{sessionId,status:idle|running}
    server→client  subagent.started    SubagentStartedNotification{parentSessionId,childSessionId}
    server→client  subagent.finished   SubagentFinishedNotification{…,status,stopReason,
                                                                     lastAssistantMessage?}

协议自己声明的两条限制必须照实转述，不能在本模块里「补」出不存在的能力：
  * 「There is no per-prompt result」——``messageId`` 只是入队收据，不代表后续 assistant
    消息、轮次结束或提示词结果。所以本客户端的「一次下发完成」边界定义在
    **会话状态转换上**（``running`` → ``idle``），而不是伪造一个 per-prompt 响应。
  * 「The wire has no per-session close or prompt-cancel method」——会话与 Agent 活到进程
    关闭为止；``shutdown`` 是进程级动作。``stop()`` 因此是进程级停机。

硬边界（不可协商，逐条可查）
----------------------------
① **工具白名单排除全部写/交易工具**。做法分两层，都落在 ``platform/install/quant-sdk/``：
   * profile 层「按行关停」（照抄 ``platform/install/quant-headless`` 的既有做法）：把不在
     白名单里的 ``tool-*`` 行 ``disabled: true``。
   * **实测该做法对 MCP 工具无效**——平台工具面经 ``@deepseek-ai/dsh-mcp-client`` **一行**
     （``quant-platform-mcp``）进入，``dsh-mcp-client`` 没有 allow/deny 配置项（已核实其
     README 的配置表：只有 serverName/transport/url/failOnStartupError/timeoutMs/reconnect），
     ``@deepseek-ai/dsh-tools`` 的 ``Config`` 也只有 ``mode`` / ``maxParallelSubCalls``。
     所以额外落一个 40 行的 profile 插件 ``quant-sdk/tool-whitelist``，用
     ``ctx.tools.guard()``（**单调**执行闸门，拒绝后任何后续监听器都无法改回允许）+
     ``ctx.tools.restrict({deny})``（从模型可见面摘掉）把白名单变成真边界。
     两侧的拒绝集必须**逐字一致**：``platform/tests/test_v3_sdk.py`` 会读那个 JS 文件并断言
     本模块的 ``WRITE_TOOL_NAMES``/``WRITE_TOOL_PREFIXES`` 与它一一对应（漂移即红）。
② **每次下发必须带人工口令**（``PROMPT_PASSPHRASE = "确认下发"``，与工作台既有「确认执行」
   同一风格）+ **写审计**（``<home>/v3-sdk-audit.jsonl``，追加逐行，成功与拒绝都留痕）。
③ **绝不下任何真实订单**：本模块不调用 ``v3_run`` 的任何写/交易端点，不导入 ``store_access``，
   不在任何代码路径里出现 ``trade_*`` / ``plan-execute`` / ``confirm-decide`` 的调用。它只做
   一件事——把提示词交给一个**工具面已被收窄的** Harness 会话，并把真实输出如实回传。

数据诚实性（本模块的硬约束）
----------------------------
* 握手标识**逐字比对**规格里写的 ``deepseek-harness-sdk-runtime``；不符就报
  ``protocol_match=false`` + ``observed`` 原文，并**拒发提示词**（fail-closed），不静默放行。
* 进程异常退出**如实上报**：``exit_code`` + ``stderr`` 原文尾部 + 触发它的会话/请求 id。
  绝不把「进程死了」写成「会话空闲」。
* ``/status`` 与 ``/sessions`` 的每个字段都来自**本进程里的真实对象**（子进程 pid / 帧计数 /
  事件环 / 退出码）。没有子进程就报 ``state="stopped"``，不编造会话与事件。
* 未做真机验证的能力不写进响应：``/status`` 只报本次进程真实观测到的东西。

落盘（全在 ``home`` 下）
------------------------
* ``<home>/v3-sdk-audit.jsonl``  —— 每次 ``POST /api/v3/sdk/prompt`` 的追加审计（含被拒）
* 会话持久化由 Harness 侧自己的 ``DSH_HOME`` 负责（默认沿用进程的 ``$DSH_HOME``）；本模块
  **不写** Harness 的存储目录。

测试：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_sdk -v``
"""
import atexit
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 规格常量
# ---------------------------------------------------------------------------

#: FR-GATEWAY-002：「握手返回协议稳定标识 deepseek-harness-sdk-runtime」。
SPEC_SERVER_NAME = "deepseek-harness-sdk-runtime"

#: FR-GATEWAY-005 / §6.3：本客户端专用 profile（**不是** shipped 的 sdk / headless / web）。
DEFAULT_PROFILE = "quant-sdk"

#: 人工口令（风格照既有「确认执行」）。不匹配即拒绝，且拒绝也落审计。
PROMPT_PASSPHRASE = "确认下发"

#: 审计文件名（相对 ``home``）。
AUDIT_FILE = "v3-sdk-audit.jsonl"

#: 单帧上限：超过即丢弃该帧并计数（协议没定义上限，但客户端必须防内存被一行撑爆）。
MAX_LINE_BYTES = 1 << 21  # 2 MiB

#: 下发提示词的长度上限（字符）。超限直接拒绝，不截断——截断等于改写了人的指令。
MAX_PROMPT_CHARS = 100_000

#: 服务端→客户端请求：协议声明「Server→client requests are a dead capability」，但传输层
#: 支持。收到就按 JSON-RPC 规范回 -32601，并计数（不静默吞掉）。
METHOD_NOT_FOUND = -32601


# ---------------------------------------------------------------------------
# 工具白名单策略（与 profile 插件 ``tool-whitelist/index.js`` 逐字同源）
# ---------------------------------------------------------------------------
#
# 判定顺序：先把工具名归一化（剥掉 ``mcp__<server>__`` 命名空间、``-``/``.``/空格 折成 ``_``、
# 转小写），再按「精确名 → 前缀 → 子串」三档拒绝。任何一档命中即写工具。
#
# 为什么用拒绝档而不是静态 116 个名字的 allow 列表：allow 列表会随平台工具面（77 工作台
# 工具 + 随 ``/api/v3/*`` 路由表自动增减的 ``v3_*`` 桥接工具）漂移，漂移那一刻白名单就
# 悄悄失真。拒绝档的**真值来源是平台的通道分级常量**（见 ``platform_tool_catalog()``：
# ``v3_mcp.NON_READONLY_PATHS`` / ``mcp_tools`` 的端点表），本模块的静态档只覆盖「名字本身
# 就是写/交易语义」的那一类，两层交集由 ``whitelist_audit()`` 在真实目录上验证。

#: 精确名（归一化后）——名字本身就是写/交易动作。**与平台自己的写类常量逐字对齐**
#: （推导见 ``platform_write_tool_names()``，与 ``quant-headless/tool-whitelist.json`` 同源），
#: 因此不是「作者记得的那几个」。
WRITE_TOOL_NAMES = frozenset({
    "auto_pipeline",          # 「自动流水线是否自动下单」总开关（模型不得自拨）
    "confirm_decide",         # 人工决定通道（有意不进 MCP 工具面，仍在此封死）
    "modify_user_security",   # 写用户富途侧自选
    "openapi_config",         # 写 AppKey/私钥（人工 Web 动作）
    "openapi_oauth",          # 发起浏览器授权
    "openapi_test",           # 连通性测试（会落盘）
    "plan_execute",           # 唯一受约束执行入口（live 需口令「确认执行」）
    "push_subscribe",         # 写推送订阅
    "push_unsubscribe",
    "record_observation",     # 落盘观察记录
    "research_tasks_claim",   # 值班队列领取（写队列状态）
    "research_tasks_report",  # 值班队列回报（写队列状态）
    "rules_decide",           # 规则上岗批准通道（同上）
    "sentiment_snapshot",     # 落盘情绪快照
    "switch_mode",            # 切账户模式（sim→live 只能人工在 Web）
})

#: 前缀档——同一族里未来新增的成员自动被覆盖。
WRITE_TOOL_PREFIXES = (
    "trade_",            # trade_place / trade_modify / trade_cancel / trade_max_qty…
    "sim_trade_",        # 官方模拟盘写面
    "openapi_",          # 设置页三端点（凭据/授权/测试）
    "v3_credentials",    # 数据源凭据读写（桥内已封死 save/clear，名字层再封一层）
    "v3_oms_sync",       # 重写本地 OMS 台账
    "v3_strategy_run",   # 落盘一轮研究流水线
    "v3_sdk",            # 本模块自己的端点（含 prompt 写端点）不得回喂给会话
    "admin_cancel",      # admin_cancel_run / admin_cancel_stale
    "admin_prune",       # admin_prune_runs
)

#: 子串档——凭据类无论挂在哪个命名空间都拒绝。
WRITE_TOOL_SUBSTRINGS = ("credentials", "confirm_decide")

#: **只读例外**：``mcp_tools.MCP_EXCLUDED_ENDPOINTS`` 里被排除但语义只读的三件
#: （与 ``quant-headless/tool-whitelist.json`` 的 ``readOnlyExcluded`` 同一份）。
READ_ONLY_EXCLUDED_ENDPOINTS = frozenset({"info_rehab", "research-tasks-list",
                                          "warrant_screen"})

#: MCP 工具的命名空间前缀形状：``mcp__<serverName>__<rawName>``。
_MCP_NAMESPACE = re.compile(r"^mcp__[A-Za-z0-9_-]{1,32}__")


def normalize_tool_name(name):
    """``mcp__quantwb__plan-execute`` → ``plan_execute``（判定用的规范形）。"""
    text = str(name or "").strip()
    text = _MCP_NAMESPACE.sub("", text)
    return re.sub(r"[\s.\-]+", "_", text).lower()


def platform_write_tool_names():
    """平台自己的写/交易工具真值（三处常量的并集，规范化后）。

      * ``server.v3_ops.WRITE_TOOLS``——交易写类唯一名单（规格 §3.2）；
      * ``server.mcp_tools.MCP_EXCLUDED_ENDPOINTS`` − ``READ_ONLY_EXCLUDED_ENDPOINTS``
        ——有意不进工具面的写类；
      * ``tools.e2e_probe.WRITE_ENDPOINTS``——端到端探测里声明的写端点。

    这三处都是**平台代码里的既有常量**，不是本模块的私有清单：本函数把它们翻成规范名，
    供 ``whitelist_audit()`` 断言「静态策略没有漏掉平台认定的写工具」（``policy_gaps``）。
    """
    names = set()
    try:
        from server import mcp_tools, v3_ops
        names |= {normalize_tool_name(item) for item in getattr(v3_ops, "WRITE_TOOLS", ())}
        names |= {normalize_tool_name(item) for item in
                  set(getattr(mcp_tools, "MCP_EXCLUDED_ENDPOINTS", ()))
                  - READ_ONLY_EXCLUDED_ENDPOINTS}
    except ImportError:
        pass
    try:
        from tools import e2e_probe
        names |= {normalize_tool_name(item)
                  for item in getattr(e2e_probe, "WRITE_ENDPOINTS", ())}
    except ImportError:
        pass
    return {name for name in names if name}


def is_write_tool(name):
    """该工具名是否属于**必须排除**的写/交易类。未知名字返回 False（读侧优先），

    但 ``whitelist_audit()`` 会用平台自己的通道分级常量再核一遍，所以「未知」不会变成
    「悄悄放行」——见该函数的 ``unclassified_*`` 字段。
    """
    normalized = normalize_tool_name(name)
    if not normalized:
        return True  # 空名字无从判定 → 拒绝（fail-closed）
    if normalized in WRITE_TOOL_NAMES:
        return True
    if normalized.startswith(WRITE_TOOL_PREFIXES):
        return True
    return any(part in normalized for part in WRITE_TOOL_SUBSTRINGS)


def whitelist(names):
    """从一份工具名清单里挑出白名单（= 非写/交易）并保持原顺序。"""
    return [name for name in names if not is_write_tool(name)]


def denied_from(names):
    """从一份工具名清单里挑出被白名单**排除**的那些（证据用，顺序保持）。"""
    return [name for name in names if is_write_tool(name)]


# ---------------------------------------------------------------------------
# 换行分帧（协议：one JSON-RPC 2.0 message per \n-terminated line）
# ---------------------------------------------------------------------------

class FrameOverflow(RuntimeError):
    """一行的字节数超过 ``max_line_bytes``。"""


class JsonRpcFramer:
    """字节流 → JSON-RPC 帧。

    与 ``@deepseek-ai/dsh-sdk-protocol`` 的 ``JsonRpcLineTransport`` 同语义：
    一行一帧（``\\r\\n`` 兼容）、空行跳过、畸形行**忽略并计数**（不抛、不断流）。
    额外加一项传输层没有的保护：单行字节上限，超限的那一行被丢弃并计入 ``overflows``。
    """

    def __init__(self, max_line_bytes=MAX_LINE_BYTES):
        self.max_line_bytes = int(max_line_bytes)
        self.buffer = bytearray()
        self.malformed = 0
        self.overflows = 0
        self.frames = 0
        self._overflowing = False

    @property
    def pending_bytes(self):
        return len(self.buffer)

    def feed(self, chunk):
        """喂一块字节（或 str），返回本次解出的帧（dict 列表，顺序即到达顺序）。"""
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        out = []
        self.buffer.extend(chunk or b"")
        while True:
            index = self.buffer.find(b"\n")
            if index < 0:
                break
            line = bytes(self.buffer[:index])
            del self.buffer[:index + 1]
            if self._overflowing:
                # 超限行的尾巴：连同换行一起丢掉，只计一次。
                self._overflowing = False
                continue
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line.strip():
                continue
            if len(line) > self.max_line_bytes:
                self.overflows += 1
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.malformed += 1
                continue
            if not isinstance(message, dict):
                self.malformed += 1
                continue
            self.frames += 1
            out.append(message)
        # 还没等到换行的半包：超限就整段丢弃，并进入「丢到下一个换行」状态。
        if len(self.buffer) > self.max_line_bytes:
            if not self._overflowing:
                self.overflows += 1
            self._overflowing = True
            self.buffer = bytearray()
        return out


# ---------------------------------------------------------------------------
# 错误类型（每一种都带「可如实上报」的字段）
# ---------------------------------------------------------------------------

class SdkError(RuntimeError):
    """本模块所有错误的基类；``to_dict()`` 是上报用的稳定形状。"""

    code = "v3-sdk/error"

    def __init__(self, message, **detail):
        super().__init__(message)
        self.message = str(message)
        self.detail = detail

    def to_dict(self):
        payload = {"code": self.code, "message": self.message}
        payload.update({key: value for key, value in self.detail.items() if value is not None})
        return payload


class SdkNotRunning(SdkError):
    code = "v3-sdk/not-running"


class SdkHandshakeTimeout(SdkError):
    code = "v3-sdk/handshake-timeout"


class SdkProtocolMismatch(SdkError):
    code = "v3-sdk/protocol-mismatch"


class SdkRpcError(SdkError):
    code = "v3-sdk/rpc-error"


class SdkRequestTimeout(SdkError):
    code = "v3-sdk/request-timeout"


class SdkProcessExited(SdkError):
    """子进程没了。``stderr`` 原文与 ``exit_code`` 一定带着（不允许只有一句『失败』）。"""

    code = "v3-sdk/process-exited"


class SdkPassphraseRequired(SdkError):
    code = "v3-sdk/passphrase-required"


#: 真机实测（2026-09-20）的一条协议行为：**同一个 session_id 不能跨进程复用**。
#: ``dsh`` 的会话存储里已有同名会话时，``session/prompt`` 会回 ``-32603
#: session "<id>" already exists``。协议里没有 resume/close，所以可用的只有两条路：
#: 换一个 session_id，或在**同一个 runtime 进程内**复用（那正是 ``SdkSessionState`` 的用途）。
SESSION_EXISTS_HINT = (
    "该 session_id 在此 DSH_HOME 的会话存储里已经存在，而 SDK 协议没有 resume/close 方法，"
    "同一个 id 无法跨进程复用：请换一个 session_id，或在同一个 runtime 进程内复用该会话。")


def explain_rpc_error(error):
    """把 wire 上的错误翻成「可执行」的形状：**保留原文**，只额外附上已知行为的提示。"""
    detail = error.to_dict()
    if error.code == "v3-sdk/rpc-error" and "already exists" in str(error.message):
        detail["session_exists"] = True
        detail["hint"] = SESSION_EXISTS_HINT
    return detail


# ---------------------------------------------------------------------------
# 运行时（一个 dsh 子进程 = 一个 SDK runtime）
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _default_spawn(argv, env, cwd):
    """真机路径：起 ``dsh --profile <profile>``。

    ``bufsize=-1``（默认）让 ``stdout`` 是 ``BufferedReader``——**必须**，因为流式分帧靠
    ``read1()`` 拿「当前可读的一块」，而 ``bufsize=0`` 给的是 raw ``FileIO``（没有 ``read1``）。
    stdin 每次写后 flush（``_write`` 里做了）。
    """
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
    )


class _Pending:
    """一个在途请求的等待位。"""

    __slots__ = ("event", "result", "error")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None

    def resolve(self, result):
        self.result = result
        self.event.set()

    def reject(self, error):
        self.error = error
        self.event.set()


class SdkSessionState:
    """客户端侧的会话状态：事件环 + 状态转换 + 队列 + 错误。"""

    def __init__(self, session_id, event_buffer=200):
        self.session_id = str(session_id)
        self.status = "unknown"
        self.created_at = _now_iso()
        self.updated_at = self.created_at
        self.events = []          # 归一化后的事件（含 cursor）
        self.event_buffer = int(event_buffer)
        self.cursor = 0           # 本会话已收到的最大事件序号（客户端计数，不是协议里的 seq）
        self.event_count = 0
        self.status_history = []  # [(at, status)]，保留最近 50 条
        self.prompts = []         # 下发流水（含排队/已发/收据/失败）
        self.queue = deque()      # 待下发的提示词 id
        self.in_flight = None
        self.message_ids = []
        self.last_error = None
        self.turn_index = 0

    def note_status(self, status):
        self.status = status
        self.updated_at = _now_iso()
        self.status_history.append({"at": self.updated_at, "status": status})
        if len(self.status_history) > 50:
            del self.status_history[:-50]

    def add_event(self, event):
        self.cursor += 1
        self.event_count += 1
        self.updated_at = _now_iso()
        record = {"cursor": self.cursor, "at": self.updated_at, **summarize_event(event)}
        self.events.append(record)
        if len(self.events) > self.event_buffer:
            del self.events[:-self.event_buffer]
        return record

    def view(self, since=0, limit=50):
        events = [item for item in self.events if item["cursor"] > int(since or 0)]
        if limit is not None and limit >= 0:
            events = events[-int(limit):] if limit else []
        return {
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "event_count": self.event_count,
            "cursor": self.cursor,
            "event_buffer": len(self.events),
            "status_history": list(self.status_history),
            "prompts_total": len(self.prompts),
            "prompts_queued": len(self.queue),
            "in_flight": self.in_flight,
            "message_ids": list(self.message_ids[-20:]),
            "prompts": [{key: value for key, value in item.items() if not key.startswith("_")}
                        for item in self.prompts[-50:]],
            "last_error": self.last_error,
            "events": events,
        }


def summarize_event(event):
    """把一个 ``SessionEvent`` 信封压成「够看 + 可审计」的一条。

    ``raw`` 保留原文（截断到 4000 字符）：事件内容由 Harness 拥有，本模块只做投影，
    不改写、不推断。``text`` 只在 assistant/user 消息上提取，方便前端直接渲染。
    """
    if not isinstance(event, dict):
        return {"type": "unknown", "seq": None, "time": None,
                "text": "", "raw": json.dumps(event, ensure_ascii=False)[:4000]}
    kind = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    text = ""
    if kind in ("assistant/message", "user/message", "assistant/attempt"):
        message = data.get("message") if isinstance(data.get("message"), dict) else data
        blocks = message.get("content") if isinstance(message, dict) else None
        parts = []
        for block in blocks or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        text = "\n".join(part for part in parts if part)
    elif kind == "turn/end":
        reason = data.get("reason")
        text = json.dumps(reason, ensure_ascii=False)[:400] if reason is not None else ""
    raw = json.dumps(event, ensure_ascii=False)
    return {
        "type": kind,
        "seq": event.get("seq"),
        "time": event.get("time"),
        "text": text[:4000],
        "raw": raw[:4000],
        "raw_truncated": len(raw) > 4000,
    }


class SdkRuntime:
    """一个 ``dsh --profile <profile>`` 子进程 + 它的 JSON-RPC 客户端状态机。

    线程模型（简单且可证）：
      * **reader 线程**：``stdout.read1()`` → ``JsonRpcFramer`` → 分派（响应/通知）。
      * **stderr 线程**：逐行收进有界环（供异常退出时给原文）。
      * **dispatcher 线程**：会话级串行下发（一条 turn 结束、状态回到 idle 再发下一条）。
    """

    def __init__(self, profile=DEFAULT_PROFILE, dsh_bin=None, dsh_home=None, cwd=None,
                 provider=None, model=None, reasoning_effort=None, max_tokens=None,
                 env=None, spawn=None, handshake_timeout=180.0, prompt_timeout=600.0,
                 stop_timeout=15.0, event_buffer=200, stderr_lines=200,
                 max_line_bytes=MAX_LINE_BYTES, clock=time.monotonic):
        self.profile = str(profile or DEFAULT_PROFILE)
        self.dsh_bin = str(dsh_bin or os.environ.get("QUANT_SDK_DSH") or "dsh")
        self.dsh_home = str(dsh_home) if dsh_home else None
        self.cwd = str(cwd) if cwd else None
        self.provider = provider or os.environ.get("QUANT_SDK_PROVIDER") or "deepseek-official"
        self.model = model or os.environ.get("QUANT_SDK_MODEL") or "deepseek-flash"
        self.reasoning_effort = reasoning_effort or os.environ.get("QUANT_SDK_EFFORT") or None
        self.max_tokens = max_tokens
        self._env_override = env
        self._spawn = spawn or _default_spawn
        self.handshake_timeout = float(handshake_timeout)
        self.prompt_timeout = float(prompt_timeout)
        self.stop_timeout = float(stop_timeout)
        self.event_buffer = int(event_buffer)
        self.stderr_lines = int(stderr_lines)
        self._clock = clock

        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._framer = JsonRpcFramer(max_line_bytes=max_line_bytes)
        self._proc = None
        self._threads = []
        self._pending = {}
        self._request_seq = 0
        self._sessions = {}
        self._stderr = deque(maxlen=self.stderr_lines)
        self._stderr_truncated = 0
        self._exit_code = None
        self._exit_reason = None
        self._stdout_closed = False
        self._stopping = False
        self._state = "stopped"      # stopped | running | exited | failed
        self._instance_error = None  # 进程级错误（spawn 失败 / 异常退出）
        self._handshake = {"state": "none"}
        self._counters = {"notifications": 0, "session_events": 0, "status_events": 0,
                          "subagent_started": 0, "subagent_finished": 0,
                          "server_requests": 0, "responses": 0, "malformed_lines": 0,
                          "frame_overflows": 0}
        self.started_at = None
        self.pid = None
        self.argv = None

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def build_argv(self):
        return [self.dsh_bin, "--profile", self.profile]

    def build_env(self):
        env = dict(os.environ if self._env_override is None else self._env_override)
        if self.dsh_home:
            env["DSH_HOME"] = self.dsh_home
        return env

    @property
    def running(self):
        return self._state == "running"

    def start(self):
        """起子进程 + 三个线程。**不做握手**（握手见 ``handshake()``，它是就绪边界）。"""
        with self._lock:
            if self._state == "running":
                return self.status()
        self.argv = self.build_argv()
        try:
            proc = self._spawn(argv=self.argv, env=self.build_env(), cwd=self.cwd)
        except OSError as error:
            with self._lock:
                self._state = "failed"
                self._instance_error = {
                    "code": "v3-sdk/spawn-failed", "message": str(error),
                    "argv": list(self.argv), "at": _now_iso()}
            raise SdkError(f"无法启动 {self.argv[0]}：{error}",
                           argv=list(self.argv), stderr="", exit_code=None) from error
        with self._lock:
            self._proc = proc
            self.pid = getattr(proc, "pid", None)
            self._state = "running"
            self.started_at = _now_iso()
            self._exit_code = None
            self._stdout_closed = False
            self._stopping = False
        self._spawn_thread(self._reader_loop, "dsh-sdk-reader")
        self._spawn_thread(self._stderr_loop, "dsh-sdk-stderr")
        self._spawn_thread(self._dispatch_loop, "dsh-sdk-dispatch")
        return self.status()

    def _spawn_thread(self, target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    def handshake(self, timeout=None):
        """``initialize``：运行时就绪边界 + 协议标识如实比对（FR-GATEWAY-002）。"""
        timeout = self.handshake_timeout if timeout is None else float(timeout)
        with self._lock:
            if self._state == "stopped":
                raise SdkNotRunning("runtime 未启动：先 start()")
        params = {"cwd": self.cwd or os.getcwd(), "provider": self.provider,
                  "model": self.model}
        if self.reasoning_effort:
            params["reasoningEffort"] = self.reasoning_effort
        if self.max_tokens:
            params["maxTokens"] = int(self.max_tokens)
        started = self._clock()
        try:
            result = self._request("initialize", params, timeout=timeout)
        except SdkRequestTimeout as error:
            elapsed_ms = int((self._clock() - started) * 1000)
            self._handshake = {"state": "timeout", "expected": SPEC_SERVER_NAME,
                               "observed": None, "protocol_match": False,
                               "elapsed_ms": elapsed_ms, "params": params,
                               "error": error.to_dict(), "at": _now_iso()}
            raise SdkHandshakeTimeout(
                f"initialize 在 {timeout:.0f}s 内没有响应；协议标识未验证（不得视为就绪）",
                elapsed_ms=elapsed_ms, params=params,
                stderr=self.stderr_text(), exit_code=self._exit_code) from error
        elapsed_ms = int((self._clock() - started) * 1000)
        info = result.get("serverInfo") if isinstance(result, dict) else None
        if not isinstance(info, dict):
            info = {}
        observed = info.get("name")
        match = observed == SPEC_SERVER_NAME
        self._handshake = {
            "state": "ok" if match else "protocol-mismatch",
            "expected": SPEC_SERVER_NAME,
            "observed": observed,
            "version": info.get("version"),
            "protocol_match": bool(match),
            "elapsed_ms": elapsed_ms,
            "params": params,
            "at": _now_iso(),
            "spec": "docs/v3-spec.md FR-GATEWAY-002 / §5.2.2",
        }
        return dict(self._handshake)

    def handshake_ok(self):
        return bool(self._handshake.get("protocol_match"))

    def stop(self, timeout=None):
        """进程级停机：先 ``shutdown``（协议规定它 dispose 根 fiber 并 exit 0），

        再关 stdin（EOF 由 app bin 负责退出），最后 terminate/kill。全程只报真实发生的。
        """
        timeout = self.stop_timeout if timeout is None else float(timeout)
        with self._lock:
            proc = self._proc
            self._stopping = True
        if proc is None:
            self._state = "stopped"
            return {"ok": True, "state": self._state, "note": "没有子进程"}
        shutdown_error = None
        if not self._stdout_closed:
            try:
                self._request("shutdown", {}, timeout=min(timeout, 10.0))
            except SdkError as error:
                shutdown_error = error.to_dict()
        self._close_stdin()
        code = self._wait_exit(timeout)
        if code is None:
            try:
                proc.terminate()
            except OSError:
                pass
            code = self._wait_exit(5.0)
        if code is None:
            try:
                proc.kill()
            except OSError:
                pass
            code = self._wait_exit(5.0)
        with self._lock:
            self._state = "stopped" if code is not None else self._state
            if code is not None:
                self._exit_code = code
        return {"ok": code is not None, "state": self._state, "exit_code": code,
                "forced": code not in (0, None), "shutdown_error": shutdown_error,
                "stderr": self.stderr_text()}

    def _close_stdin(self):
        stdin = getattr(self._proc, "stdin", None)
        if stdin is None:
            return
        try:
            stdin.close()
        except (OSError, ValueError):
            pass

    def _wait_exit(self, timeout):
        proc = self._proc
        if proc is None:
            return self._exit_code
        try:
            return proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 —— 假进程/子进程实现差异，轮询兜底
            deadline = self._clock() + timeout
            while self._clock() < deadline:
                if proc.poll() is not None:
                    return proc.poll()
                time.sleep(0.05)
            return proc.poll()

    # ── 传输 ────────────────────────────────────────────────────────────────

    def _reader_loop(self):
        stream = getattr(self._proc, "stdout", None)
        read1 = getattr(stream, "read1", None) or getattr(stream, "read", None)
        try:
            while True:
                chunk = read1(65536)
                if not chunk:
                    break
                for message in self._framer.feed(chunk):
                    self._on_frame(message)
        except (ValueError, OSError) as error:  # 流被我们关掉/管道断开
            self._note_instance_error("v3-sdk/stdout-error", str(error))
        except Exception as error:  # noqa: BLE001 —— 读取线程绝不裸抛（那会丢掉退出原因）
            self._note_instance_error("v3-sdk/stdout-reader-failed",
                                      f"{type(error).__name__}: {error}")
        finally:
            self._on_stdout_end()

    def _stderr_loop(self):
        stream = getattr(self._proc, "stderr", None)
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
                with self._lock:
                    for line in text.splitlines():
                        self._stderr.append(line)
                    self._stderr_truncated = 0
        except (ValueError, OSError):
            return
        except Exception:  # noqa: BLE001 —— 收 stderr 失败不该影响主流程
            return

    def _on_stdout_end(self):
        """stdout EOF = 进程（正在）结束。这里的结论只能是「真的观测到的」。"""
        code = self._exit_code
        proc = self._proc
        if proc is not None:
            try:
                code = proc.wait(timeout=10.0)
            except Exception:  # noqa: BLE001
                code = proc.poll()
        with self._lock:
            self._stdout_closed = True
            if code is not None:
                self._exit_code = code
            if not self._stopping:
                self._state = "exited" if code == 0 else "failed"
                self._instance_error = {
                    "code": "v3-sdk/process-exited",
                    "message": f"dsh 子进程退出，exit_code={code}",
                    "exit_code": code,
                    "stderr": self.stderr_text(),
                    "argv": list(self.argv or []),
                    "at": _now_iso(),
                }
            error = SdkProcessExited(
                f"dsh 子进程已退出（exit_code={code}）",
                exit_code=code, stderr=self.stderr_text(), argv=list(self.argv or []))
            for pending in self._pending.values():
                pending.reject(error)
            self._pending.clear()
            for session in self._sessions.values():
                session.last_error = error.to_dict()
                if session.in_flight:
                    item = session.in_flight
                    item["state"] = "failed"
                    item["error"] = error.to_dict()
                    session.in_flight = None
            self._wake.notify_all()

    def _note_instance_error(self, code, message):
        with self._lock:
            self._instance_error = {"code": code, "message": message, "at": _now_iso(),
                                    "stderr": self.stderr_text(), "exit_code": self._exit_code}

    def _write(self, message):
        stdin = getattr(self._proc, "stdin", None)
        if stdin is None:
            raise SdkNotRunning("没有可写的 stdin")
        payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        with self._write_lock:
            try:
                stdin.write(payload.encode("utf-8"))
                stdin.flush()
            except (OSError, ValueError) as error:
                raise SdkProcessExited(
                    f"写 stdin 失败：{error}", exit_code=self._exit_code,
                    stderr=self.stderr_text()) from error

    def _request(self, method, params, timeout):
        with self._lock:
            if self._state != "running":
                raise SdkProcessExited(
                    f"runtime 状态 {self._state}，不能发送 {method}",
                    exit_code=self._exit_code, stderr=self.stderr_text(),
                    argv=list(self.argv or []))
            self._request_seq += 1
            request_id = f"req_{self._request_seq}"
            pending = _Pending()
            self._pending[request_id] = pending
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                         "params": params})
        except SdkError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise SdkRequestTimeout(
                f"{method} 在 {timeout:.0f}s 内没有响应", method=method,
                request_id=request_id, stderr=self.stderr_text(),
                exit_code=self._exit_code)
        if pending.error is not None:
            raise pending.error
        return pending.result

    def _on_frame(self, message):
        has_id = "id" in message
        has_method = "method" in message
        if has_id and has_method:
            # 协议声明服务端从不发请求；真收到就按 JSON-RPC 规范回 -32601。
            with self._lock:
                self._counters["server_requests"] += 1
            try:
                self._write({"jsonrpc": "2.0", "id": message.get("id"),
                             "error": {"code": METHOD_NOT_FOUND,
                                       "message": f"客户端不实现方法 {message.get('method')}"}})
            except SdkError:
                pass
            return
        if has_id:
            with self._lock:
                self._counters["responses"] += 1
                pending = self._pending.pop(message.get("id"), None)
            if pending is None:
                return
            if "error" in message and isinstance(message.get("error"), dict):
                wire = message["error"]
                pending.reject(SdkRpcError(
                    str(wire.get("message") or "JSON-RPC error"),
                    rpc_code=wire.get("code"), data=wire.get("data")))
            else:
                pending.resolve(message.get("result"))
            return
        if has_method:
            self._on_notification(str(message.get("method")), message.get("params"))

    def _on_notification(self, method, params):
        params = params if isinstance(params, dict) else {}
        with self._lock:
            self._counters["notifications"] += 1
        if method == "session.event":
            session_id = str(params.get("sessionId") or "")
            event = params.get("event")
            with self._lock:
                self._counters["session_events"] += 1
                session = self._session(session_id)
                session.add_event(event)
                if isinstance(event, dict) and event.get("type") == "turn/end":
                    session.turn_index += 1
                self._wake.notify_all()
            return
        if method == "session.status":
            session_id = str(params.get("sessionId") or "")
            status = str(params.get("status") or "")
            with self._lock:
                self._counters["status_events"] += 1
                self._session(session_id).note_status(status)
                self._wake.notify_all()
            return
        if method in ("subagent.started", "subagent.finished"):
            key = "subagent_started" if method.endswith("started") else "subagent_finished"
            with self._lock:
                self._counters[key] += 1
                session = self._session(str(params.get("parentSessionId") or ""))
                session.add_event({"type": method, "seq": None, "time": None, "data": params})
            return
        # 未知通知：计数不留白。
        with self._lock:
            unknown = self._counters.setdefault("unknown_notifications", {})
            unknown[method] = unknown.get(method, 0) + 1

    # ── 会话 ────────────────────────────────────────────────────────────────

    def _session(self, session_id):
        key = str(session_id or "")
        session = self._sessions.get(key)
        if session is None:
            session = SdkSessionState(key, event_buffer=self.event_buffer)
            self._sessions[key] = session
        return session

    def session(self, session_id):
        with self._lock:
            return self._session(session_id)

    def enqueue_prompt(self, session_id, text, timeout=None, wait_receipt=5.0):
        """把提示词排入该会话的队列（协议自己也会排队；客户端串行是为了可观测的顺序）。

        返回**排队收据**：``{ok, session_id, queue_id, state, position, message_id}``。
        ``message_id`` 是服务器对 ``session/prompt`` 的**入队收据**（协议明确它不是 per-prompt
        结果），只在真正下发并收到响应后才出现；``wait_receipt`` 秒内没拿到就如实返回
        ``state="queued"`` + ``message_id=None``，绝不编一个 id 出来。
        """
        session_id = str(session_id or "")
        if not session_id:
            raise SdkError("session_id 不能为空")
        text = str(text or "")
        if not text.strip():
            raise SdkError("提示词不能为空")
        if len(text) > MAX_PROMPT_CHARS:
            raise SdkError(f"提示词过长（{len(text)} > {MAX_PROMPT_CHARS} 字符）",
                           length=len(text), limit=MAX_PROMPT_CHARS)
        with self._lock:
            if self._state != "running":
                raise SdkProcessExited(
                    f"runtime 状态 {self._state}，不能下发提示词",
                    exit_code=self._exit_code, stderr=self.stderr_text())
            if not self._handshake.get("protocol_match"):
                raise SdkProtocolMismatch(
                    "initialize 握手未通过（或未执行），拒发提示词（fail-closed）",
                    handshake=dict(self._handshake))
            session = self._session(session_id)
            item = {
                "queue_id": f"q_{len(session.prompts) + 1}",
                "session_id": session_id,
                "queued_at": _now_iso(),
                "sent_at": None,
                "state": "queued",
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "preview": text[:200],
                "message_id": None,
                "error": None,
                # 载荷全文只在本进程内存里（下发用）；view() 不投影它。
                "_text": text,
            }
            session.prompts.append(item)
            session.queue.append(item)
            position = len(session.queue)
            self._wake.notify_all()
            # 有界等待「真正发出去并拿到服务器收据」；不改变『排队成功』这个事实。
            deadline = self._clock() + max(0.0, float(wait_receipt or 0.0))
            while (item["state"] == "queued" and self._state == "running"
                   and self._clock() < deadline):
                self._wake.wait(min(0.2, max(0.0, deadline - self._clock())))
            receipt = {"ok": True, "session_id": session_id, "queue_id": item["queue_id"],
                       "state": item["state"], "position": position,
                       "message_id": item["message_id"], "chars": item["chars"],
                       "sha256": item["sha256"]}
            if item.get("error"):
                receipt["error"] = item["error"]
        return receipt

    def _dispatch_loop(self):
        """串行下发：同一会话一次只有一条在飞；上一条状态回到 idle 才发下一条。"""
        while True:
            with self._lock:
                item = None
                for session in self._sessions.values():
                    if session.queue and session.in_flight is None:
                        item = session.queue.popleft()
                        # 轮次基线：完成判定 = 观测到比它更大的 turn_index。
                        item["_turn_baseline"] = session.turn_index
                        session.in_flight = item
                        break
                if item is None:
                    if self._state not in ("running",):
                        return
                    self._wake.wait(0.5)
                    continue
                session = self._sessions[item["session_id"]]
                if self._state != "running":
                    item["state"] = "failed"
                    item["error"] = self._instance_error or {
                        "code": "v3-sdk/not-running", "message": f"runtime {self._state}"}
                    session.in_flight = None
                    continue
            self._send_prompt(session, item)

    def _send_prompt(self, session, item):
        # 载荷全文只留在 ``_text`` 里（对外视图只给 preview/sha256，不把全文塞进响应）。
        params = {"sessionId": session.session_id,
                  "contentBlocks": [{"type": "text", "text": item.get("_text", "")}]}
        try:
            result = self._request("session/prompt", params, timeout=self.prompt_timeout)
        except SdkError as error:
            detail = explain_rpc_error(error)
            with self._lock:
                item["state"] = "failed"
                item["error"] = detail
                self._settle(session, "failed", detail)
            return
        message_id = result.get("messageId") if isinstance(result, dict) else None
        with self._lock:
            item["state"] = "sent"
            item["sent_at"] = _now_iso()
            item["message_id"] = message_id
            if message_id:
                session.message_ids.append(message_id)
            self._wake.notify_all()
        # 边界：等到这一轮真的结束（观测到新的 ``turn/end``）。协议没有 per-prompt 结果，
        # 「轮次结束」是唯一有据可依的完成边界；不能用 status==idle，因为上一条 turn 结束后
        # 状态本来就是 idle，那样第二条会被误判成「立刻完成」。
        self._await_turn_end(session, item.get("_turn_baseline", session.turn_index))

    def _await_turn_end(self, session, baseline):
        deadline = self._clock() + self.prompt_timeout
        while True:
            with self._lock:
                if self._state != "running":
                    self._settle(session, "failed", session.last_error or {
                        "code": "v3-sdk/process-exited",
                        "message": f"runtime 状态 {self._state}",
                        "exit_code": self._exit_code, "stderr": self.stderr_text()})
                    return
                if session.turn_index > baseline:
                    self._settle(session, "done", None)
                    return
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._settle(session, "timeout", {
                        "code": "v3-sdk/turn-timeout",
                        "message": f"下发后 {self.prompt_timeout:.0f}s 内没有观测到新的 turn/end；"
                                   "轮次可能仍在进行（本字段是观测超时，不是执行失败）",
                        "turns_before": baseline, "turns_now": session.turn_index})
                    return
                self._wake.wait(min(0.5, remaining))

    def _settle(self, session, state, error):
        """收尾在途条目（在锁内调用）。``state`` 只取真实观测到的结论。"""
        item = session.in_flight
        if item is not None:
            item["state"] = state
            item["finished_at"] = _now_iso()
            item["turns_after"] = session.turn_index
            if error:
                item["error"] = error
                session.last_error = error
        session.in_flight = None
        self._wake.notify_all()

    # ── 上报 ────────────────────────────────────────────────────────────────

    def stderr_text(self, limit=4000):
        with self._lock:
            text = "\n".join(self._stderr)
        return text[-limit:]

    def status(self):
        with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda item: item.session_id)
            return {
                "state": self._state,
                "profile": self.profile,
                "argv": list(self.argv or self.build_argv()),
                "dsh_home": self.dsh_home,
                "cwd": self.cwd,
                "pid": self.pid,
                "started_at": self.started_at,
                "running": self._state == "running",
                "exit_code": self._exit_code,
                "stdout_closed": self._stdout_closed,
                "handshake": dict(self._handshake),
                "counters": dict(self._counters),
                "framer": {"frames": self._framer.frames,
                           "malformed_lines": self._framer.malformed,
                           "overflows": self._framer.overflows,
                           "pending_bytes": self._framer.pending_bytes,
                           "max_line_bytes": self._framer.max_line_bytes},
                "sessions": {item.session_id: {"status": item.status,
                                               "event_count": item.event_count,
                                               "queued": len(item.queue),
                                               "in_flight": bool(item.in_flight)}
                             for item in sessions},
                "last_error": self._instance_error,
                "stderr_tail": self.stderr_text(),
                "requested_route": {"provider": self.provider, "model": self.model,
                                    "reasoningEffort": self.reasoning_effort,
                                    "maxTokens": self.max_tokens},
            }

    def sessions_view(self, session_id=None, since=0, limit=50):
        with self._lock:
            items = self._sessions.values()
            if session_id:
                items = [item for item in items if item.session_id == session_id]
            return [item.view(since=since, limit=limit)
                    for item in sorted(items, key=lambda item: item.session_id)]


# ---------------------------------------------------------------------------
# 进程注册表（每个 home + profile 一份，进程级单例）
# ---------------------------------------------------------------------------

_REGISTRY = {}
_REGISTRY_LOCK = threading.Lock()
_ATEXIT_ARMED = False


def runtime_options(home, environ=None):
    """从环境读配置（**不**自动改 ``os.environ``；测试注入 ``environ`` 即可离线）。"""
    environ = os.environ if environ is None else environ
    return {
        "profile": environ.get("QUANT_SDK_PROFILE") or DEFAULT_PROFILE,
        "dsh_bin": environ.get("QUANT_SDK_DSH") or "dsh",
        "dsh_home": environ.get("QUANT_SDK_DSH_HOME") or environ.get("DSH_HOME") or None,
        "cwd": environ.get("QUANT_SDK_CWD") or None,
        "provider": environ.get("QUANT_SDK_PROVIDER") or "deepseek-official",
        "model": environ.get("QUANT_SDK_MODEL") or "deepseek-flash",
    }


def get_runtime(home, environ=None, spawn=None):
    """取（或建）本 home 的 runtime。**只建对象，不起进程**——起进程是显式动作。"""
    options = runtime_options(home, environ)
    key = (str(home), options["profile"])
    with _REGISTRY_LOCK:
        runtime = _REGISTRY.get(key)
        if runtime is None:
            runtime = SdkRuntime(spawn=spawn, **options)
            _REGISTRY[key] = runtime
        elif spawn is not None:
            runtime._spawn = spawn
        _arm_atexit()
    return runtime


def _arm_atexit():
    global _ATEXIT_ARMED
    if _ATEXIT_ARMED:
        return
    _ATEXIT_ARMED = True
    atexit.register(_stop_all)


def _stop_all():
    with _REGISTRY_LOCK:
        runtimes = list(_REGISTRY.values())
    for runtime in runtimes:
        try:
            if runtime.running:
                runtime.stop()
        except Exception:  # noqa: BLE001 —— 退出路径绝不抛
            pass


def metrics_view(home=None):
    """**只读**进程级度量视图（给 ``server.observability`` 的 §8.3 监控规则取数）。

    契约（``observability._optional_metrics_view`` 唯一读取的字段）::

        {"activeSessions": <int | None>, ...}

    「活跃会话数」= 已存在 runtime 对象里**已创建**的会话条目数（``SdkSessionState``；
    协议没有 per-session close，会话活到进程关闭为止——见模块头「硬边界」）。语义与
    ``runtimes`` / ``sessions`` 一起返回，调用方不必猜。

    **为什么没有运行时给 ``None`` 而不是 0**（与 ``observability.sdk_active_sessions`` 的
    no-data 纪律同一条）：两种 0 是不同的事实，不能混成一个数——

      * ``activeSessions = 0``：**有**运行时对象、只是当前一个活跃会话都没有（真读数）；
      * ``activeSessions = None``：本进程里**根本没有** SDK 运行时对象 → 「没有事实来源」，
        调用方应退回它的下一级来源（审计日志 / ``sdk_turns`` 表），或如实报 no-data。
        ``observability`` 的 §8.3 规则正是按 `None` 导出
        ``quantwb_sdk_active_sessions_source_missing`` 告警，而不是把「没有 SDK」写成
        「0 个活跃会话」。``runtimes`` 字段把这一区分显式给出。

    **三条硬约束**（与 ``observability.sdk_active_sessions`` 的注释一致）:

      1. **不创建任何对象**：刻意**不**调 :func:`get_runtime`（那会 new 出 ``SdkRuntime``
         并 arm atexit），只读**已存在**的 ``_REGISTRY``；
      2. **不起进程 / 无副作用**：不 ``start()``、不握手、不写盘、不发网络请求；
      3. **模块未接线也安全**：注册表为空 → 直接返回 ``activeSessions=None``（不抛、不造对象）。

    ``home`` 给定时只数该 home 的 runtime（key 是 ``(home, profile)``——只用 key 的
    第一段判断，不解析对象内部），缺省数全部。
    """
    stamp = _now_iso()
    with _REGISTRY_LOCK:
        items = list(_REGISTRY.items())
    wanted = None if home in (None, "") else str(home)
    sessions = {}
    runtimes = 0
    live_processes = 0
    try:
        for key, runtime in items:
            if wanted is not None and str(key[0] if isinstance(key, tuple) else key) != wanted:
                continue
            runtimes += 1
            if getattr(runtime, "_state", None) == "running":
                live_processes += 1
            states = getattr(runtime, "_sessions", None)
            if isinstance(states, dict):
                for session_id, state in states.items():
                    identifier = str(getattr(state, "session_id", session_id))
                    # 同一 session_id 出现在多个 runtime（不同 profile）时只算一次
                    sessions.setdefault(identifier, getattr(state, "status", "unknown"))
    except Exception:  # noqa: BLE001 —— 抓取路径绝不抛：拿不到就报「没有来源」
        return {"activeSessions": None, "runtimes": 0, "runningProcesses": 0,
                "sessions": {}, "asOf": stamp,
                "note": "读取 v3_sdk 进程内注册表失败 → activeSessions=null（没有事实来源，"
                        "不按 0 报；只读视图绝不抛）"}
    return {
        "activeSessions": len(sessions) if runtimes else None,
        "runtimes": runtimes,
        "runningProcesses": live_processes,
        "sessions": dict(sorted(sessions.items())),
        "asOf": stamp,
        "note": ("只读：读 _REGISTRY 里**已存在**的 runtime 对象（不调 get_runtime、不起进程、"
                 "不写盘）；会话按 session_id 去重，协议无 per-session close，"
                 "会话活到进程关闭为止；没有运行时对象时 activeSessions=null（没有事实来源）"),
    }


def reset_registry():
    """测试用：清空注册表（**不**停进程——调用方自己 stop）。"""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


# ---------------------------------------------------------------------------
# 工具白名单审计（真值来自平台自己的通道分级常量）
# ---------------------------------------------------------------------------

def platform_tool_catalog(app=None):
    """平台**真实**工具面清单：``[{name, source, endpoint}]``。

    两个来源，都是本进程里已注册的真对象，不复制任何业务逻辑：
      * ``server.mcp_tools.TOOLS``（工作台工具面，77 件）；
      * ``app.state.v3_mcp_bridge.definitions``（``/api/v3/*`` 路由桥接出的 ``v3_*`` 工具，
        随路由表自动增减——本模块自己的三条路由也会出现在里面）。
    """
    from server import mcp_tools

    out = []
    for definition in mcp_tools.TOOLS:
        out.append({"name": definition.name, "source": "mcp/quantwb",
                    "endpoint": definition.endpoint})
    bridge = getattr(getattr(app, "state", None), "v3_mcp_bridge", None)
    for definition in getattr(bridge, "definitions", ()) or ():
        out.append({"name": definition.name, "source": "mcp/v3-bridge",
                    "endpoint": definition.endpoint})
    return out


def whitelist_audit(app=None):
    """白名单的**可核对**形状：允许谁、拒了谁、拒的理由来自哪一层。"""
    from server import v3_mcp

    catalog = platform_tool_catalog(app)
    allowed, denied = [], []
    for entry in catalog:
        record = dict(entry)
        reason = write_tool_reason(entry["name"])
        # 平台自己的通道分级再核一遍（桥接工具的 endpoint 在 NON_READONLY_PATHS 里 = 写）
        if reason is None and entry.get("endpoint") in v3_mcp.NON_READONLY_PATHS:
            reason = f"平台通道分级 v3_mcp.NON_READONLY_PATHS 含 {entry['endpoint']}"
        if reason is None:
            allowed.append(record)
        else:
            record["reason"] = reason
            denied.append(record)
    return {
        "catalog_total": len(catalog),
        "allowed_total": len(allowed),
        "denied_total": len(denied),
        "allowed": [item["name"] for item in allowed],
        "denied": denied,
        # 断言形状：白名单里不得有任何写工具（跑出来的，不是写死的）
        "violations": [item["name"] for item in allowed if is_write_tool(item["name"])],
        "sources": sorted({item["source"] for item in catalog}),
        "write_tool_names": sorted(WRITE_TOOL_NAMES),
        # 平台常量认定的写工具是否都被静态策略覆盖（空 = 无漂移；非空即测试变红的缺口）
        "platform_write_tools": sorted(platform_write_tool_names()),
        "policy_gaps": sorted(name for name in platform_write_tool_names()
                              if not is_write_tool(name)),
        "write_tool_prefixes": list(WRITE_TOOL_PREFIXES),
        "write_tool_substrings": list(WRITE_TOOL_SUBSTRINGS),
        "profile_patch": "platform/install/quant-sdk/cordis.patch.yml",
        "profile_guard": "platform/install/quant-sdk/tool-whitelist/index.js",
    }


def write_tool_reason(name):
    """命中哪一档就返回那一档的说明，未命中返回 None。"""
    normalized = normalize_tool_name(name)
    if not normalized:
        return "空工具名（fail-closed）"
    if normalized in WRITE_TOOL_NAMES:
        return f"精确名 {normalized}"
    for prefix in WRITE_TOOL_PREFIXES:
        if normalized.startswith(prefix):
            return f"前缀 {prefix}*"
    for part in WRITE_TOOL_SUBSTRINGS:
        if part in normalized:
            return f"子串 {part}"
    return None


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def audit_path(home):
    return os.path.join(str(home), AUDIT_FILE)


def append_audit(home, record):
    """追加一行审计。**失败不阻断业务**，但会把失败事实原样放进返回值。"""
    path = audit_path(home)
    payload = {"at": _now_iso(), **record}
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        if new_file:
            os.chmod(path, 0o600)
        return {"ok": True, "path": path}
    except OSError as error:
        return {"ok": False, "path": path,
                "error": {"code": "v3-sdk/audit-failed", "message": str(error)}}


def read_audit(home, limit=50):
    """读最近 ``limit`` 条审计（只读，倒序）。文件不存在就空列表，不编造。"""
    path = audit_path(home)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    out = []
    for line in lines[-int(limit or 50):]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def check_passphrase(value):
    """口令校验：**必须**逐字等于 ``确认下发``。空/None/别的一律拒绝。"""
    return isinstance(value, str) and value.strip() == PROMPT_PASSPHRASE


# ---------------------------------------------------------------------------
# HTTP 装配（app.py 自动接线：register(app, v3_run, home)）
# ---------------------------------------------------------------------------

def register(app, v3_run=None, home=None):
    """注册 ``/api/v3/sdk/{status,sessions,prompt}``。

    ``v3_run`` 有意**不使用**：本模块不下单、不取数、不写台账，唯一的动作是把人的提示词
    交给一个工具面已被收窄的 Harness 会话。保留参数是为了满足 app.py 的统一接线约定
    （``_register(app, v3_run, home)``，位置传参）。
    """
    import asyncio

    from fastapi import Request
    from fastapi.responses import JSONResponse

    base_home = str(home or ".")

    def respond(payload, status_code=200):
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/api/v3/sdk/status")
    async def v3_sdk_status():
        """**只读**：真实进程状态 + 握手结果（含协议标识比对）+ 白名单审计。

        有意**不**在这里起进程——启动/握手是显式动作（``POST /prompt`` 或
        ``POST /prompt {action:start}``，都要口令并落审计）。
        """
        runtime = get_runtime(base_home)
        payload = runtime.status()
        payload.update({
            "ok": True,
            "home": base_home,
            "spec_server_name": SPEC_SERVER_NAME,
            "protocol_match": bool(runtime.handshake_ok()),
            "audit_path": audit_path(base_home),
            "audit_recent": read_audit(base_home, limit=10),
            "whitelist": whitelist_audit(app),
            "source": "本进程内的真实 dsh 子进程与 JSON-RPC 帧计数",
        })
        return respond(payload)

    @app.get("/api/v3/sdk/sessions")
    async def v3_sdk_sessions(session: str = "", since: int = 0, limit: int = 50):
        """**只读**：会话状态 + 事件回放（``since`` 之后、最多 ``limit`` 条）。"""
        runtime = get_runtime(base_home)
        bounded = max(1, min(int(limit or 50), 500))
        items = runtime.sessions_view(session_id=session or None,
                                      since=max(0, int(since or 0)), limit=bounded)
        return respond({
            "ok": True,
            "state": runtime.status()["state"],
            "session_filter": session or None,
            "since": max(0, int(since or 0)),
            "limit": bounded,
            "count": len(items),
            "sessions": items,
            "source": "本进程内 SdkRuntime 的会话环（真实事件，非回填）",
        })

    @app.post("/api/v3/sdk/prompt")
    async def v3_sdk_prompt(request: Request):
        """**写端点**：需口令「确认下发」，落审计。

        body::

            {"prompt": "...", "session_id": "quant-001", "confirmation": "确认下发"}
            {"action": "start", "session_id": "quant-001", "confirmation": "确认下发"}

        ``action=start`` 只做「起进程 + initialize 握手」（不下发提示词）——它同样是显式
        动作，因此照样要口令、照样落审计。缺口令一律拒绝并留痕（拒绝不是静默）。
        """
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return respond({"ok": False, "error": {"code": "v3-sdk/bad-json",
                                                   "message": "请求体不是合法 JSON"}})
        if not isinstance(payload, dict):
            return respond({"ok": False, "error": {"code": "v3-sdk/bad-json",
                                                   "message": "请求体需为对象"}})
        session_id = str(payload.get("session_id") or "quant-001").strip() or "quant-001"
        action = str(payload.get("action") or "prompt").strip().lower()
        prompt = payload.get("prompt")
        confirmation = payload.get("confirmation")
        has_passphrase = check_passphrase(confirmation)

        def deny(code, message, **extra):
            append_audit(base_home, {
                "action": action, "session_id": session_id, "accepted": False,
                "passphrase_ok": bool(has_passphrase), "code": code,
                "prompt_sha256": (hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()
                                  if isinstance(prompt, str) else None),
                **extra,
            })
            return respond({"ok": False, "error": {"code": code, "message": message}},
                           status_code=200)

        if not has_passphrase:
            return deny("v3-sdk/passphrase-required",
                        f"下发到 Harness 会话需人工口令「{PROMPT_PASSPHRASE}」"
                        "（服务端 fail-closed：口令缺失/不符一律拒绝，且拒绝也落审计）",
                        provided=bool(confirmation))
        if action not in ("prompt", "start"):
            return deny("v3-sdk/unknown-action",
                        f"action 需为 prompt / start，收到 {action!r}")

        runtime = get_runtime(base_home)

        def run():
            steps = []
            if not runtime.running:
                runtime.start()
                steps.append({"step": "spawn", "ok": True, "pid": runtime.pid,
                              "argv": runtime.argv})
            handshake = runtime.handshake()
            steps.append({"step": "initialize", "ok": bool(handshake.get("protocol_match")),
                          "handshake": handshake})
            if not handshake.get("protocol_match"):
                return {"ok": False, "steps": steps,
                        "error": SdkProtocolMismatch(
                            "握手返回的协议标识与规格不符，拒发提示词（fail-closed）",
                            expected=SPEC_SERVER_NAME,
                            observed=handshake.get("observed")).to_dict()}
            if action == "start":
                return {"ok": True, "steps": steps, "action": "start",
                        "handshake": handshake}
            receipt = runtime.enqueue_prompt(session_id, prompt)
            steps.append({"step": "enqueue", "ok": True, "receipt": receipt})
            return {"ok": True, "steps": steps, "action": "prompt",
                    "handshake": handshake, "receipt": receipt}

        try:
            outcome = await asyncio.to_thread(run)
        except SdkError as error:
            outcome = {"ok": False, "error": error.to_dict()}
        except Exception as error:  # noqa: BLE001 —— 未预期异常不吞，如实回
            outcome = {"ok": False, "error": {"code": "v3-sdk/internal",
                                              "message": str(error)[:300]}}

        append_audit(base_home, {
            "action": action, "session_id": session_id,
            "accepted": bool(outcome.get("ok")), "passphrase_ok": True,
            "prompt_sha256": (hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()
                              if isinstance(prompt, str) else None),
            "prompt_preview": (str(prompt)[:200] if isinstance(prompt, str) else None),
            "prompt_chars": (len(prompt) if isinstance(prompt, str) else None),
            "profile": runtime.profile, "dsh_home": runtime.dsh_home, "pid": runtime.pid,
            "handshake_state": runtime._handshake.get("state"),
            "protocol_match": bool(runtime.handshake_ok()),
            "message_id": ((outcome.get("receipt") or {}).get("message_id")
                           if isinstance(outcome.get("receipt"), dict) else None),
            "queue_id": ((outcome.get("receipt") or {}).get("queue_id")
                         if isinstance(outcome.get("receipt"), dict) else None),
            "error": outcome.get("error"),
        })
        response = dict(outcome)
        response["session_id"] = session_id
        response["state"] = runtime.status()["state"]
        response["audit_path"] = audit_path(base_home)
        return respond(response)

    return app


__all__ = [
    "AUDIT_FILE", "DEFAULT_PROFILE", "FrameOverflow", "JsonRpcFramer", "MAX_LINE_BYTES",
    "MAX_PROMPT_CHARS", "PROMPT_PASSPHRASE", "SPEC_SERVER_NAME", "SdkError",
    "SdkHandshakeTimeout", "SdkNotRunning", "SdkPassphraseRequired", "SdkProcessExited",
    "SESSION_EXISTS_HINT", "SdkProtocolMismatch", "SdkRequestTimeout", "SdkRpcError",
    "SdkRuntime", "explain_rpc_error",
    "SdkSessionState", "WRITE_TOOL_NAMES", "WRITE_TOOL_PREFIXES", "WRITE_TOOL_SUBSTRINGS",
    "append_audit", "audit_path", "check_passphrase", "denied_from", "get_runtime",
    "metrics_view", "READ_ONLY_EXCLUDED_ENDPOINTS", "is_write_tool", "normalize_tool_name",
    "platform_tool_catalog", "platform_write_tool_names", "read_audit",
    "register", "reset_registry", "runtime_options", "summarize_event", "whitelist",
    "whitelist_audit", "write_tool_reason",
]
