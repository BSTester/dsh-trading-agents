"""工具发现代理（规格 FR-TOOLS-003 / §10 决策 3）：把 MCP 面的 schema 成本压下来。

问题（实测硬数字，见 ``platform/tools/mcp_surface_report.py``）
--------------------------------------------------------------
``direct`` 模式下 ``/mcp`` 的 ``tools/list`` 是 **116 条 / ≈79.6k 字符 / ≈2.0 万 token**
（系数 3.0 字符/token 的估算）——全部工具的名字 + 描述 + 完整 inputSchema 都进系统提示词，
**每一轮**都在上下文里。规格 FR-TOOLS-003 明确要求「MCP 服务器暴露一个 ``list_tools`` 入口
和一个 ``call_tool`` 入口，让 Harness Agent 通过间接调用发现具体能力，避免上百个工具 schema
撑爆上下文窗口」（§10 决策 3 同）。

两种模式（``QUANT_MCP_SURFACE``，切换只改这一个环境变量）
---------------------------------------------------------
=========================  ==========================================================
``discovery``（**默认**） 少量高频直连工具 + 两个代理入口（见下）
``direct``                 116 条全部直暴露（向后兼容：用户/运维可一行切回）
=========================  ==========================================================

``discovery`` 模式暴露什么、为什么
----------------------------------
直连保留 4 件（**探活 / 看目录**用，是「先探活再看详情」的最小面）：

======================  ==========================================================
``snapshot``            工作台全量快照（模式/在途/缺口）——一次调用看清账户面状态。
``admin_status``        本地运行台账与维护状态——服务是否健康、有哪些 run。
``v3_gateway``          网关与调度健康（通道状态 mcp/sdk/headless、心跳、作业历史）。
``v3_tools``            六域工具**目录**（data/alpha/ml/risk/execution/ecosystem）。
======================  ==========================================================

四件都是只读、无副作用的「开胃菜」，覆盖四个方向：账户面（``snapshot``）、本地服务面
（``admin_status``）、通道/调度面（``v3_gateway``）、能力目录面（``v3_tools``）。
即使模型全程不做任何发现调用，也能确认「平台活着、有哪些域」。
反例：``series`` / ``positions`` / ``trade_place`` 这类高频业务工具**不**直连——它们数量多、
schema 大，且调用前本就该先知道标的存在（``list_tools`` 一次检索就能拿到必填参数名）。

再加两个代理入口：

* ``list_tools(domain?, prefix?, keyword?, limit?, offset?)`` —— 检索工具面，返回**精简卡片**
  （名字 + 一句话用途 + 必填参数名 + 可选参数名），**绝不返回完整 schema**，默认一页 ≤20 张；
* ``call_tool(name, arguments)`` —— 把调用转发到**同一份实现**（同一个已注册函数对象），
  返回与直连**逐字段一致**的信封。

唯一事实来源（**不维护第二份工具清单**）
----------------------------------------
目录由**两个既有注册表**在装配期合并而成，本模块不写任何名字/描述/参数常量：

* ``mcp_tools.TOOLS``（工作台基础注册表，``app.state.mcp_tools`` 的 ``definition``）；
* ``v3_mcp.V3Bridge.definitions``（``/api/v3/*`` 路由表桥接出的 ``v3_*`` 定义）。

因此「新增一条 ``/api/v3/*`` 路由」或「新增一件工作台工具」会自动出现在 ``list_tools`` 里；
删掉一件工具也会自动消失——``platform/tests/test_mcp_discovery.py`` 用「真实删一条路由 →
断言会红」验证这条性质（鉴别力，不是恒真断言）。``DIRECT_KEEP`` 里的 4 个名字也在装配期
逐个对照目录校验：写错一个就 ``AssertionError``，不会静默变成「少给了一件工具」。

约束绝不绕过（与直连面同一条边界）
----------------------------------
* ``call_tool`` 转发的是**注册进 MCPServer 的同一个函数对象**：``readOnlyHint`` 语义、富途
  限流、TTL 缓存、信封序列化、``trading/tool-failed`` 分流全部与直连逐项相同；
* ``v3_credentials`` 的 ``save``/``clear`` 封死在 ``v3_mcp.V3Bridge.__call__`` 内
  （``v3/credentials-web-only``）——代理只是把调用交给它，**封死分支依然先于 endpoint 判定**；
* 交易类工具**本来就不在** ``/api/v3/*`` 面（也不在桥接面）：代理按目录转发，目录里没有
  交易写入器，所以代理**不可能**凭空造出交易能力（测试断言代理面与直连面一一对应）；
* 未知工具名**明确报错**（``mcp/unknown-tool``）并提示用 ``list_tools`` 检索，绝不静默返回空。

卡片里的 ``readOnly`` 口径（**不发明新元数据**）
------------------------------------------------
桥接工具的只读标记现读 ``v3_mcp.NON_READONLY_PATHS``（与 ``/mcp`` 上的 ``readOnlyHint``
同源）；工作台工具的「这是下单/改单/撤单」由 ``store_access.order_operation`` 判定（与交易
闸门同一份实现）。**基础面在 MCP 上本来就没有发布 readOnlyHint**（注册时不带 annotations），
所以本模块也**不替它发明**：这类工具的 ``readOnly`` 如实为 ``null``（未知），不写
``true``/``false`` 冒充已知——卡片只做导航，真实语义始终由被转发的那份实现决定。

只读面 ``/mcp/ro``（服务侧硬边界；2026-09-21 漏洞修复）
--------------------------------------------------------
**漏洞（真机已复现、已有真实后果）**：缺省 discovery 面下，写/交易工具的唯一到达路径是
``call_tool`` 转发；而 Harness 侧白名单钩子按**工具名**匹配（``mcp__quantwb__trade_place``
等 42 项），对 ``mcp__quantwb__call_tool`` 这个入口既不在名单里、也**看不到被转发的内层
名字**——于是 ``call_tool(name="research_tasks_claim")`` 一路到达平台实现。2026-09-21
00:01Z 的一次线上 quant-headless 决策唤醒就是这样**误领了 2 条值班任务**（该 profile 的
42 项白名单明明禁止它）。全局禁止 ``call_tool`` 不可行：官方 ``headless`` profile 的值班链
（``research_duty.sh``）经同一个全局 ``/mcp`` 用 ``research_tasks_claim/report``。

**修法**：新增第二个 MCP 端点 ``/mcp/ro``（同一实现、同一目录，表面强制 discovery 形态），
差异只有一条：**``call_tool`` 只放行注册表 annotations 标注 ``readOnlyHint=True`` 的内层
工具**（判定现读 ``registry_annotations``——注册表即唯一事实源，**不维护第二份名单**；
4 件直连保留件是既有 ``DIRECT_KEEP`` 常量，本就已整体暴露在本面上，一并放行）。被拒调用
返回 ``mcp/denied-by-policy``（isError=false 的业务失败信封），发生在实参校验**之前**、
handler 调用**之前**（测试用 spy 断言零调用）。``list_tools`` 照常检索全目录（「看到」不等于
「能调」），但每张非放行卡片标注 ``roCallable=false`` + 「只读面不可调用」，避免模型反复尝试。
两个决策 profile（quant-headless / quant-sdk）的 ``quant-platform-mcp`` 指到 ``/mcp/ro``；
值班链用的官方 ``headless`` profile 走全局 ``/mcp``，不受影响。
"""
import inspect
import os

