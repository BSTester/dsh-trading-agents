"""平台内告警规则求值器 —— **不装 Grafana/Prometheus 的轻量替代**（规格 §8.3）。

存在理由
--------
§8.3 要求「Prometheus + Grafana 监控以下指标」。本机没有（也不该为此新装）Grafana/Prometheus，
但「不装」不能变成「没有监控」。本模块因此把**同一份** ``platform/deploy/monitoring/alerts.yml``
读进进程内求值：

    ``GET /metrics``（Prometheus 文本，observability.py）
        → 本模块解析成指标模型（同一份文本，不另造事实源）
        → 按 alerts.yml 的 expr / for / severity 求值
        → ``GET /api/v3/ops/alerts``（人/前端读的三态结果）

**规则文件只有一份**：``alerts.yml`` 既不复制也不派生；把 Prometheus 接上时，它读的是同一个
文件，本模块与 Prometheus 对同一条规则给出同向结论（口径差异见 README §「与 Prometheus 的关系」）。

三态（+ 两个必须分清的中间态）
-----------------------------
* ``firing``      表达式为真，且已持续满 ``for``（``for: 0m`` 即立即 firing）；
* ``pending``     表达式为真但 ``for`` 还没满——**既不是 firing 也不是 ok**；
* ``ok``          表达式为假（"判定过，且不满足"）；
* ``no-data``     判不了：表达式引用的指标**不存在**（无读数），或 rate/increase/delta/changes
                  的窗口历史不足。**绝不能显示成 ok**——「看不到」不是「正常」；
* ``unsupported`` 表达式超出本求值器支持的 PromQL 子集（例如 ``or``/聚合/``absent()``/正则匹配）。
                  显式报出，绝不静默当 ok。

分位数
------
``quantwb_call_duration_p50/p95/p99_seconds{scope=...}`` 由 ``observability`` 的滑动窗口
（最近秩，不插值）产出；本模块不自己算分位数，只用它——口径写在 observability 的 HELP
与 README「分位数口径」一节。

诚实性纪律（与仓库「数据诚实」一致）
------------------------------------
* 求值器**只读**：``/metrics`` 文本 + 落盘探测缓存 + 事件表；不触发任何写/交易端点；
* 历史窗口在**进程内**（默认 2h，``QUANT_ALERTS_HISTORY_SECONDS``）：进程重启后清零，
  因此 rate/increase/delta/changes 类规则在重启后的第一个窗口内如实报 ``no-data``
  （不做外推，也不假装窗口是满的）；
* ``QuantWorkbenchDown``（``up{job="quantwb-v3"}``）在进程内**无法判定**：``up`` 是抓取器
  生成的合成指标，进程自己的求值器看不到自己死亡。该规则会如实报 ``no-data`` 并在证据里
  写明「由外部抓取器判定」——装上 Prometheus 后由它判定。

测试：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_alerts -v``
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "ALERT_HISTORY_ENV",
    "ALERT_SAMPLE_SECONDS_ENV",
    "ALERT_SAMPLER_ENV",
    "AlertsEngine",
    "DEFAULT_HISTORY_SECONDS",
    "DEFAULT_SAMPLE_SECONDS",
    "MetricModel",
    "PrometheusParseError",
    "RULE_STATES",
    "UnsupportedExpression",
    "build_model",
    "check_prometheus_text",
    "evaluate_expression",
    "load_rules",
    "parse_prometheus_text",
    "register",
    "rules_path",
    "summary_counts",
]

#: 规则文件默认位置（``platform/deploy/monitoring/alerts.yml``）。
DEFAULT_RULES_PATH = (Path(__file__).resolve().parents[1] / "deploy" / "monitoring" / "alerts.yml")

#: 规则文件路径可用环境变量覆盖（容器/测试）。
RULES_PATH_ENV = "QUANT_ALERTS_RULES"

#: 背景采样周期（秒）：推进 ``for`` 时钟、积累 rate/increase 所需的窗口历史。
ALERT_SAMPLE_SECONDS_ENV = "QUANT_ALERTS_SAMPLE_SECONDS"
DEFAULT_SAMPLE_SECONDS = 15.0

#: 进程内历史窗口（秒）。必须 >= 规则里最长的 ``[range]`` + ``offset``（当前最长 1h + 1h）。
ALERT_HISTORY_ENV = "QUANT_ALERTS_HISTORY_SECONDS"
DEFAULT_HISTORY_SECONDS = 7200.0

#: 是否启动背景采样线程（``0`` 关闭；测试里可关，避免后台线程干扰断言）。
ALERT_SAMPLER_ENV = "QUANT_ALERTS_SAMPLER"

#: 窗口历史覆盖率下限：观测跨度必须达到窗口的这个比例，否则 ``no-data``。
#: 理由：不做 PromQL 的外推（外推会把「只看到 15s」放大成「一小时的结论」）。
HISTORY_COVERAGE = 0.9

#: 四个状态（外加 pending）。顺序即前端展示优先级。
RULE_STATES = ("firing", "pending", "no-data", "unsupported", "ok")

#: 进程内无法判定的规则（合成指标由抓取器生成，进程看不到自己死亡）。
EXTERNAL_RULES = {
    "QuantWorkbenchDown": (
        "``up{job=\"quantwb-v3\"}`` 是 Prometheus 抓取器生成的**合成指标**，平台自身不导出它："
        "进程内求值器无法观测自己的死亡。装上外部抓取器后由它判定这条规则"
    ),
}

#: PromQL 里本求值器**有意不支持**的构造 → 显式报 unsupported（不静默当 ok）。
_UNSUPPORTED_TOKENS = {
    "or": "``or`` 集合运算（本求值器只实现 and）",
    "unless": "``unless`` 集合运算",
    "by": "聚合修饰符 ``by``",
    "without": "聚合修饰符 ``without``",
    "bool": "比较修饰符 ``bool``",
    "offset": "``offset`` 由解析器单独处理（出现在此处说明用法不受支持）",
}
_UNSUPPORTED_FUNCS = {
    "absent": "absent()/absent_over_time()", "absent_over_time": "absent()/absent_over_time()",
    "histogram_quantile": "histogram_quantile()（本平台不用直方图，分位数由 sliding window 给出）",
    "sum": "聚合函数（sum/avg/...）", "avg": "聚合函数（sum/avg/...）",
    "min": "聚合函数（sum/avg/...）", "max": "聚合函数（sum/avg/...）",
    "count": "聚合函数（sum/avg/...）", "topk": "聚合函数（sum/avg/...）",
    "bottomk": "聚合函数（sum/avg/...）", "quantile": "聚合函数（sum/avg/...）",
    "stddev": "聚合函数（sum/avg/...）", "stdvar": "聚合函数（sum/avg/...）",
    "group": "聚合函数（sum/avg/...）", "count_values": "聚合函数（sum/avg/...）",
    "label_replace": "label_replace()", "label_join": "label_join()",
    "predict_linear": "predict_linear()", "deriv": "deriv()",
    "irate": "irate()（本求值器只实现 rate/increase/delta/changes）",
    "idelta": "idelta()", "resets": "resets()", "avg_over_time": "xxx_over_time()",
    "max_over_time": "xxx_over_time()", "min_over_time": "xxx_over_time()",
    "sum_over_time": "xxx_over_time()", "count_over_time": "xxx_over_time()",
    "last_over_time": "xxx_over_time()", "present_over_time": "xxx_over_time()",
}
_SUPPORTED_FUNCS = ("rate", "increase", "delta", "changes")


class UnsupportedExpression(Exception):
    """表达式超出支持子集（调用方必须把它呈现成 ``unsupported``，不能当 ok）。"""


class PrometheusParseError(Exception):
    """Prometheus 文本解析失败（结构性错误）。"""


def rules_path():
    raw = os.environ.get(RULES_PATH_ENV)
    if raw and str(raw).strip():
        return Path(str(raw).strip())
    return DEFAULT_RULES_PATH


# ===========================================================================
# 一、最小 YAML 子集解析器（trading-venv 里没有 PyYAML）
# ===========================================================================
class YamlSubsetError(ValueError):
    """YAML 子集解析失败：说明这个文件用了本解析器不支持的结构。"""


def _strip_comment(text):
    """去掉行尾注释：引号内的 ``#`` 不算注释；``#`` 前必须是行首或空白。"""
    quote = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index]
    return text


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _skippable(line):
    text = line.strip()
    return text == "" or text.startswith("#")


