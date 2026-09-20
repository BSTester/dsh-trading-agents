# 服务内调度器（WP7 任务 1）：吸收 trading_core.daemon 的常驻循环。
# 协议原样复用：daemon.tick / JOBS_DEFAULT / 心跳文件 / 指令目录；
# daemon CLI（python -m trading_core daemon）保留为手动/兼容入口。
#
# 运维语义（两句）：
#   1. tick-first：启动即先跑一轮再等间隔（_loop 先 tick 后 wait），不空等第一个周期；
#   2. 启动即补跑当日到期作业；与 daemon CLI 共享 kv `daemon:state` 的 ran 标记，
#      同日作业不重复执行（服务与手动 daemon 先后跑同一天也只执行一次）。
import sqlite3
import sys
import threading
import traceback
from pathlib import Path

try:
    # venv 里 trading_core 已 pip install -e（与 compute._load_write_command 同一口径）。
    from trading_core import daemon, store
except ImportError:
    # 未安装 trading_core 的直跑环境：回退仓库路径（插件层不在 sys.path 时按既有做法补上）。
    _CORE_PYTHON = str(Path(__file__).resolve().parents[2] / "plugins" / "core" / "python")
    if _CORE_PYTHON not in sys.path:
        sys.path.insert(0, _CORE_PYTHON)
    from trading_core import daemon, store


class Scheduler:
    """固定间隔线程：每轮调用 tick()，异常记录不杀线程。"""

    def __init__(self, tick, interval=60.0):
        # interval<=0 直接拒绝（间隔语义失真比启动失败更危险）；正数照单全收，
        # 亚秒间隔也真实生效（重复 tick 的时长由配置者自担）。
        interval = float(interval)
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._tick = tick
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wp7-scheduler", daemon=True)
        self._thread.start()

    def _loop(self):
        # last_error 保留最近一次 tick 异常、成功不清除：偶发恢复不抹掉故障证据，
        # healthz 的 last_error 因此是「最近一次出错记录」而不是「上一轮是否出错」。
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                self.last_error = traceback.format_exc(limit=5)
            self._stop.wait(self._interval)

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def alive(self):
        return bool(self._thread and self._thread.is_alive())


def _record_last_error(conn, now, error):
    """段级异常落 kv ``daemon:last_error``（保留最近一次，成功不清除）。

    与 healthz 的 ``scheduler:{alive,last_error}`` 同一证据口径（300 字符截断）：
    healthz 读的是 Scheduler 线程属性，这里落库的是调度器可查询的同一事实。
    记账失败不得影响调度主流程。
    """
    try:
        stamp = (now or daemon._real_now)()
        store.kv_set(conn, "daemon:last_error",
                     {"at": stamp, "error": str(error)[:300]})
    except Exception:  # noqa: BLE001
        pass


def build_tick(home, jobs=None, now=None, conn=None):
    """组装 daemon 的作业链 + 指令轮询的无参 callable（这里只管连接的生灭与分段）。

    conn 三种形态：
      * callable —— 每轮调用新建连接，用完即关（服务默认 ``store.connect(store.db_path(home))``，
        库路径跟随应用 home 而不是环境变量，测试可注入任意工厂）；
      * 既有连接对象 —— 每轮复用、不关闭（测试注入）；
      * None —— 每轮按上一条默认口径新建并关闭。

    两段（WP9 任务 7）：**先作业链、后指令轮询**，共用同一连接。顺序有意义——
    auto_execute 作业在 tick 内写下的 execute_plan 指令，同轮即被取走执行。
    两段各自 try/except：一段失败不阻断另一段；失败落 kv ``daemon:last_error`` 后
    **继续向上抛**，让 Scheduler.last_error 与 /healthz 保持原有的故障可见性
    （吞掉异常会让 healthz 谎报无错）。

    注：``sqlite3.Connection`` 自身可调用（``conn()`` 是游标的旧别名），因此**先按
    isinstance 认连接对象、再判可调用**——否则注入真实连接会被误当工厂调用而崩。

    第三段（V3 Headless 通道，FR-GATEWAY-004）：headless 触发循环的 keep-alive。
    **与本文件其它两段不同，这一段失败只留痕、绝不向上抛**：headless 是分析通道，
    它坏掉不该把交易作业链的 tick 判成失败（那会让 /healthz 报错并让运维按交易事故处理）。
    ``v3_headless.scheduler_tick`` 自己吞掉触发条件异常，这里再兜一层 import/运行异常。
    """
    try:
        from server import v3_headless  # noqa: PLC0415 —— 惰性 import，避免装配期环
    except Exception:  # noqa: BLE001 —— 通道模块缺失不该影响交易调度
        v3_headless = None

    def tick():
        if conn is None:
            connection = store.connect(store.db_path(str(home)))
            close_after = True
        elif isinstance(conn, sqlite3.Connection):
            connection = conn
            close_after = False
        elif callable(conn):
            connection = conn()
            close_after = True
        else:
            connection = conn
            close_after = False
        state = None
        failures = []
        try:
            try:
                state = daemon.tick(connection, home=str(home), jobs=jobs, now=now)
            except Exception as error:  # noqa: BLE001 —— 一段失败不阻断另一段
                failures.append(("tick", error))
                _record_last_error(connection, now, error)
            try:
                daemon.poll_commands(connection, home=str(home))
            except Exception as error:  # noqa: BLE001
                failures.append(("poll", error))
                _record_last_error(connection, now, error)
            if v3_headless is not None:
                # 第三段：headless 触发循环 keep-alive（一行挂载）。
                # 只留痕、不进 failures —— 见 build_tick docstring 的第三段说明。
                try:
                    v3_headless.scheduler_tick(str(home))
                except Exception as error:  # noqa: BLE001
                    _record_last_error(connection, now, error)
        finally:
            if close_after:
                connection.close()
        if failures:
            raise RuntimeError("; ".join(f"{phase}: {error}" for phase, error in failures))
        return state
    return tick
