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

- **One watcher, whole fleet:** a single model-free process — **cloud-hosted per operator
  decision (Ash, 2026-07-22): the decision plane runs in a cloud session environment (designated:
  `coord-fable-worker`), never mandatorily on resident hardware** — polls the store at a fixed
  60s interval while its container lives (setup-script re-arm at creation; hourly Routine floor
  after reclaim; duty-cycle gated at acceptance) and evaluates every agent's wake policy. Per-agent listeners drop to a 30–60 min
  safety-net cadence (defense in depth, not the primary path).
- **Policy, per agent:**
  1. *Busy-aware deferral* — an agent presence-marked busy gets non-urgent wakes queued to its next
     idle boundary.
  2. *Priority gating* — only P1 / directed-to-you items interrupt; reviews and FYIs batch.
  3. *Debounce* — coalesce to at most one wake per N minutes per agent.
- **Fan-out via wake adapters, split by deployment status** (the harness matrix in
  [`EVENT-DRIVEN-WAKE.md`](EVENT-DRIVEN-WAKE.md) is canonical; this spec adds no claim it
  contradicts):
  - *Deployed today:* `codex exec resume <thread-id>` (exact persisted thread, works with the app
    closed — `scripts/wake/codex.sh`), OpenClaw authenticated `POST /hooks/wake`
    (`scripts/wake/openclaw.sh`), Claude Managed Agents `user.message` to an idle persisted
    session, macOS notification to the human for consent-gated harnesses.
  - *Proposed in this build (new code, stage-2 tasks):* a **queued wake file** consumed by the
    SessionStart briefing hook on next human open (local/desktop fallback lane), and a **platform
    scheduled-routine leg** for Claude Code web/cloud — which has **no documented exact-session
    inbound wake**; the router must NOT substitute a different Managed Agents session. For cloud
    agents the router only aligns work to the agent's own self-armed Routine cadence (as
    coord-boss and coord-fable-worker run today) or queues for it; it never creates sessions.
  - *Fail-visible unroutable lane:* an agent whose harness offers no supported adapter is marked
    `wake: unroutable` in router output and its pending items batch to the digest + surface in the
    blocked-on-human-style fold — never silently dropped. The mixed-fleet gate (§3.4) counts an
    unroutable agent as NOT covered; listener drawdown for it is forbidden.
- **Webhook-swappable by construction:** when Fulcra webhooks ship, the poll leg is replaced by a
  webhook receiver feeding *the same router*; policy and adapters do not change. The interim build
  is therefore not throwaway — this is the pseudo-webhook infrastructure Ash asked to brainstorm,
  built so the real webhooks drop in.

### Part B — Engagement model

- **Presence schema addition:** `engagement: {mode: resident|session|occasional,
  until: <iso8601Z|null>, state: active|lapsed, lapsed_at: <iso8601Z|null>}` (absent field ⇒
  `resident`/`active`; `state`+`lapsed_at` are written only by the engine's engagement sweep —
  see the namespace writer note in the store contract).
  - `resident` — always-on host; expected to beat; staleness is meaningful.
  - `session` — bounded life; declares a TTL (`until`, default `join + 8h` — operator-confirmed
    2026-07-22). **At expiry the agent LAPSES, it does not park** (operator decision, same date):
    a lapsed session drops to a reduced check-in cadence (default every 6h) **indefinitely**,
    retains its roles, and reads **LAPSED — an explained state, never gone-dark. PARK happens
    only when told** — an explicit operator/park directive (or the agent's own park before
    context loss). The host tick (zero model tokens) marks the lapse and aligns future wakes to
    the reduced cadence; it never parks anyone. This is the structural fix for the false-liveness
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

### Part C — Agent external identity (RESOLVED by operator rule, 2026-07-22)

Ash's standing rule decides this leg: **the operator sits outside core engineering spaces and
lobs contributions in** — he will not install an App or place a machine user in `fulcradynamics`
org repos. The identity design follows from that boundary:

