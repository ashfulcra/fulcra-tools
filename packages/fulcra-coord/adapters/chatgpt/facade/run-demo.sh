#!/usr/bin/env bash
# run-demo.sh — start the Fulcra Coordination write facade for the three-agent
# coordination demo, pointed at the /coordination-demo root.
#
# This is the hosted-ChatGPT path: the Custom GPT calls this facade's
# POST /coordination/report + GET /coordination/status. The facade uses the
# HOST's fulcra-api login for outbound Fulcra I/O, so this host must itself be
# fulcra-api-authed (run `fulcra-coord doctor` first — expect Remote access: OK).
#
# Secrets are read from the environment; nothing is hardcoded. The inbound
# bearer token MUST be supplied via FULCRA_COORD_FACADE_TOKEN (the same value
# you set as the Custom GPT Action's Bearer API key).
#
# Usage:
#   export FULCRA_COORD_FACADE_TOKEN="$(openssl rand -hex 32)"   # or your token
#   ./run-demo.sh                       # serves on :8787
#   FACADE_PORT=9000 ./run-demo.sh      # override the port
#   FULCRA_COORD_REMOTE_ROOT=/other ./run-demo.sh   # override the root
#
# Then expose it with a tunnel (see README "Demo deploy") and point the Custom
# GPT's OpenAPI server at the public URL.

set -euo pipefail

# --- Required: inbound bearer token (fail closed, never invent a secret) ------
if [[ -z "${FULCRA_COORD_FACADE_TOKEN:-}" ]]; then
  echo "ERROR: FULCRA_COORD_FACADE_TOKEN is not set." >&2
  echo "Generate one and export it before running, e.g.:" >&2
  echo '  export FULCRA_COORD_FACADE_TOKEN="$(openssl rand -hex 32)"' >&2
  echo "This is the same token you paste into the Custom GPT Action (Bearer API key)." >&2
  exit 1
fi

# --- Demo defaults (overridable) ----------------------------------------------
export FULCRA_COORD_REMOTE_ROOT="${FULCRA_COORD_REMOTE_ROOT:-/coordination-demo}"
FACADE_HOST="${FACADE_HOST:-0.0.0.0}"
FACADE_PORT="${FACADE_PORT:-8787}"

# Run from this script's directory so `app:app` resolves regardless of CWD.
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "Starting Fulcra Coordination facade for the demo:"
echo "  Coordination root : ${FULCRA_COORD_REMOTE_ROOT}"
echo "  Listening on      : http://${FACADE_HOST}:${FACADE_PORT}"
echo "  Inbound token     : set (FULCRA_COORD_FACADE_TOKEN)"
echo "  Outbound Fulcra    : uses THIS host's fulcra-api login"
echo
echo "Reminder: this host must be fulcra-api-authed (run 'fulcra-coord doctor')."
echo "Expose publicly with a tunnel, e.g.:"
echo "  cloudflared tunnel --url http://localhost:${FACADE_PORT}"
echo

exec uvicorn app:app --host "${FACADE_HOST}" --port "${FACADE_PORT}"
