"""富途 OpenAPI 凭据配置（WP8 任务 7：Web 设置页）的读/写/连通性组装层。

三个入口（app.py 的 ``openapi_config``/``openapi_test`` 分支与 GET 专用路由调用）：

  * ``get_config_status(home)`` —— 状态快照：configured/mode/app_key_masked/
    algorithm/private_key_exists/private_key_fingerprint/channel/ready/last_error；
  * ``save_config(home, payload)`` —— 校验（app_key、PEM 或已存在的路径、算法与
    私钥匹配、channel 枚举）→ 写私钥 0600 → 合并写凭据 JSON（0600 原子写）→ 联动
    ``trading-platform.json`` 的 ``futu_channel`` → 返回保存后的状态快照；
  * ``test_connectivity(home, http=None)`` —— 用**已保存**凭据经真实调用路径
    （``OpenApiClient`` + ``OpenApiMarket.trading_days``，即 trading-days GET）发一次
    请求并计时；成功 ``{"ok":true,"value":{http_status,ret_code,ret_msg,latency_ms,data}}``，
    失败 ``{"ok":false,"error":{"code":"trading/openapi-unavailable",...}}``。

安全边界（本模块是执行点，测试在 tests/test_wp8_settings.py 全文 grep 钉死）：
  * **私钥 PEM 原文只进不出**：任何返回值（成功/失败信封）都不含 ``private_key_pem``
    与私钥文件内容；私钥在服务端的痕迹只有 0600 文件本身；
  * **完整 app_key 不回显**：状态里只有 ``app_key_masked``（前 4 + **** + 后 4；
    过短整体 ****）；
  * ``mode=oauth`` 的凭据由 ``scripts/futu_auth.py --openapi`` 授权流程写入，本页
    拒绝保存 OAuth 凭据（也不接受 oauth 模式携带 AppKey 字段）。

校验失败抛 ``WorkbenchError``（handle 统一落 ``trading/invalid-operation`` 业务失败
信封，与其他端点同形）；channel 等枚举**先校验后落盘**，绝不写半截配置。
"""
import datetime as dt
import json
import os
import time
from pathlib import Path

from server import futu_data
from server.config import (FUTU_CHANNELS, save_auto_pipeline as save_platform_auto_pipeline,
                           save_futu_channel)
from server.futu_openapi import key_fingerprint, write_private_key
from server.store_access import WorkbenchError

#: 凭据文件落点（私钥固定名见 server/futu_openapi.PRIVATE_KEY_FILENAME）
CREDENTIAL_FILENAME = "futu-openapi.json"

#: POST openapi_config 的载荷白名单（app.py 同名常量的单一事实源；锁定测试比对）
CONFIG_FIELDS = ("mode", "app_key", "algorithm", "private_key_pem",
                 "private_key_path", "channel")

#: POST auto_pipeline 的载荷白名单（app.py 同名常量的单一事实源）。键集必须与
#: ``trading_core.autopipeline.AUTO_PIPELINE_DEFAULTS`` 逐键一致（锁定测试比对）——
#: 语义定义在 core，这里只是「服务层允许接收哪些键」的声明。
AUTO_PIPELINE_FIELDS = ("enabled", "strategies", "exec_at", "exec_window_minutes",
                        "reconcile_at")

#: trading-days 的探测窗口（恒有交易日，信封必为 ret_code 0）
PROBE_MARKET = "HK"
PROBE_DAYS = 7
#: data 截断上限（连通性结果只需人工核对信封，不搬运整份响应）
DATA_MAX_CHARS = 4000

_ALGORITHM_ALIAS = {"ED25519": "Ed25519"}


def credential_path(home):
    """``<home>/futu-openapi.json``（与默认 ``CredentialStore`` 同一落点）。"""
    return Path(home) / CREDENTIAL_FILENAME


def _mask_app_key(app_key):
    """前 4 + ``****`` + 后 4；不足 8 位整体 ``****``；空 → None（绝不完整回显）。"""
    if not app_key:
        return None
    if len(app_key) < 8:
        return "****"
    return f"{app_key[:4]}****{app_key[-4:]}"


