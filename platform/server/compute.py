"""计算桥（WP6 补遗 C）：Host 侧**取数一律经子进程**。

唯一例外是两条**写路径**在服务进程内直连 core（``_core_module``）：指令写盘
（``commands.write_command``）与规则批准（``rule_engine.decide_rule``，来源固定
``APPROVER_WEB``）——它们必须与人工动作同进程，且不允许存在能被脚本等价复现的
离线入口（``rules-decide`` CLI 已删除，见 ``rules_decide`` docstring）。

移植源（逐行为准）：
  * ``plugins/workbench/src/analytics.py`` 的参数构造与调用 —— ``analytics.js:59-197``
    （每个端点的 mode/window/tickers/grid/ticker/days/mode+limit 规则、timeout、maxBuffer）；
  * ``plugins/workbench/src/series.js:24-45`` —— bars.py 的 period/limit 默认值与范围校验；
  * ``plugins/workbench/src/pycore.js:18-26`` + ``corebridge.js:6-18`` —— ``python -m trading_core``
    的三个只读快照子命令（60s 超时、``error`` 键抛出）；
  * ``plugins/workbench/src/commandbus.js`` → ``trading_core.commands.write_command``
    （进程内导入；指令类型白名单由该模块自己保证）。

有意差异（见各处注释）：
  1. python 可执行**不硬编码 venv 路径**，统一用 ``sys.executable``：服务进程由 venv 启动时
     即等价于 ``analytics.js:18-21 pythonPath()``，同时免去 Windows 分支与 DSH_HOME 依赖。
  2. ``analytics.js:42-79`` 的脚本级形状表（shapeOf）不在此重复：本模块只负责「进程 + JSON +
     error 键」，最小字段校验统一由 ``caches.ENDPOINT_SHAPE``（endpoints.js:42-60）承担，
     避免同一事实出现两份实现（失败语义不变：形状不符由 cached() 抛错并按失败处理）。
  3. 校验失败的消息沿用 JS 侧原文（"Invalid mode" 等），只是异常类型换成 ComputeError，
     便于 app.py 统一映射到各端点的错误码。
  4. 每个 provider 实例自带一层 30s 内层结果缓存（analytics.js:16/62-77 的 ``Map``，
     含 positions 的 skipCache 例外），见 ``analytics_providers``。
  5. 白名单集合判定统一加 ``isinstance(str)``：JS 的 ``Set.has``/``includes`` 对任意类型
     都返回布尔，而 Python 的 ``in frozenset`` 对不可哈希值抛 TypeError。
"""
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# 脚本目录：analytics.js:8 / series.js:8 的 `new URL("../python/", import.meta.url)`。
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "workbench" / "python"
# python 可执行：有意差异 1（sys.executable，不硬编码 venv）。
PYTHON = sys.executable

# 子进程的数据层版本（WP10 任务 1 补全 run.py 的同一条策略）：venv 的
# ``dsh-trading-python.pth`` 把 ``$DSH_HOME/trading-python/{datasource,core}`` 加进
# sys.path——那是安装器解出的**副本**，新增子命令/模块不会自动同步。服务进程自己已由
# run.py 把仓库路径插到最前，但 ``subprocess`` 起的是**新解释器**，不继承父进程 sys.path，
# 因此仓库内运行时会解析到旧副本（实测：新子命令报 ``invalid choice``）。
#
# 该策略与 core 作业的 ``daemon._subprocess_runner`` 共用同一实现
# （``trading_datasource.repo_paths``）：两处要回答的是同一个问题——「子进程该吃仓库
# 代码还是安装副本」——各写一份必然漂移（历史上正是两份）。行为不变：仓库数据层存在
# 才前置、保留既有 PYTHONPATH 其它项且不重复、无仓库时返回 None 回落副本。
from trading_datasource import repo_paths  # noqa: E402


def _subprocess_env():
    """子进程环境：仓库数据层优先（存在才前置，绝不覆盖既有 PYTHONPATH 的其它项）。"""
    return repo_paths.repo_pythonpath_env()

