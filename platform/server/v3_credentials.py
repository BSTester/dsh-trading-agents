"""V3 密钥与授权配置（**页面可操作**）。

为什么单独一层：规格要求「所有需要密钥的配置在页面上就能完成」。本模块把 V3 侧所需的
密钥（当前只有 Tushare Pro）做成可读状态 / 保存 / 测试 / 清除四个动作，供
``/api/v3/credentials`` 路由使用。

硬约束（与既有设置页同一纪律）：
  * 凭据落 ``<home>/v3-credentials.json``，**0600**，先校验后原子写；
  * 任何响应**都不回显凭据值**——status 只给 present/source/updated_at/掩码尾号；
  * **环境变量优先**于文件：部署方用 env，临时/单机用页面写文件；
  * 未配置时**不发出任何外部请求**（Tushare 取数直接返回 no-token）。

OAuth 类授权（富途 OpenAPI）不在本模块：既有工作台设置页已完整支持
（``/api/wb/openapi_oauth`` 的 start/status/cancel + ``openapi_config`` 保存 AppKey），
V3 接入与授权页只做状态展示与入口跳转，避免出现第二套授权实现。
"""
import json
import os
import time
import urllib.error
import urllib.request

#: 支持的密钥。env 为环境变量名（优先级高于文件），label 给人看。
KEYS = {
    "tushare_token": {
        "label": "Tushare Pro Token",
        "env": "TUSHARE_TOKEN",
        "usage": "A 股财务/行情（/api/v3/tushare）",
        "min_length": 16,
    },
}

TUSHARE_ENDPOINT = "http://api.tushare.pro"
CREDENTIALS_FILE = "v3-credentials.json"


def credential_path(home):
    return os.path.join(str(home), CREDENTIALS_FILE)


def _read_file(home):
    path = credential_path(home)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        # 损坏的凭据文件按「未配置」处理，不阻断服务；调用方会看到 present=false
        return {}
    return data if isinstance(data, dict) else {}


def _write_file(home, data):
    """原子写 + 0600（先写临时文件再 rename，避免半截文件）。"""
    path = credential_path(home)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _hint(value):
    """掩码提示：只保留尾 4 位，用于「是不是我刚填的那把」的人眼核对。"""
    text = str(value or "")
    return f"…{text[-4:]}" if len(text) >= 8 else None


def resolve(home, key, get_env=None):
    """按「环境变量 → 凭据文件」解析密钥值，返回 ``(value, source)``。"""
    meta = KEYS.get(key)
    if meta is None:
        return None, None
    if get_env is not None:
        env_value = get_env(meta["env"])
        if env_value:
            return str(env_value), f"环境变量 {meta['env']}"
    stored = _read_file(home).get(key)
    if isinstance(stored, dict):
        value = stored.get("value")
    else:
        value = stored
    if value:
        return str(value), "页面配置（v3-credentials.json）"
    return None, None


def resolve_tushare_token(home, get_env=None):
    """Tushare 取数专用：返回 ``(token, source)``。"""
    return resolve(home, "tushare_token", get_env)


def status(home, get_env=None):
    """密钥状态（**不含任何凭据值**）。"""
    stored = _read_file(home)
    keys = []
    for key, meta in KEYS.items():
        value, source = resolve(home, key, get_env)
        record = stored.get(key) if isinstance(stored.get(key), dict) else {}
        keys.append({
            "key": key,
            "label": meta["label"],
            "usage": meta["usage"],
            "env": meta["env"],
            "present": bool(value),
            "source": source,
            "updated_at": record.get("updated_at"),
            "hint": _hint(value) if value else None,
            "file": credential_path(home),
        })
    return {"ok": True, "keys": keys}


def validate(key, value):
    """保存前校验：返回错误消息或 None。"""
    meta = KEYS.get(key)
    if meta is None:
        return f"不支持的密钥 {key!r}（可选：{' / '.join(sorted(KEYS))}）"
    text = str(value or "").strip()
    if not text:
        return "密钥不能为空"
    if len(text) < meta["min_length"]:
        return f"密钥长度不足（至少 {meta['min_length']} 位）"
    if any(ch.isspace() for ch in text):
        return "密钥不能包含空白字符"
    return None


