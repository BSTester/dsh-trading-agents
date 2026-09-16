# WP6 补遗任务 B1：store 访问层（Python 移植）。规格 §八-7 / 补遗任务 B。
#
# 移植基准：plugins/workbench/src/store.js（WP7 面板退役后的服务锚版本——确认三方法、
# CONFIRM_TTL_MS/CONFIRM_OPERATIONS 等已随 legacy 面板在 Node 侧删除，确认逻辑现为本模块
# **服务自有**实现，不再有 Node 侧对应物）。移植时的逐行对应关系（行号为退役前 main）：
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
#   scripts/workbench_admin.mjs            hoursArg/ageMinutes -> _hours_to_ms/admin_runs
# 服务自有（Node 侧无对应物；WP7 起确认与端点清单只在服务侧）：
#   CONFIRM_TTL_MS / CONFIRM_OPERATIONS / order_operation() / describe_order_args() /
#   request_confirmation() / confirmation_view() / decide_confirmation() /
#   _record_confirmation_event() / endpoints()（22 项基础清单冻结自已删的
#   plugins/workbench/src/endpoints.js + WP7_ENDPOINTS + WP8 FUTU_ENDPOINTS）
# 本模块只用标准库；不写回任何 Node 侧尚未写入的键，错误语义（消息、类型）对齐 Node。
# JS 语义助手（真值、字段访问）统一来自 server/_js.py：store_access/summary/audit_chain 不再
# 各留一份，避免补遗 B 移植审查抓到的那种漂移（同一语义两处两种答案）。
#
# ---------------------------------------------------------------------------
# 业务确认的**进程内边界**（2026-09-15 main 修订；WP7 面板退役后即服务自有语义）
# ---------------------------------------------------------------------------
# 确认是「此刻等人回答」的瞬时状态，刻意不落盘——进程重启后无人回答，落盘会让陈旧请求
# 复活；持久化的只有 activity 里的事件留痕。Python 侧沿用这一语义：待确认表是本模块的
# **进程内模块级表**（`_PENDING`，按 home 归一化路径分槽），进程重启即消失。
# WP7 面板退役（用户决策 2026-09-16）后，确认只有一个发起方与一个作答方，都在本服务进程：
#
#   * 发起：服务自身处理函数（工作台 `trade_*` 工具的交易闸门，本文件 `request_confirmation`）；
#   * 作答：独立 Web 确认卡片经 `confirmation`/`confirm-decide` 两个端点（同进程直读内存表）；
#   * Node 侧（Harness 进程）已无确认实现——policy 对 futu 写一律拒绝并指引工作台，
#     因此历史上「两进程各持一份待确认表、互不可见」的跨进程限制不复存在。
#
# 仍须遵守的边界：待确认表是**本进程内存态**，不是持久总线；服务重启即清空（超时语义
# 等价于 fail-closed，TTL 120s）。
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
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server import _js, summary

# store.js:9-13
LIMIT = 100
# 研究 run 超过这个时长仍是 running，就认为发起它的会话已中断（HOST 的派生状态阈值）。
ABANDONED_AFTER_MS = 2 * 60 * 60 * 1000

# store.js:17-21 实盘业务确认的存活时长；超时按**拒绝**处理（fail-closed）。
CONFIRM_TTL_MS = 120_000

# store.js:23-29 需要业务确认的实盘操作类型（按工具名后缀判定）。撤单也确认：
# 确认回答的是「这笔业务参数对不对」，撤错单同样是业务错误。
CONFIRM_OPERATIONS = {"input": "下单", "modify": "改单", "cancel": "撤单"}

# store.js:31 从工具名取出操作类型：`mcp__futu__trading_input_order` → `input`。
_ORDER_OPERATION = re.compile(r"(?:^|_)(input|modify|cancel)_order$")

# store.js:37-38 方向：1=Buy 2=Sell（工具 schema 明文）。JS 对象键一律是字符串，
# 因此查表统一走 _js.js_key_string（`1` 与 `"1"` 命中同一项，与 JS 的属性访问一致）。
ORDER_SIDE = {"1": "买入", "2": "卖出"}
# store.js:39-45 市场代码 → 中文提示。取值来自 `sim_trade_account_list` 的**实测返回**
# （港股 market=1、A股 market=3、美股 market=100），不是从文档猜的；未列出的不猜。
MARKET_HINT = {"1": "港股", "3": "A股", "100": "美股"}