from server import mcp_tools, v3_ops, v3_mcp

# ---------------------------------------------------------------------------
# 模式开关：切换只改这一个环境变量
# ---------------------------------------------------------------------------
#: ``QUANT_MCP_SURFACE=discovery|direct``（缺省 discovery —— 规格要求的方向）。
SURFACE_ENV = "QUANT_MCP_SURFACE"
DIRECT = "direct"
DISCOVERY = "discovery"
SURFACES = (DISCOVERY, DIRECT)
DEFAULT_SURFACE = DISCOVERY

#: discovery 模式下**额外**直连保留的高频工具（理由见模块 docstring）。
DIRECT_KEEP = ("snapshot", "admin_status", "v3_gateway", "v3_tools")

#: 两个代理入口（唯一新增的两个工具名；装配时校验不与既有 116 名重合）。
LIST_TOOL = "list_tools"
CALL_TOOL = "call_tool"
PROXY_NAMES = (LIST_TOOL, CALL_TOOL)

#: 检索分页（默认一页 ≤20 张卡片；上限 50 防「换个姿势一次拉全量」）。
DEFAULT_PAGE = 20
MAX_PAGE = 50

#: 卡片里用途一句话的长度上限（超出截断并加省略号——卡片是导航，不是文档）。
PURPOSE_LIMIT = 96

#: 归不到六域的工作台工具在卡片里的域标签。
WORKBENCH_DOMAIN = "workbench"

#: 目录/检索结果的固定域清单（六域 + workbench）。
DOMAINS = tuple(v3_ops.DOMAINS) + (WORKBENCH_DOMAIN,)

# ---------------------------------------------------------------------------
# 只读面 /mcp/ro（漏洞修复：见模块 docstring「只读面」一节）
# ---------------------------------------------------------------------------
#: 第二个 MCP 端点的路径（与 ``/mcp`` 同一实现、同一目录；app.py 用它作 streamable_http_path）。
READONLY_SURFACE_PATH = "/mcp/ro"
#: 只读面 ``call_tool`` 的拒绝码/文案（写类内层工具一律拒绝，先于实参校验与 handler）。
DENIED_BY_POLICY_CODE = "mcp/denied-by-policy"
DENIED_BY_POLICY_MESSAGE = "该工具不是只读，只读面 /mcp/ro 不放行；需要写操作请由人在工作台完成"
#: 只读面 ``list_tools`` 卡片对非放行工具的标注（与拒绝码同一条边界的导航面）。
RO_NOT_CALLABLE = "只读面不可调用"


