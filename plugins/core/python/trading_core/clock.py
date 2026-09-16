"""统一时钟口径（唯一实现，WP9 拆分自 daemon.py）：tick 的 now= 注入传不进 cmd 形式
作业的 CLI 子进程（调度链的真实结构是「父进程 tick → 子进程 CLI」），跨进程演练/测试靠
``DSH_FAKE_NOW`` 环境变量对齐时间线。

依赖纪律：本模块是**叶子**——不 import 其它 core 模块；``warn_fake_now`` 需要的告警写入
经函数内延迟导入（与 watchlist/strategies 对 daemon 的延迟导入同一手法），避免在 import
期建立任何 core 内部依赖。
"""
import datetime as dt
import os

FAKE_NOW_ENV = "DSH_FAKE_NOW"

_fake_now_alerted = False  # 每进程一次（warn_fake_now 的去重位）


def _real_now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_stamp(explicit=None):
    """解析当前时刻字符串：显式注入 > ``DSH_FAKE_NOW`` > 真实时间。

    **两个来源都校验格式**（显式注入同样可能写错——CLI ``--now`` 是人工输入）：
    非法格式如实抛 ValueError，绝不静默回落真实时间。写错的时间戳静默回落会让演练
    结论失真（宁可让演练当场炸掉，也不给出一份看起来正常的假报告）。
    """
    raw = str(explicit) if explicit is not None else os.environ.get(FAKE_NOW_ENV)
    if raw is None:
        return _real_now()
    try:
        dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        source = "--now" if explicit is not None else FAKE_NOW_ENV
        raise ValueError(f"{source} 需为 YYYY-MM-DD HH:MM:SS，收到 {raw!r}") from error
    return raw


def now_fn(explicit=None):
    """``now=`` 参数用的零参 callable：**采样一次**固定，避免跨分钟抖动。"""
    stamp = now_stamp(explicit)
    return lambda: stamp


def warn_fake_now(conn, home):
    """``DSH_FAKE_NOW`` 生效时告警一次（每进程一次；未设置/已告警/无 conn 均 no-op）。

    生产防误用：演练变量忘了清理会让整个调度时间线错乱（作业提前/滞后触发），
    因此必须让工作台告警列表可见，而不是只写在文档里。
    """
    global _fake_now_alerted
    raw = os.environ.get(FAKE_NOW_ENV)
    if not raw or _fake_now_alerted or conn is None:
        return False
    _fake_now_alerted = True
    from . import alerts  # 延迟导入：本模块保持叶子，不在 import 期依赖其它 core 模块
    alerts.emit(conn, home=str(home), level="warn", title="假时钟生效",
                detail=f"{FAKE_NOW_ENV}={raw}（演练/测试专用；用后 unset）"[:160])
    return True
