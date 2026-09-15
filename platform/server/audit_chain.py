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
# labeled/zh 来自 labels.py（= labels.js:11）。JS 语义助手统一收在 server/_js.py。纯标准库、无 IO。
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
#   3. `Number.prototype.toFixed(2)` 用 _js.fixed()：与 Python 的 f"{x:.2f}" **不等价**——
#      恰为平局时 JS 取较大的 n（`(0.125).toFixed(2)` 是 "0.13"），Python 是银行家舍入
#      （f"{0.125:.2f}" 是 "0.12"）。差分用例 to_fixed_tie 钉住这一点。
#   4. 空对象真值：JS 里 `{}` 为真、`[]` 为真；Python 里 bool({}) / bool([]) 为假。
#      因此 signal 预览与 `p.value` 的过滤统一走 _js.truthy()（空容器按 JS 语义为真），
#      其余布尔位置沿用显式 None 判断（entry.ticker !== null 等）。
#   5. activity 里的非对象元素（null/字符串/数字）**在 JS 里也不会走到 audit.js:109**：
#      audit.js:104-108 先算 name=""/action=null/isFacts=false/orderNamed=false，第 108 行就
#      返回 null 被 `.filter(Boolean)` 滤掉（`toolName(a)` 用可选链，不会抛）。audit.js:109 的
#      `a.value` 只对**通过了 108 行**的元素求值，那时 a 必是对象。所以本实现显式跳过非对象
#      元素与 Node 的过滤等价，不是「比 Node 更保守」，服务进程也不会因一条脏记录 500。
#      （真正的 TypeError 在 broker_trades.js:87/163：`raw.qty`、`entry.is_error` 没有可选链，
#      见 summary.py 有意差异 9。）
#   6. `Math.round` 用 _js.js_round（半数向 +∞），Python 内置 round() 是银行家舍入。
#   7. maxEntries：Node 的默认参数只在 undefined 时生效，显式 null 得空数组；Python 侧
#      None 也按「没传」处理（120）。这是为了调用方安全——`payload.get("maxEntries")` 在
#      客户端没传字段时同样是 None，当 null 用会让最常见的缺省静默变成空时间线；
#      需要 null 语义的调用方传 0。其余取值（字符串/布尔/小数/NaN/负数）按 JS 强转。
#   8. signal 的 `signal-${index}` 兜底 id 用**过滤后**数组的下标（audit.js:68-70 先 filter
#      再 map），本实现同样先收集候选再编号。
import math
import re
from datetime import datetime, timedelta, timezone

from server import _js
from server.labels import labeled
from server.summary import ORDER_SOURCE, action_label, business_data, tool_name

SIGNAL_WINDOW_MS = 7 * 24 * 3600 * 1000

# audit.js:67 默认参数 `maxEntries = 120`：仅 undefined 触发默认值，显式 null 走
# `.slice(0, null)` -> 空数组；Python 侧 None == 缺省（有意差异 7，见 _slice_entries()）。
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

# `Date.parse` 的零点；时间值用 timedelta 整数除法算，避免浮点毫秒（补遗 B F7）
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _to_ms(value):
    """audit.js:18-23 toMs：可解析返回毫秒（int），否则 None（NaN）。

    有意差异 2：只认 ISO-8601；长度为 10 的日期串按 UTC 零点解析。

    返回值一定是 int：`Date.parse` 的时间值本来就是整数毫秒，亚毫秒部分按
    `base + floor(frac)` 落到毫秒（`Math.floor` 语义，1970 年前同样向下取整）——
    用 datetime 的差做整数除法，避免 `timestamp() * 1000` 的浮点尾巴（补遗 B F7）。
    """
    if not _js.truthy(value):
        return None
    text = _js.stringify(value)
    candidate = f"{text}T00:00:00Z" if _ISO_DATE.match(text) else text
    if not _ISO_DATETIME.match(candidate):
        return None
    try:
        moment = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # 整数毫秒：亚毫秒部分向下取整（`base + floor(frac)`，与 Date.parse 一致）
    return (moment - _EPOCH) // timedelta(milliseconds=1)


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
            # audit.js:45 `Object.entries(node)`：整数样式键提前并按数值升序枚举，扫到哪个
            # 字段先赋值的次序因此不同（差分用例 fields.numeric_string_keys 钉住）。
            items = _js.ordered_items(node)
        for key, item in items:
            lower = _js.js_key_string(key).lower()
            if (found["ticker"] is None and lower in _TICKER_KEYS and isinstance(item, str)):
                found["ticker"] = _upper(_MARKET_SUFFIX.sub(
                    "", _MARKET_PREFIX.sub("", item)))
            if found["status"] is None and lower in _STATUS_KEYS and isinstance(item, str):
                found["status"] = item
            if (found["orderId"] is None and lower in _ORDER_ID_KEYS
                    # JS: `id` 只在字符串/数字时才采用（数组/对象不取）
                    and (isinstance(item, (str, int, float)) and not isinstance(item, bool))):
                found["orderId"] = _js.template(item)
            if isinstance(item, (dict, list)):
                queue.append(item)
    return found


