# Three-Agent Coordination Demo — Operator Runbook

**Date:** 2026-06-02
**What it proves:** three real agents — on two different machines, from three
different vendors, with **no shared memory, no shared process, and no direct
calls between them** — coordinate on one piece of work by reading and writing a
shared task bus on Fulcra Files. The bus is the *only* thing linking the two
machines.

| # | Agent | Host | Reached via |
| - | ----- | ---- | ----------- |
| 1 | Claude Code | this Mac (`DeskbookPro`) | terminal in the repo |
| 2 | Codex | ChatGPT desktop, this Mac (`DeskbookPro`) | the Codex window |
| 3 | OpenClaw | **Mac mini** (`macmini`) | Telegram / Discord chat |

The Mac mini being independently authed to the **same Fulcra account** is the
cross-machine proof: nothing on this Mac can reach it except through the bus.

---

## 0. Preconditions / setup checklist

Run this **before** the audience is watching.

**On every host (this Mac, the Mac mini):**

- [ ] Files-capable `fulcra-api` installed and **authed to the same Fulcra
      account** — `fulcra-api auth login`.
- [ ] `fulcra-coord` installed (on PATH, or reachable via `python -m fulcra_coord`).
- [ ] Same coordination root exported in every shell that will run a command:
      ```bash
      export FULCRA_COORD_REMOTE_ROOT=/coordination-demo
      ```
- [ ] Per-host health check — expect `Remote access: OK`:
      ```bash
      fulcra-coord doctor
      ```
      If the **Mac mini** doctor does not show `Remote access: OK`, stop and fix
      its auth — the cross-machine beat depends on it.

**Once, from any authed host (seed the scenario):**

```bash
FULCRA_COORD_REMOTE_ROOT=/coordination-demo uv run python scripts/demo_seed.py
```

Expected tail:

```
Seeded coordination root: /coordination-demo
  Total tasks: 6
  By status:
    active     3
    waiting    1
    blocked    1
    done       1
  Stale (forgotten) tasks flagged: TASK-DEMO-backfill

  Uploaded: 6/6 tasks, 11 views
```

**Verify the bus from a *second* host** (ideally the Mac mini — proves both
machines see the same state):

```bash
fulcra-coord agents
```

You should see the four-agent digest below. If you do, the demo is live.

> The seed is **idempotent** — fixed task ids mean a reseed overwrites cleanly.
> Re-run it any time the state drifts.

---

## The seeded scenario (workstream `search` — "ship the search feature")

| Task id | Owner | Status | P | One-liner |
| ------- | ----- | ------ | - | --------- |
| `TASK-DEMO-search-api` | `claude-code:DeskbookPro:search` | active | P1 | Implement `/search` API endpoint |
| `TASK-DEMO-infra-cluster` | `openclaw:macmini:infra` | active | P2 | Provision search cluster (Terraform) |
| `TASK-DEMO-query-parser` | `codex:DeskbookPro:search` | waiting | P2 | Refactor query parser for filters |
| `TASK-DEMO-prod-index` | `claude-code:DeskbookPro:search` | blocked | P1 | Enable prod search index (TICKET-4412) |
| `TASK-DEMO-backfill` | `claude-code:DeskbookPro:backfill` | **active, ~4h stale ⚠** | P2 | Backfill historical docs into index |
| `TASK-DEMO-staging-cluster` | `openclaw:macmini:infra` | done (~2h ago) | P2 | Stand up staging search cluster |

The backfill task is the deliberate "falling through the cracks" item: an active
task that hasn't been touched in ~4h, past the 2h staleness threshold.

---

## Install beat — one line per window

Run each in its window so every surface auto-surfaces coordination state.

**Claude Code (this Mac):**
```bash
fulcra-coord install-claude-code
```
> Expected: `Installed Claude Code hooks (global) -> …/settings.json` plus
> `+ SessionStart`, `+ PreCompact`, `+ Stop`, and
> `New Claude Code sessions will now surface in-flight work…`.

**Codex (ChatGPT desktop, this Mac):**
```bash
fulcra-coord install-codex
```
> Expected: `Installed Codex hooks -> …/hooks.json` with `+ SessionStart`,
> `+ PreCompact`, and the note *"No Stop hook by design… end-parking is
> delegated to the heartbeat."*

**OpenClaw (in chat, on the Mac mini):**
```bash
fulcra-coord install-openclaw
fulcra-coord install-heartbeat
```
> Expected: `Installed OpenClaw Track A artifacts -> …` (boot + shutdown hooks),
> then `Installed fulcra-coord heartbeat (launchd) — reconcile every N min.` The
> heartbeat is the safety net that sweeps stale tasks on the always-on mini.

---

## Beat 1 — the mesh sees itself

**What this proves:** every agent, on either machine, reads the *same* picture of
who is doing what, what's blocked, and what's been forgotten — with one command.

**Claude Code window** and **Codex window:**
```bash
fulcra-coord agents
```

Expected rendered output (identical on both, and on the Mac mini):

