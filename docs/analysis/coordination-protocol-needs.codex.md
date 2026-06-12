# Coordination Protocol Needs Analysis (Codex, Blind Pass)

This is an independent needs analysis for a general human-agent and agent-agent
coordination protocol. I used `packages/fulcra-coord`, `packages/fulcra-continuity`,
and `packages/fulcra-prefs` as evidence of recurring problems, but this document
describes the needs any adopter would have, not those systems' internals.

I intentionally did not read the parallel `docs/analysis/coordination-protocol-needs.ideas-inbox.md`
draft before writing this.

## 1. Core Problems A Coordination Layer Must Solve

### Durable shared state across agent runtimes

Need: Agents need one durable coordination substrate that survives process death,
context compaction, laptop sleep, cloud sandbox teardown, and switching between
Claude, Codex, ChatGPT, local tools, CI, and other runtimes.

Why: Agents do not share memory, transcripts, local filesystems, shells, or
network topology. Without a shared ledger, "I told the other agent" is only a
chat artifact, not a recoverable operational fact.

### Work ownership and pickup

Need: The protocol must distinguish proposed work, active work, paused work,
blocked work, completed work, and abandoned work, and it must show who owns or is
expected to act on each item.

Why: The common failure is not that no one can do the work; it is that everyone
thinks someone else already picked it up. A durable lifecycle turns "maybe being
handled" into an inspectable state.

### Directed communication, not just shared notes

Need: The protocol must support one-to-one directives, broadcast notices, review
requests, handoffs, verdicts, questions, signoffs, and backlog ideas as
addressed records with delivery semantics.

Why: A generic task list cannot tell the difference between "FYI", "please do
this", "please review this", and "this is blocked on the human". Those messages
need different lifecycle, routing, expiry, and response behavior.

### Human situational awareness

Need: The human needs a concise "what is blocked on me, what is waiting, what is
stale, who is live, and what changed" view without reading every task body.

Why: Human attention is the scarce resource. If the system only stores data but
does not surface the human's plate, work will still stall silently.

### Failure recovery as a first-class behavior

Need: The protocol must assume partial reads, partial writes, stale views,
concurrent writers, transient transport failures, old clients, and malformed
records.

Why: Multi-agent systems fail through boring distributed-systems edges: one file
uploads while another view fails, a summary gets stale, a host sleeps mid-write,
or two agents update the same record. The protocol must define how to recover
without silent data loss.

### Context continuity

Need: Operational task state must be paired with richer resume packets that
carry objective, decisions, artifacts, open questions, and concrete next steps.

Why: "Task active; next: continue" is not enough after compaction or cross-agent
handoff. The receiving session needs a cold-start brief, not a breadcrumb.

### User-owned context and preference sync

Need: Agents need a portable, consented way to share user preferences, facts,
and context across platforms, with deterministic projections and disclosure
audit.

Why: Without shared user context, agents repeatedly ask the same questions or
infer preferences inconsistently. Without consent and audit, "helpful memory"
becomes ungoverned data leakage.

## 2. Required Primitives And Capabilities

### Identity

Need: Stable logical identities for agents, humans, roles, sessions, and
workstreams.

Requirements:

- Agents have durable IDs independent of transient process IDs.
- Humans are addressable as first-class coordination participants.
- Roles can exist apart from sessions, with live sessions leasing them.
- Workstreams are open labels that group work without forcing a global taxonomy.
- Identity resolution has a clear precedence order, and every command uses the
same resolution path.

Why: Routing to "the active reviewer role" is different from routing to
"whatever process happened to exist yesterday". Identity drift creates orphaned
inboxes and lost work.

### Ledger Records

Need: A durable record family for work items and communication loops.

Minimum fields:

- id, title, kind, status/state, priority, workstream
- owner/sender, assignee/recipient, collaborators
- summary, next action, blocker, due/not-before scheduling
- created/updated timestamps and last-touched-by
- evidence and verification on terminal success
- tags or structured memberships for routing and projection
- links/artifacts/remote paths
- bounded inline history plus durable event or sub-log history

Why: The protocol needs enough structure to route, resume, audit, and recover
without parsing prose.

### Lifecycle State Machines

Need: Separate but compatible lifecycle machines for work and message loops.

Work lifecycle:

```text
proposed -> active -> waiting -> active
                 \-> blocked -> active
                 \-> done
                 \-> abandoned
```

Message-loop families need their own semantics:

- tell/FYI: sent, acked, closed; no response by default
- dispatch/handoff: assigned, accepted, in-progress, delivered, closed
- review: requested, acked, in-review, responded, closed
- question/signoff: asked, answered, closed
- idea/backlog: captured, maturing, viable, routed, active, done/dropped

