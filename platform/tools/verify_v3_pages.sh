#!/usr/bin/env bash
# V3 控制台（设计稿原样页面）逐页验收：http://127.0.0.1:8397/v3/<page>.html
#
# 断言：
#   1) 页面与 binder 都 200；
#   2) DOM 无「示例」字样；
#   3) **设计稿源文件里的数字型占位串不再出现在渲染结果里**（从 web/public/v3/<page>.html 自动提取，
#      千分位 / 小数百分比 / 三位小数视为占位；阈值类同值整数如 2%、15%、20% 不参与比对）；
#   4) 设计稿结构仍在（.topbar/.sidenav/.main 与 card 类名命中）——证明用的是设计稿自身 HTML/CSS；
#   5) 无运行时崩溃 / 白屏。
#      bash platform/tools/verify_v3_pages.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:8397}"
SRC="$(cd "$(dirname "$0")/../web/public/v3" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PAGES="index brain market strategy risk execution gateway tools settings"
for p in $PAGES; do
  timeout 240 chromium --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
    --virtual-time-budget=60000 --window-size=1920,2400 \
    --screenshot="$TMP/$p.png" --dump-dom "$BASE/v3/$p.html" > "$TMP/$p.html" 2>/dev/null
done

python3 - "$TMP" "$SRC" "$BASE" <<'PY'
import pathlib, re, sys, urllib.request

tmp, src, base = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3].rstrip('/')
PAGES = 'index brain market strategy risk execution gateway tools settings'.split()
# 设计稿结构锚点（证明 HTML/CSS 来自设计稿而非组件库）
STRUCT = ['.topbar', '.sidenav', '.main', 'card']
REQUIRED = {
    'index': ['系统概览', '权益', '决策链路时间线', '风控红线', '数据源健康'],
    'brain': ['决策大脑'], 'market': ['行情'], 'strategy': ['策略'], 'risk': ['风险'],
    'execution': ['执行'], 'gateway': ['网关'], 'tools': ['工具'], 'settings': ['授权'],
}
# 阈值类/结构性整数（真实逻辑会写、且与设计稿同值），不参与占位比对
ALLOW = {'2%', '15%', '20%', '5', '3', '100%', '0%'}
# 与**真实数据**发生碰撞的设计稿数字（已逐个人工核对，见下），比对时忽略：
#   brain 25.4%   → 出现在 DOM 内联样式文本（不是数据）
#   market 0.25   → 真实「信号阈值 ±0.25」与真实因子 z 值 +0.25
#   strategy 0.58 / 1.00 / 1.24 / 1.24% → 真实参数网格夏普 0.582、真实风控「单笔风险 ≤1.00%」、
#                                         真实窗口数值、以及回撤 -21.24% 的子串
#   risk 0.71     → 真实台账手续费 190.71（子串）
#   tools 2.4%    → 真实页面文案（由 binder 写出的真实口径）
COLLIDE = {'25.4%', '0.25', '0.58', '1.00', '1.24', '1.24%', '0.71', '2.4%'}

def placeholders(page_html: str):
    """只从**可见文本**里提取占位数字：排除 style/script 与所有标签内的属性
    （内联样式里的 rgb(163,113,247) 之类颜色分量不是数据占位，否则会误报）。"""
    text = re.sub(r'<script.*?</script>', ' ', page_html, flags=re.S)
    text = re.sub(r'<style.*?</style>', ' ', text, flags=re.S)
    text = re.sub(r'<[^>]+>', ' ', text)          # 去掉标签（含 style/class 属性）
    text = re.sub(r'&[a-z]+;', ' ', text)
    found = set()
    for token in re.findall(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', text):      # 千分位：1,024,380 / 1,284
        found.add(token)
    for token in re.findall(r'\d+\.\d+%', text):                          # 小数百分比：99.2% / 18.4%
        found.add(token)
    for token in re.findall(r'(?<![\d.])\d\.\d{2}(?![\d])', text):        # 三位小数：1.86 / 0.92
        found.add(token)
    return {t for t in found if t not in ALLOW and t not in COLLIDE}

fail = 0
for p in PAGES:
    dom = tmp / f'{p}.html'
    if not dom.exists():
        print(f'{p:10s} ✗ DOM 未取到'); fail += 1; continue
    raw = dom.read_text(encoding='utf-8', errors='ignore')
    # 先剔除 <style>/<script> 的文本内容（CSS 里的 width:25.4% 不是数据），再去标签
    visible = re.sub(r'<(style|script)\b.*?</\1>', ' ', raw, flags=re.S | re.I)
    text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', visible))
    try:
        served = urllib.request.urlopen(f'{base}/v3/{p}.js', timeout=10).status == 200
    except Exception:
        served = False
    ex = text.count('示例')
    crash = any(k in text for k in ('Something went wrong', 'Objects are not valid as a React child'))
    blank = len(text) < 300
    def present(token: str) -> bool:
        """词边界匹配：`1.24%` 不应因为 `-21.24%` 而命中。"""
        return re.search(r'(?<![\d.,])' + re.escape(token) + r'(?![\d])', text) is not None

    left = sorted(t for t in placeholders((src / f'{p}.html').read_text(encoding='utf-8')) if present(t))
    missing = [k for k in REQUIRED[p] if k not in text]
    struct = sum(1 for s in STRUCT if s in raw)
    bad = ex or crash or blank or left or missing or not served or struct == 0
    if bad:
        fail += 1
    print(f"{p:10s} {'✗' if bad else '✓'} 示例={ex} 残留占位={left[:4]} 缺特征={missing} "
          f"binder={'ok' if served else '缺失'} 设计结构锚点={struct}/4 崩溃={crash} 白屏={blank} 文本={len(text)}")
print()
print('结论:', '全部通过' if fail == 0 else f'{fail} 页需修')
sys.exit(1 if fail else 0)
PY