def _slice_entries(entries, value):
    """audit.js:167 `.slice(0, maxEntries)`：JS 的 ToIntegerOrInfinity + slice 端点语义。

    有意差异 7：None/undefined 都按「没传」处理 -> 默认 120（JS 只在 undefined 时取默认
    120，显式 null 是 `.slice(0, null)` -> 空数组）。理由不是"无法区分"，而是调用方的
    安全默认：HTTP/JSON 处理函数最常见的写法是 `payload.get("maxEntries")`，客户端**没传**
    该字段时拿到的就是 None；若把 None 当 JS 的 null，最常见的「没传」会静默变成空时间线。
    要显式表达 null 语义的调用方传 0（结果与 JS 的 null 完全一致）。
    除此之外的取值都按 JS 强转：`"2"` -> 2、小数截断、NaN/空数组 -> 0、负数由切片端点处理。
    """
    if value is None or value is _js.UNDEFINED:
        return entries[:DEFAULT_MAX_ENTRIES]
    number = _js.to_number(value)   # Number("2")=2、Number(null)=0、Number([])=0、Number({})=NaN
    if math.isnan(number) or number == 0:
        return []
    if math.isinf(number):
        return entries if number > 0 else []
    return entries[:math.trunc(number)]   # 负端点由 Python 切片规则得到与 slice 相同的结果