TIMEOUT = 180_000  # analytics.js:67 execFile 默认 timeout 180s
SNAPSHOT_TIMEOUT = 60_000  # pycore.js:18 默认 timeout 60s
# analytics.js 里逐个写死的 timeout：只有 instrument 不用默认值（analytics.js:170 = 120s）
ENDPOINT_TIMEOUT_MS = {"instrument": 120_000}
# analytics.js:16 / series.js:13 的内层结果缓存 TTL：同一 (脚本, 参数) 30s 内不重起子进程
INNER_CACHE_TTL_MS = 30_000


class ComputeError(RuntimeError):
    """取数失败（子进程超时/无法启动/无 JSON/error 键）；消息即可读原因。"""


class PayloadError(ComputeError):
    """**调用方载荷**非法（参数取值/区间/形状）——与「引擎不可用」严格区分。

    E2E 缺陷 4（2026-09-17）：此前两者共用 ``ComputeError``，web 层把参数拼错也报成
    ``trading/analytics-unavailable``（引擎不可用），用户被引去查服务而不是改参数。
    归类以**异常类型**为准（``caches.cached(payload_error_types=…)``），不做消息文本匹配。
    子类关系保证既有 ``except ComputeError`` 的调用方行为不变。
    """


# analytics.js:10-16 的常量集合
MODES = frozenset(("sim", "live"))
STRATEGIES = frozenset(("ma_cross", "rsi"))
METRICS = frozenset(("total_return", "annualized", "sharpe", "max_drawdown", "win_rate"))
GRID = re.compile(r"^\d{1,3}(,\d{1,3}){1,7}$")
FACTORS = frozenset(("mom_20", "mom_60", "vol_20", "trend", "rsi_14", "liq_ratio", "mdd_60"))
TICKER = re.compile(r"^[A-Za-z0-9.^-]{1,40}$")
# series.js:10-11
PERIODS = frozenset(("1m", "5m", "15m", "30m", "60m", "1d"))


