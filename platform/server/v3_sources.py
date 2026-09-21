"""外部数据源 V3 接口（``/api/v3/*`` 的数据源半边）：富途 f10 / AKShare / SEC EDGAR / OpenBB。

（2026-09-21 数据源政策：除富途（授权使用）外一律免密钥公开端点——Tushare Pro 需要
token，已按政策**整体移除**；其能力由免密源覆盖：财务=富途 f10/东财·新浪（AKShare）、
宏观与北向=AKShare（见 ``server/v3_sources_ext.py``）。）

设计约束（与规格 FR-DATA-002 及仓库「数据诚实」纪律一致）：

  * **绝不伪造**。上游取不到就把**真实错误文本**放进 ``{ok:false,error:{code,message}}``，
    不返回估算值、不返回空壳成功。
  * **所有外部调用都带超时**。HTTP 层用 ``httpx.Timeout``（缺 httpx 时退到
    ``urllib.request`` 并传 ``timeout``）；akshare 这类不接受超时参数的三方函数用
    ``socket.setdefaulttimeout`` 兜住阻塞点（akshare 内部走 requests，会读这个默认值）。
  * **持久化/惰性**。``register()`` 只挂路由，**不 import** akshare / openbb / pandas：
    那些库导入以十秒计，服务启动与测试套件都不能被它们拖住。一切三方 import 都在
    请求处理路径内按需发生，且可被注入的假模块/假 fetch 替换（见 ``deps``）。

``/api/v3/financials`` 的**按市场路由 + 降级链**（见 ``financials_with_chain``）::

    AAPL / US.*        → SEC EDGAR（us-gaap XBRL，字段与既有响应完全兼容）
    SH.600000 / HK.00700 → 富途 f10_detail/statements（**主源**，逐期逐科目）
                           → AKShare（免密钥；A 股 stock_financial_abstract、港股按版本）
                           → 全失败如实报错，错误里带 ``chain`` 写明每一级为什么没成

参数注入（``register(app, v3_run, home, deps=None)``，前三个是约定签名；``deps`` 仅供测试）::

    deps = {
      "fetch_json":  callable(url) -> {"ok":True,"value":obj} | {"ok":False,"error":{...}},
      "akshare":     module-like | callable() -> module-like,
      "openbb":      module-like | callable() -> module-like,
      "env":         mapping-like（默认 os.environ）,
      "timeout":     float，
    }

``deps`` 里任何一项缺失都回落到真实实现，因此生产路径只写 ``register(app, v3_run, home)``。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import ModuleType


class ModuleImportTimeout(Exception):
    """三方模块 import 超过预算（见 ``Deps.module``）。"""

# 与 V3 原型（已退役）实现 的 STATEMENT_TAGS 逐项对齐：三表标签集合是**契约**，
# 前端按 statement 分组渲染，这里少一个标签或多一个标签都会让展示与规格不符。
STATEMENT_TAGS = {
    "income": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "GrossProfit",
        "OperatingIncomeLoss",
        "NetIncomeLoss",
        "EarningsPerShareDiluted",
    ),
    "balance": (
        "Assets",
        "Liabilities",
        "StockholdersEquity",
        "CashAndCashEquivalentsAtCarryingValue",
        "LongTermDebtNoncurrent",
    ),
    "cashflow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInFinancingActivities",
        "PaymentsToAcquirePropertyPlantAndEquipment",
    ),
}

# 只认定期报告：这些 form 才有可比口径（8-K/13F 之类会把口径搅浑）。
SEC_FORMS = ("10-K", "10-Q", "20-F", "6-K")

# 申报结构变化会让旧标签停用（实测 AAPL 的 ``Revenues`` 最新期停在 2018-09-29，
# 之后改用 RevenueFromContractWithCustomerExcludingAssessedTax）。超过这个天数即标记
# ``stale``，由调用方判断——不把旧期当当期用。
SEC_STALE_DAYS = 400

DEFAULT_SEC_UA = "quant-platform-v3 (ops@example.com)"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
SEC_TICKERS_CACHE = "sec_company_tickers.json"

DEFAULT_TIMEOUT = 25.0
# openbb 的 import 本身以十秒计（本机实测冷启动 ~44-57s），且它的 provider 取数另计，
# 所以要一条独立的长超时；短超时会把「能取到」误判成失败。
OPENBB_TIMEOUT = 180.0
OPENBB_IMPORT_TIMEOUT = 150.0

# tushare API 表已随 Tushare Pro 移除（2026-09-21 数据源政策：需要 token 的数据渠道一律
# 不再引用；能力由富途 f10 / AKShare / SEC EDGAR 免密覆盖）。

#: ``/api/v3/financials`` 的三表 → 富途 ``f10_detail`` 的 ``statements.statement_type``
#: （官方 1~4：1=利润表 2=资产负债表 3=现金流量表 4=主要指标）。只有前三者是「三表」口径。
#:
#: 为什么是 ``statements``：先列出富途 ``f10_detail`` 支持的 **26 个 section**（白名单单一
#: 事实源 = ``trading_datasource.futu_openapi.groups.f10.OpenApiF10.SECTIONS``）——
#:   财务：earnings_price_move / earnings_price_history / **statements** / revenue_breakdown
#:   研究：analyst_consensus / rating_summary / morningstar
#:   估值：valuation_detail / valuation_plate_stocks / valuation_index_stocks /
#:         valuation_index_stock_plates
#:   公司行为：dividends / buybacks / splits
#:   股东：shareholders_overview / holding_changes / holder_detail / institutional /
#:         insider_holders / insider_trades
#:   公司信息：company_profile / company_executives / company_executive_background /
#:             company_operational_efficiency
#:   经纪商：top_brokers / top_brokers_history
#: 其中只有 ``statements`` 给出**逐期逐科目的三表数据**（``report_list[].item_list``）；
#: ``earnings_price_move`` 是财报日涨跌、``revenue_breakdown`` 是营收构成、
#: ``valuation_detail`` 是估值倍数——都不是三表口径，因此不拿它们顶替（宁可报缺口）。
FUTU_STATEMENT_TYPES = {"income": 1, "balance": 2, "cashflow": 3}

#: 三表 → 富途报表结构名（仅用于响应里的可读标注，不参与取数）。
FUTU_STATEMENT_LABELS = {"income": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}

#: 裸 6 位 A 股代码的首位 → 交易所（公开编码规则，不做业务猜测）：
#: 6xx/9xx=上交所（含 B 股），0xx/2xx/3xx=深交所，4xx/8xx=北交所。
A_SHARE_HEAD_MARKET = {
    "6": "SH", "9": "SH", "5": "SH", "1": "SH",
    "0": "SZ", "2": "SZ", "3": "SZ",
    "4": "BJ", "8": "BJ",
}

#: ``/api/v3/financials`` 的降级链预算（秒）：三级源都没回应就当全失败。
FINANCIALS_CHAIN_TIMEOUT = 90.0

#: 市场 → 时区（把富途的毫秒时间戳折算成正确的**本地**报表期末）。
MARKET_TZ = {
    "SH": "Asia/Shanghai",
    "SZ": "Asia/Shanghai",
    "BJ": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}


# ── 通用工具 ────────────────────────────────────────────────────────────────────


def envelope_error(code, message):
    """``{ok:false,error:{code,message}}``——错误码前缀说明是哪一层失败的。"""
    return {"ok": False, "error": {"code": code, "message": message}}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def to_int(raw, default, minimum=1, maximum=500):
    """查询串一律是字符串；坏值回落到默认值，不抛 422（前端只认统一信封）。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def as_text(value):
    """单元格 → 字符串。空值返回 ``""`` 而不是 ``"nan"``/``"None"``（前端直接渲染）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN：pandas 缺值
        return ""
    text = str(value).strip()
    return "" if text in ("nan", "None", "NaT", "<NA>") else text


def as_number(value):
    """单元格 → float / None。非数值（含 NaN、空串）返回 ``None``，**不返回 0 冒充**。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    text = as_text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _error_text(error):
    """把异常压成一句**真实**文本（带类型名，便于对着日志定位）。"""
    message = str(error).strip() or repr(error)
    return f"{type(error).__name__}: {message}"