- **Inside the operator's boundary (`ashfulcra/*` repos): a DEDICATED fleet machine account,
  to be created (operator decision 2026-07-22: `FulcraBot` is reserved for Fulcra-side repos —
  e.g. the `fulcrabot`-owned website work — and is NOT the identity for the operator's
  fulcra-tools fleet).** Until the new account exists, the interim is the status quo (operator
  credentials inside the operator's own boundary). Once created: fine-grained per-repo PAT,
  rotation cadence, router as token custodian, and attribution conventions so audit reads
  unambiguously as fleet activity — the W9 custody design is unchanged, only the account it
  guards.
- **Upstream (`fulcradynamics` and any org the operator doesn't own): the contributor pattern IS
  the design, permanently.** Agents fork, prepare, and lob the PR over the wall; the merge click
  belongs to an upstream maintainer. The blocked-on-human fold models "awaiting upstream
  maintainer" as a first-class visible state — it is the intended terminal hand-off, not a gap.
- **The GitHub App proposal is a shelf artifact, not a dependency.** A per-repo-scoped,
  short-lived-token App design (contents + pull_requests write only) stays documented here so the
  `fulcradynamics` core team can *choose to install it themselves* if they ever want agent-driven
  merges on their side. Nothing in this build waits on it, and no one on our side will install it.

No decision remains open on this leg.

## 3. Restated CONCUR conditions (from the vacancy-consults-presence concurrence; they bind here)

1. **Presence-stale nudge visible** — when a fold consults presence and finds it stale, it says so
   in output; staleness is never silently swallowed into a default.
2. **Exact identity matching** — presence↔role↔wake matching on exact agent ids only; no
   substring/prefix heuristics (the `role@host` variant lesson).
3. **Dormancy independent** — declared dormancy (`occasional`, lapsed TTL) is a separate axis from
   staleness; a dormant identity must never read as abandoned, and vice versa.
4. **Mixed-fleet gate** — nothing that changes vacancy/escalation semantics ships until every
   harness in the live fleet (claude-code cloud + desktop, codex, OpenClaw, cron hosts) either
   emits the new signal or is explicitly defaulted; partial adoption must not misclassify
   non-upgraded agents.

## 4. Hard constraints

- **Never spawn working sessions:** the router wakes *existing* sessions via their own adapters or
  queues for human-opened ones; it never creates a new working session unilaterally (Ash's rule).
  Any adapter that would need to violate this surfaces to Ash instead.
- **Zero model tokens in the router:** watcher, policy evaluation, TTL lapse-marking, and wake fan-out
  are pure host-side code. Model tokens are spent only by the *woken* agent on real work.
- **Fail-closed secrets:** adapter credentials and the external-identity credential — in this
  build, the dedicated fleet machine-account fine-grained PAT (Part C; interim: operator
  credentials, no new secret) — live in host keychain / environment config,
  never in team paths (durable-state doctrine). (An upstream-adopted App would manage its own
  installation token on the upstream side; that credential never enters this build.)
- **ATC fence:** no changes to usage/headroom/route/atc/dash or `fulcra-agent-atc`.
- **Store remains the bus; the router owns exactly one namespace.** The router *reads* the shards
  agents write, and *writes only* under `team/<team>/_coord/router/` — durable state owned by the
  router SYSTEM, with per-subpath writers (stage-2 normative): `cursor.json`, `config.json`, and
  the folded `delivered.json` view — decision plane only; `queue/` — created by the decision
  plane, claim-stamped by the matching executor; `delivered/` + `dead-letter/` —
  idempotency-keyed records written by the executing claim-holder; `shadow-evidence/` — the W7
  delivery-probe writers, shadow window only, removable after acceptance. No agent-owned shard is
  ever written by any ROUTER component (decision plane or executor); the single, narrow exception
  to agent-owned presence writes belongs to the ENGINE's engagement sweep (W3 — part of
  coord-engine, not the router), whose writer authority covers exactly `engagement.state` and
  `engagement.lapsed_at`, nothing else. No router subpath is written outside its declared writer. Layout:
  `cursor.json` (monotonic cursor / idempotency keys; at-least-once delivery, replays are
  no-ops), `queue/` (deferred and debounced wakes awaiting an idle boundary), `dead-letter/`
  (wakes that exhausted bounded retry, with cause — the audit trail), and `delivered.json`
  (observable last-delivered time per agent). This adopts the relay contract in
  [`EVENT-DRIVEN-WAKE.md`](EVENT-DRIVEN-WAKE.md) (authenticated ends, allowlisted identifiers,
  monotonic cursor, bounded retry + dead-letter, no untrusted command/session fields, fail-visible
  degradation). Restart/failover: state is in the store, not host memory — a replacement router
  process resumes from `cursor.json`; while no router runs, the safety-net listener cadence is the
  backstop. No agent-owned shard is ever written by a router component (the engine sweep's two-field exception above is the only agent-shard writer outside the agent itself).
- **Read-path amendment (Addendum 1, Ash-authorized 2026-07-23).** Directory listings are
  eventually-consistent caches; the store's `data-updates` feed is the authoritative change
  ledger. The router's candidate scan and the engine's hot folds move to feed/event-driven
  sources with the full listing scan retained as the fail-closed fallback, and a typed
  `CoordEvent` record substrate indexes bus writes (shards remain canonical). Normative detail
  and task DAG (E1–E3):
  [`wake-router-ADDENDUM-1-event-substrate.md`](wake-router-ADDENDUM-1-event-substrate.md).

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

1. ~~External identity: App vs machine user~~ — **RESOLVED 2026-07-22 by operator rule** (see
   Part C): a dedicated fleet machine account inside the operator's boundary (FulcraBot is
   reserved for Fulcra-side repos), contributor pattern upstream, App proposal
   shelved for the upstream org to adopt or not.
2. ~~Router host designation~~ — **RESOLVED 2026-07-22 (Ash): cloud-first.** Decision plane in
   the `coord-fable-worker` cloud environment; host-local adapters execute via a thin, policy-free
   host executor whose failure mode is visibly-queued wakes (plan §2.5 / W5.5). No mandatory
   component may require resident hardware.
3. ~~TTL defaults~~ — **RESOLVED 2026-07-22 (Ash): `join + 8h` default; expiry ⇒ LAPSED
   indefinite reduced-cadence check-in (default 6h); park explicit-only** (see Part B).
