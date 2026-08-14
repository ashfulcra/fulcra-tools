# Fulcra Collect + Coord System Specification

**Status:** Canonical rebuild and interoperability specification, derived
2026-08-14 from shipped code and documentation at `fulcra-tools` main
`393a7449`.

**Audience:** Humans and agentic workers with no prior knowledge of Collect,
Coord, or the Agent Coordination Bus.

**Reading rule:** Unless a section is explicitly marked **target**, it describes
behavior an interoperable implementation must support today. Ratified but
not-yet-fleet-safe work is isolated in §12.

Normative terms MUST, SHOULD, and MAY follow RFC 2119.

---

## 1. What This System Is

One human owns a Fulcra account: a personal timeline of typed records, a
versioned file store, and explicit sharing controls. This system lets a fleet of
AI agents from different vendors, harnesses, and hosts do sustained work over
that human's context without requiring a message broker, shared filesystem,
VPN, or always-on coordination server.

It has three parts:

- **Collect** captures external streams into Fulcra through isolated plugins.
- **The Bus** is the coordination protocol: typed record events plus durable
  documents in the file store.
- **Coord Engine** is the reference CLI implementation: queues, tasks, reviews,
  roles, presence, continuity, deterministic views, health, and routing for any
  shell-capable agent.

```text
 human's world ---> Collect plugins ---> Fulcra account <--- human applications
                                             ^
                                             |
                           events + documents + projections
                                             |
                              Coord Engine agent peers
                     Codex | Claude Code | OpenClaw | others
```

Agents are peers over shared state. They may perform work in GitHub, a model
sandbox, a document system, or another service, but the result must return to
the shared store. An off-store result is evidence, not completed coordination.

The architecture's central choice is simple:

> Models may reason freely; shared coordination state must be durable,
> deterministic, bounded, and auditable.

## 2. Trust, Scope, and Substrate

### 2.1 Fulcra primitives

The system consumes four platform capabilities through `fulcra-api` 0.1.40 or
newer, its library surface, or equivalent OpenAPI calls:

| Primitive | System use |
|---|---|
| Custom `MomentAnnotation/<uuid>` data types | Bounded, range-queryable event channels |
| Versioned file store | Tasks, reviews, presence, checkpoints, configuration, reports |
| `data-updates` time-range feed | Incremental fold and change detection hints |
| Scoped datashares | Explicit cross-account extension |

The file store is last-writer-wins and does not expose proven atomic
compare-and-swap. Listings are eventually consistent discovery caches. Record
timestamps are server assigned. Protocols that require CAS must stay disabled
until the active transport proves that capability.

### 2.2 Authentication and identity

Every agent in one fleet acts with the account owner's platform authority.
Agent identity is a Bus-level attribution claim, not a platform authorization
boundary. No security-critical decision may rely only on a claimed agent name,
model name, source field, or event tag.

OAuth refresh credentials remain in the platform credential store with strict
local permissions. Access tokens and refresh tokens must never enter Bus notes,
documents, argv, logs, reports, or the repository.

### 2.3 Cross-account consent

Parties across a trust boundary each own their own account and credentials.
Exchange uses explicit, scoped datashare grants and a disclosure boundary that
filters and logs what crosses.

A classifier, permission, or disclosure denial stops the operation and surfaces
to the operator. Agents must not decompose work around a denial, silently fall
back to broader access, or self-grant permissions.

## 3. Laws of the System

These rules apply to Collect, the Bus, and Coord Engine:

1. **Fail loud, never silent.** UNKNOWN is not clear, absent, approved, vacant,
   healthy, or complete. Empty results require a positive control.
2. **Durable first.** Write and verify a document before emitting the event that
   points to it. Obligations live in documents, not at-most-once events.
3. **One authority per fact.** Events signal; documents own work; canonical
   shards own independently written evidence; projections accelerate reads.
4. **Bound every operation.** Remote calls, scans, folds, retries, subprocesses,
   outputs, and repairs have positive finite limits.
5. **Degrade in-band.** Machine output carries typed degradation and meaningful
   exit status. Human output names what is unknown and how to recover.
