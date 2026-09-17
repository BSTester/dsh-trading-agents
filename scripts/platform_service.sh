#!/usr/bin/env bash
# 工作台服务进程启停（start|stop|status|restart）——**耐用拉起**是这里的唯一目的。
#
# 为什么需要这个脚本（2026-09-17 实测教训）：
#   前台运行（`python -m server.run`）随终端/会话结束而死；`setsid nohup ... &` 在
#   某些环境下也会被回收（本仓库实测：会话结束时进程消失，端口释放）。仓库的
#   `platform-autostart` 插件用 node `spawn({detached:true, stdio:[ignore,log,log]}) + unref()`
#   才是耐用方式，本脚本**优先复用同一语义**，node 不可用时退回 `setsid` 兜底。
#
# 与 platform-autostart 的关系：
#   插件在**会话开始时**探活 `/healthz`，通了就什么都不做；不通才拉起。本脚本是**显式**入口
#   （运维/演练/CI 用），二者共享同一个日志文件与同一套路径解析规则，重复拉起会被
#   「已在运行」拦下（也不会与插件的并发拉起竞态：服务自身绑定端口，最终只有一个监听者）。
#
# 用法：
#   scripts/platform_service.sh start      # 未运行才拉起；已运行则打印现状
#   scripts/platform_service.sh stop       # 优雅 TERM → 等待端口释放 → 超时才 KILL
#   scripts/platform_service.sh status     # 端口/健康/PID/日志尾部/工具面计数
#   scripts/platform_service.sh restart    # stop + start
#
# 环境变量：
#   DSH_HOME                  数据根（默认 ~/.dsh）
#   DSH_TRADING_REPO          仓库根覆盖（默认读 <home>/trading-platform-repo，再退回本脚本上一级）
#   PLATFORM_START_TIMEOUT     start 后等待就绪的秒数（默认 30）
#   PLATFORM_STOP_TIMEOUT      stop 等待端口释放的秒数（默认 15）
#
# 退出码：0 成功；1 操作未达预期（未就绪/未停止）；2 前置条件缺失（仓库/venv/入口）。
set -euo pipefail

HOME_DIR="${DSH_HOME:-$HOME/.dsh}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_TIMEOUT="${PLATFORM_START_TIMEOUT:-30}"
STOP_TIMEOUT="${PLATFORM_STOP_TIMEOUT:-15}"

# ---------------------------------------------------------------- 路径解析
# 仓库根优先级与 platform-autostart.resolvePaths 同序：环境变量 > 标记文件 > 本脚本位置。
resolve_repo() {
  if [[ -n "${DSH_TRADING_REPO:-}" ]]; then
    printf '%s\n' "$DSH_TRADING_REPO"
    return
  fi
  if [[ -f "$HOME_DIR/trading-platform-repo" ]]; then
    local marker
    marker="$(head -n1 "$HOME_DIR/trading-platform-repo" | tr -d '[:space:]')"
    if [[ -n "$marker" ]]; then
      printf '%s\n' "$marker"
      return
    fi
  fi
  (cd "$SCRIPT_DIR/.." && pwd)   # 本脚本位于 <repo>/scripts/，上一级即仓库根
}

REPO_ROOT="$(resolve_repo)"
VENV_PY="$HOME_DIR/trading-venv/bin/python"
ENTRY_DIR="$REPO_ROOT/platform"
LOG_FILE="$HOME_DIR/trading-platform-service.log"

# ---------------------------------------------------------------- 配置读取
# service.host / service.port 从 trading-platform.json 读（**不硬编码 8397**）；
# 坏 JSON / 缺键一律回落默认——与 install_platform.service_config 同口径。
read_service_cfg() {
  local py=""
  for cand in "$VENV_PY" python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then py="$cand"; break; fi
  done
  if [[ -n "$py" ]]; then
    "$py" - "$HOME_DIR/trading-platform.json" <<'PYEOF' 2>/dev/null || echo "127.0.0.1 8397"
import json, sys
host, port = "127.0.0.1", 8397
try:
    raw = json.load(open(sys.argv[1], encoding="utf-8"))
    svc = raw.get("service") if isinstance(raw, dict) else None
    if isinstance(svc, dict):
        h = svc.get("host")
        if isinstance(h, str) and h:
            host = h
        p = svc.get("port")
        if isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536:
            port = p
except Exception:
    pass
print(host, port)
PYEOF
  else
    echo "127.0.0.1 8397"
  fi
}
read -r HOST PORT < <(read_service_cfg)
BASE_URL="http://${HOST}:${PORT}"

