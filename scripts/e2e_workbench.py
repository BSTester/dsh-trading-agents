#!/usr/bin/env python3
"""工作台后端端点全量 E2E 扫描（**只报告，不修复**）。

用途：对正在运行的工作台服务（默认 http://127.0.0.1:8397，sim 模式）做端到端扫描：
  阶段 0  状态快照（库表行数 / kv 关键键 / kill 文件 / pending 指令 / 配置文件指纹）
  阶段 1  读端点全扫（82 项声明端点里的读类，逐项最小合法载荷，分类 ok/degraded/error）
  阶段 1b section 全扫（f10_detail 26 项 + derivative_detail 4 项）
  阶段 2  写/动作端点的**可逆**检查（sim）：kill/unkill、switch-mode 拒绝路径、
          auto_pipeline 写回原值、plan-execute 假 hash、trade_place 三例 + 撤单、
          confirm-decide / rules-decide / research-tasks-* 拒绝路径、push 订阅往返
  阶段 3  一致性交叉核对（声明端点 vs 可达、pipeline 阶段 vs kv ran 标记与当日告警、
          plan/schedule/reconcile/audit 与库表）
  阶段 4  状态复原核对（与阶段 0 对比，按三类判定区分「服务活动 / 审计行 / 真泄漏」）

纪律（可重复运行的前提）：
  * **只做 sim；绝不切 live**（`switch-mode` 只走「无口令被拒」与 sim→sim 幂等两条路径）；
  * 写动作全部可逆，结束时复原：kill 文件删除、`auto_pipeline` 配置**按字节**还原、
    探针下单必撤净（在途单为零才判通过）；
  * `--read-only` 跳过阶段 2 全部写动作（并行任务占用同一服务时用，或 CI 只做只读巡检）；
  * 判定不依赖上一次运行——同一状态连跑两次结论一致（幂等）。

状态变更三类判定（2026-09-17 加固，判据与理由见 `SERVICE_KV_PREFIXES` 常量注释）：
  ① **服务自身活动**：调度器每 tick 写 `daemon:state`、推送/对账写 kv、`kv` 表行数与
     `sqlite_sequence` 自增——与探针无关，**忽略但列出**（首版计成 critical，造成每轮假阳性）；
  ② **审计/记录行**：`risk_checks`/`alerts`/`fills` 新增行、`orders`/`plans` 历史行
     （测试单撤销后仍留审计）——**允许，计数并列出**；但在途单残留另判 critical；
  ③ **真泄漏（critical → 非零退出）**：`pending/` 指令残留、kill 文件残留、配置文件被改写、
     在途单残留、**未知 kv 键**变更、**未知表**行数变更。

输出：终端表格 + `~/.dsh/logs/e2e-workbench-<ts>.json`。
退出码：0 = 无 error/critical 发现且无真泄漏；1 = 存在 error/critical 发现或真泄漏
        （②类审计行不影响退出码）。

用法：
  ~/.dsh/trading-venv/bin/python scripts/e2e_workbench.py [--base URL] [--timeout S]
                                                          [--read-only] [--out DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

HOME = pathlib.Path(os.environ.get("DSH_HOME") or pathlib.Path.home() / ".dsh")
DB = HOME / "trading-data" / "trading.sqlite"
PLATFORM_JSON = HOME / "trading-platform.json"
KILL_FILE = HOME / "trading-kill"
CMDS = HOME / "trading-commands"
LOGS = HOME / "logs"

# 预期内的如实降级码：无凭据 / 无关注池 / 无权限 / 无数据 —— 不是缺陷
DEGRADED_CODES = {
    "trading/openapi-unavailable",
    "trading/core-unavailable",
    "trading/futu-unavailable",
    "trading/push-unavailable",
    "trading/broker-unavailable",
    "trading/no-data",
}
# 富途业务错误里属「能力/权限/无数据」语义的 errcode（-8 不支持 / -9 无权限 / -10 无数据）
DEGRADED_ERRCODES = {-8, -9, -10}

# --------------------------------------------------------------------------- 状态变更三类判定
# 判据（2026-09-17 加固）：首版把「kv 4→5」「sqlite_sequence 1→2」计成 critical，但那些
# 主要是**服务自身活动**（调度器每 tick 写 daemon:state、推送对账写 kv）与**探针审计行**
# （risk_checks/alerts/sqlite_sequence）。每轮都报 critical 的判定等于没有判定，故按三类
# 分流：① 忽略但列出；② 允许但列出；③ 真泄漏 → critical。
#
# ① 服务自身活动：这些 kv 键由**服务进程**按自己的节奏写（每 tick / 每次推送 / 每日对账），
#    与探针无关。只列变更，不判缺陷。前缀式白名单 + 少量精确键。
SERVICE_KV_PREFIXES = (
    "daemon:",        # 调度状态：ran 标记 / last_job / last_error
    "push:",          # WS 推送状态与订阅意图
    "reconcile:",     # 对账快照（reconcile:latest）
    "daily:",         # 每日摘要（daily:digest）
    "sync:",          # 同步游标/新鲜度
    "quality:",       # 数据质量作业留痕
    "auto_exec:",     # 自动执行当日幂等标记
    "factors:",       # 因子快照作业留痕
    "research:",      # 研究快照作业留痕
    "sentiment:",     # 情绪采集留痕
    "rule:",          # 规则周度再平衡的上次权重（运行时写）
    "sync_bars:", "merge_announcements:", "fundamentals:", "universe:",
)
SERVICE_KV_EXACT = frozenset({
    "last_reject",    # 最近一次拒单（broker 适配写）
    "daemon:last_job", "daemon:last_error", "daemon:state",
})
# ② 审计/记录类表：探针自己会写（拒单留痕、告警、成交回填），以及测试单撤销后仍留的
#    历史订单/计划行。允许，但计数并列出——它们不是「未复原」，是**审计事实**。
AUDIT_TABLES = frozenset({"risk_checks", "alerts", "fills"})
RECORD_TABLES = frozenset({"orders", "plans"})
# 服务活动类表：行数变化只说明服务在跑（kv 每次 tick 写、sqlite_sequence 是自增序列）。
SERVICE_TABLES = frozenset({"kv", "sqlite_sequence"})
# ③ 真泄漏判定用：这些表**不允许**在扫描期间出现非探针新增行（在途单另由阶段 2 断言）。
#    其余未列名的表出现行数变化 → 一律按泄漏处理（保守：未知即危险）。

# 在途订单状态（阶段 4 复核用；与 core oms.OPEN_STATES 同口径）
IN_FLIGHT_STATES = ("draft", "frozen", "submitting", "submitted", "partial", "unknown")
# 有「输入必填」契约的分析端点：空载荷必须被拒（invalid-operation），且 happy path 需 tickers
TICKER_ENDPOINTS = {
    "correlation": (2, 3),   # 最少 2 个标的
    "factors": (2, 3),
    "ic": (3, 3),            # IC 需横截面 ≥3
}
# 契约拒绝码：修后语义是 invalid-operation；过渡期服务仍可能回 analytics-unavailable
CONTRACT_PARAM_CODES = ("trading/invalid-operation", "trading/analytics-unavailable")
# 分析类参数错误的**期望**码（修后语义）；仍见到旧码时计一条「修复进行中」的 finding
EXPECTED_PARAM_CODE = "trading/invalid-operation"

F10_SECTIONS = [
    "analyst_consensus", "buybacks", "company_executive_background", "company_executives",
    "company_operational_efficiency", "company_profile", "dividends",
    "earnings_price_history", "earnings_price_move", "holder_detail", "holding_changes",
    "insider_holders", "insider_trades", "institutional", "morningstar", "rating_summary",
    "revenue_breakdown", "shareholders_overview", "splits", "statements", "top_brokers",
    "top_brokers_history", "valuation_detail", "valuation_index_stock_plates",
    "valuation_index_stocks", "valuation_plate_stocks",
]
DERIVATIVE_SECTIONS = [
    "future_info", "option_exercise_probability", "option_volatility", "reference_future",
]

# --------------------------------------------------------------------------- section 必填参数
# 锁定表 §C.5 里两个 section 的**官方必填**查询参数（其余 section 只需 symbol，由适配器
# 从 `code` 位置传入）。修后语义：缺必填 → `trading/invalid-operation` 且 message 点名参数
# （不是 `futu-unavailable`——那会把「参数写错」误导成「通道不可用」）。给足参数应成功或如实降级。
F10_SECTION_PARAMS = {
    "top_brokers_history": {"days_before": 30},        # 官方必填 1..365；仅港股
    "company_executive_background": {"leader_name": "张勇"},  # 官方必填
}
# 缺参契约探针：section → 期望出现在错误消息里的参数名（用于判定「点名了参数」）
SECTION_REQUIRED_PARAM = {
    "top_brokers_history": "days_before",
    "company_executive_background": "leader_name",
}
# `future_info` 的传输层签名收 `code_list`（列表），而聚合端点按位置传单个 `code`：
# 这是**契约形态待定**的一节——探针只如实记录观察到的码，交给 findings 判定，
# 不在脚本里替服务端定契约（缺参/形态错都应为 invalid-operation，不应是通道不可用）。
DERIVATIVE_CODE_LIST_SECTIONS = ("future_info",)

# 少数端点只有 GET 读法（GET 专用路由，见 platform/server/app.py 的 @app.get 注册）。
# 其余只读端点一律 POST + 空载荷（仓库约定，与 plan/schedule/reconcile 同形）。
GET_ENDPOINTS = {"openapi_config", "auto_pipeline"}
# 交易域标的（A 股无实时权限，行情读用 HK/US；sim 下单用低价 A 股控制名义）
SYM_HK = "HK.00700"
SYM_US = "US.AAPL"
SYM_A = "SH.601988"        # 中国银行：A 股 sim 账户可提交、lot=100、名义小（远低于规则 4 预算）
OPTION_SYMBOL = "HK.TCH260918C325000"  # 由 option_chain 现场发现，此处为兜底默认
#: option_screen 的最小**合法**载荷 —— **镜像** `platform/server/futu_data.OPTION_SCREEN_EXAMPLE`
#: （真机验证过的最小示例）。漂移由 `tests/test_wp12_surface.py` 的锁断言拦截（两处必须一致）。
#: 为什么不用 `{"field_filter": {"filter_list": []}}`：空数组/空对象/0 是上游 **-3** 的
#: 形状（服务端已按 proto 占位规则前置拒绝），那是「期望被拒」的载荷，不是能力探针——
#: 能力探针必须发真实可用值，否则每条都会红（E2E 2026-09-17 的分诊结论）。
OPTION_SCREEN_PROBE = {
    "filter": {
        "strategy": {"market_category_list": [0], "filter_group_list": [
            {"option_list": [{"indicator_type": 1003,
                              "indicator_value": {"value_list": [1]}}]}]},
        "field_filter": {"option_type": 1, "volume": 1, "implied_volatility": 1},
        "limit": 3,
    }
}
#: 欠指定载荷（空数组占位）——**期望被本地前置拒绝**（`invalid-operation` 且零上游调用）。
#: 与服务端 `_is_field_filter_placeholder` 同依据（proto 占位规则）。
OPTION_SCREEN_UNDERSPECIFIED = {"filter": {"field_filter": {"filter_list": []},
                                           "strategy": {"market_category_list": [1]}}}
# A 股下单锚定价（**A 股无实时权限且该标的本地无 bars**，故只能用锚定值 + 价格网格）。
# 券商对越出当日 ±10% 价格带的限价单直接拒（`backend business error`），因此网格在
# 锚定价 ±10% 内逐档试：第一个被接受的价自然落在带内。**锚定价漂移时请按当日收盘更新**
# （网格全被拒时脚本会记「探针不可判定」并提示，不会误报成平台缺陷）。
PRICE_ANCHOR_A = 6.58
# 候选探针标的（低价 A 股，lot=100，名义远低于规则 4 预算）。**多候选**的理由：闸门的
# OMS 在途查重按 (symbol, side) 阻塞，若历史扫描/人工探针留下幽灵在途单，单标的会让
# 下单/撤单链路永久不可验证（2026-09-17 实测踩到）。第一个未被阻塞的候选用于探针。
ORDER_CANDIDATES = [("SH.601988", 6.58), ("SH.601398", 7.20), ("SH.601288", 5.10)]
# 交易写端点的**修后契约**（HEAD f86783f 起）：
#   * 被券商拒 → ok:false + error.code="trading/order-rejected"，message 带上游原因/errcode
#   * submitted/cancelled → ok:true + value.status
REJECTED_CODE = "trading/order-rejected"


# --------------------------------------------------------------------------- 基础HTTP
class Client:
    def __init__(self, base: str, timeout: float):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def call(self, endpoint: str, payload=None, method: str = "POST"):
        url = f"{self.base}/api/wb/{endpoint}" if not endpoint.startswith("/") else f"{self.base}{endpoint}"
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body if method == "POST" else None, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as error:  # 4xx/5xx 也要拿到 body
            raw = error.read().decode("utf-8", "replace")
            status = error.code
        except Exception as error:  # noqa: BLE001 —— 连接层失败如实记录
            return {"endpoint": endpoint, "payload": payload, "http": None,
                    "ms": int((time.time() - t0) * 1000), "raw": "", "body": None,
                    "transport_error": f"{type(error).__name__}: {error}"}
        ms = int((time.time() - t0) * 1000)
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return {"endpoint": endpoint, "payload": payload, "http": status, "ms": ms,
                "raw": raw, "body": parsed, "transport_error": None}

    def health(self):
        return self.call("/healthz", None, method="GET")


def classify(result):
    """→ (category, code, message, shape_problems)"""
    problems = []
    if result.get("transport_error"):
        return "error", "transport", result["transport_error"], ["transport failed"]
    if result.get("http") != 200:
        problems.append(f"HTTP {result.get('http')}")
    body = result.get("body")
    if not isinstance(body, dict):
        problems.append("响应不是 JSON 对象")
        return "error", "non-json", (result.get("raw") or "")[:160], problems
    ok = body.get("ok")
    if not isinstance(ok, bool):
        problems.append("envelope 缺少 bool 型 ok")
        return "error", "shape", json.dumps(body, ensure_ascii=False)[:160], problems
    if ok:
        if body.get("value") is None and "value" not in body:
            problems.append("ok=true 但缺 value 键")
        return "ok", None, None, problems
    error = body.get("error")
    if not isinstance(error, dict) or not error.get("code") or not error.get("message"):
        problems.append("ok=false 但 error.code/message 不全")
        return "error", "shape", json.dumps(body, ensure_ascii=False)[:160], problems
    code = error["code"]
    errcode = (error.get("details") or {}).get("errcode")
    if code in DEGRADED_CODES or errcode in DEGRADED_ERRCODES:
        return "degraded", code, str(error["message"])[:160], problems
    return "error", code, str(error["message"])[:160], problems


# --------------------------------------------------------------------------- 状态快照
def db_counts():
    """库级快照：表行数 + 在途单分布 + **全部 kv 键**。

    kv 取全量（而非首版的 5 个固定键）是为了让阶段 4 能**按键**归类：服务自身活动的键
    （`daemon:*` 等）忽略，未知键变更按泄漏处理——固定键列表既漏报未知键，又会把
    `daemon:state` 这种每 tick 都变的键误报成 critical。
    """
    out = {}
    if not DB.exists():
        return {"__missing__": True}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as error:
                out[table] = f"error:{error}"
        try:
            out["__orders_by_status__"] = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT status, COUNT(*) FROM orders GROUP BY status")}
        except sqlite3.Error:
            out["__orders_by_status__"] = {}
        try:
            out["__kv_all__"] = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM kv")}
        except sqlite3.Error:
            out["__kv_all__"] = {}
    finally:
        conn.close()
    return out


def in_flight_orders(counts):
    """在途单分布（阶段 2 与阶段 4 共用同一判据）。"""
    return {k: v for k, v in (counts.get("__orders_by_status__") or {}).items()
            if k in IN_FLIGHT_STATES}


def in_flight_pairs():
    """在途单的 (symbol, side) 集合——闸门在途查重的阻塞键（按此跳过被阻塞的探针标的）。"""
    if not DB.exists():
        return set()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(IN_FLIGHT_STATES))
        return {(r[0], r[1]) for r in conn.execute(
            f"SELECT symbol, side FROM orders WHERE status IN ({marks})", IN_FLIGHT_STATES)}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def kv_classify(key):
    """kv 键 → "service"（服务自身活动）/ "leak"（未知键，保守判泄漏）。"""
    if key in SERVICE_KV_EXACT or any(key.startswith(p) for p in SERVICE_KV_PREFIXES):
        return "service"
    return "leak"


def table_classify(table):
    """表名 → "audit" | "record" | "service" | "leak"（未列名即泄漏，保守）。"""
    if table in AUDIT_TABLES:
        return "audit"
    if table in RECORD_TABLES:
        return "record"
    if table in SERVICE_TABLES:
        return "service"
    return "leak"


def state_diff(base, after):
    """阶段 4 差异归类 → [{key, kind, before, after, why}]，kind ∈ service|audit|record|leak。

    三类判定的**唯一实现**（阶段 4 与 JSON 产物共用），避免「打印一套、判定另一套」。
    """
    out = []
    b_db, a_db = base["db"], after["db"]

    # --- 表行数 / 在途单分布
    for key in sorted(set(b_db) | set(a_db)):
        if key == "__kv_all__":
            continue
        before, after_value = b_db.get(key), a_db.get(key)
        if before == after_value:
            continue
        if key == "__orders_by_status__":
            out.append({"key": "orders 状态分布", "kind": "record",
                        "before": before, "after": after_value,
                        "why": "订单状态分布变化（探针单已撤销；历史行属审计事实）"})
            continue
        kind = table_classify(key)
        out.append({"key": f"表 {key}", "kind": kind, "before": before, "after": after_value,
                    "why": {
                        "audit": "审计行新增（探针产生的风控/告警/成交留痕，允许）",
                        "record": "记录行变化（测试订单/计划的历史行，属审计事实）",
                        "service": "服务自身活动写库（调度/推送/对账），与探针无关",
                        "leak": "未登记的表出现行数变化——未知即危险，保守判泄漏",
                    }[kind]})

    # --- kv 按键归类（服务内部键忽略；未知键保守判泄漏）
    b_kv, a_kv = b_db.get("__kv_all__") or {}, a_db.get("__kv_all__") or {}
    for key in sorted(set(b_kv) | set(a_kv)):
        before, after_value = b_kv.get(key), a_kv.get(key)
        if before == after_value:
            continue
        kind = kv_classify(key)
        out.append({"key": f"kv {key}", "kind": kind,
                    "before": str(before)[:120], "after": str(after_value)[:120],
                    "why": "服务内部键（调度/推送/对账按自身节奏写）" if kind == "service"
                           else "未登记的 kv 键变更——可能是探针泄漏，保守判泄漏"})

    # --- 文件级（这三项任何变化都是真泄漏：探针必须自行复原）
    b_files, a_files = base["files"], after["files"]
    if b_files.get("kill_file") != a_files.get("kill_file"):
        out.append({"key": "files.kill_file", "kind": "leak",
                    "before": b_files.get("kill_file"), "after": a_files.get("kill_file"),
                    "why": "kill 文件残留＝紧急制动未解除"})
    if (a_files.get("pending_count") or 0) > (b_files.get("pending_count") or 0):
        out.append({"key": "files.pending_count", "kind": "leak",
                    "before": b_files.get("pending_count"), "after": a_files.get("pending_count"),
                    "why": "pending/ 指令残留＝调度器未消费完，下次运行会看到陈旧指令"})
    elif b_files.get("pending_count") != a_files.get("pending_count"):
        out.append({"key": "files.pending_count", "kind": "service",
                    "before": b_files.get("pending_count"), "after": a_files.get("pending_count"),
                    "why": "pending 被调度器消费（服务自身活动）"})
    if b_files.get("config_md5") != a_files.get("config_md5") or \
            b_files.get("config_bytes") != a_files.get("config_bytes"):
        out.append({"key": "files.config", "kind": "leak",
                    "before": f"{b_files.get('config_md5', '')[:8]}/{b_files.get('config_bytes')}",
                    "after": f"{a_files.get('config_md5', '')[:8]}/{a_files.get('config_bytes')}",
                    "why": "trading-platform.json 被改写且未按字节复原"})
    return out



def file_state():
    config_bytes = PLATFORM_JSON.read_bytes() if PLATFORM_JSON.exists() else b""
    pending = sorted(p.name for p in (CMDS / "pending").glob("*.json")) if (CMDS / "pending").exists() else []
    processed = sorted(p.name for p in (CMDS / "processed").glob("*.json")) if (CMDS / "processed").exists() else []
    return {
        "kill_file": KILL_FILE.exists(),
        "pending_commands": pending,
        "pending_count": len(pending),
        "processed_count": len(processed),
        "config_md5": hashlib.md5(config_bytes).hexdigest(),
        "config_bytes": len(config_bytes),
    }


def baseline(client):
    return {"db": db_counts(), "files": file_state(),
            "health": (client.health().get("body") or {}),
            "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}


# --------------------------------------------------------------------------- 阶段 1
def scan_reads(client, read_probes, results, findings):
    print("\n=== 阶段 1：读端点全扫 ===")
    for endpoint, payload, expect in read_probes:
        # GET 专用端点用 GET（此前误用 POST 造成一条假阳性：openapi_config 报
        # 「Expected application/json」——那是 POST 空体被拒，不是端点缺陷）
        method = "GET" if endpoint in GET_ENDPOINTS else "POST"
        result = client.call(endpoint, payload if method == "POST" else None, method=method)
        category, code, message, problems = classify(result)
        results.append({"phase": "read", "endpoint": endpoint, "payload": payload,
                        "category": category, "code": code, "message": message,
                        "http": result.get("http"), "ms": result.get("ms"),
                        "expect": expect, "shape_problems": problems,
                        "snippet": (result.get("raw") or "")[:220]})
        mark = {"ok": "✅", "degraded": "🟡", "error": "❌"}[category]
        print(f"  {mark} {endpoint:26s} {str(code or ''):34s} {str(message or '')[:70]}")
        if category == "error":
            findings.append(finding(
                "error" if result.get("http") != 200 else "important",
                f"读端点未预期失败：{endpoint}", endpoint, payload, result,
                "期望：成功，或如实降级（openapi/core/futu/push-unavailable、-8/-9/-10）",
                "端点返回了非预期错误；需判断是实现缺陷还是未登记的降级"))
        elif problems:
            findings.append(finding("minor", f"信封形状问题：{endpoint}", endpoint, payload,
                                    result, "envelope 必须是 {ok:bool, value?|error{code,message}}",
                                    "形状不齐会破坏前端统一处理"))
        if category == "ok" and (result.get("body") or {}).get("value", "MISSING") is None:
            # 可疑项（**不判 critical**）：ok:true + value:null 说明「成功但没有数据」，
            # 与「如实降级」不可区分。首版把它当 important 缺陷，但它也可能是端点的
            # 合法空态（如该标的当日无数据）——故只登记待复现，不作退出码依据。
            item = finding("important", f"可疑：成功信封携带 null value：{endpoint}",
                           endpoint, payload, result,
                           "ok=true 时应给出数据，或如实降级说明取不到的原因",
                           "null 与「空数据」不可区分，调用方无法判断是真无数据还是取数失败；"
                           "登记为可疑项，复现后再升级")
            item["category"] = "suspicious"
            findings.append(item)


def scan_contract_params(client, results, findings):
    """输入必填端点的**契约**断言：空载荷必须被如实拒绝。

    与 happy path 分开（阶段 1 给合法 tickers 证明能力可用，这里给空载荷证明坏参数不被
    当成功）。修后语义是 `trading/invalid-operation`，且**不得**是 `analytics-unavailable`
    ——后者会让调用方以为「分析服务不可用」而不是「我参数写错了」。过渡期服务可能仍回旧码：
    两种码都接受，但见到旧码记一条「修复进行中」的 finding（不判 critical，避免与本轮
    并行修复任务打架）。
    """
    print("\n=== 阶段 1c：输入必填端点的空载荷契约 ===")
    for endpoint in TICKER_ENDPOINTS:
        result = client.call(endpoint, {})
        body = result.get("body") or {}
        code = ((body.get("error") or {}) or {}).get("code")
        message = str(((body.get("error") or {}) or {}).get("message") or "")
        ok = body.get("ok")
        accepted = ok is False and code in CONTRACT_PARAM_CODES
        results.append({"phase": "contract", "endpoint": endpoint, "payload": {},
                        "category": "ok" if accepted else "error", "code": code,
                        "message": message, "http": result.get("http"),
                        "ms": result.get("ms"), "shape_problems": [],
                        "snippet": (result.get("raw") or "")[:220],
                        "expect": f"空载荷 → {EXPECTED_PARAM_CODE}（点名必填 tickers）"})
        print(f"  {'✅' if accepted else '❌'} {endpoint:26s} 空载荷 → {code}  {message[:60]}")
        if not accepted:
            findings.append(finding("important", f"输入必填端点的空载荷未被如实拒绝：{endpoint}",
                                    endpoint, {}, result,
                                    f"空载荷应回 {EXPECTED_PARAM_CODE}（点名必填参数）",
                                    "坏参数被当成成功或归错码，会让调用方误判原因"))
        elif code != EXPECTED_PARAM_CODE:
            item = finding("minor", f"参数错误码仍是过渡码（修复进行中）：{endpoint}",
                           endpoint, {}, result,
                           f"修后语义应为 {EXPECTED_PARAM_CODE}",
                           f"当前回 {code}；本轮并行修复落地后应变为 {EXPECTED_PARAM_CODE}"
                           "（验收判据：重跑后本条消失）")
            item["category"] = "transition"
            findings.append(item)


def section_accepts(ok, code, message, param, errcode=None):
    """section 探针的接受判据（修后语义 + 过渡容忍）。

    接受：成功、如实降级（含 `-8/-9/-10` 业务码——上游"无数据/无权限"不是缺陷）、
    或 `invalid-operation`（参数形态/必填问题）。
    过渡容忍：服务端在本轮修复前可能仍把缺必填归成 `futu-unavailable`
    （根因是 TypeError 落进通道兜底）——**接受但计一条「修复进行中」**。
    """
    if ok is True:
        return "ok", None
    if code in DEGRADED_CODES or errcode in DEGRADED_ERRCODES:
        return "ok", None
    if "invalid-operation" in str(code or ""):
        return "ok", None
    if code in ("trading/futu-unavailable", "trading/broker-unavailable"):
        return "transition", "缺必填/形态错被归成通道不可用（修复进行中：应为 invalid-operation）"
    if param and param in str(message):
        return "ok", None
    return "error", None


def result_errcode(result):
    """从信封里取富途业务码（`details.errcode`），无则 None。"""
    body = result.get("body") or {}
    return ((body.get("error") or {}) or {}).get("details", {}).get("errcode")


def scan_sections(client, results, findings):
    print("\n=== 阶段 1b：section 全扫（f10 26 + derivative 4）===")
    for section in F10_SECTIONS:
        sym = SYM_US if section in ("analyst_consensus", "rating_summary", "institutional",
                                    "earnings_price_history", "earnings_price_move",
                                    "morningstar", "insider_trades", "holding_changes",
                                    "holder_detail", "insider_holders",
                                    "shareholders_overview", "revenue_breakdown",
                                    "buybacks", "splits") else SYM_HK
        params = F10_SECTION_PARAMS.get(section)
        payload = {"code": sym, "section": section}
        if params:
            payload["params"] = params
        result = client.call("f10_detail", payload)
        category, code, message, problems = classify(result)
        body = result.get("body") or {}
        verdict, note = section_accepts(body.get("ok"), code, message,
                                        SECTION_REQUIRED_PARAM.get(section),
                                        errcode=result_errcode(result))
        results.append({"phase": "f10_section", "endpoint": f"f10_detail:{section}",
                        "payload": payload, "category": category,
                        "code": code, "message": message, "http": result.get("http"),
                        "ms": result.get("ms"), "shape_problems": problems,
                        "params_supplied": params is not None,
                        "verdict": verdict, "note": note,
                        "snippet": (result.get("raw") or "")[:180]})
        mark = {"ok": "✅", "degraded": "🟡", "transition": "🟠", "error": "❌"}[verdict]
        print(f"  {mark} {section:32s} {sym:10s} {str(code or ''):30s} {str(message or '')[:56]}")
        if verdict == "error":
            findings.append(finding("important", f"F10 section 未预期失败：{section}",
                                    "f10_detail", payload, result,
                                    "成功、如实降级（-10/-9）或参数错（invalid-operation）",
                                    "section 覆盖不全或错误分类不清"))
        elif verdict == "transition":
            item = finding("minor", f"F10 section 参数错仍是通道码（修复进行中）：{section}",
                           "f10_detail", payload, result,
                           f"缺必填应回 {EXPECTED_PARAM_CODE} 并点名参数",
                           note + "（验收判据：重跑后本条消失）")
            item["category"] = "transition"
            findings.append(item)

        # 缺必填契约：只有「官方必填」的 section 才有意义（其余只需 symbol）
        param = SECTION_REQUIRED_PARAM.get(section)
        if param:
            bare = {"code": sym, "section": section}
            bare_result = client.call("f10_detail", bare)
            bare_body = bare_result.get("body") or {}
            bare_code = ((bare_body.get("error") or {}) or {}).get("code")
            bare_msg = str(((bare_body.get("error") or {}) or {}).get("message") or "")
            verdict2, note2 = section_accepts(bare_body.get("ok"), bare_code, bare_msg, param,
                                              errcode=((bare_body.get("error") or {})
                                                       .get("details", {}) or {}).get("errcode"))
            named = param in bare_msg
            results.append({"phase": "contract", "endpoint": f"f10_detail:{section}(缺必填)",
                            "payload": bare, "category": "ok" if verdict2 != "error" else "error",
                            "code": bare_code, "message": bare_msg,
                            "http": bare_result.get("http"), "ms": bare_result.get("ms"),
                            "shape_problems": [], "param_named": named,
                            "snippet": (bare_result.get("raw") or "")[:180],
                            "expect": f"缺 {param} → {EXPECTED_PARAM_CODE} 且 message 点名 {param}"})
            print(f"      ↳ 缺 {param:14s} → {bare_code}  点名参数={named}  {bare_msg[:44]}")
            if verdict2 == "error":
                findings.append(finding("important", f"缺必填参数未被如实拒绝：{section}",
                                        "f10_detail", bare, bare_result,
                                        f"缺 {param} 应回 {EXPECTED_PARAM_CODE}",
                                        "缺参被当成成功或归错码，调用方会误判原因"))
            elif verdict2 == "transition":
                item = finding("minor",
                               f"缺必填参数仍是通道码（修复进行中）：{section}",
                               "f10_detail", bare, bare_result,
                               f"缺 {param} 应回 {EXPECTED_PARAM_CODE} 并点名参数",
                               note2 + "（验收判据：重跑后本条消失）")
                item["category"] = "transition"
                findings.append(item)
            elif not named:
                findings.append(finding("minor", f"参数错消息未点名参数：{section}",
                                        "f10_detail", bare, bare_result,
                                        f"message 应点名 `{param}` 便于调用方自查",
                                        "只说「参数非法」而不点名，排查成本高"))

    for section in DERIVATIVE_SECTIONS:
        sym = OPTION_CONTRACT[0]
        payload = {"code": sym, "section": section}
        if section in DERIVATIVE_CODE_LIST_SECTIONS:
            # `future_info` 的传输层签名收 `code_list`（列表），聚合端点按位置传单个 `code`：
            # 契约形态待定 → 只如实记录，不在脚本里替服务端定契约。
            sym = SYM_HK
            payload = {"code": sym, "section": section}
        result = client.call("derivative_detail", payload)
        category, code, message, problems = classify(result)
        body = result.get("body") or {}
        if section in DERIVATIVE_CODE_LIST_SECTIONS:
            verdict, note = section_accepts(body.get("ok"), code, message, "code_list",
                                             errcode=result_errcode(result))
        else:
            verdict = {"ok": "ok", "degraded": "ok", "error": "error"}[category]
            note = None
        results.append({"phase": "derivative_section", "endpoint": f"derivative_detail:{section}",
                        "payload": payload, "category": category, "code": code,
                        "message": message, "http": result.get("http"),
                        "ms": result.get("ms"), "shape_problems": problems,
                        "verdict": verdict, "note": note,
                        "snippet": (result.get("raw") or "")[:180]})
        mark = {"ok": "✅", "degraded": "🟡", "transition": "🟠", "error": "❌"}[verdict]
        print(f"  {mark} {section:32s} {sym:24s} {str(code or ''):30s} {str(message or '')[:50]}")
        if section in DERIVATIVE_CODE_LIST_SECTIONS and code == "trading/invalid-operation":
            # **缺陷 1 的现场证据**：`future_info` 的传输层签名收 `code_list`（列表），而聚合
            # 适配层把单标的 `code` 直接当列表传 → 该 section **永远失败**。修后应成功或如实降级。
            findings.append(finding(
                "important", f"{section} 出站装配错误（缺陷 1：单标的未包成列表）",
                "derivative_detail", payload, result,
                "适配层应把单标的 `code` 包成列表后调用（该 section 的合法输入就是标的代码）",
                "该 section 永远失败 → 衍生品覆盖名存实亡。"
                "HEAD 已含修复（回归测试 `tests/test_wp12_section_params.py`）——"
                "**服务需重启加载**；重启后本条应消失"))
        if verdict == "error":
            findings.append(finding("important", f"衍生品 section 未预期失败：{section}",
                                    "derivative_detail", payload, result,
                                    "成功、如实降级（-10/-9）或参数错（invalid-operation）",
                                    "衍生品覆盖不全"))
        elif verdict == "transition":
            item = finding("minor", f"衍生品 section 参数错仍是通道码（修复进行中）：{section}",
                           "derivative_detail", payload, result,
                           f"参数/形态错应回 {EXPECTED_PARAM_CODE}",
                           (note or "") + "（验收判据：重跑后本条消失）")
            item["category"] = "transition"
            findings.append(item)


# --------------------------------------------------------------------------- 阶段 2
def wait_for(predicate, seconds, interval=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def price_grid(anchor, spread=0.10, steps=7):
    """锚定价 ±spread 内的候选价（含锚定价本身），升序、两位小数、去重。

    用途见 `PRICE_ANCHOR_A` 注释：A 股价格带按**当日收盘**算，脚本取不到收盘价时
    用「逐档试 → 第一个被接受的价」代替猜测。升序让命中价尽量低（名义小）。
    """
    lo, hi = anchor * (1 - spread), anchor * (1 + spread)
    out = []
    for i in range(steps):
        price = round(lo + (hi - lo) * i / (steps - 1), 2)
        if price not in out:
            out.append(price)
    return out


def scan_writes(client, results, findings, baseline_state):
    print("\n=== 阶段 2：写/动作端点（可逆检查，sim）===")

    # 扫描前遗留的在途单（幽灵单）：会让「同标的同方向」的下单探针全部被在途查重拦下，
    # 使 kill 验证与下单/撤单链路验证都失去意义。检测到就**明确跳过**相应探针并如实登记，
    # 而不是产出「合法单被拒」的假缺陷（2026-09-17 实测踩到：上一次探针撤单后 OMS 未回写）。
    pre_inflight = in_flight_orders(baseline_state["db"])
    if pre_inflight:
        item = finding("important", "扫描前已存在在途单（幽灵单会阻塞同标的同方向新单）",
                       "orders", None, {"in_flight": pre_inflight},
                       "扫描应从未知/干净状态开始；在途单应为零",
                       "闸门在风控前做 OMS 在途查重 → 幽灵单让后续下单全部被拒，"
                       "本次将跳过下单/撤单链路探针。根因见 S2（撤单未回写 OMS）。"
                       "**已知在修**：`tests/test_e2e_defect_fixes.py::CancelOmsWriteBackTests`")
        item["category"] = "conditional"
        findings.append(item)
        print(f"  ⚠ 扫描前存在在途单 {pre_inflight} → 跳过下单/撤单链路探针（见 S2）")

    def record(name, result, expect, expected_code=None, severity="important"):
        """记录一次写/动作探针。

        `expected_code`：**预期被拒**的探针（如「无口令切 live 必须被拒」）——命中该码
        记 `ok`，否则才是 `error`。首版把这类「按设计拒绝」也计成 error，导致汇总里
        `error=7` 全是假阳性（2026-09-17 加固）。
        """
        category, code, message, problems = classify(result)
        if expected_code is not None:
            category = "ok" if code == expected_code else "error"
        results.append({"phase": "write", "endpoint": name,
                        "payload": result.get("payload"),
                        "category": category, "code": code, "message": message,
                        "http": result.get("http"), "ms": result.get("ms"),
                        "shape_problems": problems, "expected_code": expected_code,
                        "snippet": (result.get("raw") or "")[:220], "expect": expect})
        print(f"  {name:34s} http={result.get('http')} {str(code or ''):32s} {str(message or '')[:60]}")
        return category

    # --- 2.1 kill → 断言拒绝 → unkill
    record("plan-execute(kill)", client.call("plan-execute", {"action": "kill"}),
           "指令入队（写 pending/）")
    appeared = wait_for(lambda: KILL_FILE.exists(), 130)
    if not appeared:
        findings.append(finding("critical", "kill 动作未生效（kill 文件未出现）", "plan-execute",
                                {"action": "kill"}, results[-1],
                                "kill 端点应在 ≤1 个调度周期内创建 ~/.dsh/trading-kill",
                                "kill switch 不可用＝紧急制动失效"))
    else:
        blocked = client.call("trade_place", {"symbol": SYM_A, "side": "BUY", "qty": 100,
                                              "price": 3.29})
        blocked_code = ((blocked.get("body") or {}).get("error") or {}).get("code")
        blocked_msg = ((blocked.get("body") or {}).get("error") or {}).get("message", "")
        results.append({"phase": "write", "endpoint": "trade_place(under kill)",
                        "payload": {"symbol": SYM_A, "side": "BUY", "qty": 100, "price": 3.29},
                        "category": "ok" if blocked_code == "trading/order-rejected" else "error",
                        "code": blocked_code, "message": blocked_msg,
                        "http": blocked.get("http"), "ms": blocked.get("ms"),
                        "snippet": (blocked.get("raw") or "")[:220],
                        "expect": "规则 1 拒单（order-rejected）"})
        print(f"  {'trade_place(under kill)':34s} {str(blocked_code):32s} {blocked_msg[:60]}")
        dedup_first = ("在途" in blocked_msg) or ("查重" in blocked_msg)
        if blocked_code != "trading/order-rejected":
            findings.append(finding("critical", "kill 生效期间下单未被规则 1 拒绝",
                                    "trade_place", blocked.get("payload"), blocked,
                                    "kill switch 必须拒绝一切订单（规则 1）",
                                    "紧急制动可被绕过"))
        elif dedup_first:
            # 闸门的 OMS 在途查重发生在风控之前：若**扫描前就有幽灵在途单**，被拒
            # 理由是查重而不是 kill → 本条**无法验证 kill**（不是 kill 失效）。
            # 如实记为「未验证」，并把根因指向 S2（撤单不回写 OMS）。
            item = finding("important", "kill 拦截未被验证（被 OMS 在途查重抢先）",
                           "trade_place", {"symbol": SYM_A, "side": "BUY", "qty": 100},
                           blocked,
                           "kill 生效时应由风控规则 1 拒单（而非在途查重）",
                           "存在**扫描前遗留的在途单**，闸门在风控前就因在途查重拒单；"
                           "根因是 S2（撤单成功未回写 OMS → 幽灵单永久阻塞同标的同方向新单）。"
                           "**已知在修**：`tests/test_e2e_defect_fixes.py::CancelOmsWriteBackTests`")
            item["category"] = "conditional"
            findings.append(item)
    record("plan-execute(unkill)", client.call("plan-execute", {"action": "unkill"}),
           "指令入队，kill 文件随后删除")
    if not wait_for(lambda: not KILL_FILE.exists(), 130):
        findings.append(finding("critical", "unkill 未生效（kill 文件残留）", "plan-execute",
                                {"action": "unkill"}, results[-1],
                                "unkill 应删除 kill 文件", "状态泄漏：紧急制动无法解除"))

    # --- 2.2 switch-mode 拒绝路径 + 幂等
    record("switch-mode(sim→live 无口令)",
           client.call("switch-mode", {"mode": "live", "expected_mode": "sim"}),
           "必须被拒绝且模式不变", expected_code="trading/invalid-operation")
    after_mode = (client.call("snapshot", {}).get("body") or {}).get("value", {}).get("mode")
    if after_mode != "sim":
        findings.append(finding("critical", "无口令的 sim→live 切换改变了模式！", "switch-mode",
                                {"mode": "live", "expected_mode": "sim"}, results[-1],
                                "实盘切换必须要求口令「确认实盘」", "实盘边界被绕过"))
    record("switch-mode(sim→sim 幂等)",
           client.call("switch-mode", {"mode": "sim", "expected_mode": "sim"}),
           "幂等成功或如实拒绝，模式仍为 sim")

    # --- 2.3 auto_pipeline 写 → 读回 → 按字节复原
    orig_bytes = PLATFORM_JSON.read_bytes() if PLATFORM_JSON.exists() else b""
    read1 = client.call("auto_pipeline", {})
    record("auto_pipeline(读)", read1, "返回生效配置")
    payload = {"enabled": True, "strategies": [{"market": "SH", "strategy": "watchlist_rsi",
                                                "watchlist": "watchlist"}],
               "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
               "exec_window_minutes": 30, "reconcile_at": "19:00"}
    record("auto_pipeline(写 enabled=true)", client.call("auto_pipeline", payload),
           "校验通过并落盘")
    read2 = client.call("auto_pipeline", {})
    enabled_after = ((read2.get("body") or {}).get("value") or {}).get("enabled")
    results.append({"phase": "write", "endpoint": "auto_pipeline(读回)", "payload": {},
                    "category": "ok" if enabled_after is True else "error",
                    "code": None, "message": f"enabled={enabled_after}",
                    "http": read2.get("http"), "ms": read2.get("ms"),
                    "snippet": (read2.get("raw") or "")[:200], "expect": "enabled=true"})
    if enabled_after is not True:
        findings.append(finding("important", "auto_pipeline 写入后读回未生效", "auto_pipeline",
                                payload, read2, "写入应原子生效", "设置页开关不可信"))
    # 复原：按字节写回（保证与基线完全一致）
    try:
        PLATFORM_JSON.write_bytes(orig_bytes)
    except OSError as error:
        findings.append(finding("critical", "auto_pipeline 配置复原失败", "auto_pipeline", None,
                                {"error": str(error)}, "必须还原到扫描前状态",
                                "配置残留会被调度器下一轮读取"))
    read3 = client.call("auto_pipeline", {})
    restored = ((read3.get("body") or {}).get("value") or {}).get("enabled")
    results.append({"phase": "write", "endpoint": "auto_pipeline(复原后读回)", "payload": {},
                    "category": "ok" if restored is False else "error", "code": None,
                    "message": f"enabled={restored}", "http": read3.get("http"),
                    "ms": read3.get("ms"), "snippet": (read3.get("raw") or "")[:200],
                    "expect": "复原为 enabled=false"})
    if restored is not False:
        findings.append(finding("critical", "auto_pipeline 未复原为 enabled=false",
                                "auto_pipeline", None, read3,
                                "扫描必须无残留", "可能让自动流水线在无人察觉时被打开"))

    # --- 2.4 plan-execute 假 hash
    before_pending = file_state()["pending_count"]
    record("plan-execute(假 hash)",
           client.call("plan-execute", {"plan_hash": "0" * 16, "expected_mode": "sim",
                                        "action": "execute"}),
           "接受入队（异步执行时被拒）或直接拒绝")
    time.sleep(2)
    after_pending = file_state()["pending_count"]
    if after_pending > before_pending:
        # 指令已入队：等一轮调度让它被处理（避免留下待处理指令）
        wait_for(lambda: file_state()["pending_count"] <= before_pending, 90)
    results.append({"phase": "write", "endpoint": "plan-execute(假 hash) pending 变化",
                    "payload": None, "category": "ok", "code": None,
                    "message": f"pending {before_pending} → {file_state()['pending_count']}",
                    "http": None, "ms": None, "snippet": "", "expect": "无残留待处理指令"})

    # --- 2.5 下单探针：① 拒单契约（故意越界价）② 可提交单（价网格）+ 撤单回写
    #
    # 为什么改成「价格网格」而不是写死一个价（2026-09-17 加固）：首版用
    # `SYM_A @3.29`（约收盘价一半）→ 越出 ±10% 价格带 → 券商 `backend business error`
    # 拒单 → 被当成「合法单被拒」的假缺陷。价格带依赖**当日收盘**，而 A 股无实时权限、
    # 该标的本地也无 bars，脚本无法直接取到收盘价 → 改为在锚定价 ±10% 内**逐档试**
    # （越界价会被拒，因此「第一个被接受的价格」自然落在带内）。全部档位都被拒 →
    # 记「探针不可判定」（不是平台缺陷），并提示更新锚定价。
    bad_missing = client.call("trade_place", {"symbol": SYM_A, "side": "BUY"})
    record("trade_place(缺必填)", bad_missing, "invalid-operation",
           expected_code="trading/invalid-operation")
    bad_enum = client.call("trade_place", {"symbol": SYM_A, "side": "HOLD", "qty": 100,
                                           "price": PRICE_ANCHOR_A})
    record("trade_place(非法枚举)", bad_enum, "invalid-operation",
           expected_code="trading/invalid-operation")

    # ① 拒单契约探针（**故意**用带外价触发上游拒单），断言**修后契约**：
    #    ok:false + code=order-rejected + message 含上游原因/errcode（HEAD f86783f 起）。
    #    若被本地在途查重抢先（幽灵单）→ 本条**无法验证**，不误判成契约缺陷。
    blocked_now = in_flight_pairs()
    probe_symbol = next((sym for sym, _ in ORDER_CANDIDATES
                         if (sym, "BUY") not in blocked_now), SYM_A)
    outside = round(PRICE_ANCHOR_A * 0.5, 2)
    reject_probe = client.call("trade_place", {"symbol": probe_symbol, "side": "BUY", "qty": 100,
                                               "price": outside})
    rbody = reject_probe.get("body") or {}
    rvalue = rbody.get("value") or {}
    rerror = rbody.get("error") or {}
    rcode = rerror.get("code")
    rmsg = str(rerror.get("message") or rvalue.get("err") or "")
    dedup_hit = ("在途" in rmsg) or ("查重" in rmsg)
    old_style = (rbody.get("ok") is True and rvalue.get("status") == "rejected")
    diag_ok = ("[errcode=" in rmsg) or ("errcode" in rmsg)
    results.append({"phase": "write", "endpoint": "trade_place(带外价 → 拒单契约)",
                    "payload": {"symbol": SYM_A, "price": outside, "qty": 100},
                    # dedup_hit（被本地在途查重抢先）时本条**无法证明上游拒单契约**，
                    # 但也不构成契约缺陷 → 记 ok 并在 message 里标 dedup（幽灵单条目另记）。
                    "category": "ok" if (dedup_hit or (rbody.get("ok") is False
                                                       and rcode == REJECTED_CODE)) else "error",
                    "code": rcode, "message": rmsg[:200],
                    "http": reject_probe.get("http"), "ms": reject_probe.get("ms"),
                    "shape_problems": [], "ok_field": rbody.get("ok"),
                    "status": rvalue.get("status"), "has_upstream_code": diag_ok,
                    "dedup_hit": dedup_hit,
                    "snippet": (reject_probe.get("raw") or "")[:240],
                    "expect": f"ok:false + {REJECTED_CODE} + message 含上游 errcode"})
    print(f"  {'trade_place(带外价 → 拒单契约)':34s} ok={rbody.get('ok')} code={rcode} "
          f"errcode可见={diag_ok} dedup={dedup_hit} {rmsg[:40]}")
    if dedup_hit:
        pass  # 已由「扫描前幽灵单」条登记；此处不重复也不误判
    elif old_style:
        findings.append(finding("important", "拒单仍返回 ok:true（契约未生效：缺陷 3）",
                                "trade_place", reject_probe.get("payload"), reject_probe,
                                f"业务拒单必须是 ok:false + {REJECTED_CODE}",
                                "页面/调用方会把拒单当成功。HEAD 已含修复（f86783f）——"
                                "**服务需重启加载**；重启后本条应消失"))
    elif rbody.get("ok") is False and rcode == REJECTED_CODE and not diag_ok:
        findings.append(finding("important", "拒单 message 缺上游 errcode（契约未生效：缺陷 2）",
                                "trade_place", reject_probe.get("payload"), reject_probe,
                                "message 应含 `[errcode=…]` 便于判断价格越界/权限/限频",
                                "只有笼统原因时无法定位。HEAD 已含修复（f86783f）——"
                                "**服务需重启加载**；重启后本条应消失"))
    elif rbody.get("ok") is False and rcode != REJECTED_CODE:
        findings.append(finding("important", f"拒单错误码不是 {REJECTED_CODE}（实际 {rcode}）",
                                "trade_place", reject_probe.get("payload"), reject_probe,
                                f"业务拒单应统一回 {REJECTED_CODE}",
                                "错误码族不一致会让调用方分支判断失效"))

    # ② 下单生命周期探针（S2/M1 验收证据）：place → cancel → 同标的同方向**可再次 place**
    #    → place → modify(改价) → 旧单 cancelled + 新单 submitted + OMS 行齐全。
    #
    # **前置门（重要）**：仅当「拒单契约」已生效（ok:false + order-rejected）才跑——
    # 旧代码的撤单**不回写 OMS**（S2），在旧服务上跑生命周期探针会**每跑一次留一笔幽灵单**，
    # 反而让后续运行越来越不可验证。契约未生效时直接跳过并指向「重启服务加载修复」。
    contract_ok = (rbody.get("ok") is False and rcode == REJECTED_CODE and not dedup_hit)
    blocked = in_flight_pairs()
    candidate = next(((sym, anchor) for sym, anchor in ORDER_CANDIDATES
                      if (sym, "BUY") not in blocked), None)
    if not contract_ok:
        results.append({"phase": "write", "endpoint": "下单生命周期探针",
                        "payload": None, "category": "ok", "code": None,
                        "message": "跳过：拒单契约未生效（服务运行旧代码）——"
                                   "在旧服务上跑会遗留幽灵单（S2）",
                        "http": None, "ms": None, "snippet": "",
                        "expect": "契约生效后验证 place/cancel/modify 生命周期与 OMS 回写"})
        print("  ⚠ 拒单契约未生效 → 跳过下单生命周期探针（避免制造幽灵单）")
    elif candidate is None:
        results.append({"phase": "write", "endpoint": "下单生命周期探针",
                        "payload": None, "category": "ok", "code": None,
                        "message": f"跳过：全部候选被在途单阻塞（blocked={sorted(blocked)}）",
                        "http": None, "ms": None, "snippet": "",
                        "expect": "无幽灵单时验证 place/cancel/modify 生命周期与 OMS 回写"})
        print("  ⚠ 全部候选标的被在途单阻塞 → 跳过下单生命周期探针")
    else:
        probe_symbol, anchor = candidate
        accepted, price = None, None
        for price_try in price_grid(anchor):
            attempt = client.call("trade_place", {"symbol": probe_symbol, "side": "BUY",
                                                  "qty": 100, "price": price_try})
            abody = attempt.get("body") or {}
            if abody.get("ok") is True and (abody.get("value") or {}).get("status") == "submitted":
                accepted, price = attempt, price_try
                break
        if accepted is None:
            item = finding("important", "下单链路不可判定：价格网格全部未提交",
                           "trade_place", {"symbol": probe_symbol, "anchor": anchor},
                           {"grid": price_grid(anchor)},
                           "网格内应至少一个价格落在当日价格带内并被接受",
                           f"锚定价 {anchor} 可能已漂移；更新 ORDER_CANDIDATES 后重跑。"
                           "**这不是平台缺陷**")
            item["category"] = "conditional"
            findings.append(item)
            record("trade_place(价网格全部未提交)", attempt, "至少一档应 submitted")
        else:
            value = (accepted.get("body") or {}).get("value") or {}
            first_id = value.get("broker_order_id") or value.get("order_id")
            record(f"trade_place({probe_symbol} @{price})", accepted,
                   f"ok:true + status=submitted（网格命中 {price}）")
            results.append({"phase": "write", "endpoint": "trade_place(首单) 结果",
                            "payload": None, "category": "ok", "code": None,
                            "message": f"status={value.get('status')} order_id={first_id} price={price}",
                            "http": accepted.get("http"), "ms": accepted.get("ms"),
                            "snippet": (accepted.get("raw") or "")[:240], "expect": ""})
            if not first_id:
                findings.append(finding("important", "下单成功但响应无券商订单号",
                                        "trade_place", accepted.get("payload"), accepted,
                                        "应返回 broker_order_id 以支持撤单/对账",
                                        "无法撤单=在途单可能残留"))
            # ② -1 撤单 → OMS 回写 → 同标的同方向可再次 place（S2 验收证据）
            if first_id:
                cancel = client.call("trade_cancel", {"order_id": str(first_id),
                                                      "symbol": probe_symbol})
                record("trade_cancel(撤首单)", cancel, "ok:true + status=cancelled")
                cleared = wait_for(lambda: (probe_symbol, "BUY") not in in_flight_pairs(), 25)
                results.append({"phase": "write", "endpoint": "撤单后 OMS 回写(S2)",
                                "payload": {"order_id": str(first_id)},
                                "category": "ok" if cleared else "error", "code": None,
                                "message": f"OMS 在途={in_flight_pairs() or '无'}",
                                "http": None, "ms": None, "snippet": "",
                                "expect": "撤单成功后该单不再是 OMS 在途（回写 cancelled）"})
                print(f"  {'撤单后 OMS 回写(S2)':34s} cleared={cleared}")
                if not cleared:
                    findings.append(finding(
                        "important", "撤单成功后 OMS 仍在途（S2 契约未生效）",
                        "trade_cancel", {"order_id": str(first_id)},
                        {"in_flight_pairs": sorted(in_flight_pairs())},
                        "撤单成功必须回写 OMS 终态，否则同标的同方向新单被在途查重永久阻塞",
                        "HEAD 已含修复（f86783f）；**服务需重启加载**，重启后本条应消失"))
                # ② -2 同标的同方向可再次 place（不再被在途查重阻塞）
                again = client.call("trade_place", {"symbol": probe_symbol, "side": "BUY",
                                                    "qty": 100, "price": price})
                abody2 = again.get("body") or {}
                again_ok = abody2.get("ok") is True and \
                    (abody2.get("value") or {}).get("status") == "submitted"
                record("trade_place(撤单后再下同标的同方向)", again,
                       "ok:true + submitted（不再被在途查重阻塞）")
                if not again_ok:
                    findings.append(finding(
                        "important", "撤单后同标的同方向仍被阻塞（S2 契约未生效）",
                        "trade_place", {"symbol": probe_symbol, "side": "BUY"}, again,
                        "撤单成功后应可再次下单（在途查重不应看到已撤单）",
                        "HEAD 已含修复（f86783f）；**服务需重启加载**，重启后本条应消失"))
                second_id = ((abody2.get("value") or {}).get("broker_order_id")
                             or (abody2.get("value") or {}).get("order_id"))
                # ② -3 place → modify(改价) → 旧单 cancelled + 新单 submitted + OMS 行齐全
                if again_ok and second_id:
                    new_price = round(price * 1.01, 2)
                    mod_out = client.call("trade_modify", {"order_id": str(second_id),
                                                           "symbol": probe_symbol,
                                                           "side": "BUY",
                                                           "qty": 100, "price": new_price})
                    record(f"trade_modify(改价 →{new_price})", mod_out,
                           "ok:true + submitted（改单=撤旧重下）")
                    ok_pairs = in_flight_pairs()
                    results.append({"phase": "write", "endpoint": "改单后 OMS 行齐全(M1)",
                                    "payload": {"order_id": str(second_id)},
                                    "category": "ok" if (probe_symbol, "BUY") in ok_pairs
                                    else "error", "code": None,
                                    "message": f"OMS 在途={sorted(ok_pairs)}",
                                    "http": None, "ms": None, "snippet": "",
                                    "expect": "改单后应有且仅有新单在途（旧单落 cancelled）"})
                    print(f"  {'改单后 OMS 在途':34s} {sorted(ok_pairs)}")
                    # 收尾：撤掉改单后的新单（**改单会换券商订单号**，要用 modify 返回的新 id）
                    mod_value = (mod_out.get("body") or {}).get("value") or {}
                    new_id = (mod_value.get("broker_order_id") or mod_value.get("order_id")
                              or second_id)
                    cleanup = client.call("trade_cancel", {"order_id": str(new_id),
                                                          "symbol": probe_symbol})
                    cleanup_ok = ((cleanup.get("body") or {}).get("ok") is True)
                    record("trade_cancel(收尾撤单)", cleanup, "ok:true + status=cancelled")
                    wait_for(lambda: (probe_symbol, "BUY") not in in_flight_pairs(), 25)
                    results.append({"phase": "write", "endpoint": "收尾撤单已发出",
                                    "payload": {"order_id": str(new_id)},
                                    "category": "ok", "code": None,
                                    "message": f"cleanup_ok={cleanup_ok}",
                                    "http": cleanup.get("http"), "ms": cleanup.get("ms"),
                                    "snippet": "", "expect": "收尾撤单已发出"})

    time.sleep(2)
    in_flight = in_flight_orders(db_counts())
    # 与**扫描前**对比：扫描前就有的幽灵单不算本次泄漏（已单独登记），
    # 只有**本次新增**的在途状态才是 critical 泄漏。
    new_in_flight = {k: v for k, v in in_flight.items()
                     if pre_inflight.get(k, 0) < v}
    results.append({"phase": "write", "endpoint": "在途单核对", "payload": None,
                    "category": "ok" if not new_in_flight else "error", "code": None,
                    "message": f"in-flight={in_flight or '无'}"
                               f"（扫描前 {pre_inflight or '无'}；新增 {new_in_flight or '无'}）",
                    "http": None, "ms": None, "snippet": "",
                    "expect": "本次扫描不得新增在途单"})
    if new_in_flight:
        # 若收尾撤单已**成功发出**（券商侧 ok）而 OMS 仍在途 → 根因是平台的撤单回写缺口
        # （S2），不是 harness 没收尾；按条件性登记并指向 S2，避免把它读成「harness 有 bug」。
        # 「已尝试收尾」= 本次扫描里任何一次撤单成功（首单撤单同样算）——用于把
        # 「OMS 未回写导致的在途残留」归因到平台 S2 缺口，而不是读成 harness 没收尾。
        cleanup_sent = any(r.get("phase") == "write"
                           and str(r.get("endpoint", "")).startswith("trade_cancel(")
                           and r.get("category") == "ok" for r in results)
        item = finding(
            "important" if cleanup_sent else "critical",
            "扫描后新增在途单" + ("（收尾撤单已发出，OMS 未回写 → S2）" if cleanup_sent else ""),
            "trade_place/trade_cancel", None,
            {"before": pre_inflight, "after": in_flight, "new": new_in_flight},
            "扫描结束不得新增在途单（测试单应已撤销且 OMS 回写终态）",
            "状态泄漏：真实资金路径上会留下意外在途单。" + (
                "本次已发出收尾撤单——OMS 未回写属 S2 缺口（HEAD 已修 f86783f，"
                "**服务需重启加载**）" if cleanup_sent else "收尾撤单未发出，需排查 harness"))
        if cleanup_sent:
            item["category"] = "conditional"
        findings.append(item)

    # --- 2.6 拒绝路径：confirm-decide / rules-decide / research-tasks-*
    record("confirm-decide(伪造 id)",
           client.call("confirm-decide", {"id": "nope", "decision": "approved"}),
           "拒绝（无待确认或编号不存在）", expected_code="trading/invalid-operation")
    before_rules = db_counts().get("rules", 0)
    record("rules-decide(不存在 rule_id)",
           client.call("rules-decide", {"rule_id": "no-such-rule", "decision": "enable"}),
           "拒绝且库零变化", expected_code="trading/invalid-operation")
    if db_counts().get("rules", 0) != before_rules:
        findings.append(finding("critical", "rules-decide 对不存在规则改动了库", "rules-decide",
                                {"rule_id": "no-such-rule"}, results[-1],
                                "必须拒绝且零变化", "审批通道可能被滥用"))
    q = client.call("research-tasks-list", {})
    tasks = ((q.get("body") or {}).get("value") or {}).get("tasks") or []
    if tasks:
        # **信息类观察项：不计缺陷、不计阻断**（2026-09-17 分诊）。
        # 队列非空的正当来源是服务 tick-first 补跑当日 GLOBAL 链的 `enqueue_research`
        # （关注池已配置 → 正常入队）；claim 语义测试跳过，因为 harness 是**只读验收工具**，
        # 领取会真的把任务置 running（吞掉人/定时器该消费的工作）。
        record("research-tasks-list(队列状态)", q,
               f"队列 {len(tasks)} 条（信息类：不判缺陷；claim 测试跳过以免吞任务）")
        kinds = sorted({str(t.get("kind")) for t in tasks if isinstance(t, dict)})
        print(f"  ℹ 队列积压 {len(tasks)} 条（kind={kinds}）：建议按 AGENTS.md 值班纪律"
              "消费或等待 install/research-duty.timer；harness 不代替消费")
    else:
        claim = client.call("research-tasks-claim", {})
        body = claim.get("body") or {}
        value = body.get("value") or {}
        got = value.get("task")
        record("research-tasks-claim(空队列)", claim, "{task: null}")
        if got is not None:
            findings.append(finding("important", "空队列 claim 仍返回了任务？",
                                    "research-tasks-claim", {}, claim,
                                    "空队列应返回 task=null",
                                    "claim 语义与 list 不一致（或竞态）"))
    record("research-tasks-report(伪造 task_id)",
           client.call("research-tasks-report", {"task_id": "nope", "ok": True}),
           "拒绝（任务不存在）", expected_code="trading/invalid-operation")

    # --- 2.7 push 订阅往返
    sub = client.call("push_subscribe", {"quote": [SYM_HK]})
    record("push_subscribe(quote HK.00700)", sub, "成功（或 push-unavailable 如实降级）")
    unsub = client.call("push_unsubscribe", {"quote": [SYM_HK]})
    record("push_unsubscribe(quote HK.00700)", unsub, "移除订阅意图")

    # --- 2.8 自选修改：**只用非法 op 探拒绝路径**（绝不动用户真实自选）
    # 期望 `invalid-operation`（调用方参数错）**且零上游调用**：`op` 的取值集合来自真机
    # -3 原文（`allowed: [ADD, DEL, MOVE_OUT]`），服务/传输层据此本地前置拒绝。
    # 「零上游」的可观测判据：错误码必须是本地参数码，且 `details.errcode` **不存在**
    # （信封带富途业务码 ⇒ 调用真的发出去了——2026-09-17 前正是这种状态）。
    #
    # 分类口径：命中上游业务错（且 detail 带 errcode）时**计条件性**而非阻断——因为观察到
    # 的两种成因在外观上相同：① 服务进程仍是改动前的代码（进程内模块缓存，改文件不生效，
    # 需重启）；② 本地守卫被回归。判据留给 `tests/test_wp12_surface.py` 的
    # ``DataPlaneLocalRejectionTests``（注入真方法组 + 记录客户端，直接在代码层断言
    # 零上游调用）——它才是这条不变式的阻断级守卫；harness 负责指出「跑着的服务没这行为」。
    mod = client.call("modify_user_security", {"op": "NOT_A_REAL_OP", "code_list": [SYM_HK]})
    mod_err = (mod.get("body") or {}).get("error") or {}
    mod_code = mod_err.get("code")
    mod_message = str(mod_err.get("message") or "")
    mod_upstream = (mod_err.get("details") or {}).get("errcode")
    record("modify_user_security(非法 op，仅探拒绝)", mod,
           "invalid-operation（本地白名单前置拒绝，零上游调用）",
           expected_code="trading/invalid-operation")
    if mod_code != "trading/invalid-operation" or mod_upstream is not None:
        item = finding(
            "important",
            f"非法 op 未被本地拦住（code={mod_code} upstream_errcode={mod_upstream}）",
            "modify_user_security", {"op": "NOT_A_REAL_OP"}, mod,
            "非法 op 应回 trading/invalid-operation 且**零上游调用**"
            "（判据：无 details.errcode）",
            "参数拼错会被误导成上游业务错，且白白消耗一次上游调用额度；"
            "**若刚修好本地白名单，本条多为服务进程仍是旧代码**——进程内模块缓存，"
            "改文件不生效，重启服务后复跑应消失（见 docs/RUNBOOK.md「服务启停」）；"
            "代码层阻断级守卫见 tests/test_wp12_surface.py::DataPlaneLocalRejectionTests")
        item["category"] = "conditional"
        findings.append(item)
        print("  ⚠ 非法 op 打到了上游：本地白名单未在**运行中的服务**里生效"
              "（重启服务加载新代码后复跑本条应消失）")

    # --- 2.9 期权筛选的**欠指定**载荷：期望本地前置拒绝（proto 占位规则：
    # 空数组/空对象/0 会被上游 -3，服务端据此前置拦截）。这是「期望被拒」探针——
    # 正确拒绝即 ok、不计阻断；若它真的打到上游（带 errcode 的非参数码），记条件性发现
    # （阻断判据留给 tests/test_wp12_surface.py 的本地守卫断言）。
    bad_screen = client.call("option_screen", OPTION_SCREEN_UNDERSPECIFIED)
    bad_err = (bad_screen.get("body") or {}).get("error") or {}
    bad_code = bad_err.get("code")
    bad_upstream = (bad_err.get("details") or {}).get("errcode")
    record("option_screen(欠指定 field_filter，仅探拒绝)", bad_screen,
           "invalid-operation（proto 占位规则前置拒绝，零上游调用）",
           expected_code="trading/invalid-operation")
    if bad_code != "trading/invalid-operation" or bad_upstream is not None:
        item = finding(
            "important",
            f"欠指定 field_filter 未本地拒绝（code={bad_code} upstream_errcode={bad_upstream}）",
            "option_screen", OPTION_SCREEN_UNDERSPECIFIED, bad_screen,
            "空数组/空对象占位应回 trading/invalid-operation 且零上游调用",
            "缺形状守卫 ⇒ 调用方对着上游 -3 猜原因（本地守卫断言见 test_wp12_surface.py）")
        item["category"] = "conditional"
        findings.append(item)


# --------------------------------------------------------------------------- 阶段 3
def consistency(client, declared, results, findings, read_map):
    print("\n=== 阶段 3：一致性交叉核对 ===")
    # 3.1 声明端点 vs 实际可达（除已知写/动作端点用拒绝路径探测）
    unknown = []
    for endpoint in declared:
        payload, method = read_map.get(endpoint, ({}, "POST"))
        result = client.call(endpoint, payload, method=method)
        code = ((result.get("body") or {}).get("error") or {}).get("code")
        if result.get("http") == 404 or code == "trading/unknown-endpoint":
            unknown.append(endpoint)
    results.append({"phase": "consistency", "endpoint": "声明端点可达性", "payload": None,
                    "category": "ok" if not unknown else "error", "code": None,
                    "message": f"声明 {len(declared)} 项，unknown-endpoint {len(unknown)} 项：{unknown}",
                    "http": None, "ms": None, "snippet": "", "expect": "全部可达"})
    print(f"  声明 {len(declared)} 项 → unknown-endpoint: {unknown or '无'}")
    if unknown:
        findings.append(finding("important", "snapshot 声明的端点实际不可达", "snapshot",
                                None, {"unknown": unknown},
                                "声明端点集合应与实际路由一致",
                                "前端会据此判断版本，声明与实现不一致会误导排查"))

    # 3.2 pipeline 阶段 vs kv ran 标记与当日告警
    pipe = client.call("pipeline", {})
    value = (pipe.get("body") or {}).get("value") or {}
    state_raw = (db_counts().get("__kv_all__") or {}).get("daemon:state")
    ran = {}
    if state_raw:
        try:
            ran = (json.loads(state_raw) or {}).get("ran", {})
        except ValueError:
            ran = {}
    today = date.today().isoformat()
    mismatch = []
    # 阶段名 → 作业名并非一一对应：``plan``/``execute`` 是**表驱动派生阶段**
    # （pipeline._market_stages 在 build_plan/auto_execute 之后插入，分别由 plans/orders
    # 表事实得出）。它们没有同名作业，也就不该期待 ``SH:plan:<date>`` 这类 ran 标记——
    # 拿它们去比对 ran 标记，会在「有人手工跑 plan-auto 生成了计划」时误报「没跑却显示完成」
    # （2026-09-17 实机：手工 plan-auto 后本检查报 1 项阻断，实为检查口径错）。
    # 派生阶段的诚实性由 _plan_stage/_execute_stage 的事实来源保证，这里只核对作业阶段。
    DERIVED_STAGES = {"plan", "execute"}
    for market, info in (value.get("markets") or {}).items():
        for stage, detail in (info.get("stages") or {}).items():
            if stage in DERIVED_STAGES:
                continue
            key = f"{market}:{stage}:{today}"
            has_mark = key in ran
            status = detail.get("status")
            summary = str(detail.get("summary") or "")
            # 修后语义（HEAD 2b823f0）：作业**跑过但什么都没做**时阶段显示 skipped/failed +
            # 摘要写明原因（如「当日未采集：关注池为空」）——这是**诚实**的，不是不一致。
            # 因此只在「跑过却显示待运行」或「跳过/失败却没有原因摘要」或「没跑却显示完成」时判不一致。
            explanatory = any(tok in summary for tok in
                              ("未采集", "跳过", "为空", "未生成", "尚未", "无关注池"))
            if has_mark and status == "pending":
                mismatch.append({"market": market, "stage": stage, "pipeline": status,
                                 "ran_mark": ran[key], "summary": summary})
            elif has_mark and status in ("failed", "skipped") and not explanatory:
                mismatch.append({"market": market, "stage": stage, "pipeline": status,
                                 "ran_mark": ran[key], "summary": summary,
                                 "why": "跳过/失败但没有原因摘要"})
            elif not has_mark and status == "ok":
                mismatch.append({"market": market, "stage": stage, "pipeline": status,
                                 "ran_mark": None, "summary": summary,
                                 "why": "没有 ran 标记却显示完成"})
    results.append({"phase": "consistency", "endpoint": "pipeline vs kv ran", "payload": None,
                    "category": "ok" if not mismatch else "important", "code": None,
                    "message": f"不一致 {len(mismatch)} 项", "http": pipe.get("http"),
                    "ms": pipe.get("ms"), "snippet": json.dumps(mismatch, ensure_ascii=False)[:300],
                    "expect": "作业阶段状态与实际 ran 标记一致（plan/execute 为表驱动派生阶段，不在此列）"})
    print(f"  pipeline vs kv ran 标记不一致: {len(mismatch)} 项 {mismatch[:3]}")
    if mismatch:
        findings.append(finding("important", "流程页阶段状态与 kv ran 标记不一致", "pipeline",
                                {}, {"mismatch": mismatch[:5]},
                                "阶段状态必须来自事实（ran 标记 + 告警）",
                                "状态与事实不一致会让流程页说谎"))

    # 3.3 「关注池为空」时基础链作业的状态（当前 trading-platform.json 无 watchlist 键）
    config = {}
    try:
        config = json.loads(PLATFORM_JSON.read_text()) if PLATFORM_JSON.exists() else {}
    except ValueError:
        config = {}
    no_watchlist = "watchlist" not in config
    pool_alerts = []
    if DB.exists():
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            pool_alerts = [dict(zip(("level", "title", "detail", "created_at"), row))
                           for row in conn.execute(
                               "SELECT level, title, detail, created_at FROM alerts "
                               "WHERE detail LIKE '%关注池%' OR title LIKE '%关注池%' "
                               "ORDER BY id DESC LIMIT 5")]
        finally:
            conn.close()
    stages = {f"{m}.{s}": d.get("status")
              for m, info in (value.get("markets") or {}).items()
              for s, d in (info.get("stages") or {}).items()
              if s in ("sync_bars", "factors_snapshot", "sentiment_snapshot", "research_snapshot")}
    results.append({"phase": "consistency", "endpoint": "空关注池时的阶段状态", "payload": None,
                    "category": "important" if (no_watchlist and pool_alerts) else "ok",
                    "code": None,
                    "message": f"无 watchlist 键={no_watchlist}; 关注池相关告警 {len(pool_alerts)} 条; "
                               f"阶段={stages}",
                    "http": None, "ms": None,
                    "snippet": json.dumps(pool_alerts, ensure_ascii=False)[:300],
                    "expect": "作业因关注池为空跳过时，流程页应能看出「跳过/无数据」而非显示成完成"})
    print(f"  空关注池：无 watchlist 键={no_watchlist}；相关告警 {len(pool_alerts)} 条")
    print(f"  基础链阶段状态：{stages}")
    # 修后语义（HEAD 2b823f0）：关注池为空时阶段显示 skipped + 原因摘要 → **不再是不一致**。
    # 只有仍显示 ok（把「什么都没做」报成完成）才算问题。
    # 只看**依赖关注池**的阶段：sync_bars/factors_snapshot 本来就会 ok（它们不等关注池），
    # 用全量 stages 判会恒真 → 假发现（2026-09-17 修）。
    pool_stages = {k: v for k, v in stages.items()
                   if k.endswith((".sentiment_snapshot", ".research_snapshot"))}
    claims_ok = any(v == "ok" for v in pool_stages.values())
    if no_watchlist and pool_alerts and claims_ok:
        # **条件性发现（不阻断退出码）**：本环境未配置 `watchlist` 键，基础链作业按
        # 「关注池为空」软跳过——此时阶段显示 ok 描述的是「作业跑过了」，而采集内容为零。
        # 判为条件性而非阻断：① 触发前提是运维未配置关注池（配置后作业会真正采集）；
        # ② 阶段状态与内容结局的分辨属已知在修项（流程页 loading/空态诚实性）。
        # 闭环判据：配置 watchlist 后此条消失；仍出现即升级为阻断。
        item = finding(
            "important",
            "配置无 watchlist 键：基础链作业按「关注池为空」跳过，但流程页阶段仍显示 ok",
            "pipeline", {}, {"alerts": pool_alerts[:2], "stages": stages},
            "作业跳过（未真正采集）时，阶段状态应反映「跳过/无数据」或摘要写明原因",
            "依赖基础链积累的数据（情绪/F10 PIT）实际没有积累，而面板显示一切正常——"
            "研究侧会以为数据在攒，属于静默失效/数据不诚实风险。"
            "**条件性**：仅在未配置 watchlist 时成立；闭环判据＝配置关注池后本条消失")
        item["category"] = "conditional"
        findings.append(item)

    # 3.4 plan/schedule/reconcile/audit 与库表
    plan = client.call("plan", {})
    reconcile = client.call("reconcile", {})
    audit = client.call("audit", {})
    schedule = client.call("schedule", {})
    counts = db_counts()
    results.append({"phase": "consistency", "endpoint": "plan/schedule/reconcile/audit",
                    "payload": None, "category": "ok", "code": None,
                    "message": f"plans={counts.get('plans')} orders={counts.get('orders')} "
                               f"alerts={counts.get('alerts')} risk_checks={counts.get('risk_checks')}",
                    "http": None, "ms": None,
                    "snippet": json.dumps({"plan_ok": (plan.get("body") or {}).get("ok"),
                                           "schedule_ok": (schedule.get("body") or {}).get("ok"),
                                           "reconcile_ok": (reconcile.get("body") or {}).get("ok"),
                                           "audit_ok": (audit.get("body") or {}).get("ok")},
                                          ensure_ascii=False),
                    "expect": "四个只读端点均成功且与库表一致"})
    for name, res in (("plan", plan), ("schedule", schedule), ("reconcile", reconcile),
                      ("audit", audit)):
        if not (res.get("body") or {}).get("ok"):
            findings.append(finding("important", f"{name} 端点未成功", name, {}, res,
                                    "只读聚合端点应成功（允许空数据）",
                                    "核心面板数据不可用"))


def finding(severity, title, endpoint, payload, result, expected, impact):
    body = result.get("body") if isinstance(result, dict) else None
    return {"severity": severity, "title": title, "endpoint": endpoint, "payload": payload,
            "http": (result or {}).get("http"),
            "response": json.dumps(body, ensure_ascii=False)[:400] if body else
                        ((result or {}).get("raw") or "")[:400],
            "expected": expected, "impact": impact}


# --------------------------------------------------------------------------- 主流程
OPTION_CONTRACT = [OPTION_SYMBOL]


def discover(client):
    """现场发现：有效期权合约、板块代码（让载荷尽量真实而不硬编码）。"""
    chain = client.call("option_chain", {"code": SYM_HK})
    contracts = ((chain.get("body") or {}).get("value") or {}).get("option_chain") or []
    if contracts and isinstance(contracts[0], dict) and contracts[0].get("code"):
        OPTION_CONTRACT[0] = contracts[0]["code"]
    plates = client.call("plate_list", {"market": "HK", "plate_class": "INDUSTRY"})
    plist = ((plates.get("body") or {}).get("value") or {}).get("plate_list") or []
    plate_code = plist[0].get("code") if plist and isinstance(plist[0], dict) else None
    return {"option": OPTION_CONTRACT[0], "plate_code": plate_code,
            "option_chain_ok": bool(contracts), "plate_list_ok": bool(plist)}


def read_probes(discovered):
    t = date.today()
    start = (t - timedelta(days=30)).isoformat()
    end = t.isoformat()
    prev = (t - timedelta(days=1)).isoformat()
    plate = discovered.get("plate_code") or "HK.LIST23618"
    return [
        # 只读聚合 / 空载荷
        ("snapshot", {}, "只读聚合"),
        ("audit", {}, "只读聚合"),
        ("plan", {}, "只读聚合"),
        ("schedule", {}, "只读聚合"),
        ("reconcile", {}, "只读聚合"),
        ("pipeline", {}, "只读聚合"),
        ("confirmation", {}, "待确认在途（空）"),
        ("auto_pipeline", {}, "读设置"),
        ("rules", {}, "读规则候选池"),
        ("research-tasks-list", {}, "读队列"),
        ("push_status", {}, "读推送状态"),
        # 分析层（compute 子进程）
        ("equity", {}, "账户权益（sim）"),
        ("positions", {}, "券商持仓（sim）"),
        ("risk", {}, "风控视图"),
        ("trades", {}, "交易摘要"),
        ("sources", {}, "数据源状态"),
        # 输入必填的分析端点：happy path 用**合法最小 tickers**（证明能力可用）；
        # 空载荷的契约断言另见阶段 1c（scan_contract_params）。
        ("correlation", {"tickers": [SYM_HK, SYM_US]}, "相关性（tickers 2..8）"),
        ("factors", {"tickers": [SYM_HK, SYM_US]}, "因子截面（tickers 2..8）"),
        ("ic", {"tickers": [SYM_HK, SYM_US, SYM_A]}, "IC 检验（tickers 3..8）"),
        ("factors-history", {}, "因子快照历史"),
        ("sentiment-history", {}, "情绪快照历史"),
        ("sensitivity", {"ticker": SYM_A}, "参数敏感性（需 ticker）"),
        ("events", {"ticker": SYM_A}, "事件（需 ticker）"),
        ("instrument", {"ticker": SYM_A}, "标的解析（需 ticker）"),
        ("quality", {"ticker": SYM_A}, "数据质量（需 ticker）"),
        ("series", {"ticker": SYM_A, "period": "1d", "limit": 20}, "K 线序列"),
        # 富途行情/数据面
        ("rt_quote", {"codes": [SYM_HK]}, "实时报价（HK）"),
        ("rt_order_book", {"code": SYM_HK}, "买卖盘"),
        ("capital_flow", {"code": SYM_HK}, "资金流向"),
        ("capital_flow_history", {"code": SYM_HK, "days": 5}, "资金流历史"),
        ("capital_distribution", {"code": SYM_HK}, "资金分布"),
        ("option_expiration", {"code": SYM_HK}, "期权到期日"),
        ("option_chain", {"code": SYM_HK}, "期权链"),
        # option_screen 的 filter 必须含**非空 field_filter + 非空 strategy**（TOOL-LIMITS 实测
        # 陷阱：省略 field_filter 时上游只回 4 个默认字段、其余全 null，会被误判成数据缺失）
        ("option_screen", OPTION_SCREEN_PROBE, "期权筛选（field_filter 必填，真机最小示例）"),
        ("market_snapshot", {"codes": [SYM_HK]}, "市场快照"),
        ("cur_kline", {"code": SYM_HK, "num": 10}, "当前 K 线"),
        ("rt_data", {"code": SYM_HK}, "分时数据"),
        ("rt_ticker", {"code": SYM_HK, "num": 10}, "逐笔成交"),
        ("info_basicinfo", {"codes": [SYM_HK]}, "标的基础信息"),
        ("info_trading_days", {"market": "HK", "start": start, "end": end}, "交易日历"),
        ("info_search", {"keyword": "腾讯"}, "资讯搜索"),
        ("info_market_state", {"codes": [SYM_HK]}, "市场状态"),
        ("quote_history_kline_v2", {"code": SYM_HK, "end": prev, "num": 10}, "历史 K 线"),
        # 交易只读（sim）
        ("trade_max_qty", {"code": SYM_HK, "order_type": "LIMIT", "price": 600}, "最大可交易量"),
        ("orders_open", {"market": "HK"}, "未完成订单"),
        ("orders_history", {"market": "HK", "start": start, "end": end}, "历史订单"),
        ("orders_detail", {"exchange": "HK", "order_ids": ["1"]}, "订单详情"),
        ("deals_today", {"market": "HK"}, "当日成交"),
        ("deals_history", {"market": "HK", "start": start, "end": end}, "历史成交"),
        ("account_positions", {}, "账户持仓"),
        ("account_orders", {}, "账户订单"),
        ("account_funds", {}, "账户资金"),
        # 数据面（WP12）
        ("economic_calendar_hot", {}, "经济日历热门"),
        ("economic_calendar_search", {"keyword": "CPI", "search_type": 1}, "经济日历搜索"),
        ("info_owner_plate", {"code": SYM_HK}, "所属板块"),
        ("info_rehab", {"code": SYM_HK}, "复权因子"),
        ("plate_list", {"market": "HK", "plate_class": "INDUSTRY"}, "板块列表"),
        ("plate_stock", {"plate_code": plate}, "板块成份股"),
        ("stock_screen", {"screen_queries": [{"simple_field_query": {
            "simple_field": 1, "screen_value_list": [1]}}], "limit": 1}, "股票筛选"),
        ("warrant_screen", {"market_type": 1, "limit": 1}, "窝轮筛选（HTTP-only）"),
        ("ipo_list", {"market": "hk"}, "IPO 列表"),
        ("short_daily_volume", {"code": SYM_HK, "count": 2}, "每日做空量"),
        ("short_interest", {"code": SYM_HK, "count": 2}, "空头持仓"),
        ("watchlist_list", {"group_name": "全部"}, "自选股列表（需 OAuth 身份）"),
        ("watchlist_groups", {"group_type": "ALL"}, "自选股分组（需 OAuth 身份）"),
        ("f10_detail", {"code": SYM_US, "section": "analyst_consensus"}, "F10 聚合"),
        ("derivative_detail", {"code": OPTION_CONTRACT[0], "section": "option_volatility"},
         "衍生品聚合"),
        # 设置页读
        ("openapi_config", None, "凭据状态（GET）"),
        ("openapi_oauth", {"action": "status"}, "OAuth 流程状态"),
        ("openapi_test", {}, "连通性自检（真实网络）"),
    ]


def main():
    parser = argparse.ArgumentParser(description="工作台后端端点全量 E2E 扫描（只报告）")
    parser.add_argument("--base", default="http://127.0.0.1:8397")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out", default=str(LOGS))
    parser.add_argument("--read-only", action="store_true",
                        help="跳过阶段 2 的全部写/动作检查（并行任务占用同一服务或 CI 只读巡检时用）")
    args = parser.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)
    client = Client(args.base, args.timeout)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    results, findings = [], []

    print("=" * 78)
    print("工作台后端端点全量 E2E 扫描（只报告，不修复）")
    print(f"  服务：{args.base}    时间：{ts}    模式："
          f"{'只读（跳过写检查）' if args.read_only else '读写可逆检查'}")
    print("=" * 78)

    health = client.health()
    if not (health.get("body") or {}).get("ok"):
        print(f"❌ 服务不可达：{health.get('transport_error') or health.get('raw')}")
        return 2

    print("\n=== 阶段 0：状态快照 ===")
    base = baseline(client)
    print(f"  mode={base['health'].get('mode')}  kill={base['files']['kill_file']}  "
          f"pending={base['files']['pending_count']}  config_md5={base['files']['config_md5'][:8]}")
    orders0 = base["db"].get("__orders_by_status__", {})
    print(f"  orders={orders0 or '空'}  plans={base['db'].get('plans', 0)}  "
          f"rules={base['db'].get('rules', 0)}  research_tasks={base['db'].get('research_tasks', 0)}")
    if base["health"].get("mode") != "sim":
        print("❌ 当前不是 sim 模式：本脚本只允许对模拟盘运行（绝不切 live）")
        return 2

    declared = (client.call("snapshot", {}).get("body") or {}).get("value", {}).get("endpoints", [])
    probes = read_probes(discover(client))
    read_map = {endpoint: (payload, ("GET" if endpoint == "openapi_config" else "POST"))
                for endpoint, payload, _ in probes}
    # openapi_config 的 GET 用法与 POST 读等价；declared 里它是 GET 专用
    read_map["openapi_config"] = (None, "GET")

    scan_reads(client, probes, results, findings)
    scan_sections(client, results, findings)
    scan_contract_params(client, results, findings)
    if args.read_only:
        print("\n=== 阶段 2：已跳过（--read-only）===")
    else:
        scan_writes(client, results, findings, base)
    consistency(client, declared, results, findings, read_map)

    print("\n=== 阶段 4：状态复原核对（三类判定）===")
    after = {"db": db_counts(), "files": file_state()}
    changes = state_diff(base, after)
    service_changes = [c for c in changes if c["kind"] == "service"]
    audit_changes = [c for c in changes if c["kind"] in ("audit", "record")]
    leaks = [c for c in changes if c["kind"] == "leak"]

    print(f"  ① 服务自身活动 {len(service_changes)} 条（忽略）")
    for item in service_changes[:8]:
        print(f"      · {item['key']}: {str(item['before'])[:40]} → {str(item['after'])[:40]}")
    if len(service_changes) > 8:
        print(f"      · …（另 {len(service_changes) - 8} 条）")
    print(f"  ② 审计/记录行 {len(audit_changes)} 条（允许，列出）")
    for item in audit_changes:
        print(f"      · {item['key']}: {item['before']} → {item['after']}")
    print(f"  ③ 真泄漏 {len(leaks)} 条")
    for item in leaks:
        print(f"      ✗ {item['key']}: {item['before']} → {item['after']}")

    for leak in leaks:
        findings.append(finding("critical", f"扫描留下未复原的状态：{leak['key']}",
                                None, None, {"before": leak["before"], "after": leak["after"],
                                             "class": leak["why"]},
                                "扫描结束除服务活动与审计行外不应有状态变更",
                                "状态泄漏会影响后续判断（配置被改写/指令残留/在途单残留）"))

    # 「可疑项」与「过渡码」只登记，不作退出码依据：前者可能合法（如该标的当日无数据），
    # 后者是本轮并行修复进行中的预期现象（修复落地后应消失）。
    suspicious = [f for f in findings if f.get("category") == "suspicious"]
    transitional = [f for f in findings if f.get("category") == "transition"]
    blocking = [f for f in findings
                if f["severity"] in ("critical", "error")
                or (f["severity"] == "important" and f.get("category") not in
                    ("suspicious", "transition", "conditional"))]
    conditional = [f for f in findings if f.get("category") == "conditional"]
    # 陈旧服务信号：契约探针若仍观察**旧语义**，说明服务运行的是旧代码（HEAD 修复未加载）。
    # 这比逐条 finding 更好用：运维直接看到「重启服务后复跑」。
    stale_contract = (
        any(r.get("phase") == "write" and r.get("endpoint") == "trade_place(带外价 → 拒单契约)"
            and r.get("ok_field") is True for r in results)
        or any(r.get("phase") == "contract" and r.get("code") == "trading/analytics-unavailable"
               for r in results))
    if stale_contract:
        print("\n⚠ 契约未生效：服务可能运行旧代码（拒单仍 ok:true / analytics 仍 analytics-unavailable）")
        print("  → 请重启服务加载 HEAD 修复后复跑（见 docs/RUNBOOK.md「服务启停」）")

    def tally(phase_names):
        """按阶段统计 ok/degraded/error（**阶段感知**：首版的 read_* 把写阶段也算进去，
        于是「按设计拒绝」的写探针被计成读类 error，汇总数字不可信）。"""
        subset = [r for r in results if r["phase"] in phase_names]
        return {"total": len(subset),
                "ok": sum(1 for r in subset if r["category"] == "ok"),
                "degraded": sum(1 for r in subset if r["category"] == "degraded"),
                "error": sum(1 for r in subset if r["category"] == "error")}

    phases = {
        "read": tally({"read"}),
        "section": tally({"f10_section", "derivative_section"}),
        "contract": tally({"contract"}),
        "write": tally({"write"}),
        "consistency": tally({"consistency"}),
    }
    summary = {
        "ts": ts, "base": args.base, "mode": base["health"].get("mode"),
        "read_only": args.read_only,
        "declared_endpoints": len(declared),
        "phases": phases,
        "findings": findings, "changes": changes,
        "service_changes": service_changes, "audit_changes": audit_changes, "leaks": leaks,
        "suspicious": suspicious, "transitional": transitional, "blocking": blocking,
        "conditional": conditional,
        "stale_service_contract": stale_contract,
        "results": results, "baseline": {k: v for k, v in base.items() if k != "db"},
        "baseline_db_keys": len(base["db"]),
    }
    out_path = pathlib.Path(args.out) / f"e2e-workbench-{ts}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总 ===")
    for name, t in phases.items():
        print(f"  {name:12s} 共 {t['total']:3d}：ok={t['ok']} degraded={t['degraded']} error={t['error']}")
    by_sev = {}
    for item in findings:
        by_sev[item["severity"]] = by_sev.get(item["severity"], 0) + 1
    print(f"  发现：{by_sev or '无'}"
          f"（其中可疑项 {len(suspicious)}、过渡码 {len(transitional)}、"
          f"条件性 {len(conditional)}、阻断 {len(blocking)}）")
    print(f"  状态：服务活动 {len(service_changes)}｜审计行 {len(audit_changes)}｜真泄漏 {len(leaks)}")
    print(f"  JSON：{out_path}")
    blocking_errors = (phases["read"]["error"] + phases["section"]["error"]
                       + phases["contract"]["error"])
    fatal = bool(blocking_errors or leaks or blocking)
    print(f"  退出码：{1 if fatal else 0}"
          f"（读+section+contract error={blocking_errors} leaks={len(leaks)} "
          f"blocking={len(blocking)}）")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