6. **Never silently repair authority.** Invalid configuration, cursors, tasks,
   reviews, or leases remain visible and are not recreated from guesses.
7. **Use deterministic folds.** Ordering, deduplication, tie-breaking, and state
   derivation are code, never model inference.
8. **Fix root causes upstream.** Durable interfaces and one canonical write path
   outrank local defensive patches.
9. **Protect secrets and private topology.** No secrets or team-specific machine,
   cloud, harness, model, or identity mappings belong in the public repository.
10. **Review exact code.** Code review binds to immutable heads; merge requires
    exact-head review and green tests.
11. **Verify shipping state.** A claim that work ran is not execution evidence.
    Test the installed artifact and inspect the source object behind plausible
    answers.
12. **Ship documentation with behavior.** The same change owns code, tests, and
    its canonical documentation.

## 4. Bus Identity and Addressing

An agent identity is a durable Bus name. A session-scoped identity override must
take precedence over persisted workspace identity. Concurrent sessions should use
isolated worktrees or equivalent working contexts so they cannot overwrite each
other's local identity.

The Bus registry provisions four queryable tag dimensions per identity:

- agent;
- machine or cloud host;
- harness, such as Codex, Claude Code, or OpenClaw;
- declared model.

Every event carries the corresponding tag ids. The engine can make these
declarations legible but cannot prove the claimed model or agent.

Fleet reports display identities as:

```text
machine-or-cloud:harness:identity
```

When one identity moves between hosts, reports retain one logical history and
note the movement. The live mappings and movement history are Bus-owned data.
Only their generic schema may live in this repository.

Durable responsibility belongs to roles and capabilities rather than ephemeral
sessions. Routing resolves a fresh, capable holder at dispatch time instead of
hard-coding a session id.

## 5. Event Channel and Queue

### 5.1 Channel authority

Each team has one event data type, resolved from:

```text
team/<team>/_coord/bus-v3/records.json
```

The authority document contains:

- data type and API version;
- protocol and cursor schema versions;
- minimum reader and writer engine versions;
- cursor generation and activation time;
- current engine pin when present.

All authority fields are atomic as a set. A partially upgraded authority is
INVALID, not legacy. An environment override may select a transport endpoint for
an operator, but must never be persisted as protocol authority.

### 5.2 Event envelope

The record's `note` is compact JSON schema `v:1`:

```json
{"v":1,"to":"recipient-or-all","kind":"directive","pri":"P1","slug":"stable-id","ptr":"optional/team/path"}
```

Fields:

- `to`: one agent identity or `all`;
- `kind`: `directive`, `response`, `verdict`, or `claim`;
- `pri`: `P0` through `P3`;
- `slug`: stable join key;
- `ptr`: optional durable document path.

Claims are pure signals and create no obligation. Engine-authored events should
carry a writer stamp containing engine, protocol, and cursor versions. Unknown
payload versions or kinds are not guessed.

The sender is derived from record source metadata. Readers deduplicate retries by
record id and order by server recording time with a deterministic tie-breaker.

### 5.3 Queue read

A queue read makes one bounded range query since the recipient's cursor and keeps
events addressed to that identity or `all`.

- A successful normal read advances only the caller's cursor.
- `peek` never advances a cursor and is the safe foreign-inspection operation.
- Consuming another identity's queue requires a durable takeover audit document
  before cursor mutation.
- An unreadable record window, authority, or cursor yields UNKNOWN and does not
  advance.
- Recognized but malformed control traffic is surfaced as poison or writer-census
  evidence rather than silently dropped.
- A hand-sent bare directive warns because cursor advancement is at-most-once and
  cannot guarantee recovery after a crashed wake.

### 5.4 Cursor schemas

Cursor schema 1 stores one document per agent:

```text
team/<team>/_coord/agents/<agent>/records-cursor.json
```

It provides at-most-once delivery. Therefore, any work that must survive a crash
is represented by a durable Task before its signal is sent.