# ── HTTP（全部带超时）──────────────────────────────────────────────────────────


def _urllib_get_json(url, headers, timeout):
    import urllib.request  # 惰性：只在 httpx 不可用时才需要

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 —— 仅内网/公开数据源
        raw = response.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def http_get_json(url, headers=None, timeout=None):
    """GET + JSON。返回 ``{ok,value}`` 或 ``{ok:false,error}``；**永不抛异常**。"""
    headers = dict(headers or {})
    timeout = float(timeout or DEFAULT_TIMEOUT)
    try:
        try:
            import httpx
        except ImportError:
            return {"ok": True, "value": _urllib_get_json(url, headers, timeout)}
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            response = client.get(url, headers=headers)
        if response.status_code != 200:
            return envelope_error(f"sec/http-{response.status_code}", f"{url} → HTTP {response.status_code}")
        return {"ok": True, "value": response.json()}
    except Exception as error:  # noqa: BLE001 —— 失败进 error 信封是本模块的统一口径
        return envelope_error("sec/network", f"{url} → {_error_text(error)}")


class _SocketTimeoutGuard:
    """给不接受超时参数的三方调用兜一条硬超时（akshare 内部走 requests → 读默认 socket 超时）。

    这不是精确的取消机制（超时后第三方线程仍可能活着），但它把「无限挂住」变成「有限等待
    后如实报错」，比没有超时强得多。进程级默认值按调用次数引用计数，嵌套时不会互相踩。
    """

    _lock = threading.Lock()
    _depth = 0
    _saved = None

    def __init__(self, seconds):
        self.seconds = float(seconds)

    def __enter__(self):
        with _SocketTimeoutGuard._lock:
            if _SocketTimeoutGuard._depth == 0:
                _SocketTimeoutGuard._saved = socket.getdefaulttimeout()
                socket.setdefaulttimeout(self.seconds)
            _SocketTimeoutGuard._depth += 1
        return self

    def __exit__(self, *_exc):
        with _SocketTimeoutGuard._lock:
            _SocketTimeoutGuard._depth -= 1
            if _SocketTimeoutGuard._depth == 0:
                socket.setdefaulttimeout(_SocketTimeoutGuard._saved)
        return False


# ── 共享依赖容器 ───────────────────────────────────────────────────────────────


class Deps:
    """一次 ``register()`` 一份的可注入依赖；测试用假 fetch/假模块替换网络面。"""

    def __init__(
        self,
        fetch_json=None,
        akshare=None,
        openbb=None,
        env=None,
        home=None,
        timeout=None,
        openbb_timeout=None,
        akshare_retry=None,
    ):
        self._fetch_json = fetch_json
        self._akshare = akshare
        self._openbb = openbb
        self._env = env
        self._module_cache = {}
        self.home = None if home is None else str(home)
        self.timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
        self.openbb_timeout = float(openbb_timeout if openbb_timeout is not None else OPENBB_TIMEOUT)
        #: AKShare 重试策略覆盖项（``attempts``/``base_ms``/``max_ms``/``sleep``/``clock``/
        #: ``rand``/``is_empty``），逐项透传给 ``v3_fallback.retry_akshare``。缺省空 dict →
        #: 用 ``v3_fallback`` 的默认值（3 次 / 1200ms 起 / 8000ms 封顶 / 真 sleep）。
        self.akshare_retry = dict(akshare_retry or {})

    # env -------------------------------------------------------------------
    @property
    def env(self):
        return os.environ if self._env is None else self._env

    def get_env(self, name, default=None):
        try:
            value = self.env.get(name)
        except Exception:  # noqa: BLE001 —— 假 env 形态不保证
            return default
        return default if value in (None, "") else value

    # HTTP ------------------------------------------------------------------
    def json_get(self, url, headers=None, timeout=None):
        if self._fetch_json is not None:
            try:
                return self._fetch_json(url, headers, timeout)
            except Exception as error:  # noqa: BLE001 —— 注入的假 fetch 抛错也走信封
                return envelope_error("sec/network", f"{url} → {_error_text(error)}")
        return http_get_json(url, headers=headers, timeout=timeout or self.timeout)

    # 三方模块（惰性 import + 可注入）-----------------------------------------
    def module(self, name, injector=None, timeout=None):
        """取三方模块：注入了就用注入的（测试），否则惰性 import（生产）。

        import 本身可能以十秒计（openbb 实测 44-57s），所以主线程里用 ``SIGALRM`` 兜一条
        超时；非主线程（Starlette 线程池 / anyio worker）装不上定时器就退化为无告警 import。
        """
        if name in self._module_cache:
            return self._module_cache[name]
        if injector is not None:
            module = injector() if callable(injector) and not isinstance(injector, ModuleType) else injector
            self._module_cache[name] = module
            return module

        def _alarm(_signum, _frame):
            raise ModuleImportTimeout(f"{name} import 超时（{timeout}s）")

        previous = None
        armed = False
        try:
            previous = _install_alarm(_alarm, timeout)
            armed = True
        except Exception:  # noqa: BLE001 —— 非主线程/不支持 SIGALRM：退化为无告警 import
            armed = False
        try:
            module = importlib.import_module(name)
        finally:
            if armed:
                try:
                    _restore_alarm(previous)
                except Exception:  # noqa: BLE001
                    pass
        self._module_cache[name] = module
        return module


def _install_alarm(handler, timeout):
    """在 **主线程** 里给一次 import 装超时；非主线程返回哨兵由调用方降级处理。"""
    import signal

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("not main thread")
    if timeout is None:
        return None
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, float(timeout))
    return previous


def _restore_alarm(previous):
    import signal

    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, previous)


# ── AKShare ────────────────────────────────────────────────────────────────────


def akshare_module(deps):
    """惰性取 akshare；未安装时抛 ``ImportError``，由调用方转成 ``akshare/missing``。"""
    return deps.module("akshare", deps._akshare)


# ── AKShare 调用护栏：超时 + 自动重试 + 尝试留痕 ────────────────────────────────
# 治理口径见 ``v3_fallback.retry_akshare``（连接/超时/5xx 重试；业务错误不重试）。
# 这里只负责「套上 socket 超时护栏 → 调一次 → 把 attempts 明细带回来」。

#: AKShare 现货接口的降级链（**顺序 = 先快后慢、全市场优先**）。
#: ``stock_zh_a_spot_em`` 是全市场**单请求**接口，排第一；``stock_sh/sz/bj_a_spot_em`` 是
#: 东财同族的**单请求**分市场接口；``stock_zh_a_spot``（新浪）虽然也是全市场，但它**分页抓
#: ~70 次请求**（实测单次调用 50~67s），所以排在最后——否则一个慢接口就会吃掉整条链的预算
#: （2026-09-21 实测：sina 排第二时 /api/v3/spot 要 120s，排最后后约 50s）。
#: 分市场接口命中时只覆盖一个市场，响应里 ``market_scope``/``scope_note`` 会如实写明，
#: **绝不冒充全市场**。真机实测五个接口当前全部断连/超时，因此这条链当前的价值是
#: 「把每个接口的真实错误按顺序留痕」，而不是「碰巧能拿到数据」。
AKSHARE_SPOT_CHAIN = (
    {"func": "stock_zh_a_spot_em", "source": "akshare/stock_zh_a_spot_em",
     "scope": "A股全市场", "scope_full": True},
    {"func": "stock_sh_a_spot_em", "source": "akshare/stock_sh_a_spot_em",
     "scope": "沪市（分市场接口）", "scope_full": False},
    {"func": "stock_sz_a_spot_em", "source": "akshare/stock_sz_a_spot_em",
     "scope": "深市（分市场接口）", "scope_full": False},
    {"func": "stock_bj_a_spot_em", "source": "akshare/stock_bj_a_spot_em",
     "scope": "北交所（分市场接口）", "scope_full": False},
    {"func": "stock_zh_a_spot", "source": "akshare/stock_zh_a_spot",
     "scope": "A股全市场（新浪，分页接口，最慢）", "scope_full": True},
)
#: 整条现货链的墙钟预算（毫秒）：每个接口各重试 3 次，没有预算会把一个请求拖到几分钟。
#: 超预算的接口在响应 ``chain`` 里记 ``skipped=true`` + ``chain/timeout``（不假装试过）。
#: 默认 15000ms：实测 AKShare 现货类接口当前上游全断，45000 会让一次失败请求拖到 52s；
#: 压到 15s 后约 20s 返回，且**仍然是如实报错**（带试过的接口与真实错误），不牺牲诚实性。
#: 上游恢复后可用环境变量放宽（例如 60000）以取全量数据。
SPOT_CHAIN_BUDGET_ENV = "QUANT_AKSHARE_SPOT_BUDGET_MS"
SPOT_CHAIN_BUDGET_MS = 15000