def save(home, key, value):
    """保存（校验 → 原子写 0600）。返回状态快照，**不回显值**。"""
    problem = validate(key, value)
    if problem:
        return {"ok": False, "error": {"code": "v3-credentials/invalid", "message": problem}}
    data = _read_file(home)
    data[key] = {"value": str(value).strip(), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _write_file(home, data)
    return status(home)


def clear(home, key):
    """清除文件中的密钥（环境变量不受影响）。"""
    if key not in KEYS:
        return {"ok": False, "error": {"code": "v3-credentials/unknown-key", "message": f"不支持的密钥 {key!r}"}}
    data = _read_file(home)
    if key in data:
        data.pop(key)
        _write_file(home, data)
    return status(home)


def test_tushare(home, get_env=None, http=None, timeout=20):
    """真实连通性测试：用当前生效的 token 调一次最小 Tushare 接口。

    只回传 ok/延迟/上游消息，**不含 token**。未配置 → no-token（不发请求）。
    """
    token, source = resolve_tushare_token(home, get_env)
    if not token:
        return {"ok": False, "error": {"code": "tushare/no-token",
                                       "message": "TUSHARE_TOKEN 未注入（环境变量或页面配置）"}}
    body = json.dumps({
        "api_name": "trade_cal",
        "token": token,
        "params": {"exchange": "SSE", "start_date": "20260101", "end_date": "20260110"},
        "fields": "cal_date,is_open",
    }).encode("utf-8")
    started = time.time()
    try:
        if http is not None:  # 测试注入：签名 (body, timeout) -> dict
            payload = http(body, timeout)
        else:
            payload = _post(TUSHARE_ENDPOINT, body, timeout)
    except Exception as error:  # noqa: BLE001 —— 网络/超时/解析统一收敛
        return {"ok": False, "source": source,
                "error": {"code": "tushare/network", "message": str(error)[:200]}}
    elapsed = int((time.time() - started) * 1000)
    if payload.get("code") != 0:
        return {"ok": False, "source": source, "latency_ms": elapsed,
                "error": {"code": "tushare/api", "message": str(payload.get("msg"))[:200]}}
    rows = (payload.get("data") or {}).get("items") or []
    return {"ok": True, "source": source, "latency_ms": elapsed, "rows": len(rows)}


def _post(url, body, timeout):
    request = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 —— 固定 http 端点
        return json.loads(response.read().decode("utf-8"))


def register(app, v3_run, home):
    """注册 ``/api/v3/credentials``（GET 读状态；POST save/test/clear）。"""
    import asyncio

    from fastapi import Request
    from fastapi.responses import JSONResponse

    def respond(payload):
        return JSONResponse(status_code=200, content=payload)

    @app.get("/api/v3/credentials")
    async def v3_credentials_read():
        return respond(await asyncio.to_thread(status, home, os.environ.get))

    @app.post("/api/v3/credentials")
    async def v3_credentials_write(request: Request):
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return respond({"ok": False, "error": {"code": "v3-credentials/bad-json",
                                                   "message": "请求体不是合法 JSON"}})
        if not isinstance(payload, dict):
            return respond({"ok": False, "error": {"code": "v3-credentials/bad-json",
                                                   "message": "请求体需为对象"}})
        action = str(payload.get("action") or "status")
        key = str(payload.get("key") or "tushare_token")

        def run():
            if action == "status":
                return status(home, os.environ.get)
            if action == "save":
                return save(home, key, payload.get("value"))
            if action == "clear":
                return clear(home, key)
            if action == "test":
                if key == "tushare_token":
                    return test_tushare(home, os.environ.get)
                return {"ok": False, "error": {"code": "v3-credentials/no-test",
                                               "message": f"{key} 暂无连通性测试"}}
            return {"ok": False, "error": {"code": "v3-credentials/unknown-action",
                                           "message": f"action 需为 status / save / test / clear，收到 {action!r}"}}

        return respond(await asyncio.to_thread(run))
