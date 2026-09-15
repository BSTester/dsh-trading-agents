"""调度守护进程（规格 §8.1）：无 LLM 单进程；按交易日历触发作业链；
心跳落 ~/.dsh/trading-daemon.json；指令目录轮询在 commands 模块。
调度循环与作业执行在 WP4 任务 1/4 补齐。"""
from pathlib import Path


def heartbeat_path(home):
    return Path(home) / "trading-daemon.json"


def commands_dir(home):
    return Path(home) / "trading-commands"
