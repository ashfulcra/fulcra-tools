# The Coordination Protocol

**A generalizable protocol for human–agent and agent–agent coordination.**

This document specifies what *any* coordination layer must provide for a fleet of
autonomous agents and humans to do durable work together — across different
runtimes, with no shared memory, no direct connectivity, ephemeral and crash-prone
sessions, and an unreliable transport. It is **needs-driven**: it states the
requirements and the failure each one prevents, not the design of any particular
implementation. The systems in this repository (`fulcra-coord`, `fulcra-continuity`,
`fulcra-prefs`) are one concrete realization; this doc is the protocol they imply,
abstracted so others can adopt it.

Requirement language follows RFC 2119: **MUST**, **SHOULD**, **MAY**.

---

## 0. The problem and the thesis

> Many autonomous agents and humans — running in different environments, with no
> shared memory, no direct path to each other, ephemeral and crash-prone
> lifecycles, and an unreliable transport — must coordinate **durable** work so
> that nothing is lost, every outcome is auditable, and no central broker is
> required.

Every requirement below falls out of that sentence. One discipline ties them all
together:

> **Do the work wherever it lives — a forge, a doc, a sandbox, a model call — but
> the *signal* (the verdict, the result, the answer) always returns to the shared
> store.** A result that exists only off the shared store did not happen, as far as
> coordination is concerned.

That single rule is what makes cross-agent, human-in-the-loop coordination
visible, auditable, and loss-proof. The rest of this document is the machinery
that makes it safe under failure.

### Design axioms

1. **Broker-free.** The only required infrastructure is one durable shared store.
2. **Roles over sessions.** Long-lived responsibility is a role; a session is a
   lease on it.
3. **Loops, not messages.** Every cross-boundary ask is a typed unit of work with
   a lifecycle and a guaranteed terminal state.
4. **The bus is the source of truth for coordination state** — never a side
   channel, never an agent's memory.
5. **Loss-proof before fast.** Under an unreliable transport, correctness and
   auditability win over latency.

---

## 1. Substrate

**1.1** A coordination layer MUST require only a **single durable shared store**
that every participant can read and write. It MUST NOT require a message broker,
an always-on central service, direct agent-to-agent connectivity, or shared
filesystem/VPN access. *Prevents: excluding the majority of agents, which run in
sandboxes (cloud, CI, phones) that cannot reach each other.*

**1.2** Writers MUST achieve correctness **without locks**. Because there is no
coordination server to hold a global lock, the store MUST support optimistic
concurrency (detect a conflicting concurrent write by comparing a baseline before
committing) and a **reconciliation pass** that repairs partial or conflicting
writes after the fact. *Prevents: lost updates, and a dependency on a lock the
substrate cannot provide.*

**1.3** Truth SHOULD be an **append-only event log**; reads SHOULD be served from
**materialized views** (pre-built summaries) derived from it. Each event MUST be
written to a distinct, never-overwritten path (e.g. a time-sortable id plus a
random suffix) so two concurrent appends can never collide. *Prevents: every
reader paying full-history cost, partial writes being unrecoverable, and the need
for compare-and-swap.*

**1.4** Where a mutable record and an event log coexist, exactly one MUST be
declared authoritative at any time, and migrations between them MUST be gated (see
§7.8).

---

## 2. Identity, presence, and capability

**2.1 — Roles over sessions.** Long-lived responsibility SHOULD be modeled as a
**role** (the job: `reviewer`, `deployer`, `backlog-groomer`) or capability, not
as a one-off session id. A session **claims a lease** on a role. Stable agent and
human identities may still exist as participants, but routable functions should
not depend on a particular ephemeral holder. *Prevents: responsibility drifting
every time a session dies and respawns, and work routed to a dead session id.*

**2.2 — Leases ride liveness.** A role lease MUST stay valid exactly as long as the
holder's presence is fresh, and MUST lapse automatically when the holder goes
silent — with no separate keep-alive. A vacated role MUST be visibly **vacant** and
re-claimable. *Prevents: a crashed holder appearing to still own a function.*

**2.3 — Identity is per-context and clobber-proof.** Identity MUST be scoped to the
working context (not global to a machine), so two co-located sessions cannot
overwrite each other's identity, and an explicit per-session override MUST take
precedence. Each concurrent session SHOULD have its own isolated working copy.
*Prevents: silent mis-attribution — a sibling session flipping another's identity
so its work posts under the wrong agent.*

