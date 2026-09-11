#!/usr/bin/env bash
# dsh-trading-agents 一键安装脚本
# 用法：
#   git clone https://github.com/BSTester/dsh-trading-agents.git
#   cd dsh-trading-agents && ./install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PRESET_ID="dsh-trading-agents"
PRESET_DST="$DSH_HOME/.agent-presets/$PRESET_ID"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m ==\033[0m %s\n' "$*"; }

say "安装 dsh-trading-agents 对话模式"

# 1. 检查 dsh CLI
if ! command -v dsh >/dev/null 2>&1; then
  warn "未找到 dsh CLI。请先安装 DeepSeek Harness，再运行本脚本。"
  exit 1
fi

# 2. 安装 preset（幂等：已存在则覆盖更新）
mkdir -p "$(dirname "$PRESET_DST")"
rm -rf "$PRESET_DST"
cp -R "$REPO_DIR/preset" "$PRESET_DST"
mkdir -p "$PRESET_DST/skills"
cp -R "$REPO_DIR/skills/." "$PRESET_DST/skills/"
say "preset 已安装到 $PRESET_DST"

# 3. 富途 MCP token（可选；不设置则行情/交易工具不可用，其余功能正常）
if [[ -z "${FUTU_MCP_TOKEN:-}" ]]; then
  warn "未检测到 FUTU_MCP_TOKEN（富途远程 MCP 的 Bearer token）。"
  echo "   获取方式：用富途账号完成 OAuth 授权后取得 token（见 README「获取富途 token」）。"
  read -r -p "   现在输入 token（留空跳过）: " TOKEN || true
  if [[ -n "${TOKEN:-}" ]]; then
    for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
      if [[ -f "$RC" ]] && ! grep -q "FUTU_MCP_TOKEN" "$RC"; then
        printf '\nexport FUTU_MCP_TOKEN="%s"\n' "$TOKEN" >> "$RC"
        say "已写入 $RC（新开的终端生效）"
      fi
    done
    export FUTU_MCP_TOKEN="$TOKEN"
  else
    warn "跳过。之后可手动 export FUTU_MCP_TOKEN=<token> 再启动。"
  fi
fi

say "安装完成！启动方式（任选其一）："
echo "    dsh web                     # 打开 Web 界面，会话选择「交易智囊模式」preset"
echo "    dsh --profile <你的profile> web"
echo ""
echo "验证：新会话里说「分析一下 00700.HK」即可跑通完整流水线。"
