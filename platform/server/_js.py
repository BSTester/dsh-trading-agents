# WP6 补遗 B：JavaScript 语义助手（store_access / summary / audit_chain 共用）。
#
# 为什么单独一个模块：这三个模块是 plugins/workbench/src/*.js 的逐行移植，重复实现了同一批
# 「JS 与 Python 不一样」的语义（缺失 vs 显式 null、真值、空值合并、模板字符串、Number()、
# Math.round、对象键枚举序）。各写一份的结果是它们会各自漂移——补遗 B 的移植审查正是这样
# 抓到 summary 与 audit_chain 对同一语义给了两种答案。这里收敛成唯一实现。
#
# 全部语义都以**实际运行 Node 的输出**为准（tests/test_wp6_summary_audit.py 的差分参照物
# 由真实 Node 函数生成）。纯标准库、无 IO、无副作用。
#
# 与 JS 的对应关系：
#   UNDEFINED          variable undefined（缺失的字段）；JSON 里不可表示，只在内部传递
#   present(row, key)  Object.prototype.hasOwnProperty.call(row, key)
#   field(row, key)    row.key（缺失与显式 null 都是 undefined -> None）
#   field_or_undefined 同上，但用 UNDEFINED 区分「缺失」
#   truthy(value)      Boolean(value) / if (value)
#   js_nullish(*v)     a ?? b ?? c
#   js_number_str(n)   String(n)（数字分支）
#   js_key_string(k)   ToPropertyKey(k) 后的字符串形式（对象键）
#   template(v)        `${v}` / String(v)
#   stringify(v)       `${v ?? ""}`
#   fixed(v, f)        (v).toFixed(f)
#   js_round(v)        Math.round(v)
#   numeric(v)         Number(v) + Number.isFinite -> 有限值，否则 null；整数值返回 int
#   to_number(v)       Number(v)（不滤非有限值，供算术使用）
#   json_number(v)     计算产出：JSON.stringify 语义下整数值不带小数点
#   object_entry_order Object.keys/entries 的枚举序（整数样式键提前、按数值升序）
import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "UNDEFINED", "field", "field_or_undefined", "fixed", "js_key_string", "js_nullish",
    "js_number_str", "js_round", "json_number", "numeric", "object_entry_order", "ordered_items",
    "present", "stringify", "template", "to_number", "truthy",
]


