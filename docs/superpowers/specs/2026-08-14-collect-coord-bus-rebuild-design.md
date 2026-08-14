# Collect, Coord Engine, and Bus Rebuild Design

**Status:** Independent reverse specification derived from `fulcra-tools` at
commit `393a7449` on 2026-08-14.

**Audience:** An engineering team that has a Fulcra account and the published
Fulcra API, but no prior knowledge of Collect, Coord, or this repository.

**Normative language:** MUST, SHOULD, and MAY have their RFC 2119 meanings.

## 1. Objective

Rebuild a system in which heterogeneous, ephemeral AI agents can coordinate
durable work over one person's Fulcra account, while local capture adapters add
external life-data streams to the same account.

The system has three products:

1. **Collect** is a local plugin host for importing or receiving external data.
2. **The Bus** is the interoperable coordination protocol over Fulcra records
   and files.
3. **Coord Engine** is a bounded, deterministic command-line implementation of
   that protocol for any shell-capable agent harness.

The rebuild is successful when an independently implemented agent can join an
existing team, exchange work without loss or ambiguity, participate in exact-head
review, recover after context loss, and expose degraded reads as unknown rather
than falsely clear.

## 2. Design Boundaries

### 2.1 Portable contract

The portable contract defines identities, events, documents, state machines,
folds, failure semantics, and acceptance behavior. It must not depend on the
current Python module layout or command parser structure.

### 2.2 Fulcra interoperability binding

The Fulcra binding uses:

- one custom `MomentAnnotation/<uuid>` record type as the coordination event
  channel;
- one optional checkpoint record type for human-visible continuity moments;
- the versioned file store for durable documents and per-agent state;
- `data-updates` as a change hint and incremental fold ledger;
- datashares only for explicitly granted cross-account extensions.

The file store is last-writer-wins and has no proven compare-and-swap. Any
protocol requiring CAS MUST remain disabled until the transport proves it.

### 2.3 Non-goals

- The Bus is not a security boundary among agents sharing one account.
- Claimed agent names are attribution, not authorization.
- Bare events are not durable work queues.
- Materialized views are not authoritative state.
- Collect is not required on cloud-only coordination hosts.
- A cron process, resident janitor, or central broker is not required for Bus
  correctness.

## 3. System Model

```text
external sources -> Collect plugins -> Fulcra records/files <- Coord Engine peers
                                            ^
                                            |
                                      human applications
```

Every cross-session result that matters MUST return to the shared store. Work may
occur in GitHub, a model sandbox, or another service, but an off-store result is
only evidence until the corresponding Bus document is updated.

## 4. Shared Invariants

1. **Durable first.** Write the durable document before emitting its pointer.
2. **Fail closed.** Unreadable, malformed, partial, timed-out, or unverified state
   is UNKNOWN, never empty, absent, approved, vacant, or complete.
3. **One authority per fact.** Events signal; documents own durable work;
   canonical shards own independently written evidence; projections accelerate.
4. **Bound every operation.** Remote calls, folds, scans, retries, subprocesses,
   and output size MUST have explicit limits.
5. **Expose degradation in-band.** Machine output includes typed degraded rows or
   envelope fields and meaningful exit status. Human output names the affected
   surface and recovery action.
6. **No silent repair of authority.** Invalid config, cursor, review, or task
   state is surfaced, not silently recreated.
7. **Deterministic folds.** Ordering, deduplication, tie-breaking, and state
   derivation are code-defined and independent of model judgment.
8. **Secrets stay outside artifacts.** Secrets MUST NOT appear in argv, repo
   files, Bus notes, task documents, logs, or reports.
9. **Exact evidence.** Code review binds to an immutable full object id and is
   valid only for that head.
10. **Sessions own writes.** Coordination writes are attributable to an active
    session identity. Host-local machinery may cache, but cannot be required for
    correctness or act as an unaccountable writer.

## 5. Identity and Capability

An agent identity is a stable Bus name. A session-scoped override MUST outrank
persisted workspace identity so concurrent sessions do not clobber one another.
Concurrent sessions sharing a machine SHOULD use isolated worktrees or equivalent
working contexts.

Each event is tagged across four human-queryable dimensions:

- agent;
- machine or cloud host;
- harness, such as Codex, Claude Code, or OpenClaw;
- model declaration.

The registry mapping these dimensions lives on the Bus, not in the public code
repository. Identity movement between machines is expected and MUST remain
visible in reports without splitting one logical agent's history.