def get_config_status(home):
    """状态快照（GET / POST 空载荷共用；保存成功后的返回同形）。

    ``last_error`` 只反映凭据文件本身的读取故障（坏 JSON 等）；文件健康时恒 None。
    通道缺省/非法值按 futu_data.load_channel 的既有口径回落 mcp。
    """
    from trading_datasource.futu_openapi import CredentialStore  # noqa: PLC0415
    home = str(home)
    path = credential_path(home)
    last_error = None
    cred = {}
    try:
        cred = CredentialStore(path).load()
    except (OSError, ValueError) as error:
        last_error = str(error)[:200]
    mode = cred.get("mode")
    if mode not in ("oauth", "appkey"):
        mode = None
    app_key = cred.get("app_key") if isinstance(cred.get("app_key"), str) else None
    key_path = cred.get("private_key_path")
    key_exists = bool(key_path) and Path(str(key_path)).expanduser().is_file()
    algorithm = cred.get("algorithm")
    if not isinstance(algorithm, str) or not algorithm:
        algorithm = None
    return {
        "configured": mode is not None,
        "mode": mode,
        "app_key_masked": _mask_app_key(app_key),
        "algorithm": algorithm if mode == "appkey" else None,
        "private_key_exists": key_exists,
        "private_key_fingerprint": key_fingerprint(key_path) if key_exists else None,
        "channel": futu_data.load_channel(home),
        "ready": bool(futu_data.openapi_ready(path)),
        "last_error": last_error,
    }


def _normalize_algorithm(value):
    """Ed25519 / RSA-SHA256 归一（缺省 Ed25519）；非法值 → WorkbenchError。

    先归一再校验：``Ed25519`` 大写后是 ``ED25519``，必须经别名表还原成官方拼写，
    否则合法输入会被误拒。
    """
    from trading_datasource.futu_openapi import AppKeySigner  # noqa: PLC0415
    algo = str(value or "Ed25519").strip().upper().replace("_", "-")
    algo = _ALGORITHM_ALIAS.get(algo, algo)
    if algo not in AppKeySigner.ALGORITHMS:
        raise WorkbenchError(
            f"不支持的签名算法：{value!r}（可选 Ed25519 / RSA-SHA256）")
    return algo


def _resolve_existing_key(raw):
    """路径模式：展开 ``~`` 并要求文件存在且可读（只读校验，不改文件）。"""
    path = Path(str(raw).strip()).expanduser()
    if not path.is_file():
        raise WorkbenchError(f"私钥文件不存在：{path}")
    if not os.access(path, os.R_OK):
        raise WorkbenchError(f"私钥文件不可读（请检查文件权限）：{path}")
    return path


def save_config(home, payload):
    """校验并保存 AppKey 凭据 + 联动通道，返回保存后的状态快照（同 GET 形状）。

    校验全部通过才动第一个字节：channel 枚举与算法先验，私钥（PEM 写 0600 或
    路径存在性）次之，算法×私钥匹配用 ``AppKeySigner`` 构造验证——任一步失败都是
    ``WorkbenchError`` 且凭据 JSON 保持原样（先校验后落盘）。
    """
    from trading_datasource.futu_openapi import (  # noqa: PLC0415
        AppKeySigner, CredentialStore)
    home = str(home)
    mode = payload.get("mode") or "appkey"
    if mode != "appkey":
        raise WorkbenchError(
            "本页仅保存 AppKey 凭据；OAuth 凭据请走 scripts/futu_auth.py --openapi"
            " 授权流程（MCP 通道令牌见工作台右上角「令牌」）")
    # ---- 先校验（零落盘） ----
    channel = payload.get("channel")
    if channel is not None and channel not in FUTU_CHANNELS:
        raise WorkbenchError(
            f"futu_channel 取值非法：{channel!r}（允许：{' / '.join(FUTU_CHANNELS)}）")
    app_key = payload.get("app_key")
    if not isinstance(app_key, str) or not app_key.strip():
        raise WorkbenchError("app_key 必填（富途开放平台控制台的 AppKey ID）")
    app_key = app_key.strip()
    algorithm = _normalize_algorithm(payload.get("algorithm"))
    pem_text = payload.get("private_key_pem")
    if isinstance(pem_text, str) and pem_text.strip():
        # PEM 模式：服务端写 <home>/futu-openapi-key.pem（0600 原子写）
        try:
            key_path = Path(write_private_key(home, pem_text))
        except ValueError as error:  # 坏 PEM → 业务失败信封（绝不 500、绝不落半截文件）
            raise WorkbenchError(str(error)) from error
    else:
        raw_path = payload.get("private_key_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WorkbenchError(
                "私钥必填：粘贴 PEM 原文（服务端落盘 0600）或提供已存在的私钥文件路径（二选一）")
        key_path = _resolve_existing_key(raw_path)
    try:
        AppKeySigner.from_path(key_path, algorithm)  # 构造即验：PEM 可加载且与算法匹配
    except WorkbenchError:
        raise
    except Exception as error:  # noqa: BLE001 —— 坏 PEM/类型不符/未知算法统一业务失败
        raise WorkbenchError(f"私钥校验失败（PEM 与算法 {algorithm} 不匹配或无法加载）："
                             f"{error}") from error
    # ---- 后落盘：合并保存（保留文件里已有的 OAuth token 字段，切模式不丢授权） ----
    store = CredentialStore(credential_path(home))
    try:
        data = dict(store.load())
    except ValueError as error:
        raise WorkbenchError(f"凭据文件损坏，拒绝覆盖（请手工修复或删除后重试）："
                             f"{error}") from error
    data.update({"mode": "appkey", "app_key": app_key, "algorithm": algorithm,
                 "private_key_path": str(key_path)})
    try:
        store.save(data)
    except OSError as error:
        raise WorkbenchError(f"凭据写入失败：{error}") from error
    if channel is not None:
        try:
            save_futu_channel(home, channel)
        except ValueError as error:
            raise WorkbenchError(str(error)) from error
    return get_config_status(home)


