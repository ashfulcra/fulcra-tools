# Fulcra Hermes Demo — Handoff

**Audience:** the engineer taking over this work. Assumes you're comfortable
with Node, Docker, Hermes (Nous Research's agent), Vercel Sandbox, Fly.io, and
OAuth device-code flows, but does NOT assume you've seen any of the prior
context or sessions.

**Status as of 2026-06-03:** Working end-to-end on the baseline architecture
(operator key injected directly). LiteLLM gateway upgrade is deployed but
**paused on Fly trial machine-stop cap** — needs a credit card on Fly to lift,
then ~30 minutes of wiring to flip the demo over to the gateway path.

**TL;DR of what this is:** a *one-click* Fulcra onboarding demo. The operator
runs `npm run spawn <guest>` and gets back a URL. They send the URL to a
prospect or friend. That person opens it and lands in a chat with the Hermes
agent, which walks them through connecting their data to Fulcra (Apple Health,
Strava, etc.) via the `fulcra-onboarding` skill. The agent and its sandbox are
ephemeral; the data lands in Fulcra and is permanent. **"The agent is
ephemeral; the memory is permanent via Fulcra"** is the thesis.

---

## 1. Problem statement (what we're solving)

We want to **demo Fulcra without a sales call**. Two audiences:

1. **Friendly testers** — people in our network we're asking to try Fulcra in
   real conditions. Longer session windows (overnight, several hours). Higher
   per-session cost tolerated.
2. **Marketing demos** — public-link prospects from socials, blog posts, a
   conference booth. Shorter sessions, higher concurrency, tighter cost cap per
   user.

Both need:

- **Zero setup for the guest.** Just a URL.
- **A locked-down chat surface** — guests can't break out of the agent, can't
  read operator secrets, can't pivot the demo into a generic Claude chat.
- **Cost containment** — a curious or malicious guest cannot run up the LLM
  bill beyond a per-session ceiling.
- **An isolated, ephemeral compute environment per guest** — the agent calls
  shell tools to install `fulcra-api`, run device-code auth, etc. We do NOT
  want guests sharing a machine or accessing each other's tokens.

The blocker we kept hitting before this work: hosting platforms for the
sandbox kept failing on either egress (Daytona blocked `api.fulcradynamics.com`),
isolation (anything shared-tenant was a non-starter), or the operator-key leak
(if the guest can ask the agent to `cat ~/.hermes/.env`, our OpenRouter key is
in their hands).

## 2. Architecture (the picture)

```
                                                Operator runs:
                                                  npm run spawn alice --mode friendly
                                                       │
                                                       │ 1. Vercel API: create sandbox
                                                       │    from snapshot snap_KOv28mPN...
                                                       │ 2. (gateway path) LiteLLM
                                                       │    /key/generate → mint
                                                       │    sk-litellm-… (cap, TTL)
                                                       │ 3. Inject key into sandbox's
                                                       │    ~/.hermes/.env
                                                       │ 4. Boot start-chat.sh
                                                       ▼
   ┌─────────────────────────── Vercel Sandbox (Firecracker microVM, AL2023) ──────────────────────────┐
   │                                                                                                    │
   │   Public:                                                                                          │
   │     https://<auto>.vercel.run:8080 ──▶ Caddy ─▶ deny: /api/env, /api/config, /api/cron,            │
   │     (only the link recipient                  /api/providers, /api/dashboard/agent-plugins/*,     │
   │      can reach it; no auth                    /api/skills/toggle, /api/model/set, /api/gateway/*, │
   │      on top of that)                          /api/hermes/*, /api/logs                            │
   │                                              proxy: everything else ──▶ 127.0.0.1:9119            │
   │                                                                                                    │
   │   Internal (localhost-only):                                                                       │
   │     hermes dashboard --host 127.0.0.1 --port 9119                                                   │
   │       ├─ chat UI                                                                                   │
   │       ├─ tool runner (HERMES_YOLO_MODE=1 — no approval prompts)                                    │
   │       └─ ~/.hermes/                                                                                │
   │            ├─ skills/fulcra/fulcra-onboarding/  ← fetched at boot from GitHub                      │
   │            ├─ .env                              ← OPENROUTER_API_KEY (real OR virtual)             │
   │            └─ config.json                       ← model.provider, model.default, model.base_url   │
   │                                                                                                    │
   │   Inside the chat, the agent uses shell tools (uv, fulcra-api, curl) to:                          │
   │     • run `fulcra-api auth login` (device-code OAuth → guest authenticates in their browser)      │
   │     • install integrations (Apple Health zip upload, Strava OAuth, etc.)                           │
   │     • verify data is flowing                                                                       │
   └────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                       │
                                                       │  LLM calls (when gateway is enabled)
                                                       ▼
                ┌──────────────── fulcra-litellm.fly.dev ─────────────────┐
                │                                                          │
                │  LiteLLM proxy (Docker, 1× 1GB shared-cpu, always-on)    │
                │    • holds real sk-or-v1-…  (OpenRouter key, server-side)│
                │    • virtual-key store in Fly Postgres                   │
                │    • POST /key/generate → mints sk-litellm-…             │
                │      with max_budget + duration + key_alias              │
                │    • rejects when key over budget                        │
                │    • passes through to openrouter/<model>                │
                │                                                          │
                └──────────────────────────────────────────────────────────┘
                                                       │
                                                       ▼
                                          OpenRouter ──▶ anthropic/claude-sonnet-4.5
                                          (or any model the operator picks)
```

There are **two paths** from a sandbox to the LLM. They run on the same code;
toggling between them is a `.env` change in the operator's local repo:

| Path | When | Sandbox carries | Worst-case leak |
|---|---|---|---|
| **Baseline** | `LITELLM_URL` empty in `.env` | The operator's real OpenRouter key, capped at the OpenRouter account level (~$X/mo on the account's payment method) | Whatever the OpenRouter account cap is — could be hundreds of $ if a guest exfiltrates and hammers it before the operator notices |
| **Gateway** | `LITELLM_URL` + `LITELLM_MASTER_KEY` set | A scoped `sk-litellm-…` virtual key worth at most $15 or $25 (mode-dependent), TTL 6–12h, **rejected by every endpoint except our proxy** | $15 (marketing mode) or $25 (friendly mode) per leaked key, then LiteLLM refuses calls. The proxy's master key never leaves the operator's machine + Fly. |

