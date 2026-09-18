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
    # 3.1 浏览器二进制（2026-09-18 补）：`pip install playwright` **只装库、不下载 Chromium**，
    # 而情绪/资讯采集（fin-data）与前端真浏览器 E2E 都要它——此前 README 的一键安装没有这一步，
    # 装完会出现「库在、浏览器不在」的运行期降级。幂等：已存在时秒回。
    if [[ -x "$VENV/bin/python" ]]; then
      "$VENV/bin/python" -m playwright install chromium >/dev/null 2>&1 \
        && say "Playwright Chromium 就绪" \
        || warn "Playwright Chromium 未就绪：情绪/资讯采集将降级（可手动运行 $VENV/bin/playwright install chromium）"
    fi
  fi
else
  warn "未找到 python3：AKShare（A股新闻舆情）与 X 渠道不可用，其余功能正常。"
fi

# 3.5 数据层链接：venv 建好后才写 .pth，让插件脚本能直接 import trading_datasource
if [[ -x "$VENV/bin/python" ]]; then
  "$PYTHON" "$PRESET_DST/scripts/install_plugins.py" link --repo "$PRESET_DST" --dsh-home "$DSH_HOME" \
    || warn "数据层链接失败：行情与回测工具可能不可用，可重跑 install.sh"
fi

# 3.6 平台服务依赖 + 前端构建（2026-09-18 补）：工作台服务是**常驻件不是可选件**，而它的
# 依赖（platform/requirements.txt：fastapi/uvicorn/mcp/httpx/websockets/cryptography）与
# 前端 dist 都不在上面几步里——此前只有末尾一行「手动运行 install_platform.py」的提示，
# 一键安装实际上装不出工作台。这里用 `--skip-service` **只装依赖与构建、不拉起常驻进程**：
# 起不起服务（端口/常驻）仍由用户决定，与第 5 步的分工不变。
if [[ -x "$VENV/bin/python" && -f "$PRESET_DST/scripts/install_platform.py" ]]; then
  say "安装平台服务依赖与前端（fastapi/uvicorn/mcp + npm build，约1-3分钟）…"
  "$VENV/bin/python" "$PRESET_DST/scripts/install_platform.py" --home "$DSH_HOME" --skip-service \
    || warn "平台依赖/前端未装完：工作台服务可能起不来，可重跑本脚本或单独运行 scripts/install_platform.py --home \"$DSH_HOME\""
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

# 5. 自检（2026-09-18 补）：装完必须能自己回答「装全了没有」。此前只有方式 C 的提示词里
#    让 AI 手动跑 check，方式 B 走完就结束了——缺统一 Python 层的表现是一句
#    ModuleNotFoundError，很难一眼看出该做什么。这里把自检收进安装器本身。
say "安装自检…"
if "$PYTHON" "$PRESET_DST/scripts/install_plugins.py" check --repo "$PRESET_DST" --dsh-home "$DSH_HOME"; then
  say "自检通过（✅ 安装完整）"
else
  warn "自检未通过：按上面的问题与修复建议处理后重跑 ./install.sh"
fi

say "重启 dsh web → 新建会话 → 选择「交易智囊模式」→ 说「分析一下 00700.HK」"

# 6. 量化平台独立服务（工作台 Web + quantwb MCP 工具面）：**依赖已由第 3.6 步装好**
#    （平台服务是常驻件，不是可选件），这里只把「何时拉起常驻进程」交给用户。
say "启动工作台服务（常驻进程，时机由你决定）"
warn "脚本方式：scripts/platform_service.sh start（随后 status 看健康）"
warn "或用 install/HARNESS_SETUP.md ① 的提示词交给新的 Harness 会话（含首启关注池配置）"

