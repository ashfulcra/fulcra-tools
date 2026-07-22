# Wake Router + Engagement Model — build spec (stage 1: consolidated brainstorm → spec)

**Owner:** Tycho (`coord-boss`). **Implementers:** `coord-maintainer`, Fabio (`coord-fable-worker`).
**Review gate:** `codex-reviewer` + owner, dual-green at every stage (spec → plan → each SDD task).
**Authorization:** Ash 2026-07-21 (proposal `4c8f6f02`), ownership directive relayed 2026-07-22
(`04828838`). Sequencing prerequisites shipped in PR #441 (`5be9564`): head-of-line un-starve +
blocked-on-human-first fold. **ATC is untouched by this build.**

## 1. Problem

Three incidents, one cause — the fleet has no wake policy, only per-agent listeners:

- **Token burn without work:** every agent runs its own poll loop whether or not its workstream is
  active ("I don't wanna waste tokens on agents looping forever" — Ash). N listeners each doing
  O(fold) transport per tick is the fleet's largest idle cost.
- **Unwakeable agents:** desktop/occasional agents (no loop) miss directed work for days; the only
  current fix is a human remembering to open the app.
- **False liveness:** a host-side beat kept ticking for 2.5h after its session died
  (coord-maintainer incident, 2026-07-22). Presence said live; nobody was home. The inverse also
  holds: my cloud session works for hours between beats and reads stale. Liveness derived from
  beats alone is wrong in both directions.

## 2. Shape of the fix

One **model-free fleet watcher** (the router) replaces N always-on listeners as the *primary* wake
path, and presence gains an **engagement declaration** so vacancy/escalation logic can distinguish
"parked by design" from "gone dark."

### Part A — Wake Router

- **One watcher, whole fleet:** a single process on a resident host (MBP-class) polls the store
  once/min and evaluates every agent's wake policy. Per-agent listeners drop to a 30–60 min
  safety-net cadence (defense in depth, not the primary path).
- **Policy, per agent:**
  1. *Busy-aware deferral* — an agent presence-marked busy gets non-urgent wakes queued to its next
     idle boundary.
  2. *Priority gating* — only P1 / directed-to-you items interrupt; reviews and FYIs batch.
  3. *Debounce* — coalesce to at most one wake per N minutes per agent.
- **Fan-out via the EXISTING wake adapters** — the router adds no new wake mechanism, it schedules
  the ones the fleet already uses: `codex exec resume` (works with the app closed), OpenClaw POST,
  cloud `send_later`/Routine fire, macOS notification (consent-gated harnesses), queued wake file
  consumed by the SessionStart briefing hook on next human open.
- **Webhook-swappable by construction:** when Fulcra webhooks ship, the poll leg is replaced by a
  webhook receiver feeding *the same router*; policy and adapters do not change. The interim build
  is therefore not throwaway — this is the pseudo-webhook infrastructure Ash asked to brainstorm,
  built so the real webhooks drop in.

### Part B — Engagement model

- **Presence schema addition:** `engagement: {mode: resident|session|occasional, until: <ts>}`.
  - `resident` — always-on host; expected to beat; staleness is meaningful.
  - `session` — bounded life; declares a TTL (`until`) at join. At expiry, the **host tick (zero
    model tokens)** parks continuity and releases roles per the idle-reaping rule. A session past
    TTL reads **PARKED — never gone-dark.** This is the structural fix for the false-liveness
    incident and the direct answer to coord-maintainer's DECIDE-WITH-ME liveness-substrate
    question: liveness = engagement-aware fold over (beats + activity), not beats alone.
  - `occasional` — **occasional model, resident host:** a desktop agent holds a role with *no loop
    at all*; the router's host-side legs cover it (resume-with-app-closed, human notification,
    queued wake file, digest batching), and **deputy coverage** lets a resident role execute small
    items for a dormant identity instead of waking it (existing vacancy-claim machinery).
- **Vacancy/escalation reads engagement.** Escalation fires on *unexplained* absence only. This is
  the condition for re-arming the fleet's disarmed escalate sweeps.
- **Activity-implies-liveness is consumed here** (coord-maintainer's routed P1, both constraints
  honored): every engine bus write refreshes the actor's presence beat, throttled to once per beat
  interval per process (burst of writes = one beat write), and a refresh failure never makes a
  succeeding write fail. The engagement fold treats recent *activity* as liveness proof — a busy
  agent needs no separate beat.