def _scalar(text):
    """标量转换：引号去壳 / bool / null / 数字 / 原样字符串。"""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


class _MiniYaml:
    """只认 ``alerts.yml`` 实际用到的结构：块映射 / 块序列 / 折叠与字面块标量。

    刻意**不**追求 YAML 全语法：遇到不认识的结构直接抛 ``YamlSubsetError``，
    让调用方知道「这份文件不能靠这个解析器读」——静默读错比读不出来更危险。
    """

    def __init__(self, text):
        self.lines = text.splitlines()
        self.count = len(self.lines)

    # -- 对外 --
    def parse(self):
        value, index = self._parse_node(0, 0)
        while index < self.count and _skippable(self.lines[index]):
            index += 1
        if index < self.count:
            raise YamlSubsetError(f"第 {index + 1} 行起有多余内容：{self.lines[index]!r}")
        return value

    # -- 内部 --
    def _next_content(self, index):
        while index < self.count and _skippable(self.lines[index]):
            index += 1
        return index

    def _parse_node(self, index, indent):
        index = self._next_content(index)
        if index >= self.count:
            return None, index
        if _indent_of(self.lines[index]) < indent:
            return None, index
        text = self.lines[index].lstrip()
        if text == "-" or text.startswith("- "):
            return self._parse_sequence(index, _indent_of(self.lines[index]))
        return self._parse_mapping(index, _indent_of(self.lines[index]))

    def _parse_sequence(self, index, indent):
        items = []
        while index < self.count:
            if _skippable(self.lines[index]):
                index += 1
                continue
            current = self.lines[index]
            if _indent_of(current) < indent:
                break
            if _indent_of(current) > indent:
                raise YamlSubsetError(f"第 {index + 1} 行缩进异常：{current!r}")
            text = current.lstrip()
            if not (text == "-" or text.startswith("- ")):
                break
            if text == "-":
                value, index = self._parse_node(index + 1, indent + 1)
                items.append(value)
                continue
            # ``- key: value``：把这一行原地改写成「缩进 + 2 的虚拟行」，再按映射解析。
            body = text[2:]
            self.lines[index] = " " * (indent + 2) + body
            value, index = self._parse_node(index, indent + 2)
            items.append(value)
        return items, index

    def _parse_mapping(self, index, indent):
        mapping = {}
        while index < self.count:
            if _skippable(self.lines[index]):
                index += 1
                continue
            current = self.lines[index]
            current_indent = _indent_of(current)
            if current_indent < indent:
                break
            if current_indent > indent:
                raise YamlSubsetError(f"第 {index + 1} 行缩进异常：{current!r}")
            text = current.strip()
            if text.startswith("- "):
                break
            key, separator, rest = text.partition(":")
            if not separator:
                raise YamlSubsetError(f"第 {index + 1} 行不是 key: value：{current!r}")
            key = key.strip()
            rest = rest.strip()
            index += 1
            if rest in (">-", ">", "|", "|-", "|+", ">+"):
                value, index = self._parse_block_scalar(index, current_indent, rest)
                mapping[key] = value
                continue
            if rest == "":
                probe = self._next_content(index)
                if probe < self.count and _indent_of(self.lines[probe]) > current_indent:
                    value, index = self._parse_node(probe, current_indent + 1)
                    mapping[key] = value
                else:
                    mapping[key] = None
                continue
            mapping[key] = _scalar(rest)
        return mapping, index

    def _parse_block_scalar(self, index, indent, marker):
        body = []
        while index < self.count:
            line = self.lines[index]
            if line.strip() == "":
                body.append("")
                index += 1
                continue
            if _indent_of(line) <= indent:
                break
            body.append(line.strip())
            index += 1
        if marker.startswith(">"):
            # 折叠块：行与行之间用空格连接（本文件里都是「一句话拆成几行」的用法）。
            text = " ".join(part for part in body if part != "")
        else:
            text = "\n".join(body)
        if marker.endswith("-"):
            text = text.rstrip("\n")
        return text, index


def _loads_yaml(text):
    """优先用 PyYAML（若装了）；否则用内置子集解析器。"""
    try:
        import yaml  # noqa: PLC0415 —— 可选依赖，venv 里通常没有
    except Exception:  # noqa: BLE001
        return _MiniYaml(text).parse()
    return yaml.safe_load(text)


# ===========================================================================
# 二、Prometheus 文本 → 指标模型
# ===========================================================================
_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?"
    r"[ \t]+(?P<value>[^ \t]+)"
    r"(?:[ \t]+(?P<timestamp>[^ \t]+))?[ \t]*$")
_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


class Sample:
    """一个指标样本：名字 + 标签（有序元组）+ 值。"""

    __slots__ = ("name", "labels", "value")

    def __init__(self, name, labels, value):
        self.name = str(name)
        self.labels = tuple((str(key), str(val)) for key, val in labels)
        self.value = float(value)

    def label_dict(self):
        return {key: value for key, value in self.labels}

    def __repr__(self):  # pragma: no cover - 调试用
        return f"Sample({self.name}{self.label_dict()!r}={self.value})"


def _parse_labels(text):
    """``a="1",b="2"`` → ``[("a","1"),("b","2")]``（Prometheus 只认这三种转义）。"""
    labels = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t,":
            index += 1
        if index >= length:
            break
        start = index
        while index < length and text[index] != "=":
            index += 1
        if index >= length:
            raise PrometheusParseError(f"标签缺少 '='：{text!r}")
        key = text[start:index].strip()
        index += 1  # skip '='
        if index >= length or text[index] != '"':
            raise PrometheusParseError(f"标签值必须是引号字符串：{text!r}")
        index += 1
        chunks = []
        while index < length:
            char = text[index]
            if char == "\\" and index + 1 < length:
                nxt = text[index + 1]
                chunks.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, nxt))
                index += 2
                continue
            if char == '"':
                index += 1
                break
            chunks.append(char)
            index += 1
        else:
            raise PrometheusParseError(f"标签值引号未闭合：{text!r}")
        labels.append((key, "".join(chunks)))
    return labels


class MetricModel:
    """一份 Prometheus 文本的只读视图：``families[name] = [Sample, ...]``。"""

    def __init__(self, families=None, types=None, helps=None):
        self.families = {str(k): list(v) for k, v in (families or {}).items()}
        self.types = dict(types or {})
        self.helps = dict(helps or {})

    def present(self, name):
        """该 family 是否有**至少一个样本**（空 family = 没有读数）。"""
        return bool(self.families.get(str(name)))

    def names(self):
        return sorted(self.families)

    def select(self, name, matchers=()):
        """按标签匹配取样本。

        ``matchers`` 支持 ``(key, operator, value)`` 三元组（``=``/``!=``）与
        ``(key, value)`` 二元简写（等价于 ``=``）。``!=`` 按 Prometheus 语义
        （缺该标签也算不等）。
        """
        out = []
        for sample in self.families.get(str(name), []):
            labels = sample.label_dict()
            matched = True
            for matcher in matchers:
                key, operator, value = _normalize_matcher(matcher)
                if not _match(labels, key, operator, value):
                    matched = False
                    break
            if matched:
                out.append(sample)
        return out

    def scalar(self, name, matchers=()):
        """取第一个匹配样本的值（``None`` = 没有）。"""
        found = self.select(name, matchers)
        return found[0].value if found else None

    def series_keys(self, name, matchers=()):
        return [sample.labels for sample in self.select(name, matchers)]