Cursor schema 2 is implemented but gated off. It uses generation-scoped staged
deliveries and idempotent commit tokens. Activation requires both a doctor-proven
fleet version floor and a transport with proven atomic CAS. The current file
store lacks that proof, so activation fails closed. Rollback tests must execute a
pinned old engine and prove generation-2 state remains byte-identical.

## 6. Durable Documents and Work Loops

All durable coordination state lives under `team/<team>/`:

```text
task/<slug>.md
review/<slug>.md
review/<slug>/verdicts/<head>--<reviewer>.md
review/<slug>/verdicts/<head>--<reviewer>--<timestamp>-<digest>.md
review/<slug>/verdicts/.settled
_coord/responses/<slug>/...
_coord/agents/<agent>/...
_coord/summaries.json
_coord/bus-v3/records.json
_coord/bus-v3/tags.json
```

Documents are Markdown with typed YAML frontmatter. Readers preserve unknown
fields so independently evolving agents remain compatible.

### 6.1 Tasks

Task states are `proposed`, `active`, `waiting`, `blocked`, `done`, and
`abandoned`. `done` and `abandoned` are terminal and immutable.

```text
proposed -> active | waiting | done | abandoned
active   -> waiting | blocked | done | abandoned
waiting  -> active | blocked | abandoned
blocked  -> active | waiting | abandoned
```

Same-state updates are idempotent. Completion requires evidence. A task created
directly in a terminal state requires evidence or an abandonment reason.

A blocked task carries both:

- `blocked_on`: a machine-checkable premise;
- `unlock`: the condition or operation that clears it.

Uncheckable blockers are findings. Supersession may close a live duplicate but
must never rewrite settled history.

### 6.2 Directives, responses, FYIs, and backlog

`tell` writes and verifies a Task, then emits its pointer. That Task opens an
obligation on the assignee. A response closes the obligation only when filed by
the assignee against the same slug.

An FYI is explicitly marked and born closed; it creates no obligation. Backlog
items are durable and board-visible but do not enter an active inbox.

Work performed elsewhere does not close a loop. A merge, PR comment, chat message,
or model assertion may be attached as evidence, but the shared-store response or
state transition is the closure signal.

### 6.3 Operator asks

The human operator is a first-class participant with one consolidated asks view.
Every ask is understandable with zero prior context and includes:

- the concrete question or action;
- options where relevant;
- a recommended default;
- `blocked_on` and `unlock`;
- owner and return path;
- optional not-before and due times.

Upcoming asks do not pollute the current plate. Broadcasts and FYIs never count as
blocked on the operator. Answering records the response, removes the human block,
and assigns the task back to its owner in one logical update.

## 7. Exact-Head Review

The review register is the source of truth for the request. It binds:

- the artifact reference;
- a full 40- or 64-hex object id;
- an immutable required-reviewer set;
- the review round.

Verdicts are canonical only as per-head, per-reviewer shards. Two filename forms
remain interoperable:

```text
<head>--<reviewer>.md
<head>--<reviewer>--<timestamp>-<digest>.md
```

The first is the readable legacy/hand-written form and is mutable. The shipped
verb writes the second, append-only form so concurrent verdicts cannot overwrite
one another on a last-writer-wins store.

The fold keeps the newest valid shard per reviewer deterministically while older
evidence remains auditable:

- any current CHANGES verdict makes the state `CHANGES`;
- every required reviewer must approve for `APPROVED`;
- all other states are `PENDING`.

A cached APPROVED marker may short-circuit only when every evidence shard has an
immutable append-only name and the cache's current listing digest matches. A
mutable legacy shard disables the approval-cache shortcut. A stale cache can
never override newer CHANGES evidence.

A MERGED marker is explicit forge evidence, not a recomputable approval cache.
Merge requires branch tip, forge PR head, and verdict head to match exactly, with
CI green. A changed head opens a new round while preserving the original artifact
reference and required set. Changing that reference or set requires a new slug.

Cross-model review is fleet law: Claude-authored code receives Codex review and
vice versa, at exact heads, with both tests and review green before merge.

## 8. Presence, Roles, Routing, and Continuity

### 8.1 Presence and engagement