**2.4 — Presence is first-class, with grace.** Liveness MUST be an explicit signal
that decays from live → idle → stale on a heartbeat. Routing MUST consult it, and
MUST allow an **absolute grace window** so one missed beat (or a laptop
sleep/wake) does not drop an otherwise-healthy participant. *Prevents: routing
into a void, and dropping a momentarily-quiet agent.*

**2.5 — Capability, not hard-coded identity.** Agents MUST advertise what they can
do; work MUST route to *a live, capable holder*, resolved at routing time — never
to a hard-coded agent id. *Prevents: routing maps naming specific agents that go
stale silently.*

---

## 3. The work model: loops

**3.1 — Everything crossing a boundary is a typed loop.** Every ask that crosses an
agent or session boundary — a review, a dispatch, a question, an idea, an FYI —
MUST be a single record family carrying a `kind`, where each kind selects a state
machine. Adding a work type MUST be registering a kind, not inventing a new record
type. *Prevents: a proliferation of ad-hoc message shapes with no shared
lifecycle.*

**3.2 — A terminal state is always reachable.** Every loop's state machine MUST
guarantee a terminal state is reachable from every state; a loop can never be
stranded. Lifecycles SHOULD be permissive (allow skipping ceremony) where it does
not compromise that guarantee. *Prevents: an ask stuck forever in a state with no
exit.*

**3.3 — The closed-loop guarantee.** A loop that expects a response MUST remain
**open until a response lands on the shared store** — and *nothing else* closes it.
A forge comment, a pushed commit, a chat message, or any out-of-band signal MUST
NOT close a loop. *Prevents: the single most common failure — work done, outcome
invisible, requester polling a platform forever.*

**3.4 — Out-of-band evidence is surfaced, never authoritative.** A system MAY mirror
off-store signals (a merge, a review verdict) into a loop's evidence as a
*hint*, but such evidence MUST be marked out-of-band and MUST NOT close the loop;
the responsible party still closes it explicitly on the store. *Prevents: a
slipped discipline silently passing as a completed handshake.*

**3.5 — Directives carry per-recipient acknowledgement.** A directive addressed to
many recipients MUST be acknowledged **per recipient**; one recipient's ack MUST
NOT clear it for another, and none may lose or double-handle it. *Prevents: a
fan-out being dropped or processed twice.*

**3.6 — Backlog is distinct from active asks.** "Do later" work MUST be capturable
as durable, discoverable, board-visible items that do **not** land in an inbox or
open a response-tracked loop. *Prevents: backlog noise drowning real asks — and an
inbox where most "open" items are spent FYIs.*

**3.7 — SLA, overdue detection, escalation.** A loop that expects a response SHOULD
carry a deadline; an overdue loop MUST be flagged, MAY be rerouted to another
capable holder, and MUST eventually escalate to a human if it cannot be served.
*Prevents: a dropped ask rotting unnoticed.*

---

## 4. The human in the loop

**4.1 — The human is a first-class participant.** The human operator MUST be an
addressable identity that work can be assigned to or blocked on, with a single
consolidated **"what's blocked on me"** view. *Prevents: human-blocking asks
scattering across agents and getting lost.*

**4.2 — Human asks carry scheduling.** An ask on the human MAY carry a
**not-before** (when it becomes actionable) and a **due** time. A not-yet-actionable
ask MUST sit in an "upcoming" view, not the now-plate. *Prevents: the
recurring-maintenance-modeled-as-one-shot-blocker failure — a periodically
renewing item repeatedly surfaced as "blocked now" when nothing is due.*

**4.3 — A paced digest, separate from the event stream.** The human SHOULD receive a
consolidated situational summary on a human cadence (e.g. twice daily), on a
channel distinct from the per-event firehose. *Prevents: both alert-fatigue and
missing the one thing that needed them.*

**4.4 — Only concrete asks reach the human plate.** Broadcasts and FYIs MUST NOT
count as "blocked on the human"; only a concrete, directed ask does. *Prevents: a
noisy plate that the operator learns to ignore.*

---

## 5. Continuity and handoff

