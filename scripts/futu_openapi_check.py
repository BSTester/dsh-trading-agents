#!/usr/bin/env python3
"""富途 OpenAPI 连通性自检（AppKey 签名模式 / 数据面逐方法模式）。

用途：验证「AppKey + Ed25519/RSA 私钥」这条通道能否真正调通——
    默认发一个无 body 的 GET（港股交易日历），打印 HTTP 状态与响应信封；
    ``--method POST --body '{...}'`` 可验证带 body 的签名路径。

``--dataplane``：按锁定表 §C.1–§C.7 对**数据面每个方法**用最小合法参数各调一次
    （11 个单方法端点 + F10 的 26 个 section + 衍生品 4 项 = 41 项），逐项给出分类：
    ``ok`` / ``no_data``（-10，空而非错）/ ``business``（其余业务码，如实列出）/
    ``param``（本地参数拒绝）/ ``unavailable``（传输或凭据故障）。**只有后两类算失败**
    （退出码 1）——业务码说明通道可达。需 OAuth 用户登录态的自选 3 项与属 WP13 的
    模拟交易 9 项不在本模式内。本模式用统一凭据文件（AppKey 或 OAuth 均可），
    未配置时如实报「未配置」并退出码 2。

私钥永不离开本机：只从 ``--key-file``（默认 ``~/.dsh/futu-openapi-key.pem``，
0600）或 ``--key-file -``（stdin）读取，不写日志、不回显。

用法：
    python3 scripts/futu_openapi_check.py --app-key <AppKeyID>
    python3 scripts/futu_openapi_check.py --app-key <ID> --method POST --path \
        /api/v1.0/quote/snapshot --body '{"code_list":["HK.00700"]}'
    python3 scripts/futu_openapi_check.py --dataplane          # 数据面逐方法自检
    python3 scripts/futu_openapi_check.py --dataplane --json    # 机器可读

退出码：0=HTTP 2xx 且业务信封成功（数据面模式=全部项可达）；1=通道/业务失败
（数据面模式=存在 param/unavailable 项）；2=参数/私钥/凭据未配置问题。
"""
import argparse
import base64
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from trading_datasource import futu_openapi as fo  # noqa: E402


def build_args(argv=None):
    today = dt.date.today().isoformat()
    parser = argparse.ArgumentParser(description="富途 OpenAPI 连通性自检")
    parser.add_argument("--app-key", default=None,
                        help="控制台创建的 AppKey ID（X-Api-Key）；单请求模式必填")
    parser.add_argument("--key-file", default=str(Path.home() / ".dsh" / "futu-openapi-key.pem"),
                        help="AppKey 私钥 PEM 路径（默认 ~/.dsh/futu-openapi-key.pem；'-' 读 stdin）")
    parser.add_argument("--algorithm", default="Ed25519", choices=fo.AppKeySigner.ALGORITHMS,
                        help="签名算法（须与控制台创建 AppKey 时选择的一致）")
    parser.add_argument("--host", default=fo.DEFAULT_HOST, help="API Host（默认官方生产地址）")
    parser.add_argument("--path", default="/api/v1.0/quote/trading-days",
                        help="请求路径（不含域名/query）")
    parser.add_argument("--method", default="GET", help="HTTP 方法")
    parser.add_argument("--query", default=f"market=HK&start={today}&end={today}",
                        help="原始查询串（顺序即签名顺序）")
    parser.add_argument("--body", default=None, help="请求体原文（JSON 字符串；不传则无 body）")
    parser.add_argument("--timeout", type=float, default=15.0, help="传输超时秒")
    parser.add_argument("--dataplane", action="store_true",
                        help="数据面逐方法自检（锁定表 §C.1–§C.7；41 项）")
    parser.add_argument("--json", action="store_true",
                        help="--dataplane 输出机器可读 JSON（默认逐行文本）")
    parser.add_argument("--option-symbol", default=None,
                        help="--dataplane 期权类方法用的合约代码（缺省用正股，上游会回业务码）")
    args = parser.parse_args(argv)
    if not args.dataplane and not args.app_key:
        parser.error("--app-key 必填（单请求模式）；或加 --dataplane 做数据面逐方法自检")
    return args