The gateway path is the one we want in production. The baseline path is what
runs today because the gateway deploy is paused on the Fly trial blocker.

## 3. Repos involved

Three repos. All on the operator's machine at `<the monorepo root>/packages/`.
The user's GitHub is `ashfulcra` (org `fulcradynamics`).

| Repo | Role | State |
|---|---|---|
| **`packages/hermes-vercel`** | The active demo runtime. Build/spawn/teardown scripts for Vercel Sandbox + bake of the snapshot. **This is the one a new operator runs.** | Working end-to-end on baseline path. Gateway path coded but blocked on (4) below. |
| **`packages/litellm`** | The LiteLLM proxy as a deployable Docker app. Configured for Fly.io. | Deployed (`fulcra-litellm.fly.dev`) but machines are stop-cycling on the Fly trial — needs payment method to stay up. |
| **`ashfulcra/fulcra-hermes-daytona`** (standalone repo) | The original Daytona port. **Deprecated** and extracted out of this monorepo into its own standalone repo. Kept as a reference for the shape of the boot script and the snapshot config — the Vercel port uses the same shape, ported to Amazon Linux 2023 and the `vercel-sandbox` user. | Do not invest more here. See § 8.4 for why we left Daytona. |

The Fulcra onboarding skill itself lives in
[`fulcradynamics/agent-skills`](https://github.com/fulcradynamics/agent-skills),
specifically `skills/fulcra-onboarding/`. The Vercel sandbox **fetches it at
boot** rather than baking it (see § 4.2), so updates propagate without a
snapshot rebuild.

## 4. Component-by-component

### 4.1 `hermes-vercel/src/build-snapshot.js`

Provisions a base Vercel Sandbox, installs the whole stack into it, takes a
`Sandbox.snapshot({name})`, and writes the resulting snapshot ID to
`.snapshot.json`.

What goes into the snapshot:

| Component | Provenance | Path inside sandbox |
|---|---|---|
| OS packages: `git`, `procps-ng`, `socat`, `ca-certificates` | `dnf install -y` (note `procps-ng` on AL2023, not `procps`) | system |
| `uv` (Python package manager) | `astral.sh/uv/install.sh \| sh` | `$HOME/.local/bin/uv` |
| `fulcra-api` CLI | `uv tool install --python 3.12 fulcra-api` | `$HOME/.local/bin/fulcra-api` (also `fulcra`) |
| **Hermes agent** | Vendor installer: `sudo HERMES_HOME=$HOME/.hermes bash -s -- --skip-browser --skip-setup`. Installed via `sudo` so it lands in `/usr/local/bin/hermes` + `/usr/local/lib/hermes-agent/`. | `/usr/local/bin/hermes` |
| Caddy v2 | Static binary from `caddyserver.com/api/download` | `/usr/local/bin/caddy` |
| `fulcra-onboarding` skill (fallback copy) | `git clone fulcradynamics/agent-skills` → `cp skills/fulcra-onboarding $HOME/.hermes/skills/fulcra/` | `$HOME/.hermes/skills/fulcra/fulcra-onboarding/` |
| Caddyfile + start-chat.sh | Copied from `assets/` into the sandbox | `$HOME/fhv-assets/` |
| Hermes config | `hermes config set model.provider openrouter`, `model.default anthropic/claude-sonnet-4.5` | `$HOME/.hermes/config.json` |

**One-time cost: 3–5 minutes per rebuild.** After that, every spawn starts
from the snapshot in ~10–15 seconds. Snapshots **expire 30 days after last
use** — rebuild after any asset change, or schedule a periodic refresh.

Current snapshot ID (in `.snapshot.json`): `snap_KOv28mPNr5bLanHJ8j9YrbPkElGd`,
built 2026-05-31. Confirm it's still alive before relying on it.

### 4.2 `hermes-vercel/src/spawn.js`

The per-guest entry point.

```
npm run spawn <guest-label> -- --mode friendly|marketing
```

What it does, in order:

1. Loads `.env` (`VERCEL_TOKEN`, `OPENROUTER_API_KEY`, optionally `LITELLM_URL`+`LITELLM_MASTER_KEY`).
2. Picks the spawn mode preset (see § 5).
3. **If `LITELLM_URL` is set**: calls `POST $LITELLM_URL/key/generate` with
   `{max_budget, duration, key_alias, metadata: {mode, label}}` and gets a
   scoped `sk-litellm-…`. **If not**: uses the operator's `OPENROUTER_API_KEY`
   directly.
4. `Sandbox.create({source: {type: 'snapshot', snapshotId}, ports: [8080],
   timeout: 5h, tags: {fhv, guest, mode}, env})`. The chosen key does NOT go
   into `env` (visible in process list); only `OPENROUTER_MODEL` and (when
   gateway) `LITELLM_URL` do.
5. `sandbox.runCommand({env: {OPENROUTER_API_KEY: injectedKey}, ...})` writes
   the key into `~/.hermes/.env` from inside the sandbox — never crosses the
   command line.
6. `sandbox.runCommand({detached: true, ...})` boots `start-chat.sh` in the
   background.
7. Prints `sandbox.domain(8080)` — the full https URL — and exits.

### 4.3 `hermes-vercel/assets/hermes/start-chat.sh`

Runs *inside* the sandbox. The detached boot script.

1. Exports `HERMES_YOLO_MODE=1` — **bypasses the dangerous-command approval
   prompts in chat**. This is the env var, NOT the `approvals.mode=yolo`
   config key (the latter does nothing — confirmed by reading Hermes source).
2. If `LITELLM_URL` is set, switches Hermes to `model.provider=custom` and
   `model.base_url=$LITELLM_URL`.
3. **Fetches the skill at boot** from
   `${FULCRA_SKILL_REPO:-fulcradynamics/agent-skills}@${FULCRA_SKILL_BRANCH:-main}/${FULCRA_SKILL_SUBPATH:-skills/fulcra-onboarding}`,
   copies into `$HOME/.hermes/skills/fulcra/fulcra-onboarding/`. The snapshot's
   baked copy is the fallback if the fetch fails. **This is how we ship skill
   updates without rebuilding the snapshot** — push to GitHub and the next
   spawn picks it up.
4. Starts `hermes dashboard --host 127.0.0.1 --port 9119 --no-open --insecure`
   in the background. `--insecure` is **required** for the chat PTY to come
   live; the bind to 127.0.0.1 + Caddy in front is what keeps it safe.
5. `exec caddy run` in the foreground.

### 4.4 `hermes-vercel/assets/caddy/Caddyfile`

The lockdown layer. Caddy fronts `:8080` (Vercel maps it to the public URL)
and reverse-proxies everything to `127.0.0.1:9119` **except** the deny list,
which 403s:

```
/api/env, /api/env/*, /api/config, /api/config/*, /api/cron, /api/cron/*,
/api/providers, /api/providers/*, /api/dashboard/agent-plugins/*,
/api/profiles, /api/profiles/*, /api/skills/toggle, /api/model/set,
/api/gateway/*, /api/hermes/*, /api/logs, /api/logs/*
```

These were derived by reading the Hermes dashboard's API surface and
classifying each route as "guest needs" (chat, sessions) vs "operator only"
(env, secrets, model swap, plugin toggles, gateway config). Validated against
**Hermes v0.14.0**. If you upgrade Hermes, re-read its dashboard API and
re-confirm — new routes may need adding to the deny list.

`Host` and `Origin` are rewritten to `127.0.0.1:9119` so the dashboard's
SameSite/Origin checks pass when reached through Caddy.

### 4.5 `litellm/`

A 4-file deployable: `Dockerfile`, `config.yaml`, `fly.toml`, `README.md`.

- **Dockerfile**: `FROM ghcr.io/berriai/litellm-non_root:main-stable` then
  `COPY config.yaml /app/config.yaml`. That's the whole image.
- **config.yaml**: Wildcard `model_name: "*"` → `litellm_params.model:
  "openrouter/*"`. Whatever model name the sandbox sends gets rewritten to
  `openrouter/<that>`. Master key + database URL come from env vars.
- **fly.toml**: 1× `shared-cpu-1x` 1024 MB always-on (`auto_stop_machines = off`,
  `min_machines_running = 1`), region `iad` (co-located with Vercel Sandbox
  `iad1`). Memory bumped from 512 → 1024 MB after the first deploy OOM-killed
  within 2 min.

Deployed at `https://fulcra-litellm.fly.dev`. Health endpoint is `/health`.
Admin API at `POST /key/generate`, `DELETE /key/{key}`. OpenAI-compatible
chat completions at `/v1/chat/completions`.

Secrets on the Fly app (set via `fly secrets set …`):

- `OPENROUTER_API_KEY` — the real `sk-or-v1-…` the proxy uses upstream.
- `LITELLM_MASTER_KEY` — operator-only admin key. Used to mint virtual keys.
  **Never injected into sandboxes.** This is the credential the new operator
  most needs to know about; it's stored in Fly secrets and in the local
  `hermes-vercel/.env`.
- `DATABASE_URL` — Fly Postgres connection string, auto-wired when the
  Postgres add-on is attached.

## 5. Spawn modes

Defined in `src/spawn.js` as `MODES = {friendly, marketing}`. The mode
controls *only* the virtual-key cap + TTL on the gateway path. Sandbox
lifetime is independently capped at the Vercel-plan ceiling (5h on Pro,
45 min on Hobby).

| Mode | Key TTL | Budget cap | Use case | Worst-case leak |
|---|---|---|---|---|
| `friendly` | 12 h | **$25** | Friends/colleagues we asked to try Fulcra over an afternoon or overnight | $25 |
| `marketing` | 6 h | **$15** | Public link from socials/blog — higher concurrency, tighter cap, shorter window | $15 |

A typical 2-hour Hermes session with the Fulcra onboarding flow runs **$3–12**
in actual LLM spend. The caps are insurance, not the expected envelope.

Sandbox-side, both modes get the same 5h Pro timeout. The virtual key
**outlives the sandbox** intentionally — a guest who refreshes/comes back
within the TTL hits the same budget envelope, no re-mint needed.

## 6. Setup from scratch (new operator)

Assumes: macOS, Node ≥20, `gh` CLI, an Anthropic/OpenAI-shaped LLM
configured already.

### 6.1 Accounts you need

| Account | Why | Plan |
|---|---|---|
| Vercel | Sandbox runtime | **Pro is required** for 5h sandbox lifetime (Hobby is 45 min). $20/mo. |
| OpenRouter | LLM provider | Pay-as-you-go. Capped throwaway key recommended. |
| Fly.io | LiteLLM proxy host | Trial fine for the first 5 min, but **needs a credit card** at https://fly.io/trial to keep machines running past that. ~$8–10/mo committed. |
| GitHub (org `fulcradynamics`) | Owns `agent-skills` repo (skill the sandbox fetches at boot) | n/a |

### 6.2 First-time deploy of LiteLLM

```bash
cd <monorepo>/packages/litellm
# fly auth login first if you haven't
fly apps create fulcra-litellm  # or accept the name fly generates
fly postgres create --name fulcra-litellm-db --region iad
fly postgres attach fulcra-litellm-db --app fulcra-litellm  # sets DATABASE_URL
fly secrets set OPENROUTER_API_KEY=sk-or-v1-<your-capped-key> \
                LITELLM_MASTER_KEY=sk-litellm-$(openssl rand -hex 32)
fly deploy
# wait ~2 min, then:
curl https://fulcra-litellm.fly.dev/health  # → {"status":"healthy"}
```

**Save the master key.** It's not retrievable later from Fly without
`fly ssh console` into the machine and reading the env — possible, but
inconvenient.

### 6.3 First-time bake of the Vercel snapshot

```bash
cd <monorepo>/packages/hermes-vercel
cp .env.example .env
# Edit .env: VERCEL_TOKEN, VERCEL_TEAM_ID, VERCEL_PROJECT_ID,
#            OPENROUTER_API_KEY, OPENROUTER_MODEL
#            (leave LITELLM_URL + LITELLM_MASTER_KEY empty for baseline path)
npm install
npm run build  # takes 3–5 min, writes .snapshot.json on success
```

### 6.4 Spawn your first guest

```bash
npm run spawn alice -- --mode friendly
# Prints: PRESS PLAY (send this link): https://<auto>.vercel.run
# Send that URL to alice. ~15–20s for the chat to come up on first load.
```

### 6.5 Flip to gateway path (recommended for any real demo)

```bash
# In <monorepo>/packages/hermes-vercel/.env:
LITELLM_URL=https://fulcra-litellm.fly.dev/v1
LITELLM_MASTER_KEY=<the-same-master-key-as-Fly>
```

`spawn.js` now mints a virtual key per spawn. Verify with:

```bash
npm run spawn alice -- --mode marketing
# In the spawn output, you should see:
#   "Minting per-sandbox virtual key via LiteLLM..."
#   "virtual key minted ..."
# To confirm the sandbox carries only the scoped key, not the real one:
# (after spawn, ssh-equivalent into the sandbox via Vercel CLI and:)
# cat ~/.hermes/.env  → should show OPENROUTER_API_KEY=sk-litellm-...
#                       NOT a sk-or-v1-... key.
```

## 7. Cost model

### 7.1 Fixed (the proxy + the sandbox plan)

| Item | Cost |
|---|---|
| Vercel Pro | $20/mo flat |
| Fly `shared-cpu-1x` 1GB, always-on | ~$5.70/mo |
| Fly Postgres (smallest dev tier) | ~$2–4/mo |
| Fly bandwidth | Free up to 160 GB/mo in NA — won't approach |
| **Fixed total** | **~$28–32/mo** |

### 7.2 Variable (LLM tokens via OpenRouter)

LiteLLM adds **zero markup** — it forwards to OpenRouter, OpenRouter bills at
the model's rate. `anthropic/claude-sonnet-4.5` today is $3/M input, $15/M
output.

| Scenario | LLM cost |
|---|---|
| One quiet onboarding session (~2h) | $3–8 |
| One tool-heavy session (lots of `fulcra-api` calls, Apple Health upload) | $5–12 |
| One leaked key, attacker hammering before TTL | Capped at $15 (marketing) / $25 (friendly), then LiteLLM refuses |

### 7.3 Where the cost scales

- **Per-session LLM spend** scales with chat length and tool-call density.
  The skill is the lever — a tighter, more deterministic skill burns fewer
  tokens.
- **Per-sandbox compute on Vercel Pro** is included; Pro covers a reasonable
  burst before they meter you. If concurrent sandboxes routinely exceed ~10,
  re-check Vercel's Pro fair-use.
- **Fly proxy** is flat regardless of throughput at this scale. Doesn't move
  on 10 concurrent vs 100 concurrent.

## 8. Limitations / risk surface (read this carefully)

### 8.1 The key-leak risk on the baseline path

Until `LITELLM_URL` is wired, every sandbox carries the operator's real
OpenRouter key in `~/.hermes/.env`. A curious guest **can** ask the agent
`cat ~/.hermes/.env` and the agent **will** print it — `HERMES_YOLO_MODE=1`
means no approval prompt. The Caddy deny list does not protect against this;
those rules govern the *dashboard's HTTP API*, not the agent's shell tool.

The mitigation today is: use a **low-cap throwaway OpenRouter key** on a
capped underlying payment method, and rotate if leaked. This is what the
operator does. The mitigation tomorrow (gateway path) is that the leaked key
is worth at most $15–25 and dies in 6–12h.

**Do not run a public marketing demo on the baseline path.** Friendly mode
with people we trust is acceptable; public links are not.

### 8.2 The lockdown is necessary-but-not-sufficient

The Caddy deny list prevents a guest from using the *dashboard's API* to read
config, swap models, or toggle plugins. It does NOT prevent the agent itself
from leaking those things if asked, because the agent has full shell tools
inside the sandbox. The trust model is:

- Vercel Sandbox isolates sandboxes from each other and from us (Firecracker
  microVM). We trust that.
- Caddy prevents control-plane access to the dashboard. We control that.
- The agent, running under YOLO mode, will do whatever it's asked inside the
  sandbox. We **do not** control that, and we intentionally let it call
  shell tools — that's the demo.

So our threat model is: a guest can read everything in the sandbox's
filesystem, can hit `api.fulcradynamics.com` with whatever auth they
established, and can burn LLM tokens up to the key's cap. They cannot
exfiltrate to other sandboxes, cannot break Vercel's microVM isolation, and
cannot get more LLM spend than the cap.

### 8.3 Skill-update path is good but not bulletproof

`start-chat.sh` fetches the skill from GitHub on every boot. If GitHub is
unreachable or the branch name in `FULCRA_SKILL_BRANCH` doesn't exist, it
silently falls back to the snapshot-baked copy. That's the desired behavior
(don't fail the demo), but it means **stale snapshots can serve stale
skills** if you ever break the boot fetch and don't notice. Worth a
periodic sanity check (e.g. monthly: spawn a sandbox, confirm the skill
version banner in the chat matches `agent-skills` HEAD).

### 8.4 Why Daytona is deprecated (sibling repo)

Daytona was the first runtime. We left it because:

- Tier 1/2 egress allowlist **blocks `api.fulcradynamics.com` with a TLS
  reset**. The whole demo's data path doesn't work. Verified live, not
  speculative.
- Tier 3 (which would lift the allowlist) requires sales contact and is
  priced for orgs much larger than us.
- Vercel Sandbox uses Firecracker too and has open egress out of the box, on
  a self-serve Pro plan.

The Daytona repo (standalone at `ashfulcra/fulcra-hermes-daytona`) still works
on Daytona's network for what it can reach — Apple Health and GitHub flows work
fine. It just can't talk to Fulcra. Kept as a reference for the boot script shape.

### 8.5 Node 26 + @vercel/sandbox compatibility (technical debt)

`@vercel/sandbox@2.0.2` is broken on Node 26 in two ways (reproduced + fixed
locally):

1. **Brotli response bodies aren't decoded.** Worked around by forcing
   `Accept-Encoding: identity` via `src/fetch-shim.js`, imported at the top
   of every entry script.
2. **Response headers stripped to `{}`** on some responses. Worked around
   with a 2-line tolerance patch in
   `node_modules/@vercel/sandbox/dist/api-client/api-client.js` to accept
   null content-type.

**The real fix is pinning Node 22 LTS.** The shim should stay as
defense-in-depth, but the `node_modules` patch goes away on Node 22. We
haven't switched because Node 26 was already installed and it works with the
patch; **a new operator should pin Node 22 (or wait for a fixed SDK
release).** Track the SDK upstream — when it ships a release that handles
Node 26 cleanly, drop the patch.

### 8.6 LiteLLM virtual-key revocation on teardown is TODO

`spawn.js` mints the key but does NOT `DELETE /key/{key}` on teardown. The
key auto-expires at its TTL (6 or 12h), so the worst-case leak window is the
TTL, not "forever." Improvement, not a bug — small code add to `teardown.js`
and a key-tracking JSON file or DB row. ~30 min of work.

### 8.7 The snapshot is mortal

Vercel expires snapshots **30 days after last use**. Demos lull → snapshot
dies → next spawn fails. Either:

- Rebuild on a cron (`npm run build` weekly).
- Or wrap `spawn.js` to detect "snapshot not found" and auto-rebuild.

Neither is implemented. A weekly cron job is the cheap fix.

## 9. Capabilities (what the system can do today)

- Spin up a fully isolated, ephemeral Hermes-agent sandbox for a guest in
  ~15s with a single command.
- Guest walks through Fulcra onboarding (Apple Health, Strava, etc.) via the
  skill, with the agent driving via shell tools.
- Locked-down chat surface — no dashboard control-plane access from the
  public URL.
- Self-expiring per-guest LLM budget when gateway path is enabled (currently
  paused on Fly trial).
- Skill updates ship via `git push` to `agent-skills`, no snapshot rebuild
  needed.
- Two modes (friendly / marketing) with different cap+TTL profiles.
- Cleanup via `npm run teardown --all`.

## 10. What's NOT done / open decisions

In priority order for the new operator:

1. **(BLOCKING gateway rollout)** Add a credit card on Fly at
   https://fly.io/trial. Then `fly deploy` if machines aren't already up,
   verify `/health`, and wire `LITELLM_URL` + `LITELLM_MASTER_KEY` into
   `hermes-vercel/.env`. Test with `npm run spawn ash -- --mode
   friendly`; confirm `~/.hermes/.env` inside the sandbox has the
   `sk-litellm-…` virtual key, not the real `sk-or-v1-…`.

2. **(Architectural decision)** Once the gateway is wired, do we keep the
   baseline path as a code path at all? Right now it's a fallback for
   "operator hasn't configured LiteLLM." Reasonable arguments either way:
   keep it for local testing; drop it to remove the foot-gun. My
   recommendation is **keep it, but require an explicit `--unsafe` flag** so
   spawning without a gateway is a deliberate act, not a default.

3. **(Hardening)** Add virtual-key revocation on teardown (§ 8.6).

4. **(Hardening)** Add a snapshot freshness check / weekly rebuild cron
   (§ 8.7).

5. **(Tech debt)** Pin Node 22 LTS in `package.json`'s `engines` and drop
   the `node_modules` patch (§ 8.5).

6. **(Observability)** No metrics today. LiteLLM has a built-in admin UI for
   per-key spend; expose it under operator-only auth. Beyond that: any of
   Datadog/Grafana/Honeycomb against the LiteLLM Postgres for spend trends,
   sandbox lifetime distributions, etc.

7. **(Product)** The skill is in active iteration. The current branch
   targeted from the Daytona repo (`ashfulcra/fulcra-hermes-daytona`, `fix/preconfigured-env-and-reliable-auth`)
   on `agent-skills` should be re-evaluated — is its PR merged? If yes,
   point `FULCRA_SKILL_BRANCH` back at `main` (it'll be `main` by default if
   unset).

## 11. Operational runbook

### Spawn a sandbox
```bash
cd <monorepo>/packages/hermes-vercel
npm run spawn <label> -- --mode friendly|marketing
```

### Tear down everything
```bash
npm run teardown -- --all
# Or pass a specific sandbox ID: npm run teardown -- <sandbox-id>
```

### Rebuild the snapshot
```bash
npm run build
# 3–5 min. Updates .snapshot.json. Old snapshot remains usable for already-spawned sandboxes; new spawns use the new one.
```

### Inspect LiteLLM
```bash
fly logs --app fulcra-litellm
fly status --app fulcra-litellm
fly ssh console --app fulcra-litellm
# inside the machine:
#   curl http://localhost:4000/health
#   curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
#        http://localhost:4000/key/info?key=sk-litellm-...
```

### Mint a virtual key by hand (debugging)
```bash
curl -X POST https://fulcra-litellm.fly.dev/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"max_budget": 5, "duration": "1h", "key_alias": "test"}'
```

### Get inside a running sandbox (for debugging)
The Vercel CLI doesn't have a clean `ssh`-equivalent for sandboxes; the
practical path is to add a temporary `runCommand` in `spawn.js` that does
what you need and re-spawn. Alternatively `sandbox.runCommand({cmd: 'cat',
args: ['~/.hermes/.env']})` from a small one-off node script using the
saved sandbox ID.

### Kill a stuck sandbox
```bash
# Get the sandbox name from spawn output, or list via:
node -e "import('@vercel/sandbox').then(({Sandbox}) => Sandbox.list({token: process.env.VERCEL_TOKEN, teamId: process.env.VERCEL_TEAM_ID, projectId: process.env.VERCEL_PROJECT_ID}).then(r => console.log(r)))"
# Then teardown by id.
```

## 12. Pointers, accounts, secrets

| Resource | Where | Notes |
|---|---|---|
| Vercel team | `team_gtnBwmcv1xNiQ00ghGZx0IeH` (fulcra-dynamics) | Pro plan. Auth: `VERCEL_TOKEN` in `.env`. |
| Vercel project | `prj_UClFyveQFQ5E58mViadBBLROAzML` | Houses the demo sandboxes. |
| Vercel snapshot | `snap_KOv28mPNr5bLanHJ8j9YrbPkElGd` | Recorded in `.snapshot.json`. |
| LiteLLM URL | `https://fulcra-litellm.fly.dev` | `/health` to ping, `/v1` for OpenAI-compat. |
| LiteLLM master key | Fly secret `LITELLM_MASTER_KEY` + local `.env` | NOT in this doc, NOT in git. |
| OpenRouter key | Fly secret `OPENROUTER_API_KEY` (real) + local `.env` (capped throwaway for baseline) | The Fly-side key is the one that pays for everything when gateway is on. |
| Skill repo | https://github.com/fulcradynamics/agent-skills | `skills/fulcra-onboarding/` is what the sandbox fetches. |
| Operator git identity | `ashfulcra` (personal), `fulcradynamics` (org) | Vercel sandbox's GitHub trust list does NOT include `fulcradynamics`; pushing from inside a sandbox to the org will fail. Push from the operator's laptop. |

## 13. Publishing state

All three packages live in the **`ashfulcra/fulcra-tools`** private monorepo:

| Package | Path | Notes |
|---|---|---|
| `fulcra-hermes-daytona` | standalone repo `ashfulcra/fulcra-hermes-daytona` | Deprecated; extracted out of this monorepo into its own standalone repo. Kept as reference. See its README banner. |
| `hermes-vercel` | `packages/hermes-vercel/` | Active demo runtime. |
| `litellm` | `packages/litellm/` | LiteLLM gateway, deployable to Fly. |

The Daytona port now lives in the standalone repo
`github.com/ashfulcra/fulcra-hermes-daytona` (extracted from this monorepo with
its full history preserved). Treat that repo as canonical for the deprecated
Daytona version; the active demo is the in-monorepo `packages/hermes-vercel/`.

The `.env` files in each package contain real secrets and are gitignored;
`.env.example` files are committed checklists. Operator-side state
(`.snapshot.json` for hermes-vercel, `node_modules/`) is gitignored.

The Vercel-sandbox environment (i.e. spawned guest sandboxes) **hard-blocks
pushing source code to GitHub orgs not on its trusted allowlist**;
`fulcradynamics` is not on that list. This is only relevant for the agent
inside a guest sandbox attempting to push — irrelevant to the operator
pushing from their laptop.

## 14. Where this doc lives, and where it might go

`docs/HANDOFF.md` in `packages/hermes-vercel`. The companion
`docs/ARCHITECTURE.md` in the same folder predates this and overlaps in some
sections — when you make a substantive change to either, please **reconcile
or delete the other**. Don't let them drift in parallel.

The READMEs at the root of each of the three packages are the entry points;
this doc is the deep-dive. If a new operator only reads one thing, it should
be this one.

---

*Last touched: 2026-06-03. Anything older than that in this doc, treat as
suspicious and re-verify against the code.*
