#!/usr/bin/env bash
# Create a Tailored AI from local documents (needs BRIDGE_ENABLE_KB=true).
set -euo pipefail
URL="${BRIDGE_URL:-http://127.0.0.1:8000}"
curl -s -H "Authorization: Bearer ${BRIDGE_KEY:?set BRIDGE_KEY}" "$URL/v1/uai/experts" \
  -F name="Course helper" \
  -F summary="Answers questions about the course notes" \
  -F system_prompt="Answer only from the provided documents and cite them." \
  -F files=@notes.pdf
# -> {"status":"ready","tailored_ai_id":"...","model":"tailored:<id>", ...}
