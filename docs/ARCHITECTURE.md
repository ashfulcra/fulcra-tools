# Architecture & design notes

How the Hermes-on-Daytona demo works end to end, and — more usefully — *why* it's
shaped the way it is. Most of the non-obvious decisions here were forced by how
Hermes and Daytona actually behave; they're documented so the next person doesn't
re-discover them the hard way.

## The thesis

**The agent is ephemeral; the memory is permanent via Fulcra.** A guest opens a
link, lands in a Hermes chat, and the agent walks them through creating (or
signing into) **their own** Fulcra account. Whatever the agent learns persists in
*their* Fulcra account and outlives the throwaway sandbox. Two consequences drive
the whole design:

1. **Zero meaningful persistence in the sandbox** — it's disposable; persistence
   is Fulcra's job.
2. **No Fulcra credentials anywhere in our infra** — each guest authenticates with
   their own browser via Fulcra's device-code flow.

## Components

```
operator ── fhd-spawn ──▶ Daytona API ──▶ private sandbox (from snapshot)
                                          │  • OpenRouter key written to ~/.hermes/.env
                                          │  • /opt/fhd/start-chat.sh runs:
                                          │      - fetch latest onboarding skill
                                          │      - launch Hermes dashboard on 127.0.0.1:9119
                                          │      - launch Caddy on :8080 (lockdown proxy)
operator ◀── signed preview URL ──────────┘
guest ── URL ──▶ Caddy :8080 ──▶ dashboard chat (PTY → hermes --tui)
                 (403s admin/key endpoints; proxies SPA + chat)
agent ── fulcra-api auth login (device code) ──▶ guest logs in in their own browser
agent ── fulcra-api ... ──▶ the guest's own Fulcra account (read/write memory)
[30 min idle] ─▶ sandbox auto-stops    [fhd-teardown] ─▶ deleted
```

- **The snapshot (`fhd-hermes-demo`)** is a reusable Daytona image built once from
  `src/fhd/image.py`. It bakes: `uv` + the Fulcra CLI, Hermes (configured for
  OpenRouter), Caddy, and a fallback copy of the onboarding skill. **The image
  itself is built and stored on Daytona — this repo only holds its *definition*
  (code); there is no image binary checked in.**
- **`spawn.py`** creates one private sandbox per guest, injects the OpenRouter key
  into `~/.hermes/.env`, starts `start-chat.sh`, and returns a signed preview URL.
- **`start-chat.sh`** is the per-sandbox boot script (the guest-facing entry point).
- **`teardown.py`** lists/deletes guest sandboxes (labelled `fhd=guest`).

## The non-obvious decisions

### Guest UI = the Hermes dashboard, locked down by Caddy
The Hermes dashboard's chat is the nicest guest surface, but the dashboard is a
full **admin console**: its `KEYS` tab is a live secrets manager (it can reveal and
edit the OpenRouter key) and it exposes endpoints to change config, model, cron,
install plugins, restart the gateway, etc. Handing that to a guest is unacceptable.

So the dashboard binds **`127.0.0.1` only**, and **Caddy** (`assets/caddy/Caddyfile`)
sits on `:8080` as the sole public surface. It's a default-proxy + **denylist**:
the SPA, static assets, chat websockets, and read-only status endpoints pass
through; every secret/admin/exec endpoint returns **403**
(`/api/env`, `/api/env/reveal`, `/api/config`, `/api/cron`, `/api/providers`,
`/api/dashboard/agent-plugins/*`, `/api/model/set`, `/api/gateway/*`,
`/api/hermes/*`, `/api/logs`, …). The `KEYS` tab loads but renders blank.

Caddy also **rewrites `Host`/`Origin` to localhost** on the way upstream. Without
this the dashboard sees a proxied (non-local) request and engages its OAuth gate,
which kills the chat session.

### `--insecure` is required for the chat to go live
The dashboard's PTY chat session only connects when the dashboard is run with
`--insecure` (`start-chat.sh`). It still binds `127.0.0.1` and is only reachable
through Caddy, so "insecure" here just means "don't engage the localhost OAuth
gate" — the actual lockdown is Caddy's job.

### Approvals bypassed via `HERMES_YOLO_MODE`, not config
Hermes pops an in-chat "[HIGH] approval required" prompt for "dangerous" commands
(e.g. the uv installer's `curl | sh`), forcing the guest to click "Always allow".
The config key `approvals.mode=yolo` is **not** wired to this; Hermes only honors
the **`HERMES_YOLO_MODE=1` env var** (it's exactly what the `--yolo` flag sets).
`start-chat.sh` exports it before launching the dashboard, so the dashboard and the
`hermes --tui` chat it spawns both inherit it and run prompt-free. Acceptable
because the sandbox is ephemeral and isolated.

### Onboarding skill: baked fallback + fetched on boot
`hermes skills install` is blocked by Hermes's built-in security scanner (it
false-flags the Fulcra API calls as "exfiltration"), so the skill is delivered by
**copying files** into `~/.hermes/skills/`. The image bakes a copy as a fallback,
and `start-chat.sh` **re-clones the latest skill from GitHub at boot**, so skill
updates reach new sandboxes with no image rebuild. Source is overridable via
`FULCRA_SKILL_REPO` / `FULCRA_SKILL_SUBPATH`. Trade-off: a bad commit on the
skill's default branch reaches new demos immediately (the fallback only catches
fetch *failures*, not a valid-but-broken skill).

