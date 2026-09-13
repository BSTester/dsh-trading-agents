#!/usr/bin/env python3
"""跨插件定位：找到兄弟插件包里的 python 脚本。

背景：插件包各自独立（各自进 node_modules），一个插件的脚本无法 import 另一个插件的模块。
需要跨包调用时（例如量化侧要用 fin-data 的情绪渠道），按以下顺序定位：

  1. $DSH_TRADING_PYTHON 环境变量（显式覆盖，测试与排障用）
  2. $DSH_HOME/trading-python/<plugin>/    ← 安装器生成的统一目录（单一来源，重装即刷新）
  3. <repo>/plugins/<plugin>/python/       ← 仓库开发布局

安装器写出的 <venv>/site-packages/dsh-trading-python.pth 让本包自身可被 import；
兄弟脚本则通过本模块定位后再以子进程调用（见 run_script）。
"""
import os
import subprocess
import sys
from pathlib import Path

DSH_HOME = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
UNIFIED_ROOT_NAME = "trading-python"

# 本文件位于 <...>/trading_datasource/locate.py：
#   仓库布局   <repo>/plugins/datasource/python/trading_datasource/locate.py → parents[3] = plugins/
#   安装布局   <DSH>/trading-python/datasource/trading_datasource/locate.py → parents[2] = trading-python/datasource
_REPO_PLUGINS = Path(__file__).resolve().parents[3]


def unified_root():
    override = os.environ.get("DSH_TRADING_PYTHON")
    return Path(override) if override else DSH_HOME / UNIFIED_ROOT_NAME


def plugin_python_dir(plugin):
    """返回某插件 python 目录；找不到返回 None（调用方据此降级，不伪造数据）。"""
    for candidate in (unified_root() / plugin,
                      _REPO_PLUGINS / plugin / "python"):
        if candidate.is_dir():
            return candidate
    return None


def find_script(plugin, script):
    """返回某插件内某个脚本的路径；找不到返回 None。"""
    directory = plugin_python_dir(plugin)
    if directory is None:
        return None
    path = directory / script
    return path if path.is_file() else None


def python_executable():
    """优先用交易 venv 的解释器（akshare/playwright 都装在那里）。"""
    venv = DSH_HOME / "trading-venv"
    candidate = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(candidate) if candidate.exists() else sys.executable


def run_script(plugin, script, args=(), timeout=180):
    """以子进程运行兄弟插件的脚本，返回解析后的 JSON。

    脚本不存在 → 返回 None（调用方应把该数据源标为不可用，而不是继续编造）。
    脚本非零退出 → 返回 None，错误信息留在返回结构的调用方日志里。
    """
    import json
    path = find_script(plugin, script)
    if path is None:
        return None
    try:
        completed = subprocess.run([python_executable(), str(path), *args],
                                   capture_output=True, text=True, timeout=timeout,
                                   env={**os.environ, "SOCIAL_BROWSER_PERSIST": "1"})
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = completed.stdout or ""
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        return json.loads(stdout[start:])
    except ValueError:
        return None