class SurfaceError(ValueError):
    """``QUANT_MCP_SURFACE`` 取值非法（拼错就报错，绝不静默退回某个模式）。"""


def resolve_surface(value=None):
    """环境变量/显式入参 → 表面模式（大小写与空白宽容，未知取值直接抛错）。"""
    raw = os.environ.get(SURFACE_ENV) if value is None else value
    text = str(raw).strip().lower() if raw not in (None, "") else DEFAULT_SURFACE
    if text not in SURFACES:
        raise SurfaceError(f"{SURFACE_ENV}={raw!r} 非法：只能是 {' | '.join(SURFACES)}")
    return text


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------
def purpose_of(description):
    """描述 → 卡片用的一句话用途：取首句并截断（**不改写**原文措辞）。"""
    text = " ".join(str(description or "").split())
    head, sep, _tail = text.partition("。")
    if sep and head.strip():
        text = head.strip()
    if len(text) > PURPOSE_LIMIT:
        text = text[:PURPOSE_LIMIT].rstrip() + "…"
    return text


def _bridge_readonly(definition):
    """桥接工具是否只读：现读 ``v3_mcp.NON_READONLY_PATHS``（与 ``/mcp`` 标注同源）。"""
    return definition.endpoint not in v3_mcp.NON_READONLY_PATHS


def _base_readonly(definition):
    """工作台工具是否只读：只有「下单/改单/撤单」能被既有注册表判定为写，其余返回 ``None``。

    ``None`` = **未知**（基础面在 MCP 上没有发布 readOnlyHint，不自造一个）。判定用的是
    ``store_access.order_operation``——交易闸门自己的那份映射，不另写一份工具清单。
    """
    from server import store_access
    return False if store_access.order_operation(definition.endpoint) else None


def _prefix_of(name):
    return name.split("_", 1)[0] + "_" if "_" in name else ""


class ToolCard:
    """目录里的一条：名字 + 一句话用途 + 必填/可选参数名 + 域/前缀（**不含完整 schema**）。"""

    __slots__ = ("name", "summary", "source", "domain", "prefix", "readonly",
                 "required", "optional", "endpoint")

    def __init__(self, name, summary, source, domain, prefix, readonly, required,
                 optional, endpoint):
        self.name = name
        self.summary = summary
        self.source = source
        self.domain = domain
        self.prefix = prefix
        self.readonly = readonly
        self.required = tuple(required)
        self.optional = tuple(optional)
        self.endpoint = endpoint

    def as_dict(self):
        """线格式：键名尽量短（卡片要小），必填字段名齐全（能回答「必填什么」）。"""
        return {
            "name": self.name,
            "purpose": self.summary,
            "required": list(self.required),
            "optional": list(self.optional),
            "domain": self.domain,
            "prefix": self.prefix,
            "readOnly": self.readonly,
            "source": self.source,
        }

    def __repr__(self):  # pragma: no cover —— 调试用
        return f"ToolCard({self.name!r}, domain={self.domain!r}, source={self.source!r})"


def _v3_domain(definition):
    """``v3_*`` 工具的域：路由路径的第三段优先（``/api/v3/risk/...`` → risk），否则按目录规则。"""
    parts = [part for part in str(definition.endpoint or "").split("/") if part]
    if len(parts) >= 3 and parts[0] == "api" and parts[2] in v3_ops.DOMAINS:
        return parts[2]
    return v3_ops.domain_of(definition.name)


def build_catalog(base_definitions=None, bridge=None):
    """两个既有注册表 → ``{name: ToolCard}``（本模块**不**持有第二份清单）。"""
    base_definitions = mcp_tools.TOOLS if base_definitions is None else base_definitions
    catalog = {}
    for definition in base_definitions:
        name = definition.name
        catalog[name] = ToolCard(
            name=name,
            summary=purpose_of(definition.description),
            source="workbench",
            domain=v3_ops.domain_of(name),
            prefix=_prefix_of(name),
            readonly=_base_readonly(definition),
            required=tuple(param.name for param in definition.params if param.required),
            optional=tuple(param.name for param in definition.params if not param.required),
            endpoint=definition.endpoint,
        )
    for definition in bridge.definitions:
        name = definition.name
        catalog[name] = ToolCard(
            name=name,
            summary=purpose_of(definition.description),
            source="v3_bridge",
            domain=_v3_domain(definition),
            prefix=_prefix_of(name),
            readonly=_bridge_readonly(definition),
            required=tuple(param.name for param in definition.params if param.required),
            optional=tuple(param.name for param in definition.params if not param.required),
            endpoint=definition.endpoint,
        )
    return catalog


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def match_cards(catalog, domain=None, prefix=None, keyword=None):
    """按域 / 前缀 / 关键词过滤（关键词匹配**名字与用途**，大小写不敏感，多词 AND）。"""
    needles = [part for part in str(keyword or "").lower().split() if part]
    out = []
    for name in sorted(catalog):
        card = catalog[name]
        if domain and card.domain != domain:
            continue
        if prefix and not name.startswith(prefix):
            continue
        haystack = f"{name} {card.summary}".lower()
        if needles and not all(needle in haystack for needle in needles):
            continue
        out.append(card)
    return out


