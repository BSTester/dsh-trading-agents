# dsh-trading-agents Windows 安装器（PowerShell 5+）
# 用法：
#   git clone https://github.com/BSTester/dsh-trading-agents.git
#   cd dsh-trading-agents
#   .\install.ps1
$ErrorActionPreference = "Stop"

$repoDir = $PSScriptRoot
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE ".dsh" }
$presetId = "dsh-trading-agents"
$presetDst = Join-Path $dshHome ".agent-presets\$presetId"

function Say($m) { Write-Host "==> $m" }
function Warn($m) { Write-Host " == $m" -ForegroundColor Yellow }

Say "安装 dsh-trading-agents 对话模式"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "未找到 git，请先安装 Git for Windows。"; exit 1
}

# 安装或更新（preset 发现是即时的，无需重启 harness）
if (Test-Path (Join-Path $presetDst ".git")) {
    Say "已安装，更新到最新版本…"
    git -C $presetDst pull --ff-only
} else {
    Say "安装到 $presetDst …"
    New-Item -ItemType Directory -Force -Path (Split-Path $presetDst) | Out-Null
    git clone --depth 1 https://github.com/BSTester/dsh-trading-agents.git $presetDst
}
Say "完成。DeepSeek Harness 会自动发现新 preset（无需重启）。"

# 环境初始化：持久 venv + 数据渠道依赖（AKShare/playwright）
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
$venv = Join-Path $dshHome "trading-venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if ($py) {
    if (-not (Test-Path $venvPython)) {
        Say "创建 Python 虚拟环境 $venv …"
        & $py.Source -m venv $venv
    }
    if (Test-Path $venvPython) {
        Say "安装数据渠道依赖（akshare、playwright，约1-2分钟）…"
        & $venvPython -m pip install -q --upgrade pip
        & $venvPython -m pip install -q akshare playwright
        if ($LASTEXITCODE -eq 0) { Say "依赖安装完成（$venv）" }
        else { Warn "依赖安装失败：会话内使用时 AI 会提示重试，不影响其他功能" }
    }
} else {
    Warn "未找到 python：AKShare（A股新闻舆情）与 X 渠道不可用，其余功能正常。"
}

# 富途 token（可选）
if (-not $env:FUTU_MCP_TOKEN -and -not (Test-Path (Join-Path $dshHome "futu-token"))) {
    Warn "未检测到富途 token。"
    $ans = Read-Host "现在运行授权向导吗？会打开浏览器进行富途授权 (y/N)"
    if ($ans -match '^[Yy]') {
        python (Join-Path $presetDst "scripts\futu_auth.py")
    } else {
        Warn "跳过。之后可在会话内让 AI 运行 scripts\futu_auth.py，或手动设置 FUTU_MCP_TOKEN。"
    }
}

Say "启动：dsh web → 新建会话 → 选择「交易智囊模式」→ 说「分析一下 00700.HK」"
