# 3-Agent Coordination Demo — Run of Show

Tightened beat-by-beat script with **real captured outputs** (rehearsed end-to-end from the source machine against `/coordination-demo`). Timings are targets for a ~4-minute live run. Each beat has a deterministic fallback.

## Preconditions (per host, once)
Run `scripts/demo-setup.sh --agent-type <claude-code|codex|openclaw> --seed --root /coordination-demo` on each host. All three hosts must be `fulcra-api`-authed to the **same** Fulcra account.
- **Seed host:** seed with the LIVE local hostname so each agent's owner-exact "mine" matches — `demo_seed.py` now defaults `--local-host` to `hostname -s` (openclaw stays `macmini`). Verify: `fulcra-coord agents` shows 4 agents and the ⚠ on `TASK-DEMO-backfill`.
- Reset between run-throughs: `python scripts/demo_seed.py --reset`.

---

## Beat 1 — "The mesh sees itself" (~60s)
**Do:** in the Claude Code and Codex windows run `fulcra-coord agents`; in OpenClaw chat ask *"what's the team working on, what's blocked, anything falling through the cracks?"* (hosted-ChatGPT: ask the Custom GPT the same).
**Real output (`fulcra-coord agents`):**
```
  claude-code:<host>:backfill  (active 1 / waiting 0 / blocked 0)
    [ACTIVE] [P2] TASK-DEMO-backfill ⚠  Backfill historical documents into index
          next: Monitor job, verify counts, then mark done.
  claude-code:<host>:search    (active 1 / waiting 0 / blocked 1)
    [ACTIVE] [P1] TASK-DEMO-search-api  Implement /search API endpoint
          next: Add cursor pagination, then integration tests for /search?q=
    [BLOCKED] [P1] TASK-DEMO-prod-index  Enable prod search index
  codex:<host>:search          (active 0 / waiting 1 / blocked 0)
    [WAITING] [P2] TASK-DEMO-query-parser  Refactor query parser for filters
  openclaw:macmini:infra       (active 1 / waiting 0 / blocked 0)
    [ACTIVE] [P2] TASK-DEMO-infra-cluster  Provision search cluster (Terraform)
```
**Says:** three vendors, two machines (`<host>` + `macmini`), no shared memory — one truth. **Fallback:** the CLI `agents` digest is deterministic; if a chat agent is slow, read the CLI.

## Beat 2 — "Automatic, not asked" (~60s)
**Do:** open a *fresh* Claude Code session in the `search` repo. The SessionStart hook auto-injects (no command typed):
**Real injected context:**
```
Fulcra coordination — open work on the shared bus:
  [BLOCKED] TASK-DEMO-prod-index — Enable prod search index
  [ACTIVE]  TASK-DEMO-search-api — Implement /search API endpoint
      next: Add cursor pagination, then integration tests for /search?q=
  ⚠ Possibly-forgotten (active, no recent update):
      TASK-DEMO-backfill — Backfill historical documents into index (agent claude-code:<host>:backfill)
  To resume: fulcra-coord update <id> --status active --agent claude-code:<host>:search
```
…and the **session title** is set to *"Implement /search API endpoint."*
**Says:** the forgotten-background-work rescue, automatic — you didn't ask. **Fallback:** if the hook is slow, run `fulcra-coord agents --mine claude-code:<host>:search`.

## Beat 3 — "Live handoff" (~60s)
**Do:** in Claude Code, `fulcra-coord pause TASK-DEMO-search-api --next "smoke-test GET /search?q=test, then mark done" --agent claude-code:<host>:search`. Re-ask ChatGPT/OpenClaw "what's next on the search API?" — they reflect the new next_action instantly.
**Real output:**
```
Paused: TASK-DEMO-search-api
  Next: smoke-test GET /search?q=test, then mark done
# fulcra-coord agents --mine ... now shows:
  [WAITING] [P1] TASK-DEMO-search-api  Implement /search API endpoint
        next: smoke-test GET /search?q=test, then mark done
```
**Says:** a baton crosses machines + vendors in real time, zero direct connection. **Optional capstone:** `fulcra-coord broadcast "freeze the /search contract — everyone sync"` → appears in every agent's inbox (per-agent ack), or `fulcra-coord tell codex:<host>:search "pick up the parser"`.

---

## Hosted-ChatGPT path (if showing plain ChatGPT, not Codex-desktop)
Run the facade (`adapters/chatgpt/facade/run-demo.sh`, token in env) → tunnel (`cloudflared`/`ngrok`) → Custom GPT from `adapters/chatgpt/custom-gpt/` (paste INSTRUCTIONS, import openapi with the tunnel URL, Bearer = facade token). Verified live: authed `POST /coordination/report` creates a real task; `GET /coordination/status` returns the live index; missing token → 401.

## Teardown
`python scripts/demo_seed.py --reset` (or delete `/coordination-demo` files). The demo never touches the real `/coordination` root.
