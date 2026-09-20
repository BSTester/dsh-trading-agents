"""Prometheus 指标出口（``GET /metrics``，``text/plain; version=0.0.4``）。

存在的理由（规格 §8.3「监控落地」）
-----------------------------------
告警规则要有**可抓取的契约**。既有的 ``/api/v3/metrics`` 是给前端读的人读 JSON，字段随
页面演进；Prometheus 需要的是稳定的文本格式。本模块**只读既有事实、只做格式转换**，
不新增第二事实源：

  * 进程内计数 → ``server.v3_ops.metrics_snapshot()``（工具面 / HTTP 的唯一计数源）；
  * 富途限流 → ``server.v3_ratelimit.metrics_view()``（限流治理的唯一读数口）；
  * OMS 台账 → ``server.v3_ops.OmsLedger.stage_counts()``（本地 JSON，不调上游）；
  * 调度器 / 推送 → ``app.state.scheduler`` / ``app.state.push``（与 ``/healthz`` 同源）；
  * 工作台工具面可达性 + 模拟台账回撤 → **缓存探测**（``QUANT_METRICS_PROBE_TTL`` 秒
    至多一次，后台线程刷新）。抓取路径**永不阻塞**：探测在飞时直接给上一次的快照。
    这样监控自己不会把工作台打热，也不会把探测调用喂进被测的 MCP 计数；
  * 数据源降级链 → 读 ``<home>/v3-datasource-probe.json``（由 ``/api/v3/sources/status``
    的真实探测落下）。**不在抓取路径上发起任何外部调用**——富途探测要消耗限流额度，
    用监控去触发「被限流」告警是自伤；
  * 行业集中度红线 → 读 ``<home>/v3-risk-probe.json``（由 ``POST /api/v3/metrics/probe/refresh``
    用 ``v3_industry.industry_exposure`` 对 SH/HK/US 各做一次**只读取数**后落下）。
    同理：抓取路径绝不发起富途探测；缓存超过 ``QUANT_RISK_PROBE_MAX_AGE``（默认 6h）
    即视为「没有结论」，此时**不导出** ``quantwb_risk_industry_pct``（只保留时间戳，
    让「缓存过期」这件事本身可被规则看见）。

诚实性纪律（与仓库「数据诚实」一致）
------------------------------------
* **没有真实读数就不导出该指标**，而不是导出 0——「0」会被读成「正常」，那是编造。
  因此首次抓取后的一两秒内看不到探测类指标，属预期；README 已写明。
* 探测失败如实记 ``quantwb_workbench_up 0`` 并把错误原文放进
  ``quantwb_workbench_probe_error``（该 family 帮助文本里写明取值 0 属正常）。
* 本模块**不注册任何交易/写业务端点**，不触碰交易路径，不 import ``trading``。唯一的
  ``POST`` 是 ``/api/v3/metrics/probe/refresh``：它只读取数并写本地探测缓存
  （与 ``GET /api/v3/sources/status`` 同类），不改变平台任何状态。

命名与 lint
-----------
出口用 ``promtool check metrics`` 自检。两个 ``_ms`` 名字会被它标为
「should not contain abbreviated units」，**有意保留**：
``futu_cooldown_remaining_ms`` 与 ``futu_throttle_wait_ms_total`` 直接对应平台自身字段
（``v3_ratelimit.metrics_view()`` 的 ``cooldownRemainingMs`` / ``throttleWaitMs``），
且 ``futu_cooldown_remaining_ms`` 是规格 §8.3 点名要的规则输入。其余 lint 项已按
Prometheus 约定改正（耗时用秒、不加 ``_count`` 后缀）。

测试：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_observability -v``
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Response

from server import v3_ops, v3_ratelimit

__all__ = [
    "DATASOURCE_PROBE_FILENAME",
    "PROBE_TTL_ENV",
    "RISK_PROBE_FILENAME",
    "RISK_PROBE_LIMIT_PCT",
    "RISK_PROBE_MARKETS",
    "RISK_PROBE_MAX_AGE_ENV",
    "build_metrics_text",
    "build_risk_probe_payload",
    "load_datasource_probe",
    "load_risk_probe",
    "probe_industry_exposure",
    "record_datasource_probe",
    "record_risk_probe",
    "register",
    "render_prometheus",
    "reset_probe_cache",
    "risk_probe_max_age",
    "risk_probe_path",
]

#: 数据源降级链探测结果的落盘文件名（``/api/v3/sources/status`` 写、``/metrics`` 读）。
DATASOURCE_PROBE_FILENAME = "v3-datasource-probe.json"

#: 行业暴露探测结果的落盘文件名（``POST /api/v3/metrics/probe/refresh`` 写、``/metrics`` 读）。
RISK_PROBE_FILENAME = "v3-risk-probe.json"

#: 行业暴露探测默认覆盖的市场（与 ``v3_universe`` 支持的三市场一致）。
RISK_PROBE_MARKETS = ("SH", "HK", "US")

#: 行业集中度红线的默认口径（= ``v3_ops.LIMITS["industryPct"]``，不另立第二套阈值）。
RISK_PROBE_LIMIT_PCT = 20.0

#: 行业暴露探测缓存的**最大可信年龄**（秒）。超过即视为「没有结论」，不导出取值类指标。
RISK_PROBE_MAX_AGE_ENV = "QUANT_RISK_PROBE_MAX_AGE"
DEFAULT_RISK_PROBE_MAX_AGE = 21600.0  # 6h = 定时器（默认 30min）漏跑 12 次仍可信

#: ``v3_industry`` 里表示「该市场本来就没有可分析的标的」的错误码（**不是取数失败**）：
#: 这类市场如实记进 ``per_market`` 的 ``no_data``，但不算探测失败、也不导出取值。
RISK_NO_DATA_CODES = ("industry/no-universe", "industry/no-portfolio")

#: 工作台可达性 / 模拟台账探测的缓存时长（秒）。抓取间隔通常 15s，
#: 缓存 60s ⇒ 每分钟至多一次真实探测，且**永不在抓取路径上同步执行**。
PROBE_TTL_ENV = "QUANT_METRICS_PROBE_TTL"
DEFAULT_PROBE_TTL = 60.0

#: 构建版本标签：部署方可用 ``QUANT_BUILD_VERSION`` 覆盖（默认对齐 V3 平台版本）。
VERSION_ENV = "QUANT_BUILD_VERSION"
DEFAULT_VERSION = "3.0"

#: 探测里用到的工作台工具（都是**只读**工具；本模块不碰写类端点）。
PROBE_SCHEDULE_TOOL = "schedule"
PROBE_EQUITY_TOOL = "equity"

#: OMS 六态（与 ``v3_ops.STAGES`` 同源；额外导出 ``unknown`` 以承载表外阶段）
_PROM_STAGES = tuple(v3_ops.STAGES) + ("unknown",)


# ---------------------------------------------------------------------------
# 文本渲染（Prometheus exposition format 0.0.4）
# ---------------------------------------------------------------------------
def _escape(value):
    """标签值 / HELP 文本的转义（Prometheus 只认 ``\\``、``"``、``\\n`` 三种）。"""
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"'))


