"""V3.0 运维 / 通道 / 治理类接口（``/api/v3/{metrics,gateway,tools,settings,execution,
oms/orders,oms/sync,events,audit,brain}``）。

装配方式（app.py 自动接线，本模块不改 app.py）::

    def register(app, v3_run, home): ...

``v3_run(name, payload)`` 是主 agent 注入的回调：按名调用既有 77 工具面的**同一 handle**
（与 HTTP ``/api/wb/*``、MCP ``/mcp`` 同源），返回原始信封 ``{ok, value|error}``。

数据诚实性（逐条，都是本实现的硬约束）
--------------------------------------
* **工具面**：本模块不复制任何业务逻辑，也不硬编码工具名——工具清单来自
  ``server.mcp_tools.TOOLS`` 的导入枚举，域归类复用 V3 原型（已退役）的工具域归类
  的 ``domainOf`` 规则。
* **工具数两个口径（2026-09-20 修正）**：六域工具目录（``catalog_total`` = 工作台 77 +
  5 个本地计算）与 **MCP 工具面**（``tools/list`` 的真值，另有 app.py 桥接的 ``v3_*``
  工具）**不是同一个数**，历史上两处都只报目录数、字段名却叫 ``tools``，读起来就是错的。
  现在 ``/api/v3/gateway`` 报 ``tools_total``（MCP 真值，动态取自 ``app.state.mcp`` 的注册表，
  另附 ``tools_domain_catalog`` / ``tools_bridge`` / ``tools_source``），``/metrics`` 用
  ``quantwb_tools{scope="mcp"|"domain"}`` 分口径暴露——**两边都不写死常量**。
* **数据源可用性（2026-09-20 修正）**：判据是**能力探测 / 最近一次真实调用的 source**，
  不是「某个 Python 包能否 import」（SEC EDGAR 走 HTTPS，不需要包）。
  探测的真值放在 ``source``/``as_of``/``detail`` 三个字段里；失败如实写原因，绝不写死
  「不可用」。
* **通道（2026-09-20 修）**：两条通道**都已实现**——SDK JSON-RPC 在 ``server/v3_sdk.py``、
  Headless CLI 子进程（``dsh --profile headless``）在 ``server/v3_headless.py``（真机跑通，
  调用日志落 ``v3_db.headless_log``）。本模块报的是**本进程的接线状态**，不再写「不可用/
  未实现」这类失效断言：
  * ``headless``：``today`` 读 ``headless_log`` 真实当日记录、``last`` 读最近 10 条、
    ``breaker`` 读 ``v3_headless`` 的熔断参数、``schedulerAlive``/``nextFireTimes`` 读
    进程内调度器；模块未注册 → ``status="implemented-not-registered"`` + 原因（见
    :func:`_headless_status`）。**不创建运行时、不起进程、不 spawn**。
  * ``sdk``：本模块**不直连** SDK 运行时（那会创建会话对象）——``server.v3_sdk`` 自己的
    ``/api/v3/sdk/*`` 是它的读数口；``turns``/``events`` 在 ``/api/v3/brain`` 保持空数组
    并给出原因，绝不用编造的会话/成功率填充。* **执行入口**：本模块**没有任何下单/改单/撤单方法**。执行入口只有一个——工作台 Web 的
  ``plan_execute`` + 人工确认（live 需口令）。V3 只登记、风控分级、对账与展示。
* **NAV 口径安全优先**：``equity.current``（本地模拟台账，单币种）→ 仅当 ``positions``
  只有**单一账户且单一币种**时用其持仓市值合计 → 多账户/多币种**不折算**（NAV=0，
  风控保守退回人工确认，严禁偏宽松）。
* **``/api/v3/audit`` 的 ``window``**：审计链工具面无 ``window`` 字段（app.py 的
  ``_takes_no_payload`` 会拒绝任何非空载荷），故该查询参数在确认工具面不接受时**不下传**，
  响应仍是审计工具的真实信封。
* **进程内计数**：模块级计数器在**每次 v3_run 调用**时累加。``mcp`` 与 ``wb`` 两个视图读
  同一份计数（两处本来就是同一个 handle 的同一批调用，不做第二事实源）；``http`` 只统计
  本模块注册的路由（进程内计数，不引 Prometheus）。``/api/v3/metrics`` 另加 ``futu``
  一块（``server/v3_ratelimit.metrics_view()``：calls/coalesced/retries/rateLimited/
  throttleWaitMs/cooldownUntil/inFlight/queued）——**真实计数**，是本进程富途限流治理的
  唯一读数口。
* **市场过滤（2026-09-20）**：``execution`` / ``oms/orders`` / ``brain``（以及 ``app.py`` 的
  ``overview``）支持 ``?market=SH|HK|US``——只挑分组/台账行，**既有字段一字不改**，
  另加 ``market`` 与 ``filter`` 真实计数；未登记的市场标识（``market_id`` 9/10/…）
  如实排除并计数，绝不猜成某个市场（映射口径见 ``server/v3_universe.py``）。
  不传 ``market`` 时行为与历史完全一致。

落盘（均在 ``home`` 下；自 2026-09-20 起**主存 = ``server.v3_db`` 的 SQLite 表**，
原文件保留为冷备/兼容镜像；库不可用时读路径回退文件，写失败不阻断业务）
--------------------------------------------------------------------
* ``<home>/v3.db``                  —— **主存**：``oms_orders`` / ``oms_sync`` /
  ``strategy_runs``（只读）等表；路径可用 ``QUANT_V3_DB`` 覆盖
* ``<home>/v3-oms-orders.json``     —— OMS 台账冷备（订单实体 + 风控分级 + 状态历史，原子写）
* ``<home>/v3-oms-sync.jsonl``      —— 每次对账的追加留痕（冷备；主存 ``oms_sync`` 表）
* ``<home>/v3-strategy-runs.jsonl`` —— **只读冷备**：``/api/v3/brain`` 的最近一轮策略
  （由 v3_analytics 侧写入；本模块优先读库，库空才读这个文件）

测试：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_ops -v``
"""
import asyncio
import copy
import importlib.util
import json
import os
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from server import mcp_tools, store_access, v3_db, v3_ratelimit, v3_risk_gate, v3_universe
from server.config import config_path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 六大工具域（与 V3 原型（已退役）的 DOMAINS 逐字一致）
DOMAINS = ("data", "alpha", "ml", "risk", "execution", "ecosystem")

#: 交易写类：一律归 execution（catalog.mjs 的 WRITE_TOOLS）
WRITE_TOOLS = frozenset({"trade_place", "trade_modify", "trade_cancel", "plan_execute",
                         "switch_mode"})

#: V3.0 新增的**本地计算**工具（不是工作台直通；catalog.mjs 的 LOCAL 面）
V3_LOCAL_TOOLS = (
    ("run_backtest", "ml",
     "V3 新增本地计算：单标的动量 long/flat 回测（真实富途日 K，PIT 对齐，轻量引擎）"),
    ("param_sweep", "ml",
     "V3 新增本地计算：动量窗口 × 调仓周期参数扫描网格（热力图数据）"),
    ("strategy_run", "ecosystem",
     "V3 新增本地计算：PDAT→PAAT→PCPT→PRT→PET 研究流水线（只出调仓提案，不下单）"),
    ("calc_var", "risk",
     "V3 新增本地计算：历史模拟法 VaR/CVaR、Beta/Alpha、IR、最大回撤与 Kupiec POF 检验"),
    ("search_news", "data",
     "V3 新增本地计算：A 股个股新闻（AKShare 公开端点，免密钥）"),
)

#: 风控阈值（与 V3 原型（已退役）实现 的 DEFAULT_LIMITS 逐项一致）
LIMITS = {"singlePct": 2.0, "industryPct": 20.0, "drawdownPct": 15.0}

#: OMS 生命周期阶段（与 V3 原型（已退役）实现 的 KANBAN 六态一致）+ 行业红线单列一态
STAGES = ("risk_passed", "manual", "blocked", "blocked_industry", "submitted", "filled",
          "rejected")

#: action → 触发规则名（审计留痕用；写 ``history[].rule`` 与 ``risk.rule``，既有字段不变）
_RULE_OF_ACTION = {"auto": "within-limits", "manual": "single-order",
                   "blocked": "drawdown-red-line", "blocked_industry": "industry-red-line"}

#: 设置页环境变量清单（任务书 §4）：只报「是否注入 + 来源」，**绝不出值**
#: （2026-09-21 数据源政策：TUSHARE_TOKEN 随 Tushare Pro 一并移除——需要 token 的
#: 数据渠道不再引用，能力由免密源覆盖）。
ENV_KEYS = ("DSH_HOME", "DEEPSEEK_API_KEY", "QUANT_MCP_NODE", "QUANT_MCP_SERVER",
            "QUANT_MCP_CWD", "QUANT_MCP_LOG", "FUTU_OPEND_HOST", "FUTU_OPEND_PORT")

OMS_FILENAME = "v3-oms-orders.json"
OMS_SYNC_FILENAME = "v3-oms-sync.jsonl"
STRATEGY_RUNS_FILENAME = "v3-strategy-runs.jsonl"
CREDENTIAL_FILENAME = "futu-openapi.json"
FUTU_TOKEN_FILENAME = "futu-token"
FUTU_TOKEN_EXPIRY_FILENAME = "futu-token-expiry"
FUTU_CHANNELS = ("openapi", "mcp")

#: SDK 通道的**准确**措辞（2026-09-20 修）。旧文案「本服务未挂载 SDK JSON-RPC 通道」同样是
#: 失效断言：``server/v3_sdk.py``（FR-GATEWAY-002 的 stdio 换行分帧 JSON-RPC 客户端）**已实现并
#: 装配**，它自己的 ``/api/v3/sdk/*`` 才是它的读数口。本模块**刻意不直连** SDK 运行时
#: （``get_runtime()`` 会创建 ``SdkRuntime`` 对象并 arm atexit —— 只读视图不该在抓取路径上
#: 造对象），因此这里报的是「本视图没有 SDK 会话事实来源」，而不是「没有这条通道」。
SDK_REASON = ("SDK JSON-RPC 通道由 server/v3_sdk.py 提供（/api/v3/sdk/status 是它的读数口）；"
              "本模块刻意不直连 SDK 运行时（get_runtime() 会创建运行时对象），"
              "故此处不报会话/回合读数。")
#: Headless 通道的**准确**措辞（2026-09-20 修）。旧文案是「本服务未挂载 Headless CLI
#: 子通道（无 dsh --profile headless 子进程调度）」——**已失效**：``server/v3_headless.py``
#: 已实现完整的 Headless Runner（真实 spawn ``dsh --profile headless``、白名单/熔断/调用日志
#: 落 ``v3_db.headless_log``）并在本机真机跑通。因此这里改成对**接线状态**的诚实描述：
#: 通道已实现、是否被平台调度线程保活由 ``headless.schedulerAlive`` 如实反映（见
#: :func:`_headless_status`）。取数路径只读 ``v3_headless`` 的公开只读函数 + 进程内注册表，
#: 不 import 任何写路径、不起进程。
HEADLESS_REASON = ("Headless CLI 子进程通道已实现（server/v3_headless.py：真实 spawn "
                   "dsh --profile headless + 白名单 + 熔断 + 调用日志落 v3_db.headless_log）；"
                   "该模块未注册进本进程（装配未接线）→ 调度状态/下次触发时间不可读。"
                   "headless.today/last 仍直接读 v3_db.headless_log 的真实记录。")
HEADLESS_COMMAND = 'dsh --profile headless "<task>"'
MCP_PROTOCOL = ("MCP streamable-http（/mcp，SDK 2.2.0 的 streamable_http_app）；"
                "stdio 由 dsh 侧按需拉起")
SDK_PROTOCOL = "换行分帧 JSON-RPC / stdio（未挂载）"

MODE_NOTE = ("模式切换（sim/live）沿用既有工作台 Web 闸门：switch_mode 只接受切到 sim；"
             "sim→live 必须由用户在 Web（默认 http://127.0.0.1:8397）输入口令完成；"
             "V3.0 不另开口子。")
OMS_NOTE = ("执行入口只有一个：既有工作台 Web 的「执行已冻结计划」（plan_execute）+ 人工确认"
            "（live 需口令「确认执行」）。V3 只登记、风控分级、对账与展示，不提供任何下单通道。")
#: 「没有新鲜行业读数」时 ``industry_source`` 的取值。**只用 ``no-data`` 这一个词**：
#: 既有的长句子已删除（它写着「按 0% 不阻断」，而当前实现是「无读数 → fail-open，且必留痕」，
#: 语义不同，留着会误导审计）。真实读数的来源由 ``v3_risk_gate.industry_context`` 给出
#: （``cache/futu/info_owner_plate`` / ``fetch/...`` / ``no-data``）。
INDUSTRY_SOURCE = "no-data"

#: 行业闸门上线**之前**落盘的 ``industry_source`` 原文（``HEAD`` 版 v3_ops.py 的
#: ``INDUSTRY_SOURCE`` 常量值，2026-09-19 的 10 笔存量订单就是这个字符串）。
#: 这句话**现在是失效断言**：它写着「工具面无行业分类数据源」，而 2026-09-20 起
#: ``v3_risk_gate.industry_context`` 已能给出真实行业映射（``cache/futu/info_owner_plate``，
#: 实测 SH 37.5% / HK 40% / US 50%）。它同时是识别「闸门前的历史判定」的第一判据。
LEGACY_INDUSTRY_SOURCE_TEXT = "工具面无行业分类数据源"

#: 历史判定在**视图层**的如实文案（替换上面那句已失效的断言；磁盘上的原文一个字都不动）。
LEGACY_INDUSTRY_SOURCE = "历史判定（该单登记于行业闸门上线前，当时无行业读数）"
#: 历史判定的说明：为什么 ``industry_pct`` 是 ``null`` 而不是 ``0.0``。
LEGACY_INDUSTRY_NOTE = ("该判定未包含行业红线：登记时行业闸门尚未接入，台账没有行业读数——"
                        "这里的「—」是「当时没读到」，不是「行业暴露 0%」。"
                        "原始 risk.reasons / history 原样保留，本视图不回写、不重判。")


