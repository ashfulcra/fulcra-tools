# Onboarding directive

> **Canonical coord guide:** `packages/fulcra-coord/SKILL.md` (the fulcra-coord package in the fulcra-tools monorepo) — the runtime-agnostic when/how-to-use coord reference (quick-reference + load-bearing rules). This file is the Hermes-specific layer.

When a session starts, your first action is to run the `fulcra-onboarding`
skill (read its SKILL.md and follow it). The skill greets the user; you do not
add a separate greeting.

## Environment: uv and the Fulcra CLI are already installed

`uv` is preinstalled and on PATH, and the Fulcra CLI is already installed via
`uv tool install fulcra-api`. DO NOT install or reinstall either one. Skip any
"install uv" / prerequisite step the skill mentions (e.g. running the
`curl … astral.sh/uv/install.sh | sh` installer) — it is redundant here. Just
run `uv tool run fulcra-api …` directly.

To authenticate Fulcra, run this EXACT command (the wrapper is important):

    timeout 12 uv tool run fulcra-api auth login 2>&1 || true

It prints an authorization URL and a device code, then returns. Do NOT run a
bare `uv tool run fulcra-api auth login` without the `timeout … 2>&1 || true`
wrapper — unwrapped it blocks indefinitely and the URL never reaches the user.
Present the URL and the code to the user in chat and tell them to open the URL
in their own browser to create a new Fulcra account or sign in. Then poll
`uv tool run fulcra-api user-info` until it succeeds before continuing. Never
ask the user for a Fulcra token; the browser device flow is the only auth path.

## Fulcra Continuity handoff

You may receive a Fulcra Continuity checkpoint from another agent. Treat it as
durable resume state, even if you have no prior context. Read `objective`,
`decisions`, `open_questions`, `next_actions`, and `artifacts` before acting. If
the `fulcra-continuity` CLI is available, render the brief with
`fulcra-continuity resume <checkpoint.json>`; otherwise read the JSON directly.
Do not assume local paths from another agent exist in this sandbox. Resolve work
from URLs, Fulcra remote paths, coord task IDs, or repo/ref/commit/path tuples.
Before sandbox teardown or handoff, write a new checkpoint with portable
artifacts and no secrets.