def _labels(pairs):
    """``[("chain", "kline")]`` → ``{chain="kline"}``；空则返回空串。"""
    items = [(key, value) for key, value in pairs if value is not None and value != ""]
    if not items:
        return ""
    body = ",".join(f'{key}="{_escape(value)}"' for key, value in items)
    return f"{{{body}}}"


def _number(value):
    """渲染一个数值样本；``bool`` 转 0/1，``None`` 返回 ``None``（调用方跳过该样本）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / ±Inf
        return None
    # 整数保持整数形态（计数器没有小数尾巴更易读）
    return str(int(number)) if number.is_integer() else repr(round(number, 6))


class _Writer:
    """按 family 去重地写 ``# HELP`` / ``# TYPE``，再逐行追加样本。"""

    def __init__(self):
        self._lines = []
        self._families = set()

    def family(self, name, kind, help_text):
        if name in self._families:
            return
        self._families.add(name)
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {kind}")

    def gauge(self, name, help_text, value, labels=()):
        text = _number(value)
        if text is None:
            return
        self.family(name, "gauge", help_text)
        self._lines.append(f"{name}{_labels(labels)} {text}")

    def counter(self, name, help_text, value, labels=()):
        text = _number(value)
        if text is None:
            return
        self.family(name, "counter", help_text)
        self._lines.append(f"{name}{_labels(labels)} {text}")

    def text(self):
        # 末尾必须留一个换行（Prometheus 解析器要求最后一个样本以 \n 结束）
        return "\n".join(self._lines) + "\n" if self._lines else ""