def page_cards(cards, offset=0, limit=DEFAULT_PAGE):
    """分页：返回 ``(当页, offset, limit, has_more, next_offset)``（offset 越界 → 空页）。"""
    offset = max(0, int(offset or 0))
    limit = max(1, min(MAX_PAGE, int(limit or DEFAULT_PAGE)))
    page = cards[offset:offset + limit]
    has_more = offset + limit < len(cards)
    return page, offset, limit, has_more, (offset + limit if has_more else None)


# ---------------------------------------------------------------------------
# 代理
# ---------------------------------------------------------------------------
class DiscoveryProxy:
    """发现代理：目录来自既有注册表，转发交给同一批已注册函数对象。"""

    def __init__(self, base_tools, bridge, base_definitions=None, ro_allowed=None):
        """``base_tools``：``mcp_tools.build_tools`` 的产物（含 ``.fn``）；``bridge``：V3Bridge。

        ``ro_allowed``：只读面 ``/mcp/ro`` 的 call_tool 放行名集（None = 未配置 → **空集**，
        此时代理若被用作只读面会拒绝一切转发——fail-closed；``/mcp`` 面从不读这个集合）。
        """
        self.base_tools = {tool.name: tool for tool in base_tools}
        self.bridge = bridge
        self.base_definitions = tuple(
            mcp_tools.TOOLS if base_definitions is None else base_definitions)
        self.catalog = build_catalog(self.base_definitions, bridge)
        #: discovery 模式下从注册面移出的工具名（``register`` 填；``direct`` 模式为空）。
        self.dropped = ()
        self.ro_allowed = frozenset(ro_allowed or ())
        #: 只读面判据的注册表 annotations 快照（``register_readonly`` 填；裸代理为空）。
        self.ro_annotations = {}
        self._validate_catalog()

    # -- 装配期自检：直连保留名必须真实存在；代理入口名不得撞既有名 ------------------
    def _validate_catalog(self):
        missing = [name for name in DIRECT_KEEP if name not in self.catalog]
        if missing:
            raise AssertionError(f"DIRECT_KEEP 里有注册表里不存在的工具：{missing}")
        clash = [name for name in PROXY_NAMES if name in self.catalog]
        if clash:
            raise AssertionError(f"代理入口名与既有工具重名：{clash}")

    def direct_names(self):
        """discovery 模式下仍然直连的名字（顺序 = ``DIRECT_KEEP``，稳定可比）。"""
        return tuple(DIRECT_KEEP)

    def proxy_names(self):
        return PROXY_NAMES

    def surface_names(self):
        """discovery 模式的完整暴露集合（直连保留 4 件 + 2 个代理入口）。"""
        return tuple(DIRECT_KEEP) + PROXY_NAMES

    # -- 目录 ------------------------------------------------------------------
    def empty_hint(self, domain=None, prefix=None, keyword=None):
        """空结果时的可执行提示：告诉模型「下一步怎么查」（不是一句「无结果」）。"""
        if keyword or prefix:
            return (f"没有匹配 domain={domain!r} prefix={prefix!r} keyword={keyword!r} 的工具；"
                    f"可用 domain 取 {'/'.join(DOMAINS)}，"
                    f"或不带参数调用 list_tools 看全部 {len(self.catalog)} 件")
        if domain:
            return (f"域 {domain!r} 里没有工具；可用域：{'/'.join(DOMAINS)}")
        return f"工具目录为空（目录来自注册表，共 {len(self.catalog)} 件）"

    def search(self, domain=None, prefix=None, keyword=None, limit=DEFAULT_PAGE, offset=0,
               ro=False):
        """``list_tools`` 的载荷构造（纯函数，便于测试直接断言）。

        ``ro=True``（只读面 ``/mcp/ro``）时目录**照常全量检索**（「看到」不等于「能调」），
        但每张卡片追加 ``roCallable``（本面 ``call_tool`` 是否放行，与 ``readonly_denial``
        同一个集合判定），非放行卡片再标注 ``roNote``（「只读面不可调用」）——避免模型对着
        目录反复尝试注定被拒的调用。
        """
        if domain and domain not in DOMAINS:
            return mcp_tools.failure(
                "mcp/unknown-domain",
                f"domain={domain!r} 不是可用域；可用：{'/'.join(DOMAINS)}")
        matched = match_cards(self.catalog, domain=domain, prefix=prefix, keyword=keyword)
        cards, offset, limit, has_more, next_offset = page_cards(matched, offset, limit)
        payload = {
            "ok": True,
            "total": len(matched),
            "count": len(cards),
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "query": {"domain": domain, "prefix": prefix, "keyword": keyword},
            "domains": list(DOMAINS),
            "catalog_total": len(self.catalog),
            "cards": [card.as_dict() for card in cards],
            "note": ("卡片是导航信息（名字 + 一句话用途 + 必填/可选参数名），**不含完整 schema**；"
                     "确认要调用哪个后用 call_tool(name, arguments) 转发，"
                     "返回与直连工具逐字段一致的信封"),
            "source": ("目录 = mcp_tools.TOOLS + v3_mcp.V3Bridge.definitions"
                       "（两个既有注册表，无第二份清单）"),
        }
        if ro:
            payload["surface"] = {"path": READONLY_SURFACE_PATH, "readOnlyOnly": True}
            payload["note"] += ("；本面是只读面 /mcp/ro：call_tool 只放行 roCallable=true 的工具"
                               "（注册表 readOnlyHint 标注 + 直连保留件），其余一律 "
                               "mcp/denied-by-policy")
            for card in payload["cards"]:
                allowed = card["name"] in self.ro_allowed
                card["roCallable"] = allowed
                if not allowed:
                    card["roNote"] = (f"{RO_NOT_CALLABLE}：{DENIED_BY_POLICY_MESSAGE}"
                                      "（卡片字段 roCallable=false 即注定被拒，不要尝试）")
        if next_offset is not None:
            payload["next_offset"] = next_offset
        if not cards:
            payload["hint"] = self.empty_hint(domain, prefix, keyword)
        return payload

    # -- 转发 ------------------------------------------------------------------
    def readonly_denial(self, name):
        """只读面 ``call_tool`` 的闸门：非放行内层工具 → ``mcp/denied-by-policy`` 信封，否则 ``None``。

        判据是 ``ro_allowed``（注册表 annotations 的 ``readOnlyHint=True`` ∪ ``DIRECT_KEEP``，
        见 ``readonly_allow_set``），**先于**实参校验与 handler 调用（测试用 spy 断言零调用）。
        未知工具名在这里放行到 ``call``，由它回 ``mcp/unknown-tool``（错误语义归那一层）。
        """
        if name in self.ro_allowed or self.catalog.get(name) is None:
            return None
        return mcp_tools.failure(
            DENIED_BY_POLICY_CODE,
            f"{DENIED_BY_POLICY_MESSAGE}（call_tool 名 {name!r} 的注册表 annotations 没有 "
            "readOnlyHint=true；只读面用 list_tools 卡片的 roCallable 字段区分可转发/不可转发）")

    def unknown_tool(self, name):
        """未知工具名 → 明确错误（含「可用 list_tools 检索」提示，绝不静默返回空）。"""
        return mcp_tools.failure(
            "mcp/unknown-tool",
            f"没有名为 {name!r} 的工具（当前工具面 {len(self.catalog)} 件）。"
            f"请先用 list_tools 检索（keyword 匹配名字与用途，也可用 domain/prefix 收窄），"
            f"再用 call_tool 调用；不要凭猜测构造名字。")

    def bad_arguments(self, name, extra, missing):
        """实参名/必填集不合法 → 明确错误（与直连的 schema 层拒绝同一后果，措辞可执行）。"""
        parts = []
        if missing:
            parts.append(f"缺必填参数 {sorted(missing)}")
        if extra:
            parts.append(f"不认识参数 {sorted(extra)}")
        return mcp_tools.failure(
            "mcp/bad-arguments",
            f"call_tool('{name}') 的实参不合法：{'；'.join(parts)}。"
            f"参数名与必填集用 list_tools(keyword='{name}') 查（卡片 required 即必填名）。")

    def target(self, name):
        """名字 → 已注册的可调用函数对象（``async``）或 ``None``（未知工具）。

        桥接面取 ``v3_mcp.register`` 装配时缓存进 ``bridge.bound`` 的**同一个函数对象**；
        缓存缺失（调用方手工装配）时才现造一次并写回，保证同一进程内后续取到的仍是同一个。
        """
        card = self.catalog.get(name)
        if card is None:
            return None
        if card.source == "v3_bridge":
            bound = self.bridge.bound.get(name)
            if bound is not None:
                return bound
            for definition in self.bridge.definitions:
                if definition.name == name:
                    bound = v3_mcp._bind(definition, self.bridge)  # noqa: SLF001
                    self.bridge.bound[name] = bound
                    return bound
            return None  # pragma: no cover —— 目录与定义同源，走不到
        tool = self.base_tools.get(name)
        return None if tool is None else tool.fn

    async def call(self, name, arguments=None):
        """把实参转发给**同一实现**，返回 ``(CallToolResult, card, matched)``。

        ``matched`` 是实际转发的目标函数对象（测试用它断言「同一份实现」）；未知工具/坏实参
        时是 ``None``。结果对象的 ``isError``/信封与直连逐字段一致（转发的是同一个函数）。
        """
        arguments = dict(arguments or {})
        if name not in self.catalog:
            return mcp_tools.tool_result(self.unknown_tool(name), is_error=False), None, None
        card = self.catalog[name]
        allowed = set(card.required) | set(card.optional)
        extra = set(arguments) - allowed
        missing = {field for field in card.required if field not in arguments}
        if extra or missing:
            envelope = self.bad_arguments(name, extra, missing)
            return mcp_tools.tool_result(envelope, is_error=False), card, None
        target = self.target(name)
        if target is None:  # pragma: no cover —— 目录与注册表同源，走不到
            return mcp_tools.tool_result(self.unknown_tool(name), is_error=False), None, None
        filled = {field: mcp_tools.UNSET for field in card.optional}
        filled.update(arguments)
        result = target(**filled)
        if hasattr(result, "__await__"):
            result = await result
        return result, card, target


