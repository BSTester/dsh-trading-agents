"""V3 研究报告接口（**只读**）。

职责边界（与仓库既有纪律一致）：
  * **生成**研报走 Harness 对话：`skills/trading-agents`（12 角色 / 6 阶段）→
    `research_publish(run_id, ticker, rating, report, sources)` 落工作台 store；
    值勤队列（daily_brief / factor_patrol / mining_round）由 install/research-duty.timer
    驱动 `dsh --profile headless` 消费（claim/report 只进 Harness 工具面）。
  * **展示**研报是工作台的事：本模块把工作台快照里的 runs / reports / previews / activity
    与值勤队列读出来给页面，**不提供生成、不提供下单、不启用策略**。

数据来源：
  * ``snapshot``（HTTP 端点，含 reports 的 markdown 正文与 sources）——经注入的 handle 直读；
  * ``research-tasks-list``（**HTTP-only** 端点：队列清单只给人看，不进 Harness 工具面）。

市场口径（2026-09-20）：``?market=SH|HK|US``（缺省**不过滤**，与历史一致）——
``runs`` / ``reports`` / ``previews`` / ``activity`` 都按其 ``ticker`` 的市场前缀过滤
（``previews`` 的标的在 ``value.ticker`` 里，``activity`` 的部分条目本身没有标的）；
``research/tasks`` 按其 ``market`` 字段过滤；响应带 ``market`` 与 ``filter``（含真实计数：
命中的条数、无法归属的条数、出现过哪些别的市场）。无该市场记录 → 空数组 + 说明，
**不退回全部市场**、不臆造归属。
"""
import asyncio

from server import v3_universe

#: 快照里按标的过滤的四个列表 → (字段, 命中计数字段)
RESEARCH_ROWS = (("runs", "keptRuns"), ("reports", "keptReports"),
                 ("previews", "keptPreviews"), ("activity", "keptActivity"))


def _market_of_task(task):
    """任务的市场标注（``SH``/``HK``/``US``）；缺失/认不出 → ``None``（不猜）。"""
    if not isinstance(task, dict):
        return None
    value = task.get("market")
    if value in (None, ""):
        payload = task.get("payload")
        value = payload.get("market") if isinstance(payload, dict) else None
    return v3_universe.normalize_market(value) if value not in (None, "") else None


def _row_ticker(row):
    """快照行 → 标的（``activity``/``previews`` 的标的可能在 ``value`` 里）；取不到 → ``None``。"""
    if not isinstance(row, dict):
        return None
    for key in ("ticker", "symbol", "code"):
        value = row.get(key)
        if value not in (None, ""):
            return value
    value = row.get("value")
    if isinstance(value, dict):
        for key in ("ticker", "symbol", "code"):
            item = value.get(key)
            if item not in (None, ""):
                return item
    return None


def filter_research(value, market):
    """快照 value → 四个列表都只保留该市场的行（按 ``ticker`` 前缀）；返回 ``(value, stats)``。

    没有标的的行（如无 ``ticker`` 的活动记录）**无法归属**该市场 → 一律排除并计入
    ``unattributed``（不静默留在「当前市场」的列表里，也不猜它属于哪个市场）。
    """
    stats = {"field": "runs/reports/previews/activity 的 ticker 市场前缀", "market": market,
             "runs": 0, "keptRuns": 0, "reports": 0, "keptReports": 0,
             "previews": 0, "keptPreviews": 0, "activity": 0, "keptActivity": 0,
             "unattributed": 0, "otherMarkets": [], "note": ""}
    if not isinstance(value, dict):
        stats["note"] = "快照 value 不是对象 → 未过滤，原样返回"
        return value, stats
    out = dict(value)
    for key, kept_key in RESEARCH_ROWS:
        rows = value.get(key)
        rows = rows if isinstance(rows, list) else []
        stats[key] = len(rows)
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = v3_universe.market_of_ticker(_row_ticker(row))
            if label == market:
                kept.append(row)
                stats[kept_key] += 1
            elif label is None:
                stats["unattributed"] += 1
            elif label not in stats["otherMarkets"]:
                stats["otherMarkets"].append(label)
        out[key] = kept
    return out, stats