def akshare_retry_options(deps):
    """AKShare 重试参数：``Deps.akshare_retry`` 的注入项（缺省空 dict → 用默认策略）。"""
    return dict(getattr(deps, "akshare_retry", None) or {})


def spot_chain_budget(deps=None):
    """现货链墙钟预算（毫秒）：``QUANT_AKSHARE_SPOT_BUDGET_MS``（非法/越界回落默认）。"""
    raw = None
    if deps is not None:
        try:
            raw = deps.get_env(SPOT_CHAIN_BUDGET_ENV)
        except Exception:  # noqa: BLE001 —— 假 deps 形态不保证
            raw = None
    if raw in (None, ""):
        return SPOT_CHAIN_BUDGET_MS
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return SPOT_CHAIN_BUDGET_MS
    return value if value > 0 else SPOT_CHAIN_BUDGET_MS


def _akshare_call(deps, func, kwargs=None, budget_ms=None):
    """调一次 akshare 函数（超时护栏 + 自动重试）→ ``(value, attempts, error)``。

    * ``value is None`` 表示**重试耗尽或业务错误**：``error`` 是第一次尝试的真实错误
      （``{code,message}``），``attempts`` 是完整重试明细（每次的 ok/ms/error/wait_ms）；
    * ``value`` 非 ``None`` 时原样返回（含空结果——空结果由调用方按链纪律判失败）；
    * ``budget_ms`` 透传给 ``retry_akshare``：单级取数的时间预算（降级链整链预算的剩余量）。
    """
    from server import v3_fallback  # 局部导入：与 financials_with_chain 同一口径

    def call():
        with _SocketTimeoutGuard(deps.timeout):
            return func(**(kwargs or {}))

    value, meta = v3_fallback.retry_akshare(call, budget_ms=budget_ms,
                                            **akshare_retry_options(deps))
    if value is not None:
        return value, meta, None
    first = next((item.get("error") for item in meta if item.get("error")), None)
    error = first or {"code": "akshare/all-attempts-failed", "message": "重试全部失败（无错误明细）"}
    return None, meta, error


def _akshare_failure_message(func, error, attempts, total_ms=None):
    """一句人类可读的失败说明：试了几次、每次等了多久、**真实错误原文**。"""
    detail = (error or {}).get("message") or "无错误明细"
    business = bool(attempts) and attempts[0].get("retryable") is False
    budget_stopped = bool(attempts) and attempts[-1].get("stopped") == "budget"
    waits = [item.get("wait_ms") for item in (attempts or []) if item.get("wait_ms")]
    if business:
        head = f"akshare.{func} 业务错误（不重试）：{detail}"
    else:
        head = f"akshare.{func} 尝试 {len(attempts or [])} 次仍失败"
        if waits:
            head += f"（退避 {waits} ms）"
        if budget_stopped:
            head += (f"；重试被时间预算（{int(attempts[-1].get('budget_ms') or 0)}ms）截断，"
                     "剩余次数未试")
    if total_ms is not None:
        head += f"，本接口合计 {total_ms}ms"
    return f"{head}：{detail}"


def _first_cell(row, *keys):
    """按优先级取第一个存在的列（AKShare 各接口的中文列名不完全一致）。"""
    for key in keys:
        if key in row:
            return row.get(key)
    return None


def _spot_row(row, spec):
    """现货行 → 契约行形状（列名按接口差异回落；新浪的 ``sh600519`` 归一成 ``600519``）。"""
    code = as_text(_first_cell(row, "代码", "symbol", "code"))
    if spec.get("func") == "stock_zh_a_spot":
        code = normalize_a_share_symbol(code)
    return {
        "code": code,
        "name": as_text(_first_cell(row, "名称", "name")),
        "price": as_number(_first_cell(row, "最新价", "trade", "price")),
        "change_pct": as_number(_first_cell(row, "涨跌幅", "changepercent")),
        "turnover_rate": as_number(_first_cell(row, "换手率", "turnoverratio")),
        "volume_ratio": as_number(_first_cell(row, "量比")),
        "pe": as_number(_first_cell(row, "市盈率-动态", "市盈率", "per")),
        "pb": as_number(_first_cell(row, "市净率", "pb")),
    }


def normalize_a_share_symbol(raw):
    """``SH.600519`` / ``sh600519`` / ``600519.SH`` → ``600519``（akshare 要裸代码）。"""
    text = as_text(raw)
    if not text:
        return ""
    upper = text.upper()
    for prefix in ("SH.", "SZ.", "BJ.", "SH", "SZ", "BJ"):
        if upper.startswith(prefix):
            remainder = upper[len(prefix):]
            if remainder[:1].isdigit():
                return remainder
    for suffix in (".SH", ".SZ", ".BJ"):
        if upper.endswith(suffix):
            return upper[: -len(suffix)]
    return upper


def _rows_from_frame(frame):
    """DataFrame → list[dict]。只做一次浅拷贝，不与 pandas 对象保持引用。

    优先 ``to_dict('records')``（pandas 路径），退化路径支持 list[dict] 与 ``iterrows()``，
    这样测试注入的假表不必真的安装 pandas。
    """
    if frame is None:
        return []
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            return list(to_dict("records"))
        except TypeError:
            pass
    if isinstance(frame, list):
        if frame and isinstance(frame[0], dict):
            return list(frame)
        if frame and isinstance(frame[0], (list, tuple)):
            return [dict(enumerate(row)) for row in frame]
        return []
    iterrows = getattr(frame, "iterrows", None)
    if callable(iterrows):
        return [dict(row) for _, row in iterrows()]
    return []


def _head(rows, limit):
    return rows[:limit]


def fetch_news(deps, symbol, limit):
    """``ak.stock_news_em(symbol=...)`` → 规格行形状（标题/摘要/时间/来源/链接/关键词）。

    上游断连/超时/5xx 由 ``retry_akshare`` 自动重试（指数退避 + 抖动）；业务错误不重试。
    失败信封里带 ``attempts``（每次的耗时与真实错误），成功信封里也带，便于核对重试开销。
    """
    code = normalize_a_share_symbol(symbol)
    if not code:
        return envelope_error("akshare/bad-args", "news 需要非空 symbol（如 600519 或 SH.600519）")
    try:
        ak = akshare_module(deps)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/missing", f"akshare 不可用：{_error_text(error)}")
    func = getattr(ak, "stock_news_em", None)
    if not callable(func):
        return envelope_error("akshare/missing-func", "akshare.stock_news_em 不存在（版本不兼容？）")
    frame, attempts, error = _akshare_call(deps, func, {"symbol": code})
    if frame is None:
        payload = envelope_error("akshare/stock_news_em",
                                 _akshare_failure_message("stock_news_em", error, attempts))
        payload["error"]["attempts"] = attempts
        return payload
    rows = []
    for row in _head(_rows_from_frame(frame), limit):
        rows.append(
            {
                "title": as_text(row.get("新闻标题")),
                "summary": as_text(row.get("新闻内容"))[:400],
                "published_at": as_text(row.get("发布时间")),
                "source": as_text(row.get("文章来源")),
                "url": as_text(row.get("新闻链接")),
                "keyword": as_text(row.get("关键词")),
            }
        )
    return {
        "ok": True,
        "as_of": now_iso(),
        "source": "akshare/stock_news_em",
        "symbol": code,
        "rows": rows,
        "attempts": attempts,
    }


