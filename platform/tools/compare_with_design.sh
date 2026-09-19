#!/usr/bin/env bash
# 设计稿 vs 实现：逐页对照区块清单（对齐证据）。
#   bash tools/compare_with_design.sh [base-url]
# 设计稿来源：OpenDesign 容器内 od-quant-harness-platform-20260919/<page>.html
# 实现来源：FastAPI 服务的 AntD Pro 页面 http://127.0.0.1:8397/#/v3-<page>
set -u
BASE="${1:-http://127.0.0.1:8397}"
PROJECT="od-quant-harness-platform-20260919"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for p in index brain market strategy risk execution gateway tools settings; do
  v3="$p"; [ "$p" = "index" ] && v3="overview"
  docker exec open-design cat "/app/.od/projects/$PROJECT/$p.html" > "$TMP/design-$p.html" 2>/dev/null
  timeout 120 chromium --headless=new --no-sandbox --disable-gpu --virtual-time-budget=30000 \
    --dump-dom "$BASE/#/v3-$v3" > "$TMP/impl-$p.html" 2>/dev/null
done

python3 - "$TMP" <<'PY'
import html as htmlmod, pathlib, re, sys
tmp = pathlib.Path(sys.argv[1])
PAIRS = [('index','overview'),('brain','brain'),('market','market'),('strategy','strategy'),
         ('risk','risk'),('execution','execution'),('gateway','gateway'),('tools','tools'),
         ('settings','settings')]

def text_of(path):
    if not path.exists():
        return ''
    raw = path.read_text(encoding='utf-8', errors='ignore')
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', raw)).strip()

def design_blocks(raw_html):
    """设计稿的区块清单：注释标记 <!-- n) 标题 --> 与卡片标题 <h3>。"""
    blocks = re.findall(r'<!--\s*\d*\)?\s*([^->]{2,40}?)\s*-->', raw_html)
    blocks += [re.sub(r'<[^>]+>', '', b).strip() for b in re.findall(r'<h3[^>]*>(.*?)</h3>', raw_html, re.S)]
    seen, out = set(), []
    for b in blocks:
        b = b.strip()
        if not b or b in seen or len(b) > 24:
            continue
        seen.add(b); out.append(b)
    return out

total = matched = 0
for design_key, impl_key in PAIRS:
    design_raw = (tmp / f'design-{design_key}.html').read_text(encoding='utf-8', errors='ignore')
    impl_text = text_of(tmp / f'impl-{design_key}.html')
    blocks = design_blocks(design_raw)
    hits, miss = [], []
    for b in blocks:
        # 区块名可能被拆词（如「决策链路时间线」），取前 4 字做宽松匹配
        probe = b[:4]
        (hits if probe and probe in impl_text else miss).append(b)
    total += len(blocks); matched += len(hits)
    print(f'== {design_key}（设计稿 {len(blocks)} 区块 → 实现 {len(hits)} 命中）')
    if hits:
        print('   ✓ 已对齐：', '、'.join(hits[:8]))
    if miss:
        print('   ? 未按名命中：', '、'.join(miss[:8]), '（可能已合并进其它卡片，或为无数据源区块）')
    print(f'   实现页文本长度：{len(impl_text)}')
print()
print(f'合计：设计稿区块 {total} 个，名称命中 {matched} 个（命中率 {0 if total == 0 else round(matched / total * 100)}%）')
print('说明：名称是宽松匹配，最终以 tools/verify_v3_pages.sh 的逐页断言与截图为准。')
PY
