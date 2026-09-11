#!/usr/bin/env bash
# 富途远程 MCP OAuth 授权助手（Authorization Code + PKCE，公开客户端）
#
# 用法：
#   ./scripts/futu-auth.sh            # 默认只授只读 scope（推荐）
#   ./scripts/futu-auth.sh --write    # 额外申请交易写权限 trade:write
#
# 流程：动态注册客户端 → 生成本地回调监听 → 打开富途授权页 → 你登录确认
#       → 自动用授权码换 token → 存入 ~/.dsh/futu-token（权限600）
set -euo pipefail

DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
TOKEN_FILE="$DSH_HOME/futu-token"
REFRESH_FILE="$DSH_HOME/futu-refresh"
CLIENT_FILE="$DSH_HOME/futu-client-id"
CALLBACK_PORT=18923
REDIRECT_URI="http://127.0.0.1:${CALLBACK_PORT}/callback"
REGISTER_URL="https://webapi.futunn.com/oauth2/register"
AUTHORIZE_URL="https://webapi.futunn.com/oauth2/authorize/confirm"
TOKEN_URL="https://webapi.futunn.com/oauth2/token"
SCOPES="quote:read trade:read accid:*"
[[ "${1:-}" == "--write" ]] && SCOPES="quote:read trade:read trade:write accid:*"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m ==\033[0m %s\n' "$*"; }
command -v curl >/dev/null || { echo "需要 curl"; exit 1; }
command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }
mkdir -p "$DSH_HOME"

# 1. 客户端注册（首次）或复用
if [[ -s "$CLIENT_FILE" ]]; then
  CLIENT_ID="$(cat "$CLIENT_FILE")"
  say "复用已注册客户端 $CLIENT_ID"
else
  say "向富途注册 OAuth 客户端…"
  RESP="$(curl -s -m 20 -X POST "$REGISTER_URL" -H "Content-Type: application/json" -d "{
    \"client_name\":\"dsh-trading-agents\",
    \"redirect_uris\":[\"$REDIRECT_URI\"],
    \"grant_types\":[\"authorization_code\",\"refresh_token\"],
    \"response_types\":[\"code\"],
    \"token_endpoint_auth_method\":\"none\"}")"
  CLIENT_ID="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["client_id"])')"
  printf '%s' "$CLIENT_ID" > "$CLIENT_FILE"
  say "注册成功 client_id=$CLIENT_ID"
fi

# 2. PKCE（S256）
VERIFIER="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
CHALLENGE="$(printf '%s' "$VERIFIER" | python3 -c 'import sys,hashlib,base64; print(base64.urlsafe_b64encode(hashlib.sha256(sys.stdin.buffer.read()).digest()).rstrip(b"=").decode())')"

# 3. 本地回调监听（后台，等授权码，最长5分钟）
CODE_FILE="$(mktemp)"
python3 - "$CALLBACK_PORT" "$CODE_FILE" <<'PY' &
import http.server, sys, urllib.parse, contextlib
port, out = int(sys.argv[1]), sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = q.get("code", [""])[0]
        with open(out, "w") as f: f.write(code)
        self.send_response(200); self.end_headers()
        self.wfile.write("授权成功！请回到终端查看结果，可以关闭此页面。".encode())
    def log_message(self, *a): pass
with contextlib.closing(http.server.HTTPServer(("127.0.0.1", port), H)) as s:
    s.handle_request()
PY
LISTENER_PID=$!
trap 'kill $LISTENER_PID 2>/dev/null || true' EXIT

# 4. 生成授权链接并打开
AUTH_URL="${AUTHORIZE_URL}?response_type=code&client_id=${CLIENT_ID}&redirect_uri=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$REDIRECT_URI")&scope=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$SCOPES")&code_challenge=${CHALLENGE}&code_challenge_method=S256"
say "请在浏览器中完成富途账号授权（5分钟内有效）："
echo
echo "    $AUTH_URL"
echo
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$AUTH_URL" 2>/dev/null || true
elif command -v open >/dev/null 2>&1; then open "$AUTH_URL" 2>/dev/null || true
else warn "请手动复制上方链接到浏览器打开"; fi

warn "等待授权回调…"
for i in $(seq 1 300); do [[ -s "$CODE_FILE" ]] && break; sleep 1; done
[[ -s "$CODE_FILE" ]] || { warn "超时未收到授权回调，请重试。"; exit 1; }
CODE="$(cat "$CODE_FILE")"
say "收到授权码，换取 token…"

# 5. 换取 token 并保存
RESP="$(curl -s -m 20 -X POST "$TOKEN_URL" -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "code=$CODE" \
  --data-urlencode "redirect_uri=$REDIRECT_URI" \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "code_verifier=$VERIFIER")"
ACCESS="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))')"
REFRESH="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("refresh_token",""))')"
[[ -n "$ACCESS" ]] || { warn "换取 token 失败：$RESP"; exit 1; }

umask 077
printf '%s' "$ACCESS" > "$TOKEN_FILE"
[[ -n "$REFRESH" ]] && printf '%s' "$REFRESH" > "$REFRESH_FILE"
say "授权完成！token 已存入 $TOKEN_FILE（权限600）"
warn "重启 harness 会话后，富途工具（mcp__futu__*）即可用。"