### Node on PATH; web UI builds at first launch
The Hermes-bundled Node lives at `/root/.hermes/node/bin`, which isn't on PATH by
default — so the image's `PATH` and `start-chat.sh` both add it, or the dashboard's
first-launch web build fails with "npm is not available". We deliberately do **not**
prebuild the web bundle in the image (its dev deps, e.g. `tsc`, aren't present from
the pip install); the dashboard builds it itself on first launch (~15s), which
`start-chat.sh`'s health-poll waits for.

### Signed preview URL + private sandbox
Sandboxes are created `public=False`; `spawn.py` returns
`create_signed_preview_url(8080, ttl)`. The signed URL carries its own token, so
only someone with the link gets in — the predictable non-signed URL does not.
Daytona shows a one-time "Preview URL Warning" interstitial (one click), removable
org-wide in Daytona settings.

## Security model (demo-grade)

- **Access** = "only invitees get the signed link" + ephemerality (30-min idle
  auto-stop). There is **no per-user login**; this is for a small, trusted invite
  list, not public distribution.
- **Residual risk (important):** the guest is talking to an agent with a shell, so
  a determined guest could ask it to print its own `~/.hermes/.env`. Locking the
  dashboard does **not** prevent this — and neither would any chat-with-shell-agent
  surface. **Mitigation: use a disposable, low-credit-cap OpenRouter key for
  demos and rotate it after.**

## Network egress is tier-gated (Daytona) — required for the data path

⚠️ **The Fulcra data path only works on a Daytona account at Tier 3+.** Daytona
applies a [tier-based egress policy](https://www.daytona.io/docs/en/network-limits/):

- **Tiers 1–2:** sandbox egress is locked to a default allowlist of "essential
  services" — package registries (PyPI/NPM), Git (GitHub/GitLab), container
  registries, CDNs, and AI/ML APIs. So OpenRouter (model), auth0 (Fulcra login),
  PyPI and GitHub (uv + skill fetch) all work — but **`api.fulcradynamics.com` is
  not on the list and gets a TLS reset.** This is *not* overridable per sandbox
  (`update_network_settings` returns "Network access is restricted and cannot be
  overridden at the sandbox level").
- **Tiers 3–4:** full internet access with configurable network settings.

Consequence: on a low tier the agent **logs in fine but every Fulcra data call
fails**, because login uses auth0 (allowed) and data uses `api.fulcradynamics.com`
(blocked). Symptom is `Connection reset by peer` on the TLS ClientHello; a curl to
`https://openrouter.ai` succeeds from the same sandbox, which proves it's an
allowlist, not a total block. Fix = move the Daytona account to Tier 3+ (dashboard
or support@daytona.io). Once there, egress is open; if you want to be explicit you
can pass `network_allow_list` (comma-separated IPv4 CIDRs) via
`CreateSandboxFromSnapshotParams` in `spawn.py`.

## Cost (Daytona)

Default sandbox is 1 vCPU / 1 GiB / 3 GiB disk. Rates: vCPU $0.0504/h, RAM
$0.0162/h, disk under the 5 GB free tier. Worst case (a tab pinned open for a full
24h) ≈ **$1.60/sandbox/day**; if it idle-stops at 30 min, it's a few cents. All
drawn from Daytona's $200 free credit first. `fhd-teardown --all` zeroes it out.

## Files

| Path | Role |
|---|---|
| `src/fhd/config.py` | Load + validate `.env` (Daytona / OpenRouter) |
| `src/fhd/image.py` | Declarative Daytona image definition |
| `src/fhd/build_snapshot.py` | Build/register the `fhd-hermes-demo` snapshot (idempotent) |
| `src/fhd/snapshot_params.py` | Pure helper: per-guest sandbox kwargs |
| `src/fhd/spawn.py` | Spawn one guest sandbox → signed preview URL |
| `src/fhd/teardown.py` | List / delete guest sandboxes |
| `assets/hermes/SOUL.md`, `AGENTS.md` | Agent persona + onboarding directive |
| `assets/hermes/start-chat.sh` | Per-sandbox boot: skill fetch + dashboard + Caddy |
| `assets/caddy/Caddyfile` | The lockdown reverse proxy |
