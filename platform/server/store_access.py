# WP6 补遗任务 B1：store 访问层（Python 移植）。规格 §八-7 / 补遗任务 B。
#
# 唯一事实来源：plugins/workbench/src/store.js（363 行）。逐行对应关系：
#   store.js:9       LIMIT                  -> LIMIT
#   store.js:13      ABANDONED_AFTER_MS     -> ABANDONED_AFTER_MS
#   store.js:15-16   WorkbenchError/Busy    -> WorkbenchError/WorkbenchBusyError
#   store.js:25-28   modeValue              -> mode_value()
#   store.js:30-38   atomicWrite            -> _atomic_write()
#   store.js:48-50   emptyState             -> _empty_state()
#   store.js:52-64   withRunStatus          -> with_run_status()
#   store.js:66-73   构造器里的四组路径      -> store_file/mode_file/observations_dir/lock_file
#   store.js:75-82   inFlight               -> _active_leases()
#   store.js:84-91   readMode               -> read_mode()
#   store.js:93-106  read                   -> read_store()
#   store.js:108-129 update                 -> _update()
#   store.js:131-135 event                  -> _event()
#   store.js:144-159 cancelRun              -> admin_cancel_run()
#   store.js:162-178 cancelStaleRuns        -> admin_cancel_stale()
#   store.js:187-203 pruneAbandonedRuns     -> admin_prune_runs()
#   store.js:205-224 snapshot               -> snapshot()
#   store.js:226-240 switchMode             -> switch_mode()
#   store.js:242-259 enterBrokerCall        -> 只取租约命名/在途判定（_active_leases）
#   store.js:329-336 pendingObservations    -> pending_observations()
#   scripts/workbench_admin.mjs:14-25       hoursArg/ageMinutes -> _hours_to_ms/admin_runs
#   plugins/workbench/src/endpoints.js:12-33 ENDPOINTS          -> endpoints()
# 本模块只用标准库；不写回任何 Node 侧尚未写入的键，错误语义（消息、类型）对齐 Node。
# JS 语义助手（真值、字段访问）统一来自 server/_js.py：store_access/summary/audit_chain 不再
# 各留一份，避免补遗 B 移植审查抓到的那种漂移（同一语义两处两种答案）。
#
# 有意差异（诚实边界，须与 Node 行为区分；规格 §八-7 与补遗任务 B 明文允许）：
#   1. 只读快照：snapshot() 不调用 flushObservations()（store.js:206）。trading-observations/
#      里的暂存观察不会被合并进 activity/broker，合并仍由 Harness 进程内的 Node Host 完成；
#      pending_observations 如实照抄文件系统里的待合并文件数，合并前的观察不计入快照。
#   2. trade_summary 由 summary.summarize(activity) 派生（B2 已接线），与 Node
#      store.js:217 `summarizeBrokerActivity(activity)` 同入参（过滤后倒序的 activity）
#      同输出；不写回磁盘，只读时计算。
#   3. in_flight 恒为 0：服务进程不派发券商调用（规格 §B「in_flight(空)」）。安全护栏不受
#      影响——switch_mode() 仍按 store.js:242-243 的租约文件命名做在途检查并据此拒绝切换。
#   4. started_at 只按 ISO-8601 解析（Node Date.parse 还接受 RFC 2822 等宽松格式）。run 的
#      started_at 一律由 Node 的 toISOString() 写入，实际不会出现其他格式。
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server import _js, summary

# store.js:9-13
LIMIT = 100
# 研究 run 超过这个时长仍是 running，就认为发起它的会话已中断（HOST 的派生状态阈值）。
ABANDONED_AFTER_MS = 2 * 60 * 60 * 1000

# store.js:222 原文（服务端不一致地改写这句话会让两个实现的快照出现假差异）。
NOTICE = ("交易动态来自 Harness 最近的富途工具响应，不是券商成交推送；"
          "下单、撤单及对话请在 Harness 中完成。")

ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS_JS = ROOT / "plugins" / "workbench" / "src" / "endpoints.js"

_ENDPOINTS_CACHE = None


class WorkbenchError(Exception):
    """store.js:15。业务错误：消息与 Node 侧逐字一致，由上层映射成 envelope。"""


class WorkbenchBusyError(WorkbenchError):
    """store.js:16。写锁被占用；Node 侧不导出，这里导出是为了让上层能区分重试。"""


def store_file(home):
    """store.js:69 `path.join(home, "trading-workbench.json")`。"""
    return Path(home) / "trading-workbench.json"


