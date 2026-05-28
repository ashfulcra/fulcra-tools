#!/usr/bin/env bash
# Prove the built .app is self-contained: launch the BUNDLED daemon against
# an isolated config home (its own port + web-token, no collision with a
# running launchd daemon, no dev workspace on its path) and assert the
# frozen app discovers all plugins, serves the SPA, and serves the docs.
#
# This verifies BUNDLE COMPLETENESS — not Fulcra ingestion (that needs creds
# and is already proven against the dev daemon). A plugin appearing in
# /api/status proves its module imported inside the freeze.
set -euo pipefail
cd "$(dirname "$0")/.."                       # packages/menubar
APP="dist/Fulcra Collect.app"
BIN="$APP/Contents/MacOS/Fulcra Collect"
PORT=9393

test -x "$BIN" || { echo "FAIL: $BIN missing — build first"; exit 1; }

echo "=== bundled resources present? ==="
test -f "$APP/Contents/Resources/web-ui/dist/index.html" \
  || { echo "FAIL: web-ui/dist/index.html not bundled"; exit 1; }
test -f "$APP/Contents/Resources/docs/how-do-i-get-my-data.md" \
  || { echo "FAIL: docs page not bundled"; exit 1; }
echo "  web-ui/dist + docs present ✓"

# Isolated config home so the bundled daemon gets its own port + token and
# never touches ~/.config/fulcra-collect or collides with the live daemon.
HOME_DIR="$(mktemp -d)"
mkdir -p "$HOME_DIR/fulcra-collect"
printf '[daemon]\nweb_port = %s\n' "$PORT" > "$HOME_DIR/fulcra-collect/config.toml"
export FULCRA_COLLECT_HOME="$HOME_DIR/fulcra-collect"

echo "=== launch bundled daemon (isolated home, port $PORT) ==="
"$BIN" daemon >"$HOME_DIR/daemon.log" 2>&1 &
DPID=$!
cleanup() { kill "$DPID" 2>/dev/null || true; rm -rf "$HOME_DIR"; }
trap cleanup EXIT

for i in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 1
  [ "$i" = 30 ] && { echo "FAIL: daemon never served :$PORT"; echo "--- log ---"; cat "$HOME_DIR/daemon.log"; exit 1; }
done

echo "=== SPA renders? ==="
curl -fsS "http://127.0.0.1:$PORT/" | grep -q "Fulcra Collect" \
  || { echo "FAIL: SPA did not render"; exit 1; }
echo "  SPA served ✓"

TOK="$(cat "$FULCRA_COLLECT_HOME/web-token")"

echo "=== all plugins discovered via static manifest? ==="
N=$(curl -fsS -H "Authorization: Bearer $TOK" "http://127.0.0.1:$PORT/api/status" \
     | python3 -c "import sys,json; print(len(json.load(sys.stdin)['plugins']))")
echo "  plugins discovered: $N"
test "$N" -ge 17 || { echo "FAIL: expected >=17 plugins, got $N"; echo "--- log ---"; cat "$HOME_DIR/daemon.log"; exit 1; }

echo "=== in-app docs served? ==="
curl -fsS -H "Authorization: Bearer $TOK" \
  "http://127.0.0.1:$PORT/api/docs/how-do-i-get-my-data" >/dev/null \
  || { echo "FAIL: docs endpoint 404 in bundle"; exit 1; }
echo "  docs endpoint served ✓"

echo "ALL BUNDLE SMOKE CHECKS PASSED"