def register(app, v3_run, home, wb_http=None):
    """wb_http(endpoint, payload) → 既有 HTTP 端点面的处理器（读类端点用）。"""

    def call_http(endpoint, payload=None):
        if wb_http is None:
            return {"ok": False, "error": {"code": "research/no-http-face",
                                           "message": "服务未注入 HTTP 端点面，无法读取该数据"}}
        try:
            return wb_http(endpoint, payload or {})
        except Exception as error:  # noqa: BLE001 —— 与工具面口径一致：失败进信封
            return {"ok": False, "error": {"code": "research/read-failed", "message": str(error)[:300]}}

    def snapshot_value():
        """优先走工具面（bound_tools 的 snapshot），拿不到再退回 HTTP 端点面。"""
        envelope = v3_run("snapshot", {})
        if isinstance(envelope, dict) and envelope.get("ok"):
            return envelope.get("value") or {}
        return call_http("snapshot") or {}

    def _market_code(raw):
        """``?market=`` → ``None``（不传/空）或 ``SH``/``HK``/``US``；非法 → 抛 ``ValueError``。"""
        if raw in (None, ""):
            return None
        code = v3_universe.normalize_market(raw)
        if code is None:
            raise ValueError("market 需为 SH / HK / US")
        return code

    @app.get("/api/v3/research")
    async def v3_research(market: str = ""):
        """研报 / 研究 run / 量化预览 / 活动流（全部来自工作台快照，按当前模式过滤）。

        ``?market=``：四个列表都按 ``ticker`` 市场前缀过滤（无法归属的行如实计数）；
        不传 → 与历史一致。
        """
        try:
            code = _market_code(market)
        except ValueError as error:
            return {"ok": False, "error": {"code": "market/bad-market", "message": str(error)}}
        value = await asyncio.to_thread(snapshot_value)
        if not isinstance(value, dict) or not value:
            return {"ok": False, "error": {"code": "research/snapshot-unavailable",
                                           "message": "工作台快照不可读（服务未就绪或记录损坏）"}}
        payload = {
            "ok": True,
            "mode": value.get("mode"),
            "generated_at": value.get("generated_at"),
            "notice": value.get("notice"),
            "recording_error": value.get("recording_error"),
            "runs": value.get("runs") or [],
            "reports": value.get("reports") or [],
            "previews": value.get("previews") or [],
            "activity": value.get("activity") or [],
            "source": "workbench/snapshot",
        }
        if code:
            filtered, stats = filter_research(value, code)
            for key in ("runs", "reports", "previews", "activity"):
                payload[key] = filtered.get(key) or []
            if not payload["runs"] and not payload["reports"]:
                stats["note"] += "；该市场没有研究 run/研报记录（不退回全部市场）"
            payload["market"] = code
            payload["filter"] = stats
        return payload

    register_pdf(app, v3_run, wb_http)

    @app.get("/api/v3/research/tasks")
    async def v3_research_tasks(market: str = ""):
        """值勤研究队列（daily_brief / factor_patrol / mining_round）——HTTP-only 端点直读。

        ``?market=``：按任务的 ``market`` 字段过滤（缺失标注的任务如实计入 ``unattributed``，
        不算作命中，也不静默丢弃）。
        """
        try:
            code = _market_code(market)
        except ValueError as error:
            return {"ok": False, "error": {"code": "market/bad-market", "message": str(error)}}
        envelope = await asyncio.to_thread(call_http, "research-tasks-list", {})
        if not isinstance(envelope, dict) or not envelope.get("ok"):
            error = (envelope or {}).get("error") if isinstance(envelope, dict) else None
            return {"ok": False, "error": error or {"code": "research/tasks-unavailable",
                                                    "message": "队列端点不可达"}}
        value = envelope.get("value") or {}
        tasks = value.get("tasks") or []
        payload = {"ok": True, "tasks": tasks, "source": "workbench/research-tasks-list"}
        if code:
            kept = [task for task in tasks if _market_of_task(task) == code]
            payload["tasks"] = kept
            payload["market"] = code
            payload["filter"] = {
                "field": "tasks[].market", "market": code, "tasks": len(tasks),
                "kept": len(kept), "unattributed": sum(1 for task in tasks
                                                       if _market_of_task(task) is None),
                "otherMarkets": sorted({label for label in map(_market_of_task, tasks)
                                        if label and label != code}),
            }
            if not kept:
                payload["filter"]["note"] = "该市场没有队列任务（不退回全部市场）"
        return payload


def register_pdf(app, v3_run, wb_http=None):
    """（由 register 内部调用）研报 PDF 导出：GET /api/v3/research/report.pdf?id=&ticker="""
    from fastapi import Request, Response

    from server import v3_report

    def pick_report(query):
        value = None
        envelope = v3_run("snapshot", {})
        if isinstance(envelope, dict) and envelope.get("ok"):
            value = envelope.get("value")
        if not isinstance(value, dict) and wb_http is not None:
            try:
                fallback = wb_http("snapshot", {}) or {}
                value = fallback.get("value") if fallback.get("ok") else None
            except Exception:  # noqa: BLE001
                value = None
        if not isinstance(value, dict):
            return None, None, {"code": "research/snapshot-unavailable",
                                "message": "工作台快照不可读"}
        reports = value.get("reports") or []
        wanted = (query.get("id") or "").strip()
        ticker = (query.get("ticker") or "").strip().upper()
        if wanted:
            hit = next((item for item in reports if str(item.get("id")) == wanted), None)
        elif ticker:
            hit = next((item for item in reports if str(item.get("ticker", "")).upper() == ticker), None)
        else:
            hit = reports[0] if reports else None
        if hit is None:
            return None, value, {"code": "research/report-not-found",
                                 "message": "没有匹配的已发布研报（可先用 ticker 或 id 筛选）"}
        return hit, value, None

    @app.get("/api/v3/research/report.pdf")
    async def v3_research_pdf(request: Request):
        """研报 → PDF（A4，暗色专业研报主题，封面页独占一页）。

        ``?id=<run id>`` 或 ``?ticker=SH.600000`` 选一篇；都不给则取最新一篇。
        渲染失败（无浏览器/超时）返回 JSON 错误信封而不是空文件。
        """
        query = dict(request.query_params)
        report, snapshot, error = await asyncio.to_thread(pick_report, query)
        if error:
            return {"ok": False, "error": error}
        html_text = v3_report.report_html(
            report,
            mode=(snapshot or {}).get("mode"),
            generated_at=(snapshot or {}).get("generated_at"),
            notice=(snapshot or {}).get("notice"),
        )
        data, meta = await asyncio.to_thread(v3_report.render_pdf, html_text)
        if data is None:
            return {"ok": False, "error": meta}
        filename = f"research-{str(report.get('ticker') or 'report').replace('.', '_')}.pdf"
        return Response(
            content=data,
            media_type="application/pdf",
            headers={
                "content-disposition": f'inline; filename="{filename}"',
                "x-pdf-bytes": str(meta.get("bytes", len(data))),
                "x-pdf-engine": str(meta.get("engine", "")),
            },
        )