def mode_file(home):
    """store.js:70 `trading-account-mode`。"""
    return Path(home) / "trading-account-mode"


def observations_dir(home):
    """store.js:71 `trading-observations`。"""
    return Path(home) / "trading-observations"


def lock_file(home):
    """store.js:110 写锁文件名 `trading-workbench.lock`。"""
    return Path(home) / "trading-workbench.lock"


def mode_value(mode):
    """store.js:25-28 modeValue：只接受 sim/live，其余（含 undefined/None）抛错。"""
    if mode != "sim" and mode != "live":
        raise WorkbenchError("Invalid account mode; expected sim/live")
    return mode


def _now_ms():
    return int(time.time() * 1000)


def _iso_now(now_ms=None):
    """Node `new Date().toISOString()` 的等价形式（毫秒精度、Z 结尾）。"""
    moment = datetime.now(timezone.utc) if now_ms is None else datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _parse_ms(value):
    """`Date.parse(value ?? "")`：可解析返回毫秒（float），否则 None（NaN）。

    有意差异 4：只认 ISO-8601（含 Z/偏移），不认 RFC 2822。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000


def _hours_to_ms(hours):
    """workbench_admin.mjs:14-19 hoursArg：非有限或 <=0 一律退回默认 2h。"""
    if hours is None:
        return ABANDONED_AFTER_MS
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return ABANDONED_AFTER_MS
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return ABANDONED_AFTER_MS
    return int(value * 3600_000)


def _empty_state():
    """store.js:48-50。"""
    return {"version": 1, "runs": [], "reports": [], "previews": [], "activity": [], "broker": {}}


def with_run_status(run, now=None):
    """store.js:56-62 withRunStatus：running 且超过阈值 -> 派生 abandoned（不改历史）。"""
    if not isinstance(run, dict) or run.get("status") != "running":
        return run
    started = _parse_ms(run.get("started_at"))
    if started is None:
        return run
    current = _now_ms() if now is None else now
    if current - started < ABANDONED_AFTER_MS:
        return run
    copied = dict(run)
    copied["status"] = "abandoned"
    return copied


def _atomic_write(target, content):
    """store.js:30-38 atomicWrite：独占建临时文件（0600）+ rename，失败也不留残骸。"""
    target = Path(target)
    temporary = Path(f"{target}.{uuid.uuid4()}.tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _active_leases(home):
    """store.js:75-82 inFlight + 242-243 租约命名：`trading-call-*.active` 即持有者标记。"""
    try:
        names = os.listdir(home)
    except FileNotFoundError:
        return []
    return [name for name in names
            if name.startswith("trading-call-") and name.endswith(".active")]


def pending_observations(home):
    """store.js:329-336 pendingObservations：暂存箱里的 *.json 文件名列表。"""
    try:
        names = os.listdir(observations_dir(home))
    except FileNotFoundError:
        return []
    return [name for name in names if name.endswith(".json")]


def endpoints():
    """endpoints.js:12-33 的 20 端点清单；从 JS 文本正则提取，首次调用缓存（单一事实来源）。"""
    global _ENDPOINTS_CACHE
    if _ENDPOINTS_CACHE is None:
        text = ENDPOINTS_JS.read_text(encoding="utf-8")
        block = re.search(r"export const ENDPOINTS\s*=\s*\[(.*?)\]", text, re.S)
        if block is None:
            raise WorkbenchError("Cannot read endpoints manifest: plugins/workbench/src/endpoints.js")
        _ENDPOINTS_CACHE = re.findall(r'"([^"]+)"', block.group(1))
    return list(_ENDPOINTS_CACHE)


def read_mode(home):
    """store.js:84-91 readMode：文件缺失=sim，内容非法抛错（不静默回退）。"""
    try:
        content = mode_file(home).read_text(encoding="utf-8")
    except FileNotFoundError:
        return "sim"
    return mode_value(content.strip())


def read_store(home):
    """store.js:93-106 read：缺失=空结构；语法/形状损坏一律抛错，不退回伪造空成功。"""
    try:
        text = store_file(home).read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    state = json.loads(text)  # JSON.parse 等价：SyntaxError/json.JSONDecodeError 原样上抛
    if not isinstance(state, dict):
        # Node 对 JSON null 取属性抛 TypeError、对数组/标量是 version!==1 -> WorkbenchError；
        # 这里统一按「损坏」抛 WorkbenchError：任何非对象根都不允许被当成空库。
        raise WorkbenchError("Invalid workbench state; restore a valid backup")
    broker = state.get("broker")
    if (state.get("version") != 1
            or not all(isinstance(state.get(key), list) for key in ("runs", "reports", "previews", "activity"))
            # `!state.broker || typeof state.broker !== "object"`：空对象 {} 合法（JS 真值）
            or not _js.truthy(broker) or not isinstance(broker, (dict, list))):
        raise WorkbenchError("Invalid workbench state; restore a valid backup")
    return state


def _update(home, fn):
    """store.js:108-129 update：独占写锁 -> 读 -> 改 -> 截断到 LIMIT -> 原子写 -> 放锁。

    没有 after_commit 钩子：调用方全都只要返回值（Node 侧 `update()` 同样直接返回 result），
    加一个无人使用的钩子只会成为下一处漂移点（补遗 B 审查次要项）。
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    lock = lock_file(home)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # openSync(lock, "wx", 0o600)
    except FileExistsError as error:
        raise WorkbenchBusyError("Workbench is busy; retry after the other writer finishes") from error
    try:
        state = read_store(home)
        result = fn(state)
        for key in ("runs", "reports", "previews", "activity"):
            state[key] = state[key][-LIMIT:]
        # JSON.stringify 等价：紧凑分隔符 + 不转义非 ASCII，尽量与 Node 写出的字节同形。
        _atomic_write(store_file(home), json.dumps(state, ensure_ascii=False, separators=(",", ":")))
        return result
    finally:
        os.close(fd)
        lock.unlink()


