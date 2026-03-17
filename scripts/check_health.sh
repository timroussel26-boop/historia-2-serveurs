#!/usr/bin/env bash
set -euo pipefail

AI_URL="${AI_URL:-http://127.0.0.1:4200}"
INVENTORY_URL="${INVENTORY_URL:-http://127.0.0.1:4100}"

AI_JSON="$(curl -fsS "$AI_URL/api/health")"
INV_JSON="$(curl -fsS "$INVENTORY_URL/api/health")"

echo "$AI_JSON" | python3 -c 'import sys,json; p=json.load(sys.stdin); assert p.get("status") == "ok", "AI KO"; print("AI ok")'
echo "$INV_JSON" | python3 -c 'import sys,json; p=json.load(sys.stdin); assert p.get("status") == "ok", "Inventory KO"; print("Inventory ok")'

if curl -fsS "$AI_URL/api/admin/ops" >/dev/null 2>&1; then
  echo "Admin ops endpoint ok"
else
  echo "Admin ops endpoint inaccessible (normal hors allowlist IP)"
fi
