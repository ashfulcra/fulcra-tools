#!/usr/bin/env bash
# Event-driven wake adapter for an OpenClaw Gateway. Intended as the fixed
# command passed to install-listener.sh --wake-cmd. The listener supplies team
# and agent metadata in the environment; this adapter never evaluates event
# content or accepts a command from the event payload.
set -euo pipefail

URL="${OPENCLAW_HOOK_URL:-http://127.0.0.1:18789/hooks/wake}"
TOKEN="${OPENCLAW_HOOK_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  TOKEN_FILE="${OPENCLAW_HOOK_TOKEN_FILE:-${HOME:?HOME is required when using the default token file}/.config/coord-engine/openclaw-hook-token}"
  [[ -f "$TOKEN_FILE" ]] || {
    echo "openclaw wake: set OPENCLAW_HOOK_TOKEN or create $TOKEN_FILE (mode 0600)" >&2
    exit 2
  }
  # Secrets in a scheduler environment or plist are easy to expose. Prefer a
  # root/user-readable file and reject group/world permission bits.
  MODE="$(stat -f '%Lp' "$TOKEN_FILE" 2>/dev/null || stat -c '%a' "$TOKEN_FILE" 2>/dev/null || true)"
  case "$MODE" in
    600|400) ;;
    *) echo "openclaw wake: token file must have mode 0600 or 0400" >&2; exit 2 ;;
  esac
  TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
fi
[[ -n "$TOKEN" ]] || { echo "openclaw wake: token is empty" >&2; exit 2; }
TEAM="${COORD_LISTENER_TEAM:-unknown}"
AGENT="${COORD_LISTENER_AGENT:-unknown}"
DEGRADED="${COORD_LISTENER_DEGRADED:-0}"

case "$URL" in
  http://*|https://*) ;;
  *) echo "openclaw wake: OPENCLAW_HOOK_URL must be http(s)" >&2; exit 2 ;;
esac

PAYLOAD="$(python3 - "$TEAM" "$AGENT" "$DEGRADED" <<'PY'
import json, sys
team, agent, degraded = sys.argv[1:]
reason = "listener degradation; apply targeted fallback" if degraded == "1" else "new coordination event"
print(json.dumps({
    "text": f"Coord wake for {agent} on team {team}: {reason}. Resume continuity, run the authoritative briefing once, and handle surfaced work.",
    "mode": "now",
}, separators=(",", ":")))
PY
)"

curl --fail --silent --show-error --max-time 15 \
  -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "$PAYLOAD" >/dev/null
