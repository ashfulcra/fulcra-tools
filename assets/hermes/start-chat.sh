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

# Symlink the Fulcra CLI into /usr/bin so it's discoverable from ANY shell,
# including the stripped-PATH subshells Hermes's terminal tool spawns (those
# don't inherit /root/.local/bin and otherwise make the agent think the CLI
# isn't installed). uv tool installs to /root/.local/bin as root, hence the
# source path. Idempotent: ln -sf overwrites if the link already exists.
for _bin in fulcra fulcra-api; do
	if [ -x "/root/.local/bin/${_bin}" ]; then
		ln -sf "/root/.local/bin/${_bin}" "/usr/bin/${_bin}"
	fi
done

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

# Fetch the onboarding skill at boot, so skill updates on GitHub propagate to
# new sandboxes WITHOUT rebuilding the image. The image bakes a copy as a
# fallback if this fetch fails (GitHub down, bad branch, missing subpath). All
# three knobs are env-overridable per spawn:
#   FULCRA_SKILL_REPO     — repo URL              (default: fulcradynamics/agent-skills)
#   FULCRA_SKILL_BRANCH   — ref to clone          (default: main)
#   FULCRA_SKILL_SUBPATH  — subdir holding skill  (default: skills/fulcra-onboarding)
SKILL_REPO="${FULCRA_SKILL_REPO:-https://github.com/fulcradynamics/agent-skills}"
SKILL_BRANCH="${FULCRA_SKILL_BRANCH:-main}"
SKILL_SUBPATH="${FULCRA_SKILL_SUBPATH:-skills/fulcra-onboarding}"
if git clone --depth 1 --branch "${SKILL_BRANCH}" "${SKILL_REPO}" /tmp/agent-skills >/tmp/skill-fetch.log 2>&1 \
	&& [ -d "/tmp/agent-skills/${SKILL_SUBPATH}" ]; then
	mkdir -p /root/.hermes/skills/fulcra
	rm -rf /root/.hermes/skills/fulcra/fulcra-onboarding
	cp -r "/tmp/agent-skills/${SKILL_SUBPATH}" /root/.hermes/skills/fulcra/fulcra-onboarding
	echo "skill: fetched ${SKILL_REPO}@${SKILL_BRANCH}/${SKILL_SUBPATH}"
else
	echo "skill: boot fetch failed; using the copy baked into the image" >&2
fi
rm -rf /tmp/agent-skills 2>/dev/null || true

# Legacy-skill compatibility overrides. These were originally added to patch
# three bugs in the upstream fulcra-onboarding skill (SKILL.md asking the user
# to install uv, the prereq doc pushing curl|sh install, the discovery doc
# telling the agent to run a hanging bare auth-login). The upstream fix is in
# PR fulcradynamics/agent-skills#fix/preconfigured-env-and-reliable-auth.
# Until that PR merges (and even after, for sandboxes that fall back to a stale
# baked copy), these overrides are SELF-DISABLING: each `grep -q` matches only
# the OLD buggy text. Once the PR ships, the patterns no longer match and the
# overrides become true no-ops — no need to remove them.
SKILL_DIR="/root/.hermes/skills/fulcra/fulcra-onboarding"
if [ -f "${SKILL_DIR}/SKILL.md" ] \
	&& grep -q "verify and install \`uv\` if necessary, asking the user" "${SKILL_DIR}/SKILL.md"; then
	sed -i 's|.*verify and install `uv`.*|   - **SKIPPED** — `uv` and the Fulcra CLI are PREINSTALLED in this image. Do NOT install and do NOT ask the user for confirmation. Proceed directly to step 2.|' "${SKILL_DIR}/SKILL.md"
fi
if [ -f "${SKILL_DIR}/references/fulcra-onboarding-prerequisites.md" ] \
	&& grep -q "curl -LsSf https://astral.sh/uv/install.sh" "${SKILL_DIR}/references/fulcra-onboarding-prerequisites.md"; then
	cat > "${SKILL_DIR}/references/fulcra-onboarding-prerequisites.md" <<'PREREQ'
# Prerequisites — already satisfied in this environment

`uv` is preinstalled at `/root/.local/bin/uv` (on PATH).
The Fulcra CLI is preinstalled via `uv tool install fulcra-api`.