def load_signer(key_file, algorithm):
    """读取私钥并构造 signer；返回 (signer, 公钥 base64 DER)。不打印私钥内容。"""
    if key_file == "-":
        pem = sys.stdin.buffer.read()
    else:
        path = Path(key_file).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"私钥文件不存在：{path}")
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            print(f"[warn] 私钥文件权限过宽（{oct(mode)}）；建议 chmod 600 {path}", file=sys.stderr)
        pem = path.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    signer = fo.AppKeySigner(key, algorithm)
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return signer, base64.b64encode(der).decode()


def build_request(args, signer, timestamp_ms, nonce):
    """构造请求描述 dict——签名与发送共用同一份 query/body 字节。"""
    body_bytes = args.body.encode("utf-8") if args.body else None
    authorization = signer.sign(timestamp_ms, args.method, args.path, args.query, body_bytes)
    headers = {
        "X-Api-Key": args.app_key,
        "X-Timestamp": str(timestamp_ms),
        "X-Nonce": nonce,
        "Authorization": authorization,
        "Content-Type": "application/json",
    }
    url = args.host.rstrip("/") + args.path + (("?" + args.query) if args.query else "")
    return {"url": url, "method": args.method.upper(), "headers": headers, "body": body_bytes}


def send(request, timeout):
    """默认传输：标准库 urllib；返回 (status, raw_bytes, response_headers)。"""
    http_request = urllib.request.Request(
        request["url"], data=request["body"], headers=request["headers"], method=request["method"])
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:  # 4xx/5xx 也是有效响应（含错误信封）
        return error.code, error.read(), dict(error.headers or {})


# ---------------------------------------------------------------------------
# --dataplane：数据面逐方法自检（锁定表 §C.1–§C.7）
# ---------------------------------------------------------------------------
#: 自检默认标的（正股）；F10 里 symbol 语义为例外者见下一张表。
DATAPLANE_SYMBOL = "HK.00700"
#: symbol 不是个股的 section（锁定表 §C.5 逐行口径）：指数代码。
DATAPLANE_SYMBOL_OVERRIDES = {
    "valuation_index_stocks": "HK.800000",        # 指数代码（恒指）
    "valuation_index_stock_plates": "HK.800000",  # 指数代码
}
#: 期权类方法的缺省合约代码：上游在传正股时的报错里给出此例（官方文档同款写法）。
DATAPLANE_OPTION_SYMBOL = "HK.TCH260629C470000"
#: 板块代码会随市场变动，静态常量被上游拒为 -7；优先取 plate_list 的首个板块。
DATAPLANE_PLATE_FALLBACK = "HK.LIST23618"
#: F10 中除 symbol 外还有必填参数的 section（锁定表 §C.5）。
DATAPLANE_F10_EXTRA = {
    "company_executive_background": {"leader_name": "马化腾"},
    "top_brokers_history": {"days_before": 1},
}
#: 分类名 → 是否算「通道可达」（no_data/business 都说明凭据与链路是通的）。
DATAPLANE_FAILING_CLASSES = frozenset({"param", "unavailable"})
#: 官方 `-10 no_data`（传输层 `_RestValidators.NO_DATA_ERRCODE` 同值；本脚本不导入私有基类）。
NO_DATA_ERRCODE = -10


def _first_plate_code(groups, market="HK", plate_class="INDUSTRY"):
    """现取一个真实板块代码（板块列表首个）；取不到回退常量。

    静态板块代码会被上游拒为 ``-7 invalid symbol``（编号随市场变动），因此自检优先
    用同一通道现取的代码——这也顺带验证了 plate_list → plate_stock 的串联可用性。
    """
    try:
        data = groups.plate.plate_list(market=market, plate_class=plate_class)
    except Exception:  # noqa: BLE001 —— 自检不因取号失败而中断
        return DATAPLANE_PLATE_FALLBACK
    entries = (data or {}).get("plate_list") if isinstance(data, dict) else None
    for entry in entries or ():
        if isinstance(entry, dict) and entry.get("code"):
            return entry["code"]
    return DATAPLANE_PLATE_FALLBACK


def _dataplane_groups(client):
    """八个数据面方法组（与 server/futu_data.DataPlaneGroups 同构，共享同一 client）。"""
    return SimpleNamespace(
        basic=fo.OpenApiBasicData(client), plate=fo.OpenApiPlate(client),
        screen=fo.OpenApiScreen(client), ipo=fo.OpenApiIpo(client),
        short=fo.OpenApiShort(client), watchlist=fo.OpenApiWatchlist(client),
        derivatives=fo.OpenApiDerivatives(client), f10=fo.OpenApiF10(client))


