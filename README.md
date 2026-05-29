# fulcra-hermes-daytona

Operator tooling for the **Fulcra "press play" demo**: give a small, hand-picked
set of people their own isolated, ephemeral [Hermes](https://hermes-agent.nousresearch.com)
agent on [Daytona](https://www.daytona.io) that onboards them into their *own*
Fulcra account.

## The thesis

**The agent is ephemeral; the memory is permanent via Fulcra.** A guest opens a
link, lands in a Hermes chat, and the agent walks them through creating (or
signing into) their own Fulcra account. Everything the agent learns about them
persists in *their* Fulcra account and outlives the throwaway sandbox.

- Each guest gets their **own** isolated sandbox (not a shared agent).
- **Fulcra (you) pays** for the compute; guests need no Daytona/OpenRouter
  account — they only create a Fulcra account, which is the point.
- Self-serve, pay-per-use, **no Fulcra credentials anywhere in this repo** — the
  guest authenticates via Fulcra's device-code browser flow.

## How it works

1. A reusable Daytona **snapshot** (`fhd-hermes-demo`) bakes: `uv` + the Fulcra
   CLI (`fulcra-api`), Hermes (configured for OpenRouter), the
   [`fulcra-onboarding`](https://github.com/fulcradynamics/agent-skills) skill,
   and Caddy.
2. `spawn.py` creates one **private** sandbox per guest, injects the OpenRouter
   key into `~/.hermes/.env`, starts the chat, and returns a **signed Daytona
   preview URL**.
3. The guest opens the URL → the **Hermes dashboard chat**, locked down by a
   Caddy reverse proxy so guests can use the chat but cannot reach the
   admin/key/config endpoints.
4. The agent runs the onboarding skill: greet → "what do you want to track?" →
   `fulcra-api auth login` (device-code) → it shows the guest an auth URL + code
   to complete in their own browser.
5. Sandboxes **auto-stop after 30 minutes idle**; tear them down when done.

## Prerequisites

- Python 3.12 and [`uv`](https://docs.astral.sh/uv/).
- A `.env` file in the repo root (gitignored) with:

  ```
  DAYTONA_API_KEY=dtn_...
  OPENROUTER_API_KEY=sk-or-...
  OPENROUTER_MODEL=anthropic/claude-sonnet-4.5   # optional; this is the default
  ```

- Install deps: `uv venv && uv pip install -e '.[dev]'` (or `uv sync`).

> ⚠️ **Use a disposable, low-credit-cap OpenRouter key for real demos.** The
> guest is talking to an agent with a shell, so a determined guest could ask it
> to print its own environment. Locking the dashboard does not change that.
> Rotate the key after demos.

## Usage

Build the snapshot once (and after any image change — a few minutes):

```bash
uv run python -m fhd.build_snapshot
```

Spawn one sandbox per guest and send them the printed link:

```bash
uv run python -m fhd.spawn alice
# -> PRESS PLAY (send this link): https://8080-<token>.daytonaproxy01.net
```

The chat takes ~15–20s to come up on first load (the dashboard builds its web
UI). Spawn shortly before the demo, since sandboxes auto-stop after 30 min idle.

List / tear down:

```bash
uv run python -m fhd.teardown --list
uv run python -m fhd.teardown --delete <sandbox-id>
uv run python -m fhd.teardown --all      # delete every guest sandbox
```

## Security model (demo-grade)

- **Access:** the signed preview URL carries its own token and the sandbox is
  `public=False`, so only someone with the link gets in. Control = "only
  invitees get the link" + ephemerality. There is **no per-user login**; this is
  intended for a small, trusted invite list, not public distribution.
- **Dashboard lockdown:** Caddy (`assets/caddy/Caddyfile`) proxies the SPA and
  chat but returns 403 for the secret/admin/exec endpoints (`/api/env`,
  `/api/env/reveal`, `/api/config`, `/api/cron`, `/api/providers`,
  `/api/dashboard/agent-plugins/*`, `/api/model/set`, `/api/gateway/*`,
  `/api/hermes/*`, `/api/logs`, …). The dashboard binds `127.0.0.1` only and is
  reachable solely through Caddy.
- **First-visit interstitial:** Daytona shows a one-click "Preview URL Warning"
  before the app. It's removable org-wide in Daytona settings
  (`daytona.io/docs/en/preview-and-authentication`) for a cleaner demo.

## The onboarding skill (fetched on boot)

Each sandbox **pulls the latest `fulcra-onboarding` skill from GitHub at
startup** (`assets/hermes/start-chat.sh`), so updating the skill on
`github.com/fulcradynamics/agent-skills` propagates to every newly spawned
sandbox **with no rebuild**. The image also bakes a copy, which is used as a
fallback if the boot-time fetch fails (GitHub unreachable, bad branch, etc.).

- Source is overridable per spawn via env: `FULCRA_SKILL_REPO` (default
  `https://github.com/fulcradynamics/agent-skills`) and `FULCRA_SKILL_SUBPATH`
  (default `skills/fulcra-onboarding`).
- A bad commit on the skill's default branch *will* reach live demos — that's the
  trade-off of fetch-on-boot. If you need to freeze a known-good version, point
  `FULCRA_SKILL_REPO` at a fork/tag or switch back to baking a pinned copy.
- The agent is told to run the skill on the first message via
  `assets/hermes/SOUL.md` and `assets/hermes/AGENTS.md`.

## Layout

```
src/fhd/
  config.py           # load + validate .env (DAYTONA / OPENROUTER)
  image.py            # the declarative Daytona image
  build_snapshot.py   # build/register the fhd-hermes-demo snapshot (idempotent)
  snapshot_params.py  # pure helper: per-guest sandbox kwargs
  spawn.py            # spawn one guest sandbox -> signed preview URL
  teardown.py         # list / delete guest sandboxes
assets/
  hermes/             # SOUL.md, AGENTS.md, start-chat.sh (dashboard + Caddy boot)
  caddy/Caddyfile     # the lockdown reverse proxy
docs/ARCHITECTURE.md  # how it works + the non-obvious design decisions
```

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** for how it works end to
end and *why* — the dashboard lockdown, the `HERMES_YOLO_MODE` approval bypass,
fetch-on-boot skills, the node-PATH and web-build gotchas, and the security/cost
model. `docs/superpowers/` (if present) holds the original spec, plan, and
live-spike findings.