# store.js:222 原文（服务端不一致地改写这句话会让两个实现的快照出现假差异）。
NOTICE = ("交易动态来自 Harness 最近的富途工具响应，不是券商成交推送；"
          "下单、撤单及对话请在 Harness 中完成。")


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
    """workbench_admin.mjs 的 hoursArg：非有限或 <=0 一律退回默认 2h。"""
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


def order_operation(tool):
    """store.js:31-35 orderOperation：从工具名取出 `input|modify|cancel`，否则 None。"""
    match = _ORDER_OPERATION.search(_js.stringify(tool))  # String(tool ?? "")
    return match.group(1) if match else None


def describe_order_args(tool, args=None):
    """store.js:47-77 describeOrderArgs：把券商写操作的工具入参渲染成中文订单摘要。

    未知字段一律原样列出、未知枚举附上原始代码——摘要的作用是让人核对，不是替人解释。
    值一律经 ``_js.template``（JS ``String(v)``）渲染，与 Node 写出的文字逐字一致。

    有意差异：JS 的 ``Object.entries(args ?? {})`` 对非对象入参（数组/字符串）也能枚举，
    这里把非 dict 的 ``args`` 收敛成 ``{}``——服务的调用方只会传 JSON 对象（工具入参），
    该分支不可达；``raw`` 因此也只在对象入参时保留原值。
    """
    source = args if isinstance(args, dict) else {}
    normalized = {}
    for key, value in _js.ordered_items(source):  # Object.entries 的枚举序
        normalized[str(key).lower()] = value
    fields = []

    def push(label, value):
        # store.js:59：`value === undefined || value === null || value === ""` 一律不显示
        # （0/False 不在其中：数量 0 必须显示出来让人核对）。
        if value is _js.UNDEFINED or value is None or value == "":
            return
        fields.append({"label": label, "value": _js.template(value)})

    def raw(key):
        """store.js:56 ``raw(key)``：区分「字段缺失（undefined）」与「显式为 null」。"""
        return _js.field_or_undefined(normalized, key)

    def enumerated(value, table, known, unknown):
        """三元的共用形态：命中枚举给中文，未命中原样显示并提示核对。"""
        label = table.get(_js.js_key_string(value))
        shown = _js.template(value)
        return known.format(label=label, shown=shown) if _js.truthy(label) \
            else unknown.format(shown=shown)

    market = raw("market")
    market_hint = _js.UNDEFINED if market is _js.UNDEFINED else enumerated(
        market, MARKET_HINT, "{shown}（{label}）", "{shown}（未识别的市场代码，请核对）")
    # `order_side ?? trd_side`：只有 undefined/null 回退；回退结果仍是 undefined 才不显示。
    side = raw("order_side")
    if side is _js.UNDEFINED or side is None:
        side = raw("trd_side")
    side_hint = _js.UNDEFINED if side is _js.UNDEFINED else enumerated(
        side, ORDER_SIDE, "{label}（order_side={shown}）", "未识别（order_side={shown}，请核对）")

    push("账户", raw("acc_id"))
    push("市场", market_hint)
    push("标的", raw("symbol"))
    push("方向", side_hint)
    push("数量", _js.js_nullish(raw("qty"), raw("quantity")))
    push("价格", raw("price"))
    push("订单类型", raw("order_type"))
    push("订单号", raw("order_id"))
    push("有效期", _js.js_nullish(raw("time_in_force"), raw("order_trade_time_type")))
    # WP8 任务 6：官方 place/modify 扩展参数（人批准的就是这些参数，逐项列出来可核对）。
    # 市价类没有下单价时列闸门实际使用的风控基准价（risk_price，本地收盘），口径透明。
    push("风控基准价", raw("risk_price"))
    push("触发价", raw("aux_price"))
    push("交易时段", raw("session"))
    push("手数类型", raw("lot_type"))
    push("订单类别", raw("order_class"))
    push("多腿信息", raw("multi_leg_info"))
    push("备注", _js.js_nullish(raw("text"), raw("remark")))
    return {"tool": tool, "fields": fields, "raw": source}


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