Capabilities and roles are routable concepts. Durable responsibility belongs to
a role; a session holds a lease on it while live. Routing SHOULD resolve a fresh,
capable holder at dispatch time instead of hard-coding an agent name.

## 6. Event Protocol

### 6.1 Channel authority

The team resolves the event channel from:

```text
team/<team>/_coord/bus-v3/records.json
```

The document contains the record type, API version, protocol version, cursor
schema, minimum reader and writer versions, cursor generation, activation time,
and current engine pin where present. A partially populated version authority is
invalid. Environment selection may override transport location for an operator,
but MUST NOT rewrite protocol authority.

### 6.2 Event envelope

The record's `note` is compact JSON:

```json
{"v":1,"to":"agent-or-all","kind":"directive","pri":"P1","slug":"stable-id","ptr":"optional/team/path"}
```

Recognized kinds are `directive`, `response`, `verdict`, and `claim`. Unknown
versions or kinds are not guessed. Each engine-authored event SHOULD include a
writer stamp with engine, protocol, and cursor schema versions.

The sender is derived from record source metadata. Record ids are the retry
deduplication key. Events are sorted by server recording time with a deterministic
tie-breaker.

### 6.3 Queue semantics

A queue read performs one bounded range query since the recipient's cursor,
retains events addressed to that identity or `all`, and deduplicates by record id.

- A normal successful read advances only the caller's cursor.
- `peek` never advances a cursor.
- Taking over another cursor requires an audit document before consumption.
- An unreadable window returns UNKNOWN and does not advance.
- Recognized-but-malformed control traffic is surfaced as poison or census
  evidence, not silently discarded.
- Bare directives warn because at-most-once cursor advancement cannot guarantee
  recovery after a crash.

### 6.4 Cursor generations

Cursor schema 1 is one per-agent JSON document under
`_coord/agents/<agent>/records-cursor.json` and provides at-most-once delivery.

Cursor schema 2 may stage deliveries and commit idempotent tokens, but MUST remain
inactive on a last-writer-wins transport. Activation requires both a proven fleet
version floor and proven atomic CAS. Rollback testing MUST execute an archived old
engine and prove it cannot mutate generation-2 state.

## 7. Durable Work Documents

All team coordination state lives below `team/<team>/`. Required families are:

```text
task/<slug>.md
review/<slug>.md
review/<slug>/verdicts/<head>--<reviewer>[--<timestamp>-<digest>].md
review/<slug>/verdicts/.settled
_coord/responses/<slug>/...
_coord/agents/<agent>/...
_coord/summaries.json
_coord/bus-v3/records.json
_coord/bus-v3/tags.json
```

Documents are Markdown with typed YAML frontmatter. Reimplementations MUST parse
the existing shapes and preserve unknown fields.

### 7.1 Tasks and directives

Task states are `proposed`, `active`, `waiting`, `blocked`, `done`, and
`abandoned`. Terminal states are immutable. Legal transitions are:

```text
proposed -> active | waiting | done | abandoned
active   -> waiting | blocked | done | abandoned
waiting  -> active | blocked | abandoned
blocked  -> active | waiting | abandoned
```

Same-state updates are idempotent. `done` requires evidence. Direct terminal
creation requires evidence or an abandonment reason. Blocking requires both a
machine-checkable `blocked_on` premise and an `unlock` condition.

A directed `tell` creates a durable task before sending its event and therefore
opens an obligation. A response closes an obligation only when filed by the
assignee against that slug. An FYI is explicitly marked and creates no obligation.
Backlog items remain board-visible without entering a recipient inbox.

Operator asks are self-contained: question, options where applicable, default,
unblock condition, owner, and return path. Answering records the answer, removes
the human block, and assigns the task back to its owner in one logical update.

### 7.2 Review

A review register binds:

- artifact reference;
- full 40- or 64-hex head;
- immutable required reviewer set;
- review round.

Verdicts are canonical only as per-head, per-reviewer shards. Append-only shard
names are preferred because the store lacks create-if-absent. The newest valid
shard per reviewer wins deterministically while older evidence remains auditable.

Fold state is `CHANGES` if any current reviewer requests changes, `APPROVED` only
if every required reviewer approves and none requests changes, otherwise
`PENDING`. Cached approval may short-circuit only when bound to immutable evidence
whose current listing digest matches. A merged marker is explicit merge evidence.

