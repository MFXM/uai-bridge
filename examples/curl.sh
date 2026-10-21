#!/usr/bin/env bash
# Smoke tests against a running bridge. Usage: BRIDGE_KEY=... ./examples/curl.sh
set -euo pipefail
URL="${BRIDGE_URL:-http://127.0.0.1:8000}"
H="Authorization: Bearer ${BRIDGE_KEY:?set BRIDGE_KEY}"

curl -s "$URL/health"; echo
curl -s -H "$H" "$URL/v1/models" | head -c 400; echo
curl -s -H "$H" -H "Content-Type: application/json" "$URL/v1/chat/completions" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Say hello in German."}],"max_tokens":50}'
echo
