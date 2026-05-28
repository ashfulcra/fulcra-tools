#!/usr/bin/env bash
# Boot the guest-facing chat for one sandbox.
# WHY this shape: the Hermes dashboard is the guest UI, but it must be bound to
# 127.0.0.1 and fronted by Caddy (which 403s the admin/key endpoints). --insecure
# is required for the chat PTY session to go live; --tui exposes the Chat tab;
# --skip-build uses the web bundle prebuilt into the image. Caddy runs in the
# foreground to keep this process (and thus the backgrounded dashboard) alive.
set -euo pipefail

# Apply the runtime model choice if provided (key is injected separately into ~/.hermes/.env).
if [ -n "${OPENROUTER_MODEL:-}" ]; then
	hermes config set model.default "${OPENROUTER_MODEL}" || true
fi

# Start the dashboard, localhost-only, in the background.
HERMES_DASHBOARD_TUI=1 nohup hermes dashboard \
	--host 127.0.0.1 --port 9119 --no-open --insecure --skip-build \
	> /tmp/dash.log 2>&1 &

# Wait until the dashboard answers before fronting it with Caddy.
for _ in $(seq 1 40); do
	if curl -sf -o /dev/null http://127.0.0.1:9119/api/status; then
		break
	fi
	sleep 1
done

# Caddy in the foreground on :8080 (the port exposed via the Daytona preview URL).
exec caddy run --config /opt/fhd/Caddyfile --adapter caddyfile