Why: One state machine cannot safely represent both "build the feature" and
"review this PR". Expecting loops must remain open until a bus-native response;
FYIs should not clog the system forever.

### Directed Inbox And Ack

Need: Addressed directives must be queryable by recipient, role, or broadcast
audience, and a recipient must be able to acknowledge delivery without claiming
ownership.

Why: Delivery and work pickup are different facts. "I saw this" should stop
renotification; "I am doing this" should change lifecycle state.

### Routing And Escalation

Need: The protocol must route work by explicit assignee, role, capability,
liveness, configured reviewer preference, or human escalation.

Requirements:

- Capability declarations are live presence data, not static config only.
- Routing records candidate pool, winner, reason, and attempts.
- A no-live-recipient case escalates to a human-visible ask.
- Rerouting has caps and does not yank accepted work midstream.
- Review routing is forge-agnostic: the artifact can be a PR, branch, patch,
  URL, file, or spec.

Why: The coordination problem is often "find someone live who can do X", not
"send this to exactly the stale agent named in an old doc".

### Presence And Liveness

Need: Agents publish presence records with last-seen time, workstreams, roles,
capabilities, and a short summary.

Requirements:

- Liveness states are computed from wall-clock freshness.
- Presence is best-effort and never required for correctness.
- Stale presence must not erase durable task state.
- Role leases ride presence and expire naturally.

Why: Presence helps route and diagnose, but durable work must survive missing
heartbeats.

### Materialized Views And Cheap Reads

Need: The protocol must provide cheap projections for common views: status,
inbox, active work, next work, recently done, search, needs-human, presence, and
per-workstream summaries.

Requirements:

- Views are caches, not truth.
- Readers can fall back to durable records when a view is stale or missing.
- Views carry freshness metadata.
- Write paths can skip unchanged view uploads, but reconcile can rebuild all.

Why: Agents cannot list and fetch hundreds of records on every session start,
but trusting stale views is how directives disappear.

### Reconcile And Retention

Need: A periodic reconciler must repair derived state, replay or clear repair
markers, sweep stale routing, expire completed informational messages, and prune
bounded histories.

Why: Any protocol with non-atomic fan-out needs a cleanup pass. Without it,
partial writes and obsolete notices accumulate until reads become slow or wrong.

### Continuity Checkpoints

Need: A portable checkpoint packet, separate from the operational ledger, that
can be rendered by a receiving agent.

Minimum checkpoint fields:

- objective and success condition
- current state
- decisions made and rejected alternatives
- open questions
- ordered next actions
- portable artifacts: URLs, repo/ref/path triples, remote file paths, task IDs,
  checkpoint paths, command outputs
- producing identity and optional coord task identity
- memory writes or facts that should survive the checkpoint

Why: Operational state answers "who owns this"; continuity answers "how do I
continue without reading the transcript".

### Preference And Context Signals

Need: User context should be represented as typed signals and compiled
deterministically into current platform-specific views.

Requirements:

- Preferences, facts, and consent are typed.
- Signals have source, platform, session, confidence, strength, and decay.
- Compiled docs are deterministic for fixed inputs.
- Platform-specific context overlays global context.
- Exports are consent-filtered and logged.
- Shell-less agents can read/capture through HTTP while code-capable agents run
  the compiler.

Why: Cross-platform agents need the same user context, but agents should not
re-derive preferences ad hoc in-session or leak private data without a ledger.

## 3. Invariants And Guarantees

### No silent data loss

Every operation that claims success must either make the authoritative record
durable or leave a visible repair marker, warning, or escalation.

Why: The worst failure is a sender believing a directive landed while the
recipient never sees it.

### Records are truth; views are disposable

Derived views may be stale, missing, or partially uploaded. The record family
must be sufficient to rebuild all views.

Why: Multi-file view fan-out is not atomic. Treating views as authoritative
causes invisible work during partial failures.

### Response-required loops stay open

If a directive expects a response, it must not disappear because it aged out,
was merely acked, or the assignee went stale. Only a bus-native response,
decline, closure, or explicit human action should close it.

Why: This is the closed-loop guarantee. Without it, review requests and handoffs
can look administratively clean while no one actually answered.

### Terminal work requires evidence

Done requires evidence and a verification level. Abandoned requires a reason.

Why: A terminal status without evidence destroys accountability and makes
post-hoc review impossible.

### Idempotency by construction

Repeated acks, route events, repair passes, listener ticks, checkpoint writes,
and compile passes should be safe to run more than once.