# ---------------------------------------------------------------- 探活 / 进程
health_json() { curl -s --max-time 3 "$BASE_URL/healthz" 2>/dev/null || true; }
is_healthy() {
  local body
  body="$(health_json)"
  [[ "$body" == *'"ok":true'* ]]
}
listening_pid() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null | head -n1 || true
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk -v p=":${PORT}" '$4 ~ p {print $NF}' \
      | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -n1 || true
  fi
}
port_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk -v p=":${PORT}" '$4 ~ p {found=1} END {exit !found}'
  else
    [[ -n "$(listening_pid)" ]]
  fi
}

# 工具面计数：端点数为服务自报（snapshot 的 endpoints 声明），工具面取源码常量
# （MCP 工具数要经 /mcp 握手才有，shell 里读源码常量并**标明来源**更诚实）。
tool_counts() {
  local py=""
  for cand in "$VENV_PY" python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then py="$cand"; break; fi
  done
  local endpoints="?"
  if [[ -n "$py" ]]; then
    endpoints="$("$py" - "$BASE_URL" <<'PYEOF' 2>/dev/null || echo "?"
import json, sys, urllib.request
try:
    req = urllib.request.Request(sys.argv[1] + "/api/wb/snapshot", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        eps = (json.loads(r.read()).get("value") or {}).get("endpoints") or []
    print(len(eps))
except Exception:
    print("?")
PYEOF
)"
  fi
  local tools="?"
  if [[ -f "$ENTRY_DIR/server/mcp_tools.py" ]]; then
    tools="$(grep -m1 -E '^TOOL_COUNT[[:space:]]*=' "$ENTRY_DIR/server/mcp_tools.py" \
      | sed -E 's/.*=[[:space:]]*([0-9]+).*/\1/' || echo "?")"
  fi
  printf '端点 %s（服务声明）｜工具 %s（源码 TOOL_COUNT）' "$endpoints" "$tools"
}

require_prereqs() {
  local bad=0
  if [[ ! -d "$ENTRY_DIR" ]]; then
    echo "❌ 仓库不完整：入口目录缺失（$ENTRY_DIR）" >&2; bad=1
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    echo "❌ venv 解释器缺失：$VENV_PY" >&2
    echo "   创建：python3 -m venv \"$HOME_DIR/trading-venv\" && \"$VENV_PY\" -m pip install -r \"$ENTRY_DIR/requirements.txt\"" >&2
    bad=1
  fi
  return $bad
}

# ---------------------------------------------------------------- 拉起（detached）
spawn_service() {
  mkdir -p "$HOME_DIR"
  # 首选：node detached spawn —— 与 platform-autostart 完全同一语义（随会话结束仍存活）
  if command -v node >/dev/null 2>&1; then
    node -e '
      const { spawn } = require("node:child_process");
      const fs = require("node:fs");
      const [py, cwd, log] = process.argv.slice(1);
      const fd = fs.openSync(log, "a");
      const child = spawn(py, ["-m", "server.run"], {
        cwd, detached: true, stdio: ["ignore", fd, fd],
      });
      child.unref();
      fs.closeSync(fd);
    ' "$VENV_PY" "$ENTRY_DIR" "$LOG_FILE" && return 0
    echo "⚠ node 拉起失败，退回 setsid 兜底" >&2
  fi
  # 兜底：setsid + nohup（部分环境仍会被回收，故仅作后备）
  ( cd "$ENTRY_DIR" && setsid nohup "$VENV_PY" -m server.run >>"$LOG_FILE" 2>&1 </dev/null & )
}

wait_ready() {
  local deadline=$(( SECONDS + START_TIMEOUT ))
  while (( SECONDS < deadline )); do
    if is_healthy; then return 0; fi
    sleep 0.5
  done
  return 1
}