def fetch_spot(deps, limit):
    """A 股实时快照：**多接口降级 + 自动重试**（``AKSHARE_SPOT_CHAIN``）。

    顺序（见 ``AKSHARE_SPOT_CHAIN``，先快后慢、全市场优先）::

        stock_zh_a_spot_em（全市场，单请求）
        → stock_sh_a_spot_em → stock_sz_a_spot_em → stock_bj_a_spot_em（东财分市场，单请求）
        → stock_zh_a_spot（新浪全市场，分页 ~70 次请求，最慢，排最后）

    每一级内部走 ``retry_akshare``（连接类/超时/5xx 重试，业务错误与空结果不重试）；
    整条链有墙钟预算 ``QUANT_AKSHARE_SPOT_BUDGET_MS``（默认 45000ms），超预算的接口在
    ``chain`` 里记 ``skipped`` + 原因——**不把没试过的接口写成失败**。

    响应契约（只增字段）:

      * 命中：``{ok:true, source, as_of, market_scope, scope_note, rows, chain, attempts}``；
        降级到分市场接口时 ``market_scope``/``scope_note`` 如实说明**只覆盖一部分市场**。
      * 全失败：``{ok:false, error:{code,message,attempts}, chain, as_of}``，``message`` 里带
        **试过的接口顺序**与**真实错误原文**；**绝不返回占位/空 rows 冒充成功**。
    """
    from server import v3_fallback  # 局部导入：与 financials_with_chain 同一口径

    want = to_int(limit, 20, 1, 500)
    try:
        ak = akshare_module(deps)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/missing", f"akshare 不可用：{_error_text(error)}")
    metas = {}
    budget_ms = spot_chain_budget(deps)
    chain_started = time.monotonic()

    def link(spec):
        func = getattr(ak, spec["func"], None)
        if not callable(func):
            return envelope_error("akshare/missing-func",
                                  f"akshare.{spec['func']} 不存在（版本不兼容？）")
        begin = time.monotonic()
        # 把「整链剩下的预算」也交给重试层：上游一次调用可能要几十秒，
        # 只在链级检查预算会让一个慢接口吃掉全部预算（实测新浪现货单次 50s）。
        remaining_ms = int(budget_ms - (begin - chain_started) * 1000)
        frame, attempts, error = _akshare_call(deps, func, budget_ms=max(1, remaining_ms))
        metas[spec["source"]] = attempts
        if frame is None:
            return envelope_error(
                spec["source"],
                _akshare_failure_message(spec["func"], error, attempts,
                                         int((time.monotonic() - begin) * 1000)))
        rows = [_spot_row(row, spec) for row in _head(_rows_from_frame(frame), want)]
        if not rows:
            return envelope_error("akshare/no-rows",
                                  f"akshare.{spec['func']} 未返回任何行（空结果不当可用）")
        payload = {
            "ok": True,
            "as_of": now_iso(),
            "source": spec["source"],
            "market_scope": spec["scope"],
            "scope_note": (f"本响应来自 {spec['source']}（{spec['scope']}）"
                           + ("" if spec.get("scope_full")
                              else "；分市场接口**只覆盖该市场**，不是全市场快照")),
            "rows": rows,
            "attempts": attempts,
        }
        return payload

    chain = [(spec["source"], (lambda spec=spec: link(spec))) for spec in AKSHARE_SPOT_CHAIN]
    value, used, attempts = v3_fallback.run_chain(chain, timeout=budget_ms / 1000.0)
    attempts = v3_fallback.attempts_chain(attempts)
    for item in attempts:
        detail = metas.get(item.get("source"))
        if detail is not None:
            item["attempts"] = detail
    if value is None:
        primary = next((item for item in attempts
                        if item.get("source") == "akshare/stock_zh_a_spot_em"
                        and item.get("error")), None)
        error = (primary or {}).get("error") or v3_fallback.last_error(attempts)
        payload = envelope_error(
            error.get("code") or "akshare/stock_zh_a_spot_em",
            f"A 股全市场快照：按顺序试过 {len(attempts)} 个接口全部失败。"
            f"首个接口的真实错误 {error.get('code')}: {error.get('message')}；"
            f"降级链：{v3_fallback.describe_attempts(attempts)}",
        )
        payload["as_of"] = now_iso()
        payload["chain"] = attempts
        payload["error"]["attempts"] = metas.get("akshare/stock_zh_a_spot_em")
        payload["error"]["tried"] = [item.get("source") for item in attempts]
        return payload
    payload = dict(value)
    payload["chain"] = attempts
    payload["used_source"] = used
    return payload


def fetch_kline_akshare(deps, symbol, limit=3):
    """``ak.stock_zh_a_hist`` → A 股日 K（K 线链的**降级源**，免密钥）。

    只取 ``limit`` 根（探测用 3 根即可）。历史行情需要具体起止日期——按 ``limit`` 个自然日
    反推一个足够宽的窗口（节假日会少几根，但**不会**因为窗口太窄而空手）。
    """
    code = normalize_a_share_symbol(symbol)
    if not code:
        return envelope_error("akshare/bad-args", "kline 需要非空 symbol（如 600519 或 SH.600519）")
    try:
        ak = akshare_module(deps)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/missing", f"akshare 不可用：{_error_text(error)}")
    func = getattr(ak, "stock_zh_a_hist", None)
    if not callable(func):
        return envelope_error("akshare/missing-func", "akshare.stock_zh_a_hist 不存在（版本不兼容？）")
    want = to_int(limit, 3, 1, 500)
    end = date.today()
    start = end - timedelta(days=max(14, want * 3))
    frame, attempts, error = _akshare_call(deps, func, {
        "symbol": code, "period": "daily",
        "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d"),
        "adjust": "qfq",
    })
    if frame is None:
        payload = envelope_error("akshare/stock_zh_a_hist",
                                 _akshare_failure_message("stock_zh_a_hist", error, attempts))
        payload["error"]["attempts"] = attempts
        return payload
    rows = []
    for row in _rows_from_frame(frame)[-want:]:
        rows.append(
            {
                "t": as_text(row.get("日期")),
                "o": as_number(row.get("开盘")),
                "h": as_number(row.get("最高")),
                "l": as_number(row.get("最低")),
                "c": as_number(row.get("收盘")),
                "v": as_number(row.get("成交量")),
            }
        )
    if not rows:
        return envelope_error("akshare/no-rows", f"akshare.stock_zh_a_hist 未返回 {code} 的任何日 K")
    return {"ok": True, "as_of": now_iso(), "source": "akshare/stock_zh_a_hist",
            "symbol": code, "bars": rows, "attempts": attempts}


#: AKShare 财务接口 → 三表的中文报表名（``stock_financial_report_sina`` 的 ``symbol``）。
AKSHARE_SINA_STATEMENTS = {"income": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}
#: AKShare 港股财务接口 ``stock_financial_hk_report_em`` 的 ``symbol`` 取值。
AKSHARE_HK_STATEMENTS = {"income": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}