# ---------------------------------------------------------------------------
# 端点清单：WP7 起服务自有（legacy 面板退役）
# ---------------------------------------------------------------------------
# WP7 面板退役（用户决策 2026-09-16）：legacy 面板源 `plugins/workbench/src/endpoints.js`
# 已随面板（client.js + Connection RPC）一并删除，端点清单改为本模块自有常量——
# `_BASE_ENDPOINTS`（22 项，按已删文件的原序冻结）+ `WP7_ENDPOINTS`（7 项服务自有端点），
# 合成 `endpoints()` 返回的 29 项，顺序与退役前完全一致（服务与前端零行为变化）。
#
# `_BASE_ENDPOINTS` 的来源是已删 endpoints.js 的数组字面量（含尾部两个业务确认端点）：
# 业务确认现在只由服务进程发起与作答（Web 确认卡片），这两个端点是唯一确认通道。
_BASE_ENDPOINTS = (
    "snapshot",
    "switch-mode",
    "series",
    "equity",
    "positions",
    "correlation",
    "sensitivity",
    "risk",
    "trades",
    "events",
    "factors",
    "ic",
    "audit",
    "sources",
    "instrument",
    "quality",
    "plan",
    "plan-execute",
    "schedule",
    "reconcile",
    "confirmation",
    "confirm-decide",
)

# WP7 起服务自有的端点。任务 2：factors-history；任务 3：受约束交易工具（写三个走交易闸门
# + Web 确认卡片，读三个 mode 约束直通 broker——均不进 TTL/形状表，与 plan-execute 同类）。
WP7_ENDPOINTS = ("factors-history", "trade_place", "trade_modify", "trade_cancel",
                 "account_positions", "account_orders", "account_funds")

# WP8 富途实时直通端点（8 个：skills 需要而本地无缓存的数据，由服务端实时经富途获取）。
# 取数在 server/futu_data.py（FUTU_TOOLS 与本表键集锁定比对）；全部实时不进缓存，
# 不进 CACHE_TTL_MS/ENDPOINT_SHAPE（与 WP7 交易/账户端点同一增量模式）。
FUTU_ENDPOINTS = ("rt_quote", "rt_order_book", "capital_flow", "capital_flow_history",
                  "capital_distribution", "option_expiration", "option_chain", "option_screen")

# WP8 任务 2：OpenAPI 行情接入新增端点（9 个；取数与通道路由同在 server/futu_data.py，
# futu_channel=openapi 时走 REST 后端）。实时四类（market_snapshot/cur_kline/rt_data/
# rt_ticker）不进缓存；基本四类 + quote_history_kline_v2 进 CACHE_TTL_MS/ENDPOINT_SHAPE
# 并在 value 层缓存（futu_data.CACHED_FUTU_ENDPOINTS）。
WP8_MARKET_ENDPOINTS = ("market_snapshot", "cur_kline", "rt_data", "rt_ticker",
                        "info_basicinfo", "info_trading_days", "info_search",
                        "info_market_state", "quote_history_kline_v2")

# WP8 任务 3：OpenAPI 交易只读端点（6 个；路由与闸门在 server/trading.py，
# futu_channel=openapi 且已配置凭据时走 REST）。全部实时直通：不进 CACHE_TTL_MS /
# ENDPOINT_SHAPE（与 trade_*/account_* 同类，TTL 0），受模式约束（载荷 mode 或缺省读
# 模式文件）。名单与 trading.OPENAPI_TRADE_ENDPOINTS 逐项同序（锁定测试比对）。
WP8_TRADE_ENDPOINTS = ("trade_max_qty", "orders_open", "orders_history",
                       "orders_detail", "deals_today", "deals_history")

# WP8 任务 6：推送订阅管理面（3 个；路由在 server/futu_push.py 的运行时门面，
# app.create_handler 注入 push 运行时）。push_status 是读（TTL 0，与 /healthz 的 push
# 同形）；push_subscribe/push_unsubscribe **只改本地连接订阅意图**（非交易：不改模式、
# 不过风控、不产生订单），推送未启用时如实返回 trading/push-unavailable。
# 全部实时直通：不进 CACHE_TTL_MS / ENDPOINT_SHAPE。名单与 mcp_tools 的 3 个工具逐项锁定。
WP8_PUSH_ENDPOINTS = ("push_status", "push_subscribe", "push_unsubscribe")

