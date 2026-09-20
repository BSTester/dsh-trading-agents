#!/usr/bin/env bash
# V3 工作台（Ant Design Pro，挂在根路径）逐页验收：http://127.0.0.1:8397/#/<key>
#
# 断言（与设计稿版的 verify_v3_pages.sh 同口径，保证两版都能被机械验收）：
#   1) 9 个路由都能渲染（非白屏、无运行时崩溃）；
#   2) DOM 无「示例」字样；
#   3) 设计稿里的数字型占位串不再出现（清单静态固化：原设计稿已删除，数值取自其历史 HTML）；
#   4) 每页的关键模块标题命中（与设计稿版功能一一对应的证据）；
#   5) 分组菜单（监控/研究/交易/系统）在页面内可见。
#      bash platform/tools/verify_pro_pages.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:8397}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 路由 key → 设计稿页文件名（同一套功能）
ROUTES="overview:index brain:brain market:market strategy:strategy research:research risk:risk execution:execution gateway:gateway tools:tools settings:settings"
for pair in $ROUTES; do
  key="${pair%%:*}"
  timeout 240 chromium --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
    --virtual-time-budget=60000 --window-size=1920,2400 \
    --screenshot="$TMP/$key.png" --dump-dom "$BASE/#/$key" > "$TMP/$key.html" 2>/dev/null
done

python3 - "$TMP" <<'PY'
import pathlib, re, sys

tmp = pathlib.Path(sys.argv[1])
PAIRS = [('overview','index'),('brain','brain'),('market','market'),('strategy','strategy'),
         ('research','research'),('risk','risk'),('execution','execution'),('gateway','gateway'),
         ('tools','tools'),('settings','settings')]
# 与**真实数据**碰撞、已逐条人工取证的设计稿数字（登记后不再作为硬哨兵）：
#   0.8%/99.2% 真实成功率与失败率；0.25 真实信号阈值与 z 值；0.58/1.00/1.24/1.38/0.42 真实网格夏普或
#   真实风控数值；0.71 真实手续费 190.71 子串；25.4%/2.4% 页面真实文案；0.92 market 真实因子 z 值
#   （+0.92 / 0.921 子串）与 strategy 真实网格夏普（-0.92）
COLLIDE = {'25.4%', '2.4%', '0.25', '0.58', '1.00', '1.24', '1.24%', '0.71', '0.8%', '99.2%',
           '0.38', '0.42', '0.92'}
# 设计稿里的数字型占位串（原 platform/web/public/v3/*.html 已随该版本删除，此处静态固化）
DESIGN_SENTINELS = [
    '1,024,380', '1,030,771', '18.4%', '1.86', '−6.3%', '99.2%', '1,284', '1.8 s', '11 / 1',
    '12,480', '49,152', '4,182', '¥26,750', '¥18,420', '300750.SZ', 'EX20260919',
    '¥56,900', '¥2,845,600', '¥2,845,312', '¥12,847,532', '8****@futu', '12:14:02', '14:32:05',
    'turn 3/12', '2025-06-13', '2026-09-12', 'PLN-20260919-0830', 'mining-2026-09-19',
]
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
  'research': ['研报', '研究 run', '量化预览', '活动流', '值勤队列'],
  'gateway': ['MCP', 'SDK', 'Headless', '调度', '熔断'],
  'tools': ['工具', '域', '发现代理', '注册'],
  'settings': ['交易模式', '富途', '授权', '环境变量', '自动流水线'],
}
GROUPS = ['监控', '研究', '交易', '系统']
# 市场过滤不变量：页头市场选择器（A股/港股/美股）必须出现在**每一页**，
# 否则说明该页没有接入统一市场上下文（无法按市场区分）。
MARKET_KEYS = ['A股', '港股', '美股']


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

    left = sorted(t for t in DESIGN_SENTINELS if t not in COLLIDE and present(t))
    groups = sum(1 for g in GROUPS if g in text)
    market_missing = [k for k in MARKET_KEYS if k not in text]
    bad = ex or crash or blank or left or missing or groups < 4 or market_missing
    if bad:
        fail += 1
    print(f"{key:10s} {'✗' if bad else '✓'} 示例={ex} 残留占位={left[:3]} 缺模块={missing} "
          f"菜单分组={groups}/4 市场选择器={'有' if not market_missing else '缺' + str(market_missing)} "
          f"崩溃={crash} 白屏={blank} 文本={len(text)}")
print()
print('结论:', '全部通过' if fail == 0 else f'{fail} 页需修')
sys.exit(1 if fail else 0)
PY
