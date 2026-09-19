"""外部数据源 V3 接口（``/api/v3/*`` 的数据源半边）：AKShare / SEC EDGAR / Tushare / OpenBB。

设计约束（与规格 FR-DATA-002 及仓库「数据诚实」纪律一致）：

  * **绝不伪造**。上游取不到就把**真实错误文本**放进 ``{ok:false,error:{code,message}}``，
    不返回估算值、不返回空壳成功。
  * **所有外部调用都带超时**。HTTP 层用 ``httpx.Timeout``（缺 httpx 时退到
    ``urllib.request`` 并传 ``timeout``）；akshare 这类不接受超时参数的三方函数用
    ``socket.setdefaulttimeout`` 兜住阻塞点（akshare 内部走 requests，会读这个默认值）。
  * **持久化/惰性**。``register()`` 只挂路由，**不 import** akshare / openbb / pandas：
    那些库导入以十秒计，服务启动与测试套件都不能被它们拖住。一切三方 import 都在
    请求处理路径内按需发生，且可被注入的假模块/假 fetch 替换（见 ``deps``）。

参数注入（``register(app, v3_run, home, deps=None)``，前三个是约定签名；``deps`` 仅供测试）::

    deps = {
      "fetch_json":  callable(url) -> {"ok":True,"value":obj} | {"ok":False,"error":{...}},
      "fetch_post":  callable(url, body) -> 同上,
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
from datetime import date, datetime, timezone
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
TUSHARE_ENDPOINT = "http://api.tushare.pro"

DEFAULT_TIMEOUT = 25.0
# openbb 的 import 本身以十秒计（本机实测冷启动 ~44-57s），且它的 provider 取数另计，
# 所以要一条独立的长超时；短超时会把「能取到」误判成失败。
OPENBB_TIMEOUT = 180.0
OPENBB_IMPORT_TIMEOUT = 150.0

# tushare API → 请求 fields / 请求参数 / 默认条数。fields 与 V3 原型（已退役）实现
# tushare.mjs 的选择一致（那里按用途写死，这里按 api 名分发）。
TUSHARE_APIS = {
    "income": {
        "fields": "ts_code,end_date,revenue,operate_profit,total_profit,n_income",
        "params": ("ts_code", "period"),
        "default_limit": 8,
    },
    "daily": {
        "fields": "trade_date,open,high,low,close,vol,amount",
        "params": ("ts_code", "start_date", "end_date"),
        "default_limit": 60,
    },
    "daily_basic": {
        "fields": "trade_date,pe_ttm,pb,ps_ttm,turnover_rate,volume_ratio",
        "params": ("ts_code", "trade_date"),
        "default_limit": 60,
    },
    "stock_basic": {
        "fields": "ts_code,name,industry,market,list_date",
        "params": (),
        "default_limit": 20,
    },
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


def http_post_json(url, body, headers=None, timeout=None):
    """POST JSON + JSON。返回 ``{ok,value}`` 或 ``{ok:false,error}``；**永不抛异常**。"""
    headers = dict(headers or {})
    headers.setdefault("content-type", "application/json")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    timeout = float(timeout or DEFAULT_TIMEOUT)
    try:
        try:
            import httpx
        except ImportError:
            return {"ok": True, "value": _urllib_post_json(url, payload, headers, timeout)}
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            response = client.post(url, content=payload, headers=headers)
        if response.status_code != 200:
            return envelope_error(f"tushare/http-{response.status_code}", f"{url} → HTTP {response.status_code}")
        return {"ok": True, "value": response.json()}
    except Exception as error:  # noqa: BLE001
        code = "tushare/timeout" if "timeout" in type(error).__name__.lower() else "tushare/network"
        return envelope_error(code, f"{url} → {_error_text(error)}")


def _urllib_post_json(url, payload, headers, timeout):
    import urllib.request

    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


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
        fetch_post=None,
        akshare=None,
        openbb=None,
        env=None,
        home=None,
        timeout=None,
        openbb_timeout=None,
    ):
        self._fetch_json = fetch_json
        self._fetch_post = fetch_post
        self._akshare = akshare
        self._openbb = openbb
        self._env = env
        self._module_cache = {}
        self.home = None if home is None else str(home)
        self.timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
        self.openbb_timeout = float(openbb_timeout if openbb_timeout is not None else OPENBB_TIMEOUT)

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

    def json_post(self, url, body, headers=None, timeout=None):
        if self._fetch_post is not None:
            try:
                # 命名参数调用：测试替身按 ``(url, body, headers, timeout)`` 这一显式契约签名，
                # 位置调用会让 body/headers 错位而不报错。
                return self._fetch_post(url, body=body, headers=headers, timeout=timeout or self.timeout)
            except Exception as error:  # noqa: BLE001
                return envelope_error("tushare/network", f"{url} → {_error_text(error)}")
        return http_post_json(url, body, headers=headers, timeout=timeout or self.timeout)

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
    """``ak.stock_news_em(symbol=...)`` → 规格行形状（标题/摘要/时间/来源/链接/关键词）。"""
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
    try:
        with _SocketTimeoutGuard(deps.timeout):
            frame = func(symbol=code)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/stock_news_em", _error_text(error))
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
    }


def fetch_spot(deps, limit):
    """``ak.stock_zh_a_spot_em()`` → A 股实时快照。

    实测该接口常以 ``RemoteDisconnected`` 直接断连（东财侧限流/风控），此时**如实报错**：
    不重试成假数据、不返回空 rows 冒充成功。
    """
    try:
        ak = akshare_module(deps)
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/missing", f"akshare 不可用：{_error_text(error)}")
    func = getattr(ak, "stock_zh_a_spot_em", None)
    if not callable(func):
        return envelope_error("akshare/missing-func", "akshare.stock_zh_a_spot_em 不存在（版本不兼容？）")
    try:
        with _SocketTimeoutGuard(deps.timeout):
            frame = func()
    except Exception as error:  # noqa: BLE001
        return envelope_error("akshare/stock_zh_a_spot_em", _error_text(error))
    rows = []
    for row in _head(_rows_from_frame(frame), limit):
        rows.append(
            {
                "code": as_text(row.get("代码")),
                "name": as_text(row.get("名称")),
                "price": as_number(row.get("最新价")),
                "change_pct": as_number(row.get("涨跌幅")),
                "turnover_rate": as_number(row.get("换手率")),
                "volume_ratio": as_number(row.get("量比")),
                "pe": as_number(row.get("市盈率-动态")),
                "pb": as_number(row.get("市净率")),
            }
        )
    return {"ok": True, "as_of": now_iso(), "source": "akshare/stock_zh_a_spot_em", "rows": rows}


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


# ── Tushare Pro ────────────────────────────────────────────────────────────────


def fetch_tushare(deps, api, params, limit):
    """POST ``api.tushare.pro``。**未注入 token 时不发任何请求**。"""
    meta = TUSHARE_APIS.get(api)
    if meta is None:
        wanted = " / ".join(sorted(TUSHARE_APIS))
        return envelope_error("tushare/unknown-api", f"api 需为 {wanted}，收到 {api!r}")
    # 凭据来源：环境变量优先，其次页面配置（server/v3_credentials.py，0600 落盘）
    from server import v3_credentials

    token, token_source = v3_credentials.resolve_tushare_token(getattr(deps, "home", None), deps.get_env)
    if not token:
        return envelope_error("tushare/no-token",
                              "TUSHARE_TOKEN 未注入（可在「接入与授权」页配置，或用环境变量）")
    request_params = {}
    for name in meta["params"]:
        value = params.get(name)
        if value not in (None, ""):
            request_params[name] = value
    if "ts_code" in meta["params"]:
        if not request_params.get("ts_code"):
            return envelope_error("tushare/bad-args", f"{api} 需要 ts_code（如 600519.SH）")
        request_params["ts_code"] = as_text(request_params["ts_code"]).upper()
    if "limit" not in request_params and limit:
        request_params["limit"] = limit
    body = {"api_name": api, "token": token, "params": request_params, "fields": meta["fields"]}
    result = deps.json_post(TUSHARE_ENDPOINT, body, timeout=deps.timeout)
    if not result.get("ok"):
        error = result.get("error") or {}
        return envelope_error(error.get("code", "tushare/network"), error.get("message", "tushare 请求失败"))
    payload = result.get("value")
    if not isinstance(payload, dict):
        return envelope_error("tushare/bad-payload", "tushare 返回值不是对象")
    if payload.get("code") != 0:
        # 上游错误码透传：``msg`` 是 tushare 的原话（积分不足/接口没权限等），不要改写
        return envelope_error("tushare/api", as_text(payload.get("msg")) or f"tushare 返回 code={payload.get('code')}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    columns = list(data.get("fields") or [])
    items = data.get("items") or []
    rows = []
    for item in items:
        if isinstance(item, dict):
            rows.append({key: item.get(key) for key in columns} if columns else item)
            continue
        if isinstance(item, (list, tuple)):
            rows.append({columns[i] if i < len(columns) else f"col{i}": value for i, value in enumerate(item)})
    return {"ok": True, "api": api, "as_of": now_iso(), "source": f"tushare/{api}", "rows": rows}


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
        """A 股实时快照。上游常以 RemoteDisconnected 断连——如实报错，不伪造。"""
        try:
            payload = await asyncio.to_thread(fetch_spot, deps, to_int(limit, 20, 1, 500))
        except Exception as error:  # noqa: BLE001
            payload = envelope_error("akshare/internal", _error_text(error))
        return payload

    @app.get("/api/v3/financials")
    async def v3_financials(ticker: str = "", statement: str = "income", periods: int = 4):
        """SEC EDGAR 三表（us-gaap XBRL）。未申报的标签进 missing，不编造。"""
        try:
            payload = await asyncio.to_thread(
                sec.financials, ticker, as_text(statement).lower() or "income", to_int(periods, 4, 1, 12)
            )
        except Exception as error:  # noqa: BLE001
            payload = envelope_error("sec/internal", _error_text(error))
        return payload

    @app.get("/api/v3/tushare")
    async def v3_tushare(api: str = "", ts_code: str = "", period: str = "", limit: int = 0):
        """Tushare Pro。token 未注入时不发任何请求。"""
        params = {"ts_code": ts_code, "period": period}
        api_name = as_text(api).lower()
        meta = TUSHARE_APIS.get(api_name)
        effective_limit = to_int(limit, meta["default_limit"] if meta else 20, 1, 5000)
        try:
            payload = await asyncio.to_thread(fetch_tushare, deps, api_name, params, effective_limit)
        except Exception as error:  # noqa: BLE001
            payload = envelope_error("tushare/internal", _error_text(error))
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

    app.state.v3_sources = {
        "deps": deps,
        "sec": sec,
        "v3_run": v3_run,
        "routes": ("/api/v3/news", "/api/v3/spot", "/api/v3/financials", "/api/v3/tushare", "/api/v3/openbb"),
    }
    return deps