# WP8 任务 7：设置页端点（读写在 server/settings_api.py——openapi_config
# 空载荷=读状态、带载荷=保存（写私钥 0600 + 写凭据 JSON + 联动 futu_channel），
# openapi_test 用已保存凭据发一次真实 trading-days GET；GET 读走 app.py 专用路由，
# POST 走 handle 白名单分支）。WP8 增补 openapi_oauth：OAuth 2.1+PKCE 授权流程
# 管理面（start/status/cancel，生命周期在 server/oauth_flow.py，与 AppKey 模式并列）。
# **有意排除在 MCP 工具面之外**（mcp_tools.MCP_EXCLUDED_ENDPOINTS）：凭据管理与
# 授权是人工 Web 动作，绝不做成模型工具（与 confirm-decide 同类）。
# 实时直通：不进 CACHE_TTL_MS/ENDPOINT_SHAPE。
WP8_SETTINGS_ENDPOINTS = ("openapi_config", "openapi_test", "openapi_oauth")

# WP10 端点（2 个）。
#   任务 1 pipeline：流程页只读聚合——每市场「今日闭环跑到哪一步」的阶段状态
#   + 全局阶段 + auto_pipeline 配置摘要，取数在 trading_core pipeline.py
#   （`snapshot-pipeline` 子命令，经 app.py 的 plan/schedule/reconcile 同一 core 桥）。
#   任务 2 auto_pipeline：自动流水线设置读/写（组装在 settings_api，校验复用
#   trading_core.autopipeline.apply_overlay）。**不进 MCP 工具面**
#   （mcp_tools.MCP_EXCLUDED_ENDPOINTS）——模型不得自拨自动执行开关。
# 缓存口径不同：pipeline 进 CACHE_TTL_MS（30s）与 ENDPOINT_SHAPE；auto_pipeline
# **有意两张表都不进**——它是读/写端点，读侧必须立刻反映刚写入的配置（与
# openapi_config 同口径：凭据/设置的读不能吃缓存）。
WP10_ENDPOINTS = ("pipeline", "auto_pipeline")

# WP11 端点（1 个）。任务 3 sentiment-history：情绪快照历史/最近一日采集摘要的只读查询
# （取数在 trading_core cli 的 `sentiment-history`，经 app.py 的 factors-history 同一 core 桥）；
# 进 CACHE_TTL_MS（5m）与 ENDPOINT_SHAPE，且进 MCP 工具面（只读类，模型可查采集进度）。
WP11_ENDPOINTS = ("sentiment-history",)

# WP12 任务 4：富途数据面端点（16 个；取数与通道路由在 server/futu_data.py，只在
# openapi 通道有实现——mcp 通道无登记上游，如实拒绝）。构成为：
#   直通 14 = 经济日历 2 + 所属板块 + 复权因子 + 板块 2 + 筛选 2 + IPO + 做空 2 + 自选 3；
#   聚合 2 = f10_detail（26 section）+ derivative_detail（4 section）。
# 其中 3 个是**有意的 HTTP-only**（不进 MCP 工具面，见 mcp_tools.MCP_EXCLUDED_ENDPOINTS）：
# warrant_screen / modify_user_security / info_rehab。缓存口径：除写类
# modify_user_security 外均进 CACHE_TTL_MS（6h/30m/5m/1h 四档，依据见 caches.py）；
# 形状表只登记 4 项官方文档明示包装键（见 caches.ENDPOINT_SHAPE 的 WP12 注释）。
WP12_ENDPOINTS = ("economic_calendar_hot", "economic_calendar_search", "info_owner_plate",
                  "info_rehab", "plate_list", "plate_stock", "stock_screen",
                  "warrant_screen", "ipo_list", "short_daily_volume", "short_interest",
                  "watchlist_list", "watchlist_groups", "modify_user_security",
                  "f10_detail", "derivative_detail")


