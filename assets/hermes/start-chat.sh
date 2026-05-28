#!/usr/bin/env bash
# Boot the guest-facing chat for one sandbox.
# WHY this shape: the Hermes dashboard is the guest UI, but it must be bound to
# 127.0.0.1 and fronted by Caddy (which 403s the admin/key endpoints). --insecure
# is required for the chat PTY session to go live; --tui exposes the Chat tab.
# The dashboard builds its web bundle on first launch (~15s); the health-poll
# below waits for it. Caddy runs in the foreground to keep this process (and
# thus the backgrounded dashboard) alive.
set -euo pipefail

# Ensure uv and the Hermes-bundled Node are on PATH — the dashboard shells out to
# npm to build its web UI on first launch, and this script runs in a non-login
# shell that wouldn't otherwise have them.
export PATH="/root/.local/bin:/root/.hermes/node/bin:${PATH}"

# Bypass the in-chat dangerous-command approval prompts. The `approvals.mode=yolo`
# config key alone does NOT do this — Hermes only checks the HERMES_YOLO_MODE env
# var (it's exactly what the --yolo flag sets). We export it here so the dashboard
# and the `hermes --tui` chat it spawns both inherit it; otherwise guests get a
# "[HIGH] approval required" prompt (e.g. on the uv installer's curl | sh) and have
# to click "Always allow". The sandbox is ephemeral + isolated, so this is fine.
export HERMES_YOLO_MODE=1

# Apply the runtime model choice if provided (key is injected separately into ~/.hermes/.env).
if [ -n "${OPENROUTER_MODEL:-}" ]; then
	hermes config set model.default "${OPENROUTER_MODEL}" || true
fi

# Fetch the latest onboarding skill at boot, so skill updates on GitHub propagate
# to new sandboxes WITHOUT rebuilding the image. The image still bakes a copy,
# which stays in place as a fallback if this fetch fails (GitHub down, bad branch,
# missing subpath). Repo/subpath are overridable via env for swapping the skill.
SKILL_REPO="${FULCRA_SKILL_REPO:-https://github.com/fulcradynamics/agent-skills}"
SKILL_SUBPATH="${FULCRA_SKILL_SUBPATH:-skills/fulcra-onboarding}"
if git clone --depth 1 "${SKILL_REPO}" /tmp/agent-skills >/tmp/skill-fetch.log 2>&1 \
	&& [ -d "/tmp/agent-skills/${SKILL_SUBPATH}" ]; then
	mkdir -p /root/.hermes/skills/fulcra
	rm -rf /root/.hermes/skills/fulcra/fulcra-onboarding
	cp -r "/tmp/agent-skills/${SKILL_SUBPATH}" /root/.hermes/skills/fulcra/fulcra-onboarding
	echo "skill: fetched latest from ${SKILL_REPO}/${SKILL_SUBPATH}"
else
	echo "skill: boot fetch failed; using the copy baked into the image" >&2
fi
rm -rf /tmp/agent-skills 2>/dev/null || true

# Start the dashboard, localhost-only, in the background.
HERMES_DASHBOARD_TUI=1 nohup hermes dashboard \
	--host 127.0.0.1 --port 9119 --no-open --insecure \
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
