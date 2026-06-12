#!/usr/bin/env bash
# Send a test alert to the embedded vROps webhook listener.
#
# Reads WEBHOOK_* from .env so it stays in sync with the running bot.
# Usage:
#   ./scripts/send-test-alert.sh                         # default CRITICAL "High CPU" alert
#   ./scripts/send-test-alert.sh "High Memory" WARNING vm-02 r-002
#   HOST=10.0.0.5 ./scripts/send-test-alert.sh           # target a remote bot host
#
# Args (all optional, positional):
#   $1 alert name   (default "High CPU on guest")
#   $2 criticality  (INFORMATION|WARNING|IMMEDIATE|CRITICAL, default CRITICAL)
#   $3 resourceName (default vm-01)
#   $4 resourceId   (default a real-looking uuid)
set -euo pipefail

# --- load WEBHOOK_* from .env (next to this script's parent dir) -------------
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' ; }

PORT="${WEBHOOK_PORT:-$(get WEBHOOK_PORT)}";  PORT="${PORT:-8088}"
TOKEN="${WEBHOOK_TOKEN:-$(get WEBHOOK_TOKEN)}"
WPATH="${WEBHOOK_PATH:-$(get WEBHOOK_PATH)}"; WPATH="${WPATH:-/vrops/alert}"
HOST="${HOST:-127.0.0.1}"

# --- alert fields ------------------------------------------------------------
NAME="${1:-High CPU on guest}"
CRIT="${2:-CRITICAL}"
RNAME="${3:-vm-01}"
RID="${4:-9b2c1f7a-0000-4d00-8a11-deadbeef0001}"
NOW="$(date +%s)000"   # epoch millis

read -r -d '' PAYLOAD <<JSON || true
{
  "alertId": "alert-$(date +%s)",
  "alertName": "${NAME}",
  "criticality": "${CRIT}",
  "status": "ACTIVE",
  "resourceId": "${RID}",
  "resourceName": "${RNAME}",
  "startTimeUTC": ${NOW},
  "info": "Synthetic test alert from send-test-alert.sh"
}
JSON

URL="http://${HOST}:${PORT}${WPATH}"
echo "POST ${URL}  (criticality=${CRIT})"
# -s silent, -S show errors, -w prints the HTTP status on its own line.
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  -X POST "${URL}" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: ${TOKEN}" \
  --data "${PAYLOAD}"

echo "Expect: HTTP 202 (accepted) -> a summary lands in your VROPS_ALERT_CHANNEL shortly."
echo "Tip: wrong token -> HTTP 401; the bot must be running with WEBHOOK_ENABLED=true."