class _Undefined:
    """JS 的 undefined：与 JSON 的 null（Python None）是两件事。

    只在内部传递（`field_or_undefined` / `to_number` / `template`），不写进对外输出——
    JS 里值为 undefined 的对象键在 JSON.stringify 时会被整个丢掉，移植侧靠「不生成该键」
    表达同一件事。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "undefined"

    def __bool__(self):
        # Boolean(undefined) === false
        return False

    def __eq__(self, other):
        return other is self

    def __hash__(self):
        return hash("__js_undefined__")


UNDEFINED = _Undefined()


# ---------------------------------------------------------------- 字段访问
def present(row, key):
    """JS `key in row` / `hasOwnProperty`：区分「字段缺失」与「字段显式为 null」。

    这是 broker_trades.js:49 `parsed.s !== undefined` 的关键：JSON 里 null 与缺失都是
    null，但 Number(undefined) 是 NaN 而 Number(null) 是 0。
    """
    return isinstance(row, dict) and key in row


def field(row, key):
    """JS `row.key`：非对象取不到字段（undefined -> None），缺失与显式 null 都是 None。"""
    if isinstance(row, dict):
        return row.get(key)
    return None


def field_or_undefined(row, key):
    """JS `row.key`，但保留「缺失 -> undefined」这一位（模板字符串要区分 null/undefined）。"""
    if isinstance(row, dict):
        return row[key] if key in row else UNDEFINED
    return UNDEFINED


# ---------------------------------------------------------------- 真值 / 空值
def truthy(value):
    """JS 真值语义：None/False/0/""/NaN 为假，**空数组/空对象为真**（Python 里为假）。

    空容器在 JS 里是对象，`[] || x` 取 `[]`（audit.js:77 的 strategy_label 正是这样），
    所以这里不能按 Python 的 bool() 处理。
    """
    if value is None or value is UNDEFINED or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == value and value != 0  # NaN -> false
    if isinstance(value, str):
        return value != ""
    return True  # 数组/对象/其他引用类型一律为真


def js_nullish(*values):
    """JS `a ?? b ?? c`：只有 None/undefined 触发回退，假值（0/""/False/[]）不回退。"""
    for value in values:
        if value is not None and value is not UNDEFINED:
            return value
    return None


# ---------------------------------------------------------------- 数值
_NON_DECIMAL = re.compile(
    r"(?:0[xX](?P<hex>[0-9a-fA-F]+)|0[oO](?P<oct>[0-7]+)|0[bB](?P<bin>[01]+))\Z")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")

# JS 的 WhiteSpace + LineTerminator（ECMA-262 §12.2）。刻意不用 str.strip()：Python 会把
# \x1c-\x1f、\x85 当空白（JS 不当），却不认 \ufeff（JS 当空白）。
_JS_SPACE = (
    "\t\n\x0b\x0c\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _js_trim(text):
    return text.strip(_JS_SPACE)


def _number_from_string(text):
    """StringNumericLiteral -> Number：十进制（含 01/.5/1./指数）与 0x/0o/0b，其余 NaN。

    刻意拒绝 `_`：Python 的 float()/int() 接受 `1_000`、`0x1_0`，而 JS 一律 NaN（补遗 B F6）。
    非十进制字面量不接受符号（`Number("-0x10")` 是 NaN），Unicode 数字（`１２`）也不是
    ASCII 数字 -> NaN。
    """
    body = _js_trim(text)
    if body == "":
        return 0.0
    if body in ("Infinity", "+Infinity"):
        return math.inf
    if body == "-Infinity":
        return -math.inf
    non_decimal = _NON_DECIMAL.match(body)
    if non_decimal is not None:
        radix, digits = ((16, non_decimal.group("hex")) if non_decimal.group("hex") is not None
                         else (8, non_decimal.group("oct")) if non_decimal.group("oct") is not None
                         else (2, non_decimal.group("bin")))
        return float(int(digits, radix))
    if not _DECIMAL.match(body):
        return math.nan
    return float(body)


def to_number(value):
    """ECMAScript ToNumber / `Number(value)`：返回 float（可能是 NaN/±Infinity）。

    与 numeric() 的区别：这里保留非有限值，供 `x * 100` 这类算术使用——JS 里
    `"abc" * 100` 是 NaN，`[] * 100` 是 0，`true * 100` 是 100。
    """
    if value is UNDEFINED:
        return math.nan
    if value is None:
        return 0.0
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _number_from_string(value)
    if isinstance(value, list):
        # ToPrimitive -> Array.prototype.toString -> join(",")（null/undefined 变成空串）
        return _number_from_string(template(value))
    if isinstance(value, dict):
        return math.nan  # "[object Object]" -> NaN
    return math.nan


def json_number(value):
    """计算产出的数值：JSON.stringify 把 100.0 写成 100，Python 会写成 100.0。

    只用于**计算/强转产出**（numeric()、Math.round 结果、Date.parse 毫秒数）；
    文件透传值原样保留，不经过这里（补遗 B F7）。

    * 整数值 -> int：JS 数字都是双精度，`JSON.stringify` 只有在 |x| >= 1e21 时才改用指数
      形式（"1e+21"），此前一律写成整数串。阈值取 1e21 而不是 2^53，才能与 Node 写出的 JSON
      文本逐字对齐；同时避免把一个超大整数留在 Python 里做无界的整数乘法。
    * 非有限值 -> None：`JSON.stringify(NaN)` 与 `JSON.stringify(Infinity)` 都是 "null"，
      而 Python 的 json.dumps 会写出非法的 NaN/Infinity 字面量。
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer() and abs(value) < 1e21:
            return int(value)  # -0.0 -> 0，与 JSON.stringify(-0) === "0" 一致
        return value
    return value


def numeric(value):
    """broker_trades.js:65-68 numeric：`Number(value)` 且有限，否则 None；整数返回 int。"""
    parsed = to_number(value)
    if not math.isfinite(parsed):
        return None
    return json_number(parsed)


