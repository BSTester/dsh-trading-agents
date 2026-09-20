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
"""
import asyncio


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

    @app.get("/api/v3/research")
    async def v3_research():
        """研报 / 研究 run / 量化预览 / 活动流（全部来自工作台快照，按当前模式过滤）。"""
        value = await asyncio.to_thread(snapshot_value)
        if not isinstance(value, dict) or not value:
            return {"ok": False, "error": {"code": "research/snapshot-unavailable",
                                           "message": "工作台快照不可读（服务未就绪或记录损坏）"}}
        return {
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

    register_pdf(app, v3_run, wb_http)

    @app.get("/api/v3/research/tasks")
    async def v3_research_tasks():
        """值勤研究队列（daily_brief / factor_patrol / mining_round）——HTTP-only 端点直读。"""
        envelope = await asyncio.to_thread(call_http, "research-tasks-list", {})
        if not isinstance(envelope, dict) or not envelope.get("ok"):
            error = (envelope or {}).get("error") if isinstance(envelope, dict) else None
            return {"ok": False, "error": error or {"code": "research/tasks-unavailable",
                                                    "message": "队列端点不可达"}}
        value = envelope.get("value") or {}
        return {"ok": True, "tasks": value.get("tasks") or [],
                "source": "workbench/research-tasks-list"}


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
