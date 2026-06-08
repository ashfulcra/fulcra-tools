# Greenfield architecture for fulcra-coord + fulcra-continuity

## 1. One-line thesis / organizing principle

Build one small append-friendly coordination substrate over Fulcra Files, then keep tasks, presence, review routing, operator digest, and continuity checkpoints as typed record families on top of it rather than as separate feature piles that each rediscover storage, identity, and concurrency rules.

## 2. Architecture - the substrate, the layers/packages, and the core data model

The substrate should be a stdlib-only `fulcra_bus` package with no agent-specific concepts. Its job is to turn Fulcra Files, which has upload/download/list/stat/delete but no compare-and-swap, into a predictable eventually-consistent log-and-view system. It should expose:

- `RecordRef`: `{family, id, path, schema, owner_key}`.
- `RecordEnvelope`: `{schema, family, id, version, created_at, updated_at, writer, generation, body, events}`.
- `append_event(ref, event)`: read current envelope, merge event sets by stable event id, upload the body, then rebuild affected views.
- `put_latest(ref, body)`: for naturally last-writer-wins records such as per-agent presence or `latest.json` pointers.
- `rebuild_views(families)`: enumerate durable record files and regenerate summaries/views from source records.
- `read_view(name)`: fast path that prefers materialized summaries but falls back to record enumeration when a view is absent or malformed.

Because Fulcra Files has no CAS, the write model should stop pretending there is a global linearizable object. Use per-record ownership and append-friendly event ids as the base guarantee:

- Each durable source record is a separate file under a family path, so different tasks, agents, and checkpoints do not contend.
- Every event carries `event_id`, `at`, `writer`, `observed_generation`, and a typed payload. Merge is set-union by `event_id`, then deterministic sort by parsed timestamp and id. If two status events conflict, the transition reducer decides whether they commute; otherwise the later writer leaves a `conflict` event instead of silently carrying fields.
- Summaries and views are cache, not truth. They are always replaceable from source records.
- Optimistic stat/version metadata is still useful, but only as a warning that a merge is needed, not as a correctness dependency.
- Reconcile is a first-class command that rebuilds all derived views from source families and writes a health record about what it repaired.

On top of that substrate, keep three packages:

- `fulcra-coord-core`: task, directive, presence, review, human-blocker, digest, and health schemas plus reducers. This is the only package that knows what a task status is.
- `fulcra-continuity-core`: checkpoint schema, resume rendering, checkpoint retention, and lookup APIs. This package owns the checkpoint schema instead of letting coord duplicate it by convention.
- `fulcra-coord-cli`: adapter and command surface for Claude Code, Codex, OpenClaw, ChatGPT, and generic agents. It should be boring glue: parse args, resolve identity, call core services, print results.

The remote layout should be explicit by record family:

```text
/coordination/
  records/
    tasks/TASK-*.json
    directives/TASK-*.json
    presence/<agent-slug>.json
    checkpoints/<workstream>/<agent>/<task>/CHK-*.json
    checkpoints/<workstream>/<agent>/<task>/latest.json
    health/<host>.json
    digests/<date-window>.json
  views/
    summaries.json
    inbox/<agent-slug>.json
    tasks/active.json
    tasks/next.json
    tasks/recently-done.json
    tasks/search-index.json
    agents/<agent-slug>.json
    workstreams/<workstream>.json
    presence.json
    needs-human/<human>.json
```

Tasks and directives can remain one schema if desired, but the target design should model the distinction directly: a task is work being executed; a directive is work being asked of someone. That keeps "broadcast ack," "review request," "signoff request," and "human blocker" from being disguised as ordinary ops tasks.

The core data model should be:

- `Task`: durable work item with `status`, `owner_agent`, `workstream`, `priority`, `summary`, `next_action`, `blocker`, `links`, and an event log.
- `Directive`: addressed message/work request with `assignee`, `audience`, `directive_type`, `acked_by`, `expires_at`, optional `artifact_ref`, and an event log.
- `Presence`: one last-writer-wins record per agent with `agent`, `workstreams`, `capabilities`, `summary`, `session`, and `last_seen`.
- `ReviewArtifact`: forge-agnostic review target reference, not a GitHub-only PR number. Fields: `artifact_type`, `ref`, `repo`, `branch`, `url`, `author_agent`, `author_identity`, `head_sha`.
- `ReviewDecision`: directive/event carrying `artifact_ref`, `verdict`, `reviewer_agent`, `fix_commits`, `evidence`, and `requires_second_signoff`.
- `ContinuityCheckpoint`: the current fulcra-continuity checkpoint schema, but stored by the shared substrate and linked from task/directive summaries by `checkpoint_ref`.
- `OperatorDigest`: generated projection record, not source truth.

Views should be generated by reducers over these families. Reads should never need to fetch full task bodies during normal operation: `views/summaries.json` remains the fast path, but its failure mode must be "rebuild from records" instead of "silently trust the aggregate."

## 3. Biggest departures from the current design

First, separate source records from projections as an architectural rule, not just a set of repaired bugs. The current code now treats task files as truth and summaries as a fast path, but that rule is encoded across `io.py`, `writepipe.py`, `views.py`, and retention details. In the target, this is the storage contract every feature inherits.

Second, make directives first-class. Today direct asks, broadcasts, review requests, signoff asks, and no-action receipts are all tasks with `assignee`, tags, `acked_by`, and event conventions. That was a good bootstrap, but it makes inbox semantics leak into task lifecycle and lets FYI noise look like blocked work. Directives should have their own schema, expiry, ack behavior, and routing reducers.