def _lines_from_akshare_frame(frame, periods):
    """AKShare 财务表 → 契约形状 ``lines:[{tag,label,unit,points:[{end,val}]}]``。

    两种常见朝向都支持（**不改写数值**，只做重排）:
      * 「指标为行、报告期为列」（``stock_financial_abstract``：首列是指标名，其余列是期数）；
      * 「报告期为行」（首列是报告期，其余列是指标名）。
    无法识别的形状返回 ``[]``（调用方转 ``akshare/unexpected-shape``，不当成功）。
    """
    rows = _rows_from_frame(frame)
    if not rows:
        return []
    headers = list(rows[0].keys())
    if not headers:
        return []
    label_key = None
    for candidate in ("指标", "选项", "报告期", "日期"):
        if candidate in headers:
            label_key = candidate
            break
    if label_key is None:
        label_key = headers[0]
    # 「指标为行」：除标签列外，其余列都是期数（形如 20240930 / 2024-09-30）
    period_columns = [key for key in headers
                      if key != label_key and _looks_like_period(key)]
    if period_columns:
        periods = sorted(period_columns)[-max(1, int(periods)):]
        lines = []
        for row in rows:
            label = as_text(row.get(label_key))
            if not label:
                continue
            points = []
            for column in periods:
                value = as_number(row.get(column))
                if value is None:
                    continue
                points.append({"end": _period_to_date(column), "val": value, "period": str(column)})
            if points:
                lines.append({"tag": label, "label": label, "unit": None, "points": points,
                              "latestEnd": points[-1]["end"],
                              "ageDays": age_days_since(points[-1]["end"]),
                              "stale": _is_stale(points[-1]["end"])})
        return lines
    # 「报告期为行」：标签列是期数，其余列是指标名
    line_map = {}
    for row in rows:
        end = _period_to_date(as_text(row.get(label_key)))
        if not end:
            continue
        for key in headers:
            if key == label_key:
                continue
            value = as_number(row.get(key))
            if value is None:
                continue
            entry = line_map.setdefault(str(key), {"tag": str(key), "label": str(key), "unit": None,
                                                   "points": []})
            entry["points"].append({"end": end, "val": value, "period": as_text(row.get(label_key))})
    lines = []
    for entry in line_map.values():
        entry["points"].sort(key=lambda point: point["end"])
        entry["points"] = entry["points"][-max(1, int(periods)):]
        entry["latestEnd"] = entry["points"][-1]["end"]
        entry["ageDays"] = age_days_since(entry["latestEnd"])
        entry["stale"] = _is_stale(entry["latestEnd"])
        lines.append(entry)
    return lines


def _looks_like_period(value):
    text = str(value or "").strip()
    digits = text.replace("-", "").replace("/", "")
    return len(digits) >= 6 and digits[:6].isdigit()


def _period_to_date(value):
    """``20240930`` / ``2024-09-30`` → ``2024-09-30``；无法识别返回 ``""``。"""
    text = str(value or "").strip()
    digits = text.replace("-", "").replace("/", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) >= 6 and digits[:6].isdigit():
        return text
    return ""


def _is_stale(end):
    age = age_days_since(end)
    return age is not None and age > SEC_STALE_DAYS


def fetch_financials_akshare(deps, ticker, statement, periods):
    """AKShare 财务（降级链第 2 级，免密钥；**A 股覆盖，港股依版本**）。

    A 股：``stock_financial_abstract(symbol=600519)``；港股：``stock_financial_hk_report_em``
    （若该版本没有这个函数，如实报 ``akshare/missing-func``，不编造）。
    """
    if statement not in AKSHARE_SINA_STATEMENTS:
        return envelope_error("akshare/bad-statement",
                              f"statement 需为 {sorted(AKSHARE_SINA_STATEMENTS)}，收到 {statement!r}")
    market = detect_market(ticker)
    code = normalize_a_share_symbol(ticker) if market in ("SH", "SZ", "BJ") else _bare_code(ticker)
    if not code:
        return envelope_error("akshare/bad-args", f"financials 需要可识别的标的代码，收到 {ticker!r}")
    try:
        ak = akshare_module(deps)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/missing", f"akshare 不可用：{_error_text(error)}")
    want = to_int(periods, 4, 1, 12)
    if market == "HK":
        func = getattr(ak, "stock_financial_hk_report_em", None)
        if not callable(func):
            return envelope_error(
                "akshare/missing-func",
                "akshare.stock_financial_hk_report_em 不存在（该版本不支持港股财务；"
                "港股财务降级无可用开源源）")
        frame, attempts, error = _akshare_call(deps, func, {
            "stock": code, "symbol": AKSHARE_HK_STATEMENTS[statement], "indicator": "年度"})
        source = "akshare/stock_financial_hk_report_em"
    else:
        func = getattr(ak, "stock_financial_abstract", None)
        if not callable(func):
            return envelope_error("akshare/missing-func", "akshare.stock_financial_abstract 不存在（版本不兼容？）")
        frame, attempts, error = _akshare_call(deps, func, {"symbol": code})
        source = "akshare/stock_financial_abstract"
    if frame is None:
        name = "stock_financial_hk_report_em" if market == "HK" else "stock_financial_abstract"
        payload = envelope_error(source, _akshare_failure_message(name, error, attempts))
        payload["error"]["attempts"] = attempts
        return payload
    lines = _lines_from_akshare_frame(frame, want)
    if not lines:
        return envelope_error("akshare/unexpected-shape",
                              f"{source} 返回的表结构无法识别为财务期数（未做猜测性解析）")
    return {
        "ok": True,
        "ticker": str(ticker).strip().upper(),
        "market": market,
        "statement": statement,
        "as_of": now_iso(),
        "source": source,
        "lines": lines,
        "missing": [],
        "attempts": attempts,
    }


# ── 富途 f10_detail（三表主源）────────────────────────────────────────────────


def _bare_code(ticker):
    """``SH.600519`` / ``600519.SH`` → ``600519``；``AAPL`` → ``AAPL``。"""
    text = as_text(ticker).upper()
    if "." in text:
        head, tail = text.split(".", 1)
        if head in ("SH", "SZ", "BJ", "HK", "US"):
            return tail
        if tail in ("SH", "SZ", "BJ", "HK", "US"):
            return head
    return text


def detect_market(ticker):
    """标的 → ``SH/SZ/BJ/HK/US``（识别不出返回 ``""``）。

    规则全部来自**公开编码形态**，不做业务猜测:
      * 带前缀/后缀（``SH.600000`` / ``600000.SH`` / ``00700.HK`` / ``US.AAPL``）→ 直接取；
      * 纯 6 位数字 → 按首位映射交易所（6/9/5/1→SH，0/2/3→SZ，4/8→BJ）；
      * 纯 5 位数字 → 港股；
      * 纯字母（1~6 位）→ 美股。
    """
    text = as_text(ticker).upper()
    if not text:
        return ""
    if "." in text:
        head, tail = text.split(".", 1)
        if head in ("SH", "SZ", "BJ", "HK", "US"):
            return head
        if tail in ("SH", "SZ", "BJ", "HK", "US"):
            return tail
        return ""
    if text.isdigit():
        if len(text) == 6:
            return A_SHARE_HEAD_MARKET.get(text[0], "")
        if len(text) == 5:
            return "HK"
        return ""
    if text.isalpha() and len(text) <= 6:
        return "US"
    return ""


def normalize_futu_code(ticker):
    """标的 → 富途代码形态（``SH.600000`` / ``HK.00700`` / ``US.AAPL``）。

    港股补零到 5 位（富途用 ``HK.00700``）；A 股补零到 6 位。识别不出的市场原样返回，
    由上游如实报错（错误消息里能看到是不是代码形态的问题）。
    """
    market = detect_market(ticker)
    suffix_map = {"SH": "SH", "SZ": "SZ", "BJ": "BJ", "HK": "HK", "US": "US"}
    suffix_map.update({"SS": "SH"})
    if not market:
        text = as_text(ticker).upper()
        if "." in text:
            head, tail = text.split(".", 1)
            if tail in suffix_map:
                return f"{suffix_map[tail]}.{head}"
            if head in suffix_map:
                return f"{suffix_map[head]}.{tail}"
        return text
    code = _bare_code(ticker)
    if market in ("SH", "SZ", "BJ") and code.isdigit() and len(code) < 6:
        code = code.zfill(6)
    if market == "HK" and code.isdigit() and len(code) < 5:
        code = code.zfill(5)
    return f"{market}.{code}"


def _futu_report_date(report, market=""):
    """富途报表期 → ``YYYY-MM-DD``。

    ``date_time`` 是**毫秒**时间戳，表示该市场**本地**零点的报表期末（实测 SH.600000 的
    2026/Q2 是 ``1782748800000`` = 2026-06-30 00:00+08:00）。若按 UTC 取日期会整体差一天，
    所以必须按市场时区折算。
    """
    raw = report.get("date_time")
    number = as_number(raw)
    if number is None or number <= 0:
        return ""
    zone = MARKET_TZ.get(market or "")
    try:
        if zone is None:
            moment = datetime.fromtimestamp(number / 1000.0, tz=timezone.utc)
        else:
            from zoneinfo import ZoneInfo  # 惰性：只有 A 股/港股这条链才需要

            moment = datetime.fromtimestamp(number / 1000.0, tz=timezone.utc).astimezone(ZoneInfo(zone))
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.date().isoformat()


