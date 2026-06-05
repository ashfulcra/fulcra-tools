# fulcra-hermes-daytona

> **⚠️ DEPRECATED — superseded by [`hermes-vercel`](../hermes-vercel).**
>
> Daytona's Tier 1/2 egress allowlist blocks `api.fulcradynamics.com` (verified
> live: TLS reset), which kills the demo's data path. We ported the same
> system to Vercel Sandbox + Firecracker with open egress.
>
> **New operators: start at [`../hermes-vercel/docs/HANDOFF.md`](../hermes-vercel/docs/HANDOFF.md).**
>
> This package is retained as a reference for the shape of the boot script,
> snapshot config, and skill-fetch pattern — all of which carried forward to
> the Vercel port. Do not invest more here unless we revisit Daytona Tier 3.

Operator tooling for the **Fulcra "press play" demo**: give a small, hand-picked
set of people their own isolated, ephemeral [Hermes](https://hermes-agent.nousresearch.com)
agent on [Daytona](https://www.daytona.io) that onboards each of them into their
*own* Fulcra account.

> **The thesis: the agent is ephemeral; the memory is permanent via Fulcra.** A
> guest opens a link, lands in a Hermes chat, and the agent walks them through
> creating (or signing into) their own Fulcra account. Everything the agent learns
> persists in *their* Fulcra account and outlives the throwaway sandbox.

- Each guest gets their **own** isolated sandbox — not a shared agent.
- **You pay** for the compute; guests need no Daytona/OpenRouter account. They only
  create a Fulcra account, which is the whole point.
- Self-serve, pay-per-use, **no Fulcra credentials anywhere in this repo** — the
  guest authenticates via Fulcra's device-code browser flow.

---

## Quickstart

```bash
# 0. one-time setup — run from this package's directory
cd packages/hermes-daytona           # if you're at the fulcra-tools root
uv sync                              # installs deps + the fhd-* commands
cp .env.example .env                 # then fill in your keys (see Prerequisites)

# 1. build the agent image once (~3 min)
uv run fhd-build

# 2. one link per guest — send them the printed URL
uv run fhd-spawn alice
uv run fhd-spawn bob

# 3. clean up when you're done
uv run fhd-teardown --all
```

That's the whole loop. Spawn shortly before a demo (sandboxes auto-stop after
4 hours idle), and the first chat load takes ~15–20s while the dashboard builds.

## Prerequisites

- Python 3.12 and [`uv`](https://docs.astral.sh/uv/).
- A `.env` file in this package's directory (`packages/hermes-daytona/.env`, gitignored — never committed) with:

  ```
  DAYTONA_API_KEY=dtn_...
  OPENROUTER_API_KEY=sk-or-...
  OPENROUTER_MODEL=anthropic/claude-sonnet-4.5   # optional; this is the default
  ```

- `uv sync` installs the dependencies and the `fhd-build` / `fhd-spawn` /
  `fhd-teardown` commands. (You can also run them as `uv run python -m fhd.<name>`.)
- **A Daytona account on Tier 3+.** ⚠️ This is required for the Fulcra *data*
  path. Tiers 1–2 restrict sandbox egress to a default allowlist (PyPI, GitHub,
  CDNs, AI APIs) and **block `api.fulcradynamics.com`** — so login works but
  every Fulcra data call gets a TLS reset, and it can't be overridden per
  sandbox. Tier 3+ grants full/configurable egress. See
  [Daytona network limits](https://www.daytona.io/docs/en/network-limits/);
  bump your tier in the Daytona dashboard or via support@daytona.io.

> ⚠️ **Use a disposable, low-credit-cap OpenRouter key for real demos.** The guest
> is talking to an agent with a shell, so a determined guest could ask it to print
> its own environment. Locking the dashboard does not change that — see
> [Security](#security-model-demo-grade). Rotate the key after a demo round.

## What the guest experiences

1. They open the link you sent and click **"I Understand, Continue"** on Daytona's
   one-time preview warning.
2. A clean Hermes chat loads. They type anything (e.g. "hi").
3. The agent greets them, asks what they'd like to track, then runs the Fulcra
   login — it shows an **authorization URL + code** to open in *their own* browser,
   where they create or sign into their Fulcra account.
4. From there the agent helps them set things up; whatever it captures lives in
   their Fulcra account after the sandbox is gone.

No accounts to make except Fulcra, no approval prompts, nothing to install.

## Commands

| Command | What it does |
|---|---|
| `uv run fhd-build` | Build/register the reusable `fhd-hermes-demo` Daytona snapshot. Run once, and again after any image/asset change. Idempotent (deletes + rebuilds a same-named snapshot). |
| `uv run fhd-spawn <label>` | Spawn one private sandbox for a guest and print a signed "press play" URL. |
| `uv run fhd-teardown --list` | List every live guest sandbox. |
| `uv run fhd-teardown --delete <id>` | Delete one sandbox. |
| `uv run fhd-teardown --all` | Delete all guest sandboxes (stops billing). |

## Running several demos at once

Each `fhd-spawn` is fully independent — separate sandbox, separate agent, separate
Fulcra account — all sharing the one OpenRouter key. Just spawn one per person:

```bash
for name in alice bob carol; do uv run fhd-spawn "$name"; done
```

They run concurrently with no interference. The only shared limit is OpenRouter's
per-key rate limit: fine for a handful of simultaneous chats; if you expect dozens
at once, bump your OpenRouter tier or split across keys.

## Security model (demo-grade)

- **Access:** the signed preview URL carries its own token and the sandbox is
  `public=False`, so only someone with the link gets in. Control is "only invitees
  get the link" + ephemerality (4-hour idle auto-stop). There is **no per-user
  login** — this is for a small, trusted invite list, not public distribution.
- **Dashboard lockdown:** the Hermes dashboard is an admin console (its `KEYS` tab
  can reveal/edit the OpenRouter key). It binds `127.0.0.1` only and is fronted by
  Caddy (`assets/caddy/Caddyfile`), which proxies the chat but returns **403** for
  every secret/admin/exec endpoint (`/api/env`, `/api/env/reveal`, `/api/config`,
  `/api/cron`, `/api/providers`, `/api/dashboard/agent-plugins/*`,
  `/api/model/set`, `/api/gateway/*`, `/api/hermes/*`, `/api/logs`, …).
- **Residual risk:** the agent has a shell, so a guest could ask it to print its
  own `~/.hermes/.env`. The dashboard lockdown doesn't stop that (nothing would,
  short of removing the shell). Mitigation = the disposable, capped key above.

## Cost (Daytona)

Default sandbox is 1 vCPU / 1 GiB / 3 GiB disk. Worst case — a tab pinned open for
a full 24h — is **≈ $1.60/sandbox/day**; if it idle-stops at the 4-hour mark it's about a quarter
cents. Stopped sandboxes keep a little disk until deleted, so run
`fhd-teardown --all` to truly zero it out. Everything draws from Daytona's $200
free credit first.

## The onboarding skill (fetched on boot)

Each sandbox **pulls the latest `fulcra-onboarding` skill from GitHub at startup**
(`assets/hermes/start-chat.sh`), so updating it on
[`fulcradynamics/agent-skills`](https://github.com/fulcradynamics/agent-skills)
reaches every newly spawned sandbox **with no rebuild**. The image also bakes a
copy, used as a fallback if the boot fetch fails.

- Overridable per spawn via env: `FULCRA_SKILL_REPO` and `FULCRA_SKILL_SUBPATH`.
- Trade-off: a bad commit on the skill's default branch reaches new demos
  immediately. To freeze a known-good version, point `FULCRA_SKILL_REPO` at a
  fork/tag.

## Troubleshooting

- **The chat is blank / "connecting" for a while.** First load builds the
  dashboard's web UI (~15–20s). Give it time; reload if needed.
- **A guest hits a "[HIGH] approval required" prompt.** Their sandbox predates the
  `HERMES_YOLO_MODE` fix — re-`fhd-spawn` them from the current snapshot.
- **`fhd-build` says the snapshot already exists.** It shouldn't (build is
  idempotent), but if a build was interrupted, just run `fhd-build` again — it
  deletes the stale snapshot and rebuilds.
- **`ModuleNotFoundError: fhd`.** Run `uv sync` (installs the package), or prefix
  commands with `PYTHONPATH=src`.
- **Onboarding stalls right after login.** The agent polls `fulcra-api user-info`;
  make sure the guest actually completed the browser login and tell the agent
  "done".
- **Login works but Fulcra data calls fail ("Connection reset by peer" / TLS
  reset).** This is the Daytona **network tier** limit, not a bug: Tiers 1–2
  block `api.fulcradynamics.com`. Confirm with
  `curl https://api.fulcradynamics.com` inside a sandbox (it'll reset on a low
  tier while `curl https://openrouter.ai` succeeds). Fix = move the Daytona
  account to **Tier 3+** (see Prerequisites). The agent may misdiagnose this as
  "all HTTPS blocked" — it isn't; only non-allowlisted domains are.

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

**For how it all works and *why* — the dashboard lockdown, the `HERMES_YOLO_MODE`
approval bypass, fetch-on-boot skills, the node-PATH and web-build gotchas, and the
security/cost model — read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).**