**5.1 — Work survives context loss.** Before a context boundary (compaction,
handoff, idle, session exit), an agent SHOULD capture a **structured checkpoint**:
objective, key decisions, artifacts, open questions, and next actions — enough that
a fresh session resumes without guessing. *Prevents: compaction or session death
destroying in-flight understanding.*

**5.2 — Checkpoints travel with the work and are portable.** A resume point MUST be
carried on the coordination primitive that hands off the work, preferably as a
published **ref** that is resolvable on any host. If publishing the ref fails, a
self-contained portable payload MAY ride with the handoff; a bare local path alone
MUST NOT. *Prevents: a handoff referencing state the receiver cannot load.*

**5.3 — Checkpoint at durable boundaries, not every event.** The operational ledger
MUST stay cheap and chatty; checkpoints SHOULD be written only at durable pause
points. *Prevents: checkpoint spam, and the ledger degenerating into the snapshot
store.*

**5.4 — A role has a durable resume point.** A role SHOULD carry a checkpoint ref;
claiming the role MUST surface where it left off, and session-exit SHOULD update it.
*Prevents: a respawned holder starting cold. This is the respawn backbone: spawn →
claim role → resume brief → work → checkpoint on exit.*

**5.5 — Continuity is decoupled.** The checkpoint format MUST be owned by the
continuity layer, and the coordination layer SHOULD store opaque refs rather than
interpreting checkpoint bodies. When a portable inline fallback is needed, the
coordination layer MUST still treat it as opaque payload, so either layer can evolve
independently and the coordination core never hard-depends on the continuity
implementation. *Prevents: tight coupling that makes either system un-evolvable.*

---

## 6. The shared context layer: preferences and facts

**6.1 — One user-owned context layer.** There SHOULD be a single, user-owned store
of the human's **preferences and facts** that every agent on every platform reads,
so all agents share one coherent picture of how the user wants to be served.
*Prevents: per-platform drift in user understanding.*

**6.2 — Typed signals with decay and confidence.** Each preference/fact SHOULD be an
immutable, timestamped, typed signal carrying a key, scope, signed strength, a
**confidence**, and a **half-life**. Current truth is computed by **decaying** each
signal by its age. Conflicts MUST resolve by decayed-weight × confidence, so a
low-confidence guess never overrides a confidently stated fact. *Prevents: stale or
speculative signals silently winning.*

**6.3 — Deterministic compilation — no model in the reduce.** Folding signals into
"current truth" MUST be a **pure, deterministic function** of (signals, time):
identical inputs MUST produce byte-identical output regardless of order. *Prevents:
unreproducible, unauditable "current preferences."*

**6.4 — Safe passive auto-capture.** Agents SHOULD be able to record *inferred*
preferences passively (no explicit "remember"), marked at lower confidence so — by
6.2 — a guess can never override a stated truth. *Prevents: either a capture burden
on the user or a guess corrupting stated truth.*

**6.5 — Per-scope overlay, injected at bootstrap.** Truth MUST be expressible as
**global defaults plus per-platform overrides**, and SHOULD be injected into each
agent at session start so preferences are live from the first turn. *Prevents:
preferences that exist but are not in context when the agent acts.*

**6.6 — Consent-gated disclosure with a logged ledger.** Sharing a scoped slice of
one party's context to another participant MUST be **granted**, filtered to the
granted scope, and **recorded** as a durable disclosure ("who saw what, when"). A
disclosure MUST NOT be emitted unlogged. *Prevents: unconsented or untraceable
disclosure in multi-party work.*

**6.7 — Deterministic group decisions.** Resolving a multi-party choice over
consented slices SHOULD use a **deterministic solver** (weighted scoring, hard
vetoes, lexicographic tie-breaks) that emits an auditable trace — **no model in the
decision**. *Prevents: opaque, unreproducible multi-party outcomes.*

---

## 7. Invariants — the load-bearing guarantees

These are what make §§1–6 trustworthy under an unreliable transport. They are not
optional polish; they are the protocol's spine.

**7.1 — No silent data loss.** Every write MUST be confirmed (verify-after-write),
retried on transient failure, and — on final failure — **cached locally and
self-healed on reconcile**, with a loud, unmissable warning. A write MUST NEVER be
reported as delivered when it was dropped. *Prevents: sender-believed-delivered
losses under backend throttling.*

