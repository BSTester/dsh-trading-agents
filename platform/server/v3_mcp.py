"""V3 MCP 桥：把 ``/api/v3/*`` 路由**原样**暴露成 MCP 工具（规格：平台 MCP 单一交互面）。

唯一事实来源 = **FastAPI 路由表**
--------------------------------
本模块遍历装配好的 ``app.routes``，为每条 ``/api/v3/*`` 路由造一件 MCP 工具；工具的
调用体只有一件事：**拿到该路由的 ``endpoint`` 函数对象，用 MCP 实参调用它，把返回的
信封原样交回**。因此：

* **不复制业务逻辑**：HTTP 面与 MCP 面调的是同一个函数对象（同一份适配、同一份市场
  过滤、同一份 ``v3_run`` 调用）。桥接层里没有任何字段拼装、没有任何取数分支。
* **不绕过限流与缓存**：富途限流器（``server/v3_ratelimit``）、各子模块的 TTL 缓存与
  落盘缓存、SQLite 台账都在 handler 内部；桥接层既碰不到也不需要碰。
* **信封同一份**：``{ok, source, as_of, ...}`` 由 handler 产出，桥原样透传（dict 走
  ``jsonable_encoder``，与 FastAPI 对同一 dict 的序列化逐键一致；``JSONResponse`` 解出
  正文）。handler 之外的程序异常按工具面既有口径包成 ``trading/tool-failed``
  （``isError=true``），业务失败仍是 ``isError=false`` 的正常工具结果。
* **覆盖性由构造保证**：工具集就是路由表的一次遍历，路由与工具天然一一对应；
  ``tests/test_mcp_parity.py`` 再对「路由 ⇄ 工具」双射断言一次，并把「删掉一件工具就该
  红」写成用例。

通道分级（与既有 77 工具面同一套边界，绝不新增绕过路径）
--------------------------------------------------------
* ``/api/v3/credentials`` 的 ``save`` / ``clear``（写数据源凭据）**在桥内封死**：直接返回
  ``v3/credentials-web-only``，**不触达 endpoint、不触达磁盘**——与 ``switch_mode`` 封死
  ``live`` 同一条不变量（凭据管理是人工 Web 动作）。``status`` / ``test``（只读）放行。
* ``/api/v3/*`` 里**没有任何交易写端点**：下单/改单/撤单/切模式/执行计划全部只在
  ``/api/wb/*`` 与工作台 Web；本桥不新增、也不转发任何交易工具。
* 只读工具用 MCP 官方 ``readOnlyHint`` 标注（``annotations.readOnlyHint``）；写类工具
  （``strategy/run`` 落盘研究轮、``oms/sync`` 重写本地台账、``credentials``）不标只读。
"""
import base64
import contextvars
import inspect
import json
import typing
from urllib.parse import urlencode

from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.responses import Response

from server import mcp_tools

#: V3 路由前缀（**只**桥接它；``/metrics`` 是 Prometheus 文本口，不是平台信封，刻意不在内）。
V3_PREFIX = "/api/v3"

CREDENTIALS_PATH = "/api/v3/credentials"
#: 凭据写入的封死码/文案（与 ``mcp_tools.LIVE_SWITCH_*`` 同形的通道分级措辞）。
CREDENTIALS_WEB_ONLY_CODE = "v3/credentials-web-only"
CREDENTIALS_WEB_ONLY_MESSAGE = (
    "数据源凭据的写入/清除只能在独立 Web（默认 http://127.0.0.1:8397 的设置页）由人完成；"
    "本工具只提供 status（读状态）与 test（只读连通性测试）")

#: 会改服务端状态的 V3 路由（→ ``readOnlyHint=False``）。其余（含「只读取数 + 写本地缓存」
#: 的日历刷新与风险探测刷新）都标只读——这与各 handler 自己的「只读语义」声明同源。
NON_READONLY_PATHS = frozenset({
    "/api/v3/strategy/run",   # 落盘一轮研究流水线记录（不出订单）
    "/api/v3/oms/sync",       # 重写本地 OMS 台账（不出订单）
    "/api/v3/sdk/prompt",     # 起 Harness 子进程 / 下发提示词 + 追加审计（要口令，写类）
    CREDENTIALS_PATH,         # POST 面写/清凭据（桥内封死，见上）
})

#: 桥内封死的动作（**先于** endpoint 调用判定，口令/参数都改变不了这条分支）。
BLOCKED_ACTIONS = {CREDENTIALS_PATH: frozenset({"save", "clear"})}

#: 同路径多方法时的动作→方法映射（目前只有 ``/api/v3/credentials``：GET=读状态、POST=test）。
#: 桥不猜方法：表里没有的动作一律拒绝，避免「本来要读却打到写方法」。
METHOD_BY_ACTION = {CREDENTIALS_PATH: {None: "GET", "": "GET", "status": "GET", "test": "POST"}}

#: 二进制信封的正文内联上限（超过只给元数据 + 获取方式，不把大文件塞进工具结果）。
BINARY_INLINE_LIMIT = 1 << 20  # 1 MiB

# ---------------------------------------------------------------------------
# 工具描述（每一条路由一份；测试断言「路由 ⇄ 描述」完整，缺一条就红）
# ---------------------------------------------------------------------------
_READ_NOTE = "只读，走平台标准信封（ok/source/as_of）；取不到数据源时明确给出原因，不编造数字。"

