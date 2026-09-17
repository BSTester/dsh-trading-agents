"""值班研究员（L3）队列业务层（规格 §十）。

分工（与 ``store`` / ``sentiment`` 同一分层纪律）：

  * ``store`` —— 纯状态机（表、入队/领取/完成/回收的迁移规则，零告警零网络）；
  * 本模块 —— 需要 ``home`` 与告警的那部分：任务载荷组装（``enqueue``）与超时回收的
    warn 告警（``reclaim``）。二者都不调用任何 LLM、不开子进程。

**队列即攻击面**（规格 §十一.13）：``kind`` 与载荷键都是白名单（``store.TASK_KINDS`` /
``store.TASK_PAYLOAD_KEYS``），入队侧当场拒绝自由文本——队列里永远只有结构化引用，
「把一句话塞进队列让值班研究员去执行」这条路在落库前就被堵住。

告警归属：``reclaim`` 的「研究任务失败」**不登记**进 ``pipeline._ALERT_STATUS``——它描述的
是某条任务的执行结局，不是某个作业阶段的没跑完；流程页的阶段归因不该被它改写，任务明细
由队列列表端点与告警列表各自呈现（状态说阶段，明细说任务）。
"""
from . import alerts, store


def reclaim(conn, home, now, timeout_minutes=store.TASK_TIMEOUT_MINUTES):
    """回收超时任务；升级为 failed 的逐个发 warn 告警。返回状态机结果。

    告警逐条发（不是汇总一条）：任务是独立的研究单元，汇总会让「哪条失败了、重试过几次」
    淹没在摘要里——而这两个数字正是运维判断执行体是否健康所必需的。
    """
    result = store.reclaim_tasks(conn, now, timeout_minutes=timeout_minutes)
    for task in result["failed"]:
        alerts.emit(
            conn, home=str(home), level="warn", title="研究任务失败",
            detail=(f"task={task['task_id']} kind={task['kind']} market={task['market']} "
                    f"as_of={task['as_of']} attempts={task['attempts']} "
                    f"err={task.get('err') or ''}")[:300])
    return result