def _sanitize_label_value(value):
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# 工作台可达性 / 模拟台账探测（带 TTL 缓存 + 后台刷新）
# ---------------------------------------------------------------------------
def _probe_ttl():
    raw = os.environ.get(PROBE_TTL_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PROBE_TTL
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PROBE_TTL
    return ttl if ttl > 0 else DEFAULT_PROBE_TTL


def _error_text(envelope):
    error = (envelope or {}).get("error") or {}
    code = error.get("code") or "wb/error"
    message = error.get("message") or "unknown"
    return f"{code}: {message}"


def _probe_workbench(v3_run):
    """一次真实只读探测：工作台工具面可达性（``schedule``）+ 模拟台账回撤（``equity``）。

    两个工具都**不在**富途工具集内（``v3_ratelimit.is_futu_tool`` 为假）⇒ 不经限流器、
    不消耗富途配额；也不经 ``v3_ops`` 的计数调用器 ⇒ 不污染 ``quantwb_mcp_*``。
    """
    snapshot = {"probed_at": time.time(), "workbench_up": 0, "error": None,
                "nav": None, "drawdown_pct": None, "drawdown_source": None}
    try:
        schedule = v3_run(PROBE_SCHEDULE_TOOL, {})
    except Exception as error:  # noqa: BLE001 —— 探测失败是数据，不是异常
        snapshot["error"] = f"{type(error).__name__}: {error}"[:300]
        return snapshot
    if isinstance(schedule, dict) and schedule.get("ok"):
        snapshot["workbench_up"] = 1
    else:
        snapshot["error"] = _error_text(schedule if isinstance(schedule, dict) else {})

    if not snapshot["workbench_up"]:
        # 工作台都不可达，再问 equity 只是多打一次上游；如实停在「不可达」。
        return snapshot
    try:
        equity = v3_run(PROBE_EQUITY_TOOL, {"window": 30})
    except Exception as error:  # noqa: BLE001
        snapshot["equity_error"] = f"{type(error).__name__}: {error}"[:300]
        return snapshot
    if isinstance(equity, dict) and equity.get("ok"):
        value = equity.get("value") if isinstance(equity.get("value"), dict) else {}
        current = value.get("current")
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            snapshot["nav"] = float(current)
        raw = value.get("max_drawdown")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            magnitude = abs(float(raw))
            snapshot["drawdown_pct"] = magnitude * 100 if magnitude <= 1 else magnitude
            snapshot["drawdown_source"] = "sim-ledger(max_drawdown)"
    return snapshot


class _ProbeCache:
    """TTL 缓存 + 后台刷新：``snapshot()`` **永不阻塞**，第一次调用即触发一次探测。"""

    def __init__(self, loader, ttl):
        self._loader = loader
        self._ttl = ttl
        self._data = None
        self._at = 0.0
        self._running = False
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            fresh = self._data is not None and (time.time() - self._at) < self._ttl
            if fresh or self._running:
                return self._data
            self._running = True
        threading.Thread(target=self._run, name="quant-metrics-probe",
                         daemon=True).start()
        with self._lock:
            return self._data

    def refresh_now(self):
        """同步刷新一次并返回快照（测试与预热用；生产路径不用它）。"""
        return self._run()

    def _run(self):
        try:
            data = self._loader()
        except Exception as error:  # noqa: BLE001 —— 再兜一层：探测异常也不能打断抓取
            data = {"probed_at": time.time(), "workbench_up": 0,
                    "error": f"{type(error).__name__}: {error}"[:300],
                    "nav": None, "drawdown_pct": None, "drawdown_source": None}
        if not isinstance(data, dict):
            data = {"probed_at": time.time(), "workbench_up": 0,
                    "error": f"探测器返回了 {type(data).__name__}，不是 dict",
                    "nav": None, "drawdown_pct": None, "drawdown_source": None}
        data.setdefault("probed_at", time.time())
        with self._lock:
            self._data = data
            self._at = float(data.get("probed_at") or time.time())
            self._running = False
        return data


#: 模块级缓存（进程内唯一；``register`` 只重置一次，避免多 app 串味）
_CACHE = _ProbeCache(lambda: {}, DEFAULT_PROBE_TTL)


def reset_probe_cache(loader=None, ttl=None):
    """替换进程内探测缓存（测试用；生产只在 ``register`` 时建一次）。"""
    global _CACHE
    _CACHE = _ProbeCache(loader or (lambda: {}),
                         ttl if ttl is not None else _probe_ttl())
    return _CACHE


# ---------------------------------------------------------------------------
# 数据源降级链探测缓存（``/api/v3/sources/status`` 写、``/metrics`` 读）
# ---------------------------------------------------------------------------
def datasource_probe_path(home):
    return Path(home) / DATASOURCE_PROBE_FILENAME


def record_datasource_probe(home, chains):
    """把一次**真实**探测结果落盘（best-effort，失败不阻断业务）。

    ``chains`` 是 ``/api/v3/sources/status`` 的 ``chains`` 列表（``v3_fallback.probe_chains``
    产出）。只保留指标需要的字段，不落 ``attempts``/``rows`` 等大块内容。
    """
    rows = []
    for row in chains or []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "key": _sanitize_label_value(row.get("key")),
            "label": _sanitize_label_value(row.get("label")),
            "primary": _sanitize_label_value(row.get("primary")),
            "fallback": _sanitize_label_value(row.get("fallback")),
            "available": bool(row.get("available")),
            "last_source": _sanitize_label_value(row.get("last_source")),
            "chain_size": row.get("chain_size"),
            "error": None if row.get("error") is None else str(row.get("error"))[:300],
        })
    payload = {"version": 1, "probed_at": time.time(),
               "probed_at_iso": datetime.now(timezone.utc).isoformat(), "chains": rows}
    path = datasource_probe_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return None  # 落盘失败不阻断探测本身（与仓库其余 best-effort 留痕同义）
    return payload


def load_datasource_probe(home):
    """读回 ``record_datasource_probe`` 的落盘；缺失/损坏一律返回 ``None``（不猜）。"""
    try:
        raw = json.loads(datasource_probe_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("chains"), list):
        return None
    return raw


# ---------------------------------------------------------------------------
# 行业集中度探测缓存（``POST /api/v3/metrics/probe/refresh`` 写、``/metrics`` 读）
#
# 为什么要有这个缓存：行业分类只能来自富途（``info_owner_plate`` / ``plate_stock``），
# 一次探测要按标的数打上游。把探测放进 ``/metrics`` 抓取路径就等于「监控自己制造限流」，
# 所以沿用降级链的同一套纪律：**别人探测、/metrics 只读落盘**。
# ---------------------------------------------------------------------------
def risk_probe_path(home):
    return Path(home) / RISK_PROBE_FILENAME


