"""V3.0 运维 / 通道 / 治理类接口（``/api/v3/{metrics,gateway,tools,settings,execution,
oms/orders,oms/sync,events,audit,brain}``）。

装配方式（app.py 自动接线，本模块不改 app.py）::

    def register(app, v3_run, home): ...

``v3_run(name, payload)`` 是主 agent 注入的回调：按名调用既有 77 工具面的**同一 handle**
（与 HTTP ``/api/wb/*``、MCP ``/mcp`` 同源），返回原始信封 ``{ok, value|error}``。

数据诚实性（逐条，都是本实现的硬约束）
--------------------------------------
* **工具面**：本模块不复制任何业务逻辑，也不硬编码工具名——工具清单来自
  ``server.mcp_tools.TOOLS`` 的导入枚举，域归类复用 ``platform-v3/server/mcp/catalog.mjs``
  的 ``domainOf`` 规则。
* **通道**：本服务**没有** SDK JSON-RPC 通道、也**没有** Headless CLI 子进程通道。
  相关字段一律 ``status="unavailable"`` + ``reason``，绝不用编造的会话/成功率填充
  （``headless.today`` 恒为零计数、``breaker=null``、``last=[]``、``turns=[]``）。
* **执行入口**：本模块**没有任何下单/改单/撤单方法**。执行入口只有一个——工作台 Web 的
  ``plan_execute`` + 人工确认（live 需口令）。V3 只登记、风控分级、对账与展示。
* **NAV 口径安全优先**：``equity.current``（本地模拟台账，单币种）→ 仅当 ``positions``
  只有**单一账户且单一币种**时用其持仓市值合计 → 多账户/多币种**不折算**（NAV=0，
  风控保守退回人工确认，严禁偏宽松）。
* **``/api/v3/audit`` 的 ``window``**：审计链工具面无 ``window`` 字段（app.py 的
  ``_takes_no_payload`` 会拒绝任何非空载荷），故该查询参数在确认工具面不接受时**不下传**，
  响应仍是审计工具的真实信封。
* **进程内计数**：模块级计数器在**每次 v3_run 调用**时累加。``mcp`` 与 ``wb`` 两个视图读
  同一份计数（两处本来就是同一个 handle 的同一批调用，不做第二事实源）；``http`` 只统计
  本模块注册的路由（进程内计数，不引 Prometheus）。

落盘（均在 ``home`` 下，均为 best-effort，写失败不阻断业务）
-----------------------------------------------------------
* ``<home>/v3-oms-orders.json``    —— OMS 台账（订单实体 + 风控分级 + 状态历史，原子写）
* ``<home>/v3-oms-sync.jsonl``     —— 每次对账的追加留痕
* ``<home>/v3-strategy-runs.jsonl``—— **只读**：``/api/v3/brain`` 的最近一轮策略
  （由 v3_analytics 侧写入；本模块只读最后一条有效记录，读不到即 ``decision=null``）

测试：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_ops -v``
"""
import asyncio
import importlib.util
import json
import os
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from server import mcp_tools, store_access
from server.config import config_path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 六大工具域（与 platform-v3/server/mcp/catalog.mjs 的 DOMAINS 逐字一致）
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

#: 风控阈值（与 platform-v3/server/risk.mjs 的 DEFAULT_LIMITS 逐项一致）
LIMITS = {"singlePct": 2.0, "industryPct": 20.0, "drawdownPct": 15.0}

#: OMS 生命周期阶段（与 platform-v3/web/pages/execution.js 的 KANBAN 六态一致）
STAGES = ("risk_passed", "manual", "blocked", "submitted", "filled", "rejected")

#: 设置页环境变量清单（任务书 §4）：只报「是否注入 + 来源」，**绝不出值**
ENV_KEYS = ("DSH_HOME", "DEEPSEEK_API_KEY", "QUANT_MCP_NODE", "QUANT_MCP_SERVER",
            "QUANT_MCP_CWD", "QUANT_MCP_LOG", "FUTU_OPEND_HOST", "FUTU_OPEND_PORT",
            "TUSHARE_TOKEN")

OMS_FILENAME = "v3-oms-orders.json"
OMS_SYNC_FILENAME = "v3-oms-sync.jsonl"
STRATEGY_RUNS_FILENAME = "v3-strategy-runs.jsonl"
CREDENTIAL_FILENAME = "futu-openapi.json"
FUTU_TOKEN_FILENAME = "futu-token"
FUTU_TOKEN_EXPIRY_FILENAME = "futu-token-expiry"
FUTU_CHANNELS = ("openapi", "mcp")