def _normalize_matcher(matcher):
    if len(matcher) == 2:
        key, value = matcher
        return str(key), "=", value
    key, operator, value = matcher
    return str(key), str(operator), value


def _match(labels, key, operator, value):
    actual = labels.get(key)
    if operator == "=":
        return actual == value
    if operator == "!=":
        return actual != value
    if operator == "=~":
        raise UnsupportedExpression("标签正则匹配 ``=~`` 不在支持子集内")
    if operator == "!~":
        raise UnsupportedExpression("标签正则匹配 ``!~`` 不在支持子集内")
    raise PrometheusParseError(f"未知标签运算符 {operator!r}")


def parse_prometheus_text(text):
    """Prometheus 文本 → :class:`MetricModel`。结构性错误抛 ``PrometheusParseError``。"""
    families = {}
    types = {}
    helps = {}
    seen_sample = set()
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[1] == "TYPE":
                name = parts[2]
                if name in seen_sample:
                    raise PrometheusParseError(f"L{number}: {name} 的 TYPE 出现在样本之后")
                if name in types:
                    raise PrometheusParseError(f"L{number}: {name} 重复 TYPE")
                types[name] = parts[3] if len(parts) > 3 else ""
            elif len(parts) >= 3 and parts[1] == "HELP":
                helps[parts[2]] = parts[3] if len(parts) > 3 else ""
            continue
        match = _SAMPLE_RE.match(line)
        if not match:
            raise PrometheusParseError(f"L{number}: 不是合法的样本行：{raw!r}")
        name = match.group("name")
        labels = _parse_labels(match.group("labels") or "")
        value_text = match.group("value")
        try:
            value = float(value_text)
        except ValueError as error:
            raise PrometheusParseError(f"L{number}: 值不是数字：{value_text!r}") from error
        families.setdefault(name, []).append(Sample(name, labels, value))
        seen_sample.add(name)
    return MetricModel(families, types, helps)


def check_prometheus_text(text):
    """本地 exposition-format 校验器（``promtool check metrics`` 的替代/补充）。

    返回问题字符串列表（空列表 = 合法）。检查项：
      * 样本行可解析、family 有 HELP/TYPE、同 family 样本连续、无重复 TYPE；
      * 名字合法；计数器以 ``_total`` 结尾（风格）；
      * ``_ms`` 单位名（promtool 也会报，本平台两处**有意保留**）。
    """
    problems = []
    families = {}
    order = []
    current = None
    typed = set()
    declared = set()
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[1] in ("TYPE", "HELP"):
                name = parts[2]
                declared.add(name)
                if name in families.get("_samples", set()):
                    problems.append(f"L{number}: {name} 的 {parts[1]} 出现在样本之后")
                if parts[1] == "TYPE":
                    if name in typed:
                        problems.append(f"L{number}: {name} 重复 TYPE")
                    typed.add(name)
            continue
        match = _SAMPLE_RE.match(line)
        if not match:
            problems.append(f"L{number}: 不是合法的样本行：{raw!r}")
            continue
        name = match.group("name")
        try:
            _parse_labels(match.group("labels") or "")
        except PrometheusParseError as error:
            problems.append(f"L{number}: {error}")
            continue
        try:
            float(match.group("value"))
        except ValueError:
            problems.append(f"L{number}: 值不是数字：{match.group('value')!r}")
            continue
        if current is not None and current != name and name in order:
            problems.append(f"L{number}: family {name} 的样本不连续（exposition format 要求连续）")
        if name not in order:
            order.append(name)
        current = name
        families.setdefault("_samples", set()).add(name)
        if name.endswith("_ms") or name.endswith("_ms_total"):
            problems.append(f"{name} metric names should not contain abbreviated units")
        if (name.endswith("_count") or name.endswith("_sum")) and not name.endswith("_total"):
            problems.append(f"L{number}: {name} 疑似计数器却缺 _total 后缀（风格建议）")
    for name in order:
        if name not in declared:
            problems.append(f"{name} 缺 # HELP/# TYPE 声明")
    for name in declared:
        if name not in order:
            problems.append(f"{name} 声明了 TYPE/HELP 却没有样本")
    return problems


def build_model(entries):
    """测试/调用方构造模型：``{"metric": [({"label": "v"}, 1.0), ...]}`` → MetricModel。

    也接受 ``{"metric": [Sample, ...]}`` 与 ``{"metric": [1.0, 2.0]}``（无标签）两种简写。
    """
    families = {}
    for name, samples in (entries or {}).items():
        rows = []
        for item in samples:
            if isinstance(item, Sample):
                rows.append(item)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                rows.append(Sample(name, (), float(item)))
            elif isinstance(item, dict):
                labels = item.get("labels") or {}
                rows.append(Sample(name, tuple(sorted(labels.items())), item.get("value")))
            else:
                labels, value = item
                rows.append(Sample(name, tuple(sorted(dict(labels).items())), value))
        families[str(name)] = rows
    return MetricModel(families)


# ===========================================================================
# 三、PromQL 子集：词法 / 语法
# ===========================================================================
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d|w)$")
_TOKEN_RE = re.compile(r"""
    (?P<space>\s+)
  | (?P<duration>\d+(?:\.\d+)?(?:ms|s|m|h|d|w)\b)
  | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<ident>[a-zA-Z_:][a-zA-Z0-9_:]*)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<operator>=~|!~|==|!=|>=|<=|>|<|\+|-|\*|/|%|\{|\}|\[|\]|\(|\)|,|=)
""", re.VERBOSE)


def _tokenize(expr):
    tokens = []
    index = 0
    text = str(expr or "")
    while index < len(text):
        match = _TOKEN_RE.match(text, index)
        if not match:
            raise UnsupportedExpression(f"无法识别的表达式片段：{text[index:index + 20]!r}")
        index = match.end()
        kind = match.lastgroup
        if kind == "space":
            continue
        tokens.append((kind, match.group()))
    tokens.append(("eof", ""))
    return tokens


