"""V3.0 Headless Runner + 调度器 + 外部熔断（规格 FR-GATEWAY-003/004、§5.2.3、§7.2、附录 B）。

本模块是 V3.0「Headless 通道」的**唯一实现**：平台侧不再只是「如实标注无此通道」，
而是真的按规格起 ``dsh --profile <profile> "<prompt>"`` 一次性进程、把每次调用落进
``v3_db`` 的 ``headless_log`` 表（FR-MON-003），并按 FR-GATEWAY-004 做**外部熔断**。

逐条对规格
----------
* **FR-GATEWAY-003（Headless Runner）**
  - 命令格式 ``dsh --profile <profile> "<task>"``（``argv`` 里 prompt 是**独立一个参数**，
    不拼 shell——避免注入，也避免 shell 改写退出码）。
  - 通道契约：``stdout`` / ``stderr`` **分两条管道**捕获（绝不 ``2>&1``，那是
    ``scripts/research_duty.sh`` 的旧口径）；退出码映射见 :func:`map_exit_code`
    （0=completed / 1=未完成或运行器失败 / 130=首次 SIGINT 后优雅关闭）。
  - 「退出码 0 不证明副作用存在」这条限制写进每条记录的 ``contract_note``。
* **FR-GATEWAY-004（Headless 调度器）**
  - 定时：08:30 开盘前扫描 / 12:00 午间复盘 / 16:00 收盘后分析（按 ``v3_market_calendar``
    的交易日判定，**非交易日不触发**）；
  - 事件：突发新闻 / 持仓异动 / 风控阈值突破 / 因子信号反转（触发条件**可插拔注册**，
    每条都有真实数据来源，取不到就是 ``no-data``，绝不猜）；
  - 流水线节点：调仓前决策确认 / 策略参数变更审核；
  - 全部触发可开关（``<home>/v3-headless.json`` 或环境变量）、可查
    （``GET /api/v3/headless/schedule`` 给下一次触发时间 + 最近触发记录 + 每条触发条件
    最近一次判定的原因）。
* **外部熔断**（进程内没有 token/费用熔断器，平台必须在外部设限）：
  - 并发上限默认 **3**（超出**排队**，不是丢弃）；
  - 单次超时默认 **300s**（可配）；
  - token 预算默认 **200K**，按 ``stdout+stderr``（含 prompt）字符估算——
    **明确标注是估算**（:data:`TOKEN_ESTIMATE_NOTE`），不是 Harness 的计量口径；
  - 超时/超预算一律 ``killpg(SIGKILL)`` 并**记录被 kill 的事实**（``killed=true`` +
    ``kill_reason``），``success`` 恒为 false，绝不把被杀的调用写成成功。
* **§5.2.3 / 附录 B（提示词构造）**：三类模板（``pre_market_scan`` / ``breaking_news`` /
  ``risk_review``）逐字照规格；headless 每次是全新 Agent、无历史，所以**持仓、风控阈值、
  市场摘要必须打包进提示词**（:class:`ContextBuilder`），取不到数据就在提示词里如实写
  「无数据源（原因）」，不拿 0 或估算值冒充。
* **FR-MON-003（Headless 调用日志）**：每次调用（含**被熔断拦下、根本没起进程**的那次）
  都写 ``headless_log``：prompt/stdout/stderr/exit_code/duration_ms/tokens_estimate/success
  + payload 里的来源、口径、触发条件判定依据。日志只进平台库，不依赖 Harness 的 Session 存储。

**绝不触发交易**（本模块的硬边界，三层）
---------------------------------------
1. 只会用**工具白名单 profile**（默认 ``quant-headless``）：白名单里所有 ``tool-*`` 危险行
   ``disabled: true``，并用 ``PreToolUse`` 命令钩子**逐名拒绝**全部写/交易工具
   （名单由 :func:`deny_tool_names` 从平台自己的 ``v3_ops.WRITE_TOOLS`` +
   ``mcp_tools.MCP_EXCLUDED_ENDPOINTS`` 推导，测试断言「MCP 工具面里没有漏网的写工具」）；
2. 起进程**之前**校验白名单（:func:`verify_whitelist`）：装到机器上的 profile 若与仓库材料
   不符 / 名单覆盖不全 → ``outcome="whitelist-unverified"``，**不起进程**（fail-closed）；
3. 提示词里写死只读边界与禁用端点清单。

本模块**只读**：除 ``headless_log``（调用留痕）与自身的状态文件外不写任何平台数据；
不触碰任何下单/改单/撤单/切模式/执行计划通道。
"""
from __future__ import annotations

import codecs
import json
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import date as _date
from datetime import datetime, time as _time
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import JSONResponse

from server import mcp_tools, v3_db, v3_market_calendar, v3_ops

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 调用日志表（``v3_db.TABLE_SPECS`` 里的唯一事实源；这里只是本模块的引用名）
LOG_TABLE = "headless_log"

#: 配置文件（``<home>/v3-headless.json``）；缺失/损坏按默认值跑并在响应里如实标注
CONFIG_FILENAME = "v3-headless.json"
#: 调度状态文件（已触发的去重键 + 最近触发/判定记录）
STATE_FILENAME = "v3-headless-state.json"

#: 通道契约退出码（FR-GATEWAY-003「通道契约」逐字）
EXIT_COMPLETED = 0
EXIT_INCOMPLETE = 1
EXIT_SIGINT = 130

#: ``outcome`` 取值域（记录里唯一的结果字段；``exit_code`` 记的是**真事实**，
#: 被 kill 时是负的信号号，不会为了好看改写成 0/1）
OUTCOMES = ("completed", "incomplete", "sigint", "killed", "runner-error",
            "timeout", "token-budget", "disabled", "profile-missing",
            "whitelist-unverified", "dsh-not-found")

#: 退出码 → 契约结果
_EXIT_MAP = {EXIT_COMPLETED: "completed", EXIT_INCOMPLETE: "incomplete",
             EXIT_SIGINT: "sigint"}

#: token 估算口径。**这是估算，不是计量**：dsh headless 的 stdout/stderr 只有最终文本，
#: 不回报 usage，平台侧拿不到真实 token 数，所以只能按字符估。
#: ASCII 4 字符/token（英文经验值）、非 ASCII 1 字符/token（中文一字≈一 token，偏保守）。
CHARS_PER_TOKEN_ASCII = 4
TOKEN_ESTIMATE_NOTE = ("估算口径：ASCII 4 字符/token、非 ASCII 1 字符/token（保守，宁高不低）；"
                       "计入 prompt + stdout + stderr 三段字符。dsh headless 不回报 usage，"
                       "故这是**估算**，不是计量口径，不能用于对账/计费。")

#: 「退出码 0 证明什么」。规格 FR-GATEWAY-003 的关键限制，逐条记录时带上，
#: 免得下游把 exit 0 读成「文件/部署/测试/外部效果已存在」。
CONTRACT_NOTE = ("exit 0 只证明 Agent 轮次在其运行时契约下完成（最终原因为 completed），"
                 "不证明所请求的文件、部署、测试或外部效果存在，需要独立验证。")

#: 定时触发的三个时点（FR-GATEWAY-004）。时点按**市场本地时区**（默认 SH=Asia/Shanghai）。
#: ``task_type`` 只能是规格 §5.2.3 定义的三类模板之一；午间/收盘后都用 ``risk_review``
#: 模板，但打包进去的市场摘要窗口不同（见 :data:`SLOT_CONTEXT`）。
SLOTS = (
    {"key": "pre_market_scan", "at": "08:30", "market": "SH",
     "task_type": "pre_market_scan", "label": "开盘前扫描", "window": "pre_open"},
    {"key": "midday_review", "at": "12:00", "market": "SH",
     "task_type": "risk_review", "label": "午间复盘", "window": "intraday"},
    {"key": "post_close", "at": "16:00", "market": "SH",
     "task_type": "risk_review", "label": "收盘后分析", "window": "post_close"},
)

#: 触发条件默认开关（全部可查；见 ``GET /api/v3/headless/schedule``）
DEFAULT_CONFIG = {
    # 总开关：false → 任何触发都不起进程（仍会记录一次 outcome="disabled" 的调用）
    "enabled": True,
    # 工具白名单 profile（仓库材料 platform/install/quant-headless）
    "profile": "quant-headless",
    # 只读边界：白名单校验不通过就不起进程。唯一的逃生口，只给人工排障用
    "requireWhitelist": True,
    # 外部熔断三参数（规格：并发 3 / 超时 300s / 预算 200K）
    "maxConcurrency": 3,
    "timeoutSeconds": 300,
    "tokenBudget": 200000,
    # 触发轮询间隔（秒）；调度器由平台调度线程 keep-alive（见 scheduler_tick）
    "triggerIntervalSeconds": 60,
    # 事件触发的最小间隔（同一个触发条件的冷却时间，分钟）——避免同一事实反复唤醒
    "cooldownMinutes": 60,
    # 定时触发（三个时点各自的开关）
    "schedule": {"pre_market_scan": True, "midday_review": True, "post_close": True},
    # 事件触发
    "events": {"breaking_news": True, "position_move": True,
               "risk_breach": True, "factor_reversal": True},
    # 流水线节点触发
    "pipeline": {"pre_rebalance_confirm": True, "strategy_param_change": True},
    # 触发条件阈值（每条都是**可解释的读数口径**，不是情绪打分）
    "thresholds": {
        # 资讯爆发：窗口内 ``sentiment_snapshots`` 的新观测条数
        "newsWindowMinutes": 60, "newsBurstCount": 5,
        # 持仓异动：单标的盈亏比例绝对值（%）
        "positionMovePct": 5.0,
        # 风控探测缓存的最大年龄（小时）——超过就当没有读数（不拿旧值下结论）
        "riskProbeMaxAgeHours": 6,
        # 因子信号反转：用哪个因子看横截面均值符号翻转
        "reversalFactor": "mom_20",
        # 因子快照的最大年龄（小时）
        "factorMaxAgeHours": 48,
    },
    # 提示词里的扫描面（pre_market_scan 的 {universe}）
    "universe": "A股（自选池）",
    # prompt 用的市场（基准指数按此选：SH.000300 / HK.800000 / US.SPY）
    "market": "SH",
    # 子进程工作目录（默认仓库根：profile 里的 skill 目录是仓库内路径）
    "cwd": "",
    # 子进程 DSH_HOME（默认继承环境；不设就用平台 home）
    "dshHome": "",
}

#: 三参数的环境变量覆盖（部署期不改文件也能配；见 ``docs/e2e-and-data-gaps.md``）
ENV_KEYS = {
    "enabled": "QUANT_HEADLESS_ENABLED",
    "profile": "QUANT_HEADLESS_PROFILE",
    "maxConcurrency": "QUANT_HEADLESS_CONCURRENCY",
    "timeoutSeconds": "QUANT_HEADLESS_TIMEOUT",
    "tokenBudget": "QUANT_HEADLESS_TOKEN_BUDGET",
    "requireWhitelist": "QUANT_HEADLESS_REQUIRE_WHITELIST",
    "dshHome": "QUANT_HEADLESS_DSH_HOME",
    "cwd": "QUANT_HEADLESS_CWD",
}

#: ``dsh`` 可执行文件查找顺序：QUANT_HEADLESS_DSH_BIN → DSH_BIN → PATH
DSH_BIN_ENVS = ("QUANT_HEADLESS_DSH_BIN", "DSH_BIN")