def _is_int(value):
    """JS Number.isInteger：bool 不算整数（Python 的 bool 是 int 子类，必须排除）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _mode_of(value):
    """analytics.js:23-27 modeOf：缺省 sim，非法即错。

    ``isinstance(str)`` 是 JS ``MODES.has(value)`` 的严格等价物：非字符串（数组/数字/布尔）
    在 JS 里一律 false，而在 Python 里直接 ``value in frozenset`` 会对不可哈希值抛 TypeError。
    """
    mode = "sim" if value is None else value
    if not isinstance(mode, str) or mode not in MODES:
        raise PayloadError("Invalid mode")
    return mode


def _int_in_range(value, fallback, minimum, maximum, label):
    """analytics.js:29-35 intInRange：缺省回落，越界/非整数报 ``Invalid <label> (min..max)``。"""
    number = fallback if value is None else value
    if not _is_int(number) or number < minimum or number > maximum:
        raise PayloadError(f"Invalid {label} ({minimum}..{maximum})")
    return number


def _ticker_of(value):
    """analytics.js:98/103/128/166 的单标的校验（TICKER 正则 + 字符串）。"""
    if not isinstance(value, str) or TICKER.match(value) is None:
        raise PayloadError("Invalid ticker")
    return value


def _ticker_list(value, minimum, message):
    """analytics.js:134-138/145-148/188-191 的标的列表校验。"""
    if not isinstance(value, list) or len(value) < minimum or len(value) > 8:
        raise PayloadError(message)
    if any(not isinstance(item, str) or TICKER.match(item) is None for item in value):
        raise PayloadError("Invalid ticker in list")
    return value


def timeout_for(endpoint, default=TIMEOUT):
    """端点级 timeout（analytics.js:67 默认 180s；instrument 120s，analytics.js:170）。"""
    return ENDPOINT_TIMEOUT_MS.get(endpoint, default)


def _spawn(command, timeout):
    """真实子进程调用（``analytics.js:66-67 execFile`` 的等价物）；返回 ``CompletedProcess``。

    ``env`` 由 ``_subprocess_env`` 提供（仓库数据层优先），保证子进程与父进程解析到
    同一份 ``trading_core``/``trading_datasource``。
    """
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout / 1000.0, check=False,
                              env=_subprocess_env())
    except subprocess.TimeoutExpired as error:
        raise ComputeError(f"{command[1]} 超时（{int(timeout / 1000)}s）") from error
    except OSError as error:
        raise ComputeError(f"{command[1]} 无法启动：{error}") from error


def parse_stdout(script, stdout, returncode=0, stderr=""):
    """把脚本 stdout 解析成结果对象（``analytics.js:68-73`` + ``pycore.js:21-25`` 的共用部分）。

    * stdout 首个 ``{`` 起 parse；
    * 找不到 JSON / 不是合法 JSON → ComputeError（带上 stderr 末行便于定位）；
    * ``error`` 键 → ComputeError（消息即 ``value.error``，analytics.js:71）。
    """
    start = (stdout or "").find("{")
    if start < 0:
        lines = (stderr or "").strip().splitlines()
        detail = lines[-1] if lines else f"exit {returncode}"
        raise ComputeError(f"{script} 无 JSON 输出：{detail}")
    try:
        value = json.loads(stdout[start:])
    except json.JSONDecodeError as error:
        raise ComputeError(f"{script} 输出不是合法 JSON：{error}") from error
    if isinstance(value, dict) and value.get("error"):
        raise ComputeError(str(value["error"]))
    return value


def run_script(script, args, timeout=TIMEOUT, runner=None):
    """``analytics.js:62-79 call()`` 的子进程部分：跑 ``<script> <args...>`` 并解析 stdout。

    ``runner`` 可注入（默认走真实子进程），用于离线钉住「脚本 + 参数 → 解析」链路。
    任何进程级失败（非零退出、超时、无 JSON）同样抛 ComputeError，交上层落成失败信封。
    """
    command = [PYTHON, str(SCRIPTS / script), *[str(arg) for arg in args]]
    spawn = _spawn if runner is None else runner
    completed = spawn(command, timeout)
    return parse_stdout(script, completed.stdout, completed.returncode, completed.stderr)


# ---------------------------------------------------------------------------
# 端点 → 脚本 + 参数构造函数（analytics.js:82-196 的逐端点对齐）
# ---------------------------------------------------------------------------

def _equity_args(payload, force):
    """analytics.js:82-86：``equity --mode <mode> --window <window>``（缺省 250，20..1000）。"""
    mode = _mode_of(payload.get("mode"))
    window = _int_in_range(payload.get("window"), 250, 20, 1000, "window")
    return ["equity", "--mode", mode, "--window", str(window)]


def _positions_args(payload, force):
    """analytics.js:89-93：``positions --mode <mode> [--refresh]``（无 window/limit）。"""
    mode = _mode_of(payload.get("mode"))
    args = ["--mode", mode]
    if force:
        args.append("--refresh")
    return args


def _correlation_args(payload, force):
    """analytics.js:186-194：``correlation --tickers a,b --window <window>``（缺省 120，40..500）。"""
    tickers = _ticker_list(payload.get("tickers"), 2, "tickers must list 2..8 symbols")
    window = _int_in_range(payload.get("window"), 120, 40, 500, "window")
    return ["correlation", "--tickers", ",".join(tickers), "--window", str(window)]


def _sensitivity_args(payload, force):
    """analytics.js:101-124：ticker/strategy/metric/grid/start 全校验后才进子进程。

    grid 为空时**不下发**该参数（analytics.js:120-123 的 `if (payload.X_grid)`），
    让 sensitivity.py 用自己声明的默认网格（sensitivity.py:64-67）。

    默认值语义逐条对齐 JS 的 ``??``（只在 null/undefined 时兜底，空串是非法值）：
      * ``payload.strategy``：none → "ma_cross"，``""`` → Invalid strategy；
      * ``payload.metric``：none → "total_return"，``""`` → Invalid metric；
      * ``payload.start``：none → "2023-01-01"，``""`` → Invalid start date；
      * grid 字段：JS 用 ``value === undefined`` 跳过，因此这里用 ``field not in payload``
        判定——显式 ``null`` 与 JS 的 ``null`` 一样要报 Invalid <field>。
    """
    ticker = _ticker_of(payload.get("ticker"))
    strategy = payload.get("strategy")
    if strategy is None:
        strategy = "ma_cross"
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise PayloadError("Invalid strategy")
    metric = payload.get("metric")
    if metric is None:
        metric = "total_return"
    if not isinstance(metric, str) or metric not in METRICS:
        raise PayloadError("Invalid metric")
    for field in ("fast_grid", "slow_grid", "buy_grid", "sell_grid"):
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str) or GRID.match(value) is None:
            raise PayloadError(f"Invalid {field}")
        for part in value.split(","):
            number = int(part)
            if number < 1 or number > 500:
                raise PayloadError(f"Invalid {field} value")
    start = payload.get("start")
    if start is None:
        start = "2023-01-01"
    if not isinstance(start, str) or re.match(r"^\d{4}-\d{2}-\d{2}$", start) is None:
        raise PayloadError("Invalid start date")
    args = ["--ticker", ticker, "--strategy", strategy, "--metric", metric, "--start", start]
    for field, flag in (("fast_grid", "--fast-grid"), ("slow_grid", "--slow-grid"),
                        ("buy_grid", "--buy-grid"), ("sell_grid", "--sell-grid")):
        if payload.get(field):
            args.extend([flag, payload[field]])
    return args


def _events_args(payload, force):
    """analytics.js:126-132：``events --ticker <t> --days <days>``（缺省 180，30..2000）。"""
    ticker = _ticker_of(payload.get("ticker"))
    days = _int_in_range(payload.get("days"), 180, 30, 2000, "days")
    return ["--ticker", ticker, "--days", str(days)]


def _factors_args(payload, force):
    """analytics.js:133-142：``snapshot --tickers a,b --window <window>``（2..8 标的，80..1000）。"""
    tickers = _ticker_list(payload.get("tickers"), 2, "tickers must list 2..8 symbols")
    window = _int_in_range(payload.get("window"), 250, 80, 1000, "window")
    return ["snapshot", "--tickers", ",".join(tickers), "--window", str(window)]


def _ic_args(payload, force):
    """analytics.js:143-155：``ic --tickers .. --factor .. --forward .. --window ..``（3..8 标的）。

    ``payload.factor`` 是 JS ``payload.factor ?? "mom_20"``：只有 null/undefined 才兜底，
    ``""`` 必须报 Invalid factor（不能像 ``or`` 那样被吞成默认值）。
    """
    tickers = _ticker_list(payload.get("tickers"), 3, "IC 需要 3..8 个标的（横截面相关）")
    factor = payload.get("factor")
    if factor is None:
        factor = "mom_20"
    if not isinstance(factor, str) or factor not in FACTORS:
        raise PayloadError("Invalid factor")
    forward = _int_in_range(payload.get("forward"), 5, 1, 60, "forward")
    window = _int_in_range(payload.get("window"), 250, 80, 1000, "window")
    return ["ic", "--tickers", ",".join(tickers), "--factor", factor,
            "--forward", str(forward), "--window", str(window)]


def _risk_args(payload, force):
    """analytics.js:178-180：``risk``（无参）。"""
    return ["risk"]


def _trades_args(payload, force):
    """analytics.js:181-185：``trades --mode <mode> --limit <limit>``（缺省 50，1..200）。"""
    mode = _mode_of(payload.get("mode"))
    limit = _int_in_range(payload.get("limit"), 50, 1, 200, "limit")
    return ["trades", "--mode", mode, "--limit", str(limit)]


def _sources_args(payload, force):
    """analytics.js:157-163：``sources [--no-probe]``（no_probe 必须严格为 true）。"""
    extra = [key for key in payload if key != "no_probe"]
    if extra:
        raise PayloadError("Unexpected sources field")
    return ["--no-probe"] if payload.get("no_probe") is True else []


def _instrument_args(payload, force):
    """analytics.js:164-177：``instruments.py --ticker <t>``。"""
    return ["--ticker", _ticker_of(payload.get("ticker"))]


def _quality_args(payload, force):
    """analytics.js:96-100：``quality.py --ticker <t>``。"""
    return ["--ticker", _ticker_of(payload.get("ticker"))]


# 端点 → (脚本, 参数构造函数, 源行号)。analytics.js 的 12 个分析端点全覆盖。
ENDPOINTS = {
    "equity": ("analytics.py", _equity_args, "analytics.js:82-86"),
    "positions": ("positions.py", _positions_args, "analytics.js:89-93"),
    "correlation": ("analytics.py", _correlation_args, "analytics.js:186-194"),
    "sensitivity": ("sensitivity.py", _sensitivity_args, "analytics.js:101-124"),
    "risk": ("analytics.py", _risk_args, "analytics.js:178-180"),
    "trades": ("analytics.py", _trades_args, "analytics.js:181-185"),
    "events": ("events.py", _events_args, "analytics.js:126-132"),
    "factors": ("factors.py", _factors_args, "analytics.js:133-142"),
    "ic": ("factors.py", _ic_args, "analytics.js:143-155"),
    "sources": ("sources.py", _sources_args, "analytics.js:157-163"),
    "instrument": ("instruments.py", _instrument_args, "analytics.js:164-177"),
    "quality": ("quality.py", _quality_args, "analytics.js:96-100"),
}


def analytics_providers(runner, now=None):
    """以某个 runner（真实子进程或注入替身）构造 ``{endpoint: callable(payload, force)}``。

    这是 ``analytics.js:81-196 createAnalyticsProvider()`` 返回的对象形状：app.py 直接把它
    当作分析层注入；注入替身时整条链路（参数校验 → 子进程 → 解析）保持真实，只换掉进程。

    Q-2：provider 内部还有一层 30s 结果缓存（analytics.js:16/62-64/77）。它与 rpc.js 的
    TTL 缓存**不是**同一层：rpc.js 的 ``_refresh`` 只绕过外层（TTL 分钟级），而同一
    (脚本, 参数) 在 30s 内仍由内层直接返回、不重起子进程——audit 端点正是靠它避免每次
    请求都去取一次 trades 台账（audit 自身不进 rpc.js 的 cached 包装，rpc.js:132-145）。
    唯一的例外是 positions：``options.refresh`` 会 skipCache（analytics.js:93），
    因此 ``force`` 对 positions 跳过读内层缓存（仍照常写回）。
    """
    clock = (lambda: time.time() * 1000) if now is None else now
    inner = {}
    lock = threading.Lock()  # app 侧用 to_thread 并发调用，内层缓存必须自己加锁

    def produce(name, script, build, payload, force):
        args = build(payload or {}, force)
        key = json.dumps([script, [str(arg) for arg in args]], ensure_ascii=False)
        with lock:
            hit = inner.get(key)
        if (hit is not None and clock() - hit["at"] < INNER_CACHE_TTL_MS
                and not (name == "positions" and force)):
            return hit["value"]
        value = run_script(script, args, timeout=timeout_for(name), runner=runner)
        with lock:
            inner[key] = {"at": clock(), "value": value}
        return value

    return {
        name: (lambda payload, force, name=name, script=script, build=build:
               produce(name, script, build, payload, force))
        for name, (script, build, _source) in ENDPOINTS.items()
    }


# 默认 provider 表：app.py 的分析层入口（Node 侧 createAnalyticsProvider() 的等价物）
DEFAULT_ANALYTICS = analytics_providers(None)


def analytics(endpoint, payload=None, force=False):
    """按端点表取数：``analytics.js`` 各 provider 方法的等价物（``force`` = options.refresh）。"""
    provider = DEFAULT_ANALYTICS.get(endpoint)
    if provider is None:
        raise PayloadError(f"Unknown analytics endpoint {endpoint}")
    return provider(payload or {}, force)


def series(ticker, period="5m", limit=300, runner=None):
    """``series.js:26-43``：校验（ticker 正则 / period 白名单 / limit 20..2000）→ bars.py → JSON。

    period 用 ``isinstance(str)`` 判定：series.js 的 ``PERIODS.has(period)`` 对非字符串一律
    false，而 Python 的 ``value in frozenset`` 会对不可哈希值抛 TypeError（会漏出非
    ``Invalid period`` 的失败消息）。显式 ``null``/``""`` 都是 Invalid period（JS 默认值
    只在 undefined 时生效，见 series.js:28 的解构默认）。
    """
    if not isinstance(ticker, str) or TICKER.match(ticker) is None:
        raise ComputeError("Invalid ticker")
    if not isinstance(period, str) or period not in PERIODS:
        raise PayloadError("Invalid period")
    if not _is_int(limit) or limit < 20 or limit > 2000:
        raise PayloadError("Invalid limit (20..2000)")
    return run_script("bars.py", ["--ticker", ticker, "--period", period, "--limit", str(limit)],
                      timeout=120_000, runner=runner)  # series.js:37 timeout 120s


SNAPSHOT_COMMANDS = ("snapshot-plan", "snapshot-schedule", "snapshot-reconcile",
                     "snapshot-pipeline")


def snapshot_cli(name, timeout=SNAPSHOT_TIMEOUT, runner=None):
    """``pycore.js:18-26`` + ``corebridge.js:6-18``：``python -m trading_core <name>``。"""
    if name not in SNAPSHOT_COMMANDS:
        raise PayloadError(f"Unknown snapshot command {name}")
    command = [PYTHON, "-m", "trading_core", name]
    spawn = _spawn if runner is None else runner
    completed = spawn(command, timeout)
    return parse_stdout(f"trading_core {name}", completed.stdout, completed.returncode,
                        completed.stderr)


def factors_history(limit=30, timeout=SNAPSHOT_TIMEOUT, runner=None):
    """WP7：``python -m trading_core factors-history --limit N``（解析同 snapshot_cli）。

    limit 缺省/None → 30；越界或非整数走 ``_int_in_range`` → ``Invalid limit (1..120)``
    （ComputeError 与全模块同一失败语义，app 侧经 caches.cached 落成 trading/core-unavailable）。
    CLI 的失败信封 ``{ok:false, error}`` 由 parse_stdout 的 error 键分支接住。
    """
    number = _int_in_range(limit, 30, 1, 120, "limit")
    command = [PYTHON, "-m", "trading_core", "factors-history", "--limit", str(number)]
    spawn = _spawn if runner is None else runner
    completed = spawn(command, timeout)
    return parse_stdout("trading_core factors-history", completed.stdout,
                        completed.returncode, completed.stderr)


def sentiment_history(symbol=None, limit=30, timeout=SNAPSHOT_TIMEOUT, runner=None):
    """WP11 任务 3：``python -m trading_core sentiment-history [--symbol S] --limit N``。

    与 ``factors_history`` 同一出口（parse_stdout：CLI 的 ``{ok:false,error}`` 信封
    → ComputeError，app 侧经 caches.cached 落成 trading/core-unavailable）。
    limit 校验同样走 ``_int_in_range``（缺省 30、区间 1..120，消息逐字一致），
    校验失败不起子进程。symbol 非空时透传（形状/类型白名单在路由层，compute 只做参数拼装）。
    """
    number = _int_in_range(limit, 30, 1, 120, "limit")
    command = [PYTHON, "-m", "trading_core", "sentiment-history", "--limit", str(number)]
    if symbol:
        command += ["--symbol", str(symbol)]
    spawn = _spawn if runner is None else runner
    completed = spawn(command, timeout)
    return parse_stdout("trading_core sentiment-history", completed.stdout,
                        completed.returncode, completed.stderr)


def rules_list(status=None, timeout=SNAPSHOT_TIMEOUT, runner=None):
    """WP14 任务 4：``python -m trading_core rules-list [--status S]``（候选池只读列表）。

    与 ``factors_history`` 同一出口（parse_stdout：CLI 的 ``{ok:false,error}`` 信封
    → ComputeError）。**app 侧不走 caches.cached**：候选池状态会随人工批准即时变化，
    缓存住的「待批准」会让界面拿旧结论（与 auto_pipeline/openapi_config 同一口径）。
    ``status`` 的状态枚举校验在 CLI（``rule_engine.RULE_STATUSES`` 的唯一事实源），
    本层只做参数拼装——非法状态经 CLI 失败信封 → ComputeError，调用方如实回错。
    """
    command = [PYTHON, "-m", "trading_core", "rules-list"]
    if status:
        command += ["--status", str(status)]
    spawn = _spawn if runner is None else runner
    completed = spawn(command, timeout)
    return parse_stdout("trading_core rules-list", completed.stdout,
                        completed.returncode, completed.stderr)


#: 规则批准的操作来源：**唯一写入点**，不是参数、不经载荷（见 ``rules_decide``）。
APPROVER_WEB = "web"


def rules_decide(rule_id, decision, home=None):
    """WP14 任务 4：人工批准/停用规则（**服务进程内动作端点**，无 CLI 等价物）。

    批准是「规则上岗」的唯一通道，因此这里刻意没有任何离线入口：

      * ``rules-decide`` **CLI 子命令已删除**——历史实现让 Web 端点 spawn 该子命令、
        操作来源由 ``--by`` 自报，于是 shell 跑同一条命令就能写出
        ``approved_by='web'``，「批准只在 Web」结构性不成立（规格 §9.4/§9.5）；
      * ``by`` **不是参数**：来源由本函数固定为 ``APPROVER_WEB``，调用方无法伪造；
      * 端点载荷白名单（``app.py:RULES_DECIDE_FIELDS``）只留 ``rule_id``/``decision``。

    写路径进程内直连 core：``rule_engine.decide_rule`` 是状态机唯一入口（``enabled``
    只能由它产生并记录批准人），服务进程不另写一份状态流转。非法参数在**连接数据库
    之前**就拒绝（零 DB 开销）；业务拒绝（未过验证门就启用/规则不存在/非法流转）以
    ``ComputeError`` 回，app 侧落 ``trading/invalid-operation``，绝不当成功。
    """
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise PayloadError("Invalid rule_id")
    if decision not in ("enable", "disable"):
        raise PayloadError(f"Invalid decision {decision!r} (enable/disable)")
    store = _core_module("store")
    rule_engine = _core_module("rule_engine")
    conn = store.connect(store.db_path(str(command_home(home))))
    try:
        try:
            status = rule_engine.decide_rule(conn, rule_id, decision, by=APPROVER_WEB)
        except ValueError as error:
            raise ComputeError(str(error)) from error
    finally:
        conn.close()
    return {"rule_id": rule_id, "status": status}


#: 值班研究员队列的合法状态（store 状态机的全部取值，规格 §10.2）。
TASK_STATUSES = frozenset(("pending", "running", "done", "failed"))
#: 队列列表单页上限（防一次拉全表）。
TASK_LIST_MAX = 200


def _task_conn(home):
    """打开队列所在库（list/claim/report 共用的（store, conn）组装）。"""
    store = _core_module("store")
    return store, store.connect(store.db_path(str(command_home(home))))


def research_tasks_claim(home=None, now=None):
    """值班研究员领取一条任务 → ``{"task": {...}|None, "reclaimed": {...}}``。

    三件事的顺序是刻意的：

    1. **先回收**（``research_queue.reclaim``，幂等且便宜）：执行体崩溃留下的 running 若不
       回收会永久卡住队列；领取本身是「执行体还活着」的唯一证据，顺手回收比另挂一个 tick
       作业更省（规格 §10.2）。回收判 failed 的告警由业务层发出。**超时不计 attempts**：
       回收累加的是 ``timeouts``（连续超时 ``TASK_MAX_TIMEOUTS`` 次才 failed，err 写
       「执行体未回报」），``attempts`` 只由执行体回报 ``ok=false`` 累加——两种失败不可混同
       （R2，2026-09-16 审查）。
    2. **再校验队首载荷**（队列即攻击面，规格 §10.3/§十一.13）：直改库塞进来的自由文本键
       在领取这一刻被拒——任务**不被领走**（状态保持 pending）+ critical 告警等人介入。
       拒绝而不是猜测执行，代价是被拒任务会占据队首（队列暂停在这一点上）——这是刻意的
       fail-closed：坏载荷宁可整队停下等人看，也不放一条来历不明的指令进研究侧。
    3. **最后领取**（``store.claim_task``）：状态迁移的唯一入口，服务进程不另写一份。
    """
    store, conn = _task_conn(home)
    research_queue = _core_module("research_queue")
    clock = _core_module("clock")
    alerts = _core_module("alerts")
    root = str(command_home(home))
    try:
        try:
            stamp = clock.now_stamp(now)
        except ValueError as error:
            raise ComputeError(str(error)) from error
        reclaimed = research_queue.reclaim(conn, root, stamp)
        pending = store.peek_task(conn)
        if pending is None:
            return {"task": None, "reclaimed": reclaimed}
        try:
            store.validate_task(pending["kind"], pending["as_of"], pending["market"],
                                pending["payload"])
        except ValueError as error:
            alerts.emit(conn, home=root, level="critical", title="研究任务载荷未通过领取校验",
                        detail=(f"task={pending['task_id']} kind={pending['kind']} "
                                f"market={pending['market']} as_of={pending['as_of']} "
                                f"{error}")[:300])
            raise ComputeError(
                f"队列载荷未通过领取侧校验，任务未领取：task={pending['task_id']} {error}"
            ) from error
        return {"task": store.claim_task(conn, stamp), "reclaimed": reclaimed}
    finally:
        conn.close()


def research_tasks_report(task_id, ok, result_ref=None, err=None, home=None):
    """回报一条任务的结果 → ``{"task": {...}}``（状态机在 ``store.finish_task``）。

    参数在**接触 core（因此也接触 DB）之前**校验：``ok`` 必须是真布尔——用真值判断会让
    ``"false"``/``1``/``""`` 这类载荷悄悄改变任务结局（把 failed 记成 done），这是队列
    里最不该含糊的一个字段。失败达上限转 failed 的告警由业务层（``research_queue``）发。
    **终态幂等**（R1，2026-09-16 审查）：``done``/``failed`` 再回报原样返回、状态与计数
    不变、不重复告警——工具调用重试与网络重放不会把已完成的任务拉回队列。
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise PayloadError("Invalid task_id")
    if not isinstance(ok, bool):
        raise PayloadError(f"Invalid ok (boolean required), got {type(ok).__name__}")
    for label, value in (("result_ref", result_ref), ("err", err)):
        if value is not None and not isinstance(value, str):
            raise PayloadError(f"Invalid {label} (string required), got {type(value).__name__}")
    store, conn = _task_conn(home)
    research_queue = _core_module("research_queue")
    try:
        try:
            task = research_queue.report(conn, str(command_home(home)), task_id.strip(), ok,
                                         result_ref=result_ref, err=err)
        except ValueError as error:
            raise ComputeError(str(error)) from error
        return {"task": task}
    finally:
        conn.close()


def research_tasks_list(status=None, limit=50, home=None):
    """队列列表（可按状态过滤）→ ``{"tasks": [...]}``。

    ``status`` 的状态枚举校验在这里（列表端点是只读面板，拼错状态应当报错而不是静默返回
    空列表——空列表会被读成「没有任务」）；``limit`` 走既有 ``_int_in_range`` 口径。
    """
    if status is not None and status not in TASK_STATUSES:
        raise PayloadError(f"Invalid status {status!r} ({'/'.join(sorted(TASK_STATUSES))})")
    number = _int_in_range(limit, 50, 1, TASK_LIST_MAX, "limit")
    store, conn = _task_conn(home)
    try:
        return {"tasks": store.get_tasks(conn, status=status, limit=number)}
    finally:
        conn.close()


def command_home(home):
    """指令目录根：``$DSH_HOME``（pycore.js:9-11 pythonHome）。"""
    return Path(home if home is not None else os.environ.get("DSH_HOME") or Path.home() / ".dsh")


def _core_module(name):
    """进程内导入 ``trading_core`` 子模块（**只服务写路径**：指令写盘 + 规则审批）。

    读路径一律仍走子进程（``_spawn``）——取数/快照不把分析模块拉进服务进程；
    只有「必须与人工动作同进程、且不能被离线脚本等价复现」的两条写路径例外：
    ``commands.write_command``（计划执行指令落盘）与 ``rule_engine.decide_rule``
    （规则批准，来源固定为 ``APPROVER_WEB``）。

    先直接 import（venv 里已 ``pip install -e`` 了 trading_core）；失败时退回
    ``plugins/core/python``（与 tests/test_core_wp6_approval.py 的导入方式一致）。
    """
    try:
        return importlib.import_module(f"trading_core.{name}")
    except ImportError:
        core_python = str(ROOT / "plugins" / "core" / "python")
        if core_python not in sys.path:
            sys.path.insert(0, core_python)
        return importlib.import_module(f"trading_core.{name}")


def _load_write_command():
    """进程内导入 ``trading_core.commands``（指令写盘唯一入口，白名单在该模块内）。"""
    return _core_module("commands").write_command


def write_command(home, type_, payload):
    """``commandbus.js writeCommand``：校验后的指令原子落盘，返回 nonce。

    只传 ``plan_hash``/``expected_mode`` 这类白名单字段；口令字段由 app.py 在调用前丢弃，
    因此永远不会出现在 ``trading-commands/pending/*.json``（规格 §5.2 P3）。
    """
    return _load_write_command()(str(command_home(home)), type_, payload)