Presence is a per-agent shard containing timestamp, current work summary,
capabilities, and engine stamp. It decays through live, idle, and stale with an
absolute grace window. Where cadence is observable, liveness thresholds scale to
that cadence. Batch workers are not declared dead merely because they operate in
bursts.

Engagement distinguishes resident, active, and time-bounded session modes.
Malformed engagement fails closed against marking an agent absent.

### 8.2 Role leases

Roles may be exclusive or shared. A lease carries the holder, timestamp, policy,
SLA, and nonce. The deterministic fold distinguishes:

- `HELD`;
- `LAPSED`;
- `VACANT`;
- `CONTESTED`;
- `DORMANT`;
- `UNKNOWN`.

LAPSED is not VACANT: a known holder whose lease aged out is different from no
holder. An unreadable listing is UNKNOWN and must not become a vacancy claim. A
live nonce mismatch is a takeover signal and reports its age.

Routing selects a fresh capable holder at dispatch time. UNKNOWN role state
refuses confident dispatch. Overdue work may reroute and eventually escalate to
the operator, but escalation remains idempotent and evidence-backed.

### 8.3 Continuity

A structured checkpoint records:

- schema and checkpoint id;
- agent and task;
- objective and recent decisions;
- next actions and open questions;
- artifacts;
- optional context usage and transcript path;
- creation time.

The latest valid checkpoint is selected deterministically. Malformed or future
timestamps cannot shadow valid state.

Snapshotting preserves resumable state. Parking means deliberately leaving and is
a distinct operation with an expiry or explicit operator lift. A successor session
verifies inherited state against the store rather than trusting local memory.
Checkpoint saves may also emit a human-visible timeline moment, but the durable
snapshot remains authoritative.

## 9. Folds, Projections, and Machine Output

Board, status, needs-me, inbox, obligations, threads, briefing, asks, review
status, roles, health, routing, and capacity are deterministic folds.

Canonical documents and evidence shards are truth. `_coord/summaries.json` and
other projection sections are bounded materialized views. A projection may be
served only when its schema is recognized, build is complete, timestamp is valid,
and freshness is positively established.

The preferred read algorithm is:

1. Read an eligible projection.
2. Query `data-updates` from the projection generation time.
3. Overlay changed canonical shards.
4. Serve a bounded result carrying freshness evidence.
5. Perform bounded, in-session read repair when useful.
6. On doubt, use a bounded canonical scan and mark the result degraded.

The feed is a change ledger and hint, not a second state authority. Listings are
discovery caches, not proof of absence. Missing warm infrastructure may increase
latency but cannot alter a correct answer.

Every fold has an aggregate deadline and item budget. A breach produces a typed
degraded row containing reason and scan coverage. A partial result never looks
clean.

`--json` surfaces must emit JSON only on stdout and diagnostics only on stderr.
Output is envelope-first, row and text sizes are bounded, exit status is meaningful
early, and each actionable row carries the fields needed to act, including `ptr`,
artifact `of`, assignee, state, and `unlock` where relevant.

## 10. Coord Engine Reference Implementation

Coord Engine:

- runs on Python 3.10 or newer;
- has no runtime package dependencies;
- invokes `fulcra-api` through argv-array subprocess calls;
- applies hard per-operation and aggregate deadlines, including process-group
  termination;
- distinguishes missing data from transport failure;
- verifies writes by read-back, nonce, immutable id, or equivalent proof;
- retries only bounded idempotent work;
- emits structured logs to stderr;
- uses one positive-finite parse policy for `COORD_*` tuning;
- requires no daemon or server.

Capability groups are:

- task lifecycle and views: reconcile, status, board, needs-me, search, task;
- messaging: tell, broadcast, remind, respond, later, intent, obligations, threads;
- liveness: presence, engagement, agents, roles, escalation;
- operator loop: asks and answer;
- exact-head review and forge evidence;
- continuity, park, resume, and stash transfer;
- annotation and resolution;
- wake routing and execution;
- model/capacity routing: usage, headroom, route, ATC;
- fleet health, doctor, adoption, and acceptance.

