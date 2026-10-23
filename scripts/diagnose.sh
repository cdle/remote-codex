#!/bin/bash
# One-shot health check for the remote-codex stack.
# Prints PASS/FAIL per layer with concrete remediation hints.
set -uo pipefail

say() { printf "%-46s %s\n" "$1" "$2"; }

echo "=== 1. services ==="
for s in chatgpt-split codex-app-server; do
    st=$(systemctl is-active "$s" 2>/dev/null || echo missing)
    [ "$st" = active ] && say "$s" "PASS active" || say "$s" "FAIL $st -> systemctl start $s"
done

echo "=== 2. hosts hijack ==="
if grep -q "127.0.0.1 chatgpt.com" /etc/hosts && grep -q "127.0.0.1 api.openai.com" /etc/hosts; then
    say "hosts" "PASS"
else
    say "hosts" "FAIL -> add 127.0.0.1 chatgpt.com / api.openai.com"
fi

echo "=== 3. listener ==="
ss -tlnp 2>/dev/null | grep -q ':443 ' && say "split proxy :443" "PASS" \
    || say "split proxy :443" "FAIL"

echo "=== 4. TLS + models catalogue ==="
OUT=$(timeout 15 curl -s --cacert /opt/chatgpt-mitm/ca.crt \
    https://chatgpt.com/backend-api/codex/models 2>/dev/null)
if [ -n "$OUT" ]; then
    N=$(printf '%s' "$OUT" | grep -o '"id"' | wc -l)
    G=$(printf '%s' "$OUT" | grep -c '"id":"gpt' || true)
    say "models via chatgpt.com" "PASS ($N models, $N gpt)"
else
    say "models via chatgpt.com" "FAIL (empty response)"
fi

echo "=== 5. inference (non-streaming) ==="
BODY='{"model":"cn:step-5-preview","stream":false,"input":[{"role":"user","content":"ping"}]}'
CODE=$(timeout 60 curl -s -o /dev/null -w '%{http_code}' \
    --cacert /opt/chatgpt-mitm/ca.crt -X POST \
    https://chatgpt.com/backend-api/codex/responses \
    -H 'Content-Type: application/json' -d "$BODY" 2>/dev/null)
[ "$CODE" = 200 ] && say "inference non-stream" "PASS" || say "inference non-stream" "FAIL http=$CODE"

echo "=== 6. remote-control websocket ==="
PID=$(systemctl show -p MainPID --value codex-app-server 2>/dev/null)
if [ -n "$PID" ] && ss -tnp 2>/dev/null | grep -q "pid=$PID"; then
    say "remote-control upstream" "PASS (has connections)"
else
    say "remote-control upstream" "FAIL (no upstream connection)"
fi

echo "=== 7. recent split errors ==="
tail -20 /opt/chatgpt-mitm/split.log 2>/dev/null | grep -c "handle error" | \
    xargs -I{} echo "handle errors in last 20 lines: {}"
