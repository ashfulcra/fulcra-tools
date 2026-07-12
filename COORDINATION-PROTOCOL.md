# Agents + Humans - Coordination Protocol

**A generalizable protocol for human–agent and agent–agent coordination.**

> **Current realization:** the protocol below is implementation-agnostic; the
> **current** realization in this repo is **coord** —
> [`packages/coord-engine`](packages/coord-engine/README.md) (the `coord-engine`
> CLI) + the [`skills/fulcra-agent-*`](skills) skills. Where the text names
> `fulcra-coord`, that is the **first-generation, now-deprecated** realization
> (see [`packages/fulcra-coord/DEPRECATED.md`](packages/fulcra-coord/DEPRECATED.md));
> the requirements it derives are unchanged, but new work targets coord-engine.

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

### 0.1 Design axioms

1. **Broker-free.** The only required infrastructure is one durable shared store.
2. **Roles over sessions.** Long-lived responsibility is a role; a session is a
   lease on it.
3. **Loops, not messages.** Every cross-boundary ask is a typed unit of work with
   a lifecycle and a guaranteed terminal state.
4. **The bus is the source of truth for coordination state** — never a side
   channel, never an agent's memory.
5. **Loss-proof before fast.** Under an unreliable transport, correctness and
   auditability win over latency.

### 0.2 Trust model and scope (read this before §6)