cmd_start() {
  require_prereqs || return 2
  if is_healthy; then
    echo "✅ 服务已在运行，不重复拉起"
    echo "   URL：$BASE_URL（host=$HOST port=$PORT）｜PID=$(listening_pid || echo '?')"
    return 0
  fi
  if port_listening; then
    echo "❌ 端口 $PORT 已被**非本服务**占用（/healthz 不通），不强行拉起" >&2
    echo "   占用 PID：$(listening_pid || echo '?')；请先停掉它或改 trading-platform.json 的 service.port" >&2
    return 1
  fi
  echo "启动平台服务（detached）…"
  echo "  解释器：$VENV_PY"
  echo "  工作目录：$ENTRY_DIR"
  echo "  日志：$LOG_FILE"
  spawn_service
  if wait_ready; then
    echo "✅ 已就绪：$BASE_URL（${START_TIMEOUT}s 内）"
    echo "   $(tool_counts)"
    echo "   模式：$(health_json | sed -n 's/.*"mode":"\([a-z]*\)".*/\1/p')"
    return 0
  fi
  echo "❌ ${START_TIMEOUT}s 内未就绪；日志尾部：" >&2
  tail -n 15 "$LOG_FILE" 2>/dev/null >&2 || true
  return 1
}

# ---------------------------------------------------------------- 停止
cmd_stop() {
  local pid
  pid="$(listening_pid)"
  if [[ -z "$pid" ]]; then
    if port_listening; then
      echo "❌ 端口 $PORT 在监听但取不到 PID（缺 lsof/ss 权限？）" >&2
      return 1
    fi
    echo "✅ 服务已停止（端口 $PORT 无监听）"
    return 0
  fi
  echo "停止服务（PID $pid）…"
  kill -TERM "$pid" 2>/dev/null || true
  local deadline=$(( SECONDS + STOP_TIMEOUT ))
  while (( SECONDS < deadline )); do
    if ! port_listening; then
      echo "✅ 已优雅停止（端口 $PORT 已释放）"
      return 0
    fi
    sleep 0.5
  done
  echo "⚠ ${STOP_TIMEOUT}s 未退出，发送 SIGKILL"
  kill -KILL "$pid" 2>/dev/null || true
  deadline=$(( SECONDS + 5 ))
  while (( SECONDS < deadline )); do
    if ! port_listening; then
      echo "✅ 已强制停止（端口 $PORT 已释放）"
      return 0
    fi
    sleep 0.5
  done
  echo "❌ 端口 $PORT 仍被占用；请人工检查 PID $pid" >&2
  return 1
}

# ---------------------------------------------------------------- 状态
# `status` 的输出是**尽力而为**的（运维常写 `status | head`，消费者提前关闭管道会让
# echo/tail 报 EPIPE）：先把整段输出攒进变量、再一次性 printf（stderr 丢弃），
# 判定完全靠**显式 return**。否则 `status | head -5` 会因写管道失败而误报非零/刷噪音。
cmd_status() {
  set +e +o pipefail
  local out rc
  out="$(_status_body)"
  rc=$?
  printf '%s\n' "$out" 2>/dev/null
  return $rc
}

_status_body() {
  local pid body
  pid="$(listening_pid)"
  body="$(health_json)"
  echo "平台服务状态"
  echo "  地址：$BASE_URL（host=$HOST port=$PORT，取自 trading-platform.json 的 service 节）"
  echo "  仓库：$REPO_ROOT"
  echo "  解释器：$VENV_PY"
  echo "  日志：$LOG_FILE"
  if [[ -n "$pid" ]]; then echo "  PID：$pid"; else echo "  PID：—（端口无监听）"; fi
  if [[ -n "$body" ]]; then
    echo "  /healthz：$body"
  else
    echo "  /healthz：无响应"
  fi
  if is_healthy; then echo "  $(tool_counts)"; fi
  if [[ -f "$LOG_FILE" ]]; then
    echo "  日志尾部（3 行）："
    tail -n 3 "$LOG_FILE" | sed 's/^/    /'
  else
    echo "  日志尾部：日志文件不存在"
  fi
  if is_healthy; then return 0; fi
  return 1
}

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
  case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    status)  cmd_status ;;
    restart) cmd_stop && cmd_start ;;
    ""|-h|--help|help) usage ;;
    *) echo "未知子命令：$1（可用：start|stop|status|restart）" >&2; usage >&2; exit 2 ;;
  esac
}

main "$@"
