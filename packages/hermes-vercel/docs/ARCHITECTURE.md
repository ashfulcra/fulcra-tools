# fulcra-hermes-vercel — architecture

> **Note (2026-06-03):** This is the pre-deploy design doc. For the
> current state, open questions for the next operator, runbook, and cost
> model, read **[`HANDOFF.md`](HANDOFF.md)** first. The "Open TODOs"
> section below is largely resolved — the SDK shape questions were
> answered during the build and live in the code now. The "Hosting
> LiteLLM" section recommends Railway; we pivoted to Fly (see HANDOFF
> § 4.5).


Port of the Fulcra "press play" demo from Daytona to **Vercel Sandbox**, plus a
**LiteLLM AI-gateway** so the agent never carries a real provider key. Successor to
the original Daytona port, now a standalone repo at
[`ashfulcra/fulcra-hermes-daytona`](https://github.com/ashfulcra/fulcra-hermes-daytona).

## Why move

Daytona's tier-1/2 egress allowlist blocks `api.fulcradynamics.com`, killing the
demo's data path. Vercel Sandbox uses Firecracker microVMs with open egress
([verified](#egress-verified) live: 200/401 to all destinations including
`api.fulcradynamics.com` and `www.google.com` — both reset on Daytona). That
removes the blocker entirely; tier upgrade no longer needed.

## (2) The runtime — Vercel Sandbox

Same shape as the Daytona setup, three scripts:

```
npm run build               # one-time: build the fhv-hermes-demo snapshot (~3-5 min)
npm run spawn <guest>       # per-guest: spawn a sandbox, return a preview URL
npm run teardown --all      # cleanup
```

### Snapshot lifecycle

Vercel's [Snapshot](https://vercel.com/docs/sandbox/concepts/snapshots) primitive
captures a configured sandbox's disk + state. `build-snapshot.js` provisions a
base sandbox, installs the full stack, and snapshots it; every guest spawn
starts from that snapshot (no per-spawn install delay).

Snapshots **expire 30 days** after last use (configurable). Rebuild after any
image/asset change.

### Stack baked into the snapshot

| Component | How |
|---|---|
| `uv` | `curl https://astral.sh/uv/install.sh \| sh` (installs to `$HOME/.local/bin`) |
| `fulcra-api` CLI | `uv tool install fulcra-api` |
| Hermes agent | `curl ... install.sh \| sudo bash -s -- --skip-browser --skip-setup` |
| Caddy v2 | static binary from `caddyserver.com/api/download` to `/usr/local/bin/caddy` |
| `fulcra-onboarding` skill | git clone, copied into `$HOME/.hermes/skills/fulcra/` (fallback; fetched-on-boot freshens it) |
| Hermes config | `model.provider=openrouter`, `model.default=anthropic/claude-sonnet-4.5` |

### Differences from the Daytona port (real)

- **OS**: Amazon Linux 2023 (not Debian). `dnf install -y git procps-ng socat ca-certificates` (note `procps-ng`, not `procps`).
- **User**: `vercel-sandbox` with sudo in `/vercel/sandbox`; Hermes installed `sudo` to land in `/usr/local`. Per-user files live under `$HOME/.hermes`.
- **Caddy mount**: assets land in `/opt/fhv/` (not `/opt/fhd/`).
- **Preview URL**: Vercel's port-mapped subdomain (auto-generated), not a Daytona signed URL. Same shareable-link UX; access control is "only people with the link," same as Daytona.
- **TTL**: capped at **45 min on Hobby** / **5 h on Pro**. The Daytona `AUTO_STOP_MINUTES = 240` needs Pro to match.

### Node 26 / undici compatibility (open issue)

The vendor SDK (`@vercel/sandbox@2.0.2`) is broken on Node 26 in two ways
(reproduced + diagnosed in the `vercel-sandbox-trial/` sibling project):

1. **Brotli response bodies aren't decoded** — every `runCommand` fails. Worked
   around by forcing `Accept-Encoding: identity` via a fetch shim
   ([`src/fetch-shim.js`](../src/fetch-shim.js), imported at the top of every
   entry script).
2. **Response headers stripped to `{}`** — the SDK's strict `content-type ===
   "application/x-ndjson"` check throws on null content-type. Worked around with
   a 2-line tolerance patch in `node_modules/.../api-client.js` (not portable;
   the real fix is Node 22 LTS).

**Recommended: pin Node 22 LTS** for production runs. The fetch shim stays as a
defense-in-depth; the node_modules patch goes away on Node 22.

## (3) Key-leak architecture — LiteLLM gateway with virtual keys

### Problem

In the baseline (Daytona today, Vercel today): the operator's OpenRouter key is
written into the sandbox's `~/.hermes/.env`. A curious guest in the chat can ask
the agent to `cat ~/.hermes/.env` and exfiltrate it. We rely on a low-cap
disposable key, but the leak is real.

### Solution

Front the model with an OpenAI-compatible proxy that **holds the real OpenRouter
key server-side** and accepts **per-sandbox virtual keys** with budgets + TTLs.

```
[Hermes in sandbox]  ──HTTPS──▶  [LiteLLM proxy you host]  ──HTTPS──▶  [OpenRouter]
   (knows: scoped                  (knows: real provider key,
    sk-... virtual key,             per-virtual-key budgets,
    ~$0.50 cap, 1h TTL)             rate limits, audit log)
```

### Why LiteLLM specifically

[LiteLLM Proxy](https://docs.litellm.ai/docs/proxy/virtual_keys) is the
open-source standard for this pattern. Out of the box:

- OpenAI-compatible API (Hermes can point at it with `base_url:` override).
- Admin endpoint `POST /key/generate` mints virtual keys with `max_budget`,
  `duration`, `key_alias`, `metadata`.
- `DELETE /key/{key}` revokes.
- Tracks spend per key; rejects when budget exhausted.
- Postgres-backed key store; can scale.

Managed alternatives (Cloudflare AI Gateway, Vercel AI Gateway, Helicone,
Portkey) don't expose per-virtual-key + budget + TTL with the same operational
simplicity. LiteLLM is the right pick.

### Flow per guest

1. `spawn.js` calls `LITELLM_URL/key/generate` with `{max_budget: 0.5, duration: "1h", key_alias: "fhv-<guest>-<ts>"}` → gets `sk-fhv-...`.
2. Sandbox env: `OPENROUTER_BASE_URL=<litellm-url>`, `OPENROUTER_API_KEY=<sk-fhv-...>`.
3. `start-chat.sh` translates that into Hermes config: `model.provider=custom`, `model.base_url=<litellm-url>`. (Hermes's OpenRouter integration also supports a `base_url:` override at the `custom` provider — confirm shape in [Hermes provider docs](https://hermes-agent.nousresearch.com/docs/integrations/providers).)
4. Guest can `cat ~/.hermes/.env` and find a `sk-fhv-...` key — but it's worth ≤ $0.50, expires in 1h, and is **rejected by every endpoint except your LiteLLM proxy**.
5. On `teardown` → `DELETE /key/{key}` to revoke immediately.

### Hosting LiteLLM

Recommendation: **Railway** (push-to-deploy, managed Postgres, ~$5/mo).
Alternatives: Fly.io machine (free tier), Render (free tier with sleep).

Operator deploy: standard LiteLLM Docker image + a small `config.yaml` pointing
at OpenRouter, plus a Postgres URL. One-time setup. Master key (the operator's
admin secret) stays out of every sandbox; only the virtual keys flow through.

## Open TODOs (must close before live demos)

The stubs in `src/` are clear and correct in shape, but a few SDK details need
live verification — small, mostly one-line confirmations:

- [ ] `Sandbox.create({ source: { snapshot: <id> } })` — confirm the exact param shape for spawning from a snapshot. (v2 docs say snapshots are first-class; the field may be `source.snapshot` or just `snapshot`.)
- [ ] `sandbox.snapshot({ name })` return shape — `{id, name}` or just `id`? (build-snapshot.js logs whichever exists.)
- [ ] Preview URL for port 8080 — likely `sandbox.domain(8080)` or `sandbox.routes` exposes it. Need the working pattern.
- [ ] `Sandbox.list({...})` for teardown — confirm static method vs instance, filter shape.
- [ ] `sandbox.fs.writeFile(path, contents)` — confirm signature for uploading the asset files.
- [ ] Hermes `model.base_url` for a `custom` provider — confirm via Hermes docs that pointing the OpenRouter-flavored config at a LiteLLM proxy works without code changes.

Each of these is a 5-minute live check once the new `VERCEL_TOKEN` is in
`vercel-sandbox-trial/.env` — I'll do them in one pass before running
`npm run build` for real.

## Egress verified

From [`vercel-sandbox-trial/`](../../vercel-sandbox-trial) (same fulcra-dynamics
team, Hobby plan, 2026-05-30):

```
https://api.fulcradynamics.com   exit=0  401   ← reached server; 401 is auth-required (no token)
https://www.google.com           exit=0  200
https://openrouter.ai            exit=0  200
https://github.com               exit=0  200
```

Same destinations on Daytona Tier 1/2: `api.fulcradynamics.com` and `google.com`
were `Connection reset by peer` (TLS reset). The whole reason for the move.
