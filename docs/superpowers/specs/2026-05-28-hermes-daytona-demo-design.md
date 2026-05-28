# Hermes-on-Daytona Demo — Design

**Date:** 2026-05-28
**Status:** Approved (design); pending spec review → implementation plan
**Owner:** ash@fulcradynamics.com

## Purpose

Give a limited, hand-picked set of people (a mix of technical and
non-technical — investors, partners, prospects) a **preconfigured Hermes
agent they can "press play" on** to experience Fulcra. Each person runs
their **own isolated, ephemeral** agent; Fulcra (the company) pays for the
compute; there is **no sales cycle and no monthly minimum** (Daytona is
self-serve, pay-per-use).

This is a demo vehicle, not a production product. Optimize for a smooth
first five minutes and a clear narrative, not for scale or hardening.

## The thesis (guiding principle)

**The agent is ephemeral; the memory is permanent via Fulcra.**

A guest presses play, gets a throwaway Hermes agent, and the agent walks
them through creating (or signing into) **their own** Fulcra account.
Everything the agent learns about them persists in *their* Fulcra account
and outlives the sandbox. When the sandbox is torn down, the agent is
gone but the memory remains — and would be available to any future agent
they point at their Fulcra account.

Two design consequences follow directly from the thesis:

1. **Zero meaningful persistence in the sandbox.** The sandbox is
   disposable. Persistence is Fulcra's job, not the sandbox's.
2. **No Fulcra credentials anywhere in our infrastructure.** Each guest
   authenticates with their own browser via the Fulcra device-code flow.
   We never hold, bake, or inject a Fulcra token.

## Audience & access model

- **Audience:** mixed technical / non-technical.
- **Access:** the operator (Fulcra) spawns one sandbox per invited person
  and hands each a signed, hard-to-guess preview URL. No allowlist UI in
  this phase; access control is "you have the link or you don't," scoped
  to a small invite list. A self-serve allowlist launcher is explicitly
  **out of scope** for this phase (see Non-goals).
- **Billing:** all compute runs on Fulcra's Daytona account (owner-paid by
  construction). Guests need no account on Daytona, OpenRouter, or any of
  our infra. They only create a Fulcra account, which is the point.

## Platform decision (context, already made)

Daytona was selected after a platform survey. Rationale captured here so
the choice is legible to a future reader:

- **Self-serve, no monthly minimum, pay-per-use** — rules out the
  enterprise hands-on-lab vendors (Instruqt/CloudShare/Strigo), which are
  sales-led with annual minimums.
- **Owner-paid per-user isolated sandboxes by construction** — our API
  key pays for every sandbox; guests need no account.
- **Native Hermes backend**, signed preview URLs (paste-a-link UX), and
  `auto_stop_interval` control for session lifetime.
- **Declarative image builder** — we can define the snapshot without a
  local Docker daemon (neither Docker nor the Daytona CLI is installed on
  the build machine).

## Agent framework (context, already made)

Hermes Agent (Nous Research). Chosen by the user. Runs on **OpenRouter**
(one shared key for all sandboxes), using a **strong model** for reliable
agentic skill-following and shell use. The exact model id is a config var
(`OPENROUTER_MODEL`), defaulting to a strong current Claude model on
OpenRouter so it can be swapped without rebuilding. Guest-facing surface
is the **Hermes web dashboard / chat**, exposed via a signed Daytona
preview URL.

## How Fulcra auth works (verified)

The `fulcra-onboarding` skill
(`github.com/fulcradynamics/agent-skills`, path
`skills/fulcra-onboarding`) drives a **device-code browser flow**:

- `uv tool run fulcra-api auth login` prints an **authorization URL and a
  device code**, then blocks/polls while the user authenticates in their
  own browser.
- `uv tool run fulcra-api user-info` checks auth status.

The agent's job is to run the login command, **extract the URL + code from
stdout, and present them to the guest in chat**, then wait for the guest
to confirm they finished logging in. This works headlessly: the browser
is the guest's, not the sandbox's. No localhost redirect is involved.

> Note: the onboarding skill is written for a generic agent that may need
> to install `uv` and the CLI first. Our image pre-installs both, so the
> skill's prerequisite step is effectively a no-op (faster press-play).

## Architecture / components

### 1. The image (Daytona Snapshot)

Built with Daytona's **declarative image builder** (no local Docker).
Contents:

- Ubuntu + Python base.
- `uv` installed via the official installer
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`), on PATH.
- `uv tool install fulcra-api` — Fulcra CLI pre-installed so first use is
  instant.
- Hermes Agent installed and configured for OpenRouter.
- The `fulcra-onboarding` skill files placed at a known path, sourced from
  a **configurable reference** (`FULCRA_ONBOARDING_SKILL_REF`, default =
  the GitHub path above) so the skill can be swapped later **without
  rebuilding the snapshot**.
- Hermes configured to **auto-launch the onboarding skill on first load**.
  The skill itself greets the guest (we do not add a separate agent
  greeting), then runs `fulcra-api auth login` and relays the auth URL +
  device code into chat.

The snapshot contains **no secrets** (no OpenRouter key, no Fulcra token).

### 2. Secrets

- **`OPENROUTER_API_KEY`** — the only secret. Injected **per-sandbox at
  spawn time**, never baked into the image layer.
- **No Fulcra credential** — by design (the thesis).

### 3. Per-user launch flow (Daytona SDK scripts)

- **`spawn.py <label>`** — creates an isolated sandbox from the snapshot,
  sets `OPENROUTER_API_KEY`, sets `auto_stop_interval` to **30 minutes
  idle**, starts the Hermes web dashboard, and prints a **signed
  preview URL** for the operator to hand to that guest.
- **`teardown.py <id>`** — stops and deletes a sandbox.
- **Operator runbook** — how to invite N people, what to send them, how to
  tear down afterward.

### 4. Guest experience ("press play")

1. Operator sends guest a signed preview URL.
2. Guest opens it → Hermes web chat loads, agent has already started the
   onboarding flow.
3. Agent runs Fulcra login, posts the auth URL + device code in chat.
4. Guest opens the URL in their own browser, creates/logs into Fulcra.
5. Guest returns to chat; agent confirms and proceeds with the demo.
6. Sandbox is later torn down; the guest's Fulcra memory persists.

## Data flow

```
operator → spawn.py → Daytona API → new sandbox (from snapshot)
                                   → OPENROUTER_API_KEY injected
                                   → Hermes dashboard starts
operator ← signed preview URL ←────┘
guest    → preview URL → Hermes chat (onboarding auto-started)
Hermes   → `fulcra-api auth login` → prints auth URL + device code
guest    → auth URL (own browser) → Fulcra account created / logged in
Hermes   → fulcra-api calls → guest's Fulcra account (read/write memory)
[teardown] sandbox destroyed; Fulcra data persists
```

## Error handling (demo-grade)

- **OpenRouter key missing/invalid:** spawn should fail loudly with a
  clear operator-facing message; do not hand out a broken link.
- **Fulcra login not completed:** agent waits and re-prompts; it must not
  proceed as if authenticated. `user-info` is the gate.
- **Sandbox auto-stopped mid-session:** acceptable for a demo; document
  how to resume or re-spawn in the runbook.
- **Skill ref unreachable:** fall back to a known-good copy and log a
  warning (the build should not silently produce an agent with no
  onboarding skill).

## Testing strategy

- **Build:** snapshot builds successfully via the declarative builder.
- **Smoke (needs creds):** `spawn.py` creates a sandbox, the preview URL
  loads the Hermes chat, the agent auto-starts onboarding and emits a real
  Fulcra auth URL + code, and `user-info` reflects a successful login
  after the operator completes the device flow with a test account.
- **Teardown:** `teardown.py` removes the sandbox and it stops billing.
- Live smoke-test requires a Daytona API key and the OpenRouter key;
  Fulcra requires nothing from us (guest/test-user device login).

## Non-goals (YAGNI)

- No self-serve allowlist / magic-link launcher web app (operator spawns
  manually for a small list).
- No sandbox-side persistence, backups, or state migration.
- No multi-region, autoscaling, or production hardening.
- No baked/managed Fulcra credentials.
- No support for model backends other than OpenRouter in this phase.

## Open implementation questions (to resolve in the plan's first task)

A short spike to confirm Hermes specifics, since these dictate config:

1. Hermes install method and how OpenRouter is configured (env vars /
   config file).
2. The mechanism to **auto-run a skill / seed the first action** on
   startup (system prompt, AGENTS.md, seeded first message, etc.).
3. Which port the Hermes web dashboard binds, for the Daytona preview URL.
4. How Hermes consumes a Claude-style `SKILL.md` skill (most likely:
   instruct the agent to read and execute the markdown — confirm).

Daytona snapshot/SDK mechanics and the Fulcra device-code flow are already
understood and do not need a spike.

## Deliverables

- `Daytona snapshot definition` (declarative build).
- `Hermes agent config` + onboarding-skill wiring + auto-start-on-load.
- `spawn.py`, `teardown.py` (Daytona SDK).
- `README.md` + operator runbook.
- Repo: `~/Developer/fulcra-hermes-daytona/`.