def _first_option_code(client, underlying="HK.00700"):
    """现取一个**当前有效**的期权合约代码（最近到期日的期权链首项）。

    静态示例合约会随到期失效（上游回 ``-7 invalid symbol``），先用既有
    ``option_expiration`` → ``option_chain`` 两跳取真合约，再喂给期权类方法——
    这样这三项才真正走通，而不是每次都回一个业务码。取不到回退官方示例。
    """
    try:
        market = fo.OpenApiMarket(client)
        expiration = market.option_expiration(underlying)
        entries = (expiration or {}).get("expiration_list") or []
        strike_time = entries[0].get("strike_time") if entries else None
        if not strike_time:
            return DATAPLANE_OPTION_SYMBOL
        chain = market.option_chain(underlying, start=strike_time, end=strike_time)
        for contract in (chain or {}).get("option_chain") or ():
            if isinstance(contract, dict) and contract.get("code"):
                return contract["code"]
    except Exception:  # noqa: BLE001 —— 自检不因取合约失败而中断
        pass
    return DATAPLANE_OPTION_SYMBOL


def _section_symbol(groups, section, default):
    """F10 section 的 symbol：指数用恒指代码，板块类现取真实板块代码，其余用正股。"""
    override = DATAPLANE_SYMBOL_OVERRIDES.get(section)
    if override:
        return override
    if section == "valuation_plate_stocks":
        return _first_plate_code(groups)
    return default


def dataplane_items(client, option_symbol=None):
    """自检项 ``[(名称, 无参 callable), ...]``：锁定表 §C.1–§C.7 的每个方法各一项。

    §C.8 自选 3 项需 OAuth 用户登录态、§C.9 模拟交易 9 项属 WP13——都不在此。
    期权类方法缺省用官方示例合约；要换就传 ``option_symbol``。
    """
    groups = _dataplane_groups(client)
    symbol = DATAPLANE_SYMBOL
    option = option_symbol or _first_option_code(client)
    items = []

    def add(name, call):
        items.append((name, call))

    # C.1 基础数据（4）
    add("economic_calendar_hot", lambda: groups.basic.economic_calendar_hot())
    add("economic_calendar_search",
        lambda: groups.basic.economic_calendar_search(keyword="CPI", search_type=1))
    add("info_owner_plate", lambda: groups.basic.owner_plate(symbol))
    add("info_rehab", lambda: groups.basic.rehab(symbol))
    # C.2 板块（2）——板块代码现取（静态编号会被上游拒为 -7）
    add("plate_list", lambda: groups.plate.plate_list(market="HK", plate_class="INDUSTRY"))
    add("plate_stock", lambda: groups.plate.plate_stock(
        plate_code=_first_plate_code(groups), limit=3))
    # C.3 全市场筛选（2）
    add("stock_screen", lambda: groups.screen.stock_screen(
        screen_queries=[{"simple_field_query": {"simple_field": 1, "screen_value_list": [1]}}],
        limit=3))
    add("warrant_screen", lambda: groups.screen.warrant_screen(market_type=1, limit=3))
    # C.4 IPO（1）
    add("ipo_list", lambda: groups.ipo.ipo_list(market="hk"))
    # C.5 个股深度数据（26 个 section）
    for section in sorted(fo.OpenApiF10.SECTIONS):
        add(f"f10.{section}", lambda s=section: getattr(groups.f10, s)(
            _section_symbol(groups, s, symbol), **DATAPLANE_F10_EXTRA.get(s, {})))
    # C.6 卖空（2）——仅港美可卖空证券
    add("short_daily_volume", lambda: groups.short.short_daily_volume("US.AAPL", count=2))
    add("short_interest", lambda: groups.short.short_interest("US.AAPL", count=2))
    # C.7 衍生品（4）
    add("derivative.future_info", lambda: groups.derivatives.future_info(["HK.HSI2609"]))
    add("derivative.reference_future", lambda: groups.derivatives.reference_future(option))
    add("derivative.option_volatility", lambda: groups.derivatives.option_volatility(option))
    add("derivative.option_exercise_probability",
        lambda: groups.derivatives.option_exercise_probability(option))
    return items


