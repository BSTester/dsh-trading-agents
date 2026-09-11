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

# 富途 token（可选；不设置则行情/交易工具不可用，会话内会提示授权，其余功能正常）
if [[ -z "${FUTU_MCP_TOKEN:-}" ]]; then
  warn "未检测到 FUTU_MCP_TOKEN（富途远程 MCP 的 Bearer token）。"
  echo "   授权步骤见仓库 README「获取富途 token」（OAuth + PKCE，建议先只授只读 scope）。"
  read -r -p "   现在输入 token（留空跳过，之后可在会话内按提示授权）: " TOKEN || true
  if [[ -n "${TOKEN:-}" ]]; then
    for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
      if [[ -f "$RC" ]] && ! grep -q "FUTU_MCP_TOKEN" "$RC"; then
        printf '\nexport FUTU_MCP_TOKEN="%s"\n' "$TOKEN" >> "$RC"
        say "已写入 $RC（新开的终端生效）"
      fi
    done
  else
    warn "跳过。首次会话会提示如何授权。"
  fi
fi

say "启动：dsh web → 新建会话 → 选择「交易智囊模式」→ 说「分析一下 00700.HK」"