#: 白名单里**必须**被关停的 shipped ``tool-*`` 行（与
#: ``platform/install/quant-headless/tool-whitelist.json`` 的 ``disabledRows`` 一一对应，
#: 测试断言两者相等——名单漂移即测试红）。
REQUIRED_DISABLED_ROWS = (
    "tool-bash", "tool-pwsh", "tool-jobs",
    "tool-subagent", "tool-subagent-fork", "tool-subagent-control",
    "tool-subagent-list-agents", "tool-workflow", "tool-ralph", "tool-goal",
    "tool-web", "web-search-deepseek", "web-fetch-http", "web",
)

#: 白名单 profile 的仓库材料目录（唯一事实源；装到机器上的是它的拷贝）
PROFILE_MATERIAL_DIR = (Path(__file__).resolve().parents[1]
                        / "install" / "quant-headless")
WHITELIST_FILENAME = "tool-whitelist.json"
HOOKS_FILENAME = "hooks.json"
PATCH_FILENAME = "cordis.patch.yml"
DENY_ROW_ID = "quant-headless-tool-deny"

#: prompt 里的只读边界（三层防线的第 3 层；前两层是 profile 白名单与起进程前校验）
READONLY_BOUNDARY = (
    "\n\n【只读边界（平台硬约束，逐条不得违反）】\n"
    "本次唤醒是**只读研究**：不得下单、改单、撤单、切模式、执行/取消已冻结计划，"
    "不得批准规则、不得拨自动流水线开关。这些动作只由人在工作台 Web（默认 "
    "http://127.0.0.1:8397）确认后执行。上述写/交易工具**已从你的工具面移除**；"
    "若你发现任何可绕过工具面（shell / 委派 / 直连 HTTP）的路径，立即停止并如实报告，"
    "不要使用。\n"
    "【数据诚实（平台纪律）】取不到数据就写「无数据源（原因）」，绝不用 0、估算值或占位符"
    "替代；任何读数都要带 source 与 as_of。\n"
    "【产出】只产出文字/研报级结论与建议，不产生任何副作用。"
)

#: 触发条件的真实数据来源登记（**唯一事实来源**：报告、端点与文档都引这张表）
TRIGGER_SOURCES = {
    "slot:pre_market_scan": "平台交易日历（v3_market_calendar.load_holidays + is_trading_day）",
    "slot:midday_review": "平台交易日历（同上）",
    "slot:post_close": "平台交易日历（同上）",
    "breaking_news": ("平台库 store.sentiment_snapshots（每日 sentiment_snapshot 作业写入的"
                      "渠道原文）窗口内新观测条数"),
    "position_move": ("平台工具面 positions（富途持仓，经 MCP 只读工具）+ 行内 pl_ratio/"
                      "price/cost_price"),
    "risk_breach": ("<home>/v3-risk-probe.json（行业集中度探测缓存，由 "
                    "POST /api/v3/metrics/probe/refresh 写）+ v3_db.oms_sync 最近一次的 "
                    "drawdown_pct/nav"),
    "factor_reversal": ("平台库 store.factor_snapshots（每日 factors-snapshot 作业写入）"
                        "最近两日的横截面均值符号"),
    "pre_rebalance_confirm": "平台库 store.plans（status=frozen）+ store.orders（该计划未提交订单）",
    "strategy_param_change": "平台库 store.rules（status=candidate，待人审的规则候选）",
}