def fetch_futu_financials(v3_run, ticker, statement, periods):
    """富途 ``f10_detail`` 的 ``statements`` section（**A 股/港股三表主源**）。

    ``statement_type``: 1=利润表 2=资产负债表 3=现金流量表（官方 1~4；第 4 项是主要指标，
    不是三表口径，故不接受）。返回契约形状 ``lines:[{tag,label,points:[{end,val}]}]``：
      * ``tag`` 用富途的 ``field_id``（跨期稳定）；
      * ``label`` 用 ``display_name``；
      * ``points`` 按期升序，``end`` 是报表期末（按市场时区折算），另带 ``period``（如 2026/Q2）；
      * ``yoy``/``qoq`` 是上游原值，一并保留（前端可选展示）。
    上游报「无报表期数据」时**返回失败信封**（``futu/no-reports``），好让降级链接手。
    """
    statement = as_text(statement).lower()
    if statement not in FUTU_STATEMENT_TYPES:
        return envelope_error("futu/bad-statement",
                              f"statement 需为 {sorted(FUTU_STATEMENT_TYPES)}，收到 {statement!r}")
    code = normalize_futu_code(ticker)
    if not code or "." not in code:
        return envelope_error("futu/bad-args", f"无法把 {ticker!r} 归一成富途代码（如 SH.600000 / HK.00700）")
    want = to_int(periods, 4, 1, 12)
    envelope = v3_run("f10_detail", {
        "code": code,
        "section": "statements",
        "params": {"statement_type": FUTU_STATEMENT_TYPES[statement], "limit": want},
    })
    if not isinstance(envelope, dict):
        return envelope_error("futu/bad-envelope", f"f10_detail 返回非信封对象：{type(envelope).__name__}")
    if not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        return envelope_error(error.get("code") or "futu/f10_detail",
                              error.get("message") or "f10_detail 返回 ok=false")
    value = envelope.get("value") if isinstance(envelope.get("value"), dict) else {}
    reports = [item for item in (value.get("report_list") or []) if isinstance(item, dict)]
    if not reports:
        return envelope_error("futu/no-reports",
                              f"富途 f10_detail/statements 未返回 {code} 的报表期数据"
                              f"（section 存在但该标的无此报表，或已按 -10 无数据处理）")
    ordered = list(reversed(reports))[:want]  # 上游按新→旧返回；取最近 want 期后转成旧→新
    lines = {}
    line_order = []
    for report in ordered:
        end = _futu_report_date(report, detect_market(ticker))
        period = as_text(report.get("period_text"))
        currency = as_text(report.get("currency_code"))
        for item in report.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            display = as_text(item.get("display_name"))
            field_id = item.get("field_id")
            tag = str(field_id) if field_id is not None else f"name:{display}"
            if tag not in lines:
                lines[tag] = {
                    "tag": tag,
                    "label": display or tag,
                    "unit": as_text(item.get("value_type")) or None,
                    "currency": currency or None,
                    "points": [],
                }
                line_order.append(tag)
            value_raw = item.get("data")
            number = as_number(value_raw)
            if number is None:
                continue  # 空值**不进 points**（不填 0 冒充）
            point = {"end": end, "val": number, "period": period}
            if item.get("yoy") is not None:
                point["yoy"] = as_number(item.get("yoy"))
            if item.get("qoq") is not None:
                point["qoq"] = as_number(item.get("qoq"))
            lines[tag]["points"].append(point)
    out_lines = []
    missing = []
    for tag in line_order:
        entry = lines[tag]
        entry["points"] = [point for point in entry["points"] if point.get("end")]
        if not entry["points"]:
            missing.append({"tag": tag, "error": "futu/no-points",
                            "message": f"{entry['label']} 各期均为空值（不填 0）"})
            continue
        entry["points"].sort(key=lambda point: point["end"])
        entry["latestEnd"] = entry["points"][-1]["end"]
        entry["ageDays"] = age_days_since(entry["latestEnd"])
        entry["stale"] = _is_stale(entry["latestEnd"])
        out_lines.append(entry)
    if not out_lines:
        return envelope_error("futu/no-points",
                              f"富途返回了 {len(reports)} 个报表期，但所有科目都是空值（不编造）")
    return {
        "ok": True,
        "ticker": as_text(ticker).upper(),
        "futuCode": code,
        "market": detect_market(ticker),
        "statement": statement,
        "statementType": FUTU_STATEMENT_TYPES[statement],
        "statementLabel": FUTU_STATEMENT_LABELS[statement],
        "as_of": now_iso(),
        "source": "futu/f10_detail/statements",
        "periods": [as_text(report.get("period_text")) for report in ordered],
        "lines": out_lines,
        "missing": missing,
    }


# ── SEC EDGAR ──────────────────────────────────────────────────────────────────


