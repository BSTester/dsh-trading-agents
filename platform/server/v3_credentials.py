"""V3 密钥与授权配置（**页面可操作**）。

本模块把 V3 侧所需的密钥做成可读状态 / 保存 / 测试 / 清除四个动作，供
``/api/v3/credentials`` 路由使用。

**当前注册表为空（2026-09-21 数据源政策）**：除富途（授权使用，凭据走独立的
``futu-openapi.json`` / 设置页 OAuth 流程，不在本模块）外，数据渠道一律使用
免密钥公开端点；唯一曾注册的 Tushare Pro token 因「需要 token」随 Tushare Pro
能力一并移除（能力由富途 f10 / AKShare / SEC EDGAR 免密覆盖）。
``KEYS`` 保留通用机制（校验 / 0600 落盘 / 状态掩码），供未来**确需凭据**的渠道
注册时复用——恢复注册不等于政策放宽，仍需逐项审查。

硬约束（与既有设置页同一纪律）：
  * 凭据落 ``<home>/v3-credentials.json``，**0600**，先校验后原子写；
  * 任何响应**都不回显凭据值**——status 只给 present/source/updated_at/掩码尾号；
  * **环境变量优先**于文件：部署方用 env，临时/单机用页面写文件。
"""
import json
import os
import time

#: 支持的密钥注册表。env 为环境变量名（优先级高于文件），label 给人看。
#: 当前为空（数据源政策：数据渠道一律免密钥；富途凭据不在本模块管理）。
KEYS: dict = {}

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


def status(home, get_env=None):
    """密钥状态（**不含任何凭据值**）。注册表为空时 ``keys`` 就是空列表。"""
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
    return {"ok": True, "keys": keys,
            "note": "数据源政策（2026-09-21）：数据渠道一律免密钥公开端点，当前无注册凭据；"
                    "富途凭据走设置页 OAuth/AppKey 流程（不在本模块）"}


def validate(key, value):
    """保存前校验：返回错误消息或 None。"""
    meta = KEYS.get(key)
    if meta is None:
        return (f"不支持的密钥 {key!r}（可选：{' / '.join(sorted(KEYS))}；"
                "数据源政策：数据渠道一律免密钥，不注册新的数据类凭据）")
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
        key = str(payload.get("key") or "")

        def run():
            if action == "status":
                return status(home, os.environ.get)
            if action == "save":
                return save(home, key, payload.get("value"))
            if action == "clear":
                return clear(home, key)
            if action == "test":
                # 当前注册表为空：test 动作如实说明（不再有 tushare 连通性测试）
                return {"ok": False, "error": {"code": "v3-credentials/no-test",
                                               "message": f"{key or '(未指定)'} 无已注册的连通性测试"
                                                          "（数据源政策：数据渠道一律免密钥）"}}
            return {"ok": False, "error": {"code": "v3-credentials/unknown-action",
                                           "message": f"action 需为 status / save / test / clear，收到 {action!r}"}}

        return respond(await asyncio.to_thread(run))