This protocol defaults to a **single trust domain**: one principal (e.g. one
person's account) owns the store, and every agent holding credentials to it is
trusted to read and write all of it. Within that domain, the protocol provides
*coordination*, not *authorization* — it has no primitive that stops one trusted
agent from reading or overwriting another's records, and it does not try to.

**Multi-party coordination across trust boundaries** (the consent and group-decision
needs in §6.6–§6.7) is therefore **layered on top**, not provided by the substrate:
each party MUST own its own store/credentials, and cross-party exchange happens
through the explicit disclosure boundary of §6.6. Consent in this protocol is an
enforced *filter-and-log at the disclosure point* plus an auditable record — **not**
storage-layer access control. A reader evaluating "is consent enforced?" MUST read
it as: *enforced where data is party-owned and only crosses via the disclosure API;
out of scope where parties already share one store.* Conflating the two is the most
common misreading of §6.

### 0.3 Core trade-offs (made explicit)

Every axiom buys reachability and durability at a cost. Stated plainly so adopters
choose with eyes open:

| Choice | Buys | Costs |
|---|---|---|
| Broker-free shared store (§1.1) | universal reachability, no infra | no server-side enforcement, no push semantics |
| Lock-free optimistic concurrency (§1.2) | progress without a coordinator | conflicts resolved after the fact, not prevented |
| Materialized views (§1.3) | cheap, bounded reads | views lag truth; staleness must be handled (§7.4) |
| Best-effort dual-write / async repair (§7) | availability under a flaky transport | eventual (not immediate) consistency |
| Bus-as-truth discipline (axiom 4) | visible, auditable, loss-proof coordination | a real "return the signal" tax on every actor |

---

## 1. Substrate

**1.1** A coordination layer MUST require only a **single durable shared store**
that every participant can read and write. It MUST NOT require a message broker,
an always-on central service, direct agent-to-agent connectivity, or shared
filesystem/VPN access. *Prevents: excluding the majority of agents, which run in
sandboxes (cloud, CI, phones) that cannot reach each other.*

**1.2** Writers MUST achieve correctness **without locks**. With no coordination
server to hold a global lock, the store MUST support optimistic concurrency (detect
a conflicting concurrent write by comparing a baseline before committing) and a
**reconciliation pass** that repairs partial or conflicting writes after the fact.
*Prevents: lost updates, and a dependency on a lock the substrate cannot provide.*

**1.3 — Authoritative record + auditable history + cheap reads.** The store MUST
keep, for each unit of work, an **authoritative current record**; it SHOULD also
keep an **append-only history** sufficient to audit and to recover a record after a
partial write; and reads SHOULD be served from **materialized views** so no reader
pays full-history cost. Exactly one form (the mutable record *or* a complete fold of
the history) MUST be declared authoritative at any time, and which one MUST be
unambiguous to every reader (see §7.8 for changing it). *One realization:* a mutable
`record` file as the authority plus an append-only event log as the history and a
future read-substrate. *Prevents: every reader paying full-history cost,
partial writes being unrecoverable, and ambiguity about what is true.*

**1.4 — Collision-free appends and deterministic ordering.** Each history entry MUST
be written to a distinct, never-overwritten path (e.g. a time-sortable id plus a
random suffix) so two concurrent appends never collide. Folding history into a
snapshot MUST be **deterministic and order-independent**: it MUST canonicalize
timestamps for ordering (raw string compares invert when precision differs), MUST
**dedup retries by a stable identity** (see §7.10), and MUST tolerate the bounded
clock skew of independent hosts rather than assuming a global clock. *Prevents:
colliding writes, and non-deterministic or skew-corrupted history folds.*

```
        write path                         read path
  ┌──────────────────────┐          ┌──────────────────────┐
  │ stat baseline (§1.2)  │         │ view fresh? ──yes──► serve view (§1.3) │
  │ apply + append (§1.4) │         │   │no                                  │
  │ upload record         │         │   └─► fall back to records + WARN(§7.4)│
  │ rebuild+upload views  │         └──────────────────────┘
  │ verify (§7.1); on fail:                 reconcile (periodic, §7.5)
  │   cache local + mark needs-reconcile ─────► repair · re-assert views ·
  └──────────────────────┘                     prune (§7.9) · reroute (§3.7)
```

---

## 2. Identity, presence, and capability

**2.1 — Roles over sessions.** Long-lived responsibility SHOULD be modeled as a
**role** (the job: `reviewer`, `deployer`, `backlog-groomer`) or capability, not as
a one-off session id. A session **claims a lease** on a role. Stable agent and human
identities may still exist as participants, but routable functions should not depend
on a particular ephemeral holder. *Prevents: responsibility drifting every time a
session dies and respawns, and work routed to a dead session id.*

**2.2 — Leases ride liveness.** A role lease MUST stay valid exactly as long as the
holder's presence is fresh, and MUST lapse automatically when the holder goes
silent — with no separate keep-alive. A vacated role MUST be visibly **vacant** and
re-claimable. *Prevents: a crashed holder appearing to still own a function.*

**2.3 — Identity survives co-location.** Concurrent sessions MUST NOT be able to
clobber each other's identity. Note the sharp edge: scoping identity to a *working
directory* prevents clobber only when the directories are genuinely **distinct** —
two sessions sharing one working context still overwrite each other (observed
repeatedly: two sessions in the same checkout flipped each other's identity, so work
posted under the wrong agent). The enforceable requirement is therefore: each
concurrent session MUST have an **isolated working context** (e.g. its own worktree),
**and** an explicit **session-scoped identity override** MUST take precedence over
any persisted/derived identity. *Prevents: silent mis-attribution of work.*

**2.4 — Presence is first-class, with grace.** Liveness MUST be an explicit signal
that decays from live → idle → stale on a heartbeat. Routing MUST consult it, and
MUST allow an **absolute grace window** so one missed beat (or a laptop sleep/wake)
does not drop an otherwise-healthy participant. *Prevents: routing into a void, and
dropping a momentarily-quiet agent.*

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
type. *Prevents: a proliferation of ad-hoc message shapes with no shared lifecycle.*

**3.2 — A terminal state is always reachable.** Every loop's state machine MUST
guarantee a terminal state is reachable from every state; a loop can never be
stranded. Lifecycles SHOULD be permissive where it does not compromise that
guarantee. *Prevents: an ask stuck forever in a state with no exit.*

**3.3 — The closed-loop guarantee.** A loop that expects a response MUST remain
**open until a response lands on the shared store** — and *nothing else* closes it.
A forge comment, a pushed commit, a chat message, or any out-of-band signal MUST
NOT close a loop. *Prevents: the single most common failure — work done, outcome
invisible, requester polling a platform forever.*

**3.4 — Out-of-band evidence is surfaced, never authoritative.** A system MAY mirror
off-store signals (a merge, a review verdict) into a loop's evidence as a *hint*,
but such evidence MUST be marked out-of-band and MUST NOT close the loop; the
responsible party still closes it explicitly on the store. *Prevents: a slipped
discipline silently passing as a completed handshake.*

**3.5 — Directives carry per-recipient acknowledgement.** A directive addressed to
many recipients MUST be acknowledged **per recipient**; one recipient's ack MUST
NOT clear it for another, and none may lose or double-handle it. *Prevents: a
fan-out being dropped or processed twice.*

**3.6 — Backlog is distinct from active asks.** "Do later" work MUST be capturable
as durable, discoverable, board-visible items that do **not** land in an inbox or
open a response-tracked loop. *Prevents: backlog noise drowning real asks.*

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
recurring-maintenance-modeled-as-one-shot-blocker failure — a periodically renewing
item repeatedly surfaced as "blocked now" when nothing is due.*

**4.3 — A paced digest, separate from the event stream.** The human SHOULD receive a
consolidated situational summary on a human cadence, on a channel distinct from the
per-event firehose. *Prevents: both alert-fatigue and missing the one thing that
needed them.*

**4.4 — Only concrete asks reach the human plate.** Broadcasts and FYIs MUST NOT
count as "blocked on the human"; only a concrete, directed ask does. *Prevents: a
noisy plate the operator learns to ignore.*

---

## 5. Continuity and handoff

**5.1 — Work survives context loss.** Before a context boundary (compaction,
handoff, idle, session exit), an agent SHOULD capture a **structured checkpoint**:
objective, key decisions, artifacts, open questions, and next actions. *Prevents:
compaction or session death destroying in-flight understanding.*

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
*Prevents: a respawned holder starting cold.*

**5.5 — Continuity is decoupled.** The checkpoint format MUST be owned by the
continuity layer, and the coordination layer SHOULD store opaque refs rather than
interpreting checkpoint bodies. When a portable inline fallback is needed, the
coordination layer MUST still treat it as opaque payload, so either layer can evolve
independently and the core never hard-depends on the continuity implementation.
*Prevents: tight coupling that makes either system un-evolvable.*

---

## 6. The shared context layer: preferences and facts

> **Two layers with different trust models (per §0.2).** §6.1–§6.5 are *single-user
> context sync* — one principal's preferences, shared across that principal's own
> agents. §6.6–§6.7 are *multi-party* and assume the §0.2 disclosure boundary.

### 6a. Single-user context sync

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
§6.2 — a guess can never override a stated truth. *Prevents: either a capture burden
on the user or a guess corrupting stated truth.*

**6.5 — Per-scope overlay, injected at bootstrap.** Truth MUST be expressible as
**global defaults plus per-platform overrides**, and SHOULD be injected into each
agent at session start so preferences are live from the first turn. *Prevents:
preferences that exist but are not in context when the agent acts.*

### 6b. Multi-party decisions (assumes §0.2)

**6.6 — Consent-gated disclosure with a logged ledger.** Sharing a scoped slice of
one party's context to another participant MUST be **granted**, filtered to the
granted scope at the disclosure point, and **recorded** as a durable disclosure
("who saw what, when"). A disclosure MUST NOT be emitted unlogged. Per §0.2 this is
enforced *at the disclosure boundary between party-owned stores*, not by storage
access control within a shared domain. *Prevents: unconsented or untraceable
disclosure in multi-party work.*

**6.7 — Deterministic group decisions.** Resolving a multi-party choice over
consented slices SHOULD use a **deterministic solver** (weighted scoring, hard
vetoes, lexicographic tie-breaks) that emits an auditable trace — **no model in the
decision**. *Prevents: opaque, unreproducible multi-party outcomes.*

---

## 7. Invariants — the load-bearing guarantees

These make §§1–6 trustworthy under an unreliable transport. They are the protocol's
spine, not optional polish. Several were violated by real implementations before
they were understood — each states the **anti-pattern** it forbids, so compliance is
checkable rather than aspirational.

**7.1 — No *silent* data loss (at-least-once + loud failure).** The guarantee is not
"delivery cannot fail" — it is that failure cannot be **silent or unrecoverable**.
Every write MUST be confirmed (verify-after-write), retried on transient failure
(safely, per §7.10), and — on final failure — **cached locally, self-healed on
reconcile, and announced with an unmissable warning**. A write MUST NEVER be
reported as delivered when it was not. *Prevents: sender-believed-delivered losses
under backend throttling.*

**7.2 — A failed read is never "absent" on a destructive path.** A read that errors
or times out MUST be distinguished from a read that returns *empty*. A write whose
correctness depends on prior state (delete, overwrite, "create if absent") MUST
**defer** when it cannot confirm that state, never proceed as if the record were
absent. *Prevents: a blind write reverting another agent's transition or deleting a
record the reader merely failed to fetch.*

**7.3 — Partial updates merge, never replace.** An update passing a subset of fields
MUST overlay only those fields and preserve the rest; it MUST NOT default unpassed
fields to empty. *Prevents: a routine status change silently nulling a record's
body, owner, or other unpassed fields.*

**7.4 — Degraded, but never blind.** When a materialized view is stale or
unreachable, reads MUST fall back to the authoritative records with a visible
warning — never silently serve stale truth. *Prevents: an inbox looking empty or a
live reviewer looking dead under throttling.*

**7.5 — Incremental, self-healing convergence.** A periodic reconcile pass MUST
repair partial writes, re-arm dead listeners, prune stale state, and reroute work
off dark holders. Because the store can be large and the transport throttled,
reconcile MUST be **incremental and bounded per pass with guaranteed forward
progress** — convergence is eventual across several ticks, never contingent on one
all-or-nothing pass completing. *Prevents: a one-time glitch becoming permanent,
and a reconcile that can't finish at scale leaving the fleet stuck.*

**7.6 — Graceful degradation of optional parts.** Optional components (continuity,
push notifications, timeline annotations) MUST degrade to a silent no-op when
absent, never breaking a core path. *Prevents: an optional dependency taking down
coordination.*

**7.7 — Observability is mandatory.** The coordination layer MUST expose its own
health — per-host reconcile freshness, open/overdue loop counts, substrate parity —
so a silently failing heartbeat or reconcile reads as **degraded**, not as silence.
*Prevents: the coordination layer failing invisibly.*

**7.8 — Migrations are versioned, gated, and reversible.** Evolving the substrate
(including changing which form is authoritative per §1.3) MUST be additive
(dual-write the new form), validated against a **parity gate**, opted into per host,
flipped fleet-wide only on proven parity, and reversible. *Prevents: a substrate
change breaking a mixed-version fleet.*

**7.9 — Bounded growth.** Event/history shards, checkpoints, presence records, dedup
markers, and terminal work MUST be pruned on a cadence (recent history hot, the rest
cold-archived). *Prevents: the store growing until reads time out.*

**7.10 — Idempotent writes and responses (what makes retries safe).** Every write
and every loop response MUST carry a **stable identity** (an actor + idempotency
key, or a content-addressed id) so that a retried or duplicated delivery folds to a
**single effect**. Without this, the retries mandated by §7.1 double-apply.
*Prevents: at-least-once delivery silently becoming at-least-twice.*

**7.11 — Cost bounded by fleet and history size.** Per-write work SHOULD NOT grow
unboundedly with the number of participants, and read cost SHOULD stay bounded as
task/history count grows; where a fan-out is unavoidable it MUST be incremental and
deadline-bounded. *Prevents: the failure we observed — per-write view fan-out
scaling with fleet size until a large store began timing out reads.*

---

## 8. Governance

**8.1 — Policy and routing are data, not code.** Reviewer pools, role registries,
and routing preferences MUST live as **store-readable configuration** both senders
and routers consult — never hard-coded in source or an agent's head. Announcing a
routing change as a one-off broadcast is insufficient; the change MUST update the
shared data both sides read. *Prevents: broadcast-and-hope routing going stale
silently in every author's mental model.*

**8.2 — Separation of duties.** For changes that need review, an **independent
reviewer** reviews adversarially and may commit fixes; the **author** owns the final
acceptance/merge; the reviewer MUST NOT self-merge. If no reviewer is available
within the SLA, the request escalates to a human; work is never accepted unreviewed.
*Prevents: unreviewed self-merges and ambiguous ownership.*

---

## 9. Conformance

A minimally conformant coordination layer provides: a durable lock-free shared store
with reconcile repair (§1); roles + leases + presence + capability routing (§2);
typed loops that close only on the store, with per-recipient acks, a backlog lane,
and SLA escalation (§3); the human as an addressable, scheduled participant (§4);
portable structured checkpoints (§5); the invariants of §7; and policy-as-data
governance with separation of duties (§8). The shared preference layer (§6) is
*optional for basic coordination, required once agents must act on a shared
understanding of a person or decide on behalf of a group.*

**Conformance is testable, and SHOULD be tested.** Each invariant maps to an
assertion a conformance suite can check — e.g. §6.3/§1.4 determinism (identical
inputs → byte-identical fold, across shuffled orderings — a model already realized
in the reference `test_determinism.py`); §7.2 (an erroring read never triggers a
destructive write); §7.3 (a status-only update preserves all other fields); §7.10
(a doubled write yields one effect). A claim of conformance SHOULD ship the tests
that back it; an untested invariant is the one that silently regresses.

---

*This protocol is needs-driven and implementation-agnostic. It is realized in this
repository by `fulcra-coord` (substrate, identity, loops, human-in-loop, governance,
invariants), `fulcra-continuity` (§5), and `fulcra-prefs` (§6) — but nothing here is
specific to those packages or to Fulcra. Where an invariant reads as freshly-won
rather than timeless, it is: several in §7 correct failures these implementations
actually suffered. The per-system derivation is recorded in
[`docs/analysis/`](docs/analysis/).*