TOOL_DOCS = {
    "/api/v3/audit": "审计链视图（透传工作台 audit 工具信封）：计划→订单→成交三级链路与信号/响应链路。"
                     + _READ_NOTE,
    "/api/v3/brain": "决策大脑只读视图：最近一轮策略 run（?market= 只过滤 decision）、"
                     "数据源健康与 SDK/Headless 通道状态。"
                     + _READ_NOTE,
    CREDENTIALS_PATH: "数据源凭据状态与只读连通性测试。action=status 读配置状态（是否已配置/来源/"
                      "掩码尾号，**绝不回显凭据值**）；action=test 用已保存凭据发一次真实只读请求并计时。"
                      "**写操作（save/clear）在 MCP 面封死**：凭据写入只能由人在独立 Web 的设置页完成"
                      "（返回 v3/credentials-web-only，不触达磁盘）。",
    "/api/v3/events": "标的公告/事件时间线（透传工作台 events 工具信封）。ticker 缺省取平台配置自选池首只；"
                      "window/days 是同一语义的两个查询参数（前端用 window，工具面用 days）。"
                      + _READ_NOTE,
    "/api/v3/execution": "执行面只读视图：持仓、在途委托、今日成交与 OMS 台账分级；"
                         "?market=SH|HK|US 时按账户市场过滤（不跨市场合并，附 filter 计数）。"
                         + _READ_NOTE,
    "/api/v3/execution/quality": "成交质量（只读）：委托/成交全部来自券商历史，无开源替代；"
                                 "取不到就如实报错，不给估算的滑点。market=SH|HK|US，mode=sim|live。"
                                 + _READ_NOTE,
    "/api/v3/factors/matrix": "横截面因子 z 矩阵 + 因子 IC 序列 + 六类因子的真实覆盖率（未给 tickers 时取该市场宇宙；"
                              "classes 选择并入的因子类别；forward 与 forward_days 同义，缺省 5）。"
                              "as_of（YYYY-MM-DD）显式给出时：价量列只由该时点可见的日线算出"
                              "（经 data.cache PIT 闸门），实时估值/另类列与 IC 不并入（不可 PIT，"
                              "如实跳过）；matrix.as_of=价量列实际日期与 asOf（本地因子 PIT 上界）"
                              "并列。不传 as_of = 最新口径（历史行为）。"
                              + _READ_NOTE,
    "/api/v3/factors/registry": "因子注册表（六类因子 + 真实数据源 + PIT 口径）+ 逐因子覆盖率；"
                                "覆盖率与 /api/v3/factors/matrix **同一份计算**（不为页面另造一套统计）；"
                                "classes=all 才把实时另类因子一起取。"
                                + _READ_NOTE,
    "/api/v3/financials": "三表（利润/资产/现金流）：美股走 SEC EDGAR，A 股/港股走富途 f10 并逐级降级；"
                          "chain 里逐条列出每次尝试的来源/耗时/错误。"
                          + _READ_NOTE,
    "/api/v3/gateway": "网关与调度只读视图：通道状态（mcp/sdk/headless）、调度心跳、作业历史与 kill/halt。"
                       + _READ_NOTE,
    "/api/v3/headless/log": "Headless 调用日志（FR-MON-003）：真实 headless_log 表 + 分页/筛选"
                            "（success/task_type/trigger/outcome/since/until，按 started_at 比较）；"
                            "stdout/stderr 分列存储（items[].stdout / .stderr 即原始两路）。"
                            + _READ_NOTE,
    "/api/v3/headless/schedule": "Headless 触发策略只读视图：各触发条件开关与最近判定、下一次触发时间、"
                                 "最近触发记录，以及外部熔断三参数（并发上限/单次超时/token 预算，"
                                 "超限即 kill 并记录）与 profile 白名单审计。"
                                 + _READ_NOTE,
    "/api/v3/macro": "宏观经济序列（免密钥）：主源 AKShare（国家统计局/央行口径），OpenBB OECD 为第二源。"
                     "indicator = cpi / ppi / pmi / shrzgm(社融) / m2；返回升序尾部序列 + 最新值 + 来源。"
                     "compare=oecd 时并列 OECD 标准化口径块（仅 CPI 有中国同口径序列，其余如实 oecd/no-series）；"
                     "AKShare 失败且为 CPI 时自动降到 OECD 腿（source/caliber 如实换标，不互相冒充）。"
                     + _READ_NOTE,
    "/api/v3/market": "K 线序列（周期 1m..1d/1w/1M），经工作台 series 工具取数，字段按 V3 契约适配。"
                      + _READ_NOTE,
    "/api/v3/market/watchlist": "该市场池子的快照（逐票最近日 K + 动量）：池子来自配置自选池或真实持仓，"
                                "**不发明自选池**；该市场无宇宙时返回 market/no-universe 并附原因。"
                                + _READ_NOTE,
    "/api/v3/markets/calendar": "三市场（SH/HK/US）交易时段与节假日（自动获取日历；取不到时只按周末判断并"
                                "说明原因）。now 可注入 ISO 8601 以便对齐时钟。"
                                + _READ_NOTE,
    "/api/v3/markets/calendar/refresh": "刷新交易日历缓存（**只读语义**：取数 + 写本地 market-calendar.json，"
                                        "不涉交易）：跳过缓存直取富途（A 股再降级 AKShare），逐市场返回真实来源。"
                                        + _READ_NOTE,
    "/api/v3/metrics": "平台运行指标：工具域计数、工作台可达性、调用计数、富途限流读数、SQLite 台账各表行数、"
                       "OMS 分级计数；SDK 通道缺失时如实标注。"
                       + _READ_NOTE,
    "/api/v3/metrics/probe/refresh": "风险探测刷新（**只读语义**：取数 + 写本地 v3-risk-probe.json，"
                                     "不触发任何交易/写端点）；/metrics 的行业红线指标只读该缓存。"
                                     + _READ_NOTE,
    "/api/v3/ml/backtest": "单标的动量 long/flat 回测（PIT：t 日持仓只由 ≤t-1 收盘价决定）："
                           "sharp/年化/最大回撤/换手等指标 + 净值曲线；数据不足返回 backtest/insufficient。"
                           "纯计算，不落盘任何状态。"
                           + _READ_NOTE,
    "/api/v3/ml/models": "ML 策略族（Lasso/GBDT/MLP）与动量基线的同口径样本外评估；"
                         "样本不足 → ml/insufficient-sample，该市场无宇宙 → market/no-universe。"
                         + _READ_NOTE,
    "/api/v3/ml/sweep": "动量参数网格扫描（窗口 × 调仓周期，真实日 K 回测），返回每格指标与 best。"
                        "组合硬上限 5000（超限报 bad-args）；time_budget_ms 超预算如实截断，"
                        "plannedCombos/combosExecuted/elapsedMs/truncated 如实上报。"
                        + _READ_NOTE,
    "/api/v3/news": "A 股个股资讯（AKShare 免密钥）。symbol 允许 SH.600519 形式；无数据时给出真实原因。"
                    + _READ_NOTE,
    "/api/v3/northbound": "北向资金（AKShare 免密钥）：当日四方板块（沪股通/深股通/港股通沪深）+ "
                          "沪/深股通历史净买额序列。**披露事实**：北向当日净买入自 2024-08-19 起交易所"
                          "不再披露（实测最后非空日期 2024-08-16），响应 net_buy_disclosure 写明，"
                          "上游 0 占位绝不冒充真实净买额；部件失败在 errors 里逐条留真实错误。"
                          + _READ_NOTE,
    "/api/v3/oms/orders": "OMS 台账只读视图（按市场过滤可选）：登记、风控分级与对账结果。"
                          + _READ_NOTE,
    "/api/v3/oms/sync": "重新对账：plan → 登记/分级 → 与在途委托命中 → **重写本地 OMS 台账**。"
                        "只改本地台账，不产生任何订单；行业暴露按订单所属市场分别取数。",
    "/api/v3/openbb": "OpenBB 基本面（可选依赖）：未安装时返回 openbb/unavailable 且**不发网络请求**。"
                      + _READ_NOTE,
    "/api/v3/ops/alerts": "平台内告警三态（firing / pending / ok / no-data / unsupported）："
                          "对 Prometheus 文本求值，与 Grafana 读同一份规则。state 只过滤其中一个"
                          "状态，取值非法时**不过滤也不报错**（只读端点不因参数失败）。"
                          + _READ_NOTE,
    "/api/v3/ops/alerts/rules": "告警规则清单（名字/表达式/for/severity/域/是否在支持子集内/"
                                "依赖的指标与来源文件）——与 Prometheus 共用一份 alerts.yml，"
                                "规则里用到的函数也逐条列出。"
                                + _READ_NOTE,
    "/api/v3/orderbook": "盘口五档快照；富途账号未开通实时行情权限时原样透传上游错误（如 errcode=-9），"
                         "由调用方显示「无数据源 + 原因」，不填占位。"
                         + _READ_NOTE,
    "/api/v3/overview": "系统概览：台账权益、持仓、今日成交、冻结计划、调度心跳、数据源健康、推送状态；"
                        "?market=SH|HK|US 时 positions/plan/deals_today 同口径按市场过滤"
                        "（equity 是单一台账，不可拆，响应里 sections 已说明）。"
                        + _READ_NOTE,
    "/api/v3/plates": "板块清单（ALL/INDUSTRY/REGION/CONCEPT/OTHER）；板块涨跌幅需富途实时行情权限，"
                      "无权限时只有清单并注明，不填占位。"
                      + _READ_NOTE,
    "/api/v3/research": "研报 / 研究 run / 量化预览 / 活动流（来自工作台快照，按当前模式过滤）；"
                        "?market= 时四个列表都按标的市场过滤并附 filter 计数。"
                        + _READ_NOTE,
    "/api/v3/research/report.pdf": "把一篇已发布研报渲染成 PDF（A4 暗色研报主题）。id / ticker 二选一选一篇，"
                                   "都不给取最新一篇。成功响应是二进制：桥以标准信封回**元数据**"
                                   f"（content_type/bytes/filename），正文在 ≤{BINARY_INLINE_LIMIT} 字节时内联为 "
                                   "content_base64，超过则只给元数据（正文用 HTTP GET 该路径取）；"
                                   "渲染失败/找不到研报仍返回标准错误信封。"
                                   + _READ_NOTE,
    "/api/v3/research/tasks": "值班研究员队列（daily_brief/factor_patrol/mining_round）只读视图；"
                              "?market= 按任务 market 字段过滤，无法归属市场的任务计入 unattributed，不静默丢弃。"
                              "注意：领取/回报仍用 research_tasks_claim / research_tasks_report 两件工具。"
                              + _READ_NOTE,
    "/api/v3/risk": "风控配置与当前账户风险指标（透传工作台 risk 工具信封，阈值以它为准）。"
                    + _READ_NOTE,
    "/api/v3/risk/analytics": "组合风险量：历史模拟法 VaR/CVaR、Beta/Alpha/IR、Kupiec POF 与净值曲线；"
                              "market 缺省 SH，benchmark 缺省按市场实测选取（不可用则为 null 并说明）。"
                              + _READ_NOTE,
    "/api/v3/risk/industry": "行业映射与暴露：板块归属来自富途，权重来自平台组合；limit_pct 是行业红线"
                             "（缺省 20）。上游缺数据时如实标注 missing，不估算。"
                             + _READ_NOTE,
    "/api/v3/risk/funding-check": "**事前风控·资金检查（只读）**：订单金额（order_value，或 qty×price）"
                                  "对真实可用购买力（account_funds）。资金不足 → action=blocked + 读数与来源；"
                                  "不够读数 → action=unknown + 原因。**不改既有下单前闸门语义**，只作读数与"
                                  "分级建议。"
                                  + _READ_NOTE,
    "/api/v3/sdk/status": "Harness SDK 通道**只读**状态：本进程内真实 dsh 子进程与握手结果"
                          "（含协议标识比对）、白名单审计与最近审计条目。有意**不**在这里起进程——"
                          "启动/握手是显式动作（POST /api/v3/sdk/prompt 且要口令）。"
                          + _READ_NOTE,
    "/api/v3/sdk/sessions": "Harness SDK 会话**只读**回放：会话状态 + 事件环（since 之后、最多 limit 条）。"
                            "事件是进程内真实记录，非回填。"
                            + _READ_NOTE,
    "/api/v3/sdk/prompt": "**写端点**：向 Harness SDK 会话下发提示词（或 action=start 只做「起进程 + "
                          "initialize 握手」）。需人工口令「确认下发」，服务端 fail-closed：口令缺失/不符"
                          "一律拒绝、未知 action 拒绝，**拒绝也落审计**（拒绝不是静默）。会改本进程状态"
                          "（起子进程、追加审计文件），故**不标只读**。",
    "/api/v3/settings": "接入与授权只读状态：当前模式、富途渠道与 token 状态、环境变量是否注入（**只报是否"
                        "与来源，绝不出值**）、各数据源可用性。"
                        + _READ_NOTE,
    "/api/v3/sentiment": "个股资讯情绪因子（只读）：取资讯 → 打分（自研词典 + 否定/程度修饰 + 时间半衰）→"
                         "可解释信封（score/coverage/top_terms/per_day）。窗口内无资讯或无一命中词典时 "
                         "score=null 并给 notes，**不返回 0 分冒充中性**；上游失败时 message 是真实错误原文。"
                         + _READ_NOTE,
    "/api/v3/sources/status": "各数据源降级链的**真实**可用性（每条链主源→降级源各做一次轻量只读探测；"
                              "不探测任何写端点）。keys 逗号分隔可只探指定链。"
                              + _READ_NOTE,
    "/api/v3/spot": "A 股实时快照（多接口降级 + 退避重试）；全失败时带试过的接口与真实错误原文，"
                    "**不伪造、不返回空 rows**。"
                    + _READ_NOTE,
    "/api/v3/strategies/event-study": "事件驱动策略（FR-STRAT-002，只读研究、不出订单）：真实公告事件"
                                       "（必须带可核验公告时点，缺时点不入样）+ 每事件收益 + returnCurve "
                                       "（无基准，分别平均每日与买入持有累计收益）+ 分类型统计。"
                                       "equityCurve 按日对活跃事件收益等权复利，非事件末期收益均值。"
                                       "公告日收盘后可知 → 首个后续交易日收盘建仓，持有 H 日。"
                                       "样本 < min_events 如实返回不足；K 线经 data.cache PIT 闸门。"
                                       + _READ_NOTE,
    "/api/v3/strategies/stat-arb": "统计套利策略（FR-STRAT-002，只读研究、不出订单）：同市场标的对 → 训练窗 "
                                    "OLS 对冲比率 → 单序列 DF 启发式残差筛选（标准库实现，MacKinnon(1994) "
                                    "c/N=1 左尾近似 p，非校准 Engle-Granger 协整检验）→ 样本外双腿回测"
                                    "（含成本，z 信号仅用 t-1 及之前）。无通过者 screenPassed=false，"
                                    "selected/backtest=null，不硬选，不据此证明协整。"
                                    + _READ_NOTE,
    "/api/v3/strategy": "最近一轮研究流水线（PDAT→PET）结果；从未运行过 → run=null 并说明。"
                        + _READ_NOTE,
    "/api/v3/strategy/run": "跑一轮 PDAT→PAAT→PCPT→PRT→PET 研究流水线并落盘（topN/window/universe/market）；"
                            "**不下单**：产物只是调仓建议提案，执行仍走工作台受约束入口。",
    "/api/v3/tools": "六域工具目录（data/alpha/ml/risk/execution/ecosystem）：工作台工具面 + V3 本地计算；"
                     "?domain= 可只看一域。"
                     + _READ_NOTE,
}

