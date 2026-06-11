# Coordination Protocol — Needs Analysis (ideas-inbox pass)

> Independent reverse-engineering pass by `claude-code:Mac:ideas-inbox`.
> Method: treat `fulcra-coord`, `fulcra-continuity`, and `fulcra-prefs` as *evidence*
> of what problems a coordination layer must solve, then state the **needs** a
> generalizable human-agent / agent-agent coordination protocol must satisfy —
> abstracted from the Fulcra implementation. Each need names the **failure it prevents**.

## The problem statement

> Many autonomous agents and humans — running in different environments, with no
> shared memory, no direct network path to each other, ephemeral and crash-prone
> lifecycles, and an unreliable transport — must coordinate **durable** work so
> that nothing is lost, every outcome is auditable, and no central broker is
> required.

Everything below falls out of that sentence. The protocol is the set of
guarantees that make it true.

---

## A. Substrate & transport

- **A1 — One shared durable store is the only required infrastructure.** No
  broker, no message queue, no direct agent-to-agent connectivity, no shared
  filesystem/VPN. *Prevents:* excluding the majority of agents, which live in
  sandboxes (cloud, CI, phones) that can't reach each other.
- **A2 — Correctness without locks: optimistic concurrency + reconciliation.**
  Concurrent writers with no coordination server need conflict detection (compare
  baseline before write) and a repair pass, not mutexes. *Prevents:* lost updates
  and the impossibility of a global lock across disconnected hosts.
- **A3 — Append-only event log as truth; materialized views for cheap reads.**
  Writers append immutable, distinctly-named events; readers consume pre-built
  summaries. *Prevents:* every reader paying full-history cost, and partial writes
  being unrecoverable.
- **A4 — Distinct write paths per event (no two writers collide).** Time-sortable
  + random event ids so concurrent appends never overwrite. *Prevents:* the need
  for compare-and-swap the substrate can't offer.

## B. Identity, presence & liveness

- **B1 — Durable identity is decoupled from ephemeral sessions.** A *role* (the
  job) persists; a *session* is a lease on it. *Prevents:* identity drifting every
  time a session dies/respawns, and routing to a dead session id.
- **B2 — Identity is per-context and clobber-proof.** Two co-located sessions
  sharing a working context must not overwrite each other's identity; an explicit
  per-session override must win. *Prevents:* silent mis-attribution of work
  (observed live: a sibling session flipped this session's identity and every
  task/directive posted under the wrong agent).
- **B3 — Liveness is a first-class signal with grace.** Presence heartbeats decay
  to "stale"; routing consults liveness; an absolute grace window tolerates one
  missed beat / a laptop sleep. *Prevents:* dropping a momentarily-quiet reviewer,
  and routing work into a void.
- **B4 — Capability advertisement, not hard-coded identity.** Agents declare what
  they can do (`review`, `deploy`, …); work routes to *a capable live holder*.
  *Prevents:* routing maps that name specific agents and go stale silently.

## C. Work & message model

- **C1 — Every cross-boundary ask is a typed loop with a lifecycle and a
  guaranteed-reachable terminal state.** Review, dispatch, question, idea — one
  record family, a per-kind state machine. *Prevents:* a request being stranded
  in a state with no exit.
- **C2 — The closed-loop guarantee: the signal returns *on the bus*, never
  out-of-band.** A verdict that exists only as a PR comment, a chat message, or a
  pushed commit **did not happen** as far as coordination is concerned; only an
  on-bus response closes a loop. *Prevents:* the single most common failure —
  work done, outcome invisible, requester polling forever.
- **C3 — Directives with per-recipient acknowledgement.** A fan-out to many is
  acked once per recipient; one agent's ack never clears it for another.
  *Prevents:* a broadcast being lost or double-handled.
- **C4 — A backlog/idea pipeline distinct from active asks.** "Do later" is
  durable and discoverable without landing in an inbox or opening an SLA loop.
  *Prevents:* backlog noise drowning real asks (observed live: ~50 of 60 "open"
  items were spent broadcasts/acks).
- **C5 — SLA, overdue detection, and escalation.** Loops that expect a response
  carry a deadline; a lapsed one is flagged and rerouted, then escalated to a
  human. *Prevents:* a dropped ask rotting silently.

## D. Human-in-the-loop

- **D1 — The human is an addressable participant with a "what's blocked on ME"
  plate.** Asks to the human are explicit, visible, and de-duplicated. *Prevents:*
  human-blocking asks scattering across agents and getting lost.
- **D2 — Human asks carry scheduling so they surface only when actionable.** A
  not-yet-due ask sits in "upcoming," not the now-plate. *Prevents:* the
  recurring-maintenance-modeled-as-one-shot-blocker failure (observed live: a
  periodically-renewing token was repeatedly re-flagged as "blocked now").
- **D3 — A paced operator digest, separate from the event firehose.** Humans get
  a consolidated situational summary on a human cadence. *Prevents:* either
  alert-fatigue or missing the one thing that needed them.

## E. Continuity & handoff

- **E1 — Work survives context loss via structured checkpoints.** Objective,
  decisions, artifacts, open questions, next actions — enough that the next
  session resumes without guessing. *Prevents:* compaction / session death
  destroying in-flight understanding.
- **E2 — Checkpoints travel with the work and are portable.** A resume point is a
  *ref* carried on the coordination primitive, published so it's valid on any
  host (never a bare local path). *Prevents:* a handoff that references state the
  receiver can't load.
