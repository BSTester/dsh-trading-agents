#!/usr/bin/env python3
"""富途远程 MCP OAuth 授权助手（跨平台：Linux / macOS / Windows）

流程：动态注册客户端 → PKCE → 本地回调监听 → 打开富途授权页 →
      用户登录确认 → 自动换取 token → 存入 ~/.dsh/futu-token（仅本人可读）

用法：
    python scripts/futu_auth.py            # 默认只授只读 scope（推荐）
    python scripts/futu_auth.py --write    # 额外申请交易写权限 trade:write

仅依赖 Python 3.8+ 标准库。
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DSH_HOME = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
TOKEN_FILE = DSH_HOME / "futu-token"
REFRESH_FILE = DSH_HOME / "futu-refresh"
CLIENT_FILE = DSH_HOME / "futu-client-id"
CALLBACK_PORT = 18923
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"
REGISTER_URL = "https://webapi.futunn.com/oauth2/register"
AUTHORIZE_URL = "https://webapi.futunn.com/oauth2/authorize/confirm"
TOKEN_URL = "https://webapi.futunn.com/oauth2/token"
TIMEOUT_SECONDS = 600


def say(msg): print(f"\033[1;36m==>\033[0m {msg}", flush=True)
def warn(msg): print(f"\033[1;33m ==\033[0m {msg}", flush=True)


def http_json(url, payload=None, form=None):
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def register_client():
    if CLIENT_FILE.exists():
        client_id = CLIENT_FILE.read_text().strip()
        if client_id:
            say(f"复用已注册客户端 {client_id}")
            return client_id
    say("向富途注册 OAuth 客户端…")
    resp = http_json(REGISTER_URL, {
        "client_name": "dsh-trading-agents",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    client_id = resp["client_id"]
    CLIENT_FILE.write_text(client_id)
    say(f"注册成功 client_id={client_id}")
    return client_id


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    code = ""
    seen = False

    def do_GET(self):
        if not self.path.startswith("/callback"):
            self.send_response(404)
            self.end_headers()
            return  # 无关探针：忽略并继续监听
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        CallbackHandler.code = query.get("code", [""])[0]
        CallbackHandler.seen = True
        self.send_response(200)
        self.end_headers()
        # 授权完成后自动关闭该 127.0.0.1 标签页
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<script>window.close();</script></head>"
                "<body style='font-family:sans-serif'><h3>✅ 授权成功</h3>"
                "<p>token 已保存，本页面将自动关闭。若未关闭可手动关闭。</p>"
                "</body></html>")
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *a):
        pass


def touch_preset():
    """触碰 preset 组合文件，触发 harness 原位重载 futu-mcp 行。"""
    preset_yml = Path(__file__).resolve().parent.parent / "agent.cordis.yml"
    if preset_yml.exists():
        os.utime(preset_yml, None)
        say("已触发当前会话的组合重载，富途工具应已在本会话就绪。")
        warn("若本会话仍未见 mcp__futu__* 工具，请新建会话（选本模式）。")
    else:
        warn("重启 harness 会话后，富途工具（mcp__futu__*）即可用。")


def refresh_tokens():
    """用 refresh_token 换新 access_token（token 过期时免重新授权）。"""
    if not REFRESH_FILE.exists():
        warn("无 refresh_token，请直接运行完整授权（不带 --refresh）。")
        return 1
    say("使用 refresh_token 续期…")
    resp = http_json(TOKEN_URL, form={
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_FILE.read_text().strip(),
        "client_id": CLIENT_FILE.read_text().strip(),
    })
    access = resp.get("access_token", "")
    if not access:
        warn(f"续期失败（refresh_token 可能已失效，请重新完整授权）：{json.dumps(resp, ensure_ascii=False)[:200]}")
        return 1
    TOKEN_FILE.write_text(access)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    if resp.get("refresh_token"):
        REFRESH_FILE.write_text(resp["refresh_token"])
    say("续期成功，token 已更新。")
    touch_preset()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="额外申请交易写权限 trade:write")
    ap.add_argument("--refresh", action="store_true", help="用 refresh_token 续期，免重新授权")
    args = ap.parse_args()
    if args.refresh:
        return refresh_tokens()
    scopes = "quote:read accid:* trade:read" + (" trade:write" if args.write else "")

    # 端口占用快速失败：残留的旧监听会用错误的 PKCE verifier 接走授权码，必须避免
    import socket
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", CALLBACK_PORT))
    except OSError:
        warn(f"端口 {CALLBACK_PORT} 已被占用（可能是残留的旧授权脚本）。")
        warn("请先结束旧进程再重试：  pkill -f futu_auth.py   （Windows：任务管理器结束 python）")
        return 1
    finally:
        probe.close()

    client_id = register_client()

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), CallbackHandler)
    server.timeout = 2  # 循环接收：每个请求都处理，直到拿到有效授权码
    say(f"本地回调监听已启动：http://127.0.0.1:{CALLBACK_PORT}/callback")

    def serve():
        deadline = time.time() + TIMEOUT_SECONDS
        while time.time() < deadline and not CallbackHandler.seen:
            server.handle_request()  # 每次处理一个请求，循环直到成功
        # 处理完授权回调后关闭监听，避免残留

    threading.Thread(target=serve, daemon=True).start()

    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": scopes,
        "state": secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }, safe=":", quote_via=urllib.parse.quote)  # 用 %20/%2A 精确编码，避免 + 和 * 在浏览器跳转中损坏
    say(f"请在浏览器中完成富途账号授权（{TIMEOUT_SECONDS // 60} 分钟内有效）：")
    print(f"\n    {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        warn("未能自动打开浏览器，请手动复制上方链接。")

    warn(f"等待授权回调（最长 {TIMEOUT_SECONDS // 60} 分钟）…")
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline and not CallbackHandler.seen:
        time.sleep(1)
    server.server_close()
    if not CallbackHandler.seen or not CallbackHandler.code:
        warn("超时未收到授权回调，请重试。")
        return 1
    code = CallbackHandler.code
    say("收到授权码，换取 token…")

    resp = http_json(TOKEN_URL, form={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    access = resp.get("access_token", "")
    refresh = resp.get("refresh_token", "")
    if not access:
        warn(f"换取 token 失败：{json.dumps(resp, ensure_ascii=False)[:300]}")
        return 1

    DSH_HOME.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(access)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    if refresh:
        REFRESH_FILE.write_text(refresh)
        try:
            os.chmod(REFRESH_FILE, 0o600)
        except OSError:
            pass
    say(f"授权完成！token 已存入 {TOKEN_FILE}")

    touch_preset()
    return 0


if __name__ == "__main__":
    sys.exit(main())