def get_auto_pipeline(home):
    """auto_pipeline **有效配置**快照（缺省补全后的完整结构；GET 与 POST 空载荷共用）。

    直接返回 core 的 ``autopipeline.auto_pipeline_config``：页面看到的就是调度侧实际
    生效的值（未配置的市场显示默认时刻，而不是空——空会让「没配」看起来像「不会跑」）。
    """
    from trading_core import autopipeline  # noqa: PLC0415
    return autopipeline.auto_pipeline_config(str(home))


def save_auto_pipeline(home, payload):
    """校验并原子写 trading-platform.json 的 ``auto_pipeline`` 键，返回有效配置快照。

    纪律与 ``save_config`` 一致（**先校验后落盘**）：

      * 校验复用 ``trading_core.autopipeline.apply_overlay``——与调度侧同一实现，
        服务层不重新定义「什么算合法配置」；
      * 非法载荷抛 ``WorkbenchError`` → ``trading/invalid-operation`` 业务失败信封，
        **文件零改动**（含 JSON 损坏、权限失败：一条都不写半截）；
      * 落盘的是**提交的 overlay 本身**（不是补全后的完整配置）：该文件是覆盖层，
        缺省值在读取时补——这样默认值演进对新旧配置一致生效，也不会把默认值冻结进
        用户文件（与 ``futu_channel`` 只落一个键同构）。
    """
    from trading_core import autopipeline  # noqa: PLC0415
    if not isinstance(payload, dict) or not payload:
        raise WorkbenchError("auto_pipeline 载荷需为非空对象")
    try:
        # 在默认值之上试算一遍：任何非法键/取值在此暴露，落盘不会发生
        autopipeline.apply_overlay(autopipeline.AUTO_PIPELINE_DEFAULTS, payload)
    except ValueError as error:
        raise WorkbenchError(str(error)) from error
    try:
        save_platform_auto_pipeline(home, dict(payload))
    except (OSError, ValueError) as error:
        raise WorkbenchError(f"auto_pipeline 写入失败：{error}") from error
    return get_auto_pipeline(home)


def test_connectivity(home, http=None):
    """用已保存凭据发一次真实 trading-days GET 并计时（注入 http 供测试离线）。

    成功：``{"ok":true,"value":{http_status, ret_code, ret_msg, latency_ms, data}}``
    （data 序列化超 DATA_MAX_CHARS 时截断——连通性结果只供人工核对信封）。
    失败（凭据缺失/坏私钥/业务错误信封/非信封响应/传输异常）：``ok:false`` 且
    code=``trading/openapi-unavailable``——与行情直通层同一错误码，绝不假装成功。
    """
    from trading_datasource import futu_openapi as fo  # noqa: PLC0415
    captured = {}

    def transport(method, url, headers, body):
        send = http if http is not None else fo._default_http
        status, resp_body, resp_headers = send(method, url, headers, body)
        captured["status"] = status
        captured["body"] = resp_body
        return status, resp_body, resp_headers

    client = fo.OpenApiClient(fo.CredentialStore(credential_path(home)), http=transport)
    market = fo.OpenApiMarket(client)
    today = dt.date.today()
    started = time.perf_counter()
    try:
        data = market.trading_days(
            PROBE_MARKET,
            (today - dt.timedelta(days=PROBE_DAYS)).isoformat(),
            (today + dt.timedelta(days=PROBE_DAYS)).isoformat())
    except Exception as error:  # noqa: BLE001 —— 凭据/信封/传输层全部如实收敛为 unavailable
        return {"ok": False,
                "error": {"code": "trading/openapi-unavailable",
                          "message": str(error)[:300], "details": {}}}
    latency_ms = int((time.perf_counter() - started) * 1000)
    envelope = fo._safe_json_dict(captured.get("body")) or {}
    data_payload = data if isinstance(data, dict) else {"value": data}
    serialized = json.dumps(data_payload, ensure_ascii=False)
    if len(serialized) > DATA_MAX_CHARS:
        data_payload = {"truncated": serialized[:DATA_MAX_CHARS] + "…"}
    return {"ok": True, "value": {
        "http_status": captured.get("status"),
        "ret_code": 0,
        "ret_msg": envelope.get("ret_msg") or "success",
        "latency_ms": latency_ms,
        "data": data_payload,
    }}