class SecSource:
    """SEC EDGAR 公开 XBRL（免密钥）。company_tickers.json 在内存与 ``home`` 下各缓存一份。"""

    def __init__(self, deps, home=None):
        self.deps = deps
        self.home = deps.home if home is None else (None if home is None else str(home))
        self._index = None
        self._lock = threading.Lock()

    # ticker → CIK ---------------------------------------------------------
    @property
    def cache_path(self):
        return None if not self.home else os.path.join(self.home, SEC_TICKERS_CACHE)

    def _load_file_cache(self):
        path = self.cache_path
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:  # noqa: BLE001 —— 缓存损坏就当没有，回落到网络
            return None

    def _write_file_cache(self, payload):
        path = self.cache_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
        except Exception:  # noqa: BLE001 —— 写缓存失败不影响取数
            pass

    def index(self):
        """返回 ``(index_dict, error_or_None)``；index 是 ``{TICKER: row}``。"""
        with self._lock:
            if self._index is not None:
                return self._index, None
            result = self.deps.json_get(
                SEC_TICKERS_URL,
                headers={"user-agent": self.user_agent, "accept": "application/json"},
                timeout=self.deps.timeout,
            )
            if result.get("ok"):
                payload = result.get("value")
                self._write_file_cache(payload)
            else:
                payload = self._load_file_cache()
                if payload is None:
                    error = result.get("error") or {}
                    return None, envelope_error(error.get("code", "sec/network"), error.get("message", "company_tickers 取数失败"))
            if not isinstance(payload, dict):
                return None, envelope_error("sec/bad-payload", "company_tickers.json 不是对象")
            index = {}
            for row in payload.values():
                if isinstance(row, dict) and row.get("ticker") is not None:
                    index[str(row["ticker"]).upper()] = row
            self._index = index
            return index, None

    @property
    def user_agent(self):
        return self.deps.get_env("QUANT_SEC_USER_AGENT", DEFAULT_SEC_UA)

    def resolve(self, ticker):
        """``(cik_str, company_title, error)``。cik 与 sec.mjs 一致补零到 10 位。"""
        symbol = as_text(ticker).upper()
        if not symbol:
            return None, None, envelope_error("sec/bad-args", "financials 需要非空 ticker（如 AAPL）")
        index, error = self.index()
        if error is not None:
            return None, None, error
        row = index.get(symbol)
        if not isinstance(row, dict):
            return None, None, envelope_error("sec/unknown-ticker", f"SEC 无此 ticker：{ticker}")
        raw_cik = as_text(row.get("cik_str"))
        if not raw_cik.isdigit():
            return None, None, envelope_error("sec/bad-cik", f"{ticker} 的 CIK 非法：{raw_cik!r}")
        return raw_cik.zfill(10), as_text(row.get("title")), None

    # companyconcept -------------------------------------------------------
    def concept(self, cik, tag, periods):
        """单个 us-gaap 概念的最近 ``periods`` 期（按期末去重）。"""
        url = SEC_CONCEPT_URL.format(cik=int(cik), tag=tag)
        result = self.deps.json_get(
            url,
            headers={"user-agent": self.user_agent, "accept": "application/json"},
            timeout=self.deps.timeout,
        )
        if not result.get("ok"):
            error = result.get("error") or {}
            return {"ok": False, "tag": tag, "error": error}
        payload = result.get("value")
        if not isinstance(payload, dict):
            return {"ok": False, "tag": tag, "error": {"code": "sec/bad-payload", "message": f"{tag} 返回值不是对象"}}
        units = payload.get("units") if isinstance(payload.get("units"), dict) else {}
        # 单位优先级：金额取 USD，每股取 USD/shares，其余取第一个（与 sec.mjs 一致但更明确）
        unit_key = None
        for candidate in ("USD", "USD/shares", "shares"):
            if candidate in units:
                unit_key = candidate
                break
        if unit_key is None:
            unit_key = next(iter(units), None)
        raw = units.get(unit_key) if unit_key is not None else None
        if not isinstance(raw, list):
            raw = []
        candidates = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            if row.get("form") not in SEC_FORMS:
                continue
            if row.get("end") is None or not isinstance(row.get("val"), (int, float)) or isinstance(row.get("val"), bool):
                continue
            candidates.append(row)
        # 排序键全部是字符串比较：end/filed/start 都是 YYYY-MM-DD
        candidates.sort(key=lambda r: (str(r.get("end") or ""), str(r.get("filed") or ""), str(r.get("start") or "")))
        by_end = {}
        for row in candidates:
            key = str(row["end"])
            previous = by_end.get(key)
            if previous is None:
                by_end[key] = row
                continue
            # 同一期末多口径（10-K 里年度值与 Q4 值会共用 end）：取覆盖期最长的那条，
            # 并列时取更晚上报的。
            current_span = str(row.get("start") or "")
            previous_span = str(previous.get("start") or "")
            if current_span < previous_span or (current_span == previous_span and str(row.get("filed") or "") >= str(previous.get("filed") or "")):
                by_end[key] = row
        points = []
        for key in sorted(by_end)[-periods:]:
            row = by_end[key]
            points.append(
                {
                    "end": row.get("end"),
                    "val": row.get("val"),
                    "form": row.get("form"),
                    "fy": row.get("fy"),
                    "fp": row.get("fp"),
                    "filed": row.get("filed"),
                    "start": row.get("start"),
                }
            )
        return {
            "ok": True,
            "tag": tag,
            "label": as_text(payload.get("label")) or tag,
            "unit": unit_key,
            "points": points,
            "taxonomy": as_text(payload.get("taxonomy")),
        }

    def financials(self, ticker, statement, periods):
        """三表之一：命中的标签进 ``lines``，未申报/取不到的进 ``missing``（不编造）。"""
        tags = STATEMENT_TAGS.get(statement)
        if tags is None:
            wanted = " / ".join(sorted(STATEMENT_TAGS))
            return envelope_error("sec/bad-statement", f"statement 需为 {wanted}，收到 {statement!r}")
        cik, company, error = self.resolve(ticker)
        if error is not None:
            return error
        lines = []
        missing = []
        for tag in tags:
            result = self.concept(cik, tag, periods)
            if not result.get("ok"):
                detail = result.get("error") or {}
                missing.append({"tag": tag, "error": detail.get("code") or "sec/unknown", "message": detail.get("message")})
                continue
            points = result.get("points") or []
            if not points:
                missing.append({"tag": tag, "error": "sec/no-points", "message": "该标签无 10-K/10-Q/20-F/6-K 口径数据"})
                continue
            latest_end = points[-1].get("end")
            age_days = age_days_since(latest_end)
            lines.append(
                {
                    "tag": tag,
                    "label": result.get("label") or tag,
                    "unit": result.get("unit"),
                    "points": points,
                    "latestEnd": latest_end,
                    "ageDays": age_days,
                    # 申报结构变化会让旧标签停用：新近度交给调用方判断，避免把旧期当当期
                    "stale": age_days is not None and age_days > SEC_STALE_DAYS,
                }
            )
        return {
            "ok": True,
            "ticker": as_text(ticker).upper(),
            "cik": cik,
            "company": company,
            "statement": statement,
            "as_of": now_iso(),
            "source": "sec/companyconcept(us-gaap XBRL)",
            "lines": lines,
            "missing": missing,
        }


def age_days_since(end):
    """期末 → 距今天数（四舍五入）。解析失败返回 ``None``。"""
    text = as_text(end)[:10]
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return round((date.today() - parsed).days)


# ── OpenBB ─────────────────────────────────────────────────────────────────────


def jsonable(value, depth=0):
    """把 OpenBB 的结果对象压成**朴素的 JSON 值**，只保留叶子字段。

    这是「不序列化活对象」纪律的落实点：OBBject / pydantic model 会连带 provider 引用、
    原样 ``JSONResponse`` 会崩，所以这里显式只取数据字段，遇到不认识的类型就 ``str()``。
    """
    if depth > 6:
        return as_text(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and value != value:
            return None
        return value
    if isinstance(value, dict):
        return {as_text(key): jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item, depth + 1) for item in value]
    for method in ("model_dump", "to_dict", "dict"):
        func = getattr(value, method, None)
        if callable(func):
            try:
                return jsonable(func(), depth + 1)
            except Exception:  # noqa: BLE001 —— 换下一个方法
                continue
    if isinstance(value, datetime):
        return value.isoformat()
    return as_text(value)


def fetch_openbb(deps, symbol):
    """尝试一次真实基本面取数。依赖未装则不装也不发请求，如实报 ``openbb/unavailable``。"""
    try:
        module = deps.module("openbb", deps._openbb, timeout=OPENBB_IMPORT_TIMEOUT)
    except ImportError as error:
        return envelope_error("openbb/unavailable", f"openbb 未安装：pip install openbb（{_error_text(error)}）")
    except Exception as error:  # noqa: BLE001 —— import 期其它异常（含超时/子依赖损坏）
        return envelope_error("openbb/import-failed", f"openbb 导入失败：{_error_text(error)}")
    obb = getattr(module, "obb", None) or module
    base = getattr(obb, "equity", None)
    fundamental = getattr(base, "fundamental", None) if base is not None else None
    entry = None
    for name in ("metrics", "income_statement", "balance_sheet", "cash_flow"):
        candidate = getattr(fundamental, name, None) if fundamental is not None else None
        if callable(candidate):
            entry = (name, candidate)
            break
    if entry is None:
        return envelope_error(
            "openbb/capability-missing",
            "openbb 已安装但无 equity.fundamental.metrics —— 版本不兼容，请检查 openbb 安装",
        )
    name, func = entry
    try:
        with _SocketTimeoutGuard(deps.openbb_timeout):
            output = func(symbol=as_text(symbol).upper(), provider="yfinance")
    except Exception as error:  # noqa: BLE001 —— provider 缺依赖/无密钥都在这里如实暴露
        text = _error_text(error)
        lowered = text.lower()
        code = "openbb/provider-error" if ("provider" in lowered or "credential" in lowered or "api key" in lowered) else "openbb/error"
        return envelope_error(code, f"openbb.equity.fundamental.{name} 失败：{text}")
    if output is None:
        return envelope_error("openbb/empty", f"openbb.equity.fundamental.{name} 返回空结果")
    if hasattr(output, "results"):
        results = output.results
    elif hasattr(output, "to_df") or hasattr(output, "to_dataframe"):
        results = None
    else:
        results = output
    rows = []
    if results is not None:
        converted = jsonable(results)
        if isinstance(converted, list):
            rows = [row if isinstance(row, dict) else {"value": row} for row in converted]
        elif isinstance(converted, dict):
            rows = [converted]
    if not rows:
        for method in ("to_df", "to_dataframe"):
            func_df = getattr(output, method, None)
            if callable(func_df):
                try:
                    rows = [jsonable(row) for row in _rows_from_frame(func_df())]
                    break
                except Exception as error:  # noqa: BLE001
                    return envelope_error("openbb/serialize", f"openbb 结果无法序列化：{_error_text(error)}")
    if not rows:
        return envelope_error("openbb/empty", f"openbb.equity.fundamental.{name} 无行数据")
    return {
        "ok": True,
        "as_of": now_iso(),
        "source": f"openbb/equity.fundamental.{name}",
        "symbol": as_text(symbol).upper(),
        "rows": rows,
    }


# ── /api/v3/financials 的按市场路由 + 降级链 ───────────────────────────────────