def js_number_str(value):
    """JS `String(number)`：与 Python repr() 的指数阈值不同（ECMA-262 Number::toString）。

    `String(1e-7)` 是 "1e-7"（Python repr 是 "1e-07"）、`String(1e20)` 是 20 位整数
    （Python 是 "1e+20"）、`String(100.0)` 是 "100"（Python 是 "100.0"）。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"  # 含 -0：JS `String(-0)` === "0"
    # 最短往返表示（Python 3 的 repr 与 JS 用同一套「最短且可往返」的数字串）
    text = repr(abs(value))
    mantissa, _, exponent = text.partition("e")
    exponent = int(exponent) if exponent else 0
    integer_part, _, fraction_part = mantissa.partition(".")
    digits_all = integer_part + fraction_part
    stripped = digits_all.lstrip("0")
    lead = len(digits_all) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    k = len(digits)
    n = exponent - len(fraction_part) + len(digits_all) - lead  # value = 0.digits × 10^n
    sign = "-" if value < 0 else ""
    if k <= n <= 21:
        return f"{sign}{digits}{'0' * (n - k)}"
    if 0 < n <= 21:
        return f"{sign}{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:
        return f"{sign}0.{'0' * (-n)}{digits}"
    head = digits if k == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{head}e{'+' if n - 1 >= 0 else '-'}{abs(n - 1)}"


def fixed(value, digits=2):
    """JS `(value).toFixed(digits)`：平局取较大的 n（即按绝对值半数进位），非有限值按 JS 文案。

    Python 的 f"{x:.2f}" 是银行家舍入，恰为平局时会分叉：`(0.125).toFixed(2)` 是 "0.13"，
    `f"{0.125:.2f}"` 是 "0.12"。|x| >= 1e21 时 toFixed 直接返回 ToString(x)。
    """
    number = to_number(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if abs(number) >= 1e21:
        return js_number_str(number)  # 规范：x >= 10^21 时 m = ToString(x)
    exact = Decimal(number)  # Decimal(float) 取的是双精度的**精确**值，不是十进制近似
    negative = exact < 0
    rounded = exact.copy_abs().quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    text = f"{rounded:.{digits}f}"
    return f"-{text}" if negative else text


def js_round(value):
    """JS `Math.round`：半数**向 +∞**取整（Python round() 是银行家舍入）。"""
    number = to_number(value)
    if math.isnan(number):
        return math.nan
    if math.isinf(number):
        return number
    return math.floor(number + 0.5)


# ---------------------------------------------------------------- 字符串
def js_key_string(key):
    """JS 对象键（ToPropertyKey 的字符串分支）：`String(1)` -> "1"、`String(true)` -> "true"。

    用于 hasOwnProperty 查找与整数样式键判定——JS 对象的键永远是字符串。
    """
    return template(key)


def template(value):
    """JS 模板字符串 `${value}` / `String(value)`。

    null -> "null"、undefined -> "undefined"、数组 -> join(",")、对象 -> "[object Object]"、
    数字走 js_number_str（`${1e-7}` 是 "1e-7"）。带 `?? ""` 的位置请用 stringify()。
    """
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return js_number_str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Array.prototype.join(",")：元素为 null/undefined 时输出空串
        return ",".join("" if item is None or item is UNDEFINED else template(item)
                        for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def stringify(value):
    """`${value ?? ""}`：None/undefined -> ""，其余同 template()。"""
    if value is None or value is UNDEFINED:
        return ""
    return template(value)


# ---------------------------------------------------------------- 对象键枚举序
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*\Z")
_MAX_ARRAY_INDEX = 2 ** 32 - 2  # Array index：0 <= n < 2^32-1，故 4294967294 仍是索引


def _array_index(key):
    """JS 的 array index 键：规范的数字串（无前导零）且 0 <= n <= 2^32-2。"""
    if not _ARRAY_INDEX.fullmatch(key):
        return None
    number = int(key)
    return number if number <= _MAX_ARRAY_INDEX else None


def object_entry_order(mapping):
    """`Object.keys/entries` 的枚举序：array index 键按数值升序排在最前，其余保持插入序。

    broker_trades.js:189 的查询计数表与 audit.js:45 的 Object.entries 都依赖这一点：
    否则并列项的次序、以及「第一个命中的字段」都会漂移。Python dict 只保有插入序。
    """
    indexed, plain = [], []
    for key in mapping:
        index = _array_index(js_key_string(key))
        if index is None:
            plain.append(key)
        else:
            indexed.append((index, key))
    indexed.sort(key=lambda item: item[0])
    return [key for _, key in indexed] + plain


def ordered_items(mapping):
    """按 JS 枚举序返回 (键, 值) 对（等价 `Object.entries`）。"""
    return [(key, mapping[key]) for key in object_entry_order(mapping)]