def endpoints():
    """服务端点清单（77 项）：22 项 legacy 基础清单 + 7 项 WP7 + 8 项 WP8 富途直通
    + 9 项 WP8 OpenAPI 行情 + 6 项 WP8 OpenAPI 交易只读 + 3 项 WP8 推送订阅管理
    + 3 项 WP8 设置页（openapi_config/openapi_test/openapi_oauth）+ 2 项 WP10
    （流程页 pipeline + 自动流水线设置 auto_pipeline）+ 1 项 WP11
    （情绪快照 sentiment-history）+ 16 项 WP12 数据面。

    WP7 起清单为**服务自有**（legacy 面板已退役，原「解析 endpoints.js 文本」的实现删除）：
    基础 22 项冻结在 ``_BASE_ENDPOINTS``（与已删 JS 文件的原序一致），WP7/WP8/WP10/WP11/WP12
    增量按交付顺序登记在各自常量里；锁定测试把 77 项整体钉死。
    """
    return list(_BASE_ENDPOINTS) + list(WP7_ENDPOINTS) + list(FUTU_ENDPOINTS) \
        + list(WP8_MARKET_ENDPOINTS) + list(WP8_TRADE_ENDPOINTS) \
        + list(WP8_PUSH_ENDPOINTS) + list(WP8_SETTINGS_ENDPOINTS) + list(WP10_ENDPOINTS) \
        + list(WP11_ENDPOINTS) + list(WP12_ENDPOINTS)


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
        # 待确认的业务动作也进快照（Web 确认卡片轮询 `confirmation` 端点读它，
        # 快照轮询太慢，这里只是同源冗余）。确认是本服务进程的内存态，见文件头。
        "confirmation": confirmation_view(home),
        # 有意差异 1：只报暂存箱里待合并的文件数，不在读路径上合并（store.js:220 是同名计数）。
        "pending_observations": len(pending_observations(home)),
        # 服务进程没有记录观察的路径，与 Node 进程启动时的初值一致。
        "recording_error": None,
        "notice": NOTICE,
        "endpoints": endpoints(),
    }


# ---------------------------------------------------------------------------
# 实盘业务确认（服务自有；Node 侧同名实现已随 legacy 面板退役）
# ---------------------------------------------------------------------------
# 待确认表：**进程内内存态**，按归一化后的 home 路径分槽。
# 语义：一次只允许一笔、TTL 超时按拒绝、signal 取消按拒绝、只有 decide 能批准。
# 进程内边界见文件头，这里再强调一次：**本表不跨进程、不落盘**。
_PENDING = {}
_PENDING_LOCK = threading.Lock()

# 等待判决时的轮询间隔（秒）：用于在阻塞等待中观察 signal 取消，同时保持对 decide 的即时响应
# （decide 通过 threading.Event.set() 唤醒，不依赖该间隔）。
_SIGNAL_POLL_S = 0.02


def _home_key(home):
    """待确认表的分槽键：同一目录的不同写法（Path/str、相对/绝对）必须命中同一槽。"""
    return os.path.realpath(str(home))


def _signal_aborted(signal):
    """``signal`` 的取消判定。Python 没有 AbortSignal，这里接受两种既有习惯的形态：

    * ``threading.Event``（推荐，``is_set()``）；
    * 任何带真值 ``aborted`` 属性的对象（JS AbortSignal 的直译）。
    """
    if signal is None:
        return False
    if hasattr(signal, "aborted"):
        return bool(signal.aborted)
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _settle(request, decision, reason):
    """兑现一次请求：记录结论并唤醒等待中的 ``request_confirmation``（幂等）。"""
    if request["_settled"]:
        return
    request["_settled"] = True
    request["_outcome"] = {"decision": decision, "reason": reason, "id": request["id"]}
    request["_done"].set()