# ---------------------------------------------------------------------------
# MCP 入口
# ---------------------------------------------------------------------------
def list_tools_signature():
    """``list_tools`` 的输入签名（全部可选；无必填 → 空参调用即看全部目录）。"""
    return [
        mcp_tools.opt("keyword", "str", "关键词：匹配工具名与一句话用途（空格分隔多词为 AND）"),
        mcp_tools.opt("domain", "str", f"只看一域：{'/'.join(DOMAINS)}"),
        mcp_tools.opt("prefix", "str", "只看某前缀，如 v3_ / admin_ / trade_"),
        mcp_tools.opt("limit", "int", f"本页最多返回几张卡片（1..{MAX_PAGE}，缺省 {DEFAULT_PAGE}）",
                      minimum=1, maximum=MAX_PAGE),
        mcp_tools.opt("offset", "int", "分页起点（缺省 0；has_more=true 时用 next_offset）",
                      minimum=0),
    ]


def call_tool_signature():
    """``call_tool`` 的输入签名：``name`` 必填、``arguments`` 是**自由对象**。

    ``arguments`` 刻意用 ``object``（自由 JSON 对象）而不是展开成固定字段：转发面的字段集
    随目标工具变化，展开就等于把「上百个 schema」又搬回来一次——那正是本模块要解决的问题。
    字段校验因此落在代理里（未知字段/缺必填 → ``mcp/bad-arguments``），与直连的 schema 层
    拒绝同一后果、措辞更可执行。
    """
    return [
        mcp_tools.req("name", "str", "要调用的工具名（先用 list_tools 检索确认；必须精确匹配）"),
        mcp_tools.opt("arguments", "object", "该工具的实参对象（键名见 list_tools 卡片的 "
                                            "required/optional 字段）"),
    ]