SDK_REASON = "本服务未挂载 SDK JSON-RPC 通道"
HEADLESS_REASON = "本服务未挂载 Headless CLI 子通道（无 dsh --profile headless 子进程调度）"
HEADLESS_COMMAND = 'dsh --profile headless "<task>"'
MCP_PROTOCOL = ("MCP streamable-http（/mcp，SDK 2.2.0 的 streamable_http_app）；"
                "stdio 由 dsh 侧按需拉起")
SDK_PROTOCOL = "换行分帧 JSON-RPC / stdio（未挂载）"

MODE_NOTE = ("模式切换（sim/live）沿用既有工作台 Web 闸门：switch_mode 只接受切到 sim；"
             "sim→live 必须由用户在 Web（默认 http://127.0.0.1:8397）输入口令完成；"
             "V3.0 不另开口子。")
OMS_NOTE = ("执行入口只有一个：既有工作台 Web 的「执行已冻结计划」（plan_execute）+ 人工确认"
            "（live 需口令「确认执行」）。V3 只登记、风控分级、对账与展示，不提供任何下单通道。")
INDUSTRY_SOURCE = "no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）"

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


def _failure(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def _error_text(envelope):
    error = (envelope or {}).get("error") or {}
    return f"{error.get('code') or 'wb/error'}: {error.get('message') or 'unknown'}"


def _value_or_none(envelope):
    return envelope.get("value") if isinstance(envelope, dict) and envelope.get("ok") else None


def _passthrough(envelope):
    """工具信封 → V3 约定（``{ok:true, value}`` → ``{ok:true, data}``；错误原样透传）。

    与 platform-v3/server/index.mjs 的 ``wbValue`` 同形（前端已按 ``data`` 实现）。
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
def check_order(value, nav, industry_pct=0.0, drawdown_pct=0.0, limits=None):
    """返回 ``(action, reasons)``；action ∈ auto / manual / blocked。"""
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
    industry_pct = _number(industry_pct) or 0.0
    if industry_pct > limits["industryPct"]:
        action = "blocked"
        reasons.append(f"行业集中度 {industry_pct:.1f}% > "
                       f"{limits['industryPct']:.0f}% 红线，强制阻断")
    drawdown_pct = abs(_number(drawdown_pct) or 0.0)
    if drawdown_pct >= limits["drawdownPct"]:
        action = "blocked"
        reasons.append(f"回撤 {drawdown_pct:.1f}% 触及 {limits['drawdownPct']:.0f}% 红线，强制阻断")
    return action, reasons


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
# OMS 台账（对照 platform-v3/server/oms.mjs；**无任何下单方法**）
# ---------------------------------------------------------------------------
class OmsLedger:
    """把 ``plan`` 的计划订单登记为平台侧订单，逐单风控分级并与在途订单对账。"""

    def __init__(self, call, home):
        self._call = call
        self._home = Path(home)
        self.orders_path = self._home / OMS_FILENAME
        self.sync_log_path = self._home / OMS_SYNC_FILENAME

    # ---- 持久化 ----
    def read(self):
        raw = _read_json_file(self.orders_path)
        orders = raw.get("orders") if isinstance(raw, dict) else None
        if not isinstance(orders, dict):
            return {}
        return {str(key): record for key, record in orders.items() if isinstance(record, dict)}

    def write(self, orders):
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

    def stage_counts(self):
        return self.stage_counts_of(self.read())

    def list(self):
        records = list(self.read().values())
        records.sort(key=lambda record: str(record.get("updated_at") or ""), reverse=True)
        return records

    # ---- 工作台上下文（NAV / 回撤 / 在途 / 待确认）----
    def context(self):
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

        return {
            "nav": nav,
            "nav_source": nav_source,
            "drawdown_pct": drawdown_pct,
            "drawdown_source": drawdown_source,
            "industry_pct": 0.0,
            "industry_source": INDUSTRY_SOURCE,
            "open_rows": _open_rows(orders_open),
            "equity": equity,
            "positions": positions,
            "orders_open": orders_open,
            "confirmation": confirmation_value,
            "confirmation_envelope": confirmation,
        }

    def _upsert(self, orders, order, context, plan_record):
        plan_id = str(plan_record.get("plan_id") or "")
        symbol = str(order.get("symbol") or "")
        side = str(order.get("side") or "")
        qty = _number(order.get("qty")) or 0.0
        price = _number(order.get("price")) or 0.0
        value = qty * price
        order_id = str(order.get("client_order_id")
                       or f"{plan_id}-{symbol}-{side}")
        action, reasons = check_order(value, context["nav"], context["industry_pct"],
                                      context["drawdown_pct"])
        if action == "manual" and context["nav"] <= 0:
            reasons.append(f"NAV 不可用（{context['nav_source']}）：不折算，保守退回人工确认")
        stage = {"blocked": "blocked", "manual": "manual"}.get(action, "risk_passed")
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
        history.append({"at": stamp, "stage": stage, "reasons": list(reasons)})
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
            "risk": {"action": action, "reasons": list(reasons)},
            "nav_used": context["nav"],
            "nav_source": context["nav_source"],
            "drawdown_used": context["drawdown_pct"],
            "drawdown_source": context["drawdown_source"],
            "industry_pct": context["industry_pct"],
            "industry_source": context["industry_source"],
            "open_hit": bool(hit),
            "first_seen_at": existing.get("first_seen_at") or stamp,
            "updated_at": stamp,
            "history": history[-10:],
        }
        orders[order_id] = record
        return record

    def sync(self):
        """重新对账：``plan`` → 登记/分级 → 与 ``orders_open`` 命中 → 落盘。"""
        plan = self._call("plan", {})
        if not isinstance(plan, dict) or not plan.get("ok"):
            return {"ok": False, "error": (plan or {}).get("error")
                    or {"code": "wb/error", "message": "plan 取数失败"}}
        context = self.context()
        value = plan.get("value") if isinstance(plan.get("value"), dict) else {}
        plans = [row for row in (value.get("plans") or []) if isinstance(row, dict)]
        orders = self.read()
        seen = []
        for plan_record in plans:
            for order in plan_record.get("orders") or []:
                if not isinstance(order, dict):
                    continue
                record = self._upsert(orders, order, context, plan_record)
                seen.append(record["id"])
        self.write(orders)
        result = {"at": _now(), "plans": len(plans), "orders": len(seen),
                  "nav": context["nav"], "nav_source": context["nav_source"],
                  "drawdown_pct": context["drawdown_pct"],
                  "industry_source": context["industry_source"],
                  "stages": self.stage_counts_of(orders)}
        self._append_sync(result)
        return {"ok": True, **result}

    def view(self, context=None):
        context = context if context is not None else self.context()
        return {
            "note": OMS_NOTE,
            "confirmation": context["confirmation"],
            "nav": context["nav"],
            "nav_source": context["nav_source"],
            "drawdown_pct": context["drawdown_pct"],
            "drawdown_source": context["drawdown_source"],
            "industry_source": context["industry_source"],
            "stages": self.stage_counts(),
            "orders": self.list()[:20],
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


def _data_sources(call, home):
    """五个数据源的真实可用性（能探测就探测，测不到就说测不到）。"""
    sources = call("sources", {})
    sources_detail = (f"sources 工具：ok={bool(sources.get('ok'))}"
                      + ("" if sources.get("ok") else f"（{_error_text(sources)}）"))
    credential = _read_json_file(Path(home) / CREDENTIAL_FILENAME)
    openapi_mode = credential.get("mode") if isinstance(credential, dict) else None
    tushare_token = bool(os.environ.get("TUSHARE_TOKEN"))
    return [
        {"name": "workbench 工具面（/api/wb/* 与 MCP /mcp 同一 handle）",
         "available": bool(sources.get("ok")),
         "detail": sources_detail},
        {"name": "富途 OpenAPI / OpenD",
         "available": isinstance(credential, dict) and openapi_mode in ("oauth", "appkey"),
         "detail": (f"~/{CREDENTIAL_FILENAME}：mode={openapi_mode!r}"
                    f"（{len(credential) if isinstance(credential, dict) else 0} 个键）；"
                    f"channel={_futu_status(home)['channel']}")},
        {"name": "AKShare（trading-venv 内公开端点）",
         "available": _module_available("akshare"),
         "detail": "importlib 探测本服务进程内是否可导入 akshare；实际取数由工作台/分析层执行"},
        {"name": "SEC EDGAR（美股 XBRL 公开面）",
         "available": False,
         "detail": "无数据源：本服务未实现 SEC EDGAR 客户端（不发起该外部调用，也不估算）"},
        {"name": "Tushare Pro",
         "available": tushare_token and _module_available("tushare"),
         "detail": (f"TUSHARE_TOKEN {'已注入' if tushare_token else '未注入'}；"
                    f"tushare 包 {'可导入' if _module_available('tushare') else '不可导入'}")},
    ]


# ---------------------------------------------------------------------------
# brain 的最近一轮策略（只读 v3_analytics 的落盘，读不到就 null）
# ---------------------------------------------------------------------------
def last_strategy_run(home):
    """``(record|None, path, note)``：读 ``v3-strategy-runs.jsonl`` 最后一条有效记录。"""
    path = Path(home) / STRATEGY_RUNS_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, str(path), "文件不存在或不可读"
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            return record, str(path), "取最后一条有效记录"
    return None, str(path), "文件存在但没有有效 JSON 记录"


# ---------------------------------------------------------------------------
# 注册（app.py 的自动接线点）
# ---------------------------------------------------------------------------
def register(app, v3_run, home):
    """注册 `/api/v3/*` 运维/通道/治理路由。返回内部句柄（app.py 忽略返回值）。"""
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
            return {
                "ok": True,
                "toolTotal": total,
                "toolDomains": len(DOMAINS),
                "workbenchUp": workbench_up,
                **metrics_snapshot(),
                "oms": ledger.stage_counts(),
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
            return {
                "ok": True,
                "channels": {
                    "mcp": {"status": "running", "protocol": MCP_PROTOCOL, "tools": total},
                    "sdk": {"status": "unavailable", "reason": SDK_REASON,
                            "protocol": SDK_PROTOCOL},
                    "headless": {"status": "unavailable", "reason": HEADLESS_REASON,
                                 "command": HEADLESS_COMMAND},
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
                "headless": {"today": {"total": 0, "success": 0, "failed": 0, "avgMs": 0,
                                       "killed": 0},
                             "breaker": None, "last": [],
                             "status": "unavailable", "reason": HEADLESS_REASON},
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
                "data_sources": _data_sources(call, home_path),
            }
        return await respond(build)

    # ── 5. 执行（持仓 / 在途 / 今日成交 / OMS 台账）────────────────────────────
    @app.get("/api/v3/execution")
    async def v3_execution():
        def build():
            context = ledger.context()
            deals = call("deals_today", {})
            errors = [{"tool": name, "error": envelope.get("error")}
                      for name, envelope in (("equity", context["equity"]),
                                             ("positions", context["positions"]),
                                             ("orders_open", context["orders_open"]),
                                             ("confirmation", context["confirmation_envelope"]),
                                             ("deals_today", deals))
                      if not (isinstance(envelope, dict) and envelope.get("ok"))]
            return {
                "ok": True,
                "positions": _value_or_none(context["positions"]),
                "orders_open": _value_or_none(context["orders_open"]),
                "deals_today": _value_or_none(deals),
                "oms": ledger.view(context),
                "errors": errors,
            }
        return await respond(build)

    # ── 6. OMS 台账 / 对账 ───────────────────────────────────────────────────
    @app.get("/api/v3/oms/orders")
    async def v3_oms_orders():
        return await respond(lambda: {"ok": True, **ledger.view()})

    @app.post("/api/v3/oms/sync")
    async def v3_oms_sync():
        return await respond(ledger.sync)

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
    async def v3_brain():
        def build():
            decision, decision_path, decision_note = last_strategy_run(home_path)
            sources = call("sources", {})
            return {
                "ok": True,
                "headless": {"today": {"total": 0, "success": 0, "failed": 0, "avgMs": 0,
                                       "killed": 0},
                             "breaker": None, "last": [],
                             "status": "unavailable", "reason": HEADLESS_REASON},
                "sdk": {"status": "unavailable", "reason": SDK_REASON, "serverInfo": None,
                        "route": None, "lastTurn": None, "turns": [], "events": []},
                "decision": decision,
                "sources": {
                    "decision": (f"{decision_path}（{decision_note}）" if decision is not None
                                 else f"无数据源（{decision_path}：{decision_note}）"),
                    "sdk": SDK_REASON,
                    "headless": HEADLESS_REASON,
                    "workbench": {"ok": bool(sources.get("ok")),
                                  "data": _value_or_none(sources),
                                  "error": sources.get("error")},
                },
            }
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
           "last_strategy_run", "metrics_snapshot", "OmsLedger", "open_hit",
           "positions_nav", "reset_counters", "DOMAINS", "ENV_KEYS", "LIMITS", "STAGES",
           "V3_LOCAL_TOOLS", "WB_TOOL_NAMES"]
