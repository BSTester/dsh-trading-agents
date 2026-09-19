"""V3 风险配置接口（风险监控页「事前风控」区块消费）。

数据来自既有工作台工具面 ``risk``（阈值配置的唯一事实源）。本层只做字段适配：
工作台给什么就返回什么，取不到就返回错误并说明，页面显示「无数据源 + 原因」。
"""
import asyncio


def register(app, v3_run, home):
    @app.get("/api/v3/risk")
    async def v3_risk():
        def build():
            envelope = v3_run("risk", {})
            if not isinstance(envelope, dict) or not envelope.get("ok"):
                error = (envelope or {}).get("error") if isinstance(envelope, dict) else None
                return {"ok": False, "error": error or {"code": "risk/config-unavailable",
                                                        "message": "工作台 risk 工具不可用"}}
            value = envelope.get("value") or {}
            # 工作台的配置可能在 value.config / value 本身；两者都兼容
            config = value.get("config") if isinstance(value.get("config"), dict) else value
            return {
                "ok": True,
                "data": {
                    "config": config,
                    "source": value.get("source") or "workbench/risk",
                    "as_of": value.get("as_of"),
                },
            }

        return await asyncio.to_thread(build)
