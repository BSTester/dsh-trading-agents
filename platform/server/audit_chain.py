# WP6 补遗任务 B2：审计链路 Python 移植。逐行对应 plugins/workbench/src/audit.js（188 行）。
#
#   audit.js:13        SIGNAL_WINDOW_MS   -> SIGNAL_WINDOW_MS
#   audit.js:16        ACTION_KIND        -> ACTION_KIND
#   audit.js:18-23     toMs               -> _to_ms()
#   audit.js:25-27     upper              -> _upper()
#   audit.js:35-60     extractBrokerFields -> extract_broker_fields()
#   audit.js:67-188    buildAuditChain    -> build_audit_chain()
#
# 依赖逐个移植：businessData/actionLabel/toolName/ORDER_SOURCE 来自 summary.py（= broker_trades.js），
# labeled/zh 来自 labels.py（= labels.js:11）。纯标准库、无 IO。
#
# 有意差异（诚实边界，均与 Node 行为区分并已核对）：
#   1. 循环引用：audit.js:39-41 用 `JSON.parse(text)` 试探 MCP 信封，遇到循环对象会抛
#      RangeError（栈溢出）被 catch 吞掉；Python 的 json.loads 只吃字符串、永不栈溢出。
#      本实现两处都加了幂等「已访问」集合（audit.js:41 visited < 200 的广度优先），
#      把 JS 的「递归爆栈后放弃」变成「有界终止」，输出仍与 JS 一致（JS 那两个分支最终
#      也只产出 ticker/status/orderId = null）。
#   2. toMs：JS `Date.parse` 还接受 RFC 2822、`2026/09/11` 等宽松格式；本实现只认
#      ISO-8601（含日期、可选时间与 Z/偏移），与 store_access._parse_ms 的取舍一致。
#      审计链的时间只来自预览/台账的 date 与 activity 的 at，均为 Node 写的 ISO 串。
#   3. `Number.prototype.toFixed(2)` 在 Python 里用 f"{x:.2f}" 等价表示（两者都按
#      十进制最近舍入处理二进制双精度值）；非有限值单独按 JS 的 "Infinity"/"NaN" 文案输出。
#   4. 空对象真值：JS 里 `{}` 为真、`[]` 为真；Python 里 bool({}) / bool([]) 为假。
#      因此 signal 预览与 `p.value` 的过滤统一走 _truthy()（空容器按 JS 语义为真），
#      其余布尔位置沿用显式 None 判断（entry.ticker !== null 等）。
#   5. activity 里的非对象元素（null/字符串/数字）：JS 对 null 会在 audit.js:109 的
#      `a.value` 上抛 TypeError（与 broker_trades.js:163 同一类问题），本实现显式跳过并按
#      「既非动作、也非订单事实」过滤，服务进程不因一条脏记录 500。
#   6. `Math.round` 用 _js_round（半数向 +∞），Python 内置 round() 是银行家舍入。
#   7. maxEntries：Node 里显式传 null 不触发默认参数、`.slice(0, null)` 得空数组；Python 侧
#      None 一律视作缺省 120（HTTP/JSON 层无法区分「未传」与「传 null」）。
import json
import math
import re
from datetime import datetime, timezone

from server.labels import labeled
from server.summary import ORDER_SOURCE, action_label, business_data, tool_name

SIGNAL_WINDOW_MS = 7 * 24 * 3600 * 1000

# audit.js:67 默认参数 `maxEntries = 120`（仅 undefined 触发默认值，null 会走 nullish 分支报错；
# Python 侧 None 一律视作缺省，见 _max_entries()）。
DEFAULT_MAX_ENTRIES = 120

# audit.js:16：影响券商状态的动作 → 时间线上的标签。
ACTION_KIND = {"下单": "order", "改单": "order-modify", "撤单": "order-cancel"}

