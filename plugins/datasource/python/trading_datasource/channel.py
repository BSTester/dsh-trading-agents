"""通道分派：富途取数的 ``openapi`` 优先 / ``mcp`` 回退（WP13 任务 1，规格 §8.1）。

为什么在本包：``trading_core``（core）与 ``workbench`` 脚本都要做同一件事——读
``futu_channel``、判 OpenAPI 凭据就绪、选通道取数。只有放在双方共同依赖的
``trading_datasource`` 里才可能**只有一份实现**；``platform/server/futu_data.py`` 与
``trading_core/research_sync.py`` 的 ``openapi_ready``/``load_channel`` 已改为委托本模块，
「凭据是否可用」「当前通道是什么」从此只有一个答案（历史上 core 侧是刻意镜像 + 等价测试）。

三条语义（规格 §8.1）：

  * ``openapi`` 且凭据就绪 → REST 调用；**异常原样上抛**（不静默换通道——静默换通道会让
    限频/权限/参数错误伪装成 MCP 行为，排障时看不到真实原因）；
  * ``openapi`` 但凭据未就绪 → **回退 mcp**，返回的第二元素标 ``"mcp(fallback)"``
    （可观测；不回退才是错的——没配凭据就彻底不可用会白断掉数据链）；
  * ``mcp``（默认）→ mcp，标 ``"mcp"``。

两通道都失败 → 异常上抛（宁缺毋假：调用点负责如实上报，不写占位行）。

边界（刻意不做）：本模块只做「通道选择 + 一次调用」，**不做业务聚合、不改落库口径**。
清洗/落库留在各调用点；两边**参数形状差异**（如经济日历 mcp 用 ``YYYYMMDD``、REST 用
``YYYY-MM-DD``）由调用点显式给出（``params`` 给 mcp、``openapi_params`` 给 REST），
不藏进助手——藏起来调用点就看不清两边差异了。
"""
import json
import os
from pathlib import Path

#: ``trading-platform.json`` 的 ``futu_channel`` 合法值（与 server/futu_data 既有常量同值）
CHANNEL_MCP = "mcp"
CHANNEL_OPENAPI = "openapi"

#: 回退标记（返回值第二元素）：调用方可据此告警/展示，语义是「本次实际走了 mcp」
CHANNEL_MCP_FALLBACK = "mcp(fallback)"

#: ``trading-platform.json`` 文件名（与 server/config.py 同一落点）
CONFIG_FILENAME = "trading-platform.json"


def config_home(home=None):
    """数据根目录：显式 ``home`` > ``$DSH_HOME`` > ``~/.dsh``（与 server/config 同口径）。"""
    return str(home or os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def config_path(home=None):
    """``<home>/trading-platform.json``。"""
    return Path(config_home(home)) / CONFIG_FILENAME


def channel_of(home=None):
    """读 ``futu_channel``（openapi|mcp，默认 mcp）。

    容错语义与 ``server/futu_data.load_channel`` 逐字一致（历史实现，本函数即其下沉）：
    缺文件 → 默认 mcp；坏 JSON → ``ValueError``（不静默吞配置错误）；非法值/缺键 → mcp。
    """
    path = config_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CHANNEL_MCP
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"trading-platform.json 解析失败：{error}") from error
    channel = raw.get("futu_channel") if isinstance(raw, dict) else None
    return channel if channel in (CHANNEL_MCP, CHANNEL_OPENAPI) else CHANNEL_MCP


def openapi_ready(credential_path=None):
    """OpenAPI 凭据是否可用（**全仓库唯一判定**，实现自 server/futu_data 下沉）。

    oauth → access_token/refresh_token 至少有一个（可刷新）；
    appkey → app_key 与 private_key_path 齐备**且私钥文件可加载**
    （``AppKeySigner.from_path``：文件缺失/坏 PEM/算法不支持/私钥类型不符 → 不可用）；
    其余（文件缺失/坏 JSON/mode 未配置）→ False。

    「私钥可加载」是凭据就绪的一部分：只看路径存在会让实盘写穿过闸门（**人工确认被
    消耗**）后才在签名时失败；判在这里则确认零消耗、零 HTTP 调用。
    """
    from .futu_openapi import CredentialStore  # noqa: PLC0415 —— 避免导入期拉起 cryptography
    try:
        cred = CredentialStore(credential_path).load()
    except (OSError, ValueError):
        return False
    mode = cred.get("mode")
    if mode == "oauth":
        return bool(cred.get("access_token") or cred.get("refresh_token"))
    if mode == "appkey":
        if not (cred.get("app_key") and cred.get("private_key_path")):
            return False
        from .futu_openapi import AppKeySigner  # noqa: PLC0415
        try:
            AppKeySigner.from_path(cred["private_key_path"],
                                   cred.get("algorithm", "Ed25519"))
        except Exception:  # noqa: BLE001 —— 私钥缺失/坏 PEM/算法不支持 → 凭据不可用
            return False
        return True
    return False


