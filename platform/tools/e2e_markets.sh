#!/usr/bin/env bash
# 三市场端到端验收：A股(SH) / 港股(HK) / 美股(US)
#
# 覆盖（全部只读）：
#   1) 行情：/api/v3/market 每个市场都要有真实 K 线（富途，带 source/as_of）
#   2) 财务：/api/v3/financials —— A股/港股走富途 f10_detail，美股走 SEC；失败要如实报错并给出 chain
#   3) 交易日历：/api/v3/markets/calendar 三市场的 session/isTradingDay/开盘收盘/午休/下次开市
#   4) 行业暴露：/api/v3/risk/industry（富途板块；取不到的标的必须进 missing）
#   5) 成交质量：/api/v3/execution/quality（分市场；成交类无开源替代，失败要如实）
#   6) 页面展示：/#/market（三市场切换 + 交易时段徽章）、/#/execution（成交质量）、/#/risk（行业暴露）
#      —— 断言无「示例」字样、关键模块命中
#
#   bash platform/tools/e2e_markets.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:8397}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== 1/2) 接口层（三市场）"
check() { # name url assertion-kind
  local name="$1" url="$2" expr="$3"
  printf '%-46s ' "$name"
  BODY="$(curl -s -m 120 "$url")" python3 - "$expr" <<'PYCHK'
import json, os, sys
expr = sys.argv[1]
try:
    data = json.loads(os.environ.get("BODY", ""))
except Exception:
    print("✗ 非 JSON"); raise SystemExit
ok = data.get("ok")
if expr == "kline":
    bars = ((data.get("data") or {}).get("bars")) or []
    print(("✓" if ok and bars else "✗"), f"bars={len(bars)} 最新={bars[-1].get('c') if bars else None}",
          "源=", (data.get("data") or {}).get("source"), "as_of=", (data.get("data") or {}).get("as_of"))
elif expr == "calendar":
    mk = data.get("markets") or {}
    print(("✓" if ok and len(mk) == 3 else "✗"),
          " ".join(f"{k}:{v.get('session')}/{v.get('open')}-{v.get('close')}" for k, v in mk.items()),
          "holidays_loaded=", data.get("holidays_loaded"))
elif expr == "financials":
    if ok:
        print("✓", "source=", data.get("source"), "lines=", len(data.get("lines") or []))
    else:
        chain = data.get("chain") or []
        print("✗(如实报错)", (data.get("error") or {}).get("code"),
              "| chain=", "; ".join(f"{c.get('source')}:{'ok' if c.get('ok') else 'fail'}" for c in chain) or "无")
elif expr == "industry":
    if ok:
        top = data.get("top") or {}
        print("✓", "行业数=", len(data.get("exposures") or []), "top=", top.get("industry"),
              top.get("weightPct"), "% breach=", data.get("breach"), "missing=", len(data.get("missing") or []))
    else:
        print("✗(如实报错)", (data.get("error") or {}).get("code"))
elif expr == "quality":
    if ok:
        m = data.get("metrics") or {}
        print("✓", f"orders={m.get('orders')} fillRate={m.get('fillRatePct')}% "
                   f"cancelRate={m.get('cancelRatePct')}% slip={m.get('avgSlippageBps')}bps")
    else:
        print("✗(如实报错)", (data.get("error") or {}).get("code"),
              str((data.get("error") or {}).get("message"))[:60])
elif expr == "chains":
    chains = data.get("chains") or []
    usable = sum(1 for c in chains if c.get("available"))
    print(("✓" if ok else "✗"), f"链={len(chains)} 可用={usable}",
          " · ".join(f"{c.get('key')}→{c.get('last_source') or '—'}" for c in chains[:4]))
PYCHK
}

for pair in "SH:SH.600000" "HK:HK.00700" "US:US.NVDA"; do
  market="${pair%%:*}"; code="${pair##*:}"
  check "[$market] 行情 K 线 $code" "$BASE/api/v3/market?ticker=$code&period=1d&limit=60" kline
done
check "[US] 财务（SEC）" "$BASE/api/v3/financials?ticker=AAPL&statement=income&periods=2" financials
check "[SH] 财务（富途 f10）" "$BASE/api/v3/financials?ticker=SH.600000&statement=income&periods=2" financials
check "[HK] 财务（富途 f10）" "$BASE/api/v3/financials?ticker=HK.00700&statement=income&periods=2" financials
check "交易日历（SH/HK/US）" "$BASE/api/v3/markets/calendar?markets=SH,HK,US" calendar
# 行业暴露：自选池全在 SH，HK/US 需传明确标的才能验证映射（空集返回 no-universe 是诚实行为，不是失败）
check "[SH] 行业暴露（自选池）" "$BASE/api/v3/risk/industry?market=SH" industry
check "[HK] 行业暴露（HK.00700）" "$BASE/api/v3/risk/industry?tickers=HK.00700" industry
check "[US] 行业暴露（US.NVDA）" "$BASE/api/v3/risk/industry?tickers=US.NVDA" industry
for market in SH HK US; do
  check "[$market] 成交质量" "$BASE/api/v3/execution/quality?market=$market&mode=sim" quality
