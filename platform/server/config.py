"""服务配置（规格 §3.7）：~/.dsh/trading-platform.json 的 service 节可覆盖默认值。

优先级：环境变量 TRADING_SERVICE_PORT > 配置文件 > 默认（env 覆盖在缺文件时同样生效——
集成测试正是用「临时 DSH_HOME + TRADING_SERVICE_PORT=0」起服务的）。
这是 platform/server/config.mjs 的 Python 移植版，语义逐条对齐。
"""
import json
import os
from pathlib import Path

DEFAULTS = {"port": 8397, "host": "127.0.0.1", "token": None}


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