```
============================================================
  Fulcra Coordination — Agents
============================================================

  claude-code:DeskbookPro:backfill  (active 1 / waiting 0 / blocked 0)
    [ACTIVE] [P2] TASK-DEMO-backfill ⚠  Backfill historical documents into index
          next: Monitor job, verify counts, then mark done.

  claude-code:DeskbookPro:search  (active 1 / waiting 0 / blocked 1)
    [ACTIVE] [P1] TASK-DEMO-search-api  Implement /search API endpoint
          next: Add cursor pagination, then integration tests for /search?q=
    [BLOCKED] [P1] TASK-DEMO-prod-index  Enable prod search index

  codex:DeskbookPro:search  (active 0 / waiting 1 / blocked 0)
    [WAITING] [P2] TASK-DEMO-query-parser  Refactor query parser for filters
          next: Resume once the /search contract is frozen (see TASK-DEMO-search-api).

  openclaw:macmini:infra  (active 1 / waiting 0 / blocked 0)
    [ACTIVE] [P2] TASK-DEMO-infra-cluster  Provision search cluster (Terraform)
          next: Apply prod plan after cost-review sign-off.
```

Note the `⚠` on `TASK-DEMO-backfill` — the stale marker.

**OpenClaw chat (Mac mini)** — ask in natural language:

> "what's the team working on, what's blocked, anything falling through the cracks?"

Expected answer shape (it reads the same bus, so it names the same facts):

> The team is shipping the **search** feature. Claude Code is actively building
> the `/search` API endpoint (wiring pagination) and I'm provisioning the search
> cluster — staging is up, prod plan is waiting on cost-review sign-off. Codex
> has the query-parser refactor parked until the `/search` contract is frozen.
> **Blocked:** enabling the prod search index is stuck on SRE creds approval
> (TICKET-4412). **Falling through the cracks:** the historical-document
> **backfill** has been active ~4h with no update — it looks forgotten and
> should be checked.

**Hosted-ChatGPT path (Custom GPT):** ask the Custom GPT the same question. It
calls the facade's `GET /coordination/status` and produces the same shape,
naming the stale backfill task. (Facade setup: see
`adapters/chatgpt/facade/README.md`.)

---

## Beat 2 — automatic, not asked

**What this proves:** the coordination state shows up with **no command typed** —
it's injected at session start.

Open a **fresh Claude Code session** in the repo. The `SessionStart` hook
injects the in-flight + stale summary before you type anything:

```
[fulcra-coord] Coordination state on /coordination-demo:
  Active: /search API endpoint (claude-code), search cluster (openclaw)
  Waiting: query-parser refactor (codex) — needs /search contract frozen
  Blocked: prod search index — SRE creds approval (TICKET-4412)
  ⚠ Possibly forgotten (stale >2h): TASK-DEMO-backfill — Backfill historical
    documents into index (active ~4h, no update)
  Run `fulcra-coord agents` for the full digest.
```

The ⚠ stale line is the payoff: the agent proactively flags the forgotten
backfill task at boot.

---

## Beat 3 — live handoff across machines + vendors

**What this proves:** a baton handed off on this Mac is picked up on the Mac mini
(and in ChatGPT) **instantly**, through the bus alone.

**In the Claude Code window**, pause the search-API task with a new next step:
```bash
fulcra-coord pause TASK-DEMO-search-api \
  --next "smoke-test /search?q=test then mark done" \
  --agent claude-code:DeskbookPro:search
```
> Expected: `Paused: TASK-DEMO-search-api` and `Next: smoke-test /search?q=test then mark done`.

**Immediately re-ask ChatGPT / OpenClaw:** "what's next on the search API?"

Expected answer (reflects the new `next_action` with no other prompt):

> Next on the `/search` API is to **smoke-test `/search?q=test` and then mark it
> done.** Claude Code just paused it at that step.

**Optional claim** — have Codex or OpenClaw pick it up:
```bash
fulcra-coord update TASK-DEMO-search-api --status active \
  --agent codex:DeskbookPro:search \
  --summary "Picking up smoke tests for /search"
```
Re-run `fulcra-coord agents` anywhere: the task now sits under
`codex:DeskbookPro:search`.

**The line to say out loud:** *the baton crossed machines and vendors with no
API between them — only the shared bus.*

---

## Fallbacks (if a live hook or ChatGPT beat hiccups)

- **Any beat** can fall back to a CLI window:
  ```bash
  fulcra-coord agents          # deterministic, always works
  fulcra-coord status          # fuller per-status listing
  ```
- **SessionStart didn't inject (Beat 2):** run `fulcra-coord agents` and narrate
  "this is exactly what the fresh session was handed automatically."
- **ChatGPT/facade unreachable (Beat 1/3):** use the OpenClaw chat answer, or the
  CLI digest on the Mac mini — same bus, same facts.
- **State drifted:** reseed (`scripts/demo_seed.py`) — idempotent, ~2s.

---

## Teardown / reset

```bash
FULCRA_COORD_REMOTE_ROOT=/coordination-demo uv run python scripts/demo_seed.py --reset
```

Re-seeds clean state (overwrites all demo ids + views). Run it between rehearsals
and after the live demo. Optionally uninstall the hooks per host:

```bash
fulcra-coord install-claude-code --uninstall
fulcra-coord install-codex --uninstall
fulcra-coord install-openclaw --uninstall   # on the mini
fulcra-coord install-heartbeat --uninstall  # on the mini
```