# ---------------------------------------------------------------------------
# 参数描述：按名字共享一份（同名字段在不同端点语义一致），个别端点用覆盖表写清差异
# ---------------------------------------------------------------------------
PARAM_DOCS = {
    "ticker": "标的代码，如 SH.600519 / HK.00700 / US.AAPL",
    "tickers": "标的列表（逗号分隔）",
    "symbol": "标的代码（允许 SH.600519 形式）",
    "period": "周期：1m/5m/15m/30m/60m/1d（部分端点还支持 1w/1M）",
    "limit": "最多返回条数",
    "n": "池子取前 n 只（1..20，默认 6）",
    "market": "市场：SH / HK / US",
    "markets": "市场列表（逗号分隔，缺省 SH,HK,US）",
    "plate_class": "板块分类：ALL / INDUSTRY / REGION / CONCEPT / OTHER（REGION 仅 A 股）",
    "keys": "只探这些降级链（逗号分隔，缺省全部）",
    "now": "注入当前时刻（ISO 8601，带偏移）以便对齐时钟；缺省取服务端时间",
    "horizon_days": "日历取数前瞻天数（默认 400）",
    "limit_pct": "行业集中度红线（百分比，缺省 20）",
    "windows": "动量窗口网格（逗号分隔，如 10,20,30,60）",
    "rebalance": "调仓周期网格（逗号分隔，如 5,10,20）",
    "time_budget_ms": "扫参时间预算（毫秒）：超预算如实截断并报告 plannedCombos/combosExecuted/truncated；"
                      "0 表示不限时（缺省 120000）",
    "min_events": "事件研究的最小样本数（实测事件数少于此值时如实返回「样本不足」，缺省 5）",
    "train_ratio": "训练窗占比（0.3..0.9，缺省 0.7；其余为样本外测试窗）",
    "z_window": "价差 z-score 的滚动窗口（交易日，5..250，缺省 60）",
    "z_in": "开仓阈值：|z| ≥ z_in 进（缺省 2.0）",
    "z_out": "平仓阈值：|z| ≤ z_out 平（缺省 0.5）",
    "include_docs": "true 时附带每文档明细（doc_events）；缺省 false（避免响应膨胀）",
    "window": "回看窗口长度（根）",
    "days": "时间窗天数（按发布时间过滤资讯；缺时间戳的记录保留并单独计数）",
    "horizon": "预测前瞻期数",
    "cost_bps": "单边交易成本（基点），用于同口径比较",
    "confidence": "VaR/CVaR 置信度（缺省 0.95）",
    "benchmark": "基准代码；缺省按市场实测选取（不可用则在响应里说明）",
    "weights": "组合权重（逗号分隔的 / 或 JSON），缺省等权",
    "factor": "因子名（缺省按端点默认）",
    "forward_days": "IC 前瞻期数（与 forward 同义）",
    "forward": "IC 前瞻期数（与 forward_days 同义）",
    "mode": "账户模式：sim / live（只影响读取哪个模式下的账户数据）",
    "statement": "报表类型：income / balance / cashflow",
    "periods": "返回的报告期数（1..12）",
    "indicator": "宏观指标：cpi / ppi / pmi / shrzgm(社融) / m2（或中文别名 社融 / 货币供应量）",
    "compare": "宏观对照源：oecd = 并列 OpenBB OECD 标准化口径块（仅 CPI 有中国同口径序列）；"
               "留空 = 只出 NBS 主源（不触发 openbb 冷启动 import）",
    "classes": "因子类别选择（quality,growth,sentiment 缺省；all 含实时另类；none 只要价量/估值列）",
    "as_of": "PIT 上界（严格完整 YYYY-MM-DD 日历日）。工作台 factors 未指定/空值保持最新口径；"
             "显式给出时仅用 t <= as_of 的价量日线，估值无 PIT 数据，未并入原始值、z-score 或合成权重。"
             "仅筛选最近 window 根（最多 900 根），不足 65 根报不足，不保证任意历史可重建。"
             "/api/v3/factors/matrix 显式给出时价量列经 data.cache PIT 闸门，实时估值/另类列与 IC "
             "不并入；未指定保持最新口径。其他端点缺省今天（UTC）",
    "session": "会话 id（缺省聚合全部会话）",
    "offset": "分页起点（缺省 0）",
    "success": "只看成功/失败（true|false；非法值不过滤）",
    "task_type": "按任务类型过滤（如 daily_brief / factor_patrol / mining_round）",
    "trigger": "按触发来源过滤（如 schedule / event / manual）",
    "outcome": "按结束状态过滤（如 done / failed / timeout）",
    "since": "起始时间（ISO 字符串，按 started_at 前缀比较）",
    "until": "结束时间（ISO 字符串，按 started_at 前缀比较）",
    "qty": "数量（与 price 一起算订单金额）",
    "side": "交易方向（BUY / SELL；仅作读数标注）",
}
#: 同名不同义的少数端点在此写清（键为 (路径, 参数名)）。
PARAM_DOCS_OVERRIDES = {
    ("/api/v3/strategies/event-study", "days"): "事件公告回看天数（30..2000，缺省 730；"
                                                "只有带公告时点的事件入样）",
    ("/api/v3/strategies/event-study", "horizon"): "事件持有期 H（交易日，1..60，缺省 5）："
                                                    "次日建仓、持有 H 日平仓",
    ("/api/v3/strategies/event-study", "limit"): "K 线取数根数（60..2000，缺省 500；"
                                                  "事件前瞻不足 H 的事件被如实排除）",
    ("/api/v3/strategies/stat-arb", "cost_bps"): "双腿单边成本（基点）：按 |Δpos|×(1+|β|) 计，缺省 5",
    ("/api/v3/strategies/stat-arb", "limit"): "K 线取数根数（60..2000，缺省 500；"
                                               "对齐后不足 80 天报 statarb/insufficient）",
    ("/api/v3/research/report.pdf", "ticker"): "按标的筛选一篇已发布研报（与 id 二选一）",
    ("/api/v3/financials", "ticker"): "标的代码：美股如 AAPL，A 股如 SH.600000/600519.SH，港股如 HK.00700",
    ("/api/v3/macro", "limit"): "序列尾部返回条数（1..500，缺省 24）",
    ("/api/v3/northbound", "limit"): "沪/深股通历史序列各返回的尾部条数（1..3000，缺省 60）",
    ("/api/v3/market/watchlist", "market"): "市场：SH / HK / US（缺省 SH）",
    ("/api/v3/ops/alerts", "state"): "只看某一态（firing / pending / ok / no-data / unsupported）；"
                                     "留空看全部，取值非法时不过滤（只读端点不因参数失败）",
    ("/api/v3/risk/funding-check", "order_value"): "订单金额（不给则由 qty × price 计算）",
    ("/api/v3/risk/funding-check", "qty"): "下单数量（与 price 一起算订单金额）",
    ("/api/v3/risk/funding-check", "price"): "下单价格（与 qty 一起算订单金额）",
    ("/api/v3/risk/funding-check", "side"): "交易方向：BUY / SELL（只作读数标注，不改变闸门语义）",
    ("/api/v3/risk/analytics", "details"): "true 时附带逐日明细（净值/回撤序列），false 只给汇总量",
    ("/api/v3/sdk/sessions", "session"): "只看某个会话 id（缺省聚合全部会话）",
    ("/api/v3/sdk/sessions", "since"): "从第几条事件开始回放（事件环下标，缺省 0）",
    ("/api/v3/headless/log", "offset"): "分页起点（缺省 0）",
    ("/api/v3/headless/log", "since"): "起始时间（ISO 字符串，按 started_at 前缀比较）",
    ("/api/v3/headless/log", "until"): "结束时间（ISO 字符串，按 started_at 前缀比较）",
    ("/api/v3/headless/log", "success"): "只看成功/失败（true|false；非法值不过滤）",
}

# ---------------------------------------------------------------------------
# 手工声明参数的路由：这些 endpoint 的入参不是签名（走 ``request.query_params`` 或 JSON 体），
# 因此 schema 必须显式声明。声明的是**取值域元数据**，不是业务逻辑。
# ---------------------------------------------------------------------------
def _p(name, kind, doc, required=False, minimum=None, maximum=None):
    return mcp_tools.Param(name, kind, doc, required=required, minimum=minimum, maximum=maximum)