# audit.js:101：只排除**明确**的只读查询后缀；命名未知的订单工具宁可保留。
READ_ONLY_QUERY = re.compile(r"(_info|_query|_summary|_accounts?|_accts?)$")

_MARKET_PREFIX = re.compile(r"^(SH|SZ|HK|US)\.", re.I)
_MARKET_SUFFIX = re.compile(r"\.(SH|SZ|HK|US)$", re.I)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")

# audit.js:45-52 的字段别名（小写比较）。
_TICKER_KEYS = ("code", "symbol", "ticker", "stock_code")
_STATUS_KEYS = ("status", "order_status", "state")
_ORDER_ID_KEYS = ("order_id", "orderid", "id")


def _truthy(value):
    """JS 真值语义（有意差异 4：空对象/空数组在 JS 里为真）。"""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    return True


def _field(row, key):
    """JS `row.key` 语义：非对象的行取不到字段（undefined -> None）。"""
    if isinstance(row, dict):
        return row.get(key)
    return None


def _js_nullish(*values):
    """JS `a ?? b`：仅 None（undefined/null）触发回退，假值（0/""/False）不回退。"""
    for value in values:
        if value is not None:
            return value
    return None


def _template(value):
    """JS 模板字符串 `${value}` / `String(value)`：null -> "null"、布尔 -> "true"/"false"。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, float):
        if not math.isfinite(value):
            return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        if value.is_integer():
            return str(int(value))
    return str(value)


def _stringify(value):
    """`${parsed.ret_msg ?? ""}` 等带空值合并的插值：None -> ""，其余同 _template()。"""
    if value is None:
        return ""
    return _template(value)


def _to_ms(value):
    """audit.js:18-23 toMs：可解析返回毫秒（float），否则 None（NaN）。

    有意差异 2：只认 ISO-8601；长度为 10 的日期串按 UTC 零点解析。
    """
    if not _truthy(value):
        return None
    text = _stringify(value)
    candidate = f"{text}T00:00:00Z" if _ISO_DATE.match(text) else text
    if not _ISO_DATETIME.match(candidate):
        return None
    try:
        moment = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() * 1000


def _upper(value):
    """audit.js:25-27：仅字符串 trim + 大写；其余（含 NULL）为 None。"""
    if isinstance(value, str):
        return value.strip().upper()
    return None


def extract_broker_fields(value):
    """audit.js:35-60 extractBrokerFields：从券商响应里尽力取标的与状态。

    只做浅层广度扫描（不做深递归），但**先按 MCP 信封解开**——业务 JSON 是字符串，
    包在 value.content[].text 里；直接扫 value 永远扫不到 symbol。
    """
    found = {"ticker": None, "status": None, "orderId": None}
    unwrapped = business_data({"value": value})
    queue = [unwrapped["data"] if unwrapped["ok"] else value]
    visited = 0
    seen = set()  # 有意差异 1：防环（JS 靠长度 / JSON.parse 爆栈兜底）
    while queue and visited < 200:
        node = queue.pop(0)
        visited += 1
        if not isinstance(node, (dict, list)):
            continue  # JS: typeof node !== "object" 或 null -> continue（无键可枚举）
        marker = id(node)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(node, list):
            items = list(enumerate(node))
        else:
            items = list(node.items())
        for key, item in items:
            lower = str(key).lower()
            if (found["ticker"] is None and lower in _TICKER_KEYS and isinstance(item, str)):
                found["ticker"] = _upper(_MARKET_SUFFIX.sub(
                    "", _MARKET_PREFIX.sub("", item)))
            if found["status"] is None and lower in _STATUS_KEYS and isinstance(item, str):
                found["status"] = item
            if (found["orderId"] is None and lower in _ORDER_ID_KEYS
                    # JS: `id` 只在字符串/数字时才采用（数组/对象不取）
                    and (isinstance(item, (str, int, float)) and not isinstance(item, bool))):
                found["orderId"] = _template(item)
            if isinstance(item, (dict, list)):
                queue.append(item)
    return found


def _fixed2(value):
    """audit.js:92 `${(t.return * 100).toFixed(2)}%`：两位小数，非有限值按 JS 文案。"""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return f"{value:.2f}"


def _js_round(value):
    """JS `Math.round`：半数**向 +∞**取整（audit.js:161 的 `Math.round(...)`）。"""
    if math.isnan(value) or math.isinf(value):
        return value
    return math.floor(value + 0.5)


def _max_entries(value):
    """audit.js:67 `maxEntries = 120` 的默认值与切片语义（None -> 120）。"""
    if value is None:
        return DEFAULT_MAX_ENTRIES
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_MAX_ENTRIES
    return int(value)


def build_audit_chain(snapshot, trades, max_entries=None):
    """audit.js:67-188 buildAuditChain：把「信号 → 下单 → 成交」串成可追溯的时间线。

    :param snapshot: store.snapshot() 的结果（用 previews 与 activity）
    :param trades: 本地模拟台账（analytics.trades() 的结果，用 .trades）
    :param max_entries: 时间线最多保留多少条（audit.js:167 `.slice(0, maxEntries)`），默认 120
    :returns: {"entries", "stats"}
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    trades = trades if isinstance(trades, dict) else {}

    previews = _field(snapshot, "previews")
    previews = previews if isinstance(previews, list) else []
    signals = []
    for index, preview in enumerate(previews):
        if not isinstance(preview, dict) or preview.get("kind") != "signal":
            continue
        if not _truthy(preview.get("value")):
            continue
        value = preview["value"]
        strategy = _field(value, "strategy_label") or _field(value, "strategy")
        signals.append({
            "id": _js_nullish(_field(preview, "id"), f"signal-{index}"),
            "kind": "signal",
            "at": _js_nullish(_field(preview, "at")),
            "atMs": _to_ms(_field(preview, "at")),
            "ticker": _upper(_field(value, "ticker")),
            "detail": (f"{labeled(_field(value, 'signal'), _field(value, 'signal_label'), 'SIGNAL')}"
                       f" @ {_js_nullish(_field(value, 'price'), '—')}"
                       + (f" · {strategy}" if strategy else "")),
            "source": "quant_signal",
            "source_label": "量化信号",
            "origin": True,
        })
    signals = [entry for entry in signals if entry["ticker"] is not None]

    ledger = _field(trades, "trades")
    ledger = ledger if isinstance(ledger, list) else []
    fills = []
    for index, trade in enumerate(ledger):
        date = _field(trade, "date")
        fee = _field(trade, "fee")
        profit = _field(trade, "return")
        detail = (f"{labeled(_field(trade, 'action'), _field(trade, 'action_label'), 'ACTION')}"
                  f" {_template(_field(trade, 'shares'))} @ {_template(_field(trade, 'price'))}"
                  + (f" · 费用 {_template(fee)}" if fee is not None else "")
                  + (f" · 收益 {_fixed2(profit * 100)}%" if profit is not None else ""))
        fills.append({
            "id": f"fill-{index}-{_stringify(date)}",   # `${t.date ?? ""}`
            "kind": "fill",
            "at": _js_nullish(date),
            "atMs": _to_ms(date),
            "ticker": _upper(_field(trade, "ticker")),
            "detail": detail,
            "reason": _js_nullish(_field(trade, "reason")),
            "source": "local-ledger",
        })

    activity = _field(snapshot, "activity")
    activity = activity if isinstance(activity, list) else []
    orders = []
    for entry in activity:
        if not isinstance(entry, dict):
            # JS 里非对象行取 .tool 得 undefined -> toolName ""，且 order 命名与 ORDER_SOURCE
            # 都不命中，必定被过滤掉；显式跳过等价，且省掉一次无意义的信封解析。
            continue
        name = tool_name(entry)
        action = action_label(name)
        is_facts = bool(ORDER_SOURCE.search(name))
        order_named = bool(re.search(r"order", name, re.I))
        if action is None and not is_facts and not (order_named and not READ_ONLY_QUERY.search(name)):
            continue
        # audit.js:109 `a.value ?? a`
        raw = _js_nullish(_field(entry, "value"), entry)
        fields = extract_broker_fields(raw)
        if _truthy(_field(entry, "is_error")):
            kind = "order-error"
        elif is_facts and action is None:
            kind = "order-facts"
        else:
            kind = ACTION_KIND.get(action) if action is not None else "order-other"
        detail = (f"{name}"
                  + (f" · #{fields['orderId']}" if fields["orderId"] else "")
                  + (f" · status={fields['status']}" if fields["status"] else ""))
        orders.append({
            "id": _field(entry, "id"),
            "kind": kind,
            "action": action if action is not None else ("订单查询" if is_facts else "订单工具"),
            "at": _js_nullish(_field(entry, "at")),
            "atMs": _to_ms(_field(entry, "at")),
            "order_id": fields["orderId"],
            "ticker": fields["ticker"],
            "detail": detail,
            "source": "broker-observed",
            "source_label": "券商响应（Harness 观察）",
        })

    # 下单/改单/撤单的响应只回 order_id，不带 symbol；按订单号从订单事实里补全标的。
    ticker_by_order = {}
    for entry in orders:
        if entry["order_id"] and entry["ticker"]:
            ticker_by_order[entry["order_id"]] = entry["ticker"]
    for entry in orders:
        if not entry["ticker"] and entry["order_id"] and entry["order_id"] in ticker_by_order:
            entry["ticker"] = ticker_by_order[entry["order_id"]]
            entry["ticker_from"] = "order_id"

    def link_to(entry):
        """audit.js:142-152：同标的、信号时间在前且间隔 ≤ 7 天，取最近的一个。"""
        if entry["ticker"] is None or entry["atMs"] is None:
            return None
        best = None
        for signal in signals:
            if signal["ticker"] != entry["ticker"] or signal["atMs"] is None:
                continue
            delta = entry["atMs"] - signal["atMs"]
            if delta < 0 or delta > SIGNAL_WINDOW_MS:
                continue
            if best is None or signal["atMs"] > best["atMs"]:
                best = signal
        return best

    decorated = []
    for entry in [*fills, *orders]:
        signal = link_to(entry)
        decorated.append({
            **entry,
            "signal_id": signal["id"] if signal is not None else None,
            "signal_at": signal["at"] if signal is not None else None,
            "linked": signal is not None,
            "lag_hours": (_js_round((_field(entry, "atMs") - signal["atMs"]) / 3600000)
                          if signal is not None and _field(entry, "atMs") is not None else None),
        })

    # `(b.atMs ?? 0) - (a.atMs ?? 0)`：None 视作 0；稳定排序保持同值项的原始先后。
    entries = sorted([*signals, *decorated],
                     key=lambda row: (row["atMs"] is None,
                                      -(row["atMs"] if row["atMs"] is not None else 0)))
    entries = entries[:_max_entries(max_entries)]

    linked = len([entry for entry in decorated if entry["linked"]])
    order_kinds = {}
    for entry in orders:
        label = entry["action"] if entry["action"] is not None else "其他"
        order_kinds[label] = order_kinds.get(label, 0) + 1
    return {
        "entries": entries,
        "stats": {
            "signals": len(signals),
            "orders": len(orders),
            "order_kinds": order_kinds,
            "fills": len(fills),
            "linked": linked,
            "unlinked": len(decorated) - linked,
            "link_rule": "同标的、信号时间在前且间隔 ≤ 7 天",
        },
    }


__all__ = ["ACTION_KIND", "READ_ONLY_QUERY", "SIGNAL_WINDOW_MS", "build_audit_chain",
           "extract_broker_fields"]