Third, pull continuity into the shared substrate while keeping it a separate product package. The current coord bridge deliberately avoids importing `fulcra-continuity` and reimplements the schema in stdlib. That was the right integration compromise, but the greenfield design should instead make the checkpoint schema a core library with no CLI/runtime dependency. Coord imports a small schema/reducer package, not a separate operational CLI.

Fourth, make review handshakes forge-agnostic from day one. The system should store review artifacts and decisions as bus-native records, with GitHub PRs only one artifact adapter. GitHub comments can mirror evidence, but the bus is the durable handshake.

Fifth, replace feature-specific lifecycle hooks with a small event API. `pause --snapshot`, `pre-compact`, listener idle snapshots, digest emission, annotations, and review routing should all consume typed events. The CLI should not be the place where every subsystem sneaks in another side effect.

Sixth, treat adapters as adapters. Claude Code, Codex, OpenClaw, ChatGPT facade, launchd, cron, and hooks should all use the same core services. No adapter should own product rules such as "reviewer cannot merge unreviewed Codex work" or "do not checkpoint on every update."

## 4. What to keep unchanged (the hard-won right calls)

Keep Fulcra Files as the only shared store. The brokerless constraint is not a limitation to apologize for; it is the reason the tool works across laptops, cloud sessions, and disconnected agent products.

Keep per-agent identity and presence. The presence subsystem is core-by-usage because it gives the human operator situational awareness and gives reviewer routing a real liveness signal.

Keep materialized views. Agents need cheap `inbox`, `resume`, `needs-me`, `agents`, and `search` reads. The right correction is to make view derivation more principled, not to go back to full-body scans on every command.

Keep durable per-record files as source truth. The strongest fixes in the current system came from respecting that each task file is more durable than any aggregate view.

Keep the no-CAS mental model. The system should assume concurrent writers, stale summaries, partial view uploads, malformed old records, and machines with different clocks. Parsed timestamps, event ids, deterministic reducers, and reconcile are the right tools.

Keep the state machine. `proposed`, `active`, `waiting`, `blocked`, `done`, and `abandoned` are understandable and operationally useful. The design should simplify what enters that state machine, not replace it with a more abstract workflow engine.

Keep the operator surfaces. `resume`, `needs-me`, twice-daily digest, stale-task health, and direct inbox notification are the product center. They should become smaller, cleaner projections over better records.

Keep bounded continuity writes. Checkpoints belong at durable pause points: pre-compact, handoff, idle/overnight, explicit done-for-now, or several task events without user action. Writing a checkpoint on every task update would make continuity noisy and expensive.

## 5. Where checkpoints/continuity live in the new design

Continuity should live as a sibling source-record family under the shared bus substrate, not as a task field and not as an unrelated package that coord shadows.

`fulcra-continuity-core` owns:

- `ContinuityCheckpoint` schema and validation.
- `make_checkpoint`.
- `render_resume_brief`.
- `checkpoint_from_dict`.
- retention policy for checkpoint archives.
- lookup helpers: latest by exact `coord_task_id`, by `{workstream_id, agent_id, coord_task_id}`, and by explicit remote path.

`fulcra-coord-core` owns when to write checkpoint records:

- `snapshot task --reason <reason>` writes an immutable checkpoint and updates `latest.json`.
- `pause --snapshot` is a task transition plus checkpoint event in one command.
- lifecycle hooks call the same snapshot service at pre-compact/session-end/idle points.
- `resume --with-continuity` reads checkpoint summaries and shows them as attached context, not as task truth.

Cross-agent handoff should be explicit. Same-agent resume can look up latest by task identity. Cross-agent handoff must include either the producer checkpoint JSON, the remote checkpoint path, or a record link carried in the directive. Do not infer that a receiver should read another agent's latest checkpoint just because the task id matches.

Every task and directive summary can carry `checkpoint_ref` as an optional pointer to the latest relevant checkpoint, but task lifecycle must not depend on it. A task without continuity is still a valid task. A checkpoint without an active task is still a useful resume packet.

## 6. How to structurally prevent the scope creep from recurring

Set a package boundary rule: `fulcra-coord-core` is allowed to coordinate work, route directives, surface operator state, and link checkpoints. It is not allowed to become a generic agent runtime, a GitHub client, a memory system, a notification platform, or a chat facade. Those belong in adapters or separate packages.

Make record families pass an "operator surface" test before entering core. A new core family must improve at least one of: `inbox`, `resume`, `needs-me`, `presence`, `review handshake`, `health`, or `continuity resume`. If it does not, it is not coord core.

Require every new feature to declare:

- source record family touched,
- derived views changed,
- conflict behavior under no CAS,
- retention/expiry behavior,
- human/operator surface it improves,
- adapter boundaries,
- focused tests that would fail if the feature were removed.

Keep forge and vendor integrations behind artifact adapters. GitHub, Claude Code, Codex, OpenClaw, ChatGPT, launchd, cron, Slack, Discord, and ntfy should never be imported by the core reducers. They produce or consume bus records.

Keep `fulcra-continuity-core` small and schema-centered. It should not grow task lifecycle, inbox, presence, or review routing. It writes and renders checkpoints. Coord decides when checkpoints matter operationally.

Make scope visible in CI. Add an architectural test that fails if core imports adapter modules or forge-specific CLIs, and another that fails if `fulcra-coord-core` imports `fulcra-continuity-cli` rather than the schema/core package. The current forge-agnostic AST guard is the right instinct; apply it to package boundaries, not just subprocess calls.

Finally, make the README/product docs name the center of gravity plainly: coord is a brokerless coordination ledger plus operator dashboard; continuity is a structured resume packet system. Anything outside those two sentences is suspect until it proves why it belongs.