REQUEST_PARAMS = {
    "/api/v3/tools": (
        _p("domain", "str", "只看一个工具域：data / alpha / ml / risk / execution / ecosystem"),
    ),
    "/api/v3/oms/orders": (
        _p("market", "str", "市场：SH / HK / US（缺省不过滤）"),
    ),
    "/api/v3/events": (
        _p("ticker", "str", "标的代码（缺省取平台配置自选池首只）"),
        _p("window", "int", "回看天数（前端查询参数名）"),
        _p("days", "int", "回看天数（工具面字段名；与 window 同义，window 优先）"),
    ),
    "/api/v3/audit": (
        _p("window", "int", "回看窗口（工作台 audit 工具面不接受该字段时不透传，绝不改写成别的字段）"),
    ),
    "/api/v3/research/report.pdf": (
        _p("id", "str", "研报/研究 run 的 id（与 ticker 二选一）"),
        _p("ticker", "str", "按标的筛选一篇已发布研报（与 id 二选一）"),
    ),
    CREDENTIALS_PATH: (
        _p("action", "str", "status=读配置状态（缺省）；test=用已保存凭据做一次只读连通性测试。"
                            "save/clear 在 MCP 面封死（凭据写入只能由人在 Web 设置页完成）"),
        _p("key", "str", "凭据键名（缺省读凭据注册表首键；Tushare 已随免密政策移除）"),
        _p("value", "str", "**MCP 面不接受**：凭据值只能由人在 Web 设置页输入"),
    ),
    "/api/v3/strategy/run": (
        _p("topN", "int", "候选多头数量（1..50，缺省 2）", minimum=1, maximum=50),
        _p("window", "int", "动量回看窗口（1..500，缺省 20）", minimum=1, maximum=500),
        _p("market", "str", "市场：SH / HK / US（未给 universe 时按该市场宇宙取标的）"),
        _p("universe", "str_list", "标的列表（缺省用该市场宇宙，再退回平台自选池）"),
    ),
    "/api/v3/ml/backtest": (
        _p("ticker", "str", "标的代码，如 SH.600519", required=True),
        _p("window", "int", "动量回看窗口（1..500，缺省 20）", minimum=1, maximum=500),
        _p("rebalanceDays", "int", "调仓周期天数（1..500，缺省 5）", minimum=1, maximum=500),
        _p("limit", "int", "取数根数（60..2000，缺省 500）", minimum=60, maximum=2000),
        _p("market", "str", "市场标注：SH / HK / US（缺省按标的代码前缀推断）"),
    ),
}

#: 声明了参数的路径却缺描述 → 立即炸掉（避免 schema 里出现无说明字段）。
for _path, _params in REQUEST_PARAMS.items():
    for _param in _params:
        if not str(_param.description or "").strip():
            raise AssertionError(f"{_path} 的 {_param.name} 缺参数说明")

# ---------------------------------------------------------------------------
# 进程内调用标记：MCP 桥调用的是同一条路由函数，因此 v3_ops 的「HTTP 请求计数」如果不区分
# 就会把工具调用算成 HTTP 请求。这个 contextvar 让 v3_ops 能如实区分两个面。
# ---------------------------------------------------------------------------
_bridge_depth = contextvars.ContextVar("v3_mcp_bridge_depth", default=0)


def inproc_bridge_active():
    """当前执行流是否由 MCP 桥发起（v3_ops 用它区分 HTTP 计数与工具调用计数）。"""
    return _bridge_depth.get() > 0


#: 每次注册过程中收集的描述欠账（测试断言为空；见 ``build_definitions`` 的契约说明）。
_GAPS = None


class DescriptionGaps:
    """工具面元数据欠账清单（新增 V3 路由后忘记登记工具面时的**可断言**证据）。"""

    __slots__ = ("missing_tool_docs", "missing_param_docs", "unknown_annotations",
                 "unclosed_signatures", "multi_method_paths")

    def __init__(self):
        self.missing_tool_docs = []
        self.missing_param_docs = []
        self.unknown_annotations = []
        self.unclosed_signatures = []
        self.multi_method_paths = []

    @property
    def empty(self):
        return not any((self.missing_tool_docs, self.missing_param_docs,
                        self.unknown_annotations, self.unclosed_signatures,
                        self.multi_method_paths))

    def as_dict(self):
        return {name: list(getattr(self, name)) for name in self.__slots__}

    def __repr__(self):
        if self.empty:
            return "DescriptionGaps(empty)"
        return f"DescriptionGaps({self.as_dict()!r})"


def new_gaps():
    """开一份新的欠账收集器（``build_definitions`` 每次调用前重置）。"""
    global _GAPS  # noqa: PLW0603 —— 单次装配内的收集器，与 app 同生命周期
    _GAPS = DescriptionGaps()
    return _GAPS


# 导入期即建好收集器：任何在 ``build_definitions`` 之外的调用（测试直接叫 ``_kind_of`` 等）
# 都不会撞到 None。
_GAPS = DescriptionGaps()


# ---------------------------------------------------------------------------
# 名称 / 类型
# ---------------------------------------------------------------------------
def tool_name(path):
    """``/api/v3/risk/industry`` → ``v3_risk_industry``（``/`` ``.`` ``-`` 一律折成 ``_``）。"""
    tail = path[len(V3_PREFIX):].strip("/")
    cleaned = "".join(char if (char.isalnum() and char.isascii()) or char == "_" else "_"
                      for char in tail)
    return "v3_" + cleaned.strip("_").lower()


_SIMPLE_KINDS = {str: "str", int: "int", float: "number", bool: "bool"}


def _unwrap_optional(annotation):
    """``Optional[str]`` / ``str | None`` → ``str``（其余原样）。"""
    args = [item for item in typing.get_args(annotation) if item is not type(None)]
    return args[0] if len(args) == 1 else annotation


def _kind_of(annotation, path, name):
    """注解 → MCP 取值类型；无法映射时**降级为 str 并记一笔欠账**（见文件头「永不阻断启动」）。"""
    kind = _SIMPLE_KINDS.get(_unwrap_optional(annotation))
    if kind is None:
        _GAPS.unknown_annotations.append((path, name, repr(annotation)))
        return "str"
    return kind


def _param_doc(path, name):
    """参数说明：端点级覆盖 → 名字级共享 → 占位（并记一笔欠账，由测试断言为空）。"""
    doc = PARAM_DOCS_OVERRIDES.get((path, name)) or PARAM_DOCS.get(name)
    if not doc:
        _GAPS.missing_param_docs.append((path, name))
        return f"{name}（{path} 的查询/请求字段；说明未登记）"
    return doc


def _signature_params(path, endpoint):
    """从 endpoint 签名派生参数（跳过 ``request``）：注解给类型、缺省给可选性。

    注解先经 ``typing.get_type_hints`` 解析——有若干 endpoint 写的是**前向引用字符串**
    （``market: "str" = "SH"``），只读 ``parameter.annotation`` 会拿到 ``'str'`` 字面量。
    """
    signature = inspect.signature(endpoint)
    try:
        hints = typing.get_type_hints(endpoint)
    except Exception:  # noqa: BLE001 —— 解析失败时退回原始注解（下面按 str 降级并记欠账）
        hints = {}
    params = []
    for name, parameter in signature.parameters.items():
        if name == "request":
            continue
        if parameter.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            # 变参无法生成封闭 schema：这一条只能显式声明（记欠账而不是阻断启动）。
            _GAPS.unclosed_signatures.append((path, name))
            continue
        annotation = hints.get(name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = str
        params.append(_p(name, _kind_of(annotation, path, name), _param_doc(path, name),
                         required=parameter.default is inspect.Parameter.empty))
    return tuple(params)


def _fallback_doc(path, methods):
    """没登记 TOOL_DOCS 时的兜底描述：取 endpoint 自己的 docstring 首段 + 只读提示。"""
    endpoint = next(iter(methods.values()))
    raw = inspect.getdoc(endpoint) or ""
    first = ""
    for block in raw.split("\n\n"):
        block = " ".join(line.strip() for line in block.strip().splitlines() if line.strip())
        if block:
            first = block
            break
    if not first:
        first = "（该端点没有 docstring）"
    note = _READ_NOTE if path not in NON_READONLY_PATHS else "该端点会改服务端状态（不标只读）。"
    return f"{first}\n\n【自动描述】TOOL_DOCS 未登记 {path}，此处取 handler docstring 兜底；{note}"


# ---------------------------------------------------------------------------
# 路由枚举（唯一事实来源）
# ---------------------------------------------------------------------------
def _v3_routes(app):
    """``{path: {method: endpoint}}``：只收 ``/api/v3/*``（含子路径），按路径排序。"""
    found = {}
    for route in getattr(app, "routes", ()):
        path = getattr(route, "path", "")
        if not path.startswith(V3_PREFIX + "/"):
            continue
        endpoint = getattr(route, "endpoint", None)
        if not callable(endpoint):
            continue
        for method in sorted(getattr(route, "methods", None) or ()):
            found.setdefault(path, {})[method] = endpoint
    return dict(sorted(found.items()))


def build_definitions(app):
    """路由表 → ``(ToolDefinition 元组, DescriptionGaps)``。

    **契约：本函数永不抛异常。** 平台服务是生产件，MCP 桥接面只是它的一个出口；一条新加的
    ``/api/v3/*`` 路由不应该让整个平台起不来。遇到没登记描述/参数说明/无法映射的注解时，
    桥**降级**（兜底描述、str 类型、保守只读）并把欠账记进 ``DescriptionGaps``，
    由 ``tests/test_mcp_parity.py`` 断言「欠账为空」——漂移在测试里红，而不是在启动时炸。
    """
    new_gaps()
    definitions = []
    for path, methods in _v3_routes(app).items():
        if path in TOOL_DOCS:
            description = TOOL_DOCS[path]
        else:
            _GAPS.missing_tool_docs.append(path)
            description = _fallback_doc(path, methods)
        if path in REQUEST_PARAMS:
            params = REQUEST_PARAMS[path]
        elif len(methods) == 1:
            params = _signature_params(path, next(iter(methods.values())))
        else:
            # 多方法且没声明：**只暴露 GET（读）**，绝不猜着调用可能写的方法。
            _GAPS.multi_method_paths.append((path, tuple(sorted(methods))))
            params = _signature_params(path, methods["GET"]) if "GET" in methods else ()
        definitions.append(mcp_tools.ToolDefinition(
            tool_name(path), description, path, tuple(params)))
    return tuple(definitions), _GAPS


# ---------------------------------------------------------------------------
# 调用体
# ---------------------------------------------------------------------------
def _synth_request(path, method, payload):
    """合成一个最小 Starlette ``Request``：query 取标量实参，POST 同时把实参作为 JSON 体。

    只服务「入参走 request」的那几个 endpoint（它们自己 ``request.query_params`` /
    ``request.json()``），语义与 HTTP 面逐项一致：查询参数是字符串，体是同一个 JSON 对象。
    """
    query = {key: value for key, value in payload.items()
             if isinstance(value, (str, int, float, bool)) and value is not None}
    body = b""
    headers = []
    if method == "POST":
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode("ascii"))]
    scope = {
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode("utf-8"),
        "query_string": urlencode(query).encode("utf-8"),
        "root_path": "", "headers": headers,
        "server": ("127.0.0.1", 0), "client": ("127.0.0.1", 0),
    }
    delivered = []

    async def receive():
        if delivered:
            return {"type": "http.disconnect"}
        delivered.append(True)
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _binary_envelope(path, result):
    """二进制响应 → 标准信封（元数据 + 可选内联正文）：不伪造字段、不吞状态。"""
    body = result.body
    content_type = (result.headers.get("content-type") or "").split(";")[0].strip()
    envelope = {
        "ok": True,
        "kind": "binary",
        "content_type": content_type,
        "bytes": len(body),
        "http_path": path,
        "note": ("该端点的成功响应是二进制正文（不是 JSON 信封）；MCP 文本通道在正文≤"
                 f"{BINARY_INLINE_LIMIT} 字节时内联 content_base64，"
                 "否则只给元数据（正文用 HTTP GET 该路径取）。错误路径仍返回标准错误信封。"),
    }
    disposition = result.headers.get("content-disposition")
    if disposition:
        envelope["content_disposition"] = disposition
    for header, key in (("x-pdf-bytes", "pdf_bytes"), ("x-pdf-engine", "pdf_engine")):
        if result.headers.get(header) is not None:
            envelope[key] = result.headers[header]
    if len(body) <= BINARY_INLINE_LIMIT:
        envelope["content_base64"] = base64.b64encode(body).decode("ascii")
    return envelope


