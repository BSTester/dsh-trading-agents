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
    """
    async def fn(**kwargs):
        provided = {key: value for key, value in kwargs.items() if value is not mcp_tools.UNSET}
        try:
            envelope = await bridge(definition.endpoint, provided)
        except Exception as error:  # noqa: BLE001 —— handler 之外的程序异常 → isError=true
            message = str(error)[:mcp_tools.MESSAGE_LIMIT] or error.__class__.__name__
            return mcp_tools.tool_result(mcp_tools.failure(mcp_tools.TOOL_FAILED_CODE, message),
                                         is_error=True)
        return mcp_tools.tool_result(envelope, is_error=False)

    fn.__name__ = definition.name
    fn.__doc__ = definition.description
    fn.__signature__ = inspect.Signature([param.parameter() for param in definition.params])
    return fn


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
