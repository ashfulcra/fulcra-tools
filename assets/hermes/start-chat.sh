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

# Override the onboarding skill's prereq instructions so the agent doesn't waste
# a turn (or pop a confirmation pop-up) re-installing what the image already has.
# We patch TWO places — both are needed:
#
#   1. SKILL.md step 1 itself says "verify and install uv if necessary, asking
#      the user for confirmation." Reading just the prereq reference file isn't
#      enough — the agent reads SKILL.md, asks the user "shall I install?", they
#      say yes, and it installs anyway. So we sed-replace that bullet inline.
#   2. The prereq reference file (which the agent reads when following SKILL.md)
#      is replaced with a clear "already satisfied — DO NOT install" notice.
#
# Both are idempotent and run every boot.
SKILL_DIR="/root/.hermes/skills/fulcra/fulcra-onboarding"
if [ -f "${SKILL_DIR}/SKILL.md" ]; then
	sed -i 's|.*verify and install `uv`.*|   - **SKIPPED** — `uv` and the Fulcra CLI are PREINSTALLED in this image. Do NOT install and do NOT ask the user for confirmation. Proceed directly to step 2.|' "${SKILL_DIR}/SKILL.md"
fi
if [ -d "${SKILL_DIR}/references" ]; then
	cat > "${SKILL_DIR}/references/fulcra-onboarding-prerequisites.md" <<'PREREQ'
# Prerequisites — already satisfied in this environment

`uv` is preinstalled at `/root/.local/bin/uv` (on PATH).
The Fulcra CLI is preinstalled via `uv tool install fulcra-api`.

**DO NOT run any installer and DO NOT ask the user for confirmation.** This
prerequisite step is complete; proceed to the next step in the skill.
PREREQ

	# Override the discovery reference. The upstream version's main workflow tells
	# the agent to run a bare `fulcra-api auth login`, which is a hanging command —
	# the agent then can't extract the URL from the running process and the user
	# never sees the login link. The right pattern is in a "Pitfalls" footnote that
	# the agent ignores. We replace it with a focused auth procedure that runs the
	# command in foreground with a short timeout, guaranteeing the URL is captured
	# and surfaced to the user. Intent-discovery brainstorming preserved.
	cat > "${SKILL_DIR}/references/fulcra-onboarding-discovery.md" <<'DISCOVERY'
---
name: fulcra-onboarding-discovery
description: "Handles intent discovery and authentication for new Fulcra users."
---

# Fulcra Onboarding: Discovery

**Tone Reminder:** High energy, concise, emoji-friendly. Punchy messages; no walls of text.

## 1. Intent discovery (pre-auth)

Before any auth, seed a quick brainstorm with 2-3 concrete examples of what
Fulcra can track. Personalize if you have memory of this user; otherwise pick
from these defaults: ☕ coffee intake, 📚 books read, 🏃 fitness/steps, 💼
deep work hours, 😴 sleep quality. Ask the user which of those (or something
else of their own) excites them. If they're vague ("just trying it out"),
pick one for them and keep moving.

## 2. Authentication — RUN EXACTLY THIS PROCEDURE

Do **not** improvise the commands in this section. Do **not** ask the user
for permission before checking auth — just run it.

**Step 2a — check current auth:**

    fulcra-api user-info

If it exits 0 and returns JSON, the user is authenticated → skip to step 3.
If it exits non-zero, continue to 2b.

**Step 2b — generate the login link. Run EXACTLY this command (the timeout +
2>&1 are required — they make the command print the URL and code to stdout
and return, instead of hanging in the background where the agent can't see
the output):**

    timeout 12 fulcra-api auth login 2>&1 || true

The output will contain two pieces you MUST relay to the user immediately,
verbatim, in chat:
  - an authorization URL (e.g. `https://fulcra.us.auth0.com/activate?user_code=XXXX-YYYY`)
  - a user code (e.g. `XXXX-YYYY`)

Present them like:

> 🔐 Open this URL in your browser to sign in / create your Fulcra account:
> **<URL>**
>
> Confirm the code on that page matches: **<CODE>**
>
> Once you've finished, just say "done" and we'll continue. 🚀

DO NOT run a bare `fulcra-api auth login` (no timeout / no 2>&1). DO NOT run
it as a background process. DO NOT poll a process ID for output. The single
foreground `timeout 12 … 2>&1` call above is the only correct invocation.

**Step 2c — wait for the user to confirm they finished**, then verify:

    fulcra-api user-info

Repeat with a short pause between calls if needed; do not loop more than a
few times. Once it succeeds, continue.

## 3. Proactive suggestions (post-auth)

Now that they're authenticated, suggest 2-3 concrete annotation types they
could start tracking tied to the intent they shared in step 1. Keep momentum;
move quickly toward the "wow" of writing/reading a first annotation.

## Handoff

Once authenticated AND a small set of annotations identified, hand back to
the main `fulcra-onboarding` flow for data modeling.
DISCOVERY
fi

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
