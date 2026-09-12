#!/usr/bin/env bash
# dsh-trading-agents 安装/更新 + 富途 token 设置
#
# 官方安装方式其实只有一行（preset 目录 = 仓库根目录，放入即被发现生效）：
#   git clone https://github.com/BSTester/dsh-trading-agents "$HOME/.dsh/.agent-presets/dsh-trading-agents"
#
# 完整安装还包括 web profile Host、原生工具、数据依赖和富途授权。
set -euo pipefail

REPO_URL="https://github.com/BSTester/dsh-trading-agents.git"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PRESET_ID="dsh-trading-agents"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m ==\033[0m %s\n' "$*"; }

if ! command -v git >/dev/null 2>&1; then
  warn "未找到 git，请先安装 git。"; exit 1
fi
PYTHON="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON" ]]; then
  warn "完整安装需要 Python 3，请先安装后重试。"; exit 1
fi
export DSH_HOME
DSH_HOME="$("$PYTHON" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$DSH_HOME")"
PRESET_DST="$DSH_HOME/.agent-presets/$PRESET_ID"

# 安装或更新（preset 发现是即时的，无需重启 harness）
if [[ -d "$PRESET_DST/.git" ]]; then
  say "已安装，更新到最新版本…"
  if [[ -f "$PRESET_DST/scripts/install_plugins.py" ]]; then
    "$PYTHON" "$PRESET_DST/scripts/install_plugins.py" update --repo "$PRESET_DST"
  else
    git -C "$PRESET_DST" pull --ff-only || {
      warn "更新失败：请先提交或 stash 本地修改后重试；安装器不会丢弃修改。"; exit 1;
    }
  fi
else
  say "安装对话模式到 $PRESET_DST …"
  mkdir -p "$(dirname "$PRESET_DST")"
  git clone --depth 1 "$REPO_URL" "$PRESET_DST"
fi
say "安装 web profile 工作台 Host 和投研/数据插件…"
"$PYTHON" "$PRESET_DST/scripts/install_plugins.py" install --repo "$PRESET_DST" --dsh-home "$DSH_HOME"

# 3. 环境初始化：持久 venv + 数据渠道依赖（AKShare/playwright）
VENV="$DSH_HOME/trading-venv"
if [[ -n "$PYTHON" ]]; then
  if [[ ! -x "$VENV/bin/python" ]]; then
    say "创建 Python 虚拟环境 $VENV …"
    "$PYTHON" -m venv "$VENV" || warn "venv 创建失败，AKShare/X 渠道将降级为会话内提示安装"
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

# 缺少模式文件时运行时默认 sim；安装器不覆盖模式或修复非法状态。

say "重启 dsh web → 新建会话 → 选择「交易智囊模式」→ 说「分析一下 00700.HK」"
