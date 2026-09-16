"""服务配置（规格 §3.7）：~/.dsh/trading-platform.json 的 service 节可覆盖默认值。

优先级：环境变量 TRADING_SERVICE_PORT > 配置文件 > 默认（env 覆盖在缺文件时同样生效——
集成测试正是用「临时 DSH_HOME + TRADING_SERVICE_PORT=0」起服务的）。
这是 Node 服务层 config.mjs 原实现（已退役，见 git 历史 ``aaa5f42^``）的 Python 移植版，
语义逐条对齐。
"""
import json
import os
import tempfile
from pathlib import Path

DEFAULTS = {"port": 8397, "host": "127.0.0.1", "token": None}

# 富途通道（WP8 任务 7：设置页保存凭据时联动更新；读取方是 futu_data.load_channel）
FUTU_CHANNELS = ("openapi", "mcp")


def config_path(home=None):
    home = home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(home) / "trading-platform.json"


def _valid_port(value):
    return isinstance(value, int) and not isinstance(value, bool) and (value == 0 or 0 < value < 65536)


def load_config(home=None):
    merged = dict(DEFAULTS)
    try:
        text = config_path(home).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    if text is not None:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"trading-platform.json 解析失败：{error}") from error
        service = raw.get("service") or {}
        port = service.get("port")
        if _valid_port(port) and port > 0:
            merged["port"] = port
        host = service.get("host")
        if isinstance(host, str) and host:
            merged["host"] = host
        token = service.get("token")
        if isinstance(token, str) and token:
            merged["token"] = token
    env = os.environ.get("TRADING_SERVICE_PORT")
    if env not in (None, ""):
        try:
            env_port = int(env)
        except ValueError:
            env_port = None
        if _valid_port(env_port):
            merged["port"] = env_port
    return merged


def _save_platform_key(home, key, value):
    """原子写 trading-platform.json 顶层**单键**（其余键原样保留）。

    全仓库写该文件的唯一实现（``save_futu_channel`` 与 ``save_auto_pipeline`` 共用）：
    先读现有文件 → 合并一个键 → 临时文件 fsync → ``os.replace``；文件缺失则新建
    （0600：里面有 service.token 时不能宽权限），已有文件权限原样保留。解析失败
    一律 ``ValueError``（绝不覆盖读不懂的文件——那会静默抹掉用户的其它配置）。
    """
    path = config_path(home)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"trading-platform.json 解析失败：{error}") from error
        if not isinstance(data, dict):
            raise ValueError("trading-platform.json 顶层必须是 JSON 对象")
    data = {**data, key: value}
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trading-platform.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)  # 已有文件：权限原样保留
        else:
            os.chmod(tmp, 0o600)  # 新建：可能含 service.token，不放宽
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return value


def save_futu_channel(home=None, channel=None):
    """写 trading-platform.json 顶层 ``futu_channel``（WP8 任务 7：设置页联动）。

    channel 必须是 ``openapi|mcp``（否则 ``ValueError``，调用方先校验后落盘——绝不写
    半截配置）；落盘细节见 ``_save_platform_key``。
    """
    if channel not in FUTU_CHANNELS:
        raise ValueError(f"futu_channel 取值非法：{channel!r}（允许：{' / '.join(FUTU_CHANNELS)}）")
    return _save_platform_key(home, "futu_channel", channel)


def save_auto_pipeline(home=None, value=None):
    """写 trading-platform.json 顶层 ``auto_pipeline``（WP10 任务 2：设置页开关）。

    **本函数只负责落盘，不负责校验**：校验在 ``settings_api.save_auto_pipeline``，
    复用 ``trading_core.autopipeline.apply_overlay``（与调度侧同一实现）——配置语义
    归 core，服务层不重复定义什么是合法配置。
    """
    return _save_platform_key(home, "auto_pipeline", value)