class _Parser:
    """递归下降解析 PromQL 子集；超出子集一律抛 ``UnsupportedExpression``。"""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    # -- 基础 --
    def peek(self):
        return self.tokens[self.pos]

    def kind(self):
        return self.tokens[self.pos][0]

    def text(self):
        return self.tokens[self.pos][1]

    def next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def accept(self, kind, text=None):
        token_kind, token_text = self.peek()
        if token_kind == kind and (text is None or token_text == text):
            self.pos += 1
            return token_text
        return None

    def expect(self, kind, text=None):
        got = self.accept(kind, text)
        if got is None:
            raise UnsupportedExpression(
                f"表达式语法不支持：期望 {text or kind}，实际 {self.text()!r}")
        return got

    # -- 文法 --
    def parse(self):
        node = self.parse_and()
        if self.kind() != "eof":
            raise UnsupportedExpression(f"表达式尾部有多余内容：{self.text()!r}")
        return node

    def parse_and(self):
        node = self.parse_comparison()
        while self.kind() == "ident" and self.text() in ("and", "or", "unless"):
            op = self.next()[1]
            if op != "and":
                raise UnsupportedExpression(_UNSUPPORTED_TOKENS[op])
            mode = self._match_mode()
            right = self.parse_comparison()
            node = ("and", node, right, mode)
        return node

    def _match_mode(self):
        """``and on(...)`` / ``and ignoring(...)`` 的标签匹配模式；缺省是精确标签集匹配。

        PromQL 的 ``and`` 默认要求**标签集完全相同**——这正是既有 industry 三条规则
        永远不可能命中的原因（左侧带 ``scope``/``error`` 标签，右侧守卫是无标签指标）。
        支持 ``on()``（空标签列表 = 任意序列都算匹配）后，规则可以显式表达「不按标签关联」。
        """
        if self.kind() != "ident" or self.text() not in ("on", "ignoring"):
            return None
        keyword = self.next()[1]
        self.expect("operator", "(")
        labels = []
        if not self.accept("operator", ")"):
            while True:
                labels.append(self.expect("ident"))
                if not self.accept("operator", ","):
                    self.expect("operator", ")")
                    break
        return (keyword, tuple(labels))

    def parse_comparison(self):
        node = self.parse_additive()
        if self.kind() == "operator" and self.text() in ("==", "!=", ">", "<", ">=", "<="):
            op = self.next()[1]
            right = self.parse_additive()
            return ("cmp", op, node, right)
        return node

    def parse_additive(self):
        node = self.parse_multiplicative()
        while self.kind() == "operator" and self.text() in ("+", "-"):
            op = self.next()[1]
            node = ("math", op, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self):
        node = self.parse_unary()
        while self.kind() == "operator" and self.text() in ("*", "/", "%"):
            op = self.next()[1]
            node = ("math", op, node, self.parse_unary())
        return node

    def parse_unary(self):
        if self.kind() == "operator" and self.text() == "-":
            self.next()
            return ("neg", self.parse_unary())
        if self.kind() == "operator" and self.text() == "+":
            self.next()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self):
        kind, text = self.peek()
        if kind == "number":
            self.next()
            return ("num", float(text))
        if kind == "operator" and text == "(":
            self.next()
            node = self.parse_and()
            self.expect("operator", ")")
            return node
        if kind == "ident":
            if text in _UNSUPPORTED_TOKENS and text not in ("offset",):
                raise UnsupportedExpression(_UNSUPPORTED_TOKENS[text])
            self.next()
            lowered = text.lower()
            if self.kind() == "operator" and self.text() == "(":
                if lowered in _UNSUPPORTED_FUNCS:
                    raise UnsupportedExpression(_UNSUPPORTED_FUNCS[lowered])
                if lowered in ("sum", "avg", "min", "max", "count", "topk", "bottomk"):
                    raise UnsupportedExpression(_UNSUPPORTED_FUNCS.get(lowered))
                return self.parse_call(lowered)
            if lowered in _UNSUPPORTED_FUNCS:
                raise UnsupportedExpression(_UNSUPPORTED_FUNCS[lowered])
            return self.parse_selector(text)
        raise UnsupportedExpression(f"表达式语法不支持：{text!r}")

    def parse_call(self, name):
        self.expect("operator", "(")
        if name == "time":
            self.expect("operator", ")")
            return ("time",)
        if name not in _SUPPORTED_FUNCS:
            raise UnsupportedExpression(f"函数 {name}() 不在支持子集 {_SUPPORTED_FUNCS} 内")
        argument = self.parse_and()
        self.expect("operator", ")")
        if argument[0] != "range":
            raise UnsupportedExpression(f"{name}() 的参数必须是范围选择器（如 x[10m]）")
        return ("func", name, argument)
    # 说明：``sum(...)`` 之类的聚合调用没有独立的 parse 分支——它们在
    # ``parse_primary`` 里就按函数名被拦成 unsupported，绝不会落到 here。

    def parse_selector(self, name):
        matchers = []
        if self.accept("operator", "{"):
            while True:
                if self.accept("operator", "}"):
                    break
                key = self.expect("ident")
                operator = self.accept("operator")
                if operator not in ("=", "!=", "=~", "!~"):
                    raise UnsupportedExpression(f"标签匹配符 {operator!r} 不支持")
                if operator in ("=~", "!~"):
                    raise UnsupportedExpression("标签正则匹配 ``=~`` / ``!~`` 不在支持子集内")
                value = self.next()
                if value[0] not in ("string", "ident", "number"):
                    raise UnsupportedExpression(f"标签值非法：{value[1]!r}")
                text = value[1]
                if value[0] == "string":
                    text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                matchers.append((key, operator, text))
                if not self.accept("operator", ","):
                    self.expect("operator", "}")
                    break
        node = ("sel", name, tuple(matchers), None)
        if self.accept("operator", "["):
            duration = self._duration()
            self.expect("operator", "]")
            node = ("range", node, duration)
        if self.kind() == "ident" and self.text() == "offset":
            self.next()
            offset = self._duration()
            if node[0] == "range":
                node = ("range", node[1], node[2], offset)
            else:
                node = ("sel", node[1], node[2], offset)
        return node

    def _duration(self):
        token = self.next()
        if token[0] == "duration":
            return _duration_seconds(token[1])
        if token[0] == "number" and self.kind() == "ident":
            unit = self.next()[1]
            return _duration_seconds(f"{token[1]}{unit}")
        raise UnsupportedExpression(f"不是合法的时间长度：{token[1]!r}")


def _duration_seconds(text):
    match = _DURATION_RE.match(str(text).strip())
    if not match:
        raise UnsupportedExpression(f"不是合法的时间长度：{text!r}")
    value = float(match.group(1))
    unit = match.group(2)
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}[unit]
    return value * scale


def parse_expression(expr):
    """表达式串 → AST（超出子集抛 ``UnsupportedExpression``）。"""
    return _Parser(_tokenize(expr)).parse()


def required_metrics(node, out=None):
    """AST 里引用的所有指标名（含 range 内层选择器）。"""
    out = set() if out is None else out
    if not isinstance(node, tuple):
        return out
    tag = node[0]
    if tag == "sel":
        out.add(node[1])
    elif tag == "range":
        required_metrics(node[1], out)
    elif tag in ("func", "neg", "cmp", "math", "and"):
        for item in node[1:]:
            required_metrics(item, out)
    elif tag == "time" or tag == "num":
        pass
    return out


def required_ranges(node, out=None):
    """AST 里所有范围选择器：``[(metric_name, seconds, matchers), ...]``。"""
    out = [] if out is None else out
    if not isinstance(node, tuple):
        return out
    tag = node[0]
    if tag == "range":
        inner = node[1]
        if inner[0] == "range":
            inner = inner[1]
        if inner[0] != "sel":
            raise UnsupportedExpression("范围选择器必须作用在指标选择器上")
        out.append((inner[1], float(node[2]), inner[2]))
    elif tag in ("func",):
        required_ranges(node[2], out)
    elif tag in ("neg", "cmp", "math", "and"):
        for item in node[1:]:
            required_ranges(item, out)
    return out


def thresholds_of(node, out=None):
    """AST 里所有「表达式 op 常量」比较：``[{metric, operator, threshold}]``（供展示）。

    ``metric`` 只在被比较的另一侧是**裸选择器**时给出；形如
    ``increase(x[1h]) >= 3`` 的比较也会被记下来（``metric=None``）——阈值必须能被看到，
    哪怕它不是直接挂在一个指标选择器上。
    """
    out = [] if out is None else out
    if not isinstance(node, tuple):
        return out
    if node[0] == "cmp":
        operator, left, right = node[1], node[2], node[3]
        if _is_number(left) and not _is_number(right):
            out.append({"metric": _selector_name(right), "operator": _flip(operator),
                        "threshold": left[1]})
        elif _is_number(right) and not _is_number(left):
            out.append({"metric": _selector_name(left), "operator": operator,
                        "threshold": right[1]})
        thresholds_of(left, out)
        thresholds_of(right, out)
    elif node[0] in ("neg",):
        thresholds_of(node[1], out)
    elif node[0] in ("math", "and"):
        thresholds_of(node[1], out)
        thresholds_of(node[2], out)
    elif node[0] == "func":
        thresholds_of(node[2], out)
    elif node[0] == "range":
        thresholds_of(node[1], out)
    return out