def call_item(call):
    """执行一项并分类：ok / no_data / business / param / unavailable。

    ``-10`` 是「合法但无数据」→ no_data（不是失败）；其余业务码（-3/-7/-8/-9…）→
    business 并原样带出，因为它们证明通道可达。本地参数拒绝（传输层 ValueError）与
    传输故障才分别记 param / unavailable——这两类影响退出码。
    """
    try:
        value = call()
    except ValueError as error:  # 传输层本地校验（自检参数不对）
        return {"class": "param", "code": None, "message": str(error)[:200]}
    except (fo.TransportError, fo.UnexpectedResponse) as error:  # 传输/非信封响应
        return {"class": "unavailable", "code": None, "message": str(error)[:200]}
    except fo.OpenApiError as error:  # 信封 s=error / ret_code!=0
        code = error.errcode
        cls = "no_data" if code == NO_DATA_ERRCODE else "business"
        return {"class": cls, "code": code, "message": str(error)[:200]}
    except Exception as error:  # noqa: BLE001 —— 其余按通道不可用如实报
        return {"class": "unavailable", "code": None, "message": str(error)[:200]}
    if isinstance(value, dict) and value.get("no_data"):
        return {"class": "no_data", "code": NO_DATA_ERRCODE, "message": "上游无数据"}
    return {"class": "ok", "code": 0, "message": ""}


def run_dataplane(argv, client=None, credential_path=None):
    """数据面逐方法自检；返回退出码（0 全可达 / 1 有 param|unavailable / 2 未配置凭据）。"""
    args = build_args(argv)
    credentials_note = "注入客户端"
    if client is None:
        store = fo.CredentialStore(credential_path) if credential_path else fo.CredentialStore()
        try:
            credentials = store.load()
        except ValueError as error:
            print(json.dumps({"ok": False, "stage": "credentials", "error": str(error)[:300]},
                             ensure_ascii=False))
            return 2
        if not credentials:
            print(json.dumps({
                "ok": False, "stage": "credentials",
                "error": f"未配置富途 OpenAPI 凭据：{store.path}"
                         "（先跑 scripts/futu_auth.py 授权，或在设置页配置 AppKey）",
            }, ensure_ascii=False))
            return 2
        client = fo.OpenApiClient(store)
        credentials_note = str(store.path)

    items = [{"name": name, **call_item(call)}
             for name, call in dataplane_items(client, args.option_symbol)]
    counts = Counter(item["class"] for item in items)
    ok = not (set(counts) & DATAPLANE_FAILING_CLASSES)
    summary = {"ok": ok, "mode": "dataplane", "credentials": credentials_note,
               "total": len(items), "counts": dict(counts), "items": items}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"数据面逐方法自检（锁定表 §C.1–§C.7；共 {len(items)} 项；"
              f"凭据：{credentials_note}）")
        for item in items:
            detail = f"code={item['code']}" if item["code"] not in (None, 0) else ""
            if item["message"]:
                detail = (detail + " " + item["message"]).strip()
            print(f"  {item['name']:44} {item['class']:12} {detail}".rstrip())
        print(f"总计 {len(items)} 项：" + " ".join(
            f"{cls}={counts.get(cls, 0)}"
            for cls in ("ok", "no_data", "business", "param", "unavailable"))
            + "（param/unavailable 才会让本次自检失败）")
    return 0 if ok else 1


def main(argv=None, transport=send):
    args = build_args(argv)
    if args.dataplane:
        return run_dataplane(argv)
    try:
        signer, public_key = load_signer(args.key_file, args.algorithm)
    except Exception as error:  # 参数/私钥问题：退出码 2
        print(json.dumps({"ok": False, "stage": "private-key", "error": str(error)[:300]},
                         ensure_ascii=False))
        return 2

    timestamp_ms = int(time.time() * 1000)
    request = build_request(args, signer, timestamp_ms, fo.AppKeySigner.nonce())
    summary = {
        "ok": False, "host": args.host, "method": args.method.upper(), "path": args.path,
        "algorithm": args.algorithm, "public_key": public_key,
        "signed_with_body": request["body"] is not None,
    }
    try:
        status, raw, _headers = transport(request, args.timeout)
    except Exception as error:
        summary.update({"stage": "transport", "error": str(error)[:300]})
        print(json.dumps(summary, ensure_ascii=False))
        return 1

    summary["http_status"] = status
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        payload = {"raw": raw[:300].decode("utf-8", "replace")}
    summary["response"] = payload
    ok = 200 <= status < 300 and (
        payload.get("s") == "ok" or payload.get("ret_code") == 0 or "data" in payload)
    summary["ok"] = bool(ok)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