def _event(state, value):
    """store.js:131-135 event：{id, at, ...value}（value 可覆盖 id/at）。"""
    event = {"id": str(uuid.uuid4()), "at": _iso_now()}
    event.update(value)
    state["activity"].append(event)
    return event


def _by_mode_desc(rows, mode):
    """store.js:209/212-214 `.filter(row => row.mode === mode).reverse()`。"""
    return [row for row in reversed(rows) if _js.field(row, "mode") == mode]


def snapshot(home):
    """store.js:205-224 snapshot（只读；有意差异 1-3 见文件头）。"""
    mode = read_mode(home)
    state = read_store(home)
    activity = _by_mode_desc(state["activity"], mode)
    broker_state = state.get("broker")
    return {
        "version": 1,
        "mode": mode,
        "generated_at": _iso_now(),
        "runs": [with_run_status(row) for row in _by_mode_desc(state["runs"], mode)],
        "reports": _by_mode_desc(state["reports"], mode),
        "previews": _by_mode_desc(state["previews"], mode),
        "activity": activity,
        # 派生视图：把工具调用归纳成交易事实（与 store.js:216-217 同位置同入参）。
        # 入参 activity 已按模式过滤并倒序（store.js:209），与 Node 一致；不写回磁盘。
        "trade_summary": summary.summarize(activity),
        # `state.broker[mode] ?? null`
        "broker": broker_state.get(mode) if isinstance(broker_state, dict) else None,
        # 有意差异 3：服务进程不派发券商调用，恒为 0（在途护栏在 switch_mode 内按租约文件判）。
        "in_flight": 0,
        # 有意差异 1：只报暂存箱里待合并的文件数，不在读路径上合并（store.js:220 是同名计数）。
        "pending_observations": len(pending_observations(home)),
        # 服务进程没有记录观察的路径，与 Node 进程启动时的初值一致。
        "recording_error": None,
        "notice": NOTICE,
        "endpoints": endpoints(),
    }


def switch_mode(home, mode=None, expected_mode=None, confirmation=None):
    """store.js:226-240 switchMode：校验顺序逐条一致，返回字段名一致。"""
    mode_value(mode)
    mode_value(expected_mode)

    def apply(state):
        current = read_mode(home)
        if current != expected_mode:
            raise WorkbenchError("Account mode changed; refresh before switching")
        if _active_leases(home):
            raise WorkbenchError("有账户调用正在进行，请结束后切换")
        if mode == "live" and current != "live" and confirmation != "确认实盘":
            raise WorkbenchError("请输入「确认实盘」；切换模式不等于授权下单")
        _atomic_write(mode_file(home), f"{mode}\n")
        if mode != current:
            _event(state, {"kind": "mode_changed", "mode": mode, "previous_mode": current})
        return {"mode": mode, "previous_mode": current, "order_authorized": False}

    return _update(home, apply)