def list_tools_binding(proxy, ro=False):
    """``list_tools`` 的注册函数（``__signature__`` 表达字段集，与直连面同一手法）。

    ``ro=True``：只读面的检索入口——同一份目录，卡片带 ``roCallable``/``roNote`` 标注。
    """
    def fn(**kwargs):
        provided = {key: value for key, value in kwargs.items()
                    if value is not mcp_tools.UNSET}
        payload = proxy.search(
            domain=provided.get("domain"), prefix=provided.get("prefix"),
            keyword=provided.get("keyword"),
            limit=provided.get("limit") or DEFAULT_PAGE,
            offset=provided.get("offset") or 0, ro=ro)
        return mcp_tools.tool_result(payload, is_error=False)

    fn.__name__ = LIST_TOOL
    if ro:
        fn.__doc__ = ("检索平台工具面（只读面 /mcp/ro 版）：按关键词/域/前缀过滤，返回精简卡片"
                      "（名字 + 一句话用途 + 必填参数名，不含完整 schema），支持分页。"
                      "每张卡片带 roCallable：false 的卡片标注「只读面不可调用」——本面 call_tool "
                      "只放行只读工具，需要写操作请由人在工作台完成。")
    else:
        fn.__doc__ = ("检索平台工具面：按关键词/域/前缀过滤，返回精简卡片"
                      "（名字 + 一句话用途 + 必填参数名，不含完整 schema），支持分页。"
                      f"缺省一页 {DEFAULT_PAGE} 张；确认工具名后用 call_tool 调用。")
    fn.__signature__ = inspect.Signature(
        [param.parameter() for param in list_tools_signature()])
    return fn