**7.2 — A failed read is never trusted on a destructive path.** "Could not read"
MUST NOT be treated as "absent." A write that cannot confirm the prior state MUST
**defer** rather than overwrite. *Prevents: a blind write reverting another agent's
transition or deleting a record the reader simply failed to fetch.*

**7.3 — Partial updates merge, never replace.** An update that passes a subset of
fields MUST overlay only those fields and preserve the rest. *Prevents: a routine
status change silently nulling a record's body, owner, or other unpassed fields.*

**7.4 — Degraded, but never blind.** When a materialized view is stale or
unreachable, reads MUST fall back to the authoritative records with a visible
warning — never silently serve stale truth. *Prevents: an inbox looking empty or a
live reviewer looking dead under throttling.*

**7.5 — Self-healing convergence.** A periodic reconcile pass MUST repair partial
writes, re-arm dead listeners, prune stale state, and reroute work off dark
holders, so transient failures cannot become permanent. *Prevents: a one-time
glitch leaving the fleet permanently inconsistent.*

**7.6 — Graceful degradation of optional parts.** Optional components (continuity,
push notifications, timeline annotations) MUST degrade to a silent no-op when
absent, never breaking a core path. *Prevents: an optional dependency taking down
coordination.*

**7.7 — Observability is mandatory.** The coordination layer MUST expose its own
health — per-host reconcile freshness, open/overdue loop counts, substrate parity —
so a silently failing heartbeat or reconcile reads as **degraded**, not as silence.
*Prevents: the coordination layer failing invisibly.*

**7.8 — Migrations are versioned, gated, and reversible.** Evolving the substrate
MUST be done additively (dual-write the new form), validated against a **parity
gate**, opted into per host, and flipped fleet-wide only on proven parity — and
reversible. *Prevents: a substrate change breaking a mixed-version fleet.*

**7.9 — Bounded growth.** Event shards, checkpoints, presence records, dedup
markers, and terminal work MUST be pruned on a cadence (keeping recent history hot,
cold-archiving the rest). *Prevents: the store growing until reads time out.*

---

## 8. Governance

**8.1 — Policy and routing are data, not code.** Reviewer pools, role registries,
and routing preferences MUST live as **store-readable configuration** that both
senders and routers consult — never hard-coded in source or an agent's head.
Announcing a routing change as a one-off broadcast is insufficient; the change MUST
update the shared data both sides read. *Prevents: broadcast-and-hope routing going
stale silently in every author's mental model.*

**8.2 — Separation of duties.** For changes that need review, an **independent
reviewer** reviews adversarially and may commit fixes; the **author** owns the final
acceptance/merge; the reviewer MUST NOT self-merge. If no reviewer is available
within the SLA, the request escalates to a human; work is never accepted unreviewed.
*Prevents: unreviewed self-merges and ambiguous ownership.*

---

## 9. Conformance — the minimum viable protocol

A minimally conformant coordination layer provides:

1. **A durable shared store** with optimistic-concurrency writes and a reconcile
   repair pass (§1).
2. **Roles + leases + presence + capability routing** (§2).
3. **Typed loops that close only on the store**, with per-recipient acks, a backlog
   lane, and SLA escalation (§3).
4. **The human as an addressable, scheduled participant** with a blocked-on-me view
   and a paced digest (§4).
5. **Portable structured checkpoints** carried as refs on handoffs and roles (§5).
6. **A deterministic, decaying, consent-gated shared preference layer** injected at
   bootstrap (§6) — *optional for basic coordination, required for multi-party work.*
7. **The invariants of §7** — without them the rest is not trustworthy under real
   transport failures.
8. **Policy-as-data governance and separation of duties** (§8).

Items 1–5, 7, and 8 are the core. Item 6 is required once agents must act on a
shared understanding of a person, or make decisions on behalf of a group.

---

*This protocol is needs-driven and implementation-agnostic. It is realized in this
repository by `fulcra-coord` (substrate, identity, loops, human-in-loop, governance,
invariants), `fulcra-continuity` (§5), and `fulcra-prefs` (§6) — but nothing here is
specific to those packages or to Fulcra. The derivation of these requirements from
the implementations is recorded in [`docs/analysis/`](docs/analysis/).*
