#!/usr/bin/env bash
# Ant Design Pro 工作台逐页验收：http://127.0.0.1:8397/pro/#/<key>
#
# 断言（与设计稿版的 verify_v3_pages.sh 同口径，保证两版都能被机械验收）：
#   1) 9 个路由都能渲染（非白屏、无运行时崩溃）；
#   2) DOM 无「示例」字样；
#   3) **设计稿源文件里的数字型占位串不再出现**（从 web/public/v3/<page>.html 提取，自动跟随设计稿）；
#   4) 每页的关键模块标题命中（与设计稿版功能一一对应的证据）；
#   5) 分组菜单（监控/研究/交易/系统）在页面内可见。
#      bash platform/tools/verify_pro_pages.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:8397}"
SRC="$(cd "$(dirname "$0")/../web/public/v3" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 路由 key → 设计稿页文件名（同一套功能）
ROUTES="overview:index brain:brain market:market strategy:strategy risk:risk execution:execution gateway:gateway tools:tools settings:settings"
for pair in $ROUTES; do
  key="${pair%%:*}"
  timeout 240 chromium --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
    --virtual-time-budget=60000 --window-size=1920,2400 \
    --screenshot="$TMP/$key.png" --dump-dom "$BASE/pro/#/$key" > "$TMP/$key.html" 2>/dev/null
done

python3 - "$TMP" "$SRC" <<'PY'
import pathlib, re, sys

tmp, src = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
PAIRS = [('overview','index'),('brain','brain'),('market','market'),('strategy','strategy'),
         ('risk','risk'),('execution','execution'),('gateway','gateway'),('tools','tools'),
         ('settings','settings')]
# 与设计稿版一致的占位比对规则（阈值类同值整数不参与）
ALLOW = {'2%', '15%', '20%', '5', '3', '100%', '0%'}
# 与真实数据碰撞的设计稿数字（在 verify_v3_pages.sh 中已逐条人工核对）
#   strategy 0.38 / 0.42 → 真实数据碰撞：因子 z 值 -0.384 的子串、真实参数网格夏普 -0.38 / -0.42
COLLIDE = {'25.4%', '0.25', '0.58', '1.00', '1.24', '1.24%', '0.71', '2.4%', '0.8%', '99.2%',
           '0.38', '0.42'}
# 每页必须出现的关键模块标题（功能一一对应的机械证据）
REQUIRED = {
  # 「三通道」在设计稿版与工作台都以三张卡（MCP Bridge / SDK JSON-RPC / Headless CLI）呈现，
  # 页面上没有「三通道」这个字面词，故按真实卡片标题断言。
  'overview': ['总资产', 'MCP Bridge', 'SDK JSON-RPC', 'Headless CLI', '决策链路', '风控红线', 'Agent Loop', '审批', '数据源健康'],
  'brain': ['决策', '推理', '决策结论', '引用证据', '运行元信息', '工具调用'],
  'market': ['K 线', '盘口', '自选', '多因子', '板块'],
  'strategy': ['流水线', '提案', '因子', '扫描', '回测'],
  'risk': ['VaR', 'CVaR', 'Beta', 'Alpha', 'IR', '事前', '事中', '事后', '阻断'],
  'execution': ['生命周期', '审批', '订单', '持仓', '决策链路', '冻结计划', '待确认'],
  'gateway': ['MCP', 'SDK', 'Headless', '调度', '熔断'],
  'tools': ['工具', '域', '发现代理', '注册'],
  'settings': ['交易模式', '富途', '授权', '环境变量', '自动流水线'],
}
GROUPS = ['监控', '研究', '交易', '系统']


def placeholders(page_html: str):
    text = re.sub(r'<script.*?</script>', ' ', page_html, flags=re.S)
    text = re.sub(r'<style.*?</style>', ' ', text, flags=re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    found = set()
    for token in re.findall(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', text):
        found.add(token)
    for token in re.findall(r'\d+\.\d+%', text):
        found.add(token)
    for token in re.findall(r'(?<![\d.])\d\.\d{2}(?![\d])', text):
        found.add(token)
    return {t for t in found if t not in ALLOW and t not in COLLIDE}


fail = 0
for key, design in PAIRS:
    dom = tmp / f'{key}.html'
    if not dom.exists():
        print(f'{key:10s} ✗ DOM 未取到'); fail += 1; continue
    raw = dom.read_text(encoding='utf-8', errors='ignore')
    visible = re.sub(r'<(style|script)\b.*?</\1>', ' ', raw, flags=re.S | re.I)
    text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', visible))
    ex = text.count('示例')
    crash = any(k in text for k in ('Something went wrong', 'Objects are not valid as a React child'))
    blank = len(text) < 300
    missing = [k for k in REQUIRED[key] if k not in text]

    def present(token: str) -> bool:
        return re.search(r'(?<![\d.,])' + re.escape(token) + r'(?![\d])', text) is not None

    design_file = src / f'{design}.html'
    left = sorted(t for t in placeholders(design_file.read_text(encoding='utf-8')) if present(t)) if design_file.exists() else []
    groups = sum(1 for g in GROUPS if g in text)
    bad = ex or crash or blank or left or missing or groups < 4
    if bad:
        fail += 1
    print(f"{key:10s} {'✗' if bad else '✓'} 示例={ex} 残留占位={left[:3]} 缺模块={missing} "
          f"菜单分组={groups}/4 崩溃={crash} 白屏={blank} 文本={len(text)}")
print()
print('结论:', '全部通过' if fail == 0 else f'{fail} 页需修')
sys.exit(1 if fail else 0)
PY
