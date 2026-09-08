# Wake Router + Engagement Model — build spec (stage 1: consolidated brainstorm → spec)

> **Historical implementation specification.** The router and engagement model
> are implemented; deployment acceptance must be evaluated for each team.
> Current command behavior is documented by `coord-engine --help`.

**Review gate:** independent reviewer plus component owner at each stage.
**Scope:** ATC is unchanged by this design.

## 1. Problem

A fleet needs a shared wake policy:

- **Idle polling:** independent listeners can repeat expensive folds with no work.
- **Unreachable agents:** occasional sessions need explicit delivery or visible deferral.
- **Misleading liveness:** a host heartbeat may outlive its session, while an active
  session may work between beats. Engagement and observed activity must inform liveness.

## 2. Shape of the fix

One **model-free fleet watcher** (the router) replaces N always-on listeners as the *primary* wake
path, and presence gains an **engagement declaration** so vacancy/escalation logic can distinguish
"parked by design" from "gone dark."

### Part A — Wake Router

- **One watcher, whole fleet:** a single model-free process — **the decision plane
  runs in a configured cloud environment, with no mandatory resident hardware** — polls the store at a fixed
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
    closed), OpenClaw authenticated `POST /hooks/wake`, Claude Managed Agents `user.message` to an
    idle persisted session, macOS notification to the human for consent-gated harnesses. Adapters
    are host-local on the executor (`$COORD_WAKE_ADAPTER_DIR/<adapter>.sh`); the bundled
    `wake/codex.sh` / `wake/openclaw.sh` repo copies were removed with the listener stack.
  - *Proposed in this build (new code, stage-2 tasks):* a **queued wake file** consumed by the
    SessionStart briefing hook on next human open (local/desktop fallback lane), and a **platform
    scheduled-routine leg** for Claude Code web/cloud — which has **no documented exact-session
    inbound wake**; the router must NOT substitute a different Managed Agents session. For cloud
    agents the router only aligns work to the agent's own self-armed Routine cadence (as
    coordinator and cloud-worker run today) or queues for it; it never creates sessions.
  - *Fail-visible unroutable lane:* an agent whose harness offers no supported adapter is marked
    `wake: unroutable` in router output and its pending items batch to the digest + surface in the
    blocked-on-human-style fold — never silently dropped. The mixed-fleet gate (§3.4) counts an
    unroutable agent as NOT covered; listener drawdown for it is forbidden.
- **Webhook-swappable by construction:** when Fulcra webhooks ship, the poll leg is replaced by a
  webhook receiver feeding *the same router*; policy and adapters do not change. The interim build
  is therefore not throwaway — this is the pseudo-webhook infrastructure the operator asked to brainstorm,
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
    incident and the direct answer to maintainer's DECIDE-WITH-ME liveness-substrate
    question: liveness = engagement-aware fold over (beats + activity), not beats alone.
  - `occasional` — **occasional model, resident host:** a desktop agent holds a role with *no loop
    at all*; the router's host-side legs cover it (resume-with-app-closed, human notification,
    queued wake file, digest batching), and **deputy coverage** lets a resident role execute small
    items for a dormant identity instead of waking it (existing vacancy-claim machinery).
- **Vacancy/escalation reads engagement.** Escalation fires on *unexplained* absence only. This is
  the condition for re-arming the fleet's disarmed escalate sweeps.
- **Activity-implies-liveness is consumed here** (maintainer's routed P1, both constraints
  honored): every engine bus write refreshes the actor's presence beat, throttled to once per beat
  interval per process (burst of writes = one beat write), and a refresh failure never makes a
  succeeding write fail. The engagement fold treats recent *activity* as liveness proof — a busy
  agent needs no separate beat.

### Part C — Agent external identity

Use a dedicated, scoped machine account or GitHub App for authorized project
writes. Keep credentials in a secret store and document rotation and attribution.
For repositories outside the team's control, agents contribute through forks and
pull requests; upstream maintainers retain merge authority. Represent that handoff
as a visible waiting state. No integration assumes permission to install an App
or create an account in another organization.

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
  queues for human-opened ones; it never creates a new working session unilaterally (the operator's rule).
  Any adapter that would need to violate this surfaces to the operator instead.
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
- **Read-path amendment (Addendum 1, operator-approved 2026-07-23).** Directory listings are
  eventually-consistent caches; the store's `data-updates` feed is the authoritative change
  ledger. Reconcile, the engine's hot folds, and the router's candidate scan move to
  feed-driven delta sources with the full listing scan retained as the fail-closed fallback
  (shards remain canonical; a second coordination-owned ledger was considered and cut by
  operator direction). Normative detail and task DAG (E1–E3):
  [`wake-router-ADDENDUM-1-event-substrate.md`](wake-router-ADDENDUM-1-event-substrate.md).

## 5. Deliverables & stage plan

| Stage | Artifact | Gate |
|---|---|---|
| 1 (this doc) | Consolidated spec | dual-green: reviewer + coordinator |
| 2 | Implementation plan: task DAG, schema diffs (presence `engagement`, router config file format, wake-queue shard shape), rollout order honoring the mixed-fleet gate, test plan (red-first for every fold change) | dual-green |
| 3+ | SDD execution — tasks assigned explicitly on the bus to maintainer / cloud-worker; per-task review by reviewer; engine changes land behind the mixed-fleet gate | dual-green per task |

Rollout sketch (detail belongs to stage 2): schema + folds first (engagement read/write, inert),
then router read-only shadow mode (logs what it *would* wake — measured against live listener
behavior), then adapter fan-out enabled per-agent, then listener cadence drawdown, then escalate
re-arm. Each step reversible; shadow-mode divergence is the acceptance evidence for going live.

## 6. Non-goals

Cross-account federation (BUS-78 — needs folder sharing), replacing the coordination store or
listeners entirely (safety-net cadence stays), tracker-bridge changes, any ATC coupling, webhook
receiver implementation before Fulcra ships webhooks (we build the socket it plugs into, not the
plug).

## 7. Deployment choices

1. Use a dedicated machine account with scoped credentials for authorized forge writes.
2. Run the decision plane in a cloud environment; host-local adapters execute
   through a thin executor. Executor failure leaves wakes visibly queued.
3. Session TTL defaults to `join + 8h`. Expiry means LAPSED with indefinite
   reduced-cadence check-in (default 6h); parking remains explicit.