def legacy_pre_gate(record):
    """该台账订单是否为**行业闸门上线前**落盘的历史判定（只读判据，可解释、不猜时间戳）。

    判据（任一命中即认定「历史判定」）::

        1. ``industry_source`` 含 :data:`LEGACY_INDUSTRY_SOURCE_TEXT`——当前实现
           （``v3_risk_gate``）只会写 ``cache/futu/...`` / ``fetch/...`` / ``no-data``，
           这句话已不可能由现在的代码产生；
        2. 记录**有分级留痕**（``risk.action`` 或非空 ``history``），但整条记录没有任何
           闸门标记（``risk.rule`` / ``history[].rule`` / ``history[].industry_*``）。

    判据 2 的根据是版本库事实而不是猜测：``risk.rule`` 与 ``history[].rule`` /
    ``history[].industry_*`` 与行业闸门是**同一次改动**引入的
    （``git show HEAD:platform/server/v3_ops.py`` 里 ``"risk": {"action", "reasons"}``、
    ``history.append({"at", "stage", "reasons"})``，都没有 rule 字段）。

    为什么不用「落盘时间早于某个常量」：闸门没有落盘的生效时间标记，硬编码一个时刻等于猜。
    连分级留痕都没有的记录（入库的迁移数据）**不算**历史判定——那时无从谈「含不含行业红线」，
    本函数返回 ``False``，视图不对它下任何结论。
    """
    if not isinstance(record, dict):
        return False
    if LEGACY_INDUSTRY_SOURCE_TEXT in str(record.get("industry_source") or ""):
        return True
    risk = record.get("risk") if isinstance(record.get("risk"), dict) else {}
    history = [entry for entry in (record.get("history") or []) if isinstance(entry, dict)]
    if not risk.get("action") and not history:
        return False  # 没有分级留痕 → 不是「闸门前的判定」，无可归一化
    if "rule" in risk:
        return False
    if any("rule" in entry or any(str(key).startswith("industry_") for key in entry)
           for entry in history):
        return False
    return True


def order_view(record):
    """台账订单 → **返回视图**（视图层归一化，绝不回写磁盘/库）。

    闸门后的新订单**原样返回**，只**追加**两个自解释标记（不改任何既有字段的值）：
    ``industry_graded`` / ``legacy_pre_gate``。闸门前的历史判定额外做三处改写：

    * ``industry_pct`` → ``None``（**不是 0.0**：当时没有行业读数，0.0 会被读成
      「该单行业暴露 0%」，与事实相反）；
    * ``industry_source`` → :data:`LEGACY_INDUSTRY_SOURCE`（如实说明这是历史判定，
      **不再出现**「工具面无行业分类数据源」这句已失效的断言）；
    * 追加 ``industry_note`` 说明该判定未包含行业红线。

    ``risk`` / ``history`` 原文一个字不动（历史是历史）。返回的是副本，调用方改它
    不会影响库/文件里的原始记录。
    """
    if not isinstance(record, dict):
        return record
    view = copy.deepcopy(record)
    if not legacy_pre_gate(record):
        view["industry_graded"] = True
        view["legacy_pre_gate"] = False
        return view
    view["industry_pct"] = None
    view["industry_source"] = LEGACY_INDUSTRY_SOURCE
    view["industry_graded"] = False
    view["legacy_pre_gate"] = True
    view["industry_note"] = LEGACY_INDUSTRY_NOTE
    return view


# ---------------------------------------------------------------------------
# 工具面元数据（唯一事实来源：mcp_tools.TOOLS 的导入枚举，绝不硬编码名字）
# ---------------------------------------------------------------------------
_TOOL_FIELDS = {definition.name: frozenset(param.name for param in definition.params)
                for definition in mcp_tools.TOOLS}
WB_TOOL_NAMES = tuple(definition.name for definition in mcp_tools.TOOLS)

_DATA_PATTERN = re.compile(
    r"^(series|rt_|market_snapshot|cur_kline|quote_history|capital_|plate_|stock_screen|"
    r"info_|watchlist|f10|derivative|ipo_|economic_|short_|option_)")
_ALPHA_PATTERN = re.compile(r"^(factors|ic|sensitivity|correlation|sentiment_|quality)")
_RISK_PATTERN = re.compile(r"^(risk|snapshot|equity)")
_EXECUTION_PATTERN = re.compile(
    r"^(positions|account_|orders_|deals_|trade_max_qty|push_)")


def domain_of(name):
    """catalog.mjs ``domainOf`` 的 Python 等价物（同名规则，逐分支同序）。"""
    if name in WRITE_TOOLS:
        return "execution"
    if _DATA_PATTERN.match(name):
        return "data"
    if _ALPHA_PATTERN.match(name):
        return "alpha"
    if _RISK_PATTERN.match(name):
        return "risk"
    if _EXECUTION_PATTERN.match(name):
        return "execution"
    return "ecosystem"


def build_catalog():
    """六域工具目录：工作台 77 工具（kind=proxy，名字来自 TOOLS）+ V3 本地计算（kind=local）。"""
    by_domain = {domain: [] for domain in DOMAINS}
    for definition in mcp_tools.TOOLS:
        by_domain[domain_of(definition.name)].append({
            "name": definition.name,
            "kind": "proxy",
            "wb": definition.name,
            "desc": definition.description,
        })
    for name, domain, desc in V3_LOCAL_TOOLS:
        by_domain[domain].append({"name": name, "kind": "local", "wb": None, "desc": desc})
    return by_domain


def catalog_total(catalog=None):
    catalog = catalog if catalog is not None else build_catalog()
    return sum(len(rows) for rows in catalog.values())


# ---------------------------------------------------------------------------
# MCP 工具面真值（**动态取自注册表**，绝不写死常量）
#
# 为什么单列一层：六域工具目录（``catalog_total`` = 工作台 77 + 5 个本地计算）与 MCP
# ``tools/list`` 的真实工具面**不是同一个数**——``app.py`` 在路由登记完之后还会把
# ``/api/v3/*`` 路由桥成 ``v3_*`` MCP 工具（``v3_mcp.register``），桥接后的注册表才是
# MCP 客户端真正能列出的工具面。两个口径都必须可读，且必须各自标明来源，故这里只回
# 「注册表的真实长度」+「目录长度」，由调用方（``/api/v3/gateway``、``/metrics``）分别标注。
# ---------------------------------------------------------------------------
def _state_names(state, attr):
    """``app.state.<attr>`` 里的名字序列；缺失/非序列返回 ``None``（不是空列表——空列表是事实）。"""
    value = getattr(state, attr, None)
    if value is None or isinstance(value, (str, bytes)):
        return None
    try:
        names = [str(item) for item in value if str(item or "")]
    except TypeError:
        return None
    return names


def _registry_names(server):
    """从 MCP SDK 的 ``ToolManager`` 同步读回工具名（与 ``tools/list`` 读的是同一注册表）。

    用 ``_tool_manager.list_tools()`` 而不是 ``MCPServer.list_tools()``：后者是协程，而
    ``build()`` 跑在 ``asyncio.to_thread`` 的工作线程里（没有事件循环）；两者底下的注册表
    同一个，注册表在进程存活期内**只增不减**，因此这里的读数与 ``tools/list`` 必然一致。
    """
    lister = getattr(getattr(server, "_tool_manager", None), "list_tools", None)
    if not callable(lister):
        return None
    try:
        infos = lister()
    except Exception:  # noqa: BLE001 —— 读不到注册表就退到下一级兜底，绝不猜数
        return None
    names = [str(getattr(info, "name", "") or "") for info in infos or ()]
    names = [name for name in names if name]
    return names or None


def mcp_tool_surface(app=None):
    """``{mcp_total, domain_catalog, bridge, source}``——两个口径 + 各自的真值来源。

    * ``mcp_total``：**MCP 工具面**（``tools/list`` 会列出的工具数），动态取自注册表；
    * ``domain_catalog``：平台六域工具目录条目数（``catalog_total()``，**不是** MCP 面）；
    * ``bridge``：桥接进 MCP 的 ``v3_*`` 工具数（``app.state.v3_mcp_tools``，未知则 ``None``）。

    真值优先级（每一级都在 ``source`` 里如实标注）：
      1. ``app.state.mcp`` 的 SDK 注册表（生产路径：bridge 在 ``create_app`` 里装配）；
      2. ``app.state.mcp_tools`` + ``app.state.v3_mcp_tools``（app.py 装配时的两份绑定名单）；
      3. ``mcp_tools.TOOLS`` 导入枚举——此时 MCP 桥尚未装配，``mcp_total`` **只是工作台面**，
         ``source`` 明写这一点，绝不把它冒充成 MCP 工具面。
    """
    catalog = catalog_total()
    state = getattr(app, "state", None)
    registry = _registry_names(getattr(state, "mcp", None))
    bridge_names = _state_names(state, "v3_mcp_tools")
    bound_names = _state_names(state, "mcp_tools")
    if registry is not None:
        return {
            "mcp_total": len(registry),
            "domain_catalog": catalog,
            "bridge": None if bridge_names is None else len(bridge_names),
            "source": "MCP 注册表（MCPServer._tool_manager.list_tools()，与 tools/list 同源）",
        }
    if bound_names is not None and bridge_names is not None:
        return {
            "mcp_total": len(bound_names) + len(bridge_names),
            "domain_catalog": catalog,
            "bridge": len(bridge_names),
            "source": ("app.state.mcp_tools + app.state.v3_mcp_tools（app.py 装配名单；"
                       "注册表对象不可读）"),
        }
    return {
        "mcp_total": len(WB_TOOL_NAMES),
        "domain_catalog": catalog,
        "bridge": None if bridge_names is None else len(bridge_names),
        "source": ("mcp_tools.TOOLS 导入枚举（MCP 桥尚未装配，此数只是工作台工具面，"
                   "不是 MCP tools/list 的真值）"),
    }


# ---------------------------------------------------------------------------
# 进程内计数（模块级；每次 v3_run 调用累加）
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()
_COUNTERS = {"calls": 0, "errors": 0, "duration_ms": 0.0, "tools": {}}
_HTTP = {"requests": 0, "errors": 0}


def _count_tool(name, ok, ms):
    with _LOCK:
        _COUNTERS["calls"] += 1
        _COUNTERS["errors"] += 0 if ok else 1
        _COUNTERS["duration_ms"] += ms
        entry = _COUNTERS["tools"].setdefault(name, {"calls": 0, "errors": 0})
        entry["calls"] += 1
        entry["errors"] += 0 if ok else 1


def _count_http_request():
    with _LOCK:
        _HTTP["requests"] += 1


def _count_http_error():
    with _LOCK:
        _HTTP["errors"] += 1


def reset_counters():
    """清空进程内计数（测试用；生产不需要）。"""
    with _LOCK:
        _COUNTERS.update({"calls": 0, "errors": 0, "duration_ms": 0.0, "tools": {}})
        _HTTP.update({"requests": 0, "errors": 0})


def metrics_snapshot():
    """``metrics`` 端点的计数视图（mcp / wb 同源；http 只含本模块路由）。"""
    with _LOCK:
        calls = _COUNTERS["calls"]
        errors = _COUNTERS["errors"]
        total_ms = _COUNTERS["duration_ms"]
        by_tool = {name: entry["calls"] for name, entry in _COUNTERS["tools"].items()}
        http = dict(_HTTP)
    avg_ms = round(total_ms / calls) if calls else 0
    return {
        "mcp": {"calls": calls, "errors": errors, "avgMs": avg_ms, "tools": dict(by_tool)},
        "wb": {"calls": calls, "errors": errors, "avgMs": avg_ms, "byTool": dict(by_tool)},
        "http": {"requests": http["requests"], "errors": http["errors"]},
    }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc).isoformat()


def _headless_last(home_path, limit=10):
    """``headless.last`` 的**真实读数**：``v3_db`` 的 ``headless_log`` 表最近 N 条。

    这是本模块里唯一改动的一处（原先硬编码 ``[]``）：FR-MON-003 的调用日志由
    ``server/v3_headless.py`` 写表，网关/大脑视图在这里读同一张表，读的就是**真事实**。
    投影只取网关视图需要的小字段（不含 prompt/stdout/stderr 全文，避免响应膨胀）；
    库未初始化/损坏时返回 ``[]``（与 ``v3_db`` 的「不因数据库异常 500」同口径），
    **不伪造一条记录**。
    """
    try:
        rows = v3_db.list_events(home_path, "headless_log", limit=limit)
    except Exception:  # noqa: BLE001 —— 台账读不到就是空，绝不编
        return []
    if not isinstance(rows, list):
        return []
    return [{"startedAt": row.get("started_at"), "taskType": row.get("task_type"),
             "trigger": row.get("trigger"), "success": bool(row.get("success")),
             "exitCode": row.get("exit_code"), "durationMs": row.get("duration_ms"),
             "tokensEstimate": row.get("tokens_estimate"), "outcome": row.get("outcome"),
             "killed": bool(row.get("killed"))}
            for row in rows if isinstance(row, dict)]


def _headless_module():
    """惰性取 ``server.v3_headless``（缺模块/导入失败 → ``None``，绝不让网关 500）。"""
    try:
        from server import v3_headless  # noqa: PLC0415 —— 惰性：网关是只读视图，不参与装配
        return v3_headless
    except Exception:  # noqa: BLE001
        return None


def _headless_log_rows(home_path):
    """``headless_log`` 原始行（未投影）——``today`` 统计与 ``last`` 共用一次读取。"""
    try:
        rows = v3_db.list_events(home_path, "headless_log", limit=None)
    except Exception:  # noqa: BLE001
        return None
    return rows if isinstance(rows, list) else None