The CLI spelling is compatibility surface for the current fleet. State machines,
documents, event envelopes, and failure semantics are the portable architecture.

One-command acceptance pairs two identities through delivery, response, checkpoint,
park, and resume, with per-hop PASS/FAIL evidence.

## 11. Collect Capture Subsystem

### 11.1 Runtime shape

Collect is a Python 3.11+ per-user daemon that hosts capture plugins behind one
local API and web UI on `127.0.0.1:9292`. The stable port preserves OAuth redirect
URIs.

It stores state in:

```text
~/.config/fulcra-collect/state.db
```

CLI control uses `control.sock`. Configuration writes work while the daemon is
down and signal reload when it is running. Commands that execute or inspect live
plugins require the daemon. Collect may run as launchd or a systemd user service
and may expose a menubar companion.

Unlike Coord, this local daemon is intentional: it captures machine-local data.
Coordination correctness does not depend on it.

### 11.2 Plugin contract

A plugin registers under the `fulcra_collect.plugins` Python entry-point group and
resolves to one `Plugin` object. Frozen applications may use an equivalent static
manifest. A load error, wrong object, or duplicate id excludes and reports only
that plugin; healthy plugins continue.

Each plugin declares:

- stable id, name, description, and category;
- execution kind: `scheduled`, `service`, or `manual`;
- independent collection mode: `historical`, `live_polled`, or
  `live_continuous`;
- interval and network requirement where applicable;
- OS permissions;
- credentials and their plugin/user scope;
- typed settings;
- setup steps;
- optional health, permission, OAuth, and freshness callbacks;
- `run(context)`.

Execution kind and collection mode are not derivable from one another. A manually
configured push receiver can be live-continuous.

### 11.3 Run context

The host creates the context and supplies:

- scoped configuration and credential snapshots;
- plugin run state and watermark;
- plugin-local atomic JSON KV operations;
- deduplication claim and rollback operations;
- structured progress and annotation receipts;
- safe credential rotation;
- Fulcra token and definition resolution;
- logging and bounded emit functions.

Plugin code cannot select another plugin's namespace.

Scheduled and manual plugins run in fresh worker subprocesses and stream JSON-line
events to the parent. Plugin stdout is quarantined from the protocol stream. A
timeout kills the worker; a crash cannot kill the daemon. Long-running services
are supervised with restart backoff. The parent is the single writer of run
outcome and watermark state.

Credentials live in the OS keychain under plugin or user scope. Non-secret
settings live in `config.toml`. The setting command refuses credential keys,
unknown plugins, unknown settings, and values outside the declared contract.
Sensitive values use hidden prompts or equivalent non-argv channels.

The daemon checks the Fulcra account fingerprint before ingestion. Definition ids
are cached but periodically revalidated because `data-updates` does not signal
definition deletion. Annotation receipts distinguish attempted from accepted
writes and may include source timestamps. Before any evidence exists, freshness is
UNKNOWN rather than healthy.

### 11.4 Ecosystem rule

Every new capture source becomes a plugin and inherits scheduling, supervision,
credential storage, configuration, setup UI, OAuth plumbing, activity receipts,
and state isolation. Sibling source packages must not rebuild this infrastructure.

## 12. Fleet Evolution (**Target, Not Fully Shipped**)

### 12.1 No-janitor operation

Coordination correctness must work with cloud agents only and zero host-local
maintenance processes. A cron, launchd job, CI schedule, or resident reconciler
must not hold an agent identity and write coordination state outside an accountable
session.

Reads fold `data-updates` deltas in-session. Bounded read repair warms projections
for later readers. Retention runs as a capped, session-attributed role duty. The
legacy `coord-reconcile:*` writer class is denied.

### 12.2 Fleet directives and version fence

Fleet-wide pin, minimum version, and configuration authority live in one
review-gated manifest on the canonical repository's main branch. Pins are full
commit ids. Branch names, abbreviations, and foreign repositories are invalid.