def to_envelope(path, result):
    """路由返回值 → 平台信封。

    dict 走 ``jsonable_encoder``（与 FastAPI 对同一 dict 的序列化逐键一致）；
    ``Response`` 按 media type 分流：JSON 解出正文、二进制按上面的口径收敛。
    """
    if isinstance(result, dict):
        return jsonable_encoder(result)
    if isinstance(result, Response):
        media = (result.headers.get("content-type") or "").split(";")[0].strip().lower()
        if media == "application/json" or media.endswith("+json"):
            try:
                parsed = json.loads(result.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return mcp_tools.failure("v3/bad-envelope",
                                         f"{path} 的响应不是合法 JSON 信封：{error}")
            if isinstance(parsed, dict):
                return parsed
            return mcp_tools.failure("v3/bad-envelope",
                                     f"{path} 的响应不是 JSON 对象（{type(parsed).__name__}）")
        return _binary_envelope(path, result)
    return mcp_tools.failure(
        "v3/bad-envelope",
        f"{path} 的 handler 返回了 {type(result).__name__}，既不是 dict 也不是 Response")


class V3Bridge:
    """路由表 → 调用体：把实参交给**同一条路由**的 endpoint 函数。"""

    def __init__(self, app):
        self._routes = _v3_routes(app)
        self.paths = tuple(self._routes)
        self.definitions, self.gaps = build_definitions(app)
        self.names = tuple(definition.name for definition in self.definitions)
        self.readonly = {definition.endpoint: definition.endpoint not in NON_READONLY_PATHS
                         for definition in self.definitions}
        #: 名字 → **同一个**已注册函数对象（direct 模式注册时填充；discovery 模式复用同一批，
        #: 保证「直连工具」与「发现代理转发的工具」是同一份实现，不是两份同源代码）。
        self.bound = {}

    def method_for(self, path, payload):
        """该路径该动作该走哪个 HTTP 方法（**只**按声明表；没声明就退回 GET=读，绝不猜写）。"""
        methods = self._routes[path]
        if len(methods) == 1:
            return next(iter(methods))
        table = METHOD_BY_ACTION.get(path) or {}
        method = table.get(payload.get("action"))
        if method is None:
            # 未声明的动作：保守走 GET（读），绝不因为猜错而调到写方法。
            return "GET" if "GET" in methods else next(iter(methods))
        return method

    async def __call__(self, path, payload):
        blocked = BLOCKED_ACTIONS.get(path)
        if blocked and payload.get("action") in blocked:
            # 通道分级：先于 endpoint 调用判定——参数/口令都改变不了这条分支，磁盘也不会被碰。
            return mcp_tools.failure(CREDENTIALS_WEB_ONLY_CODE, CREDENTIALS_WEB_ONLY_MESSAGE)
        method = self.method_for(path, payload)
        endpoint = self._routes[path][method]
        kwargs = {}
        wants_request = False
        for name in inspect.signature(endpoint).parameters:
            if name == "request":
                wants_request = True
            elif name in payload:
                kwargs[name] = payload[name]
        if wants_request:
            kwargs["request"] = _synth_request(path, method, payload)
        token = _bridge_depth.set(_bridge_depth.get() + 1)
        try:
            result = await endpoint(**kwargs)
        finally:
            _bridge_depth.reset(token)
        return to_envelope(path, result)


def _bind(definition, bridge):
    """造 SDK 注册函数：``**kwargs`` 收下校验后的实参，``__signature__`` 表达字段集。

    与 ``mcp_tools._bind`` 同形，唯一差别是 **async**——V3 endpoint 是协程，桥必须 await
    （业务侧仍然是各 handler 自己的 ``asyncio.to_thread``，不阻塞事件循环）。

    FR-TOOLS-002 render 分离（2026-09-21 桥接面扩展）：``mcp_tools.is_renderable`` 为真的
    v3 工具在签名**末尾追加**同一个 ``format`` 字段（复用 ``mcp_tools.format_param()``，
    schema 与卡片因此共用同一个谓词）；响应侧交给**同一个**
    ``mcp_tools.render_tool_result``——它读的也是同一份 ``TOOL_RENDERERS``，
    桥接面**没有第二套渲染机制**。``format`` 在调 endpoint 前就被摘掉，绝不进业务载荷。
    """
    async def fn(**kwargs):
        provided = {key: value for key, value in kwargs.items() if value is not mcp_tools.UNSET}
        want_text = (str(provided.pop(mcp_tools.FORMAT_FIELD, None) or "")
                     == mcp_tools.FORMAT_TEXT)
        try:
            envelope = await bridge(definition.endpoint, provided)
        except Exception as error:  # noqa: BLE001 —— handler 之外的程序异常 → isError=true
            message = str(error)[:mcp_tools.MESSAGE_LIMIT] or error.__class__.__name__
            envelope = mcp_tools.failure(mcp_tools.TOOL_FAILED_CODE, message)
            if want_text:
                return mcp_tools.render_tool_result(definition.name, provided, envelope,
                                                    is_error=True)
            return mcp_tools.tool_result(envelope, is_error=True)
        if want_text:
            return mcp_tools.render_tool_result(definition.name, provided, envelope,
                                                is_error=False)
        return mcp_tools.tool_result(envelope, is_error=False)

    fn.__name__ = definition.name
    fn.__doc__ = definition.description
    parameters = [param.parameter() for param in definition.params]
    if mcp_tools.is_renderable(definition.name):
        parameters.append(mcp_tools.format_param().parameter())
    fn.__signature__ = inspect.Signature(parameters)
    return fn


# ---------------------------------------------------------------------------
# 渲染（FR-TOOLS-002 子规范②，桥接面 2026-09-21 扩展）：``format:"text"`` 的 v3_* 人类渲染
# ---------------------------------------------------------------------------
# 注册表**只有一份**（``mcp_tools.TOOL_RENDERERS``；基础面与桥接面共用同一个 dict、同一个
# ``mcp_tools.render_tool_result`` 分发）。本段只往它里面**追加** v3_* 条目——没有第二套
# 机制、没有第二份清单，``is_renderable`` 一个谓词统管 schema / 卡片 / 渲染三处。
#
# 每条都是 ``render(args, value) -> str`` **纯函数**：同一输入恒同一输出，无时钟、无 I/O、
# 无随机（``as_of``/时间戳一律取自信封原文，渲染器自己不取当前时间）。空数据一律走
# ``mcp_tools.render_no_data`` → 「无数据源·原因」，绝不放空串；``render_tool_result``
# 另有兜底：渲染器返回空串时回退规范 JSON。
#
# 传进来的 ``value`` 是**桥原样透传的扁平 v3 信封**（``{ok:true, …}`` / ``{ok:false, error}``），
# 与基础面 ``{ok, value}`` 的包装形态不同——因此本段只按扁平形态读键。
def _v3_failure(value):
    """``ok=false`` 的 v3 信封 → 失败原因文本（``code: message``）；``ok=true``/非信封 → None。"""
    if not isinstance(value, dict):
        return "响应不是标准信封"
    if value.get("ok"):
        return None
    return mcp_tools.render_error_text(value)


def _v3_tail(*parts):
    """尾注「a · b · c」；全空 → ``来源未标注``（宁可说没有，不编一个）。"""
    text = " · ".join(str(part) for part in parts if part not in (None, ""))
    return text or "来源未标注"


def _v3_rows(items, render, limit=6):
    """列表条目：前 ``limit`` 条 + 「…其余 N 条略」（结构化信封里始终有全量）。"""
    rows = list(items) if isinstance(items, list) else []
    lines = [render(item) for item in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"- …其余 {len(rows) - limit} 条略（结构化信封里有全量）")
    return lines


def _v3_reason(value, *keys):
    """空数据的原因：取信封自带的 ``reason``/``note``/``notes``（第一条），都没有 → ``None``。"""
    for key in keys:
        item = value.get(key) if isinstance(value, dict) else None
        if isinstance(item, list) and item:
            item = item[0]
        if item not in (None, ""):
            return str(item)
    return None


@mcp_tools.register_renderer("v3_market")
def _render_v3_market(args, value):
    """``/api/v3/market``：K 线根数 + 首尾两根 + 窗口涨跌（与基础面 ``series`` 同一口径）。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    data = value.get("data") if isinstance(value.get("data"), dict) else {}
    bars = data.get("bars") if isinstance(data.get("bars"), list) else []
    ticker = data.get("ticker") or args.get("ticker") or "?"
    period = data.get("period") or args.get("period") or "?"
    source = data.get("source") or "未知"
    as_of = data.get("as_of")
    tail_note = _v3_tail(f"来源 {source}", as_of and f"as_of {as_of}")
    if not bars:
        return mcp_tools.render_no_data(f"{ticker} 取不到 K 线（bars 为空；{tail_note}）")
    head, tail = bars[0], bars[-1]
    lines = [f"{ticker} {period} K 线：共 {data.get('count', len(bars))} 根"
             f"（{head.get('t')} → {tail.get('t')}；{tail_note}）",
             f"- 最新一根：t={tail.get('t')} O={mcp_tools.render_number(tail.get('o'))} "
             f"H={mcp_tools.render_number(tail.get('h'))} "
             f"L={mcp_tools.render_number(tail.get('l'))} "
             f"C={mcp_tools.render_number(tail.get('c'))} "
             f"V={mcp_tools.render_number(tail.get('v'), 0)}",
             f"- 窗口首根：t={head.get('t')} C={mcp_tools.render_number(head.get('c'))}"]
    closes = [bar.get("c") for bar in bars if isinstance(bar, dict)]
    numbers = [item for item in closes
               if isinstance(item, (int, float)) and not isinstance(item, bool)]
    if len(numbers) >= 2 and numbers[0]:
        change = (numbers[-1] / numbers[0] - 1) * 100
        lines.append(f"- 窗口累计涨跌：{mcp_tools.render_number(change, 2)}%（首→尾收盘）")
    return "\n".join(lines)


def _orderbook_side(rows, unit):
    """单侧档位 → 一行文本；两种真实形态都认：``[[price, volume]]`` 与 ``[{price, …}]``。"""
    parts = []
    for row in rows if isinstance(rows, list) else []:
        price = volume = count = None
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            price, volume = row[0], row[1]
        elif isinstance(row, dict):
            price, volume, count = row.get("price"), row.get("volume"), row.get("order_count")
        if price is None:
            continue
        text = (f"{mcp_tools.render_number(price, 3)}×"
                f"{mcp_tools.render_number(volume, 0)}")
        if count not in (None, ""):
            text += f"（{count} 单）"
        parts.append(text)
    return f"{' / '.join(parts)} {unit}".strip() if parts else None


@mcp_tools.register_renderer("v3_orderbook")
def _render_v3_orderbook(args, value):
    """``/api/v3/orderbook``：买卖档位（富途档位表 / 免密降级快照两种真实形态都认）。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    data = value.get("data")
    ticker = value.get("ticker") or args.get("ticker") or "?"
    book, name, source, as_of, unit, fallback = None, None, None, None, "股", None
    if isinstance(data, list) and data:  # 富途原样档位表：[{books:[{bid_list, ask_list}], code, name}]
        head = data[0] if isinstance(data[0], dict) else {}
        books = head.get("books") if isinstance(head.get("books"), list) else []
        book = books[0] if books and isinstance(books[0], dict) else None
        name = head.get("sc_name") or head.get("name")
        source = "futu/rt_order_book"
    elif isinstance(data, dict):  # 免密降级快照：{book:{bid, ask, depth, volume_unit}, quote, source}
        inner = data.get("book") if isinstance(data.get("book"), dict) else None
        if inner:
            book = {"bid_list": inner.get("bid"), "ask_list": inner.get("ask")}
            unit = str(inner.get("volume_unit") or "股")
        name = data.get("name")
        source = data.get("source")
        as_of = data.get("as_of")
        chain = data.get("futu_fallback")
        fallback = chain.get("reason") if isinstance(chain, dict) else None
    if not isinstance(book, dict) or not (book.get("bid_list") or book.get("ask_list")):
        return mcp_tools.render_no_data(
            f"{ticker} 的盘口为空或形态不认识（data={type(data).__name__}）；不猜档位，"
            "请核对上游行情权限与降级链说明")
    source_text = source or "未知"
    lines = [f"{ticker}{(' ' + str(name)) if name else ''} 盘口："
             f"{_v3_tail(f'来源 {source_text}', as_of and f'as_of {as_of}')}"]
    bids = _orderbook_side(book.get("bid_list"), unit)
    asks = _orderbook_side(book.get("ask_list"), unit)
    if bids:
        lines.append(f"- 买盘：{bids}")
    if asks:
        lines.append(f"- 卖盘：{asks}")
    if fallback:
        lines.append(f"- 降级说明：{fallback}")
    return "\n".join(lines)


@mcp_tools.register_renderer("v3_sentiment")
def _render_v3_sentiment(args, value):
    """``/api/v3/sentiment``：可解释情绪分 + 正负中性计数 + 高频词 + 事件聚合。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    symbol = value.get("symbol") or args.get("symbol") or "?"
    days = value.get("days")
    source = value.get("source") or "未知"
    as_of = value.get("as_of")
    documents = value.get("documents") or 0
    score = value.get("score")
    if not documents or score is None:
        reason = _v3_reason(value, "notes", "note")
        if not reason:
            reason = (f"{symbol} 近 {days} 天没有可打分的资讯"
                      f"（documents={documents}，score={score}）——空窗不是 0 分")
        return mcp_tools.render_no_data(f"{reason}；来源 {source}，as_of {as_of}")
    lines = [f"{symbol} 资讯情绪（近 {days} 天：{documents} 篇，已打分 {value.get('scored')} 篇；"
             f"{_v3_tail(f'来源 {source}', as_of and f'as_of {as_of}')}）",
             f"- 综合分 score={mcp_tools.render_number(score)}"
             f"（覆盖 {mcp_tools.render_number(value.get('coverage'), 3)}；"
             f"正 {value.get('positive')} / 负 {value.get('negative')} / "
             f"中性 {value.get('neutral')}；半衰 {mcp_tools.render_number(value.get('half_life_hours'), 1)}h）",
             f"- 最新一条：{value.get('latest_at') or '未标注'}"
             + (f"；无时间戳 {value.get('undated')} 条（保留但单列）"
                if value.get("undated") else "")]
    terms = [item for item in (value.get("top_terms") or []) if isinstance(item, dict)]
    if terms:
        lines.append("- 高频词：" + "、".join(
            f"{item.get('term')}({mcp_tools.render_number(item.get('polarity'), 2)}×{item.get('count')})"
            for item in terms[:6]))
    events = [item for item in (value.get("events") or []) if isinstance(item, dict)]
    if events:
        lines.append("- 事件：" + "、".join(
            f"{item.get('label') or item.get('type')} {item.get('count')} 条"
            f"（最近 {item.get('latest_at')}）" for item in events[:5]))
    else:
        lines.append("- 事件：本窗口未识别到规则事件（引擎 "
                     f"{value.get('event_method')}/{value.get('event_version')}）")
    for note in (value.get("notes") or [])[:3]:
        lines.append(f"- 备注：{note}")
    return "\n".join(lines)


@mcp_tools.register_renderer("v3_factors_matrix")
def _render_v3_factors_matrix(args, value):
    """``/api/v3/factors/matrix``：标的/因子列 + 非空格数 + IC 统计 + 逐因子覆盖率。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    matrix = value.get("matrix") if isinstance(value.get("matrix"), dict) else {}
    tickers = matrix.get("tickers") if isinstance(matrix.get("tickers"), list) else []
    factors = matrix.get("factors") if isinstance(matrix.get("factors"), list) else []
    rows = matrix.get("matrix") if isinstance(matrix.get("matrix"), list) else []
    if not tickers or not factors or not rows:
        return mcp_tools.render_no_data(
            f"因子矩阵为空（tickers={len(tickers)} / factors={len(factors)} / rows={len(rows)}；"
            f"market={value.get('market') or args.get('market') or '未指定'}）")
    filled = total = 0
    for row in rows:
        if not isinstance(row, list):
            continue
        total += len(row)
        filled += sum(1 for cell in row
                      if isinstance(cell, (int, float)) and not isinstance(cell, bool))
    lines = [f"横截面因子矩阵（market={value.get('market') or args.get('market') or '未指定'}，"
             f"asOf={value.get('asOf') or '未知'}，matrix.as_of={matrix.get('as_of') or '未知'}；"
             f"来源 {matrix.get('source') or '未知'}）",
             f"- 标的 {len(tickers)} 只：{', '.join(map(str, tickers))}",
             f"- 因子列 {len(factors)} 列：{', '.join(map(str, factors[:12]))}"
             + ("…" if len(factors) > 12 else ""),
             f"- 非空格 {filled}/{total or (len(tickers) * len(factors))}"
             "（缺的格是 null，不是 0）"]
    ic = value.get("ic") if isinstance(value.get("ic"), dict) else {}
    if ic.get("ok"):
        lines.append(f"- IC（factor={ic.get('factor') or '缺省'}，"
                     f"forward={ic.get('forwardDays')}）：{ic.get('observations')} 期，"
                     f"meanIc={mcp_tools.render_number(ic.get('meanIc'))}，"
                     f"ir={mcp_tools.render_number(ic.get('ir'))}，"
                     f"latestIc={mcp_tools.render_number(ic.get('latestIc'))}"
                     + (f"（来源 {ic.get('source')}）" if ic.get("source") else ""))
    elif ic:
        lines.append(f"- IC：不可用（{mcp_tools.render_error_text(ic)}）")
    else:
        lines.append("- IC：信封里没有 ic 块")
    partial = [item for item in (value.get("factors") or [])
               if isinstance(item, dict) and (item.get("coveragePct") or 0) < 100]
    lines += _v3_rows(partial, lambda item: (
        f"- 覆盖不足：{item.get('key')}（{item.get('classLabel') or item.get('class')}）"
        f"{mcp_tools.render_number(item.get('coveragePct'), 1)}%"
        f"（{item.get('covered')}/{item.get('total')}）"
        + (f"；缺 {', '.join(map(str, (item.get('missingTickers') or [])[:4]))}"
           if item.get("missingTickers") else "")), limit=4)
    missing = [item for item in (value.get("factorsMissing") or []) if isinstance(item, dict)]
    if missing:
        lines.append(f"- 未并入的因子 {len(missing)} 项：" + "；".join(
            f"{item.get('key')}（{str(item.get('reason') or '原因未给出')[:70]}）"
            for item in missing[:3]))
    else:
        lines.append("- 未并入的因子：无（factorsMissing 为空）")
    sources = value.get("sources") if isinstance(value.get("sources"), dict) else {}
    lines.append("- 数据来源：" + "；".join(
        f"{key}={str(sources[key])[:100] + ('…' if len(str(sources[key])) > 100 else '')}"
        for key in sorted(sources) if sources.get(key)))
    return "\n".join(lines)


@mcp_tools.register_renderer("v3_risk_analytics")
def _render_v3_risk_analytics(args, value):
    """``/api/v3/risk/analytics``：VaR/CVaR/Beta/Alpha/IR/Kupiec + 组合与基准来源。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    analytics = value.get("analytics") if isinstance(value.get("analytics"), dict) else {}
    if not analytics:
        return mcp_tools.render_no_data(
            "风险量块为空（analytics 缺失；组合 "
            + str(value.get("portfolioSource") or "未标注") + "）")
    window = analytics.get("window") if isinstance(analytics.get("window"), dict) else {}
    kupiec = analytics.get("kupiec") if isinstance(analytics.get("kupiec"), dict) else {}
    lines = [f"组合风险量（confidence={mcp_tools.render_number(analytics.get('confidence'), 2)}，"
             f"观察 {analytics.get('observations')} 个交易日"
             + (f"，{window.get('from')} → {window.get('to')}" if window else "")
             + "）",
             f"- 组合：{value.get('portfolioSource') or '未标注'}；"
             f"nav={mcp_tools.render_number(value.get('nav'), 2)}"
             + (f"（{value.get('navNote')}）" if value.get("navNote") else ""),
             f"- 基准：{value.get('benchmarkTicker') or value.get('benchmark') or 'null（不可用）'}"
             + (f"（来源 {value.get('benchmarkSource')}）" if value.get("benchmarkSource") else "")]
    lines.append(f"- VaR(日)={mcp_tools.render_number(analytics.get('varDailyPct'), 2)}% · "
                 f"CVaR(日)={mcp_tools.render_number(analytics.get('cvarDailyPct'), 2)}%"
                 + (f" · 金额 VaR={mcp_tools.render_number(analytics.get('varAmount'), 2)} / "
                    f"CVaR={mcp_tools.render_number(analytics.get('cvarAmount'), 2)}"
                    if analytics.get("varAmount") is not None else ""))
    lines.append(f"- 年化波动={mcp_tools.render_number(analytics.get('annVolPct'), 2)}% · "
                 f"年化收益={mcp_tools.render_number(analytics.get('annReturnPct'), 2)}% · "
                 f"最大回撤={mcp_tools.render_number(analytics.get('maxDrawdownPct'), 2)}% · "
                 f"基准年化={mcp_tools.render_number(analytics.get('benchmarkAnnReturnPct'), 2)}%")
    lines.append(f"- Beta={mcp_tools.render_number(analytics.get('beta'), 3)} · "
                 f"Alpha(年化)={mcp_tools.render_number(analytics.get('alphaAnnPct'), 2)}% · "
                 f"IR={mcp_tools.render_number(analytics.get('ir'), 3)}")
    if kupiec:
        lines.append(f"- Kupiec POF：突破 {kupiec.get('breaches')}/{kupiec.get('observations')} 次，"
                     f"LR={mcp_tools.render_number(kupiec.get('lr'), 4)}，"
                     f"p={mcp_tools.render_number(kupiec.get('pValue'), 4)} → "
                     + ("通过" if kupiec.get("pass") else "未通过"))
    curve = analytics.get("equityCurve") if isinstance(analytics.get("equityCurve"), list) else []
    if curve:
        lines.append(f"- 净值曲线：{len(curve)} 个点（{curve[0].get('t')} → {curve[-1].get('t')}）")
    detail = value.get("risk_detail") if isinstance(value.get("risk_detail"), dict) else {}
    leverage = detail.get("leverage") if isinstance(detail.get("leverage"), dict) else {}
    if leverage:
        lines.append(
            f"- 杠杆率：持仓市值/总资产={mcp_tools.render_number(leverage.get('leverage_ratio_pct'), 2)}% · "
            f"购买力/总资产={mcp_tools.render_number(leverage.get('buying_power_ratio_pct'), 2)}% · "
            f"融资负债={mcp_tools.render_number(leverage.get('margin_debt_pct'), 2)}"
            f"（来源 {leverage.get('source') or '未知'}，as_of {leverage.get('as_of') or '未知'}）")
    liquidity = detail.get("liquidity") if isinstance(detail.get("liquidity"), dict) else {}
    if liquidity:
        lines.append(
            f"- 流动性：名义单 {mcp_tools.render_number(liquidity.get('order_value'), 2)} / "
            f"近 {liquidity.get('adv_window_days')} 日 ADV "
            f"{mcp_tools.render_number(liquidity.get('adv_amount'), 2)} → 参与率 "
            f"{mcp_tools.render_number(liquidity.get('participation_pct'), 4)}%（分级 "
            f"{liquidity.get('grade') or '未给出'}）")
    errors = (value.get("sources") or {}).get("errors") if isinstance(value.get("sources"), dict) else None
    if errors:
        lines.append(f"- 取数错误 {len(errors)} 条：" + "；".join(
            str(item)[:80] for item in errors[:3]))
    if detail.get("errors"):
        lines.append(f"- risk_detail 子项失败 {len(detail['errors'])} 条（主区块不受影响）："
                     + "；".join(str(item)[:80] for item in detail["errors"][:3]))
    return "\n".join(lines)


@mcp_tools.register_renderer("v3_risk_industry")
def _render_v3_risk_industry(args, value):
    """``/api/v3/risk/industry``：行业暴露排序 + 红线判定 + 未取到行业的标的。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    exposures = [item for item in (value.get("exposures") or []) if isinstance(item, dict)]
    missing = [item for item in (value.get("missing") or []) if isinstance(item, dict)]
    if not exposures:
        reason = "；".join(f"{item.get('ticker')}：{item.get('reason')}" for item in missing[:3])
        return mcp_tools.render_no_data(
            "没有任何标的取到行业分类"
            + (f"（{reason}）" if reason else f"（market={value.get('market') or '未过滤'}）"))
    sources = value.get("sources") if isinstance(value.get("sources"), dict) else {}
    top = value.get("top") if isinstance(value.get("top"), dict) else {}
    lines = [f"行业暴露（as_of={value.get('as_of') or '未知'}，"
             f"market={value.get('market') or '未过滤'}，红线 {mcp_tools.render_number(value.get('limitPct'), 1)}% → "
             + ("**超限**" if value.get("breach") else "未超限") + "；"
             + _v3_tail(f"板块来源 {sources.get('plate') or '未知'}",
                        f"权重来源 {sources.get('weights') or '未知'}") + "）",
             f"- 最大暴露：{top.get('industry')} {mcp_tools.render_number(top.get('weightPct'), 2)}%"]
    lines += _v3_rows(exposures, lambda item: (
        f"- {item.get('industry')}：{mcp_tools.render_number(item.get('weightPct'), 2)}%"
        f"（标的 {len(item.get('tickers') or [])} 只"
        + (f"，市值 {mcp_tools.render_number(item.get('value'), 2)}"
           if item.get("value") is not None else "，市值 null（拿不到权益，不估算）")
        + "）"), limit=8)
    if missing:
        lines.append(f"- 未取到行业 {len(missing)} 只：" + "；".join(
            f"{item.get('ticker')}（{str(item.get('reason') or '原因未给出')[:60]}）"
            for item in missing[:3]) + ("…" if len(missing) > 3 else ""))
    for note in (value.get("notes") or [])[:3]:
        lines.append(f"- 备注：{note}")
    return "\n".join(lines)


@mcp_tools.register_renderer("v3_strategy")
def _render_v3_strategy(args, value):
    """``/api/v3/strategy``：最近一轮研究流水线的阶段读数与调仓提案（只是提案）。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    run = value.get("run") if isinstance(value.get("run"), dict) else None
    if run is None:
        return mcp_tools.render_no_data(
            str(value.get("note") or "尚未运行研究流水线")
            + (f"（market={value.get('market')}）" if value.get("market") else ""))
    universe = run.get("universe") if isinstance(run.get("universe"), list) else []
    proposals = [item for item in (run.get("proposals") or []) if isinstance(item, dict)]
    stages = run.get("stages") if isinstance(run.get("stages"), dict) else {}
    lines = [f"研究流水线最近一轮（asOf={run.get('asOf') or '未知'}，"
             f"market={run.get('market') or '未标注'}，宇宙 {len(universe)} 只"
             + (f"：{', '.join(map(str, universe[:8]))}" if universe else "")
             + f"；来源 {run.get('universe_source') or '未标注'}）"]
    if proposals:
        lines.append(f"- 调仓提案 {len(proposals)} 条（提案不是委托，执行走工作台受约束入口）：")
        lines += _v3_rows(proposals, lambda item: (
            f"  - {item.get('action')} {item.get('ticker')} → 目标 "
            f"{mcp_tools.render_number(item.get('targetWeightPct'), 2)}%"
            f"（风险 {item.get('riskLevel') or '未标注'}）｜依据：{str(item.get('basis') or '')[:90]}"),
            limit=8)
    else:
        lines.append("- 调仓提案：0 条（本轮没有提案）")
    order = ("PDAT", "PAAT", "PCPT", "PRT", "PET")
    detail = []
    pdat = stages.get("PDAT") if isinstance(stages.get("PDAT"), dict) else {}
    if pdat:
        detail.append(f"PDAT bars={pdat.get('bars')}（errors={len(pdat.get('errors') or [])}）")
    paat = stages.get("PAAT") if isinstance(stages.get("PAAT"), dict) else {}
    if paat:
        detail.append(f"PAAT 分析 {paat.get('analyzed')}"
                      f"（有因子 {paat.get('withFactors')}，scoreSource={paat.get('scoreSource') or '未标注'}）")
    pcpt = stages.get("PCPT") if isinstance(stages.get("PCPT"), dict) else {}
    if pcpt:
        detail.append(f"PCPT 多头 {len(pcpt.get('longs') or [])} / 减仓 {len(pcpt.get('reduces') or [])}")
    prt = stages.get("PRT") if isinstance(stages.get("PRT"), dict) else {}
    if prt:
        detail.append(f"PRT 单只权重 {mcp_tools.render_number(prt.get('weightPctPerName'), 2)}%"
                      f"（capped={prt.get('capped')}）")
    pet = stages.get("PET") if isinstance(stages.get("PET"), dict) else {}
    if pet:
        detail.append(f"PET 提案 {pet.get('proposals')}")
    if detail:
        lines.append("- 阶段：" + " · ".join(detail))
    extra = [key for key in sorted(stages) if key not in order]
    lines.append(("- 其余阶段键：" + ", ".join(extra)) if extra
                 else "- 阶段键：PDAT→PET 五阶段齐全")
    return "\n".join(lines)


@mcp_tools.register_renderer("v3_research")
def _render_v3_research(args, value):
    """``/api/v3/research``：研报 / 研究 run / 量化预览 / 活动流的条数与最近几条。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    groups = [(key, label) for key, label in (("runs", "研究 run"), ("reports", "研报"),
                                              ("previews", "量化预览"), ("activity", "活动"))]
    counts = {key: len(value.get(key) or []) for key, _label in groups}
    if not any(counts.values()):
        return mcp_tools.render_no_data(
            f"工作台快照里没有 runs/reports/previews/activity 记录"
            f"（mode={value.get('mode') or '未知'}，generated_at={value.get('generated_at') or '未知'}"
            + (f"，market={value.get('market')}" if value.get("market") else "") + "）")
    lines = [f"研究工作台视图（mode={value.get('mode') or '未知'}，"
             f"generated_at={value.get('generated_at') or '未知'}；"
             f"来源 {value.get('source') or '未知'}）",
             "- 计数：" + " · ".join(f"{label} {counts[key]} 条" for key, label in groups)]
    reports = [item for item in (value.get("reports") or []) if isinstance(item, dict)]
    if reports:
        lines.append("- 最近研报：" + "；".join(
            f"{item.get('ticker')}《{_headline(item.get('report'))}》"
            f"评级 {item.get('rating_label') or item.get('rating') or '未标注'}"
            f"（published_at={item.get('published_at') or '未知'}）" for item in reports[:3]))
    runs = [item for item in (value.get("runs") or []) if isinstance(item, dict)]
    if runs:
        lines.append("- 最近研究 run：" + "；".join(
            f"{item.get('ticker')} {item.get('status') or '未标注'}"
            f"（started_at={item.get('started_at') or '未知'}）" for item in runs[:3]))
    previews = [item for item in (value.get("previews") or []) if isinstance(item, dict)]
    if previews:
        lines.append("- 最近量化预览：" + "；".join(
            f"{_preview_ticker(item)} {_preview_label(item)}"
            f"（{_preview_date(item)}）" for item in previews[:3]))
    activity = [item for item in (value.get("activity") or []) if isinstance(item, dict)]
    if activity:
        lines.append("- 活动流最近：" + "；".join(
            f"{item.get('kind') or '未标注'} {item.get('ticker') or '—'}"
            f"（at={item.get('at') or '未知'}）" for item in activity[:3]))
    if value.get("filter") and isinstance(value.get("filter"), dict):
        stats = value["filter"]
        lines.append(f"- 市场过滤（market={value.get('market')}）：命中 {stats.get('keptRuns')} run / "
                     f"{stats.get('keptReports')} 研报 / {stats.get('keptPreviews')} 预览 / "
                     f"{stats.get('keptActivity')} 活动；无法归属 "
                     f"{stats.get('unattributed')} 条" + (f"；{stats.get('note')}"
                                                          if stats.get("note") else ""))
    for key in ("notice", "recording_error"):
        if value.get(key):
            lines.append(f"- {key}：{value[key]}")
    return "\n".join(lines)


def _headline(markdown):
    """研报正文 → 标题（首个非空行去掉 ``#``/引用标记，截断 60 字）；取不到 → ``无标题``。"""
    for line in str(markdown or "").splitlines():
        text = line.strip().lstrip("#").lstrip(">").strip()
        if text:
            return text[:60] + ("…" if len(text) > 60 else "")
    return "无标题"


def _preview_ticker(item):
    """量化预览的标的（可能在 ``value`` 里）；取不到 → ``—``。"""
    inner = item.get("value") if isinstance(item.get("value"), dict) else {}
    return str(item.get("ticker") or inner.get("ticker") or "—")


def _preview_label(item):
    """量化预览的信号标签（``value.signal_label`` 优先）；取不到 → ``—``。"""
    inner = item.get("value") if isinstance(item.get("value"), dict) else {}
    return str(inner.get("signal_label") or inner.get("signal") or item.get("kind") or "—")


def _preview_date(item):
    """量化预览的时点（``value.date`` 优先）；取不到 → ``—``。"""
    inner = item.get("value") if isinstance(item.get("value"), dict) else {}
    return str(inner.get("date") or item.get("at") or "—")


@mcp_tools.register_renderer("v3_ops_alerts")
def _render_v3_ops_alerts(args, value):
    """``/api/v3/ops/alerts``：平台内规则求值的三态（firing/pending/ok/no-data/unsupported）。"""
    failed = _v3_failure(value)
    if failed:
        return mcp_tools.render_no_data(failed)
    alerts = [item for item in (value.get("alerts") or []) if isinstance(item, dict)]
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
    if not alerts:
        return mcp_tools.render_no_data(
            f"规则求值结果为空（summary.total={summary.get('total')}；"
            + (f"state={args.get('state')} 过滤后没有命中的规则" if args.get("state")
               else "规则清单为空或采集未就绪") + "）")
    lines = [f"平台内告警三态（as_of={value.get('as_of') or '未知'}，"
             f"模型 stale={value.get('model_stale')}）：共 {summary.get('total', len(alerts))} 条规则 —— "
             + " / ".join(f"{state} {summary.get(state, 0)}"
                          for state in ("firing", "pending", "ok", "no-data", "unsupported")),
             f"- 规则源：{value.get('rules_file') or '未知'}"
             f"（{value.get('rules_file_policy') or '策略未标注'}）"]
    for state in ("firing", "pending", "no-data", "unsupported"):
        picked = [item for item in alerts if item.get("state") == state]
        if not picked:
            continue
        lines.append(f"- {state}（{len(picked)} 条）：")
        lines += _v3_rows(picked, lambda item: (
            f"  - {item.get('rule')}［{item.get('severity') or '未标注'}/"
            f"{item.get('domain') or '未标注'}］"
            + (f" {item.get('metric')}{item.get('operator')}{mcp_tools.render_number(item.get('threshold'), 4)}"
               f"，实测 {mcp_tools.render_number(item.get('value'), 4)}"
               if item.get("metric") else "")
            + f"；for={item.get('for') or '0'}，自 {item.get('since') or '未知'}"), limit=4)
    evaluator = value.get("evaluator") if isinstance(value.get("evaluator"), dict) else {}
    if evaluator:
        lines.append(f"- 求值窗口：历史 {mcp_tools.render_number(evaluator.get('history_seconds'), 0)}s，"
                     f"序列 {evaluator.get('series')} 条 / {evaluator.get('points')} 点，"
                     f"采样间隔 {mcp_tools.render_number(evaluator.get('sample_seconds'), 1)}s")
    if value.get("collect_error"):
        lines.append(f"- 采集错误：{value['collect_error']}")
    return "\n".join(lines)


def register(server, app, bridge=None):
    """把 ``/api/v3/*`` 全部注册成 MCP 工具，返回 ``(bridge, bound_names)``。

    ``server`` 是 ``create_app`` 里那个唯一的 ``MCPServer``（与既有 77 工具同一实例、同一
    ``/mcp`` 端点）；``app`` 是刚装配完 V3 路由的 FastAPI 应用。

    注册的函数对象**每个定义只造一次**并记进 ``bridge.bound``：discovery 模式下
    ``mcp_discovery.register`` 直接复用这一批，不另造一件——因此「直连的 v3_x」与
    「call_tool 转发的 v3_x」在进程里是同一个函数对象。
    """
    from mcp.types import ToolAnnotations

    bridge = bridge or V3Bridge(app)
    with mcp_tools.schema_warning_filter():
        for definition in bridge.definitions:
            bound = _bind(definition, bridge)
            bridge.bound[definition.name] = bound
            server.add_tool(
                bound,
                name=definition.name,
                description=definition.description,
                annotations=ToolAnnotations(
                    readOnlyHint=definition.endpoint not in NON_READONLY_PATHS,
                    destructiveHint=False,
                    idempotentHint=definition.endpoint not in NON_READONLY_PATHS,
                    openWorldHint=True,
                ),
                # FR-TOOLS-002 子规范：``isConcurrencySafe`` 经 ``_meta`` 暴露（与
                # ``mcp_tools``/``mcp_discovery`` 同一份键与判定）。原先只有 discovery 代理
                # 面补齐，**direct 模式下这 51 件 v3_* 没有 meta**；此处补上后 direct 面
                # 128 件全覆盖。判定只看"是否只读"（桥接件无共享可变状态）。
                meta=mcp_tools.concurrency_meta(
                    definition.endpoint not in NON_READONLY_PATHS),
                structured_output=False,
            )
        # 与 77 工具同一套封闭性保证（additionalProperties:false），只作用于本模块注册的名字。
        # 放在同一个告警静音块里：``model_rebuild(force=True)`` + ``model_json_schema`` 会
        # 重新生成 schema，UNSET 哨兵默认值的 non-serializable-default 告警就是从这里发的。
        mcp_tools.forbid_extra_fields(server, bridge.names)
    return bridge, bridge.names


__all__ = [
    "BINARY_INLINE_LIMIT", "BLOCKED_ACTIONS", "CREDENTIALS_PATH", "CREDENTIALS_WEB_ONLY_CODE",
    "CREDENTIALS_WEB_ONLY_MESSAGE", "METHOD_BY_ACTION", "NON_READONLY_PATHS", "PARAM_DOCS",
    "PARAM_DOCS_OVERRIDES", "REQUEST_PARAMS", "TOOL_DOCS", "V3Bridge", "V3_PREFIX",
    "build_definitions", "inproc_bridge_active", "register", "to_envelope", "tool_name",
]
