# litellm

LiteLLM proxy for the Fulcra demo. Holds the real OpenRouter key server-side
and mints per-sandbox **virtual keys** with budgets + TTLs. The Hermes agent
in each [`hermes-vercel`](../hermes-vercel) sandbox uses a
virtual key against this proxy; if a guest exfiltrates it, the worst case is
bounded to that key's cap (≤ $25 friendly / ≤ $15 marketing) and dies at the
TTL.

**Hosted at:** `https://fulcra-litellm.fly.dev`
**Full architecture + handoff:** [`../hermes-vercel/docs/HANDOFF.md`](../hermes-vercel/docs/HANDOFF.md)

## Architecture

```
[Hermes in Vercel Sandbox] ──HTTPS──▶ [this proxy on Fly] ──HTTPS──▶ [OpenRouter]
   (per-sandbox sk-litellm-…           (real sk-or-…,
    bearer; capped + TTL'd)             per-virtual-key budgets + audit)
```

Sandboxes get a `sk-litellm-…` virtual key, NOT the real OpenRouter key.
LiteLLM enforces the budget per key and refuses calls past it.

## Deploy to Fly.io

You do this once. ~10 minutes including the Postgres add-on.

```bash
cd ~/Developer/fulcra-litellm
fly auth login                       # browser flow
fly apps create fulcra-litellm       # or accept the generated name and update fly.toml
fly postgres create --name fulcra-litellm-db --region iad
fly postgres attach fulcra-litellm-db --app fulcra-litellm   # auto-sets DATABASE_URL
fly secrets set \
  OPENROUTER_API_KEY=sk-or-v1-...your-capped-key... \
  LITELLM_MASTER_KEY=sk-litellm-$(openssl rand -hex 32)
fly deploy                           # builds Dockerfile + boots the machine
```

**Save the master key now.** It's not easily retrievable later (would need
`fly ssh console` into the machine to read the env).

Verify:

```bash
curl https://fulcra-litellm.fly.dev/health
# → {"status":"healthy"}
```

> **Note:** Fly's trial stops machines after 5 min. To keep the proxy
> always-on, add a credit card at <https://fly.io/trial>. Once added, the
> `min_machines_running = 1` + `auto_stop_machines = "off"` settings in
> `fly.toml` keep the proxy live indefinitely.

### Why Fly and not Railway

Earlier draft of this doc recommended Railway. We pivoted because the
operator's Railway free-tier quota was exhausted before the deploy
completed; Fly runs the same Docker image with similar pricing and simpler
always-on controls.

## Wiring into `fulcra-hermes-vercel`

Add to `hermes-vercel/.env`:

```
LITELLM_URL=https://fulcra-litellm.fly.dev/v1
LITELLM_MASTER_KEY=<same master key as Fly secret>
```

(Note the **`/v1`** suffix — LiteLLM exposes the OpenAI-compatible endpoint
at `/v1`. The `/key/generate` admin endpoint lives at the root, so spawn.js
strips the `/v1` when minting keys.)

Now `npm run spawn alice -- --mode friendly` will:

1. Call `POST /key/generate` with the friendly preset ($25 / 12h TTL).
2. Inject the resulting `sk-litellm-…` (NOT the real OpenRouter key) into
   the sandbox's `~/.hermes/.env`.
3. Hermes inside the sandbox switches `model.provider=custom` +
   `model.base_url=$LITELLM_URL` and routes all calls through the proxy.

## Modes (set per spawn in `hermes-vercel/src/spawn.js`)

| Mode | Key TTL | Budget | Worst-case leak |
|---|---|---|---|
| `friendly` | 12 h | $25 | $25 |
| `marketing` | 6 h | $15 | $15 |

Both well above a typical 2-hour session cost ($3–12 estimated). The
sandbox-side lifetime is independently capped at 5 h by Vercel Pro. The key
**outlives the sandbox** intentionally — a guest who comes back to a fresh
spawn within the TTL gets the same budget envelope.

## Files

- `Dockerfile` — extends `ghcr.io/berriai/litellm-non_root:main-stable`, bakes in `config.yaml`.
- `config.yaml` — wildcard model pass-through to OpenRouter, master key + DB URL from env.
- `fly.toml` — Fly app config. 1 GB RAM (bumped from 512 MB after OOM), region `iad` (co-located with Vercel Sandbox `iad1`), always-on.
- `.env.example` — checklist of variables (NOT a runtime config; secrets live in `fly secrets`).

## Cost

| Item | Cost |
|---|---|
| Fly `shared-cpu-1x` 1 GB, always-on | ~$5.70/mo |
| Fly Postgres (smallest dev tier) | ~$2–4/mo |
| Bandwidth | Free up to 160 GB/mo in NA — won't approach |
| LLM passthrough | At OpenRouter rate, **no markup**. ~$3–12 per real session. |

Idle floor: **~$8–10/mo flat**.

## Operations

```bash
fly logs --app fulcra-litellm        # tail logs
fly status --app fulcra-litellm      # machine state
fly ssh console --app fulcra-litellm # shell into the running machine

# Mint a virtual key by hand for debugging:
curl -X POST https://fulcra-litellm.fly.dev/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"max_budget": 5, "duration": "1h", "key_alias": "test"}'

# Revoke:
curl -X POST https://fulcra-litellm.fly.dev/key/delete \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-litellm-..."]}'
```

## TODO

- `hermes-vercel/src/teardown.js` does not yet `POST /key/delete` for
  the sandbox's virtual key. Auto-expiry covers it within the TTL, but
  immediate revocation is cleaner. ~30 min of work.
- Per-key spend dashboards: LiteLLM has an admin UI; expose it behind
  operator-only auth. Out of scope for the demo path itself.