A dedicated annotation channel carries zero-authority wake hints. Any record on
that channel means “fetch and verify the manifest.” Record content cannot select a
pin, move a fence, or alter configuration. Duplicate hints coalesce to one fetch
per wake.

An unreadable or invalid manifest holds current state and surfaces degradation.
Below-fence reads remain legal; writes refuse before mutation. Raising or lowering
the fence requires a reviewed manifest change or revert.

Activation remains feature-gated until every fleet writer can observe the manifest
and enforce the fence.

### 12.3 Event-read cutover and mesh

The event-read primary-plane cutover remains staged and acceptance-gated.

Cross-user mesh is planned as per-user outbox channels over scoped datashares.
Each party writes only its own account; peers read through explicit grants. The
platform prerequisite for channel-carried authority is server-attested record
authorship. Claim-based Bus identity is insufficient.

## 13. Interoperability Contract

An independent implementation must:

1. Read and write event schema `v:1` exactly.
2. Resolve and obey the complete records authority and engine floors.
3. Never activate cursor schema 2 without proven CAS.
4. Never advance another identity's cursor without the audited takeover path.
5. Parse and preserve current Task, Review, verdict, presence, role, response, and
   continuity documents.
6. Implement the exact Task transition graph and evidence requirements.
7. Read both review verdict filename forms and write append-only verdicts.
8. Bind cached approval only to immutable matching evidence.
9. Treat lease nonces and UNKNOWN role state correctly.
10. Emit bounded envelope-first machine output with actionable fields and visible
    degradation.
11. Keep obligations durable across a crash after event consumption.
12. Keep team-specific identity topology on the Bus, not in generic code or docs.

## 14. Acceptance Suite

Shipping requires automated proof that:

1. Two independent identities exchange a durable directive and response.
2. A failed queue read does not advance a cursor or appear empty.
3. Duplicate record ids produce one event.
4. A crash after cursor advancement does not erase a document-backed obligation.
5. Cursor schema 2 refuses activation without CAS proof.
6. Invalid Task transitions and evidence-free completion refuse.
7. A blocked Task without both premise and unlock refuses.
8. Verdicts for another head cannot approve the current head.
9. Concurrent append-only verdicts preserve every evidence file.
10. A stale approval cache cannot override newer CHANGES evidence.
11. Unreadable lease state reports UNKNOWN, not VACANT.
12. Invalid or future checkpoints cannot shadow valid continuity.
13. Projection timeout produces bounded output and visible degradation.
14. Cold canonical folds equal warm-projection folds.
15. One bad Collect plugin does not prevent healthy plugin discovery.
16. A hung Collect worker times out without killing the daemon.
17. Secrets never appear in config, argv, logs, Bus artifacts, or fixtures.
18. Account mismatch prevents Collect writes.
19. Permission or disclosure denial cannot be bypassed by fallback.
20. A forged fleet-directive hint causes at most one manifest fetch and no direct
    state change.
21. A below-fence writer refuses before shared-state mutation.
22. Reference writer to rebuilt reader and rebuilt writer to reference reader both
    pass semantic fixture comparison for every shipped document family.

Live acceptance uses a disposable team and records exact commands, engine versions,
commit ids, timestamps, and sanitized output. No private team topology enters the
fixture bundle.

## 15. Canonical Sources and Change Control

This document consolidates the behavior evidenced by:

- `COORDINATION-PROTOCOL.md`;
- `docs/coord/BUS-V3.md`;
- `docs/coord/NO-JANITOR-SPEC.md`;
- `packages/coord-engine/coord_engine/records.py`;
- Coord task, review, role, presence, continuity, projection, reconcile, routing,
  and transport modules and tests;
- `packages/collect/README.md`;
- Collect plugin, registry, runner, worker, scheduler, supervisor, credentials,
  state, database, daemon, and route modules and tests.

Historical and proposal documents explain why behavior exists, but this file is
the front-door system contract. When shipped behavior changes, update code, tests,
this specification, and interoperability fixtures in the same reviewed change.

The detailed clean-room execution procedure lives at:

```text
docs/superpowers/plans/2026-08-14-collect-coord-bus-rebuild.md
```