def risk_probe_max_age():
    """缓存最大可信年龄（秒）；环境变量非法/非正 → 默认值（不因配置写错而毁掉守卫）。"""
    raw = os.environ.get(RISK_PROBE_MAX_AGE_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_RISK_PROBE_MAX_AGE
    try:
        age = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RISK_PROBE_MAX_AGE
    return age if age > 0 else DEFAULT_RISK_PROBE_MAX_AGE


def _generated_at(value):
    """缓存里的 ``generated_at`` → float 秒；缺失/非数/非正一律 ``None``（不猜时刻）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    stamp = float(value)
    return stamp if stamp > 0 else None


def _weight_pct(value):
    """仅接受真正的数字（``bool`` 不算、字符串不转）：其余一律 ``None``，不猜。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / ±Inf
        return None
    return number


def _risk_row(market, payload):
    """把一个市场的 ``industry_exposure`` 信封压成落盘字段（**丢弃 mapping/exposures 大块**）。"""
    row = {"market": str(market), "ok": False, "top_industry": None, "top_weight_pct": None,
           "breach": None, "source": None, "missing": None, "universe": None,
           "weight_source": None, "error": None, "no_data": False}
    if not isinstance(payload, dict):
        row["error"] = f"探测器返回了 {type(payload).__name__}，不是信封对象"
        return row
    if not payload.get("ok"):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or "industry/upstream")
        row["error"] = f"{code}: {error.get('message') or '取数失败'}"[:300]
        row["no_data"] = code in RISK_NO_DATA_CODES
        return row
    top = payload.get("top") if isinstance(payload.get("top"), dict) else {}
    weight = _weight_pct(top.get("weightPct"))
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    missing = payload.get("missing")
    universe = payload.get("universe")
    row.update({
        "ok": weight is not None,
        "top_industry": None if top.get("industry") is None else str(top.get("industry")),
        "top_weight_pct": weight,
        "breach": payload.get("breach") if isinstance(payload.get("breach"), bool) else None,
        "source": None if sources.get("plate") is None else str(sources.get("plate")),
        "missing": len(missing) if isinstance(missing, list) else None,
        "universe": len(universe) if isinstance(universe, list) else None,
        "weight_source": (None if sources.get("weights") is None
                          else str(sources.get("weights"))[:200]),
    })
    if not row["ok"]:
        # 信封 ok=true 但没有 top.weightPct：这是**没有读数**，如实记成 error，不导出 0。
        row["error"] = "industry/no-reading: 成功信封里没有 top.weightPct"
        row["no_data"] = True
    return row


def build_risk_probe_payload(per_market, limit_pct=RISK_PROBE_LIMIT_PCT, generated_at=None):
    """把 ``{market: 信封}`` 装配成 ``v3-risk-probe.json`` 的载荷。

    ``scope="max"`` 取**所有给出真实读数的市场**里最高的那个行业暴露；一个都没有时
    ``top_weight_pct=None``、``breach=None``——上层据此**不导出**取值类指标。
    """
    try:
        limit_value = float(limit_pct)
    except (TypeError, ValueError):
        limit_value = RISK_PROBE_LIMIT_PCT
    rows = {}
    for market, payload in sorted((per_market or {}).items()):
        rows[str(market)] = _risk_row(market, payload)
    usable = [row for row in rows.values() if row["ok"] and row["top_weight_pct"] is not None]
    top = max(usable, key=lambda row: row["top_weight_pct"]) if usable else None
    errors = [f"{row['market']}: {row['error']}" for row in rows.values()
              if row["error"] and not row["no_data"]]
    stamp = float(generated_at) if generated_at is not None else time.time()
    return {
        "version": 1,
        "generated_at": stamp,
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "limit_pct": limit_value,
        "markets": sorted(rows),
        "market": None if top is None else top["market"],
        "top_industry": None if top is None else top["top_industry"],
        "top_weight_pct": None if top is None else top["top_weight_pct"],
        "breach": None if top is None else bool(top["top_weight_pct"] > limit_value),
        "source": None if top is None else top["source"],
        "missing": None if top is None else top["missing"],
        "per_market": rows,
        "error": "；".join(errors) if errors else None,
    }


def record_risk_probe(home, payload):
    """把一次**真实**行业暴露探测结果落盘（best-effort，失败不阻断业务）。"""
    if not isinstance(payload, dict):
        return None
    data = dict(payload)
    data["version"] = 1
    data.setdefault("generated_at", time.time())
    data.setdefault("generated_at_iso", datetime.now(timezone.utc).isoformat())
    path = risk_probe_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return None  # 落盘失败不阻断探测本身
    return data