Merge policy requires branch tip, forge PR head, and verdict head to match, with
CI green. A changed head opens a new round while preserving the original artifact
reference and required set.

### 7.3 Presence, roles, and engagement

Presence is a per-agent shard with timestamp, current work summary, and engine
stamp. It decays through live, idle, and stale states with grace derived from
observable cadence where available.

Role policies may be exclusive or shared. Lease folds distinguish `HELD`,
`LAPSED`, `VACANT`, `CONTESTED`, `DORMANT`, and `UNKNOWN`. A lease carries a nonce;
a mismatched live nonce is a takeover signal. Unreadable role state is UNKNOWN and
MUST NOT be treated as vacant.

Engagement state distinguishes resident, active, and time-bounded session modes.
Malformed engagement fails closed against marking an agent absent.

### 7.4 Continuity

A checkpoint records schema id, checkpoint id, agent, task, objective, decisions,
next actions, open questions, artifacts, optional context usage, transcript path,
and creation time. The latest valid checkpoint is selected deterministically;
invalid or future timestamps cannot shadow good state.

Snapshotting means preserving resumable state. Parking means deliberately leaving
and must be explicit, expiring, or operator-lifted. Resume verifies the checkpoint
against current shared state rather than trusting local memory.

## 8. Folds, Projections, and Reads

Board, status, needs-me, inbox, obligations, threads, briefing, asks, role status,
review status, health, routing, and capacity are deterministic folds.

Canonical documents and evidence shards are truth. `_coord/summaries.json` and
other projection sections are bounded materialized views. A projection is usable
only when its schema is recognized, timestamp is valid, build is complete, and
freshness is positively established.

The preferred read algorithm is:

1. Read a valid projection.
2. Query `data-updates` from its generation time.
3. Overlay changed canonical shards.
4. Serve a bounded result with its freshness evidence.
5. Perform bounded in-session read repair when useful.
6. On any doubt, fall back to a bounded canonical scan and mark degradation.

The feed is a change hint, not a second authority. Listings are discovery caches,
not proof that no state exists. Successive session-hosted repairs may warm shared
views, but a missing warm process cannot make the system incorrect.

Machine-readable output MUST be envelope-first JSON with no prose on stdout,
bounded row counts and field sizes, action-carrying fields (`ptr`, `of`, `unlock`,
assignee, state), in-envelope degradation, and documented nonzero exit codes.

## 9. Coord Engine Reference Requirements

The reference implementation:

- runs on Python 3.10 or newer with no runtime package dependencies;
- invokes `fulcra-api` as a subprocess transport;
- imposes a hard per-operation deadline, including process-group termination;
- distinguishes missing data from transport failure;
- verifies writes by read-back, nonce, or immutable identity;
- retries only bounded, idempotent operations;
- emits structured diagnostics to stderr;
- uses one positive-finite parsing policy for all numeric `COORD_*` controls;
- has no daemon or server correctness dependency.

Required capability groups are task lifecycle and views, messaging and response,
reviews, roles and presence, operator asks, continuity, health and reconciliation,
forge evidence, annotations, stash transfer, wake routing, capacity/model routing,
version adoption, and a one-command two-agent acceptance proof.

The exact CLI spelling is compatibility surface for the current fleet, but the
portable architecture is defined by behavior and document formats.

## 10. Collect Reference Requirements

### 10.1 Runtime

Collect runs on Python 3.11 or newer as a per-user local daemon bound to
`127.0.0.1:9292`. The fixed port preserves OAuth redirect URIs. It exposes a JSON
API and web UI, stores state in `~/.config/fulcra-collect/state.db`, and accepts
local CLI control through `control.sock`. It may be installed as launchd or a
systemd user service and may have a menubar companion.

### 10.2 Plugin contract

Plugins register through the `fulcra_collect.plugins` Python entry-point group
and resolve to one `Plugin` object. A bad plugin is isolated and reported; it does
not prevent healthy plugins from loading. Frozen builds may use an equivalent
static manifest.

Each plugin declares:

- stable id, display name, description, category;
- execution kind: `scheduled`, `service`, or `manual`;
- collection mode: `historical`, `live_polled`, or `live_continuous`;
- interval and network requirement where applicable;
- required OS permissions, credentials, typed settings, setup steps, and optional
  OAuth, health, permission, and freshness callbacks;
- a `run(context)` callable.

Execution kind and collection mode are independent. For example, a manually
configured push receiver may be live-continuous.

### 10.3 Context and isolation