#: OpenApiClient 单例表：key = 凭据路径。**为什么缓存**：token 续期状态在 client 上，
#: 每次取数新建一个 client 会让同一进程内反复刷新 token（每次取数都多一跳）。
_CLIENTS = {}


def openapi_client(credential_path=None):
    """共用 ``OpenApiClient``（按凭据路径缓存；避免同进程反复刷新 token）。"""
    key = str(credential_path or "")
    client = _CLIENTS.get(key)
    if client is None:
        from .futu_openapi import CredentialStore, OpenApiClient  # noqa: PLC0415
        client = OpenApiClient(CredentialStore(credential_path))
        _CLIENTS[key] = client
    return client


def reset_clients():
    """清空 client 缓存（测试隔离用；生产无需调用）。"""
    _CLIENTS.clear()


#: 方法组名 → 类（惰性导入：避免导入期拉起 cryptography）。名字与 ``method`` 的点号前缀一致。
_GROUP_CLASSES = None


def _group_classes():
    global _GROUP_CLASSES
    if _GROUP_CLASSES is None:
        from .futu_openapi import (  # noqa: PLC0415
            OpenApiBasicData, OpenApiDerivatives, OpenApiF10, OpenApiIpo, OpenApiPlate,
            OpenApiScreen, OpenApiShort, OpenApiWatchlist)
        _GROUP_CLASSES = {"basic": OpenApiBasicData, "derivatives": OpenApiDerivatives,
                          "f10": OpenApiF10, "ipo": OpenApiIpo, "plate": OpenApiPlate,
                          "screen": OpenApiScreen, "short": OpenApiShort,
                          "watchlist": OpenApiWatchlist}
    return _GROUP_CLASSES


def call_openapi(client, method, params):
    """按 ``"组.方法"`` 点号路径调用（如 ``"f10.statements"``、``"basic.rehab"``）。

    未知组/未知方法 → ``ValueError``（本地拒绝，零网络往返）——与传输层
    「签名即白名单」同一口径：调用点写错方法名要立刻炸，不能悄悄走到别的方法上。
    """
    if not isinstance(method, str) or "." not in method:
        raise ValueError(f"method 形如 'f10.statements'，收到 {method!r}")
    group_name, name = method.split(".", 1)
    group_cls = _group_classes().get(group_name)
    if group_cls is None:
        raise ValueError(f"未知方法组：{group_name!r}（允许：{sorted(_group_classes())}）")
    target = getattr(group_cls(client), name, None)
    if target is None:
        raise ValueError(f"方法组 {group_name} 无方法 {name!r}")
    return target(**params)


def fetch(tool, params, *, method, openapi_params=None, adapter=None, home=None,
          client=None, credential_path=None, mcp_call=None):
    """按通道取一次数，返回 ``(data, channel_used)``。

    ``tool``/``params``       —— MCP 工具名与 MCP 侧参数（原样透传 ``call_tool``）
    ``method``                —— REST 侧 ``"组.方法"``（如 ``"f10.valuation_detail"``）
    ``openapi_params``        —— REST 侧参数；缺省复用 ``params``（两边同形时省略）
    ``adapter``               —— 可选 ``data -> data`` 归一（两边响应形状确有差异时用；
                                 本函数不猜形状，归一由调用点显式声明）
    ``client``                —— 注入即可钉住 openapi 通道（测试确定性；与 server
                                 ``FutuData`` 的「注入替身视为可用」同口径）
    ``mcp_call``              —— 注入 MCP 替身（缺省 ``futu_mcp.call_tool``）

    回退语义：``channel=openapi`` 且 ``client`` 未注入且凭据未就绪 → 走 mcp 并标
    ``"mcp(fallback)"``；此时**不尝试** REST（避免明知无凭据还发一次注定失败的请求）。
    """
    channel = channel_of(home)
    if channel == CHANNEL_OPENAPI and (client is not None or openapi_ready(credential_path)):
        used_client = client if client is not None else openapi_client(credential_path)
        kwargs = dict(openapi_params if openapi_params is not None else params)
        data = call_openapi(used_client, method, kwargs)
        return (_apply(adapter, data), CHANNEL_OPENAPI)
    call = mcp_call if mcp_call is not None else _mcp_call()
    data = call(tool, params)
    used = CHANNEL_MCP_FALLBACK if channel == CHANNEL_OPENAPI else CHANNEL_MCP
    return (_apply(adapter, data), used)


def _apply(adapter, data):
    return data if adapter is None else adapter(data)


def _mcp_call():
    from .futu_mcp import call_tool  # noqa: PLC0415 —— 惰性导入（避免导入期建会话）
    return call_tool