def request_confirmation(home, tool=None, mode=None, args=None, session_id=None,
                         ttl_ms=CONFIRM_TTL_MS, signal=None):
    """发起一次**业务确认**并阻塞等人作答（服务自有实现；Node 侧同名方法已随面板退役）。

    与 DSH 的 approval 系统完全无关：权限确认回答的是「这个动作准不准做」，由会话的
    approval policy 裁决；full-access（policy="never"）下 ``approval.decide()`` 会直接返回
    rejected，连问都不问。「这笔业务参数对不对」是交易动作的固有环节，不该因为系统被设成
    免打扰就静默拒绝。因此写操作在这里无条件等人确认，调用方据结果返回 allow/deny，
    **永不返回 {kind:"ask"}** —— 权限系统无从介入。

    回答只能来自 ``decide_confirmation``（服务面由 ``confirm-decide`` 路由暴露）；工具参数
    无法自证已确认，模型不能自己批自己。

    ``ttl_ms`` 可注入（短 TTL 用于测试），``signal`` 见 ``_signal_aborted``。
    返回 ``{decision: "approved"|"rejected", reason: str, id: str|None}``；超时/取消一律
    rejected（fail-closed）。
    """
    mode_value(mode)
    if mode != "live":
        raise WorkbenchError("只有实盘写操作需要业务确认")
    if not isinstance(tool, str) or not tool.strip():
        raise WorkbenchError("Invalid tool name")
    key = _home_key(home)
    with _PENDING_LOCK:
        # 一次只允许一笔：两笔并发时「我看到的是哪一笔」会变模糊，宁可让后来的重试。
        if _PENDING.get(key) is not None:
            return {"decision": "rejected", "id": None,
                    "reason": "已有一笔待确认的实盘操作，请先在工作台处理它再重试"}
        at_ms = _now_ms()
        request = {
            "id": str(uuid.uuid4()),
            "at": _iso_now(at_ms),
            "expires_at": _iso_now(at_ms + ttl_ms),
            "mode": mode,
            "tool": tool,
            # `session_id ?? "unknown"`：只有 None 兜底，空串原样保留。
            "session_id": "unknown" if session_id is None else session_id,
            "operation": CONFIRM_OPERATIONS.get(order_operation(tool), "实盘写操作"),
            "summary": describe_order_args(tool, args),
            "status": "pending",
            "decided_at": None,
            "decided_by": None,
            # 内部字段：confirmation_view 只挑显式列出的键，绝不把 settle 句柄送出去。
            "_done": threading.Event(),
            "_settled": False,
            "_outcome": None,
        }
        _PENDING[key] = request
        # 留痕放在锁内、发布之后立刻做（**有意与 JS 的行序对齐而非字面序**）：Node 是单线程，
        # `this.pendingConfirmation = request` 与紧随其后的 recordConfirmationEvent 之间没有
        # 别的执行流能插进来；Python 服务是多线程的，若把这条写盘放到锁外，decide 可能先抢到
        # 数据文件的写锁，使「请求」事件落到「裁决」之后、甚至被 WorkbenchBusyError 静默丢掉
        # （recordConfirmationEvent 是尽力而为的）。放在锁内可保证「请求」先落盘、
        # 「裁决/取消/超时」必然在其后。写盘本身仍尽力而为，失败不影响确认结论。
        _record_confirmation_event(home, "confirmation_requested", request)

    deadline = time.monotonic() + ttl_ms / 1000

    def abort():
        """会话中断 → 清待确认、记 cancelled、按拒绝兑现（移植自退役前 Node 实现）。"""
        if request["_settled"] or not _clear_pending(key, request):
            return  # 已被 decide/expire 兑现：先到者定论（等价于 JS 里 settle 会解绑监听）
        request["status"] = "cancelled"
        _record_confirmation_event(home, "confirmation_cancelled", request, "会话已中断")
        _settle(request, "rejected", "会话已中断")

    def expire():
        """TTL 到期 → 清待确认、记 expired、按拒绝兑现（移植自退役前 Node 实现）。"""
        if request["_settled"] or not _clear_pending(key, request):
            return
        request["status"] = "expired"
        # `Math.round(ttlMs / 1000)`（退役前 Node 实现）：半数向 +∞（Python round 是银行家舍入）
        seconds = _js.js_round(ttl_ms / 1000)
        _record_confirmation_event(home, "confirmation_expired", request,
                                  f"超过 {seconds} 秒未确认")
        _settle(request, "rejected", f"超过 {seconds} 秒未确认，按拒绝处理")

    # 已 aborted 的 signal 也要走同一条取消路径（先设 settle 再判 aborted，同退役前 Node 实现）。
    while True:
        if _signal_aborted(signal):
            abort()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            expire()
            break
        # setTimeout 的等价物：等判决，最多等到 TTL 或下一次 signal 轮询。
        if request["_done"].wait(min(_SIGNAL_POLL_S, remaining)):
            break
    return dict(request["_outcome"])


