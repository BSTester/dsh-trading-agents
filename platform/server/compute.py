"""计算桥（WP6 补遗 C）：Host 侧取数一律经子进程，绝不在服务进程内 import 分析模块。

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
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# 脚本目录：analytics.js:8 / series.js:8 的 `new URL("../python/", import.meta.url)`。
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "workbench" / "python"
# python 可执行：有意差异 1（sys.executable，不硬编码 venv）。
PYTHON = sys.executable

TIMEOUT = 180_000  # analytics.js:67 execFile timeout 180s
SNAPSHOT_TIMEOUT = 60_000  # pycore.js:18 默认 timeout 60s


class ComputeError(RuntimeError):
    """取数失败（子进程无 JSON / error 键 / 参数非法）；消息即可读原因。"""


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
    """analytics.js:23-27 modeOf：缺省 sim，非法即错。"""
    mode = "sim" if value is None else value
    if mode not in MODES:
        raise ComputeError("Invalid mode")
    return mode


def _int_in_range(value, fallback, minimum, maximum, label):
    """analytics.js:29-35 intInRange：缺省回落，越界/非整数报 ``Invalid <label> (min..max)``。"""
    number = fallback if value is None else value
    if not _is_int(number) or number < minimum or number > maximum:
        raise ComputeError(f"Invalid {label} ({minimum}..{maximum})")
    return number


def _ticker_of(value):
    """analytics.js:98/103/128/166 的单标的校验（TICKER 正则 + 字符串）。"""
    if not isinstance(value, str) or TICKER.match(value) is None:
        raise ComputeError("Invalid ticker")
    return value


def _ticker_list(value, minimum, message):
    """analytics.js:134-138/145-148/188-191 的标的列表校验。"""
    if not isinstance(value, list) or len(value) < minimum or len(value) > 8:
        raise ComputeError(message)
    if any(not isinstance(item, str) or TICKER.match(item) is None for item in value):
        raise ComputeError("Invalid ticker in list")
    return value


def _spawn(command, timeout):
    """真实子进程调用（``analytics.js:66-67 execFile`` 的等价物）；返回 ``CompletedProcess``。"""
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout / 1000.0, check=False)
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
    """
    ticker = _ticker_of(payload.get("ticker"))
    strategy = payload.get("strategy") or "ma_cross"
    if strategy not in STRATEGIES:
        raise ComputeError("Invalid strategy")
    metric = payload.get("metric") or "total_return"
    if metric not in METRICS:
        raise ComputeError("Invalid metric")
    for field in ("fast_grid", "slow_grid", "buy_grid", "sell_grid"):
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or GRID.match(value) is None:
            raise ComputeError(f"Invalid {field}")
        for part in value.split(","):
            number = int(part)
            if number < 1 or number > 500:
                raise ComputeError(f"Invalid {field} value")
    start = payload.get("start") or "2023-01-01"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", start) is None:
        raise ComputeError("Invalid start date")
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
    """analytics.js:143-155：``ic --tickers .. --factor .. --forward .. --window ..``（3..8 标的）。"""
    tickers = _ticker_list(payload.get("tickers"), 3, "IC 需要 3..8 个标的（横截面相关）")
    factor = payload.get("factor") or "mom_20"
    if factor not in FACTORS:
        raise ComputeError("Invalid factor")
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
        raise ComputeError("Unexpected sources field")
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


def analytics_providers(runner):
    """以某个 runner（真实子进程或注入替身）构造 ``{endpoint: callable(payload, force)}``。

    这是 ``analytics.js:81-196 createAnalyticsProvider()`` 返回的对象形状：app.py 直接把它
    当作分析层注入；注入替身时整条链路（参数校验 → 子进程 → 解析）保持真实，只换掉进程。
    """
    return {
        name: (lambda payload, force, script=script, build=build: run_script(
            script, build(payload or {}, force), runner=runner))
        for name, (script, build, _source) in ENDPOINTS.items()
    }


# 默认 provider 表：app.py 的分析层入口（Node 侧 createAnalyticsProvider() 的等价物）
DEFAULT_ANALYTICS = analytics_providers(None)


def analytics(endpoint, payload=None, force=False):
    """按端点表取数：``analytics.js`` 各 provider 方法的等价物（``force`` = options.refresh）。"""
    provider = DEFAULT_ANALYTICS.get(endpoint)
    if provider is None:
        raise ComputeError(f"Unknown analytics endpoint {endpoint}")
    return provider(payload or {}, force)


def series(ticker, period="5m", limit=300, runner=None):
    """``series.js:26-43``：校验（ticker 正则 / period 白名单 / limit 20..2000）→ bars.py → JSON。"""
    if not isinstance(ticker, str) or TICKER.match(ticker) is None:
        raise ComputeError("Invalid ticker")
    if period not in PERIODS:
        raise ComputeError("Invalid period")
    if not _is_int(limit) or limit < 20 or limit > 2000:
        raise ComputeError("Invalid limit (20..2000)")
    return run_script("bars.py", ["--ticker", ticker, "--period", period, "--limit", str(limit)],
                      timeout=120_000, runner=runner)  # series.js:37 timeout 120s


SNAPSHOT_COMMANDS = ("snapshot-plan", "snapshot-schedule", "snapshot-reconcile")


def snapshot_cli(name, timeout=SNAPSHOT_TIMEOUT, runner=None):
    """``pycore.js:18-26`` + ``corebridge.js:6-18``：``python -m trading_core <name>``。"""
    if name not in SNAPSHOT_COMMANDS:
        raise ComputeError(f"Unknown snapshot command {name}")
    command = [PYTHON, "-m", "trading_core", name]
    spawn = _spawn if runner is None else runner
    completed = spawn(command, timeout)
    return parse_stdout(f"trading_core {name}", completed.stdout, completed.returncode,
                        completed.stderr)


def command_home(home):
    """指令目录根：``$DSH_HOME``（pycore.js:9-11 pythonHome）。"""
    return Path(home if home is not None else os.environ.get("DSH_HOME") or Path.home() / ".dsh")


def _load_write_command():
    """进程内导入 ``trading_core.commands``（指令写盘唯一入口，白名单在该模块内）。

    先直接 import（venv 里已 ``pip install -e`` 了 trading_core）；失败时退回
    ``plugins/core/python``（与 tests/test_core_wp6_approval.py 的导入方式一致）。
    """
    try:
        from trading_core import commands  # noqa: PLC0415 —— 延迟导入，避免 import 期副作用
        return commands.write_command
    except ImportError:
        core_python = str(ROOT / "plugins" / "core" / "python")
        if core_python not in sys.path:
            sys.path.insert(0, core_python)
        from trading_core import commands  # noqa: PLC0415
        return commands.write_command


def write_command(home, type_, payload):
    """``commandbus.js writeCommand``：校验后的指令原子落盘，返回 nonce。

    只传 ``plan_hash``/``expected_mode`` 这类白名单字段；口令字段由 app.py 在调用前丢弃，
    因此永远不会出现在 ``trading-commands/pending/*.json``（规格 §5.2 P3）。
    """
    return _load_write_command()(str(command_home(home)), type_, payload)
