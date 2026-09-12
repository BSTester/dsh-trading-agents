# dsh-trading-agents Windows 安装器（PowerShell 5+）
# 用法：
#   git clone https://github.com/BSTester/dsh-trading-agents.git
#   cd dsh-trading-agents
#   .\install.ps1
$ErrorActionPreference = "Stop"

$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE ".dsh" }
$dshHome = [System.IO.Path]::GetFullPath($dshHome)
$env:DSH_HOME = $dshHome
$presetId = "dsh-trading-agents"
$presetDst = Join-Path $dshHome ".agent-presets\$presetId"

function Say($m) { Write-Host "==> $m" }
function Warn($m) { Write-Host " == $m" -ForegroundColor Yellow }

Say "安装 dsh-trading-agents 对话模式"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "未找到 git，请先安装 Git for Windows。"; exit 1
}
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) { Warn "完整安装需要 Python 3，请先安装后重试。"; exit 1 }
$python = $py.Source

# 安装或更新（preset 发现是即时的，无需重启 harness）
if (Test-Path (Join-Path $presetDst ".git")) {
    Say "已安装，更新到最新版本…"
    $helper = Join-Path $presetDst "scripts\install_plugins.py"
    if (Test-Path $helper) {
        & $python $helper update --repo $presetDst
    } else {
        git -C $presetDst pull --ff-only
    }
    if ($LASTEXITCODE -ne 0) {
        Warn "更新失败：请先提交或 stash 本地修改后重试；安装器不会丢弃修改。"; exit 1
    }
} else {
    Say "安装到 $presetDst …"
    New-Item -ItemType Directory -Force -Path (Split-Path $presetDst) | Out-Null
    git clone --depth 1 https://github.com/BSTester/dsh-trading-agents.git $presetDst
    if ($LASTEXITCODE -ne 0) { Warn "git clone 失败。"; exit 1 }
}
Say "安装 web profile 工作台 Host 和投研/数据插件…"
& $python (Join-Path $presetDst "scripts\install_plugins.py") install --repo $presetDst --dsh-home $dshHome
if ($LASTEXITCODE -ne 0) { Warn "必需插件安装失败，请修复错误后重试。"; exit 1 }

# 环境初始化：持久 venv + 数据渠道依赖（AKShare/playwright）
$venv = Join-Path $dshHome "trading-venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if ($py) {
    if (-not (Test-Path $venvPython)) {
        Say "创建 Python 虚拟环境 $venv …"
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { Warn "venv 创建失败，AKShare/X 渠道不可用。" }
    }
    if (Test-Path $venvPython) {
        Say "安装数据渠道依赖（akshare、playwright，约1-2分钟）…"
        & $venvPython -m pip install -q --upgrade pip
        if ($LASTEXITCODE -ne 0) { Warn "pip 更新失败，继续使用现有版本。" }
        & $venvPython -m pip install -q akshare playwright
        if ($LASTEXITCODE -eq 0) { Say "依赖安装完成（$venv）" }
        else { Warn "依赖安装失败：会话内使用时 AI 会提示重试，不影响其他功能" }
    }
} else {
    Warn "未找到 python：AKShare（A股新闻舆情）与 X 渠道不可用，其余功能正常。"
}

# 富途授权（token 已存在则跳过；过期时可用 --refresh 续期）
$tokenFile = Join-Path $dshHome "futu-token"
if (Test-Path $tokenFile) {
    Say "检测到已有富途 token，跳过授权"
} else {
    Say "发起富途授权（浏览器将打开授权页，登录并确认即可）…"
    $authPython = if (Test-Path $venvPython) { $venvPython } else { $python }
    & $authPython (Join-Path $presetDst "scripts\futu_auth.py")
    if ($LASTEXITCODE -ne 0) { Warn "授权未完成：第一个会话会再次提示，或稍后重跑 .\install.ps1" }
}

# 缺少模式文件时运行时默认 sim；安装器不覆盖模式或修复非法状态。

Say "重启 dsh web → 新建会话 → 选择「交易智囊模式」→ 说「分析一下 00700.HK」"