**DO NOT run any installer and DO NOT ask the user for confirmation.** This
prerequisite step is complete; proceed to the next step in the skill.
PREREQ
fi
if [ -f "${SKILL_DIR}/references/fulcra-onboarding-discovery.md" ] \
	&& grep -qE "ask for their permission.*check their Fulcra authentication|timeout 12 .*auth login" "${SKILL_DIR}/references/fulcra-onboarding-discovery.md"; then
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

Do **not** improvise the commands or the presentation in this section. The
device-flow login needs to keep polling auth0 in the background for the full
~10 minutes the code is valid — a foreground `timeout` would terminate that
polling and the user's sign-in would never reach disk. The procedure below
runs the login in the background and reads the URL + code from a log file
on disk, which IS reliably readable while the process keeps running.

**Step 2a — check current auth (no consent needed):**

    fulcra-api user-info

If it exits 0 and returns JSON, the user is already authenticated → skip to
step 3. If it exits non-zero, continue to 2b.

**Step 2b — start the login in the background and capture the URL + code
from its log file. Run EXACTLY this single line:**

    rm -f /tmp/fulcra-auth.log && nohup fulcra-api auth login > /tmp/fulcra-auth.log 2>&1 & for i in $(seq 1 10); do grep -q 'activate?user_code=' /tmp/fulcra-auth.log 2>/dev/null && break; sleep 1; done; cat /tmp/fulcra-auth.log

The output (from `cat /tmp/fulcra-auth.log`) contains:
  - an authorization URL (e.g. `https://fulcra.us.auth0.com/activate?user_code=XXXX-YYYY`)
  - a confirmation code (e.g. `XXXX-YYYY`)

Extract them and present to the user using EXACTLY this template. Render the
URL wrapped in **backticks** (inline code) — do **NOT** format it as a
markdown link `[text](url)`; the user must see the literal URL string so
they can verify the code matches and (if needed) copy/paste the URL.

> 🔐 Open this URL in your browser to sign in or create your Fulcra account:
>
> `https://fulcra.us.auth0.com/activate?user_code=XXXX-YYYY`
>
> Confirm the code on that page matches: **XXXX-YYYY**
>
> Reply "done" when you've finished signing in.

The `fulcra-api auth login` process started above is STILL RUNNING in the
background, polling auth0 for the user to complete the flow. **Do NOT kill it.
Do NOT start another one.** It will write credentials to disk and exit on its
own when the user finishes.

**Step 2c — when the user says "done", verify by running:**

    fulcra-api user-info

If it returns 0 with JSON → continue to step 3.
If it returns 401, the background poll hasn't received the token yet. Wait
3 seconds and retry `fulcra-api user-info`. Repeat up to 3 times.

**Do NOT start a new `auth login`** — there is already one running in the
background. Starting a new one creates a new code and invalidates the user's
current sign-in. If still 401 after 3 retries, ask the user to confirm they
clicked Confirm on the device-code page AND signed in to Fulcra.

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

# Suppress the dashboard's "Do you want to navigate to <url>? WARNING: This link
# could potentially be dangerous" popup that fires when a guest clicks the Fulcra
# auth URL we just put in chat — it's hard-baked in the dashboard's frontend
# bundle and breaks the demo UX. Inject a tiny shim into index.html that
# auto-OKs navigation confirms only; other dashboard confirms (e.g. delete
# session) still work. Idempotent + done AFTER the dashboard's build to avoid
# being overwritten.
DASH_INDEX=/usr/local/lib/hermes-agent/hermes_cli/web_dist/index.html
if [ -f "$DASH_INDEX" ] && ! grep -q '_fhd_oc' "$DASH_INDEX"; then
	sed -i 's|</head>|<script>const _fhd_oc=window.confirm;window.confirm=(m)=>(typeof m==="string"\&\&m.indexOf("navigate to")>=0)?true:_fhd_oc.call(window,m);</script></head>|' "$DASH_INDEX"
fi

# Caddy in the foreground on :8080 (the port exposed via the Daytona preview URL).
exec caddy run --config /opt/fhd/Caddyfile --adapter caddyfile