def call_tool_binding(proxy, ro=False):
    """``call_tool`` 的注册函数（转发给同一实现，返回同一信封）。

    ``ro=True``：只读面的转发入口——转发**之前**先过 ``proxy.readonly_denial`` 闸门
    （注册表 annotations 判定），非只读内层工具回 ``mcp/denied-by-policy``，绝不触达实现。
    """
    async def fn(**kwargs):
        provided = {key: value for key, value in kwargs.items()
                    if value is not mcp_tools.UNSET}
        name = provided.get("name")
        if ro:
            denial = proxy.readonly_denial(name)
            if denial is not None:
                return mcp_tools.tool_result(denial, is_error=False)
        result, _card, _target = await proxy.call(name, provided.get("arguments"))
        return result

    fn.__name__ = CALL_TOOL
    if ro:
        fn.__doc__ = ("转发调用平台工具面里的**只读**工具（与直连工具**同一份实现**、同一信封）："
                      "name 必须精确匹配（先用 list_tools 检索，卡片 roCallable=true 才可调用），"
                      "arguments 是实参对象。非只读工具一律 mcp/denied-by-policy（只读面 /mcp/ro "
                      "不放行；需要写操作请由人在工作台完成）；未知工具名返回 mcp/unknown-tool。")
    else:
        fn.__doc__ = ("转发调用平台工具面里的任意工具（与直连工具**同一份实现**、同一信封）："
                      "name 必须精确匹配（先用 list_tools 检索），arguments 是实参对象。"
                      "业务失败仍是正常工具结果（isError=false）；未知工具名返回 "
                      "mcp/unknown-tool 并提示检索。")
    fn.__signature__ = inspect.Signature(
        [param.parameter() for param in call_tool_signature()])
    return fn


def register(server, base_tools, bridge):
    """discovery 模式的注册：**改注册面**而不是改能力。

    装配时既有的 116 件已经注册进 ``server``（``mcp_tools.register`` / ``v3_mcp.register``），
    这里做两件事：

    1. 把**不在保留集**里的工具从 ``ToolManager`` 移出（``tools/list`` 因此只剩 6 件）；
    2. 注册两个代理入口。

    移出只影响 ``tools/list`` 的**表面**：转发目标仍是同一批绑定对象（基础面 ``BoundTool.fn``、
    桥接面 ``bridge.bound[name]``），所以 ``call_tool`` 能到达的集合与 ``direct`` 模式一一对应。
    ``direct`` 模式不走这个函数，注册面一字未动（向后兼容）。
    """
    from mcp.types import ToolAnnotations

    proxy = DiscoveryProxy(base_tools, bridge)
    base_by_name = {tool.name: tool for tool in base_tools}
    # 桥接面**同一份绑定对象**（v3_mcp.register 装配时缓存）；discovery 模式下若某件没被注册
    # （例如调用方手工装配），这里现造一次并**写回缓存**，保证两种模式拿到的是同一个函数对象。
    bridge_by_name = bridge.bound
    list_fn, call_fn = list_tools_binding(proxy), call_tool_binding(proxy)
    manager = server._tool_manager  # noqa: SLF001 —— SDK 无公开的「列出全部」口子
    proud = set(DIRECT_KEEP) | set(PROXY_NAMES)
    dropped = []
    for info in manager.list_tools():
        name = getattr(info, "name", "")
        if name in proud:
            continue
        server.remove_tool(name)  # 只移出注册面；定义与绑定对象都还在（代理照用）
        dropped.append(name)
    with mcp_tools.schema_warning_filter():
        for name in DIRECT_KEEP:
            tool = base_by_name.get(name)
            if tool is not None:
                continue  # 基础面保留件仍在注册面里（未被动过）
            if name in bridge_by_name:
                continue  # v3_* 保留件由 v3_mcp.register 注册（标注逐字段同一口径），不重复注册
            raise AssertionError(f"DIRECT_KEEP 的 {name} 不在任何注册表里")  # pragma: no cover
        server.add_tool(list_fn, name=LIST_TOOL, description=list_fn.__doc__,
                        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                    idempotentHint=True, openWorldHint=False),
                        structured_output=False)
        server.add_tool(call_fn, name=CALL_TOOL, description=call_fn.__doc__,
                        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                                    idempotentHint=False, openWorldHint=True),
                        structured_output=False)
        mcp_tools.forbid_extra_fields(server, PROXY_NAMES + DIRECT_KEEP)
    proxy.dropped = tuple(dropped)
    return proxy


def registry_annotations(server):
    """``MCPServer`` 注册表现读 ``{name: ToolAnnotations|None}``（只读面判据的唯一事实源）。

    读的是 ToolManager 里每件 ``Tool.annotations``——即**注册时真正传进去的**标注
    （桥接面 ``v3_mcp.register`` 按是否写类给 ``readOnlyHint``；基础面注册时不带，
    如实为 ``None``）。不在这里重推、不维护第二份名单。
    """
    return {tool.name: tool.annotations for tool in server._tool_manager.list_tools()}  # noqa: SLF001