def _is_number(node):
    return isinstance(node, tuple) and node[0] == "num"


def _selector_name(node):
    if isinstance(node, tuple) and node[0] == "sel":
        return node[1]
    if isinstance(node, tuple) and node[0] == "range":
        return _selector_name(node[1])
    return None


def _flip(operator):
    return {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(operator, operator)


# ===========================================================================
# 四、求值
# ===========================================================================
class _Vector:
    __slots__ = ("samples",)

    def __init__(self, samples=()):
        self.samples = list(samples)

    def truthy(self):
        # Prometheus 语义：**向量非空即为真**（比较运算保留 lhs 的值，
        # 所以 ``up == 0`` 命中的样本值是 0，但它确实是一条命中的时间序列）。
        return bool(self.samples)


class _Scalar:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = float(value)

    def truthy(self):
        return self.value != 0


class HistoryGap(Exception):
    """窗口历史不足（调用方呈现为 no-data，绝不当 ok）。"""


class _Evaluator:
    """在 ``model`` 上求值：当前样本来自 model，窗口函数来自 ``history``。"""

    def __init__(self, model, history, now):
        self.model = model
        self.history = history
        self.now = now
        #: 求值过程中的「规则可能写错了」提示（例如 and 两侧标签集无交集）。
        self.notes = []

    # -- 历史 --
    def _windows(self, name, matchers, seconds, offset):
        """一个范围选择器在**每条匹配序列**上的历史样本（逐条做覆盖率检查）。

        与 Prometheus 一致：范围函数是**逐序列**的（``increase(x[1h])`` 对 x 的每条序列
        各算一个值）。任何一条序列的窗口历史不足就抛 ``HistoryGap``——不做外推，
        也不「挑一条有的算」。
        """
        end = self.now - (offset or 0.0)
        start = end - seconds
        keys = self.model.series_keys(name, matchers)
        if not keys:
            limit = dict((key, value) for key, _, value in matchers)
            raise HistoryGap(f"{name} 当前没有匹配 {limit} 的样本")
        out = []
        for key in keys:
            points = [item for item in self.history.get((name, key), ())
                      if start <= item[0] <= end]
            if len(points) < 2:
                raise HistoryGap(
                    f"{name}[{_human(seconds)}] 在窗口内只有 {len(points)} 个样本"
                    f"（至少需要 2 个；求值器历史窗口默认 "
                    f"{int(_env_float(ALERT_HISTORY_ENV, DEFAULT_HISTORY_SECONDS))}s，"
                    "进程重启后需要重新积累）")
            span = points[-1][0] - points[0][0]
            if span < seconds * HISTORY_COVERAGE:
                raise HistoryGap(
                    f"{name}[{_human(seconds)}] 的历史只覆盖 {_human(span)}"
                    f"（< {int(HISTORY_COVERAGE * 100)}% 窗口）：不外推，如实报 no-data")
            out.append((key, points))
        return out

    def _rate(self, name, matchers, seconds, offset):
        out = []
        for key, points in self._windows(name, matchers, seconds, offset):
            first, last = points[0], points[-1]
            span = last[0] - first[0]
            delta = last[1] - first[1]
            if delta < 0:  # 计数器重置：按 Prometheus 的直觉取「重置后的当前值」
                delta = last[1]
            out.append((key, delta / span if span > 0 else 0.0))
        return out

    def _delta(self, name, matchers, seconds, offset):
        return [(key, points[-1][1] - points[0][1])
                for key, points in self._windows(name, matchers, seconds, offset)]

    def _changes(self, name, matchers, seconds, offset):
        return [(key, float(sum(1 for previous, current in zip(points, points[1:])
                              if current[1] != previous[1])))
                for key, points in self._windows(name, matchers, seconds, offset)]

    # -- 递归求值 --
    def value(self, node):
        tag = node[0]
        if tag == "num":
            return _Scalar(node[1])
        if tag == "time":
            return _Scalar(self.now)
        if tag == "neg":
            value = self.value(node[1])
            if isinstance(value, _Scalar):
                return _Scalar(-value.value)
            return _Vector([(labels, -val) for labels, val in value.samples])
        if tag == "sel":
            return self._selector(node)
        if tag == "range":
            raise UnsupportedExpression("裸范围选择器不能直接作为告警条件（需要 rate/increase/delta/changes）")
        if tag == "func":
            return self._func(node)
        if tag == "math":
            return self._math(node)
        if tag == "cmp":
            return self._compare(node)
        if tag == "and":
            return self._and(node)
        raise UnsupportedExpression(f"未知 AST 节点 {tag!r}")

    def _selector(self, node):
        name, matchers, offset = node[1], node[2], node[3]
        rows = self.model.select(name, matchers)
        if offset:
            end = self.now - float(offset)
            out = []
            for sample in rows:
                key = (name, sample.labels)
                points = [item for item in self.history.get(key, ()) if item[0] <= end]
                if not points:
                    raise HistoryGap(f"{name} offset {_human(offset)} 之前没有历史样本")
                out.append((sample.labels, points[-1][1]))
            return _Vector(out)
        return _Vector([(sample.labels, sample.value) for sample in rows])

    def _func(self, node):
        name, selector = node[1], node[2]
        inner = selector[1] if selector[0] == "range" else selector
        if inner[0] != "sel":
            raise UnsupportedExpression("范围函数只支持作用在指标选择器上")
        seconds = float(selector[2])
        offset = float(selector[3] or 0.0) if len(selector) > 3 else 0.0
        if name == "rate":
            rows = self._rate(inner[1], inner[2], seconds, offset)
        elif name == "delta":
            rows = self._delta(inner[1], inner[2], seconds, offset)
        elif name == "changes":
            rows = self._changes(inner[1], inner[2], seconds, offset)
        else:  # increase：速率 × 规则窗口（观测跨度不足时上面已按覆盖率拦截）
            rows = [(key, value * seconds)
                    for key, value in self._rate(inner[1], inner[2], seconds, offset)]
        return _Vector(rows)

    def _math(self, node):
        _, operator, left_node, right_node = node
        left, right = self.value(left_node), self.value(right_node)
        if isinstance(left, _Scalar) and isinstance(right, _Scalar):
            return _Scalar(_apply_math(operator, left.value, right.value))
        if isinstance(left, _Vector) and isinstance(right, _Scalar):
            return _Vector([(labels, _apply_math(operator, val, right.value))
                            for labels, val in left.samples])
        if isinstance(left, _Scalar) and isinstance(right, _Vector):
            return _Vector([(labels, _apply_math(operator, left.value, val))
                            for labels, val in right.samples])
        index = {labels: val for labels, val in right.samples}
        out = []
        for labels, val in left.samples:
            if labels in index:
                out.append((labels, _apply_math(operator, val, index[labels])))
        return _Vector(out)

    def _compare(self, node):
        _, operator, left_node, right_node = node
        left, right = self.value(left_node), self.value(right_node)
        if isinstance(left, _Scalar) and isinstance(right, _Scalar):
            return _Scalar(1.0 if _compare_values(operator, left.value, right.value) else 0.0)
        if isinstance(left, _Vector) and isinstance(right, _Scalar):
            return _Vector([(labels, val) for labels, val in left.samples
                            if _compare_values(operator, val, right.value)])
        if isinstance(left, _Scalar) and isinstance(right, _Vector):
            return _Vector([(labels, val) for labels, val in right.samples
                            if _compare_values(operator, left.value, val)])
        index = {labels: val for labels, val in right.samples}
        out = []
        for labels, val in left.samples:
            if labels in index and _compare_values(operator, val, index[labels]):
                out.append((labels, val))
        return _Vector(out)

    def _and(self, node):
        _, left_node, right_node = node[0], node[1], node[2]
        mode = node[3] if len(node) > 3 else None
        left, right = self.value(left_node), self.value(right_node)
        if isinstance(right, _Scalar):
            return _Vector(left.samples if right.truthy() else [])
        if isinstance(left, _Scalar):
            return _Vector(right.samples if left.truthy() else [])
        out = [(labels, val) for labels, val in left.samples
               if any(_labels_match(labels, other, mode) for other, _ in right.samples)]
        if not out and left.samples and right.samples:
            # 两侧都有数据却一条都关联不上：在 PromQL 默认语义下，这通常意味着规则**写错了**
            # （标签集不可能相同 → 该规则永远不会命中）。把它记进证据里，别让它静默当 ok。
            self.notes.append(
                "``and`` 两侧向量均有数据但标签集无交集，且未写 on()/ignoring()："
                "按 PromQL 的精确标签匹配语义，本规则在当前指标标签结构下不可能命中"
                "（很可能是规则缺陷，例如一侧带 scope/chain 标签、另一侧是无标签指标）")
        return _Vector(out)


def _labels_match(left, right, mode=None):
    """``and`` 的标签匹配：默认精确相同；``on(labels)`` 只比指定标签；``ignoring`` 反之。"""
    if mode is None:
        return left == right
    keyword, names = mode
    left_map, right_map = dict(left), dict(right)
    if keyword == "on":
        return all(left_map.get(name) == right_map.get(name) for name in names)
    dropped_left = {key: value for key, value in left_map.items() if key not in names}
    dropped_right = {key: value for key, value in right_map.items() if key not in names}
    return dropped_left == dropped_right


def _apply_math(operator, left, right):
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    if operator == "/":
        return float("nan") if right == 0 else left / right
    if operator == "%":
        return left % right
    raise UnsupportedExpression(f"算术运算符 {operator!r} 不支持")


def _compare_values(operator, left, right):
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == ">":
        return left > right
    if operator == "<":
        return left < right
    if operator == ">=":
        return left >= right
    if operator == "<=":
        return left <= right
    raise UnsupportedExpression(f"比较运算符 {operator!r} 不支持")


def evaluate_expression(expr, model, history=None, now=None):
    """求值一个表达式，返回 ``{"truthy", "value", "series", "thresholds", ...}``。

    ``history`` 是 ``{(metric, labels): [(t, v), ...]}``；缺历史且表达式需要窗口时抛
    :class:`HistoryGap`（调用方翻成 no-data）。
    """
    now = time.time() if now is None else float(now)
    ast = parse_expression(expr)
    evaluator = _Evaluator(model, history or {}, now)
    result = evaluator.value(ast)
    if isinstance(result, _Scalar):
        series = []
        value = result.value
    else:
        series = [{"labels": dict(labels), "value": val} for labels, val in result.samples]
        value = max((item["value"] for item in series), default=None)
        if value is not None and not math.isfinite(value):
            value = None
    return {
        "truthy": result.truthy(),
        "value": value,
        "series": series,
        "notes": list(evaluator.notes),
        "thresholds": thresholds_of(ast),
        "metrics": sorted(required_metrics(ast)),
        "ranges": [{"metric": name, "seconds": seconds}
                   for name, seconds, _ in required_ranges(ast)],
    }


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _human(seconds):
    seconds = float(seconds)
    if seconds >= 3600:
        return f"{seconds / 3600:.2f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.1f}s"


# ===========================================================================
# 五、规则文件 → 规则对象
# ===========================================================================
_ANNOTATION_KEYS = ("summary", "description", "runbook_url")


def load_rules(path=None):
    """读 ``alerts.yml`` → ``[rule, ...]``（每条含 name/expr/for/severity/domain/annotations）。

    规则文件的**唯一性**：这里读的就是 Prometheus 读的那一份文件；本模块不生成、不派生规则。
    """
    target = Path(path) if path is not None else rules_path()
    text = target.read_text(encoding="utf-8")
    document = _loads_yaml(text) or {}
    groups = document.get("groups")
    if not isinstance(groups, list):
        raise ValueError(f"{target}: 顶层缺 groups 列表")
    rules = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "")
        for raw in group.get("rules") or []:
            if not isinstance(raw, dict):
                continue
            labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
            annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
            expr = " ".join(str(raw.get("expr") or "").split())
            rule = {
                "name": str(raw.get("alert") or raw.get("record") or ""),
                "group": group_name,
                "expr": expr,
                "for": str(raw.get("for") or "0m"),
                "for_seconds": _for_seconds(raw.get("for")),
                "severity": str(labels.get("severity") or "warning"),
                "domain": str(labels.get("domain") or "platform"),
                "labels": dict(labels),
                "annotations": {key: annotations.get(key) for key in _ANNOTATION_KEYS
                                if annotations.get(key) is not None},
                "summary": annotations.get("summary"),
                "source_file": str(target),
            }
            rule["metrics_required"] = sorted(required_metrics_for(rule["expr"]))
            rule["supported"] = not rule.get("unsupported_reason")
            rules.append(rule)
    return rules