def _headless_status(home_path):
    """``/api/v3/gateway`` 与 ``/api/v3/brain`` 的 ``headless`` 块（**真实读数**）。

    2026-09-20 修（原实现恒 ``today=0`` / ``status="unavailable"`` / ``breaker=null``，
    并挂一句已失效的「未挂载 Headless CLI 子通道」）:

      * ``today``：``v3_db.headless_log`` 里 ``started_at`` 属于**今天（UTC）**的记录，
        用 ``v3_headless.summarize`` 的同一口径统计（模块在不在都读，读的就是真表）；
      * ``breaker``：``v3_headless`` 的真实熔断参数（并发上限 / 单次超时 / token 预算），
        经进程内注册表 ``get_entry(home)`` 取 ``runner.params()``——**不创建运行时、不起进程**；
        ``maxConcurrency`` / ``timeoutSeconds`` / ``tokenBudget`` 都在，另外带上 ``enabled``；
      * ``schedulerAlive`` / ``nextFireTimes``：模块已注册进本进程时给调度器真实状态；
        未注册 → ``null`` + ``status="implemented-not-registered"``（**不说「不可用/未实现」**）；
      * ``registered``：``v3_headless.get_entry(home) is not None``（装配真实性的唯一判据）；
      * ``logNote`` / ``breakerNote``：写清每项来自哪张表 / 哪个对象，读不到的原因照实写。
    """
    rows = _headless_log_rows(home_path)
    if rows is None:
        log_note = "headless_log 表不可读（v3_db.list_events 失败）→ today/last 为空，不伪造记录"
        rows = []
    else:
        log_note = f"v3_db.headless_log（server/v3_headless.py 写入；共 {len(rows)} 条历史记录）"
    day = datetime.now(timezone.utc).date().isoformat()
    todays = [row for row in rows
              if isinstance(row, dict) and str(row.get("started_at") or "").startswith(day)]
    durations = []
    for row in todays:
        number = _number(row.get("duration_ms"))
        if number is not None:
            durations.append(number)
    today = {
        "total": len(todays),
        "success": sum(1 for row in todays if row.get("success")),
        "failed": sum(1 for row in todays if not row.get("success")),
        "killed": sum(1 for row in todays if row.get("killed")),
        "avgMs": (round(sum(durations) / len(durations), 3) if durations else 0),
        "tokensEstimate": sum(int(_number(row.get("tokens_estimate")) or 0) for row in todays),
        "date": day,
        "basis": "UTC 当日（started_at 前缀），口径与 v3_headless.today_summary 一致",
    }

    module = _headless_module()
    registered = False
    breaker = None
    scheduler_alive = None
    next_fire = None
    status_note = HEADLESS_REASON
    if module is not None:
        # 熔断参数是**配置真值**（不依赖是否注册）：直接读 v3_headless.load_config(home)。
        try:
            config, config_sources = module.load_config(home_path)
            breaker = {"maxConcurrency": config.get("maxConcurrency"),
                       "timeoutSeconds": config.get("timeoutSeconds"),
                       "tokenBudget": config.get("tokenBudget"),
                       "enabled": config.get("enabled"),
                       "profile": config.get("profile"),
                       "configSources": config_sources}
        except Exception as error:  # noqa: BLE001
            breaker = {"error": f"{type(error).__name__}: {error}"}
        try:
            entry = module.get_entry(home_path)
        except Exception:  # noqa: BLE001
            entry = None
        registered = entry is not None
        if registered:
            runner = entry.get("runner")
            scheduler = entry.get("scheduler")
            try:
                params = runner.params()
                breaker = {"maxConcurrency": params.get("maxConcurrency"),
                           "timeoutSeconds": params.get("timeoutSeconds"),
                           "tokenBudget": params.get("tokenBudget"),
                           "enabled": params.get("enabled"),
                           "profile": params.get("profile"),
                           "configSources": breaker.get("configSources")
                           if isinstance(breaker, dict) else None}
            except Exception as error:  # noqa: BLE001
                breaker = {"error": f"{type(error).__name__}: {error}"}
            try:
                scheduler_alive = bool(getattr(scheduler, "alive", False))
                next_fire = scheduler.next_fire_times()[:3]
            except Exception as error:  # noqa: BLE001
                scheduler_alive = None
                next_fire = None
                status_note = (f"调度器视图读取失败（{type(error).__name__}: {error}）；"
                               f"registry 已注册但状态不可读")
            status = "running" if scheduler_alive else "registered-idle"
            status_note = (
                f"headless 模块已注册进本进程（{HEADLESS_COMMAND}）；调度线程 "
                f"{'已保活' if scheduler_alive else '未运行（触发循环由 server/scheduler.py '
                                                 '的 scheduler_tick 保活）'}；"
                f"熔断参数取自 runner.params() 真实配置")
        else:
            status = "implemented-not-registered"
    else:
        status = "implemented-not-registered"
        status_note = ("server.v3_headless 导入失败（模块不可用）→ 只读 headless_log 表；"
                       + HEADLESS_REASON)
    return {
        "today": today,
        "breaker": breaker,
        "breakerNote": ("外部熔断（FR-GATEWAY-004）三参数：并发上限 / 单次超时 / token 预算"
                        "（估算口径见 v3_headless.TOKEN_ESTIMATE_NOTE）；注册未接线时为 null"),
        "last": _headless_last(home_path),
        "status": status,
        "reason": status_note,
        "registered": registered,
        "schedulerAlive": scheduler_alive,
        "nextFireTimes": next_fire,
        "command": HEADLESS_COMMAND,
        "logNote": log_note,
        "source": "v3_db.headless_log + server.v3_headless（get_entry/params/next_fire_times）",
    }


