# 服务内调度器（WP7 任务 1）：吸收 trading_core.daemon 的常驻循环。
# 协议原样复用：daemon.tick / JOBS_DEFAULT / 心跳文件 / 指令目录；
# daemon CLI（python -m trading_core daemon）保留为手动/兼容入口。
#
# 运维语义（两句）：
#   1. tick-first：启动即先跑一轮再等间隔（_loop 先 tick 后 wait），不空等第一个周期；
#   2. 启动即补跑当日到期作业；与 daemon CLI 共享 kv `daemon:state` 的 ran 标记，
#      同日作业不重复执行（服务与手动 daemon 先后跑同一天也只执行一次）。
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


def build_tick(home, jobs=None, now=None, conn=None):
    """组装 daemon.tick 的无参 callable（daemon 协议零改动，这里只管连接的生灭）。

    conn 三种形态：
      * callable —— 每轮调用新建连接，用完即关（服务默认 ``store.connect(store.db_path(home))``，
        库路径跟随应用 home 而不是环境变量，测试可注入任意工厂）；
      * 既有连接对象 —— 每轮复用、不关闭（测试注入）；
      * None —— 每轮按上一条默认口径新建并关闭。
    """
    def tick():
        if callable(conn):
            connection = conn()
            close_after = True
        elif conn is not None:
            connection = conn
            close_after = False
        else:
            connection = store.connect(store.db_path(str(home)))
            close_after = True
        try:
            return daemon.tick(connection, home=str(home), jobs=jobs, now=now)
        finally:
            if close_after:
                connection.close()
    return tick