def build_audit_chain(snapshot, trades, max_entries=None):
    """audit.js:67-188 buildAuditChain：把「信号 → 下单 → 成交」串成可追溯的时间线。

    :param snapshot: store.snapshot() 的结果（用 previews 与 activity）
    :param trades: 本地模拟台账（analytics.trades() 的结果，用 .trades）
    :param max_entries: 时间线最多保留多少条（audit.js:167 `.slice(0, maxEntries)`）；
        缺省/None -> 120（有意差异 7），要 JS 的 `null` 语义传 0
    :returns: {"entries", "stats"}
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    trades = trades if isinstance(trades, dict) else {}

    previews = _js.field(snapshot, "previews")
    previews = previews if isinstance(previews, list) else []
    # audit.js:68-70：先 `.filter(...)` 再 `.map((p, index) => ...)`，所以兜底 id 的下标是
    # **过滤后**的位置（有意差异 8），不是原数组下标。
    candidates = []
    for preview in previews:
        if not isinstance(preview, dict) or _js.field(preview, "kind") != "signal":
            continue
        value = _js.field(preview, "value")
        if not _js.truthy(value) or not isinstance(value, (dict, list)):
            continue
        candidates.append(preview)
    signals = []
    for index, preview in enumerate(candidates):
        value = _js.field(preview, "value")
        # `${p.value.strategy_label || p.value.strategy ? ` · ${p.value.strategy_label ?? p.value.strategy}` : ""}`
        # `||` 是**真值**判断（空数组为真，故 `[] || "ma_cross"` 取 `[]`），`??` 只看 null/undefined
        # ——两者不可混用（补遗 B F10，差分用例 falsy_strategy_label）。
        strategy_label = _js.field(value, "strategy_label")
        strategy_fallback = _js.field(value, "strategy")
        detail = (f"{_js.template(labeled(_js.field(value, 'signal'), _js.field(value, 'signal_label'), 'SIGNAL'))}"
                  f" @ {_js.template(_js.js_nullish(_js.field(value, 'price'), '—'))}")
        if _js.truthy(strategy_label) or _js.truthy(strategy_fallback):
            detail += f" · {_js.template(_js.js_nullish(strategy_label, strategy_fallback))}"
        signals.append({
            "id": _js.js_nullish(_js.field(preview, "id"), f"signal-{index}"),
            "kind": "signal",
            "at": _js.js_nullish(_js.field(preview, "at")),
            "atMs": _to_ms(_js.field(preview, "at")),
            "ticker": _upper(_js.field(value, "ticker")),
            "detail": detail,
            "source": "quant_signal",
            "source_label": "量化信号",
            "origin": True,
        })
    signals = [entry for entry in signals if entry["ticker"] is not None]

    ledger = _js.field(trades, "trades")
    ledger = ledger if isinstance(ledger, list) else []
    fills = []
    for index, trade in enumerate(ledger):
        date = _js.field(trade, "date")
        fee = _js.field_or_undefined(trade, "fee")
        profit = _js.field_or_undefined(trade, "return")
        # audit.js:90 `\`${t.shares} @ ${t.price}\``：字段**缺失**时 JS 插的是 "undefined"，
        # 与显式 null 的 "null" 不同（补遗 B F11，差分用例 trade_missing_optional_keys）。
        detail = (f"{_js.template(labeled(_js.field(trade, 'action'), _js.field(trade, 'action_label'), 'ACTION'))}"
                  f" {_js.template(_js.field_or_undefined(trade, 'shares'))}"
                  f" @ {_js.template(_js.field_or_undefined(trade, 'price'))}"
                  + (f" · 费用 {_js.template(fee)}" if fee is not None and fee is not _js.UNDEFINED else "")
                  # audit.js:92 `(t.return * 100).toFixed(2)`：`*` 是 ToNumber 强转（"0.5" -> 0.5、
                  # "abc" -> NaN -> "NaN"），toFixed 的平局取较大 n（有意差异 3）。
                  + (f" · 收益 {_js.fixed(_js.to_number(profit) * 100)}%"
                     if profit is not None and profit is not _js.UNDEFINED else ""))
        fills.append({
            "id": f"fill-{index}-{_js.stringify(date)}",   # `${t.date ?? ""}`
            "kind": "fill",
            "at": _js.js_nullish(date),
            "atMs": _to_ms(date),
            "ticker": _upper(_js.field(trade, "ticker")),
            "detail": detail,
            "reason": _js.js_nullish(_js.field(trade, "reason")),
            "source": "local-ledger",
        })

    activity = _js.field(snapshot, "activity")
    activity = activity if isinstance(activity, list) else []
    orders = []
    for entry in activity:
        if not isinstance(entry, dict):
            # 有意差异 5：audit.js:108 对这类元素早退（可选链不抛），过滤结果等价。
            continue
        name = tool_name(entry)
        action = action_label(name)
        is_facts = bool(ORDER_SOURCE.search(name))
        order_named = bool(re.search(r"order", name, re.I))
        if action is None and not is_facts and not (order_named and not READ_ONLY_QUERY.search(name)):
            continue
        # audit.js:109 `a.value ?? a`
        raw = _js.js_nullish(_js.field(entry, "value"), entry)
        fields = extract_broker_fields(raw)
        if _js.truthy(_js.field(entry, "is_error")):
            kind = "order-error"
        elif is_facts and action is None:
            kind = "order-facts"
        else:
            kind = ACTION_KIND.get(action) if action is not None else "order-other"
        detail = (f"{name}"
                  + (f" · #{fields['orderId']}" if fields["orderId"] else "")
                  + (f" · status={fields['status']}" if fields["status"] else ""))
        row = {
            "kind": kind,
            "action": action if action is not None else ("订单查询" if is_facts else "订单工具"),
            "at": _js.js_nullish(_js.field(entry, "at")),
            "atMs": _to_ms(_js.field(entry, "at")),
            "order_id": fields["orderId"],
            "ticker": fields["ticker"],
            "detail": detail,
            "source": "broker-observed",
            "source_label": "券商响应（Harness 观察）",
        }
        # audit.js:114 `id: a.id`：字段缺失时 JS 的值是 undefined，JSON.stringify 会把整个键
        # 丢掉；这里同样不生成该键，保证线上 JSON 与 Node 一致（补遗 B F11 的同类语义）。
        entry_id = _js.field_or_undefined(entry, "id")
        if entry_id is not _js.UNDEFINED:
            row["id"] = entry_id
        orders.append(row)

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
            # audit.js:161 `signal && entry.atMs !== null ? Math.round(...) : null`
            "lag_hours": (_js.js_round((entry["atMs"] - signal["atMs"]) / 3600000)
                          if signal is not None and entry["atMs"] is not None else None),
        })

    # audit.js:166 `(b.atMs ?? 0) - (a.atMs ?? 0)`：`?? 0` 把 null/undefined 当 **0** 参与比较，
    # 不是「null 恒排最后」——负 atMs（1970 年前）会排在 null 之后（补遗 B F12）。
    entries = sorted([*signals, *decorated],
                     key=lambda row: -(row["atMs"] if row["atMs"] is not None else 0))
    entries = _slice_entries(entries, max_entries)

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