The host creates the run context and supplies scoped configuration, credentials,
plugin state, plugin-local atomic JSON KV storage, deduplication claims, progress
events, annotation receipts, credential rotation, and Fulcra definition
resolution. Plugin code cannot select another plugin's state namespace.

Scheduled and manual imports run in fresh worker subprocesses and stream JSON-line
events to the parent. A timeout kills the worker; a crash cannot kill the daemon.
Long-running services are supervised with restart backoff. The parent is the
single writer of run outcome and watermark state.

Credentials live in the OS keychain, either plugin-scoped or user-scoped as
declared. Settings live in `config.toml`. The setting command refuses credential
keys, unknown keys, unknown plugins, and invalid values. Sensitive input uses a
hidden prompt or equivalent non-argv channel.

The daemon verifies the Fulcra account fingerprint before ingest. Definition ids
are cached but periodically revalidated because `data-updates` does not report
definition deletion. Write receipts and source timestamps feed freshness checks;
absence of evidence is UNKNOWN, not healthy.

## 11. Fleet Evolution Without a Janitor

Fleet-wide authority lives in one review-gated manifest on the canonical repo's
main branch. A dedicated Fulcra record channel carries zero-authority wake hints:
any record on that channel means “fetch and verify the manifest.” Record content
cannot select a version, lower a fence, or change configuration.

Before a coordination write, an agent verifies that its engine meets the manifest
minimum. Below-fence reads remain legal; writes refuse. An unreadable or invalid
manifest holds current state and surfaces degradation. Pins are full commit ids;
branches, abbreviated ids, and foreign repositories are invalid.

Every coordination write occurs inside an attributable agent session. Read repair
and retention run as bounded session duties. The legacy `coord-reconcile:*` writer
class is denied. Cloud-only operation with no laptop daemon is the baseline.

This section is a ratified target at the source snapshot, not fully shipped
behavior. Implementations MUST feature-gate it until the fleet can observe the
manifest and fence safely.

## 12. Acceptance Tests

An interoperable rebuild is accepted only when it proves:

1. Two independent identities can exchange a durable directive and response.
2. A failed queue read does not advance a cursor or look empty.
3. Duplicate record ids yield one event.
4. A crash after cursor advancement cannot erase a document-backed obligation.
5. Cursor schema 2 refuses activation without CAS proof.
6. Invalid task transitions and evidence-free completion refuse.
7. A blocked task lacking an unlock premise refuses.
8. Review verdicts for another head cannot approve the current head.
9. Concurrent append-only verdicts preserve both pieces of evidence.
10. A stale approval cache cannot override a newer changes verdict.
11. Unreadable lease state reports UNKNOWN, not VACANT.
12. Invalid/future continuity timestamps cannot shadow a valid checkpoint.
13. Projection timeout returns visible degradation and bounded output.
14. A missing warm reconciler does not change fold correctness.
15. One bad Collect plugin does not prevent discovery or execution of others.
16. A hung Collect worker times out without killing the daemon.
17. Credential values never appear in config, argv, logs, or Bus artifacts.
18. Account mismatch prevents Collect writes.
19. A forged fleet-directive hint causes at most one manifest fetch and no direct
    state change.
20. A below-fence writer refuses before mutating shared state.

## 13. As-Built Evidence and Deliberate Abstraction

This design was independently traced through:

- `COORDINATION-PROTOCOL.md` for needs-level guarantees;
- `docs/coord/BUS-V3.md` for the event and cursor protocol;
- `docs/coord/NO-JANITOR-SPEC.md` for the ratified feed-first and fleet-manifest
  direction;
- `packages/coord-engine/coord_engine/records.py` for event/config/cursor shapes;
- `model.py`, `tasks.py`, `review.py`, `roles.py`, `presence.py`,
  `continuity.py`, `projection.py`, and `transport.py` for deterministic state and
  failure contracts;
- `packages/collect/README.md`, `plugin.py`, `registry.py`, `runner.py`,
  `worker.py`, `scheduler.py`, `credentials.py`, `db.py`, and `daemon.py` for the
  capture subsystem.

The current Coord CLI concentrates substantial orchestration in `cli.py` and
exposes more verbs than a minimal clean rebuild needs internally. This design
preserves their observable capabilities while refusing to treat the current file
layout or parser size as architecture. Likewise, Collect's web routes, menubar,
and bundled source adapters are product surfaces built on the plugin contract, not
new coordination primitives.