def _for_seconds(value):
    text = str(value or "0m").strip()
    if not text:
        return 0.0
    try:
        return _duration_seconds(text)
    except UnsupportedExpression:
        return 0.0


def required_metrics_for(expr):
    """表达式里引用的指标名；表达式不支持时返回空集合（由 unsupported 状态承载原因）。"""
    try:
        return required_metrics(parse_expression(expr))
    except UnsupportedExpression:
        return set()


# ===========================================================================
# 六、求值引擎（三态 + pending + unsupported，含 for 时钟与窗口历史）
# ===========================================================================
class AlertsEngine:
    """把「指标模型采集 + 规则求值 + for 状态机 + 窗口历史」装在一起。

    ``collect`` 是无参回调，返回 :class:`MetricModel`；``clock`` 可注入（测试用固定时钟）。
    """

    def __init__(self, rules=None, collect=None, *, clock=time.time,
                 history_seconds=None, sample_seconds=None):
        self.rules = list(rules) if rules is not None else load_rules()
        self.collect = collect
        self.clock = clock
        self.history_seconds = float(history_seconds if history_seconds is not None
                                     else _env_float(ALERT_HISTORY_ENV, DEFAULT_HISTORY_SECONDS))
        self.sample_seconds = float(sample_seconds if sample_seconds is not None
                                    else _env_float(ALERT_SAMPLE_SECONDS_ENV, DEFAULT_SAMPLE_SECONDS))
        self._history = {}
        self._state = {}
        self._lock = threading.Lock()
        self._last_model = None
        self._last_collected_at = None
        self._collect_error = None
        self._parsed = {}
        self._parse(rules)

    # -- 解析缓存（一次性，把不支持的原因留在规则上） --
    def _parse(self, rules):
        for rule in self.rules:
            expr = rule.get("expr") or ""
            try:
                self._parsed[rule["name"]] = parse_expression(expr)
                rule.setdefault("unsupported_reason", None)
            except UnsupportedExpression as error:
                self._parsed[rule["name"]] = None
                rule["unsupported_reason"] = str(error)
            rule["supported"] = not rule.get("unsupported_reason")

    # -- 采集与历史 --
    def observe(self, model, now):
        history_seconds = self.history_seconds
        horizon = now - history_seconds
        with self._lock:
            for name, samples in model.families.items():
                for sample in samples:
                    key = (name, sample.labels)
                    bucket = self._history.setdefault(key, deque())
                    if bucket and bucket[-1][0] == now:
                        bucket[-1] = (now, sample.value)
                        continue
                    bucket.append((now, sample.value))
                    while bucket and bucket[0][0] < horizon:
                        bucket.popleft()
            for key in [key for key, bucket in self._history.items() if not bucket]:
                self._history.pop(key, None)

    def history_view(self):
        with self._lock:
            return {key: list(bucket) for key, bucket in self._history.items()}

    def history_stats(self, now=None):
        moment = self.clock() if now is None else float(now)
        with self._lock:
            oldest = min((bucket[0][0] for bucket in self._history.values() if bucket),
                         default=None)
            series = len(self._history)
            points = sum(len(bucket) for bucket in self._history.values())
        return {
            "history_seconds": self.history_seconds,
            "series": series,
            "points": points,
            "oldest_sample_age_seconds": None if oldest is None else max(0.0, moment - oldest),
            "sample_seconds": self.sample_seconds,
            "coverage_floor": HISTORY_COVERAGE,
        }

    def tick(self, now=None):
        """采样一次（喂历史 + 求值，推进 for 时钟）。后台线程与 HTTP 端点共用。"""
        moment = self.clock() if now is None else float(now)
        if self.collect is None:
            return None
        try:
            model = self.collect()
            self._collect_error = None
        except Exception as error:  # noqa: BLE001 —— 采集失败是数据，不是崩溃
            self._collect_error = f"{type(error).__name__}: {error}"
            return None
        if not isinstance(model, MetricModel):
            self._collect_error = f"采集器返回了 {type(model).__name__}，不是 MetricModel"
            return None
        self.observe(model, moment)
        self._last_model = model
        self._last_collected_at = moment
        return self.evaluate(model, now=moment, observe=False)

    # -- 求值 --
    def evaluate(self, model=None, *, now=None, observe=True):
        moment = self.clock() if now is None else float(now)
        if model is None:
            if self._last_model is None:
                return None
            model, moment = self._last_model, self._last_collected_at or moment
        if observe:
            self.observe(model, moment)
        history = self.history_view()
        results = [self._evaluate_rule(rule, model, history, moment) for rule in self.rules]
        return results

    def _evaluate_rule(self, rule, model, history, now):
        name = rule["name"]
        base = {
            "rule": name,
            "name": name,
            "group": rule.get("group"),
            "severity": rule.get("severity"),
            "domain": rule.get("domain"),
            "expr": rule.get("expr"),
            "for": rule.get("for"),
            "for_seconds": rule.get("for_seconds"),
            "summary": rule.get("summary"),
            "description": (rule.get("annotations") or {}).get("description"),
            "source_file": rule.get("source_file"),
            "metrics_required": list(rule.get("metrics_required") or []),
            "thresholds": [],
            "threshold": None,
            "operator": None,
            "metric": None,
            "value": None,
            "series": [],
            "evidence": {},
        }
        state, evidence = self._judge(rule, model, history, now)
        base.update(self._apply_for(name, state, now, rule))
        base["evidence"] = evidence
        base.update(evidence.get("exposed", {}))
        return base

    def _judge(self, rule, model, history, now):
        """返回 ``(状态, 证据)``：状态 ∈ RULE_STATES。"""
        name = rule["name"]
        expr = rule.get("expr") or ""
        ast = self._parsed.get(name)
        if ast is None:
            return "unsupported", {
                "reason": rule.get("unsupported_reason") or "表达式不在支持子集内",
                "supported_subset": ("选择器 + 比较 + and + 算术 + "
                                     "rate/increase/delta/changes + time() + offset"),
            }
        required = sorted(required_metrics(ast))
        missing = [item for item in required if not model.present(item)]
        if missing:
            evidence = {
                "reason": ("这些指标在 /metrics 文本里没有任何样本："
                           + "、".join(missing) + "——没有读数就不能判定（不是 ok）"),
                "missing_metrics": missing,
                "metrics_present": [item for item in required if model.present(item)],
            }
            if name in EXTERNAL_RULES:
                evidence["external_note"] = EXTERNAL_RULES[name]
                evidence["judged_by"] = "外部抓取器（Prometheus / 黑盒探针）"
            return "no-data", evidence
        try:
            outcome = evaluate_expression(expr, model, history, now)
        except HistoryGap as gap:
            return "no-data", {
                "reason": f"窗口历史不足：{gap}（不外推、不拿旧值顶替）",
                "ranges": [{"metric": item, "seconds": seconds}
                           for item, seconds, _ in _safe_ranges(ast)],
            }
        except UnsupportedExpression as error:
            return "unsupported", {"reason": str(error)}
        except PrometheusParseError as error:
            return "unsupported", {"reason": f"表达式解析失败：{error}"}

        exposed = {
            "thresholds": outcome["thresholds"],
            "threshold": outcome["thresholds"][0]["threshold"] if outcome["thresholds"] else None,
            "operator": outcome["thresholds"][0]["operator"] if outcome["thresholds"] else None,
            "metric": outcome["thresholds"][0]["metric"] if outcome["thresholds"] else None,
            "value": outcome["value"],
            "series": outcome["series"][:10],
        }
        evidence = {
            "reason": None,
            "ranges": outcome["ranges"],
            "series_count": len(outcome["series"]),
            "exposed": exposed,
        }
        if outcome.get("notes"):
            evidence["warnings"] = list(outcome["notes"])
        if outcome["truthy"]:
            evidence["reason"] = (
                "表达式为真"
                + (f"，命中 {len(outcome['series'])} 条时间序列" if outcome["series"] else ""))
            return "firing-condition", evidence  # 由 _apply_for 决定 firing / pending
        evidence["reason"] = "表达式为假（判定过，且不满足）"
        return "ok", evidence

    def _apply_for(self, name, state, now, rule):
        """把「条件为真」按 ``for`` 展开成 firing / pending，并维护 since。"""
        with self._lock:
            entry = self._state.setdefault(
                name, {"state": None, "since": now, "condition_since": None})
            if state == "firing-condition":
                if entry.get("condition_since") is None:
                    entry["condition_since"] = now
                elapsed = now - entry["condition_since"]
                threshold = float(rule.get("for_seconds") or 0.0)
                if threshold <= 0 or elapsed >= threshold:
                    final = "firing"
                    remaining = 0.0
                else:
                    final = "pending"
                    remaining = threshold - elapsed
            else:
                entry["condition_since"] = None
                final = state
                remaining = None
            if entry["state"] != final:
                entry["state"] = final
                entry["since"] = now
            since = entry["since"]
            condition_since = entry.get("condition_since")
        return {"state": final, "since": _iso(since),
                "since_epoch": since,
                # ``since`` = 进入当前状态的时刻（Prometheus 的 ActiveAt 语义）；
                # ``condition_since`` = 表达式**首次为真**的时刻（pending 的起点）。
                "condition_since": _iso(condition_since),
                "for_remaining_seconds": remaining}

    # -- 汇总 --
    def snapshot(self, model=None, *, now=None):
        """采样一次 + 求值一次，返回可直接 JSON 化的信封（``GET /api/v3/ops/alerts``）。

        每次请求都重新采集（求值器读的就是 Prometheus 会抓到的那份 ``/metrics`` 文本），
        采集失败时如实把 ``collect_error`` 放进信封；只有在**从来没有**采到过模型时才回
        ``ok:false``（拿不到任何读数就别假装有结论）。
        """
        moment = self.clock() if now is None else float(now)
        collect_error = None
        if model is None and self.collect is not None:
            try:
                model = self.collect()
                self._collect_error = None
            except Exception as error:  # noqa: BLE001 —— 采集失败是数据，不是崩溃
                collect_error = f"{type(error).__name__}: {error}"
                self._collect_error = collect_error
                model = None
        stale = False
        if model is None:
            if self._last_model is None:
                return {"ok": False, "error": {
                    "code": "alerts/no-model",
                    "message": collect_error or "尚未采到指标模型（采集器未配置或首次采集失败）"}}
            model, moment, stale = self._last_model, self._last_collected_at or moment, True
        results = self.evaluate(model, now=moment)
        counts = summary_counts(results)
        envelope = {
            "ok": True,
            "as_of": _iso(moment),
            "model_stale": stale,
            "source": ("平台内规则求值器 server.v3_alerts（解析 "
                       "deploy/monitoring/alerts.yml）——**非 Grafana**："
                       "本机未部署 Grafana/Prometheus，这里没有 Grafana 面板"),
            "rules_file": str(rules_path()),
            "rules_file_policy": "与 Prometheus 读的是同一份文件（本模块不派生第二份规则清单）",
            "evaluator": self.history_stats(moment),
            "summary": counts,
            "alerts": sorted(results, key=_sort_key),
        }
        if collect_error:
            envelope["collect_error"] = collect_error
        return envelope


