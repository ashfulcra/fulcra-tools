#!/usr/bin/env bash
# Boot the guest-facing chat for one Vercel Sandbox.
#
# Identical in shape to the Daytona version (fulcra-hermes-daytona), with three
# environment-specific deltas:
#   - Runs as the `vercel-sandbox` user (uid 1000-ish) with sudo, not root.
#     Home is $HOME (NOT /root). uv installs to $HOME/.local/bin.
#   - Hermes was installed root via sudo at build time, so /usr/local/bin/hermes
#     and /usr/local/lib/hermes-agent exist. Node lives at /usr/local/lib/hermes-agent/node/bin
#     when installed root (not $HOME/.hermes/node/bin as on Daytona root install).
#   - HERMES_HOME is configured to $HOME/.hermes so configs/skills/sessions are
#     per-user under /vercel/sandbox/.hermes.
set -euo pipefail

# Make sure uv + Hermes-bundled node are on PATH for any subshell the agent's
# terminal tool spawns (those don't inherit our login PATH).
export PATH="$HOME/.local/bin:/usr/local/lib/hermes-agent/node/bin:${PATH}"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

# Symlink the Fulcra CLI into /usr/bin so it's discoverable from ANY shell.
for _bin in fulcra fulcra-api; do
	if [ -x "$HOME/.local/bin/${_bin}" ]; then
		sudo ln -sf "$HOME/.local/bin/${_bin}" "/usr/bin/${_bin}"
	fi
done

# Bypass the in-chat dangerous-command approval prompts. The `approvals.mode=yolo`
# config key alone does NOT do this — Hermes only checks the HERMES_YOLO_MODE env
# var (it's exactly what the --yolo flag sets).
export HERMES_YOLO_MODE=1

# Apply the runtime model choice if provided (key is injected separately into
# $HOME/.hermes/.env).
if [ -n "${OPENROUTER_MODEL:-}" ]; then
	hermes config set model.default "${OPENROUTER_MODEL}" || true
fi

# If we're routing through a LiteLLM gateway (key-leak architecture), point
# Hermes at it. Without LITELLM_URL set, the agent uses OPENROUTER_API_KEY
# directly against openrouter.ai (baseline / capped-key model).
if [ -n "${LITELLM_URL:-}" ]; then
	hermes config set model.provider custom || true
	hermes config set model.base_url "${LITELLM_URL}" || true
fi

# Fetch the onboarding skill at boot, so skill updates on GitHub propagate to
# new sandboxes WITHOUT rebuilding the snapshot. The snapshot bakes a copy as a
# fallback if this fetch fails.
SKILL_REPO="${FULCRA_SKILL_REPO:-https://github.com/fulcradynamics/agent-skills}"
SKILL_BRANCH="${FULCRA_SKILL_BRANCH:-main}"
SKILL_SUBPATH="${FULCRA_SKILL_SUBPATH:-skills/fulcra-onboarding}"
if git clone --depth 1 --branch "${SKILL_BRANCH}" "${SKILL_REPO}" /tmp/agent-skills >/tmp/skill-fetch.log 2>&1 \
	&& [ -d "/tmp/agent-skills/${SKILL_SUBPATH}" ]; then
	mkdir -p "$HOME/.hermes/skills/fulcra"
	rm -rf "$HOME/.hermes/skills/fulcra/fulcra-onboarding"
	cp -r "/tmp/agent-skills/${SKILL_SUBPATH}" "$HOME/.hermes/skills/fulcra/fulcra-onboarding"
	echo "skill: fetched ${SKILL_REPO}@${SKILL_BRANCH}/${SKILL_SUBPATH}"
else
	echo "skill: boot fetch failed; using the copy baked into the snapshot" >&2
fi
rm -rf /tmp/agent-skills 2>/dev/null || true

# Start the dashboard, localhost-only, in the background. --insecure is REQUIRED
# for the chat PTY to go live (still bound to 127.0.0.1, only reachable via Caddy).
HERMES_DASHBOARD_TUI=1 nohup hermes dashboard \
	--host 127.0.0.1 --port 9119 --no-open --insecure \
	> /tmp/dash.log 2>&1 &

# Wait for the dashboard to answer before fronting it with Caddy.
for _ in $(seq 1 40); do
	if curl -sf -o /dev/null http://127.0.0.1:9119/api/status; then
		break
	fi
	sleep 1
done

# Caddy in the foreground on :8080 (exposed via Vercel's port mapping).
exec caddy run --config "$HOME/fhv-assets/Caddyfile" --adapter caddyfile