done
check "降级链状态" "$BASE/api/v3/sources/status" chains

echo
echo "== 2) 市场过滤是否真的生效（逐市场断言：返回行必须属于该市场）"
BODY="" python3 - "$BASE" <<'PYFILTER'
import json, os, sys, urllib.request

base = sys.argv[1]
# 市场 → 合法标的前缀（A股含 SH/SZ/BJ；港股 HK.；美股 US.）
PREFIX = {"SH": ("SH.", "SZ.", "BJ."), "HK": ("HK.",), "US": ("US.",)}
CHECKS = [
    ("market/watchlist", "watchlist", "rows"),
    ("factors/matrix", "factors", "tickers"),
    ("risk/analytics", "analytics", None),
    ("execution", "execution", None),
    ("research", "research", None),
]
fail = 0
for market, prefixes in PREFIX.items():
    print(f"-- {market}")
    for path, label, kind in CHECKS:
        url = f"{base}/api/v3/{path}?{'n=3&' if path.endswith('watchlist') else ''}market={market}"
        try:
            with urllib.request.urlopen(url, timeout=180) as response:
                body = json.loads(response.read().decode("utf-8", "ignore"))
        except Exception as error:  # noqa: BLE001
            print(f"   {label:22s} ✗ 请求失败：{str(error)[:70]}")
            fail += 1
            continue
        if not body.get("ok"):
            code = (body.get("error") or {}).get("code")
            print(f"   {label:22s} ✓(如实报错) {code}")
            continue
        # 断言：返回的标的都属于该市场；不满足即为「传了参数没筛」
        bad = []
        if path == "market/watchlist":
            bad = [r.get("ticker") for r in (body.get("rows") or []) if not str(r.get("ticker", "")).startswith(prefixes)]
        elif path == "factors/matrix":
            bad = [t for t in ((body.get("matrix") or {}).get("tickers") or []) if not str(t).startswith(prefixes)]
        elif path == "risk/analytics":
            bad = [t for t in ((body.get("analytics") or {}).get("tickers") or {}) if not str(t).startswith(prefixes)]
        elif path == "execution":
            oms = body.get("oms") or {}
            bad = [o.get("ticker") for o in (oms.get("orders") or []) if not str(o.get("ticker", "")).startswith(prefixes)]
        elif path == "research":
            bad = [r.get("ticker") for r in ((body.get("runs") or []) + (body.get("reports") or [])) if not str(r.get("ticker", "") or "").startswith(prefixes)]
        if bad:
            print(f"   {label:22s} ✗ 过滤未生效：出现跨市场标的 {bad[:3]}")
            fail += 1
        else:
            echo = body.get("universe_source") or body.get("market") or ""
            print(f"   {label:22s} ✓ 仅含 {market} 标的（{str(echo)[:48]}）")
print()
print("过滤结论:", "全部生效" if fail == 0 else f"{fail} 项未生效/失败")
sys.exit(1 if fail else 0)
PYFILTER

echo
echo "== 3) 页面展示（真实浏览器）"
for page in market execution risk; do
  timeout 240 chromium --headless=new --no-sandbox --disable-gpu --virtual-time-budget=60000 \
    --dump-dom "$BASE/#/$page" > "$TMP/$page.html" 2>/dev/null
done
python3 - "$TMP" <<'PY'
import pathlib, re, sys
tmp = pathlib.Path(sys.argv[1])
REQUIRED = {
    "market": ["A股", "港股", "美股", "休市"],
    "execution": ["成交", "滑点", "成交率", "撤单率"],
    "risk": ["行业", "暴露", "集中度", "上限"],
}
fail = 0
for page, keys in REQUIRED.items():
    raw = (tmp / f"{page}.html").read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script)\b.*?</\1>", " ", raw, flags=re.S | re.I)))
    ex = text.count("示例")
    crash = any(k in text for k in ("Something went wrong", "Objects are not valid as a React child"))
    missing = [k for k in keys if k not in text]
    blank = len(text) < 300
    bad = ex or crash or blank or missing
    fail += 1 if bad else 0
    print(f"{page:10s} {'✗' if bad else '✓'} 示例={ex} 缺模块={missing} 崩溃={crash} 白屏={blank} 文本={len(text)}")
print()
print("结论:", "全部通过" if fail == 0 else f"{fail} 页需修")
sys.exit(1 if fail else 0)
PY