Why: Agents and schedulers retry. A protocol that treats retries as new work
will duplicate tasks or spam humans.

### No compare-and-swap assumptions

If the storage layer lacks conditional writes, the protocol must avoid shared
read-modify-write hot spots or merge them deliberately.

Why: Concurrent agents will clobber single-record fields unless multi-writer
facts are stored as per-writer shards, append-only events, or mergeable sets.

### Fail safe on unknown state

Unknown remote state, unparseable timestamps, unknown loop kinds, corrupt
payloads, and stale liveness must keep work visible or retryable rather than
close it silently.

Why: Optimistic cleanup is dangerous. The protocol should prefer visible debt
over hidden loss.

### Deterministic folds

Any computed current state, whether views, preference docs, or solver rankings,
must be deterministic for the same inputs.

Why: Agents in different runtimes need to agree on the current truth without
negotiating with an LLM every time.

### Portable artifacts only for handoff

Handoff artifacts must be resolvable by the receiver.

Why: Local paths, shell history, and unpushed branches are not portable across
machines, sandboxes, or tools.

## 4. Roles And Relationships

### Human to agent

The human can initiate work, set priorities, approve/deny, answer blockers, and
receive escalations. The protocol should make the human's plate explicit, not
bury it in agent logs.

Why: Human-blocked work is only actionable if the human sees the ask, deadline,
and waiting agent.

### Agent to human

Agents can block on the human with a concrete ask, a due date, and optional
not-before. They can also request governance decisions, credentials, approvals,
or repo ownership calls.

Why: A vague "blocked" field does not tell the human what to do.

### Agent to agent

Agents can send FYIs, assign work, hand off state, request reviews, answer
questions, return verdicts, and escalate failures.

Why: Multi-agent fleets are not just parallel workers; they are a distributed
organization with delegation, review, and continuity.

### Reviewer and author

Review must be an explicit relationship: requester, reviewer, artifact, verdict,
evidence, and, when the reviewer changed code, a second signoff or author review.

Why: "Someone looked at it" is not enough. The protocol needs to prevent an
agent from merging unreviewed work and to distinguish approval from changes.

### Role and session

Roles are durable functions; sessions are temporary holders.

Why: "Reviewer" may be staffed by different processes over time. Routing to a
role should work even as individual sessions come and go.

## 5. Task And Work Lifecycle Needs

The work lifecycle should answer:

- Is this merely proposed, or did someone pick it up?
- Who owns it now?
- What is the next concrete action?
- Is it blocked, and on whom?
- Is it waiting by choice, by schedule, or by missing input?
- Is it stale?
- What evidence proves completion?
- Can someone cold-resume it?

The model must make illegal states hard:

- blocked work needs a blocker or ask
- waiting work needs a next action
- done work needs evidence
- self-owned implementation work cannot satisfy its own independent-review gate
- unreviewed code cannot be merged by its author

Why: The lifecycle is the protocol's shared memory. If it allows ambiguous
terminal or blocked states, the fleet cannot coordinate reliably.

## 6. Messaging And Directive Model

The protocol needs a first-class directive/message family, not only generic
tasks with prose.

Directive requirements:

- sender and recipient are explicit
- message type/kind is explicit
- "expects response" is explicit
- ack is distinct from response
- response/verdict is a durable record, not only a forge comment
- broadcasts ack per recipient
- old informational messages can auto-close after a TTL
- response-required messages never auto-close by TTL
- routing attempts and reassignment are auditable

Why: This prevents both classes of failure: never-ending informational clutter
and premature closure of real asks.

## 7. Identity, Presence, And Liveness

Identity must be stable enough for inboxes and accountability, but flexible
enough for many runtimes.

Needs:

- per-workspace or per-context identity persistence
- explicit override for session-scoped identities
- human handle resolution
- role registry and role lease state
- capabilities advertised by live sessions
- liveness thresholds with grace for sleep/wake
- stale and contested roles surfaced visibly
- identity migration or forwarding when an agent changes IDs

Why: A directed task is only as good as the identity it targets. If identity
changes silently, work is orphaned.

## 8. Continuity And Handoff Across Context Loss

The protocol must treat context loss as normal.

Needs:

- session-start resume briefing
- pre-compaction checkpoint
- session-end or idle parking when possible
- no-stop-hook runtimes compensated by explicit checkpoints and heartbeat
- cross-agent handoff messages that carry checkpoint refs
- producer checkpoint paths or JSON for cross-agent transfer
- pickup checkpoints by the receiver
- artifact portability rules

Why: A handoff that requires the next agent to read the old transcript is not a
handoff; it is a hope.

