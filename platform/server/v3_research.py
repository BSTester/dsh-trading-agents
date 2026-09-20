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