def _failure(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def _error_text(envelope):
    error = (envelope or {}).get("error") or {}
    return f"{error.get('code') or 'wb/error'}: {error.get('message') or 'unknown'}"


def _value_or_none(envelope):
    return envelope.get("value") if isinstance(envelope, dict) and envelope.get("ok") else None


def _passthrough(envelope):
    """工具信封 → V3 约定（``{ok:true, value}`` → ``{ok:true, data}``；错误原样透传）。

    与 V3 原型（已退役）实现 的 ``wbValue`` 同形（前端已按 ``data`` 实现）。
    """
    if isinstance(envelope, dict) and envelope.get("ok"):
        return {"ok": True, "data": envelope.get("value")}
    return {"ok": False,
            "error": (envelope or {}).get("error") or {"code": "wb/error", "message": "unknown"}}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _query_int(request, key, default=None):
    raw = request.query_params.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _read_json_file(path):
    """容错读 JSON：文件缺失/坏 JSON → None（绝不抛错，也绝不伪造内容）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_trim(path):
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _read_mode(home):
    """服务真实模式（模式文件；内容非法按 unknown 处理并回传原因）。"""
    try:
        return store_access.read_mode(str(home)), None
    except Exception as error:  # noqa: BLE001 —— 坏文件不 500，但也绝不伪造成 sim
        return "unknown", str(error)[:200]


# ---------------------------------------------------------------------------
# 风控分级（risk.mjs ``checkOrder`` 的 Python 等价物，阈值同源）
# ---------------------------------------------------------------------------
def check_order(value, nav, industry_pct=0.0, drawdown_pct=0.0, limits=None,
                industry_source=None, industry_top=None, industry_as_of=None,
                industry_probe_age_ms=None, industry_missing=None, industry_universe=None,
                industry_market=None, industry_reason=None, funds=None):
    """下单前风控分级（纯函数）。返回 ``(action, reasons)``。

    action ∈ ``auto`` / ``manual`` / ``blocked``（回撤红线）/ ``blocked_industry``（行业红线）。

    判定顺序（后者覆盖前者，行业/回撤红线优先于单笔结论）
    ----------------------------------------------------
    1. 缺 NAV 或金额 → ``manual``（不折算，保守退回人工确认）；
    2. 单笔占比 > ``LIMITS["singlePct"]``(2%) → ``manual``；
    3. **行业集中度** > ``LIMITS["industryPct"]``(20%) → ``blocked_industry``；
       无读数（``industry_pct=None``）→ **fail-open**（不阻断），但必须留痕
       「行业暴露数据不可用，未参与阻断（原因：…）」；
    4. 回撤 ≥ ``LIMITS["drawdownPct"]``(15%) → ``blocked``；**最后**叠加
       ``funds``（FR-EXEC-003 资金检查，可选）——资金不足 → ``blocked``。

    行业参数（``industry_*``）来自 :func:`server.v3_risk_gate.industry_context`，只用于
    **措辞与留痕**（读数、来源、as_of、缺失数），阈值判定仍只看 ``industry_pct``：

    * ``industry_source``：读数来源（``cache/futu/info_owner_plate`` / ``fetch/...`` /
      ``no-data``）——写进原因原文，避免「阻断了一个不知道哪来的数」；
    * ``industry_missing`` > 0：上游有标的没取到行业分类 → 读数只是**下界**，必须标注；
    * **历史调用口径不变**：完全不传任何 ``industry_*``（``industry_pct`` 取默认 ``0.0``）
      时不做行业判定、不产生行业原因——与闸门接入前逐字段一致。

    ``funds``（FR-EXEC-003 资金检查，**缺省 ``None`` → 本函数行为逐字段不变**）：形状同
    ``v3_analytics.funding_check_data`` 的信封（``action``/``readings``/``source``/``reason``）。
    ``action="blocked"``（订单金额 > 真实可用购买力）→ 判定升级为 ``blocked`` 并把读数写进
    原因；``action="unknown"``（上游没有购买力字段）→ **不改判定**，只把原因作为留痕追加
    （fail-open，与行业无读数同口径：绝不因为「读不到」就假装资金充足，也绝不凭空阻断）。
    """
    limits = limits or LIMITS
    reasons = []
    action = "auto"
    value = _number(value) or 0.0
    nav = _number(nav) or 0.0
    if nav > 0 and value > 0:
        single_pct = value / nav * 100
        if single_pct > limits["singlePct"]:
            action = "manual"
            reasons.append(f"单笔占比 {single_pct:.2f}% > {limits['singlePct']:.0f}%，需人工确认")
    else:
        action = "manual"
        reasons.append("缺少市值或订单金额，无法自动判定")

    industry_pct = _number(industry_pct)
    # 「提供了行业口径」的判据：没有读数 + 没有来源/原因 = 调用方压根没接行业口径（历史行为）
    has_industry = not (industry_pct is None and industry_source is None
                        and industry_reason is None)
    no_data = has_industry and (industry_pct is None or str(industry_source or "") == "no-data")
    if industry_pct is not None and has_industry and not no_data:
        limit = _number(limits["industryPct"]) or 0.0
        context_bits = []
        if industry_top:
            context_bits.append(f"top={industry_top}")
        context_bits.append(f"来源 {industry_source or 'unknown'}")
        if industry_as_of:
            context_bits.append(f"as_of {industry_as_of}")
        if industry_market:
            context_bits.append(f"市场 {industry_market}")
        if industry_probe_age_ms is not None:
            context_bits.append(f"探测年龄 {float(industry_probe_age_ms) / 1000.0:.0f}s")
        text = (f"单一行业暴露 {industry_pct:.1f}% > {limit:.0f}%（"
                + "，".join(context_bits) + "）")
        if industry_pct > limit:
            action = "blocked_industry"
            reasons.append(text + "，强制阻断")
        elif (isinstance(industry_missing, (int, float))
              and not isinstance(industry_missing, bool) and industry_missing >= 1):
            reasons.append(text + "（未超限，但读数只是下界）")
        missing_note = _industry_missing_note(industry_missing, industry_universe)
        if missing_note:
            reasons.append(missing_note)
    elif no_data:
        detail = industry_reason or f"无新鲜行业读数（来源 {industry_source or 'no-data'}）"
        reasons.append(f"行业暴露数据不可用，未参与阻断（原因：{detail}）")

    drawdown_pct = abs(_number(drawdown_pct) or 0.0)
    if drawdown_pct >= limits["drawdownPct"]:
        action = "blocked"
        reasons.append(f"回撤 {drawdown_pct:.1f}% 触及 {limits['drawdownPct']:.0f}% 红线，强制阻断")

    # ── FR-EXEC-003 资金检查（可选维度；缺省不参与，历史调用零改动）────────────────
    if isinstance(funds, dict) and funds.get("action") in ("blocked", "noted", "unknown"):
        readings = funds.get("readings") if isinstance(funds.get("readings"), dict) else {}
        fields = funds.get("funding_basis_field")
        basis = readings.get("buyingPower")
        if funds.get("action") == "blocked":
            action = "blocked"
            reasons.append(
                f"资金检查：订单金额 {value:.2f} > 可用购买力 {basis}"
                f"（字段 {fields or '—'}，来源 {funds.get('source') or '—'}，"
                f"as_of {funds.get('as_of') or '—'}）→ 强制阻断")
        elif funds.get("action") == "unknown":
            reasons.append(f"资金检查无读数，未参与阻断（原因：{funds.get('reason') or 'unknown'}）")
        else:
            reasons.append(
                f"资金检查：订单金额 {value:.2f} ≤ 可用购买力 {basis}"
                f"（字段 {fields or '—'}）→ 通过（仅留痕）")
    return action, reasons


def _industry_missing_note(missing, universe):
    """``missing`` 非空 → 读数只是下界（数据质量提示；不改判定，只留痕）。"""
    if isinstance(missing, bool) or not isinstance(missing, (int, float)) or missing < 1:
        return None
    covered = (f"（{universe:.0f} 只标的）" if isinstance(universe, (int, float))
               and not isinstance(universe, bool) else "")
    return (f"行业暴露数据不完整：{missing:.0f} 只标的未取到行业分类{covered}，"
            f"当前读数是**下界**，真实暴露可能更高")


def risk_reasons_detail(action, reasons, context, industry=None):
    """把一次分级压成**审计留痕行**（台账 ``history`` 的字段面，只加不删既有键）。

    ``context`` 是 :meth:`OmsLedger.context` 的返回值；``industry`` 是
    ``v3_risk_gate.industry_context`` 的读数信封（可为 ``None``）。返回的字典同时带上
    ``rule``/``readings``，便于事后回答「阻断时看的是哪个数、哪个来源、哪一刻」。
    """
    industry = industry if isinstance(industry, dict) else {}
    return {
        "action": action,
        "rule": ("industry-red-line" if action == "blocked_industry"
                 else ("drawdown-red-line" if action == "blocked"
                       else ("single-order" if action == "manual" else "within-limits"))),
        "reasons": list(reasons),
        "readings": {
            "nav": context.get("nav"),
            "nav_source": context.get("nav_source"),
            "drawdown_pct": context.get("drawdown_pct"),
            "drawdown_source": context.get("drawdown_source"),
            "industry_pct": context.get("industry_pct"),
            "industry_source": context.get("industry_source"),
            "industry_top": industry.get("top_industry"),
            "industry_market": industry.get("market"),
            "industry_as_of": industry.get("as_of"),
            "industry_probe_age_ms": industry.get("probe_age_ms"),
            "industry_missing": industry.get("missing"),
            "industry_universe": industry.get("universe"),
        },
    }


_SIDE_ALIASES = {"BUY": "BUY", "B": "BUY", "买入": "BUY", "LONG": "BUY",
                 "SELL": "SELL", "S": "SELL", "卖出": "SELL", "SHORT": "SELL"}
_MARKET_CURRENCY = {"SH": "CNY", "SZ": "CNY", "BJ": "CNY", "CN": "CNY", "HKCC": "CNY",
                    "HK": "HKD", "US": "USD", "SG": "SGD", "JP": "JPY", "KR": "KRW",
                    "CA": "CAD"}
#: 富途模拟账户 market_id → 币种（与 trading.SIM_MARKET_CHAIN 的 1/3/100 同源实测口径；
#: 表外 market_id 如实回落为「未知」，不猜）
_SIM_MARKET_CURRENCY = {1: "HKD", 3: "CNY", 100: "USD"}
_CURRENCY_CODES = frozenset({"CNY", "HKD", "USD", "SGD", "JPY", "KRW", "CAD", "AUD", "EUR"})
_PREFIXED = re.compile(r"^(?P<market>[A-Za-z]{2})\.(?P<code>[0-9A-Za-z]+)$")
_SUFFIXED = re.compile(r"^(?P<code>[0-9]{6})\.(?P<market>[A-Za-z]{2})$")


def _norm_symbol(value):
    text = str(value or "").strip().upper()
    match = _PREFIXED.match(text)
    if match:
        return f"{match.group('market')}.{match.group('code')}"
    match = _SUFFIXED.match(text)
    if match:
        return f"{match.group('market')}.{match.group('code')}"
    return text


def _norm_side(value):
    text = str(value or "").strip().upper()
    return _SIDE_ALIASES.get(text, text)


def _currency_from_market(value):
    """市场标识（前缀 / 市场链名 / 币种码 / 富途 market_id）→ 币种；认不出返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _SIM_MARKET_CURRENCY.get(int(value)) if float(value).is_integer() else None
    text = str(value).strip().upper()
    if not text:
        return None
    if text in _CURRENCY_CODES:
        return text
    if text in _MARKET_CURRENCY:
        return _MARKET_CURRENCY[text]
    if text.isdigit():
        return _SIM_MARKET_CURRENCY.get(int(text))
    return None


def _currency_of(row, group_market=None):
    """行的币种信号：行内市场/币种 → 代码前缀 → 账户分组声明的市场 → 未知。"""
    for value in (row.get("market"), row.get("trd_market"), row.get("currency")):
        currency = _currency_from_market(value)
        if currency:
            return currency
    symbol = _norm_symbol(row.get("code") or row.get("symbol") or row.get("ticker"))
    if "." in symbol:
        currency = _currency_from_market(symbol.split(".", 1)[0])
        if currency:
            return currency
    currency = _currency_from_market(group_market)
    return currency or "?"


def _row_market_value(row):
    for key in ("market_val", "market_value", "mv", "value"):
        number = _number(row.get(key))
        if number is not None:
            return number
    return 0.0


#: 模拟账户 ``market_id`` → 市场链（与 ``v3_analytics.SIM_MARKET_TO_CHAIN`` 同源实测口径）。
_SIM_MARKET_CHAIN = {1: "HK", 3: "SH", 100: "US"}


def _sim_market_chain(value):
    """账户分组的 ``market`` → 市场链（表外/非法 → ``None``，不猜）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and float(value).is_integer():
        return _SIM_MARKET_CHAIN.get(int(value))
    text = str(value).strip().upper()
    if text in ("SH", "SZ", "BJ", "HK", "US"):
        return "SH" if text in ("SH", "SZ", "BJ") else text
    if text.isdigit():
        return _SIM_MARKET_CHAIN.get(int(text))
    return None


def _first_field(mapping, keys):
    """按优先级取第一个**可解析数值**的字段名 → 命中字段名 / ``None``。"""
    for key in keys:
        if _number((mapping or {}).get(key)) is not None:
            return key
    return None


def _first_of(mapping, keys):
    """按优先级取第一个可解析数值 → 值 / ``None``（口径与 ``_first_field`` 一致）。"""
    for key in keys:
        number = _number((mapping or {}).get(key))
        if number is not None:
            return number
    return None


def positions_nav(envelope):
    """``positions`` 信封 → ``(nav, nav_source)``；多账户/多币种一律 0（不折算）。"""
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return 0.0, f"无数据源（positions 取数失败：{_error_text(envelope)}）"
    value = envelope.get("value") if isinstance(envelope.get("value"), dict) else {}
    groups = [group for group in (value.get("groups") or []) if isinstance(group, dict)]
    if not groups:
        return 0.0, "无数据源（positions 无账户分组）"
    if len(groups) > 1:
        return 0.0, f"multi-account({len(groups)} 个账户/多币种) 不折算"
    group = groups[0]
    account = group.get("acc_id") or ""
    rows = [row for row in (group.get("positions") or []) if isinstance(row, dict)]
    currencies = {_currency_of(row, group.get("market")) for row in rows}
    if len(currencies) > 1:
        return 0.0, (f"multi-currency(单账户 {account} 含 {'/'.join(sorted(currencies))}) "
                     f"不折算")
    total = sum(_row_market_value(row) for row in rows)
    if total <= 0:
        return 0.0, f"positions(单账户 {account} 持仓市值为 0 或缺市值字段)"
    currency = next(iter(currencies), "?")
    label = f"；币种 {currency}" if currency != "?" else "；币种未知"
    return total, f"positions(单账户 {account} 持仓市值合计，不含现金{label})"


def _open_rows(envelope):
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return []
    value = envelope.get("value") if isinstance(envelope.get("value"), dict) else {}
    rows = []
    groups = value.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict):
                rows.extend(row for row in (group.get("rows") or []) if isinstance(row, dict))
    if isinstance(value.get("rows"), list):
        rows.extend(row for row in value["rows"] if isinstance(row, dict))
    return rows


def _symbol_matches(row_symbol, wanted_symbol):
    """标的比对：完整符号相等；一侧没有市场前缀时（富途模拟账本的行只有 code）
    退回代码部分比较——前缀缺失时本就没有市场信息可区分。"""
    if not row_symbol or not wanted_symbol:
        return False
    if row_symbol == wanted_symbol:
        return True
    if "." in row_symbol and "." in wanted_symbol:
        return False
    return row_symbol.split(".")[-1] == wanted_symbol.split(".")[-1]


# ---------------------------------------------------------------------------
# 按市场过滤（``?market=SH|HK|US``；不改任何既有字段，只挑分组/行并在 filter 里如实计数）
# ---------------------------------------------------------------------------
#: 分组里可能承载行的键（positions / orders_open 用 positions·rows，成交/委托用 rows）
GROUP_ROW_KEYS = ("positions", "rows", "deals", "orders")


def _group_rows(group):
    """分组 → ``(行列表, 行所在的键)``；没有行的分组 → ``([], None)``。"""
    for key in GROUP_ROW_KEYS:
        rows = group.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)], key
    return [], None


def _row_market(row):
    """行 → 市场口径（按标的归一后取前缀）；无法判定 → ``None``（不猜）。"""
    symbol = None
    for key in ("symbol", "code", "ticker", "stock_code"):
        value = row.get(key)
        if value not in (None, ""):
            symbol = value
            break
    ticker = v3_universe.canonical_ticker(symbol)
    if ticker is None:
        return None
    return v3_universe.market_of_ticker(ticker)


def filter_grouped_value(value, market, note=None):
    """``positions``/``orders_open``/``deals_today`` 的 value → 只保留该市场的账户分组。

    返回 ``(value, stats)``：命中市场的分组原样保留（行不动）。分两种「不确定」：

      * 上游**声明了**市场但不在已知口径内（``market_id`` 9/10/11/12/13/16）→ **不猜市场**，
        整组排除并计入 ``unknownMarketGroups`` / ``unknownMarkets``（上游原值）；
      * 分组**没有**市场声明（实盘账户 ``market=null``）→ 逐行按标的自身前缀归因，
        只保留命中该市场的行（``attributedByRowPrefix``）；一行都归不了也计入未知。

    ``value`` 不是分组结构 → 原样返回并在 ``note`` 里说明（不静默丢弃）。
    """
    stats = {"field": "groups[].market", "market": market, "groups": 0, "keptGroups": 0,
             "otherMarketGroups": 0, "unknownMarketGroups": 0, "keptRows": 0,
             "excludedRows": 0, "attributedByRowPrefix": 0, "otherMarkets": [],
             "unknownMarkets": [], "note": note}
    if not isinstance(value, dict):
        stats["note"] = "上游 value 不是对象 → 未过滤，原样返回"
        return value, stats
    groups = value.get("groups")
    if not isinstance(groups, list):
        stats["note"] = "上游没有 groups 字段（未按账户分组）→ 未过滤，原样返回"
        return value, stats
    stats["groups"] = len(groups)
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        rows, rows_key = _group_rows(group)
        raw_label = group.get("market")
        declared = raw_label not in (None, "")
        label = v3_universe.market_of_account_label(raw_label)
        if declared and label is None:
            # 上游声明了市场但不在已知口径内（如 market_id 9/10/11/12/13/16）：
            # **不猜成某个市场**，如实排除并在 unknownMarkets 里列出上游原值。
            stats["unknownMarketGroups"] += 1
            stats["excludedRows"] += len(rows)
            if raw_label not in stats["unknownMarkets"]:
                stats["unknownMarkets"].append(raw_label)
            continue
        if label is None:
            # 分组**没有**市场声明（实盘账户 market=null）→ 逐行按标的自身前缀归因
            matched = [row for row in rows if _row_market(row) == market]
            if matched and rows_key:
                kept.append({**group, rows_key: matched})
                stats["keptGroups"] += 1
                stats["keptRows"] += len(matched)
                stats["excludedRows"] += len(rows) - len(matched)
                stats["attributedByRowPrefix"] += 1
            else:
                stats["unknownMarketGroups"] += 1
                stats["excludedRows"] += len(rows)
                raw = group.get("market")
                if raw not in stats["unknownMarkets"]:
                    stats["unknownMarkets"].append(raw)
            continue
        if label != market:
            stats["otherMarketGroups"] += 1
            stats["excludedRows"] += len(rows)
            if label not in stats["otherMarkets"]:
                stats["otherMarkets"].append(label)
            continue
        kept.append(group)
        stats["keptGroups"] += 1
        stats["keptRows"] += len(rows)
    return {**value, "groups": kept}, stats


def filter_plan_value(value, market, note=None):
    """``plan`` 的 value → ``plans`` 只保留「``target`` 里含该市场标的」的计划。

    ``target`` **原样保留**（不裁剪权重，否则一份计划的权重和就不再是 1）；
    没有 ``target`` 字段的计划同样保留（它不是市场相关条目，隐藏反而丢信息）。
    """
    stats = {"field": "plans[].target", "market": market, "plans": 0, "keptPlans": 0,
             "otherMarketPlans": 0, "noTargetPlans": 0, "note": note}
    if not isinstance(value, dict):
        stats["note"] = "上游 value 不是对象 → 未过滤，原样返回"
        return value, stats
    plans = value.get("plans")
    if not isinstance(plans, list):
        stats["note"] = "上游没有 plans 字段 → 未过滤，原样返回"
        return value, stats
    stats["plans"] = len(plans)
    kept = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        target = plan.get("target")
        if not isinstance(target, dict) or not target:
            kept.append(plan)
            stats["noTargetPlans"] += 1
            continue
        markets = {v3_universe.market_of_ticker(ticker) for ticker in target}
        if market in markets:
            kept.append(plan)
            stats["keptPlans"] += 1
        else:
            stats["otherMarketPlans"] += 1
    return {**value, "plans": kept}, stats


def filter_deals_value(value, market):
    """``deals_today`` 的 value → 只保留该市场的成交分组；返回 ``(value, filter.deals)``。

    口径**复用**，不另造：账户/分组声明的市场标识（``market`` 数值 ``market_id``、
    数字字符串、``SH``/``HK``/``US`` 链名）经 ``v3_universe.market_of_account_label``
    归一——那一份的白名单就是 ``v3_quality.MARKET_TRD_CODES``。未知/未登记的标识
    （``market_id`` 9/10/…）**不猜市场**，整组排除并计入 ``unknownMarketGroups``。

    统计 ``{market, kept, excluded, unknownMarketGroups}``：``kept``/``excluded`` 是
    **成交笔数**（分组里的行数）——「按市场看成交」要看的就是笔数；需要分组级明细
    （``keptGroups``/``attributedByRowPrefix``/``unknownMarkets``…）直接用
    ``filter_grouped_value``，本函数只暴露契约要求的 4 个计数，不造第二份统计。
    """
    filtered, stats = filter_grouped_value(value, market, note="deals_today 按账户市场过滤")
    return filtered, {"market": market,
                      "kept": stats["keptRows"],
                      "excluded": stats["excludedRows"],
                      "unknownMarketGroups": stats["unknownMarketGroups"]}


def open_hit(rows, order_id, symbol, side, qty):
    """在途订单命中（client_order_id / 备注单号 / 同标的同方向同数量）。"""
    wanted_symbol = _norm_symbol(symbol)
    wanted_side = _norm_side(side)
    wanted_qty = _number(qty)
    for row in rows:
        identifier = str(row.get("client_order_id") or row.get("remark") or "").strip()
        if identifier and identifier == str(order_id):
            return True
        row_symbol = _norm_symbol(row.get("symbol") or row.get("code") or row.get("ticker"))
        if not _symbol_matches(row_symbol, wanted_symbol):
            continue
        if _norm_side(row.get("side") or row.get("trd_side")) != wanted_side:
            continue
        row_qty = _number(row.get("qty") if row.get("qty") is not None else row.get("order_qty"))
        if row_qty is not None and wanted_qty is not None and row_qty == wanted_qty:
            return True
    return False


# ---------------------------------------------------------------------------
# OMS 台账（口径与 V3 原型一致；**无任何下单方法**）
# ---------------------------------------------------------------------------
class OmsLedger:
    """把 ``plan`` 的计划订单登记为平台侧订单，逐单风控分级并与在途订单对账。"""

    def __init__(self, call, home):
        self._call = call
        self._home = Path(home)
        self.orders_path = self._home / OMS_FILENAME
        self.sync_log_path = self._home / OMS_SYNC_FILENAME

    # ---- 持久化（主存 SQLite；文件为冷备/兼容镜像）----
    def read(self):
        """台账（全量字典）：优先库；库空/不可用 → 回退只读 JSON 文件。"""
        records = self._read_db()
        if records is not None:
            return records
        raw = _read_json_file(self.orders_path)
        orders = raw.get("orders") if isinstance(raw, dict) else None
        if not isinstance(orders, dict):
            return {}
        return {str(key): record for key, record in orders.items() if isinstance(record, dict)}

    def _read_db(self):
        """库里的台账；库不可用/无记录 → ``None``（调用方回退文件）。

        任何数据库异常都在这里收敛成 ``None``——**接口不会因此 500**，最坏情况是
        退回「读文件」这条迁移前的路径。
        """
        try:
            rows = v3_db.list_events(self._home, "oms_orders", limit=None, order="asc")
        except Exception:  # noqa: BLE001 —— 库异常按「库里没有」处理
            return None
        if not rows:
            return None
        orders = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = row.get("id")
            if identifier in (None, ""):
                continue
            orders[str(identifier)] = row
        return orders or None

    def write(self, orders):
        """整表落盘：先写库（逐单 upsert + 清理已不在集合里的行），再原子写文件冷备。

        库写失败**不阻断**（文件仍是完整副本，读路径会回退文件）；文件写失败沿用既有
        语义（抛给调用方，best-effort 由 ``sync`` 的调用面决定）。
        """
        records = [record for record in (orders or {}).values() if isinstance(record, dict)]
        try:
            v3_db.replace_events(self._home, "oms_orders", records)
        except Exception:  # noqa: BLE001 —— 主存失败不清空冷备，绝不把好数据写坏
            pass
        payload = json.dumps({"version": 1, "updated_at": _now(), "orders": orders},
                             ensure_ascii=False, indent=2) + "\n"
        path = self.orders_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".v3-oms-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _append_sync(self, record):
        try:
            v3_db.append_event(self._home, "oms_sync", record)
        except Exception:  # noqa: BLE001 —— 留痕失败不阻断业务
            pass
        try:
            self.sync_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.sync_log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 留痕失败不阻断业务（参考实现 audit 的 best-effort 同义）

    # ---- 台账视图 ----
    @staticmethod
    def stage_counts_of(orders):
        counts = {}
        for record in orders.values():
            stage = str(record.get("stage") or "unknown")
            counts[stage] = counts.get(stage, 0) + 1
        return counts

    def stage_counts(self, market=None):
        if market:
            return self.stage_counts_of({record.get("id"): record for record in self.list(market)})
        return self.stage_counts_of(self.read())

    def list(self, market=None):
        """台账订单（按 ``updated_at`` 倒序）；``market`` 给定时按订单标的的市场前缀过滤。"""
        records = list(self.read().values())
        if market:
            records = [record for record in records
                       if v3_universe.market_of_ticker(record.get("ticker")) == market]
        records.sort(key=lambda record: str(record.get("updated_at") or ""), reverse=True)
        return records

    def market_filter_stats(self, market):
        """台账按市场过滤的真实计数（台账订单的市场前缀分布，供前端核对）。"""
        records = list(self.read().values())
        stats = {"field": "orders[].ticker", "market": market, "orders": len(records),
                 "keptOrders": 0, "otherOrders": 0, "unattributedOrders": 0,
                 "prefixes": {}}
        for record in records:
            prefix = v3_universe.market_of_ticker(record.get("ticker"))
            label = prefix or "unknown"
            stats["prefixes"][label] = stats["prefixes"].get(label, 0) + 1
            if prefix == market:
                stats["keptOrders"] += 1
            elif prefix is None:
                stats["unattributedOrders"] += 1
            else:
                stats["otherOrders"] += 1
        return stats

    # ---- 工作台上下文（NAV / 回撤 / 在途 / 待确认 / 行业暴露）----
    def industry_context(self, market=None, *, wb_call=None, max_age_ms=None, now=None):
        """某市场的行业集中度读数（``v3_risk_gate.industry_context`` 的台账封装）。

        ``wb_call`` 给了就允许「缓存不可用时现取一次」（只读，板块映射过 v3_ratelimit）；
        不给则**只读缓存**，没有读数就按 fail-open 返回 no-data 信封。
        """
        return v3_risk_gate.industry_context(self._home, market, wb_call=wb_call,
                                            max_age_ms=max_age_ms, now=now)

    def context(self, *, industry=None, industry_wb_call=None, now=None):
        """台账上下文。``industry`` 是这个市场的行业读数（``None`` → 现读一次，只读缓存）。"""
        equity = self._call("equity", {"window": 30})
        positions = self._call("positions", {})
        orders_open = self._call("orders_open", {})
        confirmation = self._call("confirmation", {})

        nav = 0.0
        nav_source = "无数据源"
        drawdown_pct = 0.0
        drawdown_source = "none"
        if isinstance(equity, dict) and equity.get("ok"):
            value = equity.get("value") if isinstance(equity.get("value"), dict) else {}
            current = _number(value.get("current"))
            if current is not None and current > 0:
                nav = current
                nav_source = "sim-ledger(equity.current)"
            raw_drawdown = _number(value.get("max_drawdown"))
            if raw_drawdown is not None:
                drawdown_pct = abs(raw_drawdown) * 100 if abs(raw_drawdown) <= 1 \
                    else abs(raw_drawdown)
                drawdown_source = "sim-ledger(max_drawdown)"
        if nav <= 0:
            # ① 单账户单币种才用持仓市值；② 多账户/多币种不折算（NAV=0，保守退回人工确认）
            nav, nav_source = positions_nav(positions)

        confirmation_value = None
        if isinstance(confirmation, dict) and confirmation.get("ok"):
            confirmation_value = confirmation.get("value")

        if industry is None:
            industry = self.industry_context(None, wb_call=industry_wb_call, now=now)
        industry_pct = _number(industry.get("industry_pct"))

        return {
            "nav": nav,
            "nav_source": nav_source,
            "drawdown_pct": drawdown_pct,
            "drawdown_source": drawdown_source,
            # 行业读数：None = 没有读数（fail-open），**不是** 0%
            "industry_pct": industry_pct,
            "industry_source": industry.get("industry_source") or INDUSTRY_SOURCE,
            "industry_top": industry.get("top_industry"),
            "industry_market": industry.get("market"),
            "industry_as_of": industry.get("as_of"),
            "industry_probe_age_ms": industry.get("probe_age_ms"),
            "industry_missing": industry.get("missing"),
            "industry_universe": industry.get("universe"),
            "industry_reason": industry.get("reason"),
            "industry": dict(industry),
            "open_rows": _open_rows(orders_open),
            "equity": equity,
            "positions": positions,
            "orders_open": orders_open,
            "confirmation": confirmation_value,
            "confirmation_envelope": confirmation,
        }

    def _upsert(self, orders, order, context, plan_record, industry=None, funds=None):
        plan_id = str(plan_record.get("plan_id") or "")
        symbol = str(order.get("symbol") or "")
        side = str(order.get("side") or "")
        qty = _number(order.get("qty")) or 0.0
        price = _number(order.get("price")) or 0.0
        value = qty * price
        order_id = str(order.get("client_order_id")
                       or f"{plan_id}-{symbol}-{side}")
        # FR-EXEC-003 资金检查（**可选维度**）：给了 ``funds`` 才参与判定；不给则与历史逐字段一致。
        # ``funds`` 是 ``v3_analytics.funding_check_data`` 的**同活订单金额**读数（只读 account_funds）。
        order_funds = None
        if callable(funds):
            order_funds = funds(value)
        elif isinstance(funds, dict):
            order_funds = {**funds, "orderValue": value}
        action, reasons = check_order(
            value, context["nav"], context["industry_pct"], context["drawdown_pct"],
            industry_source=context.get("industry_source"),
            industry_top=context.get("industry_top"),
            industry_as_of=context.get("industry_as_of"),
            industry_probe_age_ms=context.get("industry_probe_age_ms"),
            industry_missing=context.get("industry_missing"),
            industry_universe=context.get("industry_universe"),
            industry_market=context.get("industry_market"),
            industry_reason=context.get("industry_reason"),
            funds=order_funds)
        if action == "manual" and context["nav"] <= 0:
            reasons.append(f"NAV 不可用（{context['nav_source']}）：不折算，保守退回人工确认")
        # 行业红线阻断**单独成态**（blocked_industry）：单一行业 20% 与最大回撤 15% 都是硬阻断，
        # 但「哪个规则拦的」必须能一眼看出（否则事后只能靠 reasons 文本猜）。
        stage = {"blocked_industry": "blocked_industry", "blocked": "blocked",
                 "manual": "manual"}.get(action, "risk_passed")
        broker_order_id = order.get("broker_order_id") or None
        status = str(order.get("status") or "")
        hit = open_hit(context["open_rows"], order_id, symbol, side, qty)
        if broker_order_id or hit:
            stage = "submitted"
        if status == "filled":
            stage = "filled"
        if status == "rejected":
            stage = "rejected"

        existing = orders.get(order_id) or {}
        stamp = _now()
        history = list(existing.get("history") or [])
        previous_stage = existing.get("stage")
        trail = {"at": stamp, "stage": stage, "rule": _RULE_OF_ACTION.get(action, "unknown"),
                 "changed": previous_stage != stage, "reasons": list(reasons)}
        trail.update(v3_risk_gate.industry_trail(industry) if industry is not None
                     else {"industry_pct": context["industry_pct"],
                           "industry_source": context["industry_source"]})
        history.append(trail)
        record = {
            "id": order_id,
            "plan_id": plan_id,
            "plan_status": plan_record.get("status"),
            "mode": plan_record.get("mode"),
            "strategy_id": plan_record.get("strategy_id"),
            "ticker": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "value": value,
            "broker_order_id": broker_order_id,
            "stage": stage,
            "risk": {"action": action, "reasons": list(reasons),
                     "rule": _RULE_OF_ACTION.get(action, "unknown"),
                     "industry_source": context.get("industry_source"),
                     "industry_pct": context["industry_pct"],
                     "industry_top": context.get("industry_top"),
                     "industry_as_of": context.get("industry_as_of"),
                     "industry_missing": context.get("industry_missing"),
                     "industry_reason": context.get("industry_reason")},
            "nav_used": context["nav"],
            "nav_source": context["nav_source"],
            "drawdown_used": context["drawdown_pct"],
            "drawdown_source": context["drawdown_source"],
            "industry_pct": context["industry_pct"],
            "industry_source": context["industry_source"],
            "industry_top": context.get("industry_top"),
            "industry_as_of": context.get("industry_as_of"),
            "industry_probe_age_ms": context.get("industry_probe_age_ms"),
            "industry_missing": context.get("industry_missing"),
            "industry_reason": context.get("industry_reason"),
            "open_hit": bool(hit),
            "first_seen_at": existing.get("first_seen_at") or stamp,
            "updated_at": stamp,
            "history": history[-20:],
        }
        if isinstance(order_funds, dict):
            # 只增字段：既有读取方按名取值，不受影响；资金检查没接线时这一项不出现。
            record["funds_check"] = {
                "action": order_funds.get("action"),
                "reason": order_funds.get("reason"),
                "orderValue": order_funds.get("orderValue", value),
                "readings": order_funds.get("readings"),
                "fundingBasisField": order_funds.get("funding_basis_field"),
                "source": order_funds.get("source"),
                "as_of": order_funds.get("as_of"),
                "market": order_funds.get("market"),
                "basisNote": order_funds.get("basisNote"),
            }
        orders[order_id] = record
        return record

    def sync(self, funds=None):
        """重新对账：``plan`` → 登记/分级 → 与 ``orders_open`` 命中 → 落盘。

        行业暴露按**订单所属市场**取（``_industry_contexts``），跨市场不合并；每个市场
        至多现取一次（缓存新鲜则一次上游都不打）。

        ``funds``（FR-EXEC-003 资金检查，**缺省 ``None`` → 行为与历史逐字段一致**）：
        给定时只读调一次 ``account_funds``（同一市场链分组求和），并对每张订单按
        **该单自己的金额**做资金检查（``action="blocked"`` 时该单升级为 ``blocked``，
        原因写进 ``risk.reasons`` 与新增的 ``funds_check`` 字段）。**不触任何写/交易端点**。
        """
        plan = self._call("plan", {})
        if not isinstance(plan, dict) or not plan.get("ok"):
            return {"ok": False, "error": (plan or {}).get("error")
                    or {"code": "wb/error", "message": "plan 取数失败"}}
        value = plan.get("value") if isinstance(plan.get("value"), dict) else {}
        plans = [row for row in (value.get("plans") or []) if isinstance(row, dict)]
        contexts, industry_error, industry_base = self._industry_contexts(plans=plans)
        fund_checker, funds_error = self._funds_checker(funds)
        orders = self.read()
        seen = []
        for plan_record in plans:
            for order in plan_record.get("orders") or []:
                if not isinstance(order, dict):
                    continue
                market = v3_risk_gate.market_of_order(order.get("symbol"))
                industry = contexts.get(v3_risk_gate.industry_context_key(market))
                record = self._upsert(orders, order, context=industry["context"],
                                      plan_record=plan_record, industry=industry,
                                      funds=fund_checker)
                seen.append(record["id"])
        self.write(orders)
        # 结果里的行业读数 = 本次实际读到的各市场里**最严**的那个（一个都没读到 → 如实 null）
        strictest = None
        for item in contexts.values():
            pct = _number(item.get("industry_pct"))
            if pct is None:
                continue
            if strictest is None or pct > _number(strictest["industry_pct"]):
                strictest = item
        base = industry_base["context"]
        result = {"at": _now(), "plans": len(plans), "orders": len(seen),
                  "nav": base["nav"], "nav_source": base["nav_source"],
                  "drawdown_pct": base["drawdown_pct"],
                  "industry_source": (strictest["industry_source"] if strictest
                                      else INDUSTRY_SOURCE),
                  "industry_pct": _number(strictest["industry_pct"]) if strictest else None,
                  "industry_top": strictest.get("top_industry") if strictest else None,
                  "industry_as_of": strictest.get("as_of") if strictest else None,
                  "industry_missing": strictest.get("missing") if strictest else None,
                  "industry_markets": {key: item.get("industry_source")
                                       for key, item in sorted(contexts.items())},
                  "industry_error": industry_error,
                  "funds_check": (None if fund_checker is None
                                  else {"source": "workbench/account_funds",
                                        "error": funds_error,
                                        "note": "逐单按订单金额比对可用购买力；"
                                                "无购买力字段时 action=unknown（不改判定）"}),
                  "stages": self.stage_counts_of(orders)}
        self._append_sync(result)
        return {"ok": True, **result}

    def _funds_checker(self, funds):
        """构造逐单资金检查闭包 ``(order_value) -> 读数 | None``（只读 ``account_funds``）。

        ``funds`` 为 ``None`` → 返回 ``(None, None)``（**不接资金维度**，与历史一致）；
        为 ``False`` 同样不接（显式关闭）；否则现读一次 ``account_funds``（每轮一次，不按订单重复打上游）。
        """
        if funds is None or funds is False:
            return None, None
        envelope = self._call("account_funds", {})
        value = _value_or_none(envelope) or {}
        rows = []
        for group in (value.get("groups") or []):
            if not isinstance(group, dict):
                continue
            market = _sim_market_chain(group.get("market"))
            cash = group.get("cash") if isinstance(group.get("cash"), dict) else {}
            rows.append({
                "market": market,
                "acc_id": group.get("acc_id"),
                "totalAssets": _number(cash.get("total_assets", cash.get("total_asset"))),
                "buyingPower": _first_of(cash, ("power", "max_power_long", "available_funds")),
                "cash": _number(cash.get("cash", cash.get("balance"))),
                "buyingPowerField": _first_field(cash, ("power", "max_power_long",
                                                        "available_funds")),
            })
        source = value.get("source")
        as_of = value.get("as_of")
        error = None
        if not rows:
            error = _error_text(envelope) if not envelope.get("ok") else "资金响应里没有账户分组"
        elif not any(row["buyingPower"] is not None for row in rows):
            error = ("资金读数里没有购买力字段（power / max_power_long / available_funds）→ "
                     "资金检查 no-data（不改既有判定）")

        def checker(order_value):
            scoped = [row for row in rows if row["buyingPower"] is not None]
            return {
                "action": "unknown" if not scoped else (
                    "blocked" if (_number(order_value) or 0.0) > sum(
                        row["buyingPower"] for row in scoped) else "noted"),
                "orderValue": order_value,
                "readings": {"buyingPower": (sum(row["buyingPower"] for row in scoped)
                                             if scoped else None),
                             "cash": (sum(row["cash"] for row in rows
                                          if row["cash"] is not None) or None)},
                "funding_basis_field": sorted({row["buyingPowerField"] for row in scoped
                                               if row["buyingPowerField"]}),
                "accounts": rows,
                "source": source, "as_of": as_of,
                "market": None,
                "basisNote": ("购买力按账户分组求和（不跨市场/币种折算）；"
                              "权益不作可用资金"),
                "reason": (f"订单金额 {order_value} 与可用购买力比对（字段 "
                           f"{sorted({row['buyingPowerField'] for row in scoped if row['buyingPowerField']})}）"
                           if scoped else
                           f"资金读数里没有购买力字段（power/max_power_long/available_funds）："
                           f"{error or 'unknown'}"),
            }

        return checker, error

    # ---- 行业读数：缓存快照（不打上游）与对账分级（可现取一次）----
    def industry_readings(self, market=None, *, max_age_ms=None, now=None):
        """``(逐市场读数, 最严读数)``——**只读落盘缓存**，不调任何工具面函数。

        视图/指标/``/api/v3/metrics`` 都走这条路径，所以刷新看板**永远不会**去探测富途
        （与 ``observability._render_risk_industry`` 同一纪律）。``market`` 给定时
        ``最严读数`` 就是该市场的读数（认不出市场前缀的订单另走
        :meth:`_industry_contexts` 的 ``unknown`` 口径）。
        """
        markets = {}
        for code in v3_universe.MARKETS:
            markets[code] = self.industry_context(code, max_age_ms=max_age_ms, now=now)
        usable = [envelope for envelope in markets.values()
                  if _number(envelope.get("industry_pct")) is not None]
        if market is not None:
            strictest = markets.get(market)
            if strictest is None:
                strictest = self.industry_context(market, max_age_ms=max_age_ms, now=now)
                markets = dict(markets, **{market: strictest})
        else:
            strictest = (max(usable, key=lambda item: _number(item["industry_pct"]))
                         if usable else None)
        return markets, (strictest or self.industry_context(None, max_age_ms=max_age_ms,
                                                            now=now))

    def industry_view_context(self, market=None, *, context=None, now=None):
        """视图/指标用上下文：``context`` 给定（``/api/v3/execution`` 已取过）则复用，
        否则只补行业块（NAV/回撤字段在 ``context`` 为 ``None`` 时**不出值**）。

        行业口径与对账完全同源（``industry_context``），因此**缓存过期 → 视图也会如实
        显示 no-data**，不会拿一个 6 小时前的暴露冒充现读数。
        """
        now = time.time() if now is None else float(now)
        markets, picked = self.industry_readings(market, now=now)
        out = dict(context or {})
        out.update({
            "industry_pct": _number(picked.get("industry_pct")),
            "industry_source": picked.get("industry_source") or INDUSTRY_SOURCE,
            "industry_top": picked.get("top_industry"),
            "industry_market": picked.get("market"),
            "industry_as_of": picked.get("as_of"),
            "industry_probe_age_ms": picked.get("probe_age_ms"),
            "industry_missing": picked.get("missing"),
            "industry_universe": picked.get("universe"),
            "industry_reason": picked.get("reason"),
            "industry": dict(picked),
            "industry_markets": {key: dict(envelope) for key, envelope in markets.items()},
        })
        return out

    def _industry_contexts(self, plans=None, *, wb_call=None, now=None):
        """按**本次对账涉及的市场**取行业读数；返回 ``(contexts, error, base)``。

        ``contexts`` 的键是计划里涉及的 ``SH``/``HK``/``US`` 与 ``unknown``（认不出市场
        前缀的订单 → 取本次读到的各市场**最严**，理由写在读数的 ``reason`` 里、
        不跨市场合并暴露）。只为**计划里真的有订单**的市场现取（每个市场至多一次，
        ``wb_call`` 即 ``v3_run``：只读工具 + 全局限流器，缺省为 ``self._call``）——
        因此「缓存新鲜」时一次上游都不打、「全是 SH 单」时不会顺手去探测 HK/US。
        NAV/回撤来自**同一份**基准上下文（懒取一次，不因市场而异），行业块逐市场替换。
        """
        now = time.time() if now is None else float(now)
        cache = {}
        errors = []
        store = {"base": None}

        def base_context():
            """NAV/回撤基准上下文：**懒取一次**（只有真的要分级/展示时才打工具面）。"""
            if store["base"] is None:
                store["base"] = self.context(now=now)
            return store["base"]

        def wrap(acquired):
            context = dict(base_context())
            context.update({
                "industry_pct": _number(acquired.get("industry_pct")),
                "industry_source": acquired.get("industry_source") or INDUSTRY_SOURCE,
                "industry_top": acquired.get("top_industry"),
                "industry_market": acquired.get("market"),
                "industry_as_of": acquired.get("as_of"),
                "industry_probe_age_ms": acquired.get("probe_age_ms"),
                "industry_missing": acquired.get("missing"),
                "industry_universe": acquired.get("universe"),
                "industry_reason": acquired.get("reason"),
                "industry": dict(acquired),
            })
            key = v3_risk_gate.industry_context_key(acquired.get("market"))
            cache[key] = dict(acquired, context=context)
            if acquired.get("reason") and acquired.get("industry_pct") is None:
                errors.append(f"{key}: {acquired['reason']}")
            return cache[key]

        fetch = self._call if wb_call is None else wb_call
        symbols = [order.get("symbol")
                   for plan_record in (plans or [])
                   for order in (plan_record.get("orders") or [])
                   if isinstance(order, dict)]
        needed = []
        for symbol in symbols:
            market = v3_risk_gate.market_of_order(symbol)
            if market is not None and market not in needed:
                needed.append(market)
        for market in needed:
            wrap(self.industry_context(market, wb_call=fetch, now=now))
        # 认不出市场前缀的订单：在**本次读到的**市场里取最严（不跨市场合并暴露）
        strict = None
        for key, item in cache.items():
            if key == v3_risk_gate.MARKET_UNKNOWN:
                continue
            pct = _number(item.get("industry_pct"))
            if pct is None:
                continue
            if strict is None or pct > _number(strict["industry_pct"]):
                strict = item
        if strict is None:
            fallback = dict(self.industry_context(None, wb_call=None, now=now))
            fallback["reason"] = (fallback.get("reason")
                                  or "本次对账没有可归市场的标的，也未读到任何市场读数")
            cache[v3_risk_gate.MARKET_UNKNOWN] = wrap(fallback)
        else:
            derived = dict(strict)
            derived["market"] = None
            derived["reason"] = (f"订单标的认不出市场前缀 → 取本次读到的各市场最严读数"
                                 f"（{strict.get('market')} 的 {strict.get('top_industry')}）")
            context = dict(strict["context"])
            context["industry_market"] = None
            context["industry_reason"] = derived["reason"]
            context["industry"] = dict(derived)
            derived["context"] = context
            cache[v3_risk_gate.MARKET_UNKNOWN] = derived
        return cache, ("；".join(errors) if errors else None), cache[v3_risk_gate.MARKET_UNKNOWN]

    def view(self, context=None, market=None):
        """OMS 台账视图；``market`` 给定时 ``orders`` 与 ``stages`` 同步按市场过滤。

        ``context`` 给定时（``/api/v3/execution`` 已经取过一份）复用它；否则取一份
        NAV/回撤上下文再补行业块——行业块走**只读落盘缓存**的
        :meth:`industry_view_context`，不会为了看板去探测富途。

        ``orders`` 逐单过 :func:`order_view`：闸门前的历史判定在**视图层**归一化
        （``industry_pct=None`` + ``legacy_pre_gate``），磁盘/库里的原始记录与原始
        ``reasons`` / ``history`` 一个字不动。``/api/v3/execution`` 与本端点同走此函数，
        两处口径不可能漂移。
        """
        if context is None:
            context = self.industry_view_context(context=self.context())
        payload = {
            "note": OMS_NOTE,
            "confirmation": context["confirmation"],
            "nav": context["nav"],
            "nav_source": context["nav_source"],
            "drawdown_pct": context["drawdown_pct"],
            "drawdown_source": context["drawdown_source"],
            "industry_source": context["industry_source"],
            "industry_pct": context["industry_pct"],
            "industry_top": context.get("industry_top"),
            "industry_market": context.get("industry_market"),
            "industry_as_of": context.get("industry_as_of"),
            "industry_missing": context.get("industry_missing"),
            "industry_markets": context.get("industry_markets"),
            "industry_limit_pct": _number(LIMITS["industryPct"]),
            "stages": self.stage_counts(market),
            "orders": [order_view(record) for record in self.list(market)[:20]],
            "market": market,
        }
        if market:
            payload["filter"] = self.market_filter_stats(market)
        return payload

    def gate_view(self):
        """``/api/v3/metrics`` 的行业闸门读数（**只加字段**；抓取路径**不打任何上游**）。

        ``blockedIndustry`` = 台账里 ``stage="blocked_industry"`` 的单数（真被行业红线拦下的
        那些）；``industry`` 块是**落盘缓存**的读数状态（``industryPct=null`` +
        ``industrySource="no-data"`` 表示没有新鲜读数、闸门 fail-open）。抓取路径只读缓存、
        不发起任何工具调用（与 ``observability._render_risk_industry`` 同一纪律）。
        """
        orders = self.read()
        # 只读落盘缓存：**不调用任何工具面函数**（刷新看板绝不去探测富途）
        markets, picked = self.industry_readings()
        return {
            "blockedIndustry": sum(1 for record in orders.values()
                                   if str(record.get("stage")) == "blocked_industry"),
            "blockedByIndustry": sum(1 for record in orders.values()
                                     if str((record.get("risk") or {}).get("rule"))
                                     == "industry-red-line"),
            "blockedByDrawdown": sum(1 for record in orders.values()
                                     if str((record.get("risk") or {}).get("rule"))
                                     == "drawdown-red-line"),
            "industryLimitPct": _number(LIMITS["industryPct"]),
            "industryPct": _number(picked.get("industry_pct")),
            "industrySource": picked.get("industry_source") or INDUSTRY_SOURCE,
            "industryTop": picked.get("top_industry"),
            "industryAsOf": picked.get("as_of"),
            "industryProbeAgeMs": picked.get("probe_age_ms"),
            "industryMissing": picked.get("missing"),
            "perMarket": {key: envelope.get("industry_pct")
                          for key, envelope in sorted(markets.items())},
            "failOpen": _number(picked.get("industry_pct")) is None,
            "note": ("单一行业 > 20% → stage=blocked_industry（硬阻断）；没有新鲜读数时 "
                     "fail-open 不阻断，但订单 reasons 里会写明「数据不可用，未参与阻断」"),
        }



# ---------------------------------------------------------------------------
# 设置页数据源
# ---------------------------------------------------------------------------
def _futu_status(home):
    """``futu_channel`` / MCP Bearer 元信息 / OpenAPI 凭据**键名**（绝不回显值）。"""
    platform_config = _read_json_file(config_path(home))
    raw_channel = platform_config.get("futu_channel") if isinstance(platform_config, dict) else None
    channel = raw_channel if raw_channel in FUTU_CHANNELS else "mcp"

    credential = _read_json_file(Path(home) / CREDENTIAL_FILENAME)
    config_keys = sorted(credential.keys()) if isinstance(credential, dict) else []
    mode = credential.get("mode") if isinstance(credential, dict) else None

    token_present = _read_trim(Path(home) / FUTU_TOKEN_FILENAME) is not None
    expiry = _read_trim(Path(home) / FUTU_TOKEN_EXPIRY_FILENAME)
    return {
        "channel": channel,
        "channel_source": ("trading-platform.json(futu_channel)" if raw_channel in FUTU_CHANNELS
                           else f"默认 mcp（文件值 {raw_channel!r} 缺失或非法）"),
        "mcp_bearer": {"present": bool(token_present), "expiry": expiry},
        "openapi": {"mode": mode, "config_keys": config_keys},
    }


def _env_status(home):
    """``{key, injected, source}``——**只报是否注入与来源，绝不出值**。"""
    rows = []
    for key in ENV_KEYS:
        value = os.environ.get(key)
        if value:
            source = "环境变量"
            injected = True
        elif key == "DSH_HOME":
            source = "默认（DSH_HOME）"
            injected = bool(str(home))
        else:
            source = "环境变量（未注入）"
            injected = False
        rows.append({"key": key, "injected": bool(injected), "source": source})
    return rows


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 数据源**能力探测**（真发一次 HTTP，而不是「某个 Python 包能不能 import」）
#
# 纪律：探测结果必须带 ``as_of`` 与真实失败原因；**任何**「不可用」的结论都要能追到一次
# 真实调用/真实探测，绝不允许写死文本（2026-09-20 修正：SEC 与 Tushare 两条曾与之相反）。
# 探测结果按 TTL 缓存：设置页会被反复打开，不能每次都打上游；TTL 内复用**同一份**结果
# （连它的 ``as_of`` 一起复用），绝不假装刚刚探测过。
# ---------------------------------------------------------------------------
SOURCE_PROBE_TTL_ENV = "QUANT_SOURCE_PROBE_TTL"
DEFAULT_SOURCE_PROBE_TTL = 600.0
SOURCE_PROBE_TIMEOUT_ENV = "QUANT_SOURCE_PROBE_TIMEOUT"
DEFAULT_SOURCE_PROBE_TIMEOUT = 6.0
#: 落盘探测证据的新鲜窗口：超过它仍可用（是真实调用结果），只是会在 detail 里注明年龄。
SOURCE_PROBE_EVIDENCE_MAX_AGE = 3600.0

#: SEC XBRL 探测参数：与 ``GET /api/v3/financials?ticker=AAPL`` 同一条路径（Apple CIK=0000320193）。
SEC_PROBE_CIK = 320193
SEC_PROBE_TAG = "Revenues"

_SOURCE_PROBE_LOCK = threading.Lock()
_SOURCE_PROBE_CACHE = {}


def reset_source_probes():
    """清空能力探测缓存（测试用；生产不需要——TTL 到期自然重探）。"""
    with _SOURCE_PROBE_LOCK:
        _SOURCE_PROBE_CACHE.clear()


def _env_positive_float(name, default):
    raw = os.environ.get(name)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _source_probe_ttl():
    return _env_positive_float(SOURCE_PROBE_TTL_ENV, DEFAULT_SOURCE_PROBE_TTL)


def _source_probe_timeout():
    return _env_positive_float(SOURCE_PROBE_TIMEOUT_ENV, DEFAULT_SOURCE_PROBE_TIMEOUT)


def _http_probe(url, headers=None, timeout=None):
    """真发一次 GET，返回 ``{ok, reachable, status, evidence, as_of}``。

    ``reachable`` = HTTP 层拿到了响应（**4xx/5xx 也算**：端点在、只是路径/鉴权不对）；
    ``ok`` = 拿到了 2xx（才够格把数据源判成可用）。两者分开报：笼统写成「不可用」正是
    本次要修掉的不诚实，而把 403 当成「可用」同样是自欺。
    """
    as_of = _now()
    seconds = _source_probe_timeout() if timeout is None else float(timeout)
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=seconds) as response:  # noqa: S310 —— 公开只读端点
            status = int(getattr(response, "status", 0) or 0)
            chunk = response.read(4096)
    except urllib.error.HTTPError as error:
        return {"ok": False, "reachable": True, "status": int(error.code), "as_of": as_of,
                "evidence": f"HTTP {error.code}（GET {url}）——端点可达但返回了错误码"}
    except Exception as error:  # noqa: BLE001 —— 网络/超时/DNS/证书：原因逐字带回
        return {"ok": False, "reachable": False, "status": None, "as_of": as_of,
                "evidence": f"{type(error).__name__}: {error}"[:200]}
    return {"ok": 200 <= status < 400, "reachable": True, "status": status, "as_of": as_of,
            "evidence": f"HTTP {status}（GET {url}，首段 {len(chunk)} 字节）"}


def _probe_cached(key, probe):
    """带 TTL 的能力探测：TTL 内复用同一份真实结果（``as_of`` 不刷新）。"""
    ttl = _source_probe_ttl()
    now = time.monotonic()
    with _SOURCE_PROBE_LOCK:
        entry = _SOURCE_PROBE_CACHE.get(key)
    if entry is not None and (now - entry["at"]) < ttl:
        return dict(entry["result"])
    result = probe()
    with _SOURCE_PROBE_LOCK:
        _SOURCE_PROBE_CACHE[key] = {"at": now, "result": dict(result)}
    return dict(result)


def _load_datasource_probe(home):
    """读**落盘**的数据源降级链探测结果（写方 = ``GET /api/v3/sources/status``）。

    惰性 import ``server.observability``：它在模块级 import 本模块，顶层 import 会成环。
    读不到不算错误（返回 ``None``）——此时调用方退回实时探测。
    """
    try:
        from server import observability  # noqa: PLC0415 —— 只为打断导入环
        return observability.load_datasource_probe(home)
    except Exception:  # noqa: BLE001 —— 探测缓存的读取绝不阻断设置页
        return None


def _chain_row(probe, key):
    if not isinstance(probe, dict):
        return None
    for row in probe.get("chains") or []:
        if isinstance(row, dict) and row.get("key") == key:
            return row
    return None


def _evidence_age(probe):
    """落盘探测证据的年龄（秒）；没有时间戳返回 ``None``（不猜）。"""
    stamped = probe.get("probed_at") if isinstance(probe, dict) else None
    try:
        return max(0.0, time.time() - float(stamped))
    except (TypeError, ValueError):
        return None


def _age_text(seconds):
    if seconds is None:
        return "时间戳缺失"
    if seconds < 120:
        return f"{int(seconds)} 秒前"
    if seconds < 7200:
        return f"{seconds / 60:.0f} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


def _sec_probe_request():
    """SEC 探测用的 ``(url, headers)``——端点/UA 取自 ``v3_sources``（不另造第二份常量）。"""
    from server import v3_sources  # noqa: PLC0415 —— 惰性 import，避免模块级环
    url = v3_sources.SEC_CONCEPT_URL.format(cik=SEC_PROBE_CIK, tag=SEC_PROBE_TAG)
    return url, {"user-agent": v3_sources.DEFAULT_SEC_UA, "accept": "application/json"}


def _sec_edgar_row(home, http_probe):
    """SEC EDGAR 的可用性：**先认最近一次真实调用命中的 source**，没有证据才真发一次 HTTP。

    为什么这样判：三表美股走 HTTPS ``sec/companyconcept``（``v3_sources.SecSource``），
    与任何 Python 包无关；「本服务未实现」是写死的错话（``GET /api/v3/financials?ticker=AAPL``
    实测返回 ``source=sec/companyconcept(us-gaap XBRL)``）。
    """
    name = "SEC EDGAR（美股 XBRL 公开面）"
    probe = _load_datasource_probe(home)
    row = _chain_row(probe, "financials_us")
    as_of = probe.get("probed_at_iso") if isinstance(probe, dict) else None
    if row is not None and row.get("available"):
        hit = str(row.get("last_source") or "").strip() or "unknown"
        age = _evidence_age(probe)
        stale_note = ""
        if age is None or age > SOURCE_PROBE_EVIDENCE_MAX_AGE:
            stale_note = (f"；注意该证据已过期（{_age_text(age)}），"
                          "重跑 GET /api/v3/sources/status 可刷新")
        return {"name": name, "available": True, "as_of": as_of,
                "source": f"datasource-probe:financials_us→{hit}",
                "detail": (f"最近一次真实调用成功：{hit}（落盘于 {as_of or '时间戳缺失'}，"
                           f"{_age_text(age)}，/api/v3/sources/status 写入）；"
                           f"实现 = HTTPS companyconcept（us-gaap XBRL），"
                           f"核对入口 GET /api/v3/financials?ticker=AAPL"
                           + stale_note)}
    if row is not None:
        age = _evidence_age(probe)
        return {"name": name, "available": False, "as_of": as_of,
                "source": "datasource-probe:financials_us",
                "detail": (f"最近一次真实调用失败（落盘于 {as_of or '时间戳缺失'}，"
                           f"{_age_text(age)}）：{_error_text(row)}；"
                           "这不等于永久不可用——重跑 GET /api/v3/sources/status 会重新探测")}
    url, headers = _sec_probe_request()
    result = _probe_cached("sec-edgar", lambda: http_probe(url, headers=headers))
    if result.get("ok"):
        return {"name": name, "available": True, "as_of": result.get("as_of"),
                "source": "http-probe:data.sec.gov",
                "detail": (f"HTTP 能力探测通过：{result.get('evidence')}；"
                           "（尚无落盘的真实调用证据，故本次为端点可达性验证，"
                           "完整取数以 GET /api/v3/financials?ticker=AAPL 的 source 为准）")}
    if result.get("reachable"):
        return {"name": name, "available": False, "as_of": result.get("as_of"),
                "source": "http-probe:data.sec.gov",
                "detail": (f"HTTP 能力探测未通过：{result.get('evidence')}；"
                           "端点本身可达，失败原因是上面的状态码（可能 UA/权限/路径变化），"
                           "不是「服务未实现」")}
    return {"name": name, "available": False, "as_of": result.get("as_of"),
            "source": "http-probe:data.sec.gov",
            "detail": (f"HTTP 能力探测失败：{result.get('evidence')}；"
                       "失败原因如上（网络/超时/DNS 等），重试或用 "
                       "GET /api/v3/sources/status 刷新落盘的真实调用证据")}


def _data_sources(call, home, http_probe=None):
    """四个数据源的真实可用性：能探测就探测，测不到就说清**为什么**并带上 ``as_of``。

    判据一律是**能力/真实调用结果**，不是包导入（AKShare 一条除外——它在**本进程内**被
    import 调用，importlib 探测与实现语义一致，故保留）。Tushare Pro 行已随
    「数据源政策：需要 token 的渠道一律移除」（2026-09-21）删除。
    """
    probe = http_probe or _http_probe
    checked_at = _now()
    sources = call("sources", {})
    sources_detail = (f"sources 工具：ok={bool(sources.get('ok'))}"
                      + ("" if sources.get("ok") else f"（{_error_text(sources)}）"))
    credential = _read_json_file(Path(home) / CREDENTIAL_FILENAME)
    openapi_mode = credential.get("mode") if isinstance(credential, dict) else None
    return [
        {"name": "workbench 工具面（/api/wb/* 与 MCP /mcp 同一 handle）",
         "available": bool(sources.get("ok")),
         "detail": sources_detail,
         "source": "v3_run:schedule/sources（本次请求实时调用）",
         "as_of": checked_at},
        {"name": "富途 OpenAPI / OpenD",
         "available": isinstance(credential, dict) and openapi_mode in ("oauth", "appkey"),
         "detail": (f"~/{CREDENTIAL_FILENAME}：mode={openapi_mode!r}"
                    f"（{len(credential) if isinstance(credential, dict) else 0} 个键）；"
                    f"channel={_futu_status(home)['channel']}"),
         "source": f"credentials:{CREDENTIAL_FILENAME}",
         "as_of": checked_at},
        {"name": "AKShare（trading-venv 内公开端点）",
         "available": _module_available("akshare"),
         "detail": ("importlib 探测本服务进程内是否可导入 akshare（AKShare 由**本进程**调用，"
                    "故 import 探测与实现语义一致）；真实取数结果见 "
                    "GET /api/v3/sources/status 的 kline/spot 链"),
         "source": "importlib:akshare",
         "as_of": checked_at},
        _sec_edgar_row(home, probe),
    ]


# ---------------------------------------------------------------------------
# brain 的最近一轮策略（优先读库；库空/不可用回退只读 JSONL 冷备）
# ---------------------------------------------------------------------------
def _strategy_run_records(home):
    """``(records, path_label, empty_note)``：主源 = SQLite ``strategy_runs``；否则读 JSONL 冷备。

    ``path_label`` 始终带上 ``v3-strategy-runs.jsonl`` 这个名字（它是这条数据的冷备与
    兼容读路径），库命中时前缀 ``sqlite:`` 如实标注真实来源。``empty_note`` 仅在
    records 为空时有值，且与迁移前的两种文案逐字一致（区分「文件不存在」与「文件存在
    但没有有效记录」）。
    """
    try:
        records = v3_db.list_events(home, "strategy_runs", limit=None, order="asc")
    except Exception:  # noqa: BLE001 —— 库不可用按「库里没有」处理，绝不 500
        records = []
    legacy = Path(home) / STRATEGY_RUNS_FILENAME
    if records:
        return records, f"sqlite:{v3_db.db_path(home)}#strategy_runs（冷备 {legacy}）", None
    try:
        text = legacy.read_text(encoding="utf-8")
    except OSError:
        return [], str(legacy), "文件不存在或不可读"
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    if not out:
        return [], str(legacy), "文件存在但没有有效 JSON 记录"
    return out, str(legacy), None


def last_strategy_run(home, market=None):
    """``(record|None, path, note)``：最近一轮策略流水线记录。

    主源是库里的 ``strategy_runs`` 表（含迁移进来的历史记录），库空/不可用才读
    ``<home>/v3-strategy-runs.jsonl`` 冷备——因此迁移前的行为逐字段不变。

    ``market`` 给定时优先取带该 ``market`` 标注的最后一条记录；**旧记录没有 ``market``
    字段时不被隐藏**——一条带市场的记录都没有，就退回最后一条旧记录并把原因写进
    ``note``（调用方据此标注 ``decisionMarket: null``）。已有带市场的记录、但没有该市场的
    → ``None``（如实说明，不拿别的市场顶替）。
    """
    records, path, empty_note = _strategy_run_records(home)
    if not records:
        return None, path, empty_note or "文件不存在或不可读"
    if market is None:
        return records[-1], path, "取最后一条有效记录"
    matched = [record for record in records
               if str(record.get("market") or "").strip().upper() == market]
    if matched:
        return matched[-1], path, f"取最后一条 market={market} 的记录"
    legacy = [record for record in records if record.get("market") in (None, "")]
    if legacy:
        return legacy[-1], path, (
            f"文件里没有任何带 market 标注的记录（{len(records)} 条旧记录）→ 取最后一条并标注 "
            f"market=null，未按市场隐藏")
    return None, path, (f"文件里有 {len(records)} 条记录，但没有 market={market} 的"
                        f"（不拿其它市场的记录顶替）")


# ---------------------------------------------------------------------------
# 注册（app.py 的自动接线点）
# ---------------------------------------------------------------------------
def register(app, v3_run, home, deps=None):
    """注册 `/api/v3/*` 运维/通道/治理路由。返回内部句柄（app.py 忽略返回值）。

    ``deps`` 仅供测试注入（app.py 只按位置传三个参数，故缺省即生产行为）：
    ``http_probe`` 替换数据源能力探测的真发 HTTP 实现（签名 ``(url, headers=, timeout=)``）。
    """
    deps = deps or {}
    http_probe = deps.get("http_probe")
    home_path = Path(home)
    ledger = OmsLedger(_make_caller(v3_run), home_path)
    catalog = build_catalog()
    total = catalog_total(catalog)
    call = ledger._call  # noqa: SLF001 —— 同一份「计数 + 信封兜底」调用器，不另造第二份
    tool_fields = _TOOL_FIELDS

    async def respond(build):
        """在线程里跑同步取数（阻塞调用让出事件循环），失败一律转 ``{ok:false,error}``。"""
        _count_http_request()  # 请求进入即计数（本模块注册的路由；当前这次也算）
        try:
            content = await asyncio.to_thread(build)
        except Exception as error:  # noqa: BLE001 —— 与工具面口径一致：失败进信封
            _count_http_error()
            return JSONResponse(status_code=200,
                                content=_failure("v3/internal", str(error)[:300]))
        if not isinstance(content, dict):
            _count_http_error()
            return JSONResponse(status_code=200,
                                content=_failure("v3/internal", "响应不是 JSON 对象"))
        if content.get("ok") is False:
            _count_http_error()
        return JSONResponse(status_code=200, content=content)

    # ── 1. 指标 ──────────────────────────────────────────────────────────────
    @app.get("/api/v3/metrics")
    async def v3_metrics():
        def build():
            workbench_up = bool(call("schedule", {}).get("ok"))
            # 工具面**动态真值**（每个请求现读注册表；数量随装配变化而变化，绝不写死常量）
            surface = mcp_tool_surface(app)
            return {
                "ok": True,
                # 既有字段：六域工具目录条目数（mcp_tools.TOOLS 导入枚举 + 5 个本地计算）
                "toolTotal": total,
                "toolDomains": len(DOMAINS),
                # 新增字段：MCP 工具面真值（tools/list 会列出的工具数，含 v3_* 桥接工具）
                "mcpToolTotal": surface["mcp_total"],
                "mcpToolSurface": {"bridge": surface["bridge"], "source": surface["source"],
                                   "domainCatalog": surface["domain_catalog"]},
                "workbenchUp": workbench_up,
                **metrics_snapshot(),
                # 富途限流治理的真实计数（v3_ratelimit 唯一起源；既有字段一字不改，只加这块）
                "futu": v3_ratelimit.metrics_view(),
                # SQLite 持久化层的真实读数（v3_db 唯一起源）：路径/体积/各表行数/读写计数/
                # 已迁移文件。**只加字段**；库不可用时 stats() 内部收敛成 ok=false + error，
                # 绝不因为这一块把 /api/v3/metrics 打成 500。
                "db": v3_db.stats(home_path),
                "oms": ledger.stage_counts(),
                # 行业红线闸门读数（只加字段）：台账里因**单一行业超限**被阻断的单数、
                # 当前参与判定的行业读数与来源（None = 没有新鲜读数 → fail-open，不阻断）。
                "industryGate": ledger.gate_view(),
                "sdk": {"status": "unavailable", "reason": SDK_REASON},
                "generated_at": _now(),
            }
        return await respond(build)

    # ── 2. 通道 / 调度 ───────────────────────────────────────────────────────
    @app.get("/api/v3/gateway")
    async def v3_gateway():
        def build():
            schedule = call("schedule", {})
            value = schedule.get("value") if schedule.get("ok") else None
            value = value if isinstance(value, dict) else {}
            jobs = [row for row in (value.get("jobs") or []) if isinstance(row, dict)]
            heartbeat = value.get("heartbeat") if isinstance(value.get("heartbeat"), dict) else {}
            note = ("工作台 schedule 工具只提供 daemon 心跳（heartbeat）与作业历史（jobs），"
                    "没有「定时规则表」——规则定义在 install/*.timer 与配置里，不在工具面，"
                    "故 rules 恒为空数组；recent 取 schedule.jobs 的真实运行时间。")
            if not schedule.get("ok"):
                note += f" schedule 工具取数失败：{_error_text(schedule)}"
            # MCP 工具面真值：每个请求现读注册表（重启后 v3_* 桥接工具一并计入）
            surface = mcp_tool_surface(app)
            headless = _headless_status(home_path)
            return {
                "ok": True,
                "channels": {
                    "mcp": {
                        "status": "running", "protocol": MCP_PROTOCOL,
                        # 既有字段 ``tools``：语义更正为「MCP 工具面」真值（= ``tools_total``）。
                        # 它历史上报的是六域目录数（77+5），与 ``tools/list`` 的真值不是同一个
                        # 口径——两个数上下并列而字段名不区分，正是本次要修掉的不诚实。
                        "tools": surface["mcp_total"],
                        # 真实 MCP 工具面（``tools/list`` 会列出的工具数，**动态取自注册表**）
                        "tools_total": surface["mcp_total"],
                        # 既有六域工具目录数（平台 77 工具 + 5 个本地计算）——**不是** MCP 面
                        "tools_domain_catalog": surface["domain_catalog"],
                        # 桥接进 MCP 的 ``v3_*`` 工具数（null = 桥未装配，不猜）
                        "tools_bridge": surface["bridge"],
                        "tools_source": surface["source"],
                    },
                    "sdk": {"status": "unavailable", "reason": SDK_REASON,
                            "protocol": SDK_PROTOCOL},
                    # Headless 通道：**已实现**（server/v3_headless.py），这里的 status 说的是
                    # 「本进程有没有把它装配进来」，不再是「有没有这个能力」。字段值全部来自
                    # 真实读数（见 _headless_status）。
                    "headless": {"status": headless["status"], "reason": headless["reason"],
                                 "command": HEADLESS_COMMAND,
                                 "registered": headless["registered"],
                                 "schedulerAlive": headless["schedulerAlive"]},
                },
                "scheduler": {
                    "rules": [],
                    "recent": [{"job": row.get("job"), "ran": row.get("ran")}
                               for row in jobs[:20]],
                    "heartbeat": heartbeat,
                    "kill": bool(value.get("kill")),
                    "halt": bool(value.get("halt")),
                    "critical": bool(value.get("critical")),
                    "note": note,
                },
                "headless": headless,
                "generated_at": _now(),
            }
        return await respond(build)

    # ── 3. 工具目录 ──────────────────────────────────────────────────────────
    @app.get("/api/v3/tools")
    async def v3_tools(request: Request):
        def build():
            domain = (request.query_params.get("domain") or "").strip()
            if domain:
                if domain not in DOMAINS:
                    return _failure("v3/bad-request", f"domain 需为 {'/'.join(DOMAINS)}")
                return {"ok": True, "total": len(catalog[domain]),
                        "domains": {domain: catalog[domain]}}
            return {"ok": True, "total": total,
                    "domains": {name: catalog[name] for name in DOMAINS}}
        return await respond(build)

    # ── 4. 设置 ──────────────────────────────────────────────────────────────
    @app.get("/api/v3/settings")
    async def v3_settings():
        def build():
            mode, mode_error = _read_mode(home_path)
            note = MODE_NOTE
            if mode_error:
                note += f"（模式文件读取失败，trading_mode 记 unknown：{mode_error}）"
            return {
                "ok": True,
                "mode_note": note,
                "trading_mode": mode,
                "futu": _futu_status(home_path),
                "env": _env_status(home_path),
                "data_sources": _data_sources(call, home_path, http_probe=http_probe),
            }
        return await respond(build)

    # ── 5. 执行（持仓 / 在途 / 今日成交 / OMS 台账）────────────────────────────
    @app.get("/api/v3/execution")
    async def v3_execution(market: str = ""):
        """执行面只读视图；``?market=SH|HK|US`` 时按市场账户过滤持仓/在途/今日成交与台账。

        不传 ``market`` → 与历史完全一致（全部市场合并展示，value 原样透传）。
        """
        def build():
            code = None
            if str(market or "").strip():
                code = v3_universe.normalize_market(market)
                if code is None:
                    return _failure("market/bad-market", "market 需为 SH / HK / US")
            context = ledger.context()
            deals = call("deals_today", {})
            errors = [{"tool": name, "error": envelope.get("error")}
                      for name, envelope in (("equity", context["equity"]),
                                             ("positions", context["positions"]),
                                             ("orders_open", context["orders_open"]),
                                             ("confirmation", context["confirmation_envelope"]),
                                             ("deals_today", deals))
                      if not (isinstance(envelope, dict) and envelope.get("ok"))]
            payload = {
                "ok": True,
                "positions": _value_or_none(context["positions"]),
                "orders_open": _value_or_none(context["orders_open"]),
                "deals_today": _value_or_none(deals),
                "oms": ledger.view(context, code),
                "errors": errors,
            }
            if code:
                filters = {}
                for key, envelope in (("positions", context["positions"]),
                                      ("orders_open", context["orders_open"]),
                                      ("deals_today", deals)):
                    value = _value_or_none(envelope)
                    filtered, stats = filter_grouped_value(
                        value, code, note=f"{key} 按账户市场过滤（不跨市场合并）")
                    payload[key] = filtered
                    filters[key] = stats
                payload["market"] = code
                payload["filter"] = filters
            return payload
        return await respond(build)

    # ── 6. OMS 台账 / 对账 ───────────────────────────────────────────────────
    @app.get("/api/v3/oms/orders")
    async def v3_oms_orders(request: Request):
        def build():
            raw = (request.query_params.get("market") or "").strip()
            code = None
            if raw:
                code = v3_universe.normalize_market(raw)
                if code is None:
                    return _failure("market/bad-market", "market 需为 SH / HK / US")
            return {"ok": True, **ledger.view(market=code)}
        return await respond(build)

    @app.post("/api/v3/oms/sync")
    async def v3_oms_sync(request: Request):
        """重新对账（**本地台账动作，不出订单**）。

        ``?funds=1``（或请求体 ``{"funds": true}``）时，额外做一次**只读**的
        FR-EXEC-003 资金检查：逐单按订单金额比对 ``account_funds`` 的真实可用购买力，
        资金不足的单升级为 ``blocked`` 并写进 ``risk.reasons`` / ``funds_check``。
        **缺省不接**（``funds`` 未给 → 与历史逐字段一致）；无论开关如何，本端点都不触达
        任何下单/改单/撤单/切模式通道。
        """
        want_funds = (request.query_params.get("funds") or "").strip().lower() in (
            "1", "true", "yes", "on")
        if not want_funds:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001 —— 空体/坏体按「不给开关」处理
                body = None
            if isinstance(body, dict) and body.get("funds") in (True, 1, "1", "true"):
                want_funds = True
        return await respond(lambda: ledger.sync(funds=want_funds))

    # ── 7. 事件（直接透传 events 工具信封）───────────────────────────────────
    @app.get("/api/v3/events")
    async def v3_events(request: Request):
        def build():
            ticker = (request.query_params.get("ticker") or "").strip()
            if not ticker:
                # 缺省兜底：平台配置里的自选池首只（与既有页面「不传 ticker 也能看事件」一致）
                try:
                    with open(os.path.join(home, "trading-platform.json"), encoding="utf-8") as handle:
                        watchlist = json.load(handle).get("watchlist") or []
                except Exception:  # noqa: BLE001 —— 配置缺失/损坏不阻断，退回明确报错
                    watchlist = []
                ticker = str(watchlist[0]).strip() if watchlist else ""
                if not ticker:
                    return _failure("v3/bad-request", "ticker 必填（如 SH.600519）")
            payload = {"ticker": ticker}
            # 前端查询参数是 window；events 工具面的字段名是 days（同一语义，此处适配）
            days = _query_int(request, "window")
            if days is None:
                days = _query_int(request, "days")
            if days is not None:
                payload["days"] = days
            return _passthrough(call("events", payload))
        return await respond(build)

    # ── 8. 审计链（直接透传 audit 工具信封）──────────────────────────────────
    @app.get("/api/v3/audit")
    async def v3_audit(request: Request):
        def build():
            payload = {}
            window = _query_int(request, "window")
            # audit 工具面无 window 字段（app.py 的 _takes_no_payload 会拒绝非空载荷）：
            # 工具面不接受就不下传，绝不改写成别的字段。
            if window is not None and "window" in tool_fields.get("audit", frozenset()):
                payload["window"] = window
            return _passthrough(call("audit", payload))
        return await respond(build)

    # ── 9. 智能决策（SDK/Headless 无数据源，如实标注）─────────────────────────
    @app.get("/api/v3/brain")
    async def v3_brain(market: str = ""):
        """``?market=`` 只过滤 ``decision``（最近一轮策略）：``run.market`` 匹配才取；
        旧记录没有 ``market`` 字段 → **不按市场隐藏**，取最后一条并标注 ``decisionMarket:null``。
        """
        def build():
            code = None
            if str(market or "").strip():
                code = v3_universe.normalize_market(market)
                if code is None:
                    return _failure("market/bad-market", "market 需为 SH / HK / US")
            decision, decision_path, decision_note = last_strategy_run(home_path, code)
            sources = call("sources", {})
            headless = _headless_status(home_path)
            payload = {
                "ok": True,
                # 与 /api/v3/gateway 同一份真实读数（同一函数，不做第二事实源）
                "headless": headless,
                "sdk": {"status": "unavailable", "reason": SDK_REASON, "serverInfo": None,
                        "route": None, "lastTurn": None, "turns": [], "events": []},
                "decision": decision,
                "decisionMarket": ((decision or {}).get("market") or None)
                                  if isinstance(decision, dict) else None,
                "sources": {
                    "decision": (f"{decision_path}（{decision_note}）" if decision is not None
                                 else f"无数据源（{decision_path}：{decision_note}）"),
                    "sdk": SDK_REASON,
                    "headless": headless["reason"],
                    "workbench": {"ok": bool(sources.get("ok")),
                                  "data": _value_or_none(sources),
                                  "error": sources.get("error")},
                },
            }
            if code:
                payload["market"] = code
                payload["filter"] = {"field": "v3-strategy-runs.jsonl[].market", "market": code,
                                     "note": decision_note}
            return payload
        return await respond(build)

    return {"ledger": ledger, "catalog": catalog, "total": total, "call": call}


def _make_caller(v3_run):
    """把注入的 ``v3_run`` 包成「计数 + 信封兜底」的同步调用器。"""

    def call(name, payload=None):
        started = time.perf_counter()
        try:
            envelope = v3_run(name, payload if isinstance(payload, dict) else {})
        except Exception as error:  # noqa: BLE001 —— v3_run 已兜底，这里再兜一层
            envelope = _failure("v3/tool-failed", str(error)[:300])
        if not isinstance(envelope, dict) or "ok" not in envelope:
            envelope = _failure("v3/tool-failed", f"工具 {name} 返回了非法信封")
        _count_tool(name, bool(envelope.get("ok")), (time.perf_counter() - started) * 1000)
        return envelope

    return call


__all__ = ["register", "build_catalog", "catalog_total", "check_order", "domain_of",
           "filter_grouped_value", "filter_plan_value", "last_strategy_run",
           "metrics_snapshot", "OmsLedger", "open_hit",
           "positions_nav", "reset_counters", "DOMAINS", "ENV_KEYS", "LIMITS", "STAGES",
           "V3_LOCAL_TOOLS", "WB_TOOL_NAMES"]
