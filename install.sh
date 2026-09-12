#!/usr/bin/env bash
# dsh-trading-agents 安装/更新 + 富途 token 设置
#
# 官方安装方式其实只有一行（preset 目录 = 仓库根目录，放入即被发现生效）：
#   git clone https://github.com/BSTester/dsh-trading-agents "$HOME/.dsh/.agent-presets/dsh-trading-agents"
#
# 本脚本做的是这行命令之上的补充：更新已有安装、引导录入富途 token。
set -euo pipefail

REPO_URL="https://github.com/BSTester/dsh-trading-agents.git"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PRESET_ID="dsh-trading-agents"
PRESET_DST="$DSH_HOME/.agent-presets/$PRESET_ID"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m ==\033[0m %s\n' "$*"; }

if ! command -v git >/dev/null 2>&1; then
  warn "未找到 git，请先安装 git。"; exit 1
fi

# 安装或更新（preset 发现是即时的，无需重启 harness）
if [[ -d "$PRESET_DST/.git" ]]; then
  say "已安装，更新到最新版本…"
  git -C "$PRESET_DST" pull --ff-only
else
  say "安装对话模式到 $PRESET_DST …"
  mkdir -p "$(dirname "$PRESET_DST")"
  git clone --depth 1 "$REPO_URL" "$PRESET_DST"
fi
say "完成。DeepSeek Harness 会自动发现新 preset（无需重启）。"

# 3. 环境初始化：持久 venv + 数据渠道依赖（AKShare/playwright）
VENV="$DSH_HOME/trading-venv"
if command -v python3 >/dev/null 2>&1; then
  if [[ ! -x "$VENV/bin/python" ]]; then
    say "创建 Python 虚拟环境 $VENV …"
    python3 -m venv "$VENV" 2>/dev/null || warn "venv 创建失败，AKShare/X 渠道将降级为会话内提示安装"
  fi
  if [[ -x "$VENV/bin/python" ]]; then
    say "安装数据渠道依赖（akshare、playwright，约1-2分钟）…"
    "$VENV/bin/pip" install -q --upgrade pip 2>/dev/null || true
    "$VENV/bin/pip" install -q akshare playwright && say "依赖安装完成（$VENV）" \
      || warn "依赖安装失败：会话内使用时 AI 会提示重试，不影响其他功能"
  fi
else
  warn "未找到 python3：AKShare（A股新闻舆情）与 X 渠道不可用，其余功能正常。"
fi

# 4. 富途授权（在安装阶段完成，确保第一次会话就能用全部工具）
if [[ -s "$DSH_HOME/futu-token" ]]; then
  say "检测到已有富途 token，跳过授权（过期时可用 --refresh 续期）"
elif [[ -x "$VENV/bin/python" ]]; then
  say "发起富途授权（浏览器将打开授权页，登录并确认即可）…"
  "$VENV/bin/python" "$PRESET_DST/scripts/futu_auth.py" || warn "授权未完成：第一个会话会再次提示，或稍后重跑本脚本"
else
  warn "无 python 环境，跳过授权。第一个会话内会提示如何授权。"
fi

# 5. 统一金融数据插件（fin_news/fin_sentiment 工具）——tarball 安装规避 pnpm 缓存坑
if command -v dsh >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  say "安装统一金融数据插件（fin_news/fin_sentiment）…"
  TGZ="$( (cd "$PRESET_DST/plugins/fin-data" && npm pack --pack-destination /tmp 2>/dev/null) | tail -1)"
  if [[ -n "$TGZ" ]] && [[ "$TGZ" == /* ]] && dsh plugin --profile web add "$TGZ" 2>/dev/null; then
    sed -i '/^- id: fin-data$/,/^  disabled: true$/ s/^  disabled: true$//' "$PRESET_DST/agent.cordis.yml"
    say "fin-data 插件已安装并启用（重启 harness 后新会话生效）"
  else
    warn "fin-data 安装失败（跳过）。快讯/舆情仍可用脚本手动方式；重跑本脚本可重试。"
  fi
fi

# 5b. 投研确定性引擎（run_trading_analysis 工具）
if command -v dsh >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  say "安装投研确定性引擎…"
  TGZ="$( (cd "$PRESET_DST/plugins/engine" && npm pack --pack-destination /tmp 2>/dev/null) | tail -1)"
  if [[ -n "$TGZ" ]] && [[ "$TGZ" == /* ]] && dsh plugin --profile web add "$TGZ" 2>/dev/null; then
    sed -i '/^- id: trading-engine$/,/^  disabled: true$/ s/^  disabled: true$//' "$PRESET_DST/agent.cordis.yml"
    say "trading-engine 已安装并启用（重启 harness 后新会话生效）"
  else
    warn "trading-engine 安装失败（跳过），仍可用 skill 驱动方式。"
  fi
fi

# 6. 账户模式开关默认 sim（安全），文件不存在时补齐
[[ -s "$DSH_HOME/trading-account-mode" ]] || printf 'sim\n' > "$DSH_HOME/trading-account-mode"

say "启动：dsh web → 新建会话 → 选择「交易智囊模式」→ 说「分析一下 00700.HK」"
