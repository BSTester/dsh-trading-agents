#!/usr/bin/env bash
# V3 控制台（Ant Design Pro）逐页验收：http://127.0.0.1:8397/#/v3-<page>
# 断言：1) 页面渲染出真实数据（关键值命中）；2) 无「示例」字样；3) 无设计稿占位数字；
#       4) 无错误 Alert / 白屏；5) 记录文本长度与截图。
#   bash tools/verify_v3_pages.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:8397}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PAGES="overview brain market strategy risk execution gateway tools settings"
for p in $PAGES; do
  # 重页（行情：K线+自选+板块+因子矩阵）需要更长预算；统一 60s 避免误报白屏
  timeout 240 chromium --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
    --virtual-time-budget=60000 --window-size=1920,2200 \
    --screenshot="$TMP/$p.png" --dump-dom "$BASE/#/v3-$p" > "$TMP/$p.html" 2>/dev/null
done

python3 - "$TMP" <<'PY'
import re, sys, pathlib
tmp = pathlib.Path(sys.argv[1])
PAGES = 'overview brain market strategy risk execution gateway tools settings'.split()
# 设计稿里的占位数字/标识（必须全部消失）
# 注意：PLN-2026 是**真实**计划号前缀（PLN-20260918-SIM-12FB），不作为占位哨兵；
# 8****@futu 已统一改写为「账号掩码」，故也不再作为哨兵。
SENTINELS = ['1,024,380','¥1,030,771','18.4%','99.2%','1,284','1.8 s','11 / 1','300750.SZ',
             'EX20260919','4,182','12,480','0.8%','宁德时代','¥56,900','12,480 tokens']
# 每页必须出现的真实数据/区块特征
REQUIRED = {
  'overview': ['系统概览','台账权益','三通道状态','数据源健康'],
  'brain': ['决策大脑','推理流','决策结论'],
  'market': ['行情','K 线','自选'],
  'strategy': ['策略','流水线','因子'],
  'risk': ['风险','VaR','CVaR'],
  'execution': ['执行','订单','台账'],
  'gateway': ['网关','MCP','调度'],
  'tools': ['工具','域'],
  'settings': ['授权','环境变量','富途'],
}
fail = 0
for p in PAGES:
    f = tmp / f'{p}.html'
    if not f.exists():
        print(f'{p:10s} ✗ DOM 未取到'); fail += 1; continue
    raw = f.read_text(encoding='utf-8', errors='ignore')
    text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', raw))
    ex = text.count('示例')
    left = [s for s in SENTINELS if s in text]
    missing = [k for k in REQUIRED[p] if k not in text]
    # AntD 错误态：页面里出现「取数失败 / 加载失败」且无数据
    crash = any(k in text for k in ('Something went wrong', 'Objects are not valid as a React child',
                                    'is not a function', 'Cannot read propert', 'Minified React error'))
    err = ('取数失败' in text) or ('Loading' == text.strip()) or crash
    blank = len(text) < 300
    bad = ex or left or missing or err or blank
    if bad: fail += 1
    print(f"{p:10s} {'✗' if bad else '✓'} 示例={ex} 残留占位={left[:3]} 缺特征={missing} 错误态={err}/崩溃={crash} 白屏={blank} 文本长度={len(text)}")
print()
print('截图目录:', tmp)
print('结论:', '全部通过' if fail == 0 else f'{fail} 页需修')
sys.exit(1 if fail else 0)
PY