def load_risk_probe(home):
    """读回 ``record_risk_probe`` 的落盘；缺失/损坏/无 ``generated_at`` 一律 ``None``。"""
    try:
        raw = json.loads(risk_probe_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or _generated_at(raw.get("generated_at")) is None:
        return None
    return raw


def probe_industry_exposure(v3_run, home, markets=None, limit_pct=RISK_PROBE_LIMIT_PCT):
    """对每个市场做一次 ``v3_industry.industry_exposure``（**只读取数**）并回 ``{market: 信封}``。

    惰性 import ``server.v3_industry``：它与 ``v3_universe``/``v3_quality`` 同链，
    顶层 import 会把本模块拖进 ``v3_ops`` 的导入环（与 ``v3_fallback`` 的注释同因）。
    """
    from server import v3_industry  # noqa: PLC0415 —— 只为打断导入环，见 docstring

    wanted = tuple(markets) if markets else RISK_PROBE_MARKETS
    out = {}
    for market in wanted:
        try:
            out[str(market)] = v3_industry.industry_exposure(
                v3_run, home, market=str(market), limit_pct=limit_pct)
        except Exception as error:  # noqa: BLE001 —— 单个市场炸了不影响其它市场取证
            out[str(market)] = {"ok": False, "error": {
                "code": "industry/internal", "message": f"{type(error).__name__}: {error}"[:300]}}
    return out


# ---------------------------------------------------------------------------
# 指标装配
# ---------------------------------------------------------------------------
def _render_process(writer, started_at, app=None):
    writer.gauge("quantwb_up", "1 = 本服务正在提供 /metrics（抓取成功即代表进程存活）", 1)
    uptime = max(0.0, time.time() - started_at)
    writer.gauge("quantwb_process_start_time_seconds",
                 "服务进程启动的 Unix 时间戳（秒）；重启频繁度用 changes() 判定", started_at)
    writer.gauge("quantwb_process_uptime_seconds",
                 "服务进程已运行秒数（与 start_time 同源，便于人读）", uptime)
    total = v3_ops.catalog_total()
    # 工具面两个口径**都从运行时读**（绝不写死）：``mcp`` 取自 MCP 注册表（与 ``tools/list``
    # 同源，含 app.py 装配的 v3_* 桥接工具），``domain`` 是六域工具目录条目数。
    surface = v3_ops.mcp_tool_surface(app)
    writer.gauge("quantwb_build_info",
                 "构建信息常量标签（值恒为 1，用于 join 与版本核对）；tools 标签是**六域目录**"
                 "条目数（mcp_tools.TOOLS 导入枚举 + 5 个 V3 本地计算），MCP 工具面见 "
                 "quantwb_tools{scope=\"mcp\"}", 1,
                 labels=(("version", os.environ.get(VERSION_ENV) or DEFAULT_VERSION),
                         ("tools", total), ("domains", len(v3_ops.DOMAINS))))
    # 同一 family 的两条样本必须**紧挨着**写（exposition format 要求 family 连续）；
    # HELP 里写清两个口径的差异，避免「82 与 116 哪个是真工具数」这种混淆。
    tools_help = ("工具面数量，按口径分标签：scope=\"mcp\" = MCP 注册表里真实可列出的工具数"
                  "（与 MCP tools/list 同源，含桥接的 v3_* 工具）；scope=\"domain\" = 平台六域"
                  "工具目录条目数（mcp_tools.TOOLS 导入枚举 + 5 个 V3 本地计算）。"
                  "两者不可相加、不可互相替代")
    writer.gauge("quantwb_tools", tools_help, surface["mcp_total"],
                 labels=(("scope", "mcp"),))
    writer.gauge("quantwb_tools", tools_help, total, labels=(("scope", "domain"),))


def _render_traffic(writer, snapshot):
    http = snapshot.get("http") or {}
    mcp = snapshot.get("mcp") or {}
    writer.counter("quantwb_http_requests_total",
                   "V3 运维面 HTTP 请求数（v3_ops 注册的路由，进程内计数）",
                   http.get("requests"))
    writer.counter("quantwb_http_errors_total",
                   "V3 运维面 HTTP 错误数（信封 ok=false 或内部异常）", http.get("errors"))
    writer.counter("quantwb_mcp_calls_total",
                   "工作台工具面调用数（HTTP /api/wb/* 与 MCP /mcp 同一 handle，同一份计数）",
                   mcp.get("calls"))
    writer.counter("quantwb_mcp_errors_total",
                   "工作台工具面失败调用数（失败率 = errors / calls）", mcp.get("errors"))
    writer.gauge("quantwb_mcp_call_duration_seconds",
                 "工具面调用平均耗时（**秒**；进程内累计 / 调用数。"
                 "Prometheus 约定用基本单位秒，不用毫秒）",
                 (mcp.get("avgMs") or 0) / 1000.0)
    for tool, calls in sorted((mcp.get("tools") or {}).items()):
        writer.counter("quantwb_tool_calls_total", "按工具名的调用数（进程内计数）",
                       calls, labels=(("tool", tool),))


def _render_futu(writer, futu):
    writer.gauge("futu_enabled", "1 = 富途全局限流器已启用", futu.get("enabled"))
    writer.counter("futu_calls_total", "经限流器的富途调用总数", futu.get("calls"))
    writer.counter("futu_coalesced_total",
                   "被单飞（同键并发合并）合并掉的调用数", futu.get("coalesced"))
    writer.counter("futu_retries_total", "退避后重试的调用数", futu.get("retries"))
    writer.counter("futu_rate_limited_total",
                   "命中富途限流（上游返回限流错误）的调用数；> 0 即说明配额被打满",
                   futu.get("rateLimited"))
    writer.counter("futu_throttle_wait_ms_total",
                   "为遵守速率上限而主动等待的累计毫秒", futu.get("throttleWaitMs"))
    writer.gauge("futu_in_flight", "当前在飞的富途调用数（并发上限内的占用）",
                 futu.get("inFlight"))
    writer.gauge("futu_queued", "当前排队等待限流器放行的调用数", futu.get("queued"))
    writer.gauge("futu_cooldown_remaining_ms",
                 "限流冷却剩余毫秒（0 = 未冷却；> 0 表示正在熔断等待）",
                 futu.get("cooldownRemainingMs"))


def _render_oms(writer, ledger):
    orders = ledger.read()  # 一次读，两个 family 共用（避免二次打库/读文件）
    counts = ledger.stage_counts_of(orders)
    for stage in _PROM_STAGES:
        writer.gauge("quantwb_oms_orders", "平台侧 OMS 台账订单数（按生命周期阶段）",
                     counts.get(stage, 0), labels=(("stage", stage),))
    for stage, count in sorted(counts.items()):
        if stage not in _PROM_STAGES:
            writer.gauge("quantwb_oms_orders", "平台侧 OMS 台账订单数（按生命周期阶段）",
                         count, labels=(("stage", stage),))
    # 单笔占比红线（v3_ops.LIMITS["singlePct"] = 2%）：台账里**正在等人工确认**的
    # （stage=manual）单，取其中最大的单笔占比 value / nav_used。ledger 已落盘 value 与
    # nav_used，因此这是既有事实的换算，不需要任何外部探测；没有 manual 单就不导出
    # （缺席 = 「当前没有触发 2% 红线的在途单」，而不是导出 0 让人以为「算过且为 0」）。
    percents = []
    for record in orders.values():
        if not isinstance(record, dict) or str(record.get("stage") or "") != "manual":
            continue
        value = _weight_pct(record.get("value"))
        nav = _weight_pct(record.get("nav_used"))
        if value and nav and value > 0 and nav > 0:
            percents.append(value / nav * 100.0)
    if percents:
        writer.gauge("quantwb_risk_single_order_pct_max",
                     "台账中待人工确认（stage=manual）订单里最大的单笔占比（%）；"
                     "超过 2% 即为平台单笔红线（v3_ops.LIMITS.singlePct）",
                     max(percents))


def _render_risk_industry(writer, cache, *, now=None):
    """行业集中度红线：只读落盘探测缓存，**抓取路径不打任何上游**。

    * 缓存不存在/损坏/无 ``generated_at`` → 整个 family 缺席；
    * 缓存**过期**（超过 ``QUANT_RISK_PROBE_MAX_AGE``）→ 只导出时间戳（让「过期」可见），
      **不导出**取值类指标——拿 6 小时前的暴露当现在的结论就是编造；
    * 缓存里有 ``error`` → 导出 ``quantwb_risk_industry_probe_failed``（值恒 1，原因在标签），
      供规则/值班感知；取值是否存在取决于是否有市场真的给出了读数。
    """
    if not isinstance(cache, dict):
        return
    stamp = _generated_at(cache.get("generated_at"))
    if stamp is None:
        return
    moment = time.time() if now is None else float(now)
    writer.gauge("quantwb_risk_industry_probe_timestamp_seconds",
                 "行业暴露探测缓存的生成时刻（Unix 秒）；用 time() - 该值做新鲜度守卫",
                 stamp)
    if (moment - stamp) > risk_probe_max_age():
        return
    rows = cache.get("per_market") if isinstance(cache.get("per_market"), dict) else {}
    usable = [row for row in rows.values()
              if isinstance(row, dict) and row.get("ok") and _weight_pct(row.get("top_weight_pct")) is not None]
    top = max(usable, key=lambda row: _weight_pct(row["top_weight_pct"])) if usable else None
    if top is not None:
        # 先写 scope="max" 再写各市场：同一 family 的样本必须连续（exposition format 硬要求）。
        writer.gauge("quantwb_risk_industry_pct",
                     "行业集中度（单一行业占组合的最大权重 %）；scope=max 是各市场最高值。"
                     "数据来自落盘探测缓存，抓取路径不发起富途探测",
                     _weight_pct(top["top_weight_pct"]), labels=(("scope", "max"),))
    for row in sorted(usable, key=lambda item: str(item.get("market"))):
        writer.gauge("quantwb_risk_industry_pct",
                     "行业集中度（单一行业占组合的最大权重 %）；scope=max 是各市场最高值。"
                     "数据来自落盘探测缓存，抓取路径不发起富途探测",
                     _weight_pct(row["top_weight_pct"]),
                     labels=(("scope", str(row.get("market"))),))
    if top is not None:
        writer.gauge("quantwb_risk_industry_breach",
                     "1 = 最高行业暴露已超过探测时的 limit_pct 红线（v3_ops.LIMITS.industryPct）",
                     1 if top.get("breach") else 0)
        writer.gauge("quantwb_risk_industry_limit_pct",
                     "探测时实际使用的行业集中度红线（%）；规则里的 20 应与它一致",
                     cache.get("limit_pct"))
        writer.gauge("quantwb_risk_industry_missing",
                     "最高暴露所在市场里**没取到行业分类**的标的数；> 0 说明暴露被低估，"
                     "该读数只是下界",
                     top.get("missing"), labels=(("market", str(top.get("market"))),))
        writer.gauge("quantwb_risk_industry_probe_info",
                     "最高暴露的行业/市场/板块来源（info 指标，值恒为 1）",
                     1, labels=(("market", str(top.get("market"))),
                                ("industry", top.get("top_industry") or "unknown"),
                                ("source", top.get("source") or "unknown")))
    if cache.get("error"):
        # 与 quantwb_workbench_probe_failed 同形：值恒 1、原因在标签，避免「error 标签取值
        # 变化」让 Prometheus 无法断言「探测确实失败过」。
        writer.gauge("quantwb_risk_industry_probe_failed",
                     "行业暴露探测失败标记（值恒为 1；error 标签是各市场失败原因原文）",
                     1, labels=(("error", str(cache.get("error"))[:300]),))


def _render_risk(writer, probe):
    if not isinstance(probe, dict):
        return
    writer.gauge("quantwb_workbench_up",
                 "1 = 工作台工具面（/api/wb/* 与 MCP 同一 handle）最近一次只读探测可达",
                 probe.get("workbench_up"))
    writer.gauge("quantwb_workbench_probe_timestamp_seconds",
                 "最近一次工作台探测的 Unix 时间戳（秒）；新鲜度用 time() - 该值判定",
                 probe.get("probed_at"))
    writer.gauge("quantwb_workbench_probe_nav",
                 "探测读到的模拟台账净值（equity.current，单币种本地台账，不代表券商资产）",
                 probe.get("nav"))
    writer.gauge("quantwb_risk_drawdown_pct",
                 "探测读到的模拟台账最大回撤（百分比）；>=15 即触及平台风控红线",
                 probe.get("drawdown_pct"))
    if probe.get("error"):
        # 失败原因做成 info 指标（值恒为 1，原因在标签里）——不能导出成两个样本，
        # 否则 error 标签的取值会变，Prometheus 反而更难断言「探测确实失败过」。
        writer.gauge("quantwb_workbench_probe_failed",
                     "工作台探测失败标记（值恒为 1；error 标签是失败原因原文）",
                     1, labels=(("error", probe.get("error")),))


def _render_scheduler(writer, scheduler):
    if scheduler is None:
        return
    writer.gauge("quantwb_scheduler_alive", "1 = 服务内调度器线程存活",
                 bool(getattr(scheduler, "alive", False)))
    writer.gauge("quantwb_scheduler_last_error",
                 "1 = 调度器有未清除的最近一次 tick 异常（成功不清除，见 /healthz 口径）",
                 0 if getattr(scheduler, "last_error", None) is None else 1)


def _render_push(writer, push):
    if push is None:
        return
    try:
        from server import futu_push
        status = futu_push.safe_status(push)
    except Exception:  # noqa: BLE001 —— 推送是旁路，绝不因此让 /metrics 失败
        return
    if not isinstance(status, dict):
        return
    blocks = [(channel, status.get(channel)) for channel in ("quote", "trade")]
    blocks = [(channel, block) for channel, block in blocks if isinstance(block, dict)]
    # 一个 family 的样本必须连续（exposition format 的硬要求），故按 family 分两轮写。
    writer.gauge("quantwb_push_enabled", "1 = 富途 WS 推送运行时已启用", status.get("enabled"))
    for channel, block in blocks:
        writer.gauge("quantwb_push_connected",
                     "1 = 该 WS 通道已连接并鉴权（推送是旁路加速，不作唯一事实源）",
                     block.get("connected"), labels=(("channel", channel),))
    for channel, block in blocks:
        writer.counter("quantwb_push_reconnects_total",
                       "该 WS 通道的重连次数（连接不稳的信号）",
                       block.get("reconnects"), labels=(("channel", channel),))


def _render_datasources(writer, cache):
    if not isinstance(cache, dict):
        return
    rows = [row for row in (cache.get("chains") or [])
            if isinstance(row, dict) and row.get("key")]
    if not rows:
        return
    probed_at = cache.get("probed_at")
    available_total = sum(1 for row in rows if row.get("available"))
    # 同上：按 family 分轮写，保证每个 family 的样本连续。
    writer.gauge("quantwb_datasource_chains",
                 "降级链探测缓存里登记的链数量", len(rows))
    writer.gauge("quantwb_datasource_available_chains",
                 "最近一次探测中可用的链数量", available_total)
    for row in rows:
        writer.gauge("quantwb_datasource_available",
                     "1 = 该降级链至少一级真的取到了数据（0 = 主源与降级源全不可用）",
                     1 if row.get("available") else 0,
                     labels=(("chain", row.get("key")),))
    for row in rows:
        writer.gauge("quantwb_datasource_is_fallback",
                     "1 = 该链最近一次命中的是降级源（主源已降级，链路仍在工作）",
                     1 if row.get("available") and row.get("last_source") != row.get("primary")
                     else 0,
                     labels=(("chain", row.get("key")),))
    for row in rows:
        writer.gauge("quantwb_datasource_probe_timestamp_seconds",
                     "该链最近一次真实探测的 Unix 时间戳（秒）",
                     probed_at, labels=(("chain", row.get("key")),))
    for row in rows:
        writer.gauge("quantwb_datasource_probe_info",
                     "该链最近一次命中的来源（info 指标，值恒为 1）",
                     1, labels=(("chain", row.get("key")),
                                ("source", row.get("last_source") or "unknown"),
                                ("primary", row.get("primary") or "")))


def build_metrics_text(home, *, probe_cache=None, started_at=None, app=None):
    """装配全部指标文本。``home`` 决定 OMS 台账与数据源缓存的读取位置。

    ``probe_cache`` 缺省时读**进程内唯一**的 ``_CACHE``（``register`` / ``reset_probe_cache``
    维护它），因此 ``reset_probe_cache`` 能真正替换线上路由正在用的那份缓存。
    """
    cache = probe_cache if probe_cache is not None else _CACHE
    writer = _Writer()
    _render_process(writer, started_at if started_at is not None else _STARTED_AT, app)
    _render_traffic(writer, v3_ops.metrics_snapshot())
    _render_futu(writer, v3_ratelimit.metrics_view())
    try:
        _render_oms(writer, v3_ops.OmsLedger(None, home))
    except Exception:  # noqa: BLE001 —— 台账读不出来不该让整个抓取失败
        pass
    _render_risk(writer, cache.snapshot())
    if app is not None:
        _render_scheduler(writer, getattr(app.state, "scheduler", None))
        _render_push(writer, getattr(app.state, "push", None))
    _render_datasources(writer, load_datasource_probe(home))
    _render_risk_industry(writer, load_risk_probe(home))
    return writer.text()


def render_prometheus(home, **kwargs):
    """``build_metrics_text`` 的别名（语义化命名，供测试与外部调用）。"""
    return build_metrics_text(home, **kwargs)


#: 进程启动时刻（模块导入即记录；uvicorn 单进程，与「服务启动时间」同义）
_STARTED_AT = time.time()


def process_started_at():
    """进程启动时刻（模块导入时间；测试可据此断言 uptime 单调）。"""
    return _STARTED_AT


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
def register(app, v3_run, home, deps=None):
    """挂 ``GET /metrics``（Prometheus 抓取口）与 ``POST /api/v3/metrics/probe/refresh``（探测写入器）。

    ``deps`` 仅供测试注入：``probe``（替换工作台探测器）、``probe_ttl``（缓存秒数）、
    ``started_at``（替换进程启动时刻）、``industry_probe``（替换行业暴露探测器，
    签名 ``(markets, limit_pct) -> {market: 信封}``）。
    """
    deps = deps or {}
    ttl = deps.get("probe_ttl")
    loader = deps.get("probe") or (lambda: _probe_workbench(v3_run))
    cache = reset_probe_cache(loader=loader, ttl=ttl)
    started_at = deps.get("started_at") or _STARTED_AT
    industry_probe = deps.get("industry_probe") or (
        lambda markets, limit_pct: probe_industry_exposure(v3_run, home, markets, limit_pct))

    @app.get("/metrics")
    async def metrics():
        """Prometheus 文本出口。**同步组装放线程里**，不阻塞事件循环。

        刻意**不**把 ``cache`` 关进闭包：每次请求读模块级 ``_CACHE``，
        这样 ``reset_probe_cache`` 能真正换掉线上路由用的那份缓存（测试与运维都需要）。
        """
        text = await asyncio.to_thread(build_metrics_text, str(home),
                                       started_at=started_at, app=app)
        return Response(content=text, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.post("/api/v3/metrics/probe/refresh")
    async def metrics_probe_refresh(markets: str = "", limit_pct: float = RISK_PROBE_LIMIT_PCT):
        """**取数并写缓存**（只读语义）：对每个市场跑一次行业暴露探测，落盘 ``v3-risk-probe.json``。

        这是 ``/metrics`` 那两条行业红线指标的**触发者**——抓取路径自己绝不发起富途探测
        （富途调用要过限流器，用监控触发「被限流」告警是自伤）。本端点与
        ``GET /api/v3/sources/status`` 同类：只读取数 + 本地落盘，**不碰任何交易/写端点**。
        由 ``platform/install/quant-v3-probe.timer`` 定时调用（也可人工演练）。
        """
        wanted = [item.strip().upper() for item in str(markets or "").split(",")
                  if item.strip()] or list(RISK_PROBE_MARKETS)

        def run():
            envelopes = industry_probe(wanted, limit_pct)
            payload = build_risk_probe_payload(envelopes, limit_pct=limit_pct)
            written = record_risk_probe(home, payload) is not None
            return payload, written

        try:
            payload, written = await asyncio.to_thread(run)
        except Exception as error:  # noqa: BLE001 —— 统一信封，不把栈透给调用方
            return {"ok": False, "error": {"code": "metrics/probe-failed",
                                           "message": f"{type(error).__name__}: {error}"[:300]}}
        return {
            "ok": True,
            "written": written,
            "probe_path": str(risk_probe_path(home)),
            "note": ("探测只读取数（v3_industry.industry_exposure）并写本地缓存，"
                     "不触发任何交易/写端点；抓取路径 /metrics 只读该缓存。"),
            "probe": payload,
        }

    app.state.observability = {"routes": ("/metrics", "/api/v3/metrics/probe/refresh")}
    return cache