def _now_iso(moment=None):
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _number(value):
    """宽容取数：bool/None/非数字串 → None（**不是 0**，免得把「读不到」记成读数）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# token 估算（FR-GATEWAY-004 的预算口径）
# ---------------------------------------------------------------------------
def estimate_tokens(*texts):
    """按字符估算 token（口径见 :data:`TOKEN_ESTIMATE_NOTE`）。``None`` 段按空串算。

    返回 ``(tokens, detail)``；``detail`` 里带 ASCII/非 ASCII 字符数，便于复核估算口径。
    """
    ascii_chars = 0
    wide_chars = 0
    for text in texts:
        if not text:
            continue
        for char in str(text):
            if ord(char) < 128:
                ascii_chars += 1
            else:
                wide_chars += 1
    tokens = int(math.ceil(ascii_chars / CHARS_PER_TOKEN_ASCII)) + wide_chars
    return tokens, {"asciiChars": ascii_chars, "wideChars": wide_chars,
                    "charsPerTokenAscii": CHARS_PER_TOKEN_ASCII,
                    "wideCharsPerToken": 1, "note": TOKEN_ESTIMATE_NOTE}


def map_exit_code(code):
    """退出码 → 契约结果（FR-GATEWAY-003）。``None`` = 没能起进程（运行器失败）。"""
    if code is None:
        return "runner-error"
    if code in _EXIT_MAP:
        return _EXIT_MAP[code]
    if isinstance(code, int) and code < 0:
        # 被信号杀死（killpg 后 wait() 给的是 -signum）
        return "killed"
    return "runner-error"


# ---------------------------------------------------------------------------
# 进程捕获（stdout / stderr 分离 + 熔断）
# ---------------------------------------------------------------------------
class ProcessResult:
    """一次子进程的真实结果（唯一事实：两条管道分开、退出码原样）。"""

    __slots__ = ("stdout", "stderr", "exit_code", "killed", "kill_reason", "duration_ms",
                 "spawn_error", "signal", "tokens_estimate", "token_detail", "read_error")

    def __init__(self, stdout="", stderr="", exit_code=None, killed=False, kill_reason=None,
                 duration_ms=0.0, spawn_error=None, signal=None, tokens_estimate=0,
                 token_detail=None, read_error=None):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.killed = bool(killed)
        self.kill_reason = kill_reason
        self.duration_ms = float(duration_ms)
        self.spawn_error = spawn_error
        self.signal = signal
        self.tokens_estimate = int(tokens_estimate)
        self.token_detail = token_detail or {}
        self.read_error = read_error


def _kill_group(process):
    """整组 kill（``start_new_session=True`` 起的进程组）：headless 自己拉起的子进程一并收走。"""
    try:
        os.killpg(os.getpgid(process.pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


class DefaultSpawner:
    """真实子进程捕获：``Popen`` + 两条管道 + 轮询熔断。

    与规格示例的 ``subprocess.run(timeout=)`` **语义等价但更严格**：
      * ``capture_output=True`` 的等价物是两条独立 ``PIPE``（stdout 与 stderr 永不合流），
        用 ``os.read`` 块读（不是 readline）：无换行的输出也能被预算检查看见；
      * 超时不再抛 ``TimeoutExpired`` 后由调用方猜，而是在这里 kill、并如实带回
        ``killed/kill_reason``；
      * 另加 token 预算熔断：边读边估，超预算立刻 kill（这是平台侧的**外部**熔断，
        进程内没有任何费用刹车）。
    """

    #: 轮询间隔（秒）：熔断的检测粒度。0.05s 下单次调用的额外开销可忽略。
    POLL_SECONDS = 0.05

    def run(self, argv, *, timeout, token_budget, env, cwd):
        started = time.monotonic()
        try:
            # 二进制 + bufsize=0：reader 用 os.read 按**到达即读**取块，token 预算才能
            # 「边读边算」。用 text/readline 会按行阻塞——没有换行的那一大段输出在
            # 预算检查里根本看不见（预算熔断就形同虚设）。
            process = subprocess.Popen(
                list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, bufsize=0, env=env, cwd=(cwd or None),
                start_new_session=True)
        except OSError as error:
            return ProcessResult(exit_code=None, spawn_error=str(error),
                                 duration_ms=(time.monotonic() - started) * 1000)
        except ValueError as error:  # 空 argv 等
            return ProcessResult(exit_code=None, spawn_error=str(error),
                                 duration_ms=(time.monotonic() - started) * 1000)

        buffers = {"stdout": [], "stderr": []}
        counters = {"ascii": 0, "wide": 0}
        lock = threading.Lock()
        read_error = []

        def pump(stream, key):
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                fd = stream.fileno()
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    if not text:
                        continue
                    ascii_chars = sum(1 for char in text if ord(char) < 128)
                    with lock:
                        buffers[key].append(text)
                        counters["ascii"] += ascii_chars
                        counters["wide"] += len(text) - ascii_chars
                tail = decoder.decode(b"", True)
                if tail:
                    with lock:
                        buffers[key].append(tail)
                        counters["wide"] += len(tail)
            except Exception as error:  # noqa: BLE001 —— 读失败不该把进程结果吞掉
                read_error.append(f"{key}: {error}")
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        readers = [threading.Thread(target=pump, args=(process.stdout, "stdout"),
                                    name="v3-headless-stdout", daemon=True),
                   threading.Thread(target=pump, args=(process.stderr, "stderr"),
                                    name="v3-headless-stderr", daemon=True)]
        for reader in readers:
            reader.start()

        deadline = started + float(timeout)
        kill_reason = None
        while True:
            if process.poll() is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                kill_reason = "timeout"
                break
            if token_budget is not None:
                with lock:
                    ascii_chars, wide_chars = counters["ascii"], counters["wide"]
                current = int(math.ceil(ascii_chars / CHARS_PER_TOKEN_ASCII)) + wide_chars
                if current > int(token_budget):
                    kill_reason = "token-budget"
                    break
            time.sleep(self.POLL_SECONDS)

        if kill_reason:
            _kill_group(process)
        try:
            exit_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover —— killpg 之后不该发生
            _kill_group(process)
            exit_code = process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        duration_ms = (time.monotonic() - started) * 1000
        with lock:
            ascii_chars, wide_chars = counters["ascii"], counters["wide"]
            stdout = "".join(buffers["stdout"])
            stderr = "".join(buffers["stderr"])
        tokens = int(math.ceil(ascii_chars / CHARS_PER_TOKEN_ASCII)) + wide_chars
        return ProcessResult(
            stdout=stdout, stderr=stderr, exit_code=exit_code, killed=bool(kill_reason),
            kill_reason=kill_reason, duration_ms=duration_ms,
            signal=(-exit_code if isinstance(exit_code, int) and exit_code < 0 else None),
            tokens_estimate=tokens,
            token_detail={"asciiChars": ascii_chars, "wideChars": wide_chars,
                          "note": TOKEN_ESTIMATE_NOTE},
            read_error="; ".join(read_error) or None)


# ---------------------------------------------------------------------------
# 提示词构造（§5.2.3 / 附录 B）
# ---------------------------------------------------------------------------
#: 规格 §5.2.3 的三类模板，**逐字**照抄（改动即偏离规格；测试按同一份常量断言）
PROMPT_TEMPLATES = {
    "pre_market_scan": (
        "扫描 {universe} 板块，评估隔夜新闻情绪影响。\n"
        "当前持仓：{positions}\n"
        "风控阈值：{risk_limits}\n"
        "输出调仓建议，包含因子依据。"
    ),
    "breaking_news": (
        "评估以下新闻对当前持仓的影响：\n{news_text}\n"
        "当前持仓：{positions}\n"
        "输出风险等级和应对建议。"
    ),
    "risk_review": (
        "复盘当前组合的风险暴露。\n"
        "持仓详情：{positions}\n"
        "近期市场数据：{market_summary}\n"
        "输出风险归因和改进建议。"
    ),
}

#: 每类模板需要的上下文键（缺键 → ValueError，不静默补空串）
TEMPLATE_FIELDS = {
    "pre_market_scan": ("universe", "positions", "risk_limits"),
    "breaking_news": ("news_text", "positions"),
    "risk_review": ("positions", "market_summary"),
}

TASK_TYPES = tuple(PROMPT_TEMPLATES)

BENCHMARKS = {"SH": "SH.000300", "HK": "HK.800000", "US": "US.SPY"}

#: 定时时点的上下文窗口（同一模板、不同窗口；见 SLOTS）
SLOT_CONTEXT = {"pre_open": "开盘前", "intraday": "午间", "post_close": "收盘后"}


def build_prompt(task_type, context):
    """规格 §5.2.3 的 ``HeadlessRunner.build_prompt``：模板 + 上下文 → 提示词。"""
    if task_type not in PROMPT_TEMPLATES:
        raise ValueError(f"未知 task_type={task_type!r}（可用：{'/'.join(TASK_TYPES)}）")
    missing = [key for key in TEMPLATE_FIELDS[task_type] if context.get(key) is None]
    if missing:
        # 缺上下文就报错，**不用空串**凑一个看起来正常的提示词
        raise ValueError(f"{task_type} 缺上下文：{','.join(missing)}")
    return PROMPT_TEMPLATES[task_type].format(**{key: context[key]
                                                 for key in TEMPLATE_FIELDS[task_type]})


def build_task_prompt(task_type, context):
    """模板正文 + 只读边界脚注（平台自己的包装，不改模板本身）。"""
    return build_prompt(task_type, context) + READONLY_BOUNDARY


def render_positions(envelope):
    """``positions`` 信封 → 提示词里的人类可读块（含来源；读不到如实写原因）。"""
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        reason = ""
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(error, dict):
            reason = str(error.get("message") or error.get("code") or "")
        return f"无数据源（positions 取数失败：{reason or '未知原因'}）"
    value = envelope.get("value") if isinstance(envelope.get("value"), dict) else {}
    groups = [group for group in (value.get("groups") or []) if isinstance(group, dict)]
    if not groups:
        return "无数据源（positions 无账户分组）"
    lines = []
    for group in groups:
        rows = [row for row in (group.get("positions") or []) if isinstance(row, dict)]
        account = group.get("account") or group.get("acc_id") or "?"
        if not rows:
            lines.append(f"- 账户 {account}：无持仓")
            continue
        for row in rows:
            symbol = row.get("symbol") or row.get("code") or "?"
            lines.append(
                f"- {account} {symbol} {row.get('name') or ''} 数量={row.get('qty')} "
                f"成本={row.get('cost_price')} 现价={row.get('price')} "
                f"市值={row.get('market_value')} 盈亏={row.get('pl_val')} "
                f"盈亏比例={row.get('pl_ratio')}")
    source = value.get("source") or value.get("as_of") or "positions(工作台工具面)"
    return "\n".join(lines) + f"\n（来源：positions 工具面；{source}）"


def render_risk_limits(limits, home=None, extra=None):
    """风控阈值块：平台自己的 ``v3_ops.LIMITS``（与真实闸门同源，不另立一套数）。"""
    rows = [f"- 单标的仓位上限 {limits['singlePct']}% NAV",
            f"- 行业集中度上限 {limits['industryPct']}%（达到/超过即阻断）",
            f"- 回撤红线 {limits['drawdownPct']}%（达到/超过即阻断新单）"]
    note = "（来源：platform/server/v3_ops.py LIMITS——与平台真实闸门同源）"
    if extra:
        rows.append(f"- 本次触发阈值：{extra}")
    return "\n".join(rows) + "\n" + note


def render_market_summary(summary):
    """市场摘要块（``None`` → 如实写「无数据源」；绝不用 0 冒充读数）。"""
    if not summary:
        return "无数据源（行情工具面未返回可用读数）"
    return summary


def positions_rows(envelope):
    """``positions`` 信封 → 归一化行列表（持仓异动触发用；读不到就是空列表）。"""
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return []
    value = envelope.get("value") if isinstance(envelope.get("value"), dict) else {}
    rows = []
    for group in (value.get("groups") or []):
        if not isinstance(group, dict):
            continue
        for row in (group.get("positions") or []):
            if isinstance(row, dict):
                rows.append(dict(row, _account=(group.get("account") or group.get("acc_id"))))
    return rows


def position_move(row):
    """单行持仓的异动读数 → ``(pct, source)``；读不到返回 ``(None, 原因)``。

    口径（按优先级，逐条都是**行内真字段**，不猜价）：
      1. ``pl_ratio``（富途盈亏比例，已是百分数）；
      2. ``(price / cost_price - 1) * 100``。
    """
    ratio = _number(row.get("pl_ratio"))
    if ratio is not None:
        return ratio, "positions[].pl_ratio"
    price = _number(row.get("price") or row.get("nominal_price"))
    cost = _number(row.get("cost_price"))
    if price is not None and cost:
        return (price / cost - 1) * 100, "positions[].price/cost_price"
    return None, "行内无 pl_ratio/price/cost_price"


# ---------------------------------------------------------------------------
# 子进程唤醒前的白名单校验（fail-closed）
# ---------------------------------------------------------------------------
#: ``MCP_EXCLUDED_ENDPOINTS`` 里**只读**的那几个：它们不进 MCP 工具面是「HTTP-only」
#: 或「取数内部用」，不是写，因此**不**进 headless 的拒绝名单（免得把只读面也关掉）。
READ_ONLY_EXCLUDED_ENDPOINTS = frozenset({"warrant_screen", "info_rehab",
                                          "research-tasks-list"})


def probe_write_endpoints():
    """``platform/tools/e2e_probe.py`` 的写端点清单（平台自己的「只读探针不许碰」名单）。

    可导入时用它；导入不到返回空集（此时 :func:`deny_tool_names` 仍有
    ``v3_ops.WRITE_TOOLS`` + ``MCP_EXCLUDED_ENDPOINTS`` 两块来源，测试另有 parity 断言）。
    """
    try:
        import importlib
        probe = importlib.import_module("tools.e2e_probe")
        return frozenset(str(item) for item in getattr(probe, "WRITE_ENDPOINTS", ()))
    except Exception:  # noqa: BLE001 —— 探针模块是可选依赖
        return frozenset()


def deny_tool_names():
    """headless 会话**绝不允许**出现的工具名（MCP 名 + 端点名两种写法都收）。

    推导来源（都是平台自己的常量，不另抄一份名单，避免漂移）：
      * ``v3_ops.WRITE_TOOLS``——交易写类（trade_place/modify/cancel、plan_execute、switch_mode）；
      * ``platform/tools/e2e_probe.WRITE_ENDPOINTS``——平台自己的写/副作用端点清单
        （sim 交易、确认、自动流水线、规则批准、凭据读写、推送订阅、数据采集触发……）；
      * ``mcp_tools.MCP_EXCLUDED_ENDPOINTS``——有意不进 MCP 工具面的写/人工端点
        （``confirm-decide`` / ``auto_pipeline`` / ``rules-decide`` / ``modify_user_security``
        等；只读的那三个见 :data:`READ_ONLY_EXCLUDED_ENDPOINTS`，不关）；
      * 拒绝名单是**归一化集合**：``trade_place`` 与 ``trade-place`` 都进，
        因为钩子匹配的是工具名、而端点是连字符写法，改名/两套写法都不会漏。
    """
    names = set(v3_ops.WRITE_TOOLS)
    names |= probe_write_endpoints()
    for endpoint in mcp_tools.MCP_EXCLUDED_ENDPOINTS:
        if endpoint not in READ_ONLY_EXCLUDED_ENDPOINTS:
            names.add(str(endpoint))
    normalized = set()
    for name in names:
        normalized.add(str(name))
        normalized.add(str(name).replace("-", "_"))
        normalized.add(str(name).replace("_", "-"))
    return frozenset(normalized)


def deny_matchers(names=None, server_name="quantwb"):
    """写/交易工具 → ``PreToolUse`` 钩子的字面 matcher 列表。

    Claude Code 的字面 matcher 语义（``dsh-hook-protocol.matchesMatcher``：pattern 只含
    字母数字下划线竖线时按 ``|`` 拆成精确名）——所以这里给**完整工具名**，
    既收 MCP 前缀名（``mcp__quantwb__trade_place``）也收裸名（本地工具）。
    """
    names = set(names if names is not None else deny_tool_names())
    out = set()
    for name in names:
        out.add(str(name))
        out.add(f"mcp__{server_name}__{name}")
    return sorted(out)


def parse_patch_rows(text):
    """``cordis.patch.yml`` 的极简行扫描（不引 YAML 依赖）。

    只认**我们自己写的形状**（顶层列表项 ``- id: X`` / ``- insert:`` 后跟 ``- id:``）；
    形状不符 → 该行视为不存在 → 校验失败（fail-closed，宁可不起进程）。
    """
    rows = []
    current = None
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- id:") or line.startswith("- insert:"):
            current = {"id": line.split(":", 1)[1].strip(), "disabled": False,
                       "name": None, "configPath": None, "insert": line.startswith("- insert:")}
            rows.append(current)
            continue
        if current is None:
            continue
        # insert 列表里的条目形如 "- id: xxx"（缩进后仍是列表项）——上面已按 id 收下，
        # 这里只认普通键值。
        if line.startswith("- id:"):
            continue
        if ":" in line and not line.startswith("-"):
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip().strip("'\"")
            if key == "disabled":
                current["disabled"] = value.lower() == "true"
            elif key == "name":
                current["name"] = value
            elif key == "configPath":
                current["configPath"] = value
    return rows


def load_whitelist_material(material_dir=None):
    """读仓库里的白名单材料 → ``(payload, error)``；读不到给原因（不猜）。"""
    directory = Path(material_dir or PROFILE_MATERIAL_DIR)
    path = directory / WHITELIST_FILENAME
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except OSError as error:
        return None, f"白名单材料不可读（{path}）：{error}"
    except ValueError as error:
        return None, f"白名单材料不是合法 JSON（{path}）：{error}"


def verify_whitelist(profile_dir, *, material_dir=None, server_name="quantwb"):
    """起进程**之前**校验装到机器上的白名单 profile → ``(ok, detail)``。

    四项判据（任一不成立即 fail-closed）：
      1. ``cordis.patch.yml`` 存在，且每个 :data:`REQUIRED_DISABLED_ROWS`（或仓库材料的
         ``disabledRows``）都被 ``disabled: true`` 关停；
      2. patch 里插入了 ``dsh-hooks-claude-code`` 行（``id: quant-headless-tool-deny``）
         并指向 ``hooks.json``；
      3. ``hooks.json`` 的 ``PreToolUse`` matcher 覆盖**全部**写/交易工具名
         （:func:`deny_tool_names`，逐个名字都能被某个 matcher 精确命中）；
      4. patch 里的行形状可解析（形状不符 → 第 1/2 条自然失败）。
    """
    directory = Path(profile_dir)
    material, error = load_whitelist_material(material_dir)
    required_rows = tuple(material.get("disabledRows") or ()) if material \
        else REQUIRED_DISABLED_ROWS
    if not required_rows:
        required_rows = REQUIRED_DISABLED_ROWS
    detail = {"profileDir": str(directory), "material": str(material_dir or PROFILE_MATERIAL_DIR),
              "materialError": error, "requiredRows": list(required_rows),
              "deniedNames": sorted(deny_tool_names())}
    patch_path = directory / PATCH_FILENAME
    try:
        rows = parse_patch_rows(patch_path.read_text(encoding="utf-8"))
    except OSError as read_error:
        detail["error"] = f"profile patch 不可读（{patch_path}）：{read_error}"
        return False, detail
    by_id = {row["id"]: row for row in rows}
    missing_rows = [name for name in required_rows
                    if not (by_id.get(name) or {}).get("disabled")]
    detail["missingDisabledRows"] = missing_rows
    deny_row = by_id.get(DENY_ROW_ID)
    if deny_row is None or deny_row.get("name") != "@deepseek-ai/dsh-hooks-claude-code":
        detail["error"] = (f"patch 里没有 {DENY_ROW_ID}（dsh-hooks-claude-code 行）："
                           "没有它就没有逐名拒绝工具的能力")
        return False, detail
    hooks_path = deny_row.get("configPath") or f"${{PROFILE}}/{HOOKS_FILENAME}"
    candidate = Path(hooks_path)
    if not candidate.is_absolute():
        candidate = directory / candidate.name
    if not candidate.exists():
        # patch 里写的是仓库绝对路径（README 方式 A 要求按机器改）：装到别处时按
        # profile 目录下的同名文件回退，两者都没有才失败。
        candidate = directory / HOOKS_FILENAME
    try:
        hooks = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as hooks_error:
        detail["error"] = f"hooks.json 不可读/非法（{candidate}）：{hooks_error}"
        return False, detail
    detail["hooksPath"] = str(candidate)
    matchers = []
    for group in ((hooks.get("hooks") or {}).get("PreToolUse") or []):
        if isinstance(group, dict) and isinstance(group.get("matcher"), str):
            matchers.append(group["matcher"])
    denied = deny_matchers(server_name=server_name)
    uncovered = [name for name in denied if not _matcher_covers(matchers, name)]
    detail["uncoveredTools"] = uncovered
    detail["matchers"] = matchers
    if missing_rows or uncovered or not matchers:
        detail["error"] = ("白名单不完整："
                           + (f"未关停行 {missing_rows}；" if missing_rows else "")
                           + (f"未覆盖工具 {uncovered}；" if uncovered else "")
                           + ("无 PreToolUse matcher" if not matchers else "")).rstrip("；")
        return False, detail
    detail["ok"] = True
    return True, detail


def _matcher_covers(matchers, name):
    """某个字面 matcher 列表是否精确命中 ``name``（与 dsh-hook-protocol 同语义）。"""
    for matcher in matchers:
        text = str(matcher or "")
        if not text:
            continue
        if text in ("*", ".*"):  # 匹配全部
            return True
        if set(text) <= set("abcdefghijklmnopqrstuvwxyz"
                            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_|"):
            if name in text.split("|"):
                return True
        else:  # 正则
            import re
            try:
                if re.search(text, name):
                    return True
            except re.error:
                continue
    return False


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _env_bool(value, default):
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _env_number(value, default, cast):
    try:
        return cast(str(value).strip())
    except (TypeError, ValueError):
        return default


def load_config(home, env=None, *, override=None):
    """读 ``<home>/v3-headless.json`` + 环境变量覆盖 → ``(config, sources)``。

    ``sources`` 逐键记「这个值从哪来」（file/env/default），供 ``/schedule`` 如实展示；
    未知键忽略（不因为别人多写一个键就整体失效），值类型不对时保留默认值并记原因。
    """
    env = os.environ if env is None else env
    config = {key: (dict(value) if isinstance(value, dict) else value)
              for key, value in DEFAULT_CONFIG.items()}
    sources = {key: "default" for key in config}
    path = Path(home) / CONFIG_FILENAME
    notes = []
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if key not in config:
                        notes.append(f"忽略未知配置键 {key!r}")
                        continue
                    if isinstance(config[key], dict) and isinstance(value, dict):
                        merged = dict(config[key])
                        merged.update(value)
                        config[key] = merged
                    else:
                        config[key] = value
                    sources[key] = f"file:{path.name}"
            else:
                notes.append(f"{path.name} 顶层不是对象，按默认值跑")
        else:
            notes.append(f"未找到 {path.name}，按默认值跑")
    except (OSError, ValueError) as error:
        notes.append(f"{path.name} 读取失败（{error}），按默认值跑")
    for key, name in ENV_KEYS.items():
        if name not in env:
            continue
        raw = env.get(name)
        if isinstance(config.get(key), bool):
            config[key] = _env_bool(raw, config[key])
        elif isinstance(config.get(key), int) and not isinstance(config.get(key), bool):
            config[key] = int(_env_number(raw, config[key], int))
        elif isinstance(config.get(key), float):
            config[key] = float(_env_number(raw, config[key], float))
        else:
            config[key] = str(raw).strip()
        sources[key] = f"env:{name}"
    if override:
        for key, value in override.items():
            if key in config and value is not None:
                config[key] = value
                sources[key] = "override"
    if not config.get("cwd"):
        config["cwd"] = str(Path(__file__).resolve().parents[2])
    return config, {"configPath": str(path), "sources": sources, "notes": notes}


def _dsh_bin(env=None):
    env = os.environ if env is None else env
    for name in DSH_BIN_ENVS:
        candidate = str(env.get(name) or "").strip()
        if candidate:
            return candidate, f"env:{name}"
    found = shutil.which("dsh")
    return found, ("PATH" if found else None)


# ---------------------------------------------------------------------------
# Runner（FR-GATEWAY-003 + 外部熔断）
# ---------------------------------------------------------------------------
def _ticket_record(ticket):
    """等待中的票据在超时被问到时的应答（诚实：还不知道结果）。"""
    return {"ok": False, "pending": True, "ticket": ticket.id,
            "queued_ms": round(ticket.queued_ms, 3)}


class Ticket:
    """一次排队/执行的票据（``submit`` 返回；调度器不等结果，测试与探针可以等）。"""

    _counter = 0

    def __init__(self, ticket_id, trigger, task_type, event_key=None):
        self.id = ticket_id
        self.trigger = trigger
        self.task_type = task_type
        self.event_key = event_key
        self.submitted_at = time.monotonic()
        self.started_at = None
        self.finished_at = None
        self.record = None
        self.error = None
        self._done = threading.Event()

    @property
    def queued_ms(self):
        end = self.started_at if self.started_at is not None else time.monotonic()
        return max(0.0, (end - self.submitted_at) * 1000)

    @property
    def done(self):
        return self._done.is_set()

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.record if self.done else _ticket_record(self)

    def summary(self):
        return {"ticket": self.id, "trigger": self.trigger, "taskType": self.task_type,
                "eventKey": self.event_key, "done": self.done,
                "queuedMs": round(self.queued_ms, 3),
                "outcome": (self.record or {}).get("outcome")}


class HeadlessRunner:
    """``dsh --profile <profile> "<prompt>"`` 的发起者（含并发/超时/预算熔断与落库）。

    注入点（测试与探针用，生产不用）：
      * ``spawner``——替身进程（离线单测覆盖退出码映射/分离捕获/超时/预算）；
      * ``v3_run``——平台工具面回调（持仓/行情；缺省无工具面 → 提示词如实写「无数据源」）；
      * ``clock``——可注入时钟（触发时间判定用）。
    """

    def __init__(self, home, *, v3_run=None, config=None, spawner=None, clock=None,
                 material_dir=None, env=None, data_dir=None):
        self.home = str(home)
        self.data_dir = data_dir
        self._env = dict(os.environ if env is None else env)
        self.config, self.config_sources = (load_config(self.home, self._env)
                                            if config is None else (config, {"sources": {}}))
        # 注入的 config 走的是「测试/探针」路径，load_config 的派生默认值要在这里补齐，
        # 否则 cwd="" 会让 Popen 以空 cwd 起进程（Errno 2，看起来像「找不到 dsh」）。
        if not self.config.get("cwd"):
            self.config["cwd"] = str(Path(__file__).resolve().parents[2])
        self.v3_run = v3_run
        self.spawner = spawner or DefaultSpawner()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.material_dir = material_dir or PROFILE_MATERIAL_DIR
        self._queue = queue.Queue()
        self._workers = []
        self._worker_lock = threading.Lock()
        self._tickets = 0
        self._ticket_lock = threading.Lock()
        self._recent = []
        self._recent_lock = threading.Lock()
        self._running = 0
        self._running_lock = threading.Lock()
        self._processes = set()
        self._closed = False
        self._profile_dir_override = None
        self._whitelist_cache = None

    # ---- 生效参数（熔断三参数的「真实生效值」，端点直接报这个） ----
    def params(self):
        dsh_bin, dsh_source = _dsh_bin(self._env)
        dsh_home = self.config.get("dshHome") or self._env.get("DSH_HOME") or self.home
        return {
            "enabled": bool(self.config["enabled"]),
            "profile": self.config["profile"],
            "maxConcurrency": int(self.config["maxConcurrency"]),
            "timeoutSeconds": float(self.config["timeoutSeconds"]),
            "tokenBudget": int(self.config["tokenBudget"]),
            "requireWhitelist": bool(self.config["requireWhitelist"]),
            "triggerIntervalSeconds": float(self.config["triggerIntervalSeconds"]),
            "cooldownMinutes": float(self.config["cooldownMinutes"]),
            "cwd": self.config["cwd"],
            "dshHome": dsh_home,
            "dshBin": dsh_bin,
            "dshBinSource": dsh_source,
            "schedule": dict(self.config["schedule"]),
            "events": dict(self.config["events"]),
            "pipeline": dict(self.config["pipeline"]),
            "thresholds": dict(self.config["thresholds"]),
        }

    def profile_dir(self):
        if self._profile_dir_override:
            return Path(self._profile_dir_override)
        dsh_home = self.config.get("dshHome") or self._env.get("DSH_HOME") or self.home
        return Path(dsh_home) / "profiles" / str(self.config["profile"])

    # ---- 工具面（只读） ----
    def call_tool(self, name, payload=None):
        """调平台只读工具面；没有工具面/抛错 → 没有读数的信封（不猜）。"""
        if self.v3_run is None:
            return {"ok": False, "error": {
                "code": "headless/no-tool-surface",
                "message": "本 Runner 没有注入平台工具面（v3_run），提示词将如实写「无数据源」"}}
        try:
            result = self.v3_run(name, payload or {})
        except Exception as error:  # noqa: BLE001 —— 工具面异常不该炸调度线程
            return {"ok": False, "error": {"code": "headless/tool-call-failed",
                                           "message": f"{name}: {error}"}}
        if not isinstance(result, dict):
            return {"ok": False, "error": {"code": "headless/tool-call-failed",
                                           "message": f"{name} 返回非对象：{type(result).__name__}"}}
        return result

    # ---- 起进程前的门（fail-closed） ----
    def preflight(self):
        """返回 ``None`` = 可以起进程；否则返回阻止记录（含原因与证据）。"""
        if not self.config["enabled"]:
            return {"outcome": "disabled", "code": "headless/disabled",
                    "message": "headless 通道总开关关闭（v3-headless.json 的 enabled 或 "
                               "QUANT_HEADLESS_ENABLED）"}
        dsh_bin, source = _dsh_bin(self._env)
        if not dsh_bin:
            return {"outcome": "dsh-not-found", "code": "headless/dsh-not-found",
                    "message": "找不到 dsh 可执行文件（把 dsh 放进 PATH，或设 "
                               "QUANT_HEADLESS_DSH_BIN / DSH_BIN）"}
        profile = str(self.config["profile"])
        directory = self.profile_dir()
        if not (directory / "package.json").exists():
            return {"outcome": "profile-missing", "code": "headless/profile-missing",
                    "message": f"profile 未安装：{directory}（按 platform/install/"
                               f"quant-headless/README.md 拷进 $DSH_HOME/profiles/），"
                               "不自动回退到没有白名单的 profile"}
        if self.config["requireWhitelist"]:
            if self._whitelist_cache is None:
                self._whitelist_cache = verify_whitelist(directory,
                                                         material_dir=self.material_dir)
            ok, detail = self._whitelist_cache
            if not ok:
                return {"outcome": "whitelist-unverified",
                        "code": "headless/whitelist-unverified",
                        "message": "工具白名单校验不通过，拒绝起进程（fail-closed）："
                                   + str(detail.get("error") or ""), "whitelist": detail}
        return None

    # ---- 执行 ----
    def _argv(self, prompt):
        return [_dsh_bin(self._env)[0] or "dsh", "--profile", str(self.config["profile"]),
                prompt]

    def _record_base(self, *, task_type, trigger, event_key=None, prompt=None):
        return {
            "task_type": task_type,
            "trigger": trigger,
            "event_key": event_key,
            "source": TRIGGER_SOURCES.get(trigger, "手动调用（无触发条件）"),
            "profile": self.config["profile"],
            "contract_note": CONTRACT_NOTE,
            "token_estimate_note": TOKEN_ESTIMATE_NOTE,
            "prompt": prompt,
        }

    def execute(self, prompt, *, task_type="manual", trigger="manual", event_key=None,
                context=None, sources=None):
        """跑一次 headless 调用并落库（同步）。返回**记录**（与落库同一条）。"""
        started_moment = self.clock()
        started = time.monotonic()
        started_at = _now_iso(started_moment)
        record = self._record_base(task_type=task_type, trigger=trigger,
                                   event_key=event_key, prompt=prompt)
        if context is not None:
            record["context_keys"] = sorted(context)
        if sources:
            record["sources"] = sources
        record.update({"started_at": started_at, "started_epoch": time.time()})
        gate = self.preflight()
        if gate is not None:
            record.update({
                "success": 0, "exit_code": None, "duration_ms": 0.0,
                "tokens_estimate": 0, "outcome": gate["outcome"],
                "killed": 0, "error": {"code": gate.get("code"), "message": gate.get("message")},
                "stdout": "", "stderr": "",
            })
            if gate.get("whitelist"):
                record["whitelist"] = gate["whitelist"]
            record["finished_at"] = _now_iso()
            self._finish(record)
            return record

        argv = self._argv(prompt)
        params = self.params()
        record["argv_head"] = argv[:3] + ["<prompt>"]
        record["dsh_bin_source"] = params["dshBinSource"]
        record["timeout_s"] = params["timeoutSeconds"]
        record["token_budget"] = params["tokenBudget"]
        record["max_concurrency"] = params["maxConcurrency"]
        record["cwd"] = params["cwd"]
        record["dsh_home"] = params["dshHome"]
        record["tool_deny_count"] = len(deny_tool_names())
        child_env = dict(self._env)
        child_env["DSH_HOME"] = params["dshHome"]
        with self._running_lock:
            self._running += 1
        try:
            result = self.spawner.run(argv, timeout=params["timeoutSeconds"],
                                      token_budget=params["tokenBudget"],
                                      env=child_env, cwd=params["cwd"])
        finally:
            with self._running_lock:
                self._running -= 1
        tokens, token_detail = estimate_tokens(prompt, result.stdout, result.stderr)
        outcome = result.kill_reason or map_exit_code(result.exit_code)
        if result.spawn_error:
            outcome = "runner-error"
        record.update({
            "success": 1 if (outcome == "completed" and result.exit_code == 0) else 0,
            "exit_code": result.exit_code,
            "duration_ms": round(result.duration_ms, 3),
            "tokens_estimate": max(tokens, result.tokens_estimate),
            "token_estimate_detail": result.token_detail or token_detail,
            "outcome": outcome,
            "killed": 1 if result.killed else 0,
            "kill_reason": result.kill_reason,
            "signal": result.signal,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_chars": len(result.stdout or ""),
            "stderr_chars": len(result.stderr or ""),
            "streams_separated": 1,
            "finished_at": _now_iso(),
        })
        if result.spawn_error:
            record["error"] = {"code": "headless/spawn-failed",
                               "message": result.spawn_error}
        if result.read_error:
            record["read_error"] = result.read_error
        if outcome in ("timeout", "token-budget"):
            record["error"] = {
                "code": f"headless/{outcome}",
                "message": (f"{'超时' if outcome == 'timeout' else 'token 预算超限'}，已 kill"
                            f"（{'timeout_s=' + str(params['timeoutSeconds']) if outcome == 'timeout' else 'token_budget=' + str(params['tokenBudget']) + '（估算）'}）")}
        record["duration_wall_ms"] = round((time.monotonic() - started) * 1000, 3)
        self._finish(record)
        return record

    # ---- 并发上限 + 排队（FR-GATEWAY-004） ----
    def _ensure_workers(self):
        with self._worker_lock:
            if self._closed:
                return
            while len(self._workers) < max(1, int(self.config["maxConcurrency"])):
                worker = threading.Thread(target=self._worker,
                                          name=f"v3-headless-worker-{len(self._workers)}",
                                          daemon=True)
                worker.start()
                self._workers.append(worker)

    def _worker(self):
        while True:
            job = self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            ticket, prompt, kwargs = job
            ticket.started_at = time.monotonic()
            try:
                ticket.record = self.execute(prompt, **kwargs)
            except Exception as error:  # noqa: BLE001 —— 一个 ticket 失败不杀 worker
                ticket.error = str(error)
            finally:
                ticket.finished_at = time.monotonic()
                ticket._done.set()
                self._queue.task_done()

    def submit(self, prompt, *, task_type="manual", trigger="manual", event_key=None,
               context=None, sources=None):
        """投递一次调用（**不阻塞**，超出并发上限就排队）。返回 :class:`Ticket`。"""
        with self._ticket_lock:
            self._tickets += 1
            ticket = Ticket(self._tickets, trigger, task_type, event_key)
        self._ensure_workers()
        self._queue.put((ticket, prompt, {"task_type": task_type, "trigger": trigger,
                                          "event_key": event_key, "context": context,
                                          "sources": sources}))
        return ticket

    def drain(self, timeout=None):
        """等队列排空（探针/测试用；生产调度线程不调用）。``timeout`` 仅为接口自解释保留。"""
        self._queue.join()

    def stop(self, timeout=5.0):
        """停 worker 并收掉在跑的子进程（幂等）。"""
        with self._worker_lock:
            self._closed = True
            workers = list(self._workers)
            self._workers = []
        for _ in workers:
            self._queue.put(None)
        for worker in workers:
            worker.join(timeout)
        for process in list(self._processes):
            _kill_group(process)

    # ---- 记录 ----
    def _finish(self, record):
        self._log(record)
        self._remember(record)

    def _remember(self, record):
        with self._recent_lock:
            self._recent.append(record)
            del self._recent[:-50]

    def recent(self, limit=10):
        with self._recent_lock:
            return list(self._recent[-limit:])[::-1]

    def _log(self, record):
        """写 ``headless_log``（FR-MON-003）。写不进去也不抛：错误进 ``log_error``。"""
        try:
            v3_db.append_event(self.home, LOG_TABLE, record)
            record.pop("log_error", None)
        except Exception as error:  # noqa: BLE001 —— 台账写失败不能吞掉调用事实
            record["log_error"] = str(error)

    # ---- 状态 ----
    def state(self):
        return {
            "params": self.params(),
            "configSources": self.config_sources,
            "queueDepth": self._queue.qsize(),
            "running": self._running,
            "recent": self.recent(5),
            "whitelist": (self._whitelist_cache[1] if self._whitelist_cache else None),
            "whitelistOk": (self._whitelist_cache[0] if self._whitelist_cache else None),
            "triggerSources": dict(TRIGGER_SOURCES),
        }


# ---------------------------------------------------------------------------
# 触发条件（可插拔注册；每条都有真实数据来源）
# ---------------------------------------------------------------------------
def _store_connect(home):
    """平台库连接（trading_core.store）；不可用返回 ``None``（→ no-data，不猜）。"""
    try:
        from trading_core import store  # noqa: PLC0415
    except ImportError:
        import sys  # noqa: PLC0415
        core = Path(__file__).resolve().parents[2] / "plugins" / "core" / "python"
        if str(core) not in sys.path:
            sys.path.insert(0, str(core))
        try:
            from trading_core import store  # noqa: PLC0415
        except ImportError as error:
            return None, None, f"trading_core 不可导入：{error}"
    try:
        return store, store.connect(store.db_path(str(home))), None
    except Exception as error:  # noqa: BLE001
        return store, None, f"平台库不可打开：{error}"


def _no_data(source, reason, **extra):
    payload = {"status": "no-data", "source": source, "reason": reason}
    payload.update(extra)
    return payload


class TriggerResult:
    """触发条件一次判定的结果（``fire`` 才起进程；``no-data``/``idle`` 只留痕）。"""

    __slots__ = ("status", "event_key", "source", "as_of", "detail", "context")

    def __init__(self, status, *, event_key=None, source=None, as_of=None, detail=None,
                 context=None):
        self.status = status
        self.event_key = event_key
        self.source = source
        self.as_of = as_of
        self.detail = detail or {}
        self.context = context or {}

    def to_dict(self):
        return {"status": self.status, "eventKey": self.event_key, "source": self.source,
                "asOf": self.as_of, "detail": self.detail}


class Trigger:
    """触发条件（可插拔：``check(runner, config, moment)`` → :class:`TriggerResult`）。"""

    def __init__(self, key, kind, task_type, label, check, *, note=None):
        self.key = key
        self.kind = kind            # schedule | event | pipeline
        self.task_type = task_type  # 规格 §5.2.3 的三类模板之一
        self.label = label
        self.check = check
        self.note = note or TRIGGER_SOURCES.get(key, "")

    def to_dict(self):
        return {"key": self.key, "kind": self.kind, "taskType": self.task_type,
                "label": self.label, "source": self.note,
                "switch": self.key}


# ---- 定时 ----
def slot_datetime(slot, moment, tzname):
    """时点 → 当天（市场本地时区）的 ``datetime``。"""
    hour, minute = str(slot["at"]).split(":")
    local = moment.astimezone(ZoneInfo(tzname))
    return datetime.combine(local.date(), _time(int(hour), int(minute)),
                            tzinfo=ZoneInfo(tzname))


def market_today(market, moment):
    """市场本地日期（交易日判定按**市场本地日**，不是 UTC 日）。"""
    tzname = v3_market_calendar.MARKET_TIMEZONES.get(market, "Asia/Shanghai")
    return moment.astimezone(ZoneInfo(tzname)).date(), tzname


def is_trading_day(home, market, day, env=None):
    """交易日判定（既有 ``v3_market_calendar``）→ ``(bool, holidays_loaded, note)``。"""
    try:
        holidays, loaded, note = v3_market_calendar.load_holidays(home=home, env=env)
    except Exception as error:  # noqa: BLE001 —— 日历取不到 → 不当交易日（不空跑 LLM）
        return False, False, f"节假日表读取失败（{error}）；非交易日口径保守"
    return v3_market_calendar.is_trading_day(market, day, holidays), bool(loaded), note


def check_slot(slot):
    """生成定时触发的 ``check``（时点是否已到 + 当天是否交易日）。"""
    def check(runner, config, moment):
        market = slot["market"]
        day, tzname = market_today(market, moment)
        trading, loaded, note = is_trading_day(runner.home, market, day, env=runner._env)
        detail = {"market": market, "localDay": day.isoformat(), "at": slot["at"],
                  "isTradingDay": trading, "holidaysLoaded": loaded, "holidaysNote": note}
        if not trading:
            return TriggerResult("idle", source=TRIGGER_SOURCES[f"slot:{slot['key']}"],
                                 as_of=day.isoformat(), detail={**detail, "reason": "非交易日"})
        due = slot_datetime(slot, moment, tzname)
        if moment.astimezone(ZoneInfo(tzname)) < due:
            return TriggerResult("idle", source=TRIGGER_SOURCES[f"slot:{slot['key']}"],
                                 as_of=day.isoformat(),
                                 detail={**detail, "reason": "未到触发时点",
                                         "due": due.isoformat()})
        event_key = f"{slot['key']}:{day.isoformat()}"
        return TriggerResult("fire", event_key=event_key,
                             source=TRIGGER_SOURCES[f"slot:{slot['key']}"],
                             as_of=day.isoformat(),
                             detail={**detail, "window": slot["window"],
                                     "taskType": slot["task_type"]},
                             context={"window": slot["window"]})
    return check


# ---- 事件 ----
def check_breaking_news(runner, config, moment):
    """突发新闻：窗口内 ``sentiment_snapshots`` 的新观测条数（**采集量口径，不打分**）。"""
    source = TRIGGER_SOURCES["breaking_news"]
    store, conn, error = _store_connect(runner.home)
    if conn is None:
        return TriggerResult("no-data", source=source, detail={"reason": error})
    try:
        minutes = float(config["thresholds"]["newsWindowMinutes"])
        least = int(config["thresholds"]["newsBurstCount"])
        since = (moment - timedelta(minutes=minutes)).isoformat()
        rows = conn.execute(
            "SELECT symbol, source, fetched_at FROM sentiment_snapshots"
            " WHERE fetched_at >= ? ORDER BY fetched_at DESC LIMIT 500", (since,)).fetchall()
    except Exception as db_error:  # noqa: BLE001
        return TriggerResult("no-data", source=source, detail={"reason": f"查询失败：{db_error}"})
    finally:
        conn.close()
    detail = {"windowMinutes": minutes, "threshold": least, "observations": len(rows),
              "caliber": "窗口内资讯观测条数（渠道原文条数，不做情绪打分）"}
    if not rows:
        return TriggerResult("no-data", source=source, as_of=since,
                             detail={**detail, "reason": "窗口内无资讯观测（源未采集/未运行）"})
    if len(rows) < least:
        return TriggerResult("idle", source=source, as_of=str(rows[0]["fetched_at"]),
                             detail={**detail, "reason": f"观测 {len(rows)} 条 < 阈值 {least}"})
    symbols = sorted({str(row["symbol"]) for row in rows})[:20]
    channels = sorted({str(row["source"]) for row in rows})
    return TriggerResult("fire", event_key=f"news:{moment.strftime('%Y-%m-%dT%H')}",
                         source=source, as_of=str(rows[0]["fetched_at"]),
                         detail={**detail, "symbols": symbols, "channels": channels},
                         context={"symbols": symbols, "channels": channels,
                                  "observations": len(rows)})


def check_position_move(runner, config, moment):
    """持仓异动：工具面 positions 的行内盈亏比例/现价成本比（读不到 → no-data）。"""
    source = TRIGGER_SOURCES["position_move"]
    envelope = runner.call_tool("positions", {})
    rows = positions_rows(envelope)
    if not rows:
        if isinstance(envelope, dict) and envelope.get("ok"):
            reason = "positions 无持仓行（空仓或该账户未返回持仓）"
        else:
            error = envelope.get("error") if isinstance(envelope, dict) else None
            reason = ("positions 取数失败：" + str((error or {}).get("message")
                                                  or (error or {}).get("code") or "未知原因"))
        return TriggerResult("no-data", source=source, detail={"reason": reason})
    threshold = float(config["thresholds"]["positionMovePct"])
    worst = None
    for row in rows:
        pct, caliber = position_move(row)
        if pct is None:
            continue
        item = {"symbol": row.get("symbol") or row.get("code"),
                "account": row.get("_account"), "pct": round(pct, 4), "source": caliber}
        if worst is None or abs(item["pct"]) > abs(worst["pct"]):
            worst = item
    if worst is None:
        return TriggerResult("no-data", source=source,
                             detail={"reason": "所有持仓行都缺 pl_ratio/price/cost_price",
                                     "rows": len(rows)})
    if abs(worst["pct"]) < threshold:
        return TriggerResult("idle", source=source, detail={
            "thresholdPct": threshold, "worst": worst, "rows": len(rows),
            "reason": f"最大异动 {worst['pct']}% < 阈值 {threshold}%"})
    return TriggerResult("fire",
                         event_key=f"position:{worst['symbol']}:{moment.strftime('%Y-%m-%d')}",
                         source=source, detail={"thresholdPct": threshold, "worst": worst,
                                                "rows": len(rows)},
                         context={"worst": worst, "thresholdPct": threshold})


def check_risk_breach(runner, config, moment):
    """风控阈值突破：行业探测缓存（age 守卫）+ 最近一次对账的 drawdown/nav。"""
    source = TRIGGER_SOURCES["risk_breach"]
    max_age = float(config["thresholds"]["riskProbeMaxAgeHours"]) * 3600
    readings = {}
    probe_path = Path(runner.home) / "v3-risk-probe.json"
    probe_reason = None
    try:
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        generated = _number(probe.get("generated_at"))
        age = (moment.timestamp() - generated) if generated is not None else None
        if age is None or age > max_age:
            probe_reason = (f"探测缓存过期/无时间戳（age={None if age is None else round(age)}s "
                            f"> {max_age}s），不拿旧值下结论")
        else:
            readings["industry"] = {
                "pct": _number(probe.get("top_weight_pct")), "top": probe.get("top_industry"),
                "market": probe.get("market") or probe.get("markets"),
                "limit_pct": _number(probe.get("limit_pct")) or v3_ops.LIMITS["industryPct"],
                "breach": bool(probe.get("breach")), "source": probe.get("source"),
                "as_of": probe.get("generated_at_iso"), "ageSeconds": round(age)}
    except OSError:
        probe_reason = f"探测缓存不存在（{probe_path}）：没有行业读数"
    except ValueError as error:
        probe_reason = f"探测缓存不是合法 JSON（{error}）"
    try:
        syncs = v3_db.list_events(runner.home, "oms_sync", limit=1)
    except Exception as error:  # noqa: BLE001
        syncs = []
        probe_reason = (probe_reason or "") + f"；oms_sync 读取失败：{error}"
    if syncs:
        latest = syncs[0]
        readings["drawdown"] = {"pct": _number(latest.get("drawdown_pct")),
                                "nav": _number(latest.get("nav")),
                                "as_of": latest.get("at"),
                                "limit_pct": v3_ops.LIMITS["drawdownPct"],
                                "source": "v3_db.oms_sync（最近一次对账）"}
    breaches = []
    industry = readings.get("industry") or {}
    if industry.get("breach") or (_number(industry.get("pct")) is not None
                                  and industry["pct"] >= industry["limit_pct"]):
        breaches.append({"rule": "industry", "pct": industry.get("pct"),
                         "limit_pct": industry.get("limit_pct"), "top": industry.get("top")})
    drawdown = readings.get("drawdown") or {}
    if drawdown.get("pct") is not None and drawdown["pct"] >= drawdown["limit_pct"]:
        breaches.append({"rule": "drawdown", "pct": drawdown.get("pct"),
                         "limit_pct": drawdown["limit_pct"]})
    detail = {"readings": readings, "limits": dict(v3_ops.LIMITS),
              "probeNote": probe_reason, "breaches": breaches}
    if not readings:
        return TriggerResult("no-data", source=source,
                             detail={**detail, "reason": probe_reason or "无任何风控读数"})
    if not breaches:
        return TriggerResult("idle", source=source, detail={
            **detail, "reason": "有读数但未触及红线"})
    key = "risk:" + "+".join(f"{item['rule']}{item['pct']}" for item in breaches)
    return TriggerResult("fire", event_key=key[:120], source=source,
                         as_of=(industry.get("as_of") or drawdown.get("as_of")),
                         detail=detail, context={"breaches": breaches, "readings": readings})


def check_factor_reversal(runner, config, moment):
    """因子信号反转：``factor_snapshots`` 最近两日横截面均值的符号翻转。"""
    source = TRIGGER_SOURCES["factor_reversal"]
    store, conn, error = _store_connect(runner.home)
    if conn is None:
        return TriggerResult("no-data", source=source, detail={"reason": error})
    factor = str(config["thresholds"]["reversalFactor"])
    try:
        snapshots = store.list_factor_snapshots(conn, limit=2)
    except Exception as db_error:  # noqa: BLE001
        return TriggerResult("no-data", source=source, detail={"reason": f"查询失败：{db_error}"})
    finally:
        conn.close()
    if len(snapshots) < 2:
        return TriggerResult("no-data", source=source,
                             detail={"reason": f"因子快照不足两日（{len(snapshots)} 日），无法判翻转",
                                     "factor": factor})
    means = []
    for snapshot in snapshots[:2]:
        values = []
        for row in ((snapshot.get("payload") or {}).get("tickers") or {}).values():
            value = _number((row or {}).get(factor)) if isinstance(row, dict) else None
            if value is not None:
                values.append(value)
        means.append({"date": snapshot.get("date"), "mean": (sum(values) / len(values)
                                                             if values else None),
                      "n": len(values), "createdAt": snapshot.get("created_at")})
    latest, previous = means[0], means[1]
    if latest["mean"] is None or previous["mean"] is None:
        return TriggerResult("no-data", source=source, detail={
            "reason": f"两日快照里 {factor} 无可用读数", "means": means, "factor": factor})
    flipped = (latest["mean"] > 0 > previous["mean"]) or (latest["mean"] < 0 < previous["mean"])
    detail = {"factor": factor, "means": means, "flipped": flipped}
    if not flipped:
        return TriggerResult("idle", source=source, as_of=latest["date"],
                             detail={**detail, "reason": "符号未翻转"})
    return TriggerResult("fire", event_key=f"factor:{factor}:{latest['date']}",
                         source=source, as_of=latest["date"], detail=detail,
                         context={"factor": factor, "means": means})


# ---- 流水线节点 ----
def check_pre_rebalance_confirm(runner, config, moment):
    """调仓前决策确认：已冻结计划 + 该计划下未提交订单（真事实，不用订单数猜意图）。"""
    source = TRIGGER_SOURCES["pre_rebalance_confirm"]
    store, conn, error = _store_connect(runner.home)
    if conn is None:
        return TriggerResult("no-data", source=source, detail={"reason": error})
    try:
        plans = [plan for plan in store.list_plans(conn)
                 if str(plan.get("status")) == "frozen"]
        pending = []
        for plan in plans:
            orders = store.get_open_orders(conn, plan.get("plan_id"))
            if orders:
                pending.append({"planId": plan.get("plan_id"), "asOf": plan.get("as_of"),
                                "mode": plan.get("mode"), "orders": len(orders)})
    except Exception as db_error:  # noqa: BLE001
        return TriggerResult("no-data", source=source, detail={"reason": f"查询失败：{db_error}"})
    finally:
        conn.close()
    detail = {"frozenPlans": len(plans), "pending": pending}
    if not pending:
        return TriggerResult("idle", source=source,
                             detail={**detail, "reason": "无「已冻结且未提交」的计划"})
    first = pending[0]
    return TriggerResult("fire", event_key=f"plan:{first['planId']}", source=source,
                         as_of=first.get("asOf"), detail=detail,
                         context={"plans": pending})


def check_strategy_param_change(runner, config, moment):
    """策略参数变更审核：规则候选池里 ``status=candidate`` 的条目（人审通道）。"""
    source = TRIGGER_SOURCES["strategy_param_change"]
    store, conn, error = _store_connect(runner.home)
    if conn is None:
        return TriggerResult("no-data", source=source, detail={"reason": error})
    try:
        rules = store.get_rules(conn, status="candidate")
    except Exception as db_error:  # noqa: BLE001
        return TriggerResult("no-data", source=source, detail={"reason": f"查询失败：{db_error}"})
    finally:
        conn.close()
    detail = {"candidates": len(rules),
              "ruleIds": [rule.get("rule_id") for rule in rules][:20]}
    if not rules:
        return TriggerResult("idle", source=source,
                             detail={**detail, "reason": "候选池为空"})
    latest = max(rules, key=lambda rule: str(rule.get("created_at") or ""))
    return TriggerResult("fire", event_key=f"rule:{latest.get('rule_id')}", source=source,
                         as_of=latest.get("created_at"), detail=detail,
                         context={"rules": detail["ruleIds"]})


#: 触发条件注册表（可插拔：新增触发条件 = 往这里加一条 + 在 CONFIG 里给开关）
def build_triggers():
    triggers = []
    for slot in SLOTS:
        triggers.append(Trigger(f"slot:{slot['key']}", "schedule", slot["task_type"],
                                slot["label"], check_slot(slot)))
    triggers.extend([
        Trigger("breaking_news", "event", "breaking_news", "突发新闻", check_breaking_news),
        Trigger("position_move", "event", "risk_review", "持仓异动", check_position_move),
        Trigger("risk_breach", "event", "risk_review", "风控阈值突破", check_risk_breach),
        Trigger("factor_reversal", "event", "risk_review", "因子信号反转",
                check_factor_reversal),
        Trigger("pre_rebalance_confirm", "pipeline", "pre_market_scan", "调仓前决策确认",
                check_pre_rebalance_confirm),
        Trigger("strategy_param_change", "pipeline", "risk_review", "策略参数变更审核",
                check_strategy_param_change),
    ])
    return triggers


TRIGGERS = build_triggers()


# ---------------------------------------------------------------------------
# 上下文打包（headless 无历史 → 持仓/阈值/市场摘要必须进提示词）
# ---------------------------------------------------------------------------
class ContextBuilder:
    """按 task_type 打包提示词上下文；缺读数就在提示词里如实写「无数据源」。"""

    def __init__(self, runner):
        self.runner = runner

    def build(self, task_type, *, trigger=None, slot_context=None):
        config = self.runner.config
        market = str(config["market"])
        positions_env = self.runner.call_tool("positions", {})
        context = {
            "positions": render_positions(positions_env),
            "risk_limits": render_risk_limits(v3_ops.LIMITS,
                                              extra=(slot_context.get("extraLimits")
                                                     if slot_context else None)),
        }
        sources = {"positions": _positions_source(positions_env),
                   "risk_limits": "platform/server/v3_ops.py LIMITS"}
        if task_type == "pre_market_scan":
            context["universe"] = str(config["universe"])
            sources["universe"] = "v3-headless.json:universe"
        if task_type == "breaking_news":
            context["news_text"] = self._news_text(slot_context or {})
            sources["news_text"] = TRIGGER_SOURCES["breaking_news"]
        if task_type == "risk_review":
            context["market_summary"] = self._market_summary(market, slot_context)
            sources["market_summary"] = "tools: series + v3_db.oms_sync"
        return context, sources

    def _news_text(self, slot_context):
        event = (slot_context or {}).get("detail") or {}
        symbols = slot_context.get("symbols") or event.get("symbols") or []
        channels = slot_context.get("channels") or event.get("channels") or []
        observations = slot_context.get("observations") or event.get("observations")
        if not symbols and not channels:
            return ("无数据源（触发时没有拿到资讯观测明细；窗口/条数见 "
                    "store.sentiment_snapshots 的触发判定记录）")
        lines = [f"- 窗口内资讯观测 {observations} 条，渠道 {','.join(map(str, channels)) or '未知'}",
                 f"- 涉及标的：{','.join(map(str, symbols)) or '未取到'}",
                 "- 原文在平台库 sentiment_snapshots（date/symbol/source/payload），"
                 "可用 v3_* 工具面查询；平台**不做情绪打分**，请只依据原文与行情读数判断。"]
        return "\n".join(lines)

    def _market_summary(self, market, slot_context):
        window = (slot_context or {}).get("window") or "intraday"
        benchmark = BENCHMARKS.get(market, BENCHMARKS["SH"])
        parts = [f"窗口：{SLOT_CONTEXT.get(window, window)}；市场：{market}；基准：{benchmark}"]
        series = self.runner.call_tool("series", {"ticker": benchmark, "period": "1d",
                                                  "limit": 6})
        if isinstance(series, dict) and series.get("ok"):
            rows = ((series.get("value") or {}).get("bars")
                    or (series.get("value") or {}).get("series") or [])
            closes = [_number(row.get("close")) for row in rows if isinstance(row, dict)]
            closes = [value for value in closes if value is not None]
            if len(closes) >= 2:
                change = (closes[-1] / closes[0] - 1) * 100
                parts.append(f"- {benchmark} 近 {len(closes)} 根日K：最新收盘 {closes[-1]}，"
                             f"区间变动 {change:.2f}%（来源：series 工具面，"
                             f"source={series.get('value', {}).get('source') or 'n/a'}）")
            else:
                parts.append(f"- {benchmark} 日K读数不足（{len(closes)} 根），不给区间变动")
        else:
            reason = ""
            if isinstance(series, dict) and isinstance(series.get("error"), dict):
                reason = str(series["error"].get("message") or series["error"].get("code") or "")
            parts.append(f"- 行情无数据源（series 取数失败：{reason or '未知原因'}）")
        try:
            syncs = v3_db.list_events(self.runner.home, "oms_sync", limit=1)
        except Exception as error:  # noqa: BLE001
            syncs = []
            parts.append(f"- 对账台账读取失败（{error}）")
        if syncs:
            latest = syncs[0]
            parts.append(f"- 最近一次对账（{latest.get('at')}）：nav={latest.get('nav')} "
                         f"回撤={latest.get('drawdown_pct')}% "
                         f"行业暴露={latest.get('industry_pct')}% "
                         f"（source={latest.get('nav_source') or 'n/a'}）")
        else:
            parts.append("- 无对账台账记录（v3_db.oms_sync 空）")
        return "\n".join(parts)


def _positions_source(envelope):
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return "无数据源（positions 工具面未返回）"
    return "tools: positions（工作台 MCP 工具面 → 富途持仓）"


# ---------------------------------------------------------------------------
# 调度器（FR-GATEWAY-004）
# ---------------------------------------------------------------------------
class HeadlessScheduler:
    """触发条件评估 + 投递（**不阻塞**调用方；worker 池负责排队与熔断）。

    ``tick_once`` 是确定性的（可注入 ``moment``），线程循环只是按
    ``triggerIntervalSeconds`` 反复调它——测试与探针都直接调 ``tick_once``，
    不需要等线程。
    """

    def __init__(self, runner, *, triggers=None, state_path=None, store=None):
        self.runner = runner
        self.triggers = list(triggers if triggers is not None else TRIGGERS)
        self.state_path = Path(state_path or (Path(runner.home) / STATE_FILENAME))
        self._lock = threading.Lock()
        self._state = self._load_state()
        self._thread = None
        self._stop = threading.Event()
        self._last_tick = None
        self._tickets = []

    # ---- 状态文件（去重 + 最近触发/判定留痕） ----
    def _load_state(self):
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("fired", {})
                payload.setdefault("evaluations", {})
                payload.setdefault("recent", [])
                return payload
        except (OSError, ValueError):
            pass
        return {"fired": {}, "evaluations": {}, "recent": []}

    def _save_state(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    def _switch(self, trigger):
        config = self.runner.config
        if trigger.kind == "schedule":
            return bool((config.get("schedule") or {}).get(trigger.key.split(":", 1)[1], False))
        if trigger.kind == "event":
            return bool((config.get("events") or {}).get(trigger.key, False))
        return bool((config.get("pipeline") or {}).get(trigger.key, False))

    def _cooldown_bucket(self, trigger, moment):
        minutes = max(1.0, float(self.runner.config["cooldownMinutes"]))
        if trigger.kind == "schedule":
            return None  # 定时用「当天一次」的去重键（check 里给的 event_key）
        step = int(moment.timestamp() // (minutes * 60))
        return f"{trigger.key}:{step}"

    # ---- 一次评估 ----
    def tick_once(self, moment=None):
        moment = moment or self.runner.clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        config = self.runner.config
        result = {"at": _now_iso(moment), "checked": [], "fired": [], "skipped": [],
                  "enabled": bool(config["enabled"]), "reason": None}
        if not config["enabled"]:
            result["reason"] = "headless 通道总开关关闭"
            self._last_tick = result
            return result
        for trigger in self.triggers:
            if not self._switch(trigger):
                result["skipped"].append({"trigger": trigger.key, "reason": "开关关闭"})
                continue
            try:
                outcome = trigger.check(self.runner, config, moment)
            except Exception as error:  # noqa: BLE001 —— 一条触发条件坏掉不拖垮其它
                outcome = TriggerResult("error", source=trigger.note,
                                        detail={"reason": f"{type(error).__name__}: {error}"})
            entry = outcome.to_dict()
            entry["trigger"] = trigger.key
            entry["kind"] = trigger.kind
            entry["evaluatedAt"] = result["at"]
            self._state["evaluations"][trigger.key] = entry
            result["checked"].append(entry)
            if outcome.status != "fire":
                continue
            event_key = outcome.event_key or f"{trigger.key}:{result['at']}"
            bucket = self._cooldown_bucket(trigger, moment) if trigger.kind != "schedule" \
                else None
            fire_key = bucket or event_key
            with self._lock:
                if fire_key in self._state["fired"]:
                    result["skipped"].append({"trigger": trigger.key, "reason": "冷却中",
                                              "fireKey": fire_key})
                    continue
                self._state["fired"][fire_key] = {"at": result["at"],
                                                  "trigger": trigger.key,
                                                  "eventKey": event_key}
            try:
                prompt, context, sources = self._build_task(trigger, outcome, moment)
            except Exception as error:  # noqa: BLE001
                result["skipped"].append({"trigger": trigger.key,
                                          "reason": f"提示词构造失败：{error}"})
                continue
            ticket = self.runner.submit(prompt, task_type=trigger.task_type,
                                       trigger=trigger.key, event_key=event_key,
                                       context=context, sources=sources)
            self._tickets.append(ticket)
            del self._tickets[:-50]
            fired = {"trigger": trigger.key, "kind": trigger.kind, "taskType": trigger.task_type,
                     "eventKey": event_key, "ticket": ticket.id, "source": outcome.source,
                     "asOf": outcome.as_of, "detail": outcome.detail}
            result["fired"].append(fired)
            with self._lock:
                self._state["recent"].append({**fired, "at": result["at"]})
                del self._state["recent"][:-50]
        with self._lock:
            self._prune_fired(moment)
        self._save_state()
        self._last_tick = result
        return result

    def _prune_fired(self, moment):
        """去重键只留近 14 天（避免状态文件无限长大）。"""
        cutoff = (moment - timedelta(days=14)).isoformat()
        fired = self._state["fired"]
        for key in [key for key, value in fired.items()
                    if str((value or {}).get("at") or "") < cutoff]:
            fired.pop(key, None)

    def _build_task(self, trigger, outcome, moment):
        builder = ContextBuilder(self.runner)
        slot_context = {**outcome.context, "detail": outcome.detail, "window": outcome.detail.get("window")}
        context, sources = builder.build(trigger.task_type, trigger=trigger.key,
                                         slot_context=slot_context)
        context = dict(context)
        context["triggerNote"] = f"触发条件：{trigger.key}（{trigger.label}）；来源：{outcome.source}"
        prompt = build_task_prompt(trigger.task_type, context)
        sources = dict(sources, trigger=outcome.source, asOf=outcome.as_of)
        return prompt, context, sources

    # ---- 线程 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="v3-headless-scheduler",
                                        daemon=True)
        self._thread.start()
        return True

    def _loop(self):
        interval = max(5.0, float(self.runner.config["triggerIntervalSeconds"]))
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001 —— 单轮异常不杀线程
                pass
            self._stop.wait(interval)

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def stop_all(self, timeout=5.0):
        self.stop(timeout)
        self.runner.stop(timeout)

    # ---- 只读视图（可开关、可查） ----
    def next_fire_times(self, moment=None, horizon_days=10, limit=12):
        """下一次触发时间：把三个定时时点在未来 ``horizon_days`` 自然日里展开，按时间排序。"""
        moment = moment or self.runner.clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        upcoming = []
        for slot in SLOTS:
            market = slot["market"]
            tzname = v3_market_calendar.MARKET_TIMEZONES.get(market, "Asia/Shanghai")
            for offset in range(0, horizon_days + 1):
                day = moment.astimezone(ZoneInfo(tzname)).date() + timedelta(days=offset)
                trading, loaded, note = is_trading_day(self.runner.home, market, day,
                                                       env=self.runner._env)
                if not trading:
                    continue
                due = slot_datetime(slot, moment, tzname).replace(
                    year=day.year, month=day.month, day=day.day)
                if due <= moment.astimezone(ZoneInfo(tzname)):
                    continue
                upcoming.append({"trigger": f"slot:{slot['key']}", "label": slot["label"],
                                 "at": due.isoformat(), "market": market,
                                 "taskType": slot["task_type"], "holidaysLoaded": loaded,
                                 "holidaysNote": note})
                break
        upcoming.sort(key=lambda item: item["at"])
        return upcoming[:limit]

    def view(self, moment=None):
        moment = moment or self.runner.clock()
        evaluations = self._state.get("evaluations") or {}
        return {
            "params": self.runner.params(),
            "triggers": [
                {**trigger.to_dict(),
                 "enabled": self._switch(trigger),
                 "source": trigger.note,
                 "lastEvaluation": evaluations.get(trigger.key)}
                for trigger in self.triggers
            ],
            "nextFireTimes": self.next_fire_times(moment),
            "recent": list(self._state.get("recent") or [])[-20:][::-1],
            "lastTick": self._last_tick,
            "schedulerAlive": self.alive,
            "firedKeys": len(self._state.get("fired") or {}),
            "statePath": str(self.state_path),
            "calendarSource": "platform/server/v3_market_calendar.py",
        }


# ---------------------------------------------------------------------------
# 进程内注册表 + 平台调度线程的挂钩
# ---------------------------------------------------------------------------
_REGISTRY = {}


def _key(home):
    try:
        return str(Path(home).resolve())
    except OSError:  # pragma: no cover
        return str(home)


def get_entry(home):
    return _REGISTRY.get(_key(home))


def reset_registry():
    """测试用：清空注册表（不 stop，调用方自己收）。"""
    _REGISTRY.clear()


def scheduler_tick(home, *, now=None):
    """平台调度线程每轮的挂钩（``server/scheduler.py`` 里**一行**）。

    调度器还没注册（模块未装配 / 测试环境）→ 返回 ``{"ok": false, "reason": ...}``，
    不抛异常（不能因为 headless 通道的问题弄挂交易作业链）。
    """
    entry = get_entry(home)
    if entry is None:
        return {"ok": False, "reason": "headless-not-registered"}
    scheduler = entry["scheduler"]
    started = scheduler.start() if not scheduler.alive else False
    return {"ok": True, "schedulerStarted": started, "alive": scheduler.alive,
            "nextFireTimes": scheduler.next_fire_times(now) if now is not None
            else scheduler.next_fire_times()[:3]}


# ---------------------------------------------------------------------------
# 只读端点
# ---------------------------------------------------------------------------
def _ok(content):
    return JSONResponse(status_code=200, content=content)


def _failure(code, message, **extra):
    payload = {"ok": False, "error": {"code": code, "message": message}}
    payload["error"].update(extra)
    return payload


def query_log(home, *, limit=50, offset=0, success=None, task_type=None, trigger=None,
              outcome=None, since=None, until=None):
    """读 ``headless_log``（真实表）并做分页/筛选；返回 ``(payload, error)``。"""
    try:
        rows = v3_db.list_events(home, LOG_TABLE, limit=None)
    except Exception as error:  # noqa: BLE001
        return None, str(error)
    filtered = []
    for row in rows:
        if success is not None and bool(row.get("success")) != success:
            continue
        if task_type and row.get("task_type") != task_type:
            continue
        if trigger and row.get("trigger") != trigger:
            continue
        if outcome and row.get("outcome") != outcome:
            continue
        started = str(row.get("started_at") or "")
        if since and started < since:
            continue
        if until and started > until:
            continue
        filtered.append(row)
    filtered.sort(key=lambda row: str(row.get("started_at") or ""), reverse=True)
    page = filtered[offset:offset + limit] if limit else filtered[offset:]
    return {"items": page, "total": len(rows), "filtered": len(filtered),
            "limit": limit, "offset": offset, "source": f"v3_db.{LOG_TABLE}"}, None


def summarize(rows):
    """调用统计（给端点与 v3_ops 的 ``headless.today`` 口径复用）。"""
    total = len(rows)
    success = sum(1 for row in rows if row.get("success"))
    killed = sum(1 for row in rows if row.get("killed"))
    durations = [_number(row.get("duration_ms")) for row in rows]
    durations = [value for value in durations if value is not None]
    tokens = sum(int(row.get("tokens_estimate") or 0) for row in rows)
    return {"total": total, "success": success, "failed": total - success,
            "killed": killed, "avgMs": round(sum(durations) / len(durations), 3)
            if durations else 0, "tokensEstimate": tokens,
            "tokensEstimateNote": TOKEN_ESTIMATE_NOTE}


def today_summary(home, moment=None):
    """今日口径的统计（``/api/v3/gateway`` 的 ``headless.today`` 可用；读真实表）。"""
    moment = moment or datetime.now(timezone.utc)
    day = moment.astimezone(timezone.utc).date().isoformat()
    try:
        rows = [row for row in v3_db.list_events(home, LOG_TABLE, limit=None)
                if str(row.get("started_at") or "").startswith(day)]
    except Exception:  # noqa: BLE001
        return None
    return summarize(rows)


def last_records(home, limit=10):
    """最近 N 条调用的**精简投影**（不含 prompt/stdout 全文；给网关视图用）。"""
    try:
        rows = v3_db.list_events(home, LOG_TABLE, limit=limit)
    except Exception:  # noqa: BLE001
        return []
    return [{"startedAt": row.get("started_at"), "taskType": row.get("task_type"),
             "trigger": row.get("trigger"), "success": bool(row.get("success")),
             "exitCode": row.get("exit_code"), "durationMs": row.get("duration_ms"),
             "tokensEstimate": row.get("tokens_estimate"), "outcome": row.get("outcome"),
             "killed": bool(row.get("killed"))} for row in rows]


def register(app, v3_run, home):
    """装配（``app.py`` 自动接线调用）：建 Runner + 调度器 + 两个只读端点。

    本函数**不起线程**：触发循环由平台调度线程通过 :func:`scheduler_tick` 保活
    （``server/scheduler.py`` 的 ``build_tick`` 里一行）。这样服务停了 headless
    就不再唤醒，不会有第二个「看不见的调度器」。
    """
    runner = HeadlessRunner(home, v3_run=v3_run, data_dir=None)
    scheduler = HeadlessScheduler(runner)
    entry = {"runner": runner, "scheduler": scheduler}
    _REGISTRY[_key(home)] = entry

    @app.get("/api/v3/headless/log")
    async def v3_headless_log(request: Request, limit: int = 50, offset: int = 0,
                              success: str = "", task_type: str = "", trigger: str = "",
                              outcome: str = "", since: str = "", until: str = ""):
        """Headless 调用日志（FR-MON-003）：真实 ``headless_log`` 表 + 分页/筛选。

        筛选：``success=true|false``、``task_type``、``trigger``、``outcome``、
        ``since``/``until``（ISO 时间字符串，按 ``started_at`` 前缀比较）。**只读**。
        """
        def work():
            flag = None
            text = str(success or "").strip().lower()
            if text in ("1", "true", "yes", "ok"):
                flag = True
            elif text in ("0", "false", "no", "failed"):
                flag = False
            payload, error = query_log(home, limit=max(0, int(limit)),
                                       offset=max(0, int(offset)), success=flag,
                                       task_type=task_type.strip() or None,
                                       trigger=trigger.strip() or None,
                                       outcome=outcome.strip() or None,
                                       since=since.strip() or None,
                                       until=until.strip() or None)
            if error:
                return _failure("headless/log-unavailable",
                                f"调用日志不可读（{LOG_TABLE}）：{error}")
            return {"ok": True, **payload, "summary": summarize(payload["items"]),
                    "estimation": TOKEN_ESTIMATE_NOTE,
                    "streams": "stdout/stderr 分列存储（separate pipes；本端点的 items[].stdout"
                               " / .stderr 即原始两路）"}
        return _ok(await _to_thread(work))

    @app.get("/api/v3/headless/schedule")
    async def v3_headless_schedule():
        """触发策略只读视图：开关、下一次触发时间、最近触发记录、每条触发条件的最近判定。"""
        def work():
            view = scheduler.view()
            view["ok"] = True
            view["breaker"] = {key: view["params"][key] for key in
                               ("maxConcurrency", "timeoutSeconds", "tokenBudget")}
            view["breakerNote"] = ("外部熔断（FR-GATEWAY-004）：并发上限 / 单次超时 / token 预算"
                                   "（估算）三参数；超限即 kill 并记录")
            view["profileWhitelist"] = {
                "profile": view["params"]["profile"],
                "profileDir": str(runner.profile_dir()),
                "verified": runner.preflight() is None,
                "detail": runner.state()["whitelist"],
                "denyToolCount": len(deny_tool_names()),
                "material": str(runner.material_dir),
            }
            return view
        return _ok(await _to_thread(work))

    return runner, scheduler


async def _to_thread(work):
    import asyncio
    return await asyncio.to_thread(work)