def _clear_pending(key, request):
    """仅当表里仍是这一笔时才清除（超时/取消与 decide 竞争时不让后者被前者覆盖）。"""
    with _PENDING_LOCK:
        if _PENDING.get(key) is request:
            _PENDING.pop(key, None)
            return True
    return False


def confirmation_view(home):
    """当前待确认项的只读视图（不含内部 settle 句柄）。

    字段与 store.js 逐字一致：``id/at/expires_at/mode/tool/operation/session_id/status/summary``。
    """
    request = _PENDING.get(_home_key(home))
    if request is None:
        return None
    return {"id": request["id"], "at": request["at"], "expires_at": request["expires_at"],
            "mode": request["mode"], "tool": request["tool"], "operation": request["operation"],
            "session_id": request["session_id"], "status": request["status"],
            "summary": request["summary"]}


def decide_confirmation(home, confirmation_id=None, decision=None):
    """用户作出决定。**唯一能批准实盘操作的入口**（服务侧 Web 确认卡片）。

    ``confirmation_id`` 对应 RPC 载荷的 ``id``（此处不叫 id 是为了不遮蔽内建函数；
    app.handle 负责 ``id`` → ``confirmation_id`` 的映射）。
    """
    if decision != "approved" and decision != "rejected":
        raise WorkbenchError("Invalid decision; expected approved/rejected")
    key = _home_key(home)
    with _PENDING_LOCK:
        request = _PENDING.get(key)
        if request is None:
            raise WorkbenchError("没有待确认的实盘操作（可能已超时或被处理）")
        if request["id"] != confirmation_id:
            raise WorkbenchError("确认编号不匹配；可能已被处理或已超时")
        _PENDING.pop(key, None)
        request["status"] = decision
        request["decided_at"] = _iso_now()
        # 唯一能批准实盘操作的入口就是工作台作答通道，所以主体恒为工作台界面。
        request["decided_by"] = "workbench-ui"
        # 在锁内兑现：与超时/取消的竞争由此变成「先到者定论」，后到者只看得到已兑现的结论。
        _settle(request, decision,
                "用户在工作台确认" if decision == "approved" else "用户在工作台拒绝")
    _record_confirmation_event(
        home, "confirmation_approved" if decision == "approved" else "confirmation_rejected",
        request)
    return {"id": confirmation_id, "decision": decision,
            "tool": request["tool"], "operation": request["operation"]}


def _record_confirmation_event(home, kind, request, note=None):
    """确认链路的活动留痕。

    写盘失败不影响确认本身（锁被占用时不能卡住等人回答）——与 Node 的空 catch 同义。
    """
    try:
        payload = {"kind": kind, "mode": request["mode"], "tool": request["tool"],
                   "operation": request["operation"], "confirmation_id": request["id"],
                   "session_id": request["session_id"],
                   # `request.summary?.fields ?? []`（退役前 Node 实现语义）
                   "summary": _js.field(request.get("summary") or {}, "fields") or []}
        if note:
            payload["note"] = note

        def apply(state):
            _event(state, payload)

        _update(home, apply)
    except Exception:  # noqa: BLE001 —— 留痕尽力而为，绝不因此改变确认结论
        pass


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
    """服务侧新 API（Node 侧没有对应的 store 方法）：形状照 workbench_admin.mjs 的
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
    """服务侧新 API（Node 侧没有对应的 store 方法）：形状照 workbench_admin.mjs 的
    runs 打印——每条 run 的状态与年龄（started_at 解析失败 -> None，对应 "年龄未知"）。"""
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
    "ABANDONED_AFTER_MS", "CONFIRM_OPERATIONS", "CONFIRM_TTL_MS", "LIMIT", "MARKET_HINT",
    "NOTICE", "ORDER_SIDE", "WorkbenchBusyError", "WorkbenchError",
    "admin_cancel_run", "admin_cancel_stale", "admin_prune_runs", "admin_runs", "admin_status",
    "confirmation_view", "decide_confirmation", "describe_order_args", "endpoints", "lock_file",
    "mode_file", "mode_value", "observations_dir", "order_operation",
    "pending_observations", "read_mode", "read_store", "request_confirmation", "snapshot",
    "store_file", "switch_mode", "with_run_status",
]