def readonly_allow_set(annotations):
    """注册表 annotations 快照 → 只读面 ``call_tool`` 的放行名集（frozenset）。

    * ``readOnlyHint is True`` 的名字放行——判据就是注册表标注本身；
    * ``DIRECT_KEEP`` 四件一并放行：它们是 discovery 面自己的保留常量（模块 docstring：
      「四件都是只读、无副作用」），在只读面上**本来就整体直连暴露**——拒绝它们的
      ``call_tool`` 路径保护不了任何东西，只会制造「直连能调、转发被拒」的假边界；
      基础面不发布 annotations，所以不能靠标注识别它们。
    * 其余（含全部基础面写/非写工具与桥接写类）一律不在集合里 → ``mcp/denied-by-policy``。
      基础面只读工具（``series`` 等）因**没有只读标注**也被拒——这是 fail-closed 的代价，
      取数走 ``v3_*`` 只读桥接件（``v3_market``/``v3_news``/…），README 有说明。
    """
    allowed = {name for name, ann in annotations.items()
               if ann is not None and ann.read_only_hint is True}
    allowed.update(DIRECT_KEEP)
    return frozenset(allowed)


def register_readonly(server, base_tools, bridge, annotations):
    """只读面 ``/mcp/ro`` 的注册：discovery 形态 + ``call_tool`` 只放行只读内层工具。

    ``server`` 是**第二个** ``MCPServer``（与 ``/mcp`` 的实例互不相干，注册面从零开始，
    因此不存在「移出」——只登记 6 件：4 件直连保留 + 2 个代理入口）。直连保留件复用
    **同一个函数对象与同一份 annotations**（``bridge.bound`` / ``base_tools``，标注取
    ``annotations`` 快照），代理入口的 ``call_tool`` 带 ``readonly_denial`` 闸门。
    返回 ``DiscoveryProxy``（``ro_allowed`` 已按 ``readonly_allow_set`` 配好）。
    """
    from mcp.types import ToolAnnotations

    proxy = DiscoveryProxy(base_tools, bridge, ro_allowed=readonly_allow_set(annotations))
    proxy.ro_annotations = dict(annotations)  # 快照留档（测试核对推导用；快照先于 discovery 移出）
    base_by_name = {tool.name: tool for tool in base_tools}
    list_fn = list_tools_binding(proxy, ro=True)
    call_fn = call_tool_binding(proxy, ro=True)
    with mcp_tools.schema_warning_filter():
        for name in DIRECT_KEEP:
            if name in bridge.bound:
                bound = bridge.bound[name]  # v3_* 保留件：装配时缓存的同一函数对象
            elif name in base_by_name:
                bound = base_by_name[name].fn  # 基础面保留件：注册面同一个函数对象
            else:
                raise AssertionError(f"DIRECT_KEEP 的 {name} 不在任何注册表里")  # pragma: no cover
            server.add_tool(bound, name=name, description=bound.__doc__,
                            annotations=annotations.get(name), structured_output=False)
        server.add_tool(list_fn, name=LIST_TOOL, description=list_fn.__doc__,
                        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                    idempotentHint=True, openWorldHint=False),
                        structured_output=False)
        # 只读面的 call_tool 本身**只会**转发只读工具（闸门在绑定里）——因此这里如实标
        # readOnlyHint=True（与 /mcp 面的 call_tool=False 相反，语义各自诚实）。
        server.add_tool(call_fn, name=CALL_TOOL, description=call_fn.__doc__,
                        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                    idempotentHint=False, openWorldHint=True),
                        structured_output=False)
        mcp_tools.forbid_extra_fields(server, PROXY_NAMES + DIRECT_KEEP)
    proxy.dropped = ()  # 只读面从一开始就只注册 6 件，没有「移出」动作
    return proxy


def surface_names(server):
    """当前 ``MCPServer`` 上真实注册的工具名（装配后自检/报告用）。"""
    import asyncio
    return [tool.name for tool in asyncio.run(server.list_tools())]


__all__ = [
    "CALL_TOOL", "DENIED_BY_POLICY_CODE", "DENIED_BY_POLICY_MESSAGE", "DEFAULT_PAGE",
    "DEFAULT_SURFACE", "DIRECT", "DIRECT_KEEP", "DISCOVERY", "DOMAINS", "DiscoveryProxy",
    "LIST_TOOL", "MAX_PAGE", "PROXY_NAMES", "READONLY_SURFACE_PATH", "RO_NOT_CALLABLE",
    "SURFACE_ENV", "SURFACES", "SurfaceError", "ToolCard", "WORKBENCH_DOMAIN", "build_catalog",
    "call_tool_binding", "call_tool_signature", "list_tools_binding", "list_tools_signature",
    "match_cards", "page_cards", "purpose_of", "readonly_allow_set", "register",
    "register_readonly", "registry_annotations", "resolve_surface", "surface_names",
]