def _safe_ranges(ast):
    try:
        return required_ranges(ast)
    except UnsupportedExpression:
        return []


_STATE_ORDER = {state: index for index, state in enumerate(RULE_STATES)}
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _sort_key(item):
    return (_STATE_ORDER.get(item.get("state"), 9),
            _SEVERITY_ORDER.get(item.get("severity"), 9),
            str(item.get("rule") or ""))


def summary_counts(results):
    counts = {state: 0 for state in RULE_STATES}
    counts["total"] = 0
    for item in results or []:
        state = item.get("state")
        if state in counts:
            counts[state] += 1
        counts["total"] += 1
    return counts


def _iso(stamp):
    if stamp is None:
        return None
    return datetime.fromtimestamp(float(stamp), tz=timezone.utc).isoformat()


# ===========================================================================
# 七、采集器（默认读 /metrics 文本）+ 背景采样线程
# ===========================================================================
def metrics_collector(home, app=None):
    """默认采集器：``GET /metrics`` 的**同一份文本** → 指标模型。

    刻意复用 ``observability.build_metrics_text``：求值器读的是 Prometheus 会抓到的那份
    文本，而不是另开一条取数路径——否则「规则求值结果」与「抓取结果」可能不一致。
    """
    from server import observability  # noqa: PLC0415 —— 惰性 import，避免模块级循环

    def collect():
        text = observability.build_metrics_text(str(home), app=app)
        return parse_prometheus_text(text)

    return collect