## 9. Preference, Fact, And Context Sync

User context needs a protocol-level place because it shapes agent decisions but
should not be re-inferred by each agent.

Needs:

- typed signal capture
- confidence and source metadata
- decay for preferences, durable treatment for facts
- supersession/correction
- deterministic compile into current docs
- per-platform overlays
- group-decision solver with trace
- consent grants and disclosure logging
- offline outbox and retry
- tiered access for CLI-capable, HTTP-capable, and read-only agents

Why: Preferences are operationally relevant, but they are also private and
change over time. The protocol needs both usefulness and governance.

## 10. Failure Modes And Recovery Needs

### Partial write

Need: If the authoritative record lands but views fail, keep the record and mark
views for repair.

Why: Losing the task because a view failed is backwards; views are recoverable.

### Unknown delivery

Need: Verify writes where possible, retry transient failures, and leave repair
markers when delivery is unconfirmed.

Why: Success-shaped uploads can still fail to become visible.

### Stale views

Need: Views carry generated-at timestamps, and readers fall back to durable
records if the view is stale.

Why: A stale summaries file can make an inbox look empty while task files exist.

### Concurrent writers

Need: Avoid global shared mutation for multi-writer facts. Use append-only
events, per-writer shards, or mergeable set fields with conflict detection.

Why: Broadcast acks, route events, and preference captures are natural
multi-writer data.

### Transport outage

Need: If remote state cannot be confirmed, keep markers and retry rather than
clearing them.

Why: "Could not read" is not the same as "absent".

### Stale routing

Need: Review and dispatch routing should detect recipients that went dark and
reroute or escalate, within caps.

Why: Liveness at send time is not a guarantee of future action.

### Old or mixed clients

Need: New fields and loop kinds must degrade safely for older records and old
clients.

Why: A fleet upgrades gradually. The protocol cannot require a flag day.

### Malformed records

Need: Readers tolerate missing optional fields and surface malformed records
without crashing global views.

Why: One bad record should not break the whole fleet's status surface.

## 11. Governance And Review

The protocol must encode review and governance norms, not leave them in chat.

Needs:

- every non-trivial change goes through an artifact and review loop
- reviewer identity differs from author identity for independent review
- review is adversarial and evidence-bearing
- reviewer fixes create a secondary signoff need
- clean approvals can land once checks pass
- no agent merges its own unreviewed code
- human escalation if no reviewer is live
- review verdicts are durable bus records, not only forge-native state
- specs/designs/plans can be reviewed, not only code

Why: Agent fleets move fast enough that informal review rules drift. Durable
review loops make the control auditable and recoverable across tools.

## 12. Anti-Creep Boundaries

A general protocol should keep these boundaries sharp:

- Coordination ledger: owns task/message state, routing, presence, and views.
- Continuity: owns rich resume packets and handoff context.
- Preference/context layer: owns user memory, consent, and compiled context.
- Forge integrations: mirror artifact state but do not become the source of
  coordination truth.
- Consoles/digests/notifications: read projections and push awareness, but
  should not define core semantics.

Why: If every concern enters one task object, the protocol becomes impossible to
adopt. Separate primitives let small adopters implement the minimum while still
interoperating with richer fleets.

## 13. Minimum Adoptable Protocol

The smallest useful general protocol is:

1. Durable task/message records with stable identity, owner, assignee, status,
   summary, next action, and evidence.
2. A legal lifecycle with evidence-gated terminal states.
3. Directed inbox with ack and response distinction.
4. Presence with liveness and capabilities.
5. Human-blocked surface.
6. Review request and verdict records.
7. Reconcile/repair semantics for partial writes.
8. Portable continuity checkpoints at handoff boundaries.
9. Deterministic user-context projection with consented export, if agents share
   memory/preferences.

Everything else can be layered on top: digests, notifications, rich dashboards,
forge mirroring, automated rerouting, retention, group solvers, and self-update.

## 14. Design North Star

A coordination protocol should make these statements true:

- If an agent was asked to do something, there is a durable addressed record.
- If a response is required, the loop stays open until a response is recorded.
- If work is complete, the evidence is attached.
- If work is blocked on the human, the human can see the ask.
- If an agent dies, another agent can see the work and resume it.
- If an artifact needs review, the review request and verdict are visible
  outside the forge.
- If context is needed after compaction, there is a portable checkpoint.
- If user preference/context influences behavior, it is typed, deterministic,
  consented, and auditable.

That is the core need: not a task tracker, not a chat log, and not a memory
system by itself, but a small set of durable coordination primitives that make
human-agent and agent-agent work recoverable.
