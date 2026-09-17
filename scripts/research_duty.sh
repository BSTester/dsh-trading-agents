#!/usr/bin/env bash
# 值班研究员（L3）唤醒入口（WP15 任务 4；规格 §10.4）。
#
# 设计要点（改这个脚本前先读）：
#   * 唤醒的是 DSH 本体的 **headless 单次运行**（`dsh --profile headless "<任务>"`：
#     回答一个任务、打印结果、退出）——服务进程不承载 LLM 循环（规格 §3.1 第三条），
#     本脚本自己也不做循环、不常驻；
#   * 本脚本只做三件事：探活平台服务（队列的领取/回报端点由服务提供）、拼装提示词、
#     带超时运行并落日志；
#   * 提示词把「领取→处理→回报」循环与**禁用写端点点名清单**写死在正文里：
#     定时执行体没有人类在场纠偏，提示漂移等于越权（技能「值班模式的边界」第 2 条）；
#   * 任何失败都非零退出并给出可读指引，不静默成功（RUNBOOK「值班研究员（L3）」）。
set -euo pipefail

home="${DSH_HOME:-$HOME/.dsh}"
profile="${DSH_DUTY_PROFILE:-headless}"
budget="${DSH_DUTY_TIMEOUT:-1800}"
base_url="${DSH_DUTY_BASE_URL:-http://127.0.0.1:8397}"
python_bin="${DSH_DUTY_PYTHON:-$home/trading-venv/bin/python}"
log_dir="$home/logs"
guide="排障见 docs/RUNBOOK.md「值班研究员（L3）」"

fail() { printf '[research-duty] %s\n' "$1" >&2; exit "${2:-1}"; }

dsh_bin="${DSH_BIN:-}"
if [ -z "$dsh_bin" ]; then
  dsh_bin="$(command -v dsh 2>/dev/null || true)"
fi
[ -n "$dsh_bin" ] || fail "找不到 dsh 命令：L3 唤醒要跑 DSH 本体的 headless 会话。把 dsh 放进 PATH，或用 DSH_BIN 指定绝对路径；$guide" 127

[ -x "$python_bin" ] || fail "找不到可执行的 Python（$python_bin）：探活与运行都需要量化 venv，或用 DSH_DUTY_PYTHON 指向可用解释器；$guide" 1

# 服务探活：队列端点由平台服务提供，服务没起就没有可领取的任务——此时**不该**假装
# 值班成功（非零退出，让 systemd/cron 与运维看到失败），也不自作主张拉起服务
# （拉起是 platform-autostart 的职责，见 RUNBOOK）。
if ! "$python_bin" - "$base_url" <<'PY'
import sys, urllib.request
url = sys.argv[1].rstrip("/") + "/healthz"
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        sys.exit(0 if response.status == 200 else 3)
except Exception:
    sys.exit(3)
PY
then
  fail "平台服务不可达（$base_url/healthz）：先启动服务，或确认会话自动拉起（platform-autostart）已生效；$guide" 2
fi

prompt="$(cat <<'PROMPT'
你是值班研究员（L3 定时唤醒）。本次唤醒的职责是消费平台的任务队列，不是与用户对话。

执行循环（反复做，直到队列为空）：
1. 调用 mcp__quantwb__research_tasks_claim（无参数）。
2. 返回 task 非空 → 按 skills/research-institute/SKILL.md「值班模式」手册处理这一条：
   读 payload 给出的 refs/标的/窗口，产出简报、巡检报告或候选提案；结束后调用
   mcp__quantwb__research_tasks_report 回报——成功 ok=true 且 result_ref 指向产物；
   失败如实 ok=false 且 err 写清卡在哪，不掩盖、不重跑到「看起来成功」。
3. 返回 task = null → 队列已空，本次值班收工，停止调用并直接结束。

硬约束（等同于技能「值班模式的边界」，逐条不得违反）：
- 禁止调用任何写端点，点名清单：trade_place、trade_modify、trade_cancel、plan_execute、
  switch_mode、auto_pipeline、rules-decide、confirm-decide。前五个是交易/模式写；
  后三个只属于人的动作（流水线开关、规则批准、实盘确认），它们不在你的工具面里，
  也不许经 shell 或 HTTP 绕过。
- 不直接下单、不直接启用策略：产物只进研究页与候选池，启用由用户在 Web 批准。
- 不修改任务载荷（只能领取与回报）；refs 里某张表 0 行时如实写「当日无该源快照」。
- 领到坏载荷被拒（critical 告警）→ 停止本次值班并如实报告，不猜、不绕。
PROMPT
)"

mkdir -p "$log_dir"
log="$log_dir/research-duty-$(date +%Y%m%d-%H%M%S).log"

runner=("$dsh_bin" --profile "$profile" "$prompt")
if command -v timeout >/dev/null 2>&1; then
  runner=(timeout "$budget" "${runner[@]}")
else
  printf '[research-duty] 未找到 timeout 命令：本次没有单次运行时长上限（建议装 coreutils）；%s\n' "$guide" >&2
fi

set +e
"${runner[@]}" 2>&1 | tee "$log"
status="${PIPESTATUS[0]}"
set -e

if [ "$status" -eq 124 ]; then
  fail "本次值班超过 ${budget}s 上限已中止：未完成任务留在队列（下次唤醒或开会话补跑），日志 $log；$guide" 124
fi
if [ "$status" -ne 0 ]; then
  fail "headless 值班会话退出码 $status（未完成的任务留在队列，不会被丢弃）；日志 $log；$guide" "$status"
fi
printf '[research-duty] 本次值班结束：队列已清空或按手册收工；日志 %s\n' "$log"