_SAMPLER = {"thread": None, "stop": None, "engine": None}


def start_sampler(engine, interval=None):
    """启动背景采样线程（幂等）。返回线程对象；``QUANT_ALERTS_SAMPLER=0`` 时不启动。"""
    if str(os.environ.get(ALERT_SAMPLER_ENV, "1")).strip() in ("0", "false", "False"):
        return None
    if _SAMPLER["thread"] is not None and _SAMPLER["thread"].is_alive():
        return _SAMPLER["thread"]
    stop = threading.Event()
    period = float(interval if interval is not None else engine.sample_seconds)

    def loop():
        while not stop.wait(max(1.0, period)):
            try:
                engine.tick()
            except Exception:  # noqa: BLE001 —— 采样线程绝不因单次失败退出
                continue

    thread = threading.Thread(target=loop, name="quant-alerts-sampler", daemon=True)
    thread.start()
    _SAMPLER.update({"thread": thread, "stop": stop, "engine": engine})
    return thread


def stop_sampler():
    """停止背景采样线程（测试用；生产不需要）。"""
    stop = _SAMPLER.get("stop")
    if stop is not None:
        stop.set()
    _SAMPLER.update({"thread": None, "stop": None, "engine": None})


# ===========================================================================
# 八、路由注册（只读端点）
# ===========================================================================
def register(app, v3_run=None, home=None, deps=None):
    """挂 ``GET /api/v3/ops/alerts`` 与 ``GET /api/v3/ops/alerts/rules``（**只读**）。

    幂等：``app.state.v3_alerts`` 存在即直接返回既有路由（``observability.register`` 会调用
    本函数；将来若把 ``v3_alerts`` 也加进 ``app.py`` 的子模块清单，不会重复注册）。

    ``deps`` 仅供测试注入：``rules`` / ``rules_path`` / ``collect`` / ``clock`` /
    ``sampler``（``False`` 不启动背景采样）/ ``engine``。
    """
    existing = getattr(app.state, "v3_alerts", None)
    if isinstance(existing, dict) and existing.get("routes"):
        return existing["routes"]
    deps = deps or {}
    home = home if home is not None else os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    rules = deps.get("rules")
    if rules is None:
        rules = load_rules(deps.get("rules_path"))
    collect = deps.get("collect") or metrics_collector(home, app)
    engine = deps.get("engine") or AlertsEngine(rules, collect, clock=deps.get("clock") or time.time)
    if deps.get("sampler", True):
        start_sampler(engine, deps.get("sample_seconds"))

    @app.get("/api/v3/ops/alerts")
    async def ops_alerts(state: str = ""):
        """告警三态（firing / pending / ok / no-data / unsupported）——平台内求值，非 Grafana。

        ``?state=firing`` 可只看某一态（值非法则不过滤，不报错——只读端点不该因参数失败）。
        """
        try:
            snapshot = await asyncio.to_thread(engine.snapshot)
        except Exception as error:  # noqa: BLE001 —— 统一信封
            return {"ok": False, "error": {"code": "alerts/internal",
                                           "message": f"{type(error).__name__}: {error}"[:300]}}
        wanted = str(state or "").strip()
        if wanted and wanted in RULE_STATES and snapshot.get("ok"):
            snapshot = dict(snapshot)
            snapshot["alerts"] = [item for item in snapshot["alerts"]
                                  if item.get("state") == wanted]
        return snapshot

    @app.get("/api/v3/ops/alerts/rules")
    async def ops_alerts_rules():
        """规则清单（名字/表达式/for/severity/域/是否在支持子集内）——与 Prometheus 同源。"""
        items = []
        for rule in engine.rules:
            items.append({
                "rule": rule.get("name"),
                "group": rule.get("group"),
                "expr": rule.get("expr"),
                "for": rule.get("for"),
                "severity": rule.get("severity"),
                "domain": rule.get("domain"),
                "supported": bool(rule.get("supported")),
                "unsupported_reason": rule.get("unsupported_reason"),
                "metrics_required": list(rule.get("metrics_required") or []),
                "summary": rule.get("summary"),
                "source_file": rule.get("source_file"),
            })
        return {
            "ok": True,
            "as_of": _iso(time.time()),
            "rules_file": str(rules_path()),
            "source": "平台内规则求值器 server.v3_alerts（与 Prometheus 读同一份 alerts.yml）",
            "count": len(items),
            "supported_functions": list(_SUPPORTED_FUNCS) + ["time", "offset", "and"],
            "rules": items,
        }

    routes = ("/api/v3/ops/alerts", "/api/v3/ops/alerts/rules")
    app.state.v3_alerts = {
        "routes": routes,
        "engine": engine,
        "rules_file": str(rules_path()),
        "note": ("只读端点：求值只读 /metrics 文本与本地落盘缓存，不触发任何写/交易端点；"
                 "规则文件与 Prometheus 共用一份"),
    }
    return routes


def rules_json(path=None):
    """规则清单的 JSON 文本（供 CLI/测试核对；不含求值）。"""
    return json.dumps([{key: rule[key] for key in
                        ("name", "group", "expr", "for", "severity", "domain")}
                       for rule in load_rules(path)],
                      ensure_ascii=False, indent=2)
