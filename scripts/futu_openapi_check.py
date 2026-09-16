#!/usr/bin/env python3
"""富途 OpenAPI 连通性自检（AppKey 签名模式）。

用途：验证「AppKey + Ed25519/RSA 私钥」这条通道能否真正调通——
    默认发一个无 body 的 GET（港股交易日历），打印 HTTP 状态与响应信封；
    ``--method POST --body '{...}'`` 可验证带 body 的签名路径。

私钥永不离开本机：只从 ``--key-file``（默认 ``~/.dsh/futu-openapi-key.pem``，
0600）或 ``--key-file -``（stdin）读取，不写日志、不回显。

用法：
    python3 scripts/futu_openapi_check.py --app-key <AppKeyID>
    python3 scripts/futu_openapi_check.py --app-key <ID> --method POST --path \
        /api/v1.0/quote/snapshot --body '{"code_list":["HK.00700"]}'

退出码：0=HTTP 2xx 且业务信封成功；1=通道或业务失败；2=参数/私钥问题。
"""
import argparse
import base64
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from trading_datasource import futu_openapi as fo  # noqa: E402


def build_args(argv=None):
    today = dt.date.today().isoformat()
    parser = argparse.ArgumentParser(description="富途 OpenAPI 连通性自检（AppKey 模式）")
    parser.add_argument("--app-key", required=True, help="控制台创建的 AppKey ID（X-Api-Key）")
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
    return parser.parse_args(argv)


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


def main(argv=None, transport=send):
    args = build_args(argv)
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
