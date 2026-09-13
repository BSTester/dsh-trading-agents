#!/usr/bin/env python3
"""券商真实持仓（富途）—— 按账户模式读取，带磁盘缓存。

两种模式走**不同**的账户体系，字段名也不同，不能混用：
  模拟盘(sim)  sim_trade_account_list → 每个市场一个账户
               sim_trade_position_list(acc_id, market)   ← 缺 market 会报 ret=-5
  实盘(live)   account_authorized_trd_accs → 真实交易账户
               account_positions(acc_id)

诚实性约束（面板可以缓存展示，但不能误导）：
  * **不跨账户/币种求和**：模拟盘响应里根本没有币种字段，且每个账户本身就是单一市场，
    因此只做「按账户小计」；实盘响应自带 currency，才按币种小计。
  * 缓存命中或降级时必须带 `as_of` 与 `stale`，让界面能说明数据是何时取的。
  * 单个账户失败不掩盖：计入 errors 并继续读其他账户。

用法:
  python positions.py --mode sim [--refresh]
输出: JSON {mode, as_of, source, stale, groups:[...], counts:{...}, errors:[...]}
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trading_datasource.futu_mcp import FutuUnavailable, call_tool, reset_session  # noqa: E402

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
CACHE_TTL_SECONDS = 300  # 5 分钟：面板是查看用途，不必每次进页面都问券商


def cache_path(mode):
    return DSH / f"trading-positions-{mode}.json"


def read_cache(mode):
    try:
        data = json.loads(cache_path(mode).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_cache(mode, payload):
    try:
        DSH.mkdir(parents=True, exist_ok=True)
        path = cache_path(mode)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False))
        temp.replace(path)
    except OSError:
        pass


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def round_or_none(value, digits=2):
    return None if value is None else round(value, digits)


# ---- 模拟盘 ----

def sim_groups(errors):
    """每个市场一个模拟账户；并发读持仓。

    串行读 9 个账户需要约 36 秒（每次往返约 3.5 秒）。共享客户端已做线程安全，
    因此用线程池并发，耗时降到 ≈ 一轮往返。
    """
    accounts = [a for a in ((call_tool("sim_trade_account_list", {}) or {}).get("accounts") or [])
                if a.get("account_id")]
    groups = []

    def fetch(account):
        try:
            return account, call_tool("sim_trade_position_list",
                                      {"acc_id": str(account["account_id"]),
                                       "market": account.get("market_id")}, timeout=30) or {}
        except FutuUnavailable as error:
            return account, error

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(accounts)))) as pool:
        fetched = list(pool.map(fetch, accounts))

    for account, data in fetched:
        acc_id = str(account.get("account_id") or "")
        title = str(account.get("account_title") or "模拟账户")
        market = account.get("market_id")
        if isinstance(data, Exception):
            errors.append({"account": title, "acc_id": acc_id, "reason": str(data)[:160]})
            continue
        rows = data.get("positions") or []
        positions = []
        for row in rows:
            qty = number(row.get("qty"))
            cost = number(row.get("cost_price"))
            price = number(row.get("cur_price"))
            positions.append({
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("stock_name") or ""),
                "qty": qty,
                "available": number(row.get("qty_avbl")),
                "cost_price": round_or_none(cost, 4),
                "price": round_or_none(price, 4),
                "market_value": round_or_none(number(row.get("mv"))),
                "pl_val": round_or_none(number(row.get("profit"))),
                "pl_ratio": round_or_none(number(row.get("profit_ratio"))),
                "currency": None,  # 模拟盘响应未提供，不猜
            })
        if not positions:
            continue
        groups.append({
            "account": title,
            "acc_id": acc_id,
            "market": market,
            "kind": "simulated",
            "positions": positions,
            # 单账户本身即单一市场，小计不涉及跨币种合并
            "market_value": round_or_none(sum(p["market_value"] or 0 for p in positions)),
            "pl_val": round_or_none(sum(p["pl_val"] or 0 for p in positions)),
        })
    return groups, len(accounts)


# ---- 实盘 ----

def live_groups(errors):
    accounts = (call_tool("account_authorized_trd_accs", {}) or {}).get("accounts") or []
    groups = []
    for account in accounts:
        acc_id = str(account.get("account_id") or "")
        if not acc_id:
            continue
        reset_session()
        try:
            rows = call_tool("account_positions", {"acc_id": acc_id}, timeout=30)
        except FutuUnavailable as error:
            errors.append({"account": f"账户 …{acc_id[-4:]}", "acc_id": acc_id,
                           "reason": str(error)[:160]})
            continue
        rows = rows if isinstance(rows, list) else (rows.get("positions") or [])
        positions = []
        for row in rows:
            positions.append({
                "symbol": str(row.get("code") or ""),
                "name": str(row.get("stock_name") or ""),
                "qty": number(row.get("qty")),
                "available": number(row.get("can_sell_qty")),
                "cost_price": round_or_none(number(row.get("cost_price")), 4),
                "price": round_or_none(number(row.get("nominal_price")), 4),
                "market_value": round_or_none(number(row.get("market_val"))),
                "pl_val": round_or_none(number(row.get("pl_val"))),
                "pl_ratio": round_or_none(number(row.get("pl_ratio"))),
                "currency": row.get("currency") or None,
            })
        if not positions:
            continue
        groups.append({
            "account": f"真实账户 …{acc_id[-4:]}",
            "acc_id": acc_id,
            "market": None,
            "kind": "real",
            "positions": positions,
            # 实盘账户可能同时持有港币与美元标的，按币种小计而不是直接相加
            "subtotals": currency_subtotals(positions),
            "market_value": None,
            "pl_val": None,
        })
    return groups, len(accounts)


def currency_subtotals(positions):
    buckets = {}
    for row in positions:
        currency = row.get("currency") or "未知"
        bucket = buckets.setdefault(currency, {"currency": currency, "market_value": 0.0, "pl_val": 0.0})
        bucket["market_value"] += row.get("market_value") or 0
        bucket["pl_val"] += row.get("pl_val") or 0
    return [{**bucket,
             "market_value": round_or_none(bucket["market_value"]),
             "pl_val": round_or_none(bucket["pl_val"])}
            for bucket in sorted(buckets.values(), key=lambda item: item["currency"])]


def collect(mode):
    errors = []
    groups, accounts_checked = (sim_groups(errors) if mode == "sim" else live_groups(errors))
    positions = sum(len(group["positions"]) for group in groups)
    return {
        "mode": mode,
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "source": ("futu/sim_trade_position_list" if mode == "sim" else "futu/account_positions"),
        "stale": False,
        "groups": groups,
        "counts": {
            "accounts_checked": accounts_checked,
            "accounts_with_positions": len(groups),
            "positions": positions,
        },
        "errors": errors,
        "note": "券商返回的真实持仓（按账户小计，不跨账户/币种合并）；"
                "本地面板按 TTL 缓存以避免频繁调用，as_of 为实际取数时间。",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="sim", choices=["sim", "live"])
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新读取")
    ap.add_argument("--ttl", type=int, default=CACHE_TTL_SECONDS)
    args = ap.parse_args()

    cached = read_cache(args.mode)
    fresh = False
    if cached and not args.refresh:
        try:
            age = time.time() - datetime.fromisoformat(cached["as_of"]).timestamp()
            fresh = age < args.ttl
        except (KeyError, ValueError, TypeError):
            fresh = False
    if fresh:
        cached["cached"] = True
        print(json.dumps(cached, ensure_ascii=False))
        return 0

    try:
        payload = collect(args.mode)
    except Exception as error:  # noqa: BLE001 - 取数失败时回退到上次缓存
        if cached:
            cached.update({"stale": True, "cached": True,
                           "error": f"实时读取失败，展示上次缓存：{str(error)[:160]}"})
            print(json.dumps(cached, ensure_ascii=False))
            return 0
        print(json.dumps({"error": f"持仓读取失败：{str(error)[:200]}"}, ensure_ascii=False))
        return 1

    payload["cached"] = False
    write_cache(args.mode, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
