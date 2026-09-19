#!/usr/bin/env bash
# 设计稿保真度校验：服务的 /v3/<page>.html 与设计稿源文件是否**结构完全一致**。
# 做法：取服务返回的 HTML，去掉我们注入的 <script src="shared.js|*.js"> 两行，与设计稿源文件逐字节比较。
#   bash platform/tools/compare_with_design.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:8397}"
SRC="$(cd "$(dirname "$0")/../web/public/v3" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
for p in index brain market strategy risk execution gateway tools settings; do
  curl -s -m 30 -o "$TMP/$p.served.html" "$BASE/v3/$p.html"
  # 两边都去掉注入的两行脚本（我们唯一的改动点），再逐字节比较
  python3 - "$TMP/$p.served.html" "$TMP/$p.clean.html" "$SRC/$p.html" "$TMP/$p.src.html" <<'PY'
import re, sys
pat = [r'<script src="shared\.js"></script>\s*\n?', r'<script src="[a-z]+\.js" defer></script>\s*\n?']
def strip(src, dst):
    text = open(src, encoding='utf-8').read()
    for pattern in pat:
        text = re.sub(pattern, '', text)
    open(dst, 'w', encoding='utf-8').write(text)
strip(sys.argv[1], sys.argv[2])
strip(sys.argv[3], sys.argv[4])
PY
  if diff -q "$TMP/$p.src.html" "$TMP/$p.clean.html" >/dev/null; then
    echo "$(printf '%-10s' $p) ✓ 与设计稿逐字节一致（仅多两行注入脚本）"
  else
    echo "$(printf '%-10s' $p) ✗ 与设计稿存在差异："
    diff "$SRC/$p.html" "$TMP/$p.clean.html" | head -6 | sed 's/^/      /'
    fail=$((fail+1))
  fi
done
echo
echo "结论: $([ $fail -eq 0 ] && echo '9 页与设计稿完全一致' || echo "$fail 页结构被改动")"
exit $([ $fail -eq 0 ] && echo 0 || echo 1)
