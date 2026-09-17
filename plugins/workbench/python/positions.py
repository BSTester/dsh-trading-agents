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
EQUITY_MARKS_KEPT = 400  # 约一年半的交易日

#: 通道分派的 call_tool（WP13 任务 2）：模拟交易工具在 ``futu_channel=openapi`` 且凭据
#: 就绪时走 REST，其余工具（live 账户/行情）原样走 MCP——与 core/broker 和平台闸门用
#: 同一个适配器（``trading_datasource.channel.sim_call``），翻译表只有一份。
#: 默认通道（mcp）下行为与改造前逐字一致；惰性构造，避免只为读 MCP 就加载 OpenAPI 私钥。
_CHANNEL_CALL = None


def channel_call(name, arguments, timeout=30):
    global _CHANNEL_CALL
    if _CHANNEL_CALL is None:
        try:
            from trading_datasource.channel import sim_call  # noqa: PLC0415
            # MCP 侧仍经**本模块的 call_tool 名字**转发（不直接绑 futu_mcp.call_tool）：
            # 测试与运维都用 ``patch.object(positions, "call_tool")`` 注入替身，
            # 直接绑死会把那个注入点悄悄掐掉。
            _CHANNEL_CALL = sim_call(
                mcp_call=lambda tool, params, timeout=30:
                call_tool(tool, params, timeout=timeout))
        except Exception:  # noqa: BLE001 —— 适配器不可用：如实回退 MCP（既有行为）
            _CHANNEL_CALL = call_tool
    return _CHANNEL_CALL(name, arguments, timeout=timeout)


def equity_path(mode):
    return DSH / f"trading-equity-{mode}.json"


def read_marks(mode):
    try:
        data = json.loads(equity_path(mode).read_text())
    except (OSError, ValueError):
        return []
    marks = data.get("marks")
    return marks if isinstance(marks, list) else []


def append_mark(mode, accounts, positions_count):
    """记录当日盯市。

    **只从今天开始累积，不回溯伪造历史**：我们没有历史持仓快照，用当前持仓反推
    过去的权益曲线会得到一个从未真实存在过的数字。
    同一天重复取数时覆盖当天，不产生重复点。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    mark = {
        "date": today,
        "at": datetime.now().isoformat(timespec="seconds"),
        "accounts": accounts,
        "positions": positions_count,
    }
    marks = [row for row in read_marks(mode) if row.get("date") != today]
    marks.append(mark)
    marks.sort(key=lambda row: row.get("date") or "")
    marks = marks[-EQUITY_MARKS_KEPT:]
    try:
        DSH.mkdir(parents=True, exist_ok=True)
        path = equity_path(mode)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"mode": mode, "marks": marks}, ensure_ascii=False))
        temp.replace(path)
    except OSError:
        pass
    return marks


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
    accounts = [a for a in ((channel_call("sim_trade_account_list", {}) or {}).get("accounts") or [])
                if a.get("account_id")]
    groups = []

    def fetch(account):
        try:
            return account, channel_call("sim_trade_position_list",
                                      {"acc_id": str(account["account_id"]),
                                       "market": account.get("market_id")}, timeout=30) or {}
        except FutuUnavailable as error:
            return account, error

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(accounts)))) as pool:
        fetched = list(pool.map(fetch, accounts))

    # 只为**有持仓**的账户补一次资金，用于盯市记总资产（9 个账户里通常只有 2-3 个）
    held = [str(a["account_id"]) for a, data in fetched
            if not isinstance(data, Exception) and (data.get("positions") or [])]

    def fetch_cash(acc_id):
        try:
            return acc_id, channel_call("sim_trade_cash_info", {"acc_id": acc_id}, timeout=30) or {}
        except FutuUnavailable:
            return acc_id, {}

    cash_by_account = {}
    if held:
        with ThreadPoolExecutor(max_workers=min(8, len(held))) as pool:
            cash_by_account = dict(pool.map(fetch_cash, held))

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
        cash = cash_by_account.get(acc_id) or {}
        groups.append({
            "account": title,
            "acc_id": acc_id,
            "market": market,
            "kind": "simulated",
            "positions": positions,
            # 单账户本身即单一市场，小计不涉及跨币种合并
            "market_value": round_or_none(sum(p["market_value"] or 0 for p in positions)),
            "pl_val": round_or_none(sum(p["pl_val"] or 0 for p in positions)),
            "cash": round_or_none(number(cash.get("balance"))),
            "total_asset": round_or_none(number(cash.get("total_asset"))),
            "currency": None,  # 响应未提供，不推断
            "risk": account_risk(positions, round_or_none(number(cash.get("total_asset")))),
        })
    return groups, len(accounts)


# ---- 实盘 ----

def live_groups(errors):
    accounts = (channel_call("account_authorized_trd_accs", {}) or {}).get("accounts") or []
    groups = []
    for account in accounts:
        acc_id = str(account.get("account_id") or "")
        if not acc_id:
            continue
        reset_session()
        try:
            rows = channel_call("account_positions", {"acc_id": acc_id}, timeout=30)
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
            "risk": account_risk(positions),
        })
    return groups, len(accounts)


def account_risk(positions, total_asset=None):
    """按**单个账户**算持仓风险。绝不跨账户合并——不同账户可能不同币种。

    占比给两个口径（都带明确分母，避免误读）：
      share_of_positions —— 占本账户**持仓市值**
      share_of_assets    —— 占本账户**总资产**（含现金；取不到总资产时为 None）
    """
    valued = [row for row in positions if (row.get("market_value") or 0) > 0]
    total = sum(row["market_value"] for row in valued)
    ranked = sorted(valued, key=lambda row: row["market_value"], reverse=True)
    top = [{
        "symbol": row["symbol"], "name": row["name"], "market_value": row["market_value"],
        "share_of_positions": round(row["market_value"] / total * 100, 2) if total > 0 else None,
        "share_of_assets": (round(row["market_value"] / total_asset * 100, 2)
                            if total_asset and total_asset > 0 else None),
        "pl_ratio": row.get("pl_ratio"),
    } for row in ranked[:3]]
    winners = [row for row in positions if (row.get("pl_val") or 0) > 0]
    losers = [row for row in positions if (row.get("pl_val") or 0) < 0]
    return {
        "positions": len(positions),
        "valued_positions": len(valued),
        "market_value": round_or_none(total),
        "top": top,
        "max_share_of_positions": top[0]["share_of_positions"] if top else None,
        "max_share_symbol": top[0]["symbol"] if top else None,
        "winners": {"count": len(winners),
                    "pl_val": round_or_none(sum(row.get("pl_val") or 0 for row in winners))},
        "losers": {"count": len(losers),
                   "pl_val": round_or_none(sum(row.get("pl_val") or 0 for row in losers))},
        "note": "占比分母：share_of_positions = 本账户持仓市值；"
                "share_of_assets = 本账户总资产（含现金，取不到则为空）。不跨账户合并。",
    }


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
    # 记录当日盯市（真实数据；只从今天开始累积）
    marks = append_mark(mode, [{
        "account": group["account"],
        "market_value": group["market_value"],
        "cash": group.get("cash"),
        "total_asset": group.get("total_asset"),
        "currency": group.get("currency"),
    } for group in groups], positions)
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
        "equity_marks": marks,
        "note": "券商返回的真实持仓（按账户小计，不跨账户/币种合并）；"
                "权益盯市从首次取数当日起累积，**不回溯伪造历史**；"
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
