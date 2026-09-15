"""调度守护进程（规格 §8.1）：无 LLM 单进程；按交易日历触发作业链；
心跳落 ~/.dsh/trading-daemon.json；指令目录轮询在 commands 模块。"""
import json
from pathlib import Path

from . import store

JOBS_DEFAULT = {
    "SH": [{"name": "sync_bars", "at": "16:00", "cmd": ["sync-bars", "--tickers", "@watchlist"]},
            {"name": "sync_fundamentals", "at": "16:00", "cmd": ["fundamentals", "--tickers", "@watchlist"]},
            {"name": "merge_announcements", "at": "16:05", "cmd": ["merge-announcements", "--period", "@latest-quarter"]},
            {"name": "quality", "at": "16:10", "cmd": ["quality", "--market", "SH"]}],
    "HK": [{"name": "sync_bars", "at": "16:30", "cmd": ["sync-bars", "--tickers", "@watchlist"]}],
    "US": [{"name": "sync_bars", "at": "05:30", "cmd": ["sync-bars", "--tickers", "@watchlist"]}],
}


def heartbeat_path(home):
    return Path(home) / "trading-daemon.json"


def commands_dir(home):
    return Path(home) / "trading-commands"


def write_heartbeat(home, payload):
    p = heartbeat_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def tick(conn, home, jobs=None, now=None):
    """一轮调度：对每个市场判断「今日为交易日 且 当前时间 ≥ at 且 今日未跑」，
    满足则执行并记录。now 注入便于假时钟测试。"""
    now = now or _real_now
    jobs = jobs or JOBS_DEFAULT
    state = store.kv_get(conn, "daemon:state", default={"ran": {}})
    stamp = now()
    for market, chain in jobs.items():
        if not store.is_trading_day(conn, market, stamp[:10]):
            continue
        for job in chain:
            key = f"{market}:{job['name']}:{stamp[:10]}"
            if state["ran"].get(key) or stamp[11:16] < job["at"]:
                continue
            _run_job(conn, job, home)
            state["ran"][key] = stamp
    store.kv_set(conn, "daemon:state", state)
    write_heartbeat(home, {"heartbeat": stamp,
                           "last_job": store.kv_get(conn, "daemon:last_job", ""),
                           "next": "见 trading-platform.json"})
    return state


def _run_job(conn, job, home, runner=None):
    """fn 形式直接调用（测试注入）；cmd 形式经 runner 跑 CLI 子进程（任务 4 接线）。"""
    if "fn" in job:
        job["fn"]({"conn": conn, "home": home})
    elif runner:
        runner(job["cmd"])
    store.kv_set(conn, "daemon:last_job", job["name"])


def _real_now():
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
