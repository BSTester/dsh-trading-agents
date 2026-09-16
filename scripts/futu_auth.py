#!/usr/bin/env python3
"""富途授权助手（跨平台：Linux / macOS / Windows）

通道一（默认）：远程 MCP OAuth（token 存 ~/.dsh/futu-token 等散文件）
流程：动态注册客户端 → PKCE → 本地回调监听 → 打开富途授权页 →
      用户登录确认 → 自动换取 token → 存入 ~/.dsh/futu-token（仅本人可读）

用法：
    python scripts/futu_auth.py            # 默认只授只读 scope（推荐）
    python scripts/futu_auth.py --write    # 额外申请交易写权限 trade:write

通道二（--openapi，WP8）：OpenAPI（REST）OAuth 2.1+PKCE
流程：POST /oauth2/register（public client，PKCE required）→ 保存 client_id 到
      ~/.dsh/futu-openapi.json → 本地监听 60355 收 /callback?code&state →
      打印授权 URL 浏览器完成 → 校验 state → 换 token → 凭据落盘（0600 原子写）

用法：
    python scripts/futu_auth.py --openapi            # 默认含交易写 scope
    python scripts/futu_auth.py --openapi --scope …  # 自定义 scope

默认流程仅依赖 Python 3.8+ 标准库；--openapi 复用统一数据层的
trading_datasource.futu_openapi.CredentialStore（凭据文件唯一读写实现，
从仓库根目录运行即可）。
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
EXPIRY_FILE = DSH_HOME / "futu-token-expiry"
CALLBACK_PORT = 18923
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"
REGISTER_URL = "https://webapi.futunn.com/oauth2/register"
AUTHORIZE_URL = "https://webapi.futunn.com/oauth2/authorize/confirm"
TOKEN_URL = "https://webapi.futunn.com/oauth2/token"
TIMEOUT_SECONDS = 600

# ------------------------------------------------------------- --openapi（WP8）
OPENAPI_CALLBACK_PORT = 60355
OPENAPI_REDIRECT_URI = f"http://localhost:{OPENAPI_CALLBACK_PORT}/callback"
OPENAPI_DEFAULT_SCOPE = "quote:read accid:* trade:read trade:write"


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
    """触碰 preset 组合文件。

    注意：**实测 preset 目录没有任何 watcher**，组合是在会话挂载时读取的，
    因此 touch 只对**之后新建的会话**生效，不会让当前已挂载的会话重新求值
    Authorization 头。此处保留 touch 是为了让后续会话读到最新状态。
    """
    preset_yml = Path(__file__).resolve().parent.parent / "agent.cordis.yml"
    if preset_yml.exists():
        os.utime(preset_yml, None)
        say("token 已就绪。")
        warn("富途工具只在**新建会话**时读取本组合，请新建会话（仍选本模式）后再使用；"
             "在当前会话里等待不会生效。")
    else:
        warn("未找到 preset 组合文件；新建会话后富途工具（mcp__futu__*）即可用。")


def record_expiry(resp):
    """记录 access_token 到期时刻。

    实测 OAuth 响应 expires_in = 7200（2 小时）——很短，且过期时服务端对所有工具
    返回 internal error 而不是 401。把它写下来，渠道状态页才能提前预警。
    """
    seconds = resp.get("expires_in")
    if not seconds:
        return None
    from datetime import datetime, timedelta
    moment = datetime.now() + timedelta(seconds=float(seconds))
    try:
        EXPIRY_FILE.write_text(moment.isoformat(timespec="seconds"))
    except OSError:
        pass
    return moment


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
    moment = record_expiry(resp)
    if resp.get("refresh_token"):
        REFRESH_FILE.write_text(resp["refresh_token"])
    moment = record_expiry(resp)
    say("续期成功，token 已更新。" + (f"（有效期至 {moment:%H:%M}）" if moment else ""))
    touch_preset()
    return 0


# ================================================================ --openapi
# 命令行可测部分抽出为纯函数（离线单测见 tests/test_wp8_openapi_client.py）；
# 真实网络流程（注册/浏览器授权/60355 回调/token 端点实连）不自动化测试，人工执行。

def openapi_register_payload(redirect_uri=OPENAPI_REDIRECT_URI):
    """POST /oauth2/register 注册体：public client，PKCE required。"""
    return {
        "client_name": "dsh-trading-agents-openapi",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def openapi_generate_pkce():
    """(verifier, challenge)：S256 = BASE64URL(SHA256(verifier))，无 padding。

    算法与 trading_datasource.futu_openapi.Pkce 一致；此处独立成纯函数，
    便于脱离统一数据层单独测试。
    """
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def openapi_generate_state():
    """防 CSRF 的 state（回调时逐字校验）。"""
    return secrets.token_urlsafe(16)


def openapi_build_authorize_url(client_id, redirect_uri, state, code_challenge, scope):
    """GET /oauth2/authorize/confirm 的授权 URL（S256；%20/%2A 精确编码）。"""
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }, safe=":", quote_via=urllib.parse.quote)


def openapi_exchange_token(post_form, *, code, code_verifier, client_id, redirect_uri):
    """POST /oauth2/token（authorization_code + code_verifier）。

    post_form(url, form) -> dict 注入以便离线测试；生产传 http_json。
    """
    return post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    })


def openapi_credential_update(cred, token_resp, now_ms):
    """token 响应 → 新凭据 dict（expires_at 毫秒）。

    官方不轮换 refresh_token：响应带了才更新，否则保留旧值；
    既有字段（如 app_key）原样保留，mode 固定 oauth。
    """
    out = dict(cred)
    out["mode"] = "oauth"
    out["access_token"] = token_resp["access_token"]
    out["expires_at"] = now_ms + int(float(token_resp.get("expires_in", 7200)) * 1000)
    if token_resp.get("refresh_token"):
        out["refresh_token"] = token_resp["refresh_token"]
    if token_resp.get("scope"):
        out["scope"] = token_resp["scope"]
    return out


def openapi_validate_state(expected, received):
    """回调 state 逐字校验，不符即拒绝换 token。"""
    if not expected or received != expected:
        raise ValueError(f"state 不匹配（期望 {expected!r}，收到 {received!r}）"
                         "——疑似回调劫持，拒绝换 token")
    return True


class OpenApiCallbackHandler(http.server.BaseHTTPRequestHandler):
    """60355 回调：捕获 code + state（与 MCP 通道的 CallbackHandler 互不干扰）。"""

    code = ""
    state = ""
    seen = False

    def do_GET(self):
        if not self.path.startswith("/callback"):
            self.send_response(404)
            self.end_headers()
            return  # 无关探针：忽略并继续监听
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        OpenApiCallbackHandler.code = query.get("code", [""])[0]
        OpenApiCallbackHandler.state = query.get("state", [""])[0]
        OpenApiCallbackHandler.seen = True
        self.send_response(200)
        self.end_headers()
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<script>window.close();</script></head>"
                "<body style='font-family:sans-serif'><h3>✅ OpenAPI 授权成功</h3>"
                "<p>凭据已保存，本页面将自动关闭。若未关闭可手动关闭。</p>"
                "</body></html>")
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *a):
        pass


def _datasource_store():
    """统一数据层的 CredentialStore（凭据文件唯一读写实现）。

    从仓库根目录运行即可解析 plugins/datasource/python；
    安装布局下 venv 的 .pth 已含该目录，sys.path 补写幂等无害。
    """
    repo_python = Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"
    if repo_python.is_dir() and str(repo_python) not in sys.path:
        sys.path.insert(0, str(repo_python))
    from trading_datasource.futu_openapi import CredentialStore
    return CredentialStore


def cmd_openapi(scope=OPENAPI_DEFAULT_SCOPE):
    """OpenAPI OAuth 2.1+PKCE 完整流程（真实网络，人工执行）。

    register → 保存 client_id → 监听 60355 收 code/state → 浏览器授权 →
    校验 state → 换 token → 凭据落盘（~/.dsh/futu-openapi.json，0600）。
    """
    try:
        CredentialStore = _datasource_store()
    except ImportError as e:
        warn(f"无法导入 trading_datasource（请从仓库根目录运行）：{e}")
        return 1
    store = CredentialStore()
    cred = store.load()

    client_id = cred.get("client_id")
    if client_id:
        say(f"复用已注册 OpenAPI client {client_id}")
    else:
        say("向富途注册 OpenAPI OAuth 客户端（public client + PKCE）…")
        resp = http_json(REGISTER_URL, openapi_register_payload())
        client_id = resp.get("client_id")
        if not client_id:
            warn(f"注册失败：{json.dumps(resp, ensure_ascii=False)[:300]}")
            return 1
        say(f"注册成功 client_id={client_id}")

    verifier, challenge = openapi_generate_pkce()
    state = openapi_generate_state()

    # 端口占用快速失败（口径同 MCP 通道）：残留监听会用错误 verifier 接走授权码
    import socket
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", OPENAPI_CALLBACK_PORT))
    except OSError:
        warn(f"端口 {OPENAPI_CALLBACK_PORT} 已被占用（可能是残留的旧授权进程），"
             "请先结束再重试。")
        return 1
    finally:
        probe.close()

    OpenApiCallbackHandler.seen = False
    OpenApiCallbackHandler.code = ""
    OpenApiCallbackHandler.state = ""
    server = http.server.HTTPServer(("127.0.0.1", OPENAPI_CALLBACK_PORT),
                                    OpenApiCallbackHandler)
    server.timeout = 2  # 循环接收：每次处理一个请求，直到拿到授权码
    say(f"本地回调监听已启动：{OPENAPI_REDIRECT_URI}")

    auth_url = openapi_build_authorize_url(client_id, OPENAPI_REDIRECT_URI,
                                           state, challenge, scope)
    say(f"请在浏览器中完成富途账号授权（{TIMEOUT_SECONDS // 60} 分钟内有效）：")
    print(f"\n    {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        warn("未能自动打开浏览器，请手动复制上方链接。")

    warn("等待授权回调…")
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline and not OpenApiCallbackHandler.seen:
        server.handle_request()
    server.server_close()
    if not OpenApiCallbackHandler.seen or not OpenApiCallbackHandler.code:
        warn("超时未收到授权回调，请重试。")
        return 1
    try:
        openapi_validate_state(state, OpenApiCallbackHandler.state)
    except ValueError as e:
        warn(str(e))
        return 1

    say("收到授权码，换取 token…")
    resp = openapi_exchange_token(http_json, code=OpenApiCallbackHandler.code,
                                  code_verifier=verifier, client_id=client_id,
                                  redirect_uri=OPENAPI_REDIRECT_URI)
    if not resp.get("access_token"):
        warn(f"换取 token 失败：{json.dumps(resp, ensure_ascii=False)[:300]}")
        return 1
    cred = openapi_credential_update({**cred, "client_id": client_id},
                                     resp, int(time.time() * 1000))
    store.save(cred)
    say(f"OpenAPI 授权完成！凭据已存入 {store.path}（0600）")
    expires_at = cred.get("expires_at")
    if expires_at:
        say("access_token 有效期至 "
            + time.strftime("%H:%M", time.localtime(expires_at / 1000))
            + "，过期由 futu_openapi.OpenApiClient 自动用 refresh_token 续期（不轮换）。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="额外申请交易写权限 trade:write")
    ap.add_argument("--refresh", action="store_true", help="用 refresh_token 续期，免重新授权")
    ap.add_argument("--openapi", action="store_true",
                    help="配置 OpenAPI（REST）OAuth 2.1+PKCE 凭据"
                         "（写入 ~/.dsh/futu-openapi.json，0600）")
    ap.add_argument("--scope", default=OPENAPI_DEFAULT_SCOPE,
                    help="--openapi 授权 scope（默认含交易写，供交易链路使用）")
    args = ap.parse_args()
    if args.openapi:
        return cmd_openapi(args.scope)
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
    moment = record_expiry(resp)
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