### Part C — Agent external identity (coord-maintainer's addendum, folded in)

Agents currently act on GitHub as `ashfulcra` with pull-only reach on upstream orgs — which is why
an approved upstream PR (community-skills#1) cannot be merged by any agent. Two candidate fixes:

- **GitHub App on `fulcradynamics`** with fine-grained per-repo permissions (contents +
  pull_requests write on maintained repos); the router is the natural holder of the installation
  token (it is the one resident, secretable process). **(Recommended — revocable, per-repo scoped,
  auditable as itself.)**
- **Machine-user account** — simpler to stand up, coarser to scope, one more seat to secure.

**DECISION REQUIRED (Ash, org-owner access):** App vs machine user, and who creates it. Until then
this leg designs to an interface (`external identity provider` handing the router short-lived
tokens) so Parts A/B do not block on it.

## 3. Restated CONCUR conditions (from the vacancy-consults-presence concurrence; they bind here)

1. **Presence-stale nudge visible** — when a fold consults presence and finds it stale, it says so
   in output; staleness is never silently swallowed into a default.
2. **Exact identity matching** — presence↔role↔wake matching on exact agent ids only; no
   substring/prefix heuristics (the `role@host` variant lesson).
3. **Dormancy independent** — declared dormancy (`occasional`, parked TTL) is a separate axis from
   staleness; a dormant identity must never read as abandoned, and vice versa.
4. **Mixed-fleet gate** — nothing that changes vacancy/escalation semantics ships until every
   harness in the live fleet (claude-code cloud + desktop, codex, OpenClaw, cron hosts) either
   emits the new signal or is explicitly defaulted; partial adoption must not misclassify
   non-upgraded agents.

## 4. Hard constraints

- **Never spawn working sessions:** the router wakes *existing* sessions via their own adapters or
  queues for human-opened ones; it never creates a new working session unilaterally (Ash's rule).
  Any adapter that would need to violate this surfaces to Ash instead.
- **Zero model tokens in the router:** watcher, policy evaluation, TTL parking, and wake fan-out
  are pure host-side code. Model tokens are spent only by the *woken* agent on real work.
- **Fail-closed secrets:** adapter credentials and the (future) installation token live in host
  keychain / environment config — never in team paths (durable-state doctrine).
- **ATC fence:** no changes to usage/headroom/route/atc/dash or `fulcra-agent-atc`.
- **Store remains the bus:** the router is a *reader* of the same shards agents already write; no
  new coordination channel.

## 5. Deliverables & stage plan

| Stage | Artifact | Gate |
|---|---|---|
| 1 (this doc) | Consolidated spec | dual-green: codex-reviewer + coord-boss |
| 2 | Implementation plan: task DAG, schema diffs (presence `engagement`, router config file format, wake-queue shard shape), rollout order honoring the mixed-fleet gate, test plan (red-first for every fold change) | dual-green |
| 3+ | SDD execution — tasks assigned explicitly on the bus to coord-maintainer / coord-fable-worker; per-task review by codex-reviewer; engine changes land behind the mixed-fleet gate | dual-green per task |

Rollout sketch (detail belongs to stage 2): schema + folds first (engagement read/write, inert),
then router read-only shadow mode (logs what it *would* wake — measured against live listener
behavior), then adapter fan-out enabled per-agent, then listener cadence drawdown, then escalate
re-arm. Each step reversible; shadow-mode divergence is the acceptance evidence for going live.

## 6. Non-goals

Cross-account federation (BUS-78 — needs folder sharing), replacing the coordination store or
listeners entirely (safety-net cadence stays), tracker-bridge changes, any ATC coupling, webhook
receiver implementation before Fulcra ships webhooks (we build the socket it plugs into, not the
plug).

## 7. Open decisions (blocked-on-Ash ledger, led per standing rule)

1. **External identity: GitHub App vs machine user** — and who creates it (org-owner access).
   Recommendation: App, created by Ash, installed on `fulcradynamics` maintained repos only.
2. **Router host designation** — which resident box is the router's home (MBP assumed; confirm),
   acknowledging today's evidence that desktop hosts are "unstable" by Ash's own assessment — the
   router must tolerate its own host dying (safety-net listener cadence is the fallback).
3. **TTL defaults** — proposed: session agents default `until = join + 8h` unless declared;
   confirm or adjust.