def financials_with_chain(sec, deps, v3_run, ticker, statement, periods):
    """按市场选主源并组装降级链（纯函数，路由只是它的异步外壳；便于离线单测）。

    返回**契约形状**（美股保持既有字段；A 股/港股给 ``lines`` + ``chain``）:

        {"ok":true,"ticker":"SH.600000","market":"SH","statement":"income",
         "source":"futu/f10_detail/statements","lines":[{tag,label,points:[{end,val}]}],
         "missing":[...],"chain":[{"source":...,"ok":...,"ms":...,"error"?}]}

    全链失败 → ``{ok:false,error:{code,message,chain}}``：``code/message`` 取**最后一级的
    真实错误**（保留上游错误码原文），``chain`` 写明每一级为什么没成。
    """
    from server import v3_fallback  # 局部导入：v3_fallback 反向依赖本模块的取数函数

    symbol = as_text(ticker)
    if not symbol:
        return envelope_error("financials/bad-args", "financials 需要非空 ticker（如 AAPL / SH.600000 / HK.00700）")
    market = detect_market(symbol)
    # ── 美股：SEC EDGAR（既有行为逐字不变，只追加 chain）────────────────────────
    if market == "US":
        started = time.monotonic()
        payload = sec.financials(symbol, statement, periods)
        elapsed = int((time.monotonic() - started) * 1000)
        attempt = {"source": "sec/companyconcept(us-gaap XBRL)", "ok": bool(payload.get("ok")),
                   "ms": elapsed}
        if not payload.get("ok"):
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            attempt["error"] = {"code": error.get("code", "sec/error"),
                                "message": error.get("message", "SEC 取数失败")}
            payload["chain"] = [attempt]
            payload["market"] = "US"
            return payload
        payload["market"] = "US"
        payload["chain"] = [attempt]
        return payload

    # ── A 股/港股：富途 f10_detail → AKShare（Tushare 已按数据源政策移除）────────
    if statement not in FUTU_STATEMENT_TYPES:
        return envelope_error("financials/bad-statement",
                              f"statement 需为 {sorted(FUTU_STATEMENT_TYPES)}，收到 {statement!r}")

    def futu_link():
        return fetch_futu_financials(v3_run, symbol, statement, periods)

    def akshare_link():
        if market == "HK":
            # AKShare 的 A 股财务接口不覆盖港股（港股接口依版本可能不存在）——真试一次，
            # 让真实的 missing-func/上游错误进 chain，而不是写死一句「不支持」。
            return fetch_financials_akshare(deps, symbol, statement, periods)
        if market not in ("SH", "SZ", "BJ"):
            return envelope_error(
                "akshare/unsupported-market",
                f"AKShare 财务降级仅覆盖 A 股（market={market or '未知'}）；港股美股请以富途/SEC 为准")
        return fetch_financials_akshare(deps, symbol, statement, periods)

    chain = [
        ("futu/f10_detail/statements", futu_link),
        (f"akshare/{'stock_financial_hk_report_em' if market == 'HK' else 'stock_financial_abstract'}",
         akshare_link),
    ]
    value, used_source, attempts = v3_fallback.run_chain(chain, timeout=FINANCIALS_CHAIN_TIMEOUT)
    attempts = v3_fallback.attempts_chain(attempts)
    if value is None:
        error = v3_fallback.last_error(attempts)
        payload = envelope_error(
            error.get("code") or "financials/all-sources-failed",
            f"{symbol} 的 {statement} 各级源全部失败。降级链：{v3_fallback.describe_attempts(attempts)}",
        )
        payload["ticker"] = symbol.upper()
        payload["market"] = market
        payload["statement"] = statement
        payload["chain"] = attempts
        return payload
    payload = dict(value)
    payload["chain"] = attempts
    payload["sources_chain"] = [item["source"] for item in attempts]
    payload["used_source"] = used_source
    return payload


# ── 路由注册 ───────────────────────────────────────────────────────────────────


def register(app, v3_run, home, deps=None):
    """把 5 条外部数据源路由挂到 ``app``。

    ``v3_run`` 供概览类复用（本模块只用它做错误上下文，不新造第二事实源）；``home`` 是
    数据目录（SEC ticker 索引缓存在此）。返回 ``deps``，便于测试断言缓存/调用次数。
    """
    if deps is None:
        deps = Deps(home=home)
    elif deps.home is None:
        deps.home = None if home is None else str(home)
    sec = SecSource(deps, home=home or deps.home)

    @app.get("/api/v3/news")
    async def v3_news(symbol: str = "", limit: int = 10):
        """A 股个股资讯（akshare 免密钥）。symbol 允许 ``SH.600519`` 形式。"""
        try:
            payload = await asyncio.to_thread(fetch_news, deps, symbol, to_int(limit, 10, 1, 200))
        except Exception as error:  # noqa: BLE001 —— 统一信封，不把栈透给前端
            payload = envelope_error("akshare/internal", _error_text(error))
        return payload

    @app.get("/api/v3/spot")
    async def v3_spot(limit: int = 20):
        """A 股实时快照：多接口降级（``AKSHARE_SPOT_CHAIN``）+ 自动重试。

        上游实测常以 ``RemoteDisconnected`` 断连——先按退避重试，再按接口顺序降级；
        全失败时 ``error.message`` 带试过的接口与真实错误原文，**不伪造、不返回空 rows**。
        """
        try:
            payload = await asyncio.to_thread(fetch_spot, deps, to_int(limit, 20, 1, 500))
        except Exception as error:  # noqa: BLE001
            payload = envelope_error("akshare/internal", _error_text(error))
        return payload

    @app.get("/api/v3/financials")
    async def v3_financials(ticker: str = "", statement: str = "income", periods: int = 4):
        """三表：美股走 SEC EDGAR；A 股/港股走富途 ``f10_detail`` 并按**降级链**兜底。

        路由规则（按 ticker 的市场识别，见 ``detect_market``）:
          * ``AAPL`` 之类美股 → SEC EDGAR（**既有字段保持不变**，仅追加 ``chain``）；
          * ``SH.600000`` / ``600519.SH`` / ``HK.00700`` → 富途 ``f10_detail`` 的
            ``statements`` section（``statement_type`` 1/2/3）→ AKShare（免密钥，A 股）
            → 全失败如实报错。

        任何一条链的尝试结果都在 ``chain:[{source,ok,ms,error?}]`` 里，前端可逐级核对。
        """
        want_statement = as_text(statement).lower() or "income"
        want_periods = to_int(periods, 4, 1, 12)
        try:
            payload = await asyncio.to_thread(
                financials_with_chain, sec, deps, v3_run, ticker, want_statement, want_periods
            )
        except Exception as error:  # noqa: BLE001
            payload = envelope_error("financials/internal", _error_text(error))
        return payload

    @app.get("/api/v3/openbb")
    async def v3_openbb(symbol: str = ""):
        """OpenBB 基本面（可选依赖；未安装直接如实报错，不发网络请求）。"""
        if not as_text(symbol):
            return envelope_error("openbb/bad-args", "openbb 需要非空 symbol（如 AAPL）")
        try:
            payload = await asyncio.to_thread(fetch_openbb, deps, symbol)
        except Exception as error:  # noqa: BLE001
            payload = envelope_error("openbb/internal", _error_text(error))
        return payload

    routes = ("/api/v3/news", "/api/v3/spot", "/api/v3/financials", "/api/v3/openbb")
    # 扩展数据源（北向 / 宏观 / A 股公开行情·盘口降级链）：app.py 的模块接线清单是封闭的，
    # 扩展模块借本入口一并装配（共享同一份 ``deps`` 与 ``v3_run``）。模块缺失不阻断主装配
    # （与 app.py 的 try/import 同口径）；装配后的完整路由清单回写 ``app.state.v3_sources``。
    try:
        from server import v3_sources_ext

        v3_sources_ext.register(app, v3_run, home, deps=deps)
        routes = routes + tuple(app.state.v3_sources_ext["routes"])
    except ModuleNotFoundError:
        pass
    app.state.v3_sources = {
        "deps": deps,
        "sec": sec,
        "v3_run": v3_run,
        "routes": routes,
    }
    return deps