- **E3 — Checkpoint at durable boundaries, not every event.** Before
  compaction/handoff/idle/exit — keep the operational ledger cheap and chatty.
  *Prevents:* checkpoint spam, and the ledger becoming the snapshot store.
- **E4 — A role has a durable resume point.** Claiming a role prints where it
  left off; session-exit checkpoints it. *Prevents:* respawned sessions starting
  cold.

## F. Shared preference & context layer

- **F1 — One user-owned context layer (preferences + facts) every agent reads.**
  Cross-platform, so every agent shares one coherent picture. *Prevents:*
  per-platform drift in how the user wants to be served.
- **F2 — Typed signals with decay and confidence.** Preferences age (half-life);
  conflicts resolve by decayed-weight × confidence so a guess never overrides a
  stated fact. *Prevents:* stale or low-confidence inferences silently winning.
- **F3 — Deterministic compilation — no model in the reduce.** Same signals →
  byte-identical compiled truth, regardless of order. *Prevents:* unreproducible,
  unauditable "current preferences."
- **F4 — Passive auto-capture, safely.** Agents record inferred preferences at
  lower confidence without an explicit "remember." *Prevents:* either a capture
  burden on the user or a guess corrupting stated truth.
- **F5 — Per-platform scope, injected at session bootstrap.** Global + platform
  overrides, live from turn one. *Prevents:* preferences that exist but aren't in
  context when the agent acts.
- **F6 — Consent-gated disclosure with a logged ledger.** Sharing a scoped slice
  to another participant is granted, filtered, and recorded. *Prevents:*
  unconsented or untraceable disclosure in multi-party work.
- **F7 — Deterministic group-decision solving over consented slices.** Weighted
  scoring + hard veto + an auditable trace, no LLM in the loop. *Prevents:*
  opaque, unreproducible multi-party outcomes.

## G. Failure modes & invariants (cross-cutting — the load-bearing part)

- **G1 — No silent data loss.** Verify-after-write; retry on throttle; on final
  failure cache locally and self-heal on reconcile — never a success-shaped drop.
  *Prevents:* sender-believed-delivered losses (observed live: writes silently
  dropped under backend throttling).
- **G2 — A failed read is never trusted on a destructive path.** "Couldn't read"
  ≠ "absent"; a write that can't confirm the prior state defers rather than
  clobbering. *Prevents:* blind writes reverting another agent's transition or
  blanking fields (observed live: a partial `update` nulled a task's whole body).
- **G3 — Partial updates merge, never replace.** Overlay only the passed fields;
  preserve the rest. *Prevents:* a routine status change destroying summary/owner.
- **G4 — Degraded but never blind.** A stale materialized view falls back to the
  durable records with a warning, never silently serves stale truth. *Prevents:*
  an inbox looking empty / a live reviewer looking dead under throttling.
- **G5 — Self-healing convergence.** A reconcile pass repairs partial writes,
  re-arms dead listeners, prunes stale state, reroutes dark reviewers. *Prevents:*
  transient failures becoming permanent.
- **G6 — Graceful degradation of optional parts.** Continuity, push
  notifications, annotations, etc. are optional; missing → silent no-op, never a
  broken core path. *Prevents:* an optional dependency taking down coordination.
- **G7 — A health/observability surface.** Per-host reconcile freshness, loop
  health, substrate parity are reported; a silently-failing heartbeat reads as
  degraded. *Prevents:* the coordination layer failing invisibly.
- **G8 — Versioned, gated, reversible migrations.** Evolve the substrate by
  additive dual-write + a parity gate + per-host opt-in; flip only on proven
  parity. *Prevents:* a substrate change breaking a mixed-version fleet.
- **G9 — Bounded growth / retention.** Event shards, checkpoints, presence,
  markers, and terminal tasks are pruned on a cadence. *Prevents:* the bus
  growing until reads time out (observed live: a large index began 504-ing).

## H. Governance

- **H1 — Routing and policy are data, not hard-coded.** Reviewer seeds, role
  registries, conventions live as bus-readable config both senders and routers
  read. *Prevents:* broadcast-and-hope routing going stale silently in every
  author's head/script.
- **H2 — Separation of duties on review.** Independent reviewer reviews
  adversarially and commits fixes; the author owns the final merge; the reviewer
  never merges. *Prevents:* unreviewed self-merges and ambiguous ownership.

---

## The shape of the protocol (synthesis)

A generalizable coordination protocol is **seven guarantees over one shared
append-only store**:

1. **Durable, broker-free substrate** (A) — a shared log anyone can reach, correct
   without locks.
2. **Roles over sessions** (B) — durable identity + liveness + capability, so work
   routes to a *live capable holder*, never a dead id.
3. **Loops that always close on the bus** (C) — every cross-boundary ask is typed,
   lifecycle-bounded, and closed only by an on-bus response.
4. **The human as a first-class, scheduled participant** (D) — addressable,
   blocked-on-me visible, paced.
5. **Continuity as portable resume state** (E) — work survives context loss and
   travels with the handoff.
6. **A deterministic shared context layer** (F) — user preferences/facts compiled
   reproducibly, consented, injected everywhere.
7. **Loss-proof, self-healing, observable operation** (G) + **policy-as-data
   governance** (H) — the invariants that make the other six trustworthy under an
   unreliable transport.

The thesis tying them together: **do the work wherever it lives, but the *signal*
always returns to the shared store** — that single discipline is what makes
cross-agent, human-in-the-loop coordination visible, auditable, and loss-proof.