def admin_status(home):
    """服务侧新 API（Node 侧没有对应的 store 方法）：形状照 workbench_admin.mjs:28-31 的
    status 打印——数据文件路径与四张表的计数，让面板不必解析 CLI 文本。"""
    state = read_store(home)
    return {
        "file": str(store_file(home)),
        "runs": len(state["runs"]),
        "reports": len(state["reports"]),
        "previews": len(state["previews"]),
        "activity": len(state["activity"]),
    }


def admin_runs(home, now=None):
    """服务侧新 API（Node 侧没有对应的 store 方法）：形状照 workbench_admin.mjs:21-25/33-39
    的 runs 打印——每条 run 的状态与年龄（started_at 解析失败 -> None，对应 "年龄未知"）。"""
    state = read_store(home)
    current = _now_ms() if now is None else now
    rows = []
    for run in state["runs"]:
        started = _parse_ms(_js.field(run, "started_at"))
        rows.append({
            "id": _js.field(run, "id"),
            "status": _js.field(run, "status"),
            "ticker": _js.field(run, "ticker"),
            "mode": _js.field(run, "mode"),
            "age_minutes": None if started is None else (current - started) / 60_000,
        })
    return rows


def admin_cancel_run(home, run_id):
    """store.js:144-159 cancelRun：标记 cancelled 并保留记录，返回快照副本。"""
    settled = None

    def apply(state):
        nonlocal settled
        run = next((row for row in state["runs"] if _js.field(row, "id") == run_id), None)
        if run is None:
            raise WorkbenchError(f"Unknown run: {run_id}")
        if _js.field(run, "status") != "running":
            raise WorkbenchError(f"Run is already settled: {_js.field(run, 'status')}")
        run["status"] = "cancelled"
        run["settled_at"] = _iso_now()
        _event(state, {"kind": "research_cancelled", "mode": _js.field(run, "mode"),
                       "ticker": _js.field(run, "ticker"), "run_id": _js.field(run, "id"),
                       "session_id": _js.field(run, "session_id")})
        settled = dict(run)

    _update(home, apply)
    return settled


def admin_cancel_stale(home, hours=2, now=None):
    """store.js:162-178 cancelStaleRuns：只取消超时仍 running 的 run，返回 id 列表。"""
    older_than_ms = _hours_to_ms(hours)
    current = _now_ms() if now is None else now
    cancelled = []

    def apply(state):
        for run in state["runs"]:
            if _js.field(run, "status") != "running":
                continue
            started = _parse_ms(_js.field(run, "started_at"))
            if started is not None and current - started >= older_than_ms:
                run["status"] = "cancelled"
                run["settled_at"] = _iso_now()
                _event(state, {"kind": "research_cancelled", "mode": _js.field(run, "mode"),
                               "ticker": _js.field(run, "ticker"), "run_id": _js.field(run, "id"),
                               "session_id": _js.field(run, "session_id")})
                cancelled.append(_js.field(run, "id"))

    _update(home, apply)
    return cancelled


def admin_prune_runs(home, hours=2, now=None):
    """store.js:187-203 pruneAbandonedRuns：只删超时、running、且无研报的孤儿 run。"""
    older_than_ms = _hours_to_ms(hours)
    current = _now_ms() if now is None else now
    removed = []

    def apply(state):
        # 报告主键 id 就是 run id（publishResearch 用 run.id），另有 row.run_id 兼容；filter(Boolean)。
        # JS 的 Set 对对象按键值引用比较，永远配不上 run.id（另一个对象），因此只收标量即可等价，
        # 同时避免 JSON 里出现数组/对象键时的不可哈希崩溃。
        published = set()
        for row in state["reports"]:
            for key in ("id", "run_id"):
                value = _js.field(row, key)
                if _js.truthy(value) and isinstance(value, (str, int, float)):
                    published.add(value)
        kept = []
        for run in state["runs"]:
            if _js.field(run, "status") != "running":
                kept.append(run)
                continue
            started = _parse_ms(_js.field(run, "started_at"))
            if started is None or current - started < older_than_ms:
                kept.append(run)
                continue
            if _js.field(run, "id") in published:
                kept.append(run)
                continue
            removed.append(_js.field(run, "id"))
        state["runs"] = kept

    _update(home, apply)
    return removed


__all__ = [
    "ABANDONED_AFTER_MS", "LIMIT", "NOTICE", "WorkbenchBusyError", "WorkbenchError",
    "admin_cancel_run", "admin_cancel_stale", "admin_prune_runs", "admin_runs", "admin_status",
    "endpoints", "lock_file", "mode_file", "mode_value", "observations_dir",
    "pending_observations", "read_mode", "read_store", "snapshot", "store_file",
    "switch_mode", "with_run_status",
]
