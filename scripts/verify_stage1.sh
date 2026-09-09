#!/usr/bin/env bash
# Stage 1 verification: is the MCP endpoint publicly reachable over HTTPS, and
# does it answer a PLAIN 401 that leads a client nowhere?
#
# Asserts the ABSENCE of OAuth discovery on purpose. A WWW-Authenticate header or
# a live .well-known route is a FAILURE at this stage -- either one would send
# Claude on to authorization-server discovery, which is stage 2.
#
#   ./scripts/verify_stage1.sh https://mcp.example.com/mcp
set -u

URL="${1:-}"
[ -z "$URL" ] && { echo "usage: $0 https://host/mcp"; exit 2; }

BASE="${URL%/mcp}"
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

hdrs=$(mktemp); body=$(mktemp)
trap 'rm -f "$hdrs" "$body"' EXIT

echo "== 1. Reachable over HTTPS =="
case "$URL" in
  https://*) ok "URL is https" ;;
  http://127.0.0.1*|http://localhost*) echo "  SKIP  local run, https not required" ;;
  *) bad "URL is not https -- Claude requires TLS" ;;
esac

code=$(curl -s -o "$body" -D "$hdrs" -w '%{http_code}' --max-time 20 \
  -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"stage1-verify","version":"0"}}}')

if [ "$code" = "000" ]; then
  bad "endpoint unreachable (DNS, TLS or firewall)"
  echo; echo "$PASS passed, $((FAIL)) failed"; exit 1
fi
ok "endpoint answered (HTTP $code)"

echo
echo "== 2. Unauthenticated request gets 401 =="
case "$code" in
  401) ok "401 Unauthorized with no token" ;;
  421) bad "421 Misdirected Request -- add this hostname to ALLOWED_HOSTS" ;;
  *)   bad "expected 401, got $code"; head -c 300 "$body"; echo ;;
esac

echo
echo "== 3. No OAuth challenge (must be absent at this stage) =="
if grep -qi '^www-authenticate:' "$hdrs"; then
  bad "WWW-Authenticate is present -- Claude will follow it into OAuth discovery"
  grep -i '^www-authenticate:' "$hdrs" | tr -d '\r' | sed 's/^/        /'
  echo "        set OAUTH_CHALLENGE_ENABLED=false and restart"
else
  ok "no WWW-Authenticate header"
fi

echo
echo "== 4. No discovery endpoints exposed =="
for wk in \
  "/.well-known/oauth-protected-resource/mcp" \
  "/.well-known/oauth-protected-resource" \
  "/.well-known/oauth-authorization-server"
do
  wcode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE$wk")
  if [ "$wcode" = "200" ]; then
    bad "$wk -> 200 (should not exist yet)"
  else
    ok "$wk -> $wcode"
  fi
done

echo
echo "== 5. Non-MCP routes still work =="
hcode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE/healthz")
[ "$hcode" = "200" ] && ok "/healthz -> 200" || bad "/healthz -> $hcode"

echo
echo "-------------------------------------------"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
