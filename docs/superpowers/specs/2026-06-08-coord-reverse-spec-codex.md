# Fulcra Coord Reverse-Engineered Product Spec - Codex

## 1. One-line product definition

`fulcra-coord` is a stdlib-only coordination CLI that lets independent agents coordinate durable tasks, review handoffs, presence, blockers, and resume state through Fulcra Files as the shared bus.

## 2. Target user + core jobs-to-be-done

The primary user is a human or agent operator running multiple autonomous coding/operations agents across machines, runtimes, and sessions. The core job is to keep work visible, routable, recoverable, and reviewable when the agents do not share memory, a checkout, a live chat thread, or one long-running process.

The secondary user is an agent runtime integrator. Their job is to install lifecycle hooks, heartbeat/listener jobs, and runtime-specific instructions so the coordination bus is surfaced automatically at session start, compaction, idle polling, and scheduled reconciliation.

The reviewer/operator job is also first-class: see what is active, what is stale, what is blocked on the human, what reviews are missing, who is live enough to receive work, and what evidence closed a task.

## 3. Capability surface - grouped list of what it does, from the CLI/API

Task lifecycle:

- `start` creates proposed tasks with workstream, agent, priority, summary, next action, and surface.
- `update` changes summary, next action, blocker text, and status among active/waiting/blocked/abandoned.
- `block` parks work on an external blocker or on the human via `--on-user`, `needs:human`, `not_before`, and `due`.
- `pause` sets work to waiting, and can write a continuity snapshot.
- `snapshot` writes a Fulcra Continuity-compatible checkpoint without changing task state.
- `done` requires evidence and verification level before closing.
- `abandon` closes work as intentionally not continuing.

Directive and inbox routing:

- `tell` creates a proposed directive assigned to one agent, or routes by declared capability.
- `broadcast` creates wildcard directives visible to all agents and acknowledged per agent.
- `assign` redirects an existing task to another assignee.
- `inbox` reads open directives for an agent and can ack without claiming.
- `notify-inbox` polls directed work and writes a local surface/notification for later session pickup.

Review workflow:

- `request-review <artifact>` routes an opaque artifact to a live/idle reviewer using presence, capability declaration, canonical reviewer preference, route events, reroute limits, and human escalation.
- `review-done <artifact> --verdict approve|changes` posts the review outcome back to the author as a bus directive rather than relying on forge-only approvals.
- The core package enforces forge-agnostic review semantics in tests: the coordination layer must not call `gh` or a specific forge API.

Presence and situational awareness:

- `connect`, `workstream`, and `presence` record and display who is alive, what workstreams they are on, their summary, and declared capabilities such as review.
- `status`, `agents`, `needs-me`, and `resume` render active work, agent-owned work, human-blocked work, owed work, and restart context.
- `health` records and reads reconcile freshness, task load counts, view refresh results, listener timestamps, retention state, and bus task counts.
- `digest` and `install-digest` write a twice-daily operator timeline digest with blocked-on-you, upcoming, per-agent activity, stale work, and infrastructure health.

Bus storage, consistency, and maintenance:

- Task bodies live under `/coordination/tasks`, with materialized views for active/next/recently-done/search/workstreams/agents/inbox.
- Writes use optimistic concurrency, merge attempts, bounded event logs, local cache, operation markers, and best-effort lifecycle annotations.
- `reconcile` repairs views, uploads views concurrently, detects stale claims, writes health, sweeps review reroutes, expires old broadcasts, runs retention, prunes stale presence/health/digest markers, and bounds continuity archives.
- `search` queries hot task records and optionally cold archive shards.
- `restore` moves archived tasks back into the hot path.

Local configuration and diagnostics:

- `identity`, `human`, `annotations`, `session-task`, `doctor`, and `capabilities` manage per-cwd agent identity, the human handle, Agent Tasks annotation mode, session task pointers, setup diagnostics, and version/capability probing.
- Environment knobs tune remote root, Fulcra CLI command, timeouts, staleness, inbox age, broadcast expiry, retention windows, presence grace, review reroute/stall thresholds, notification webhook format/timeouts, continuity retention, annotations, and session identity.

Runtime adapters and installers:

- `install-claude-code`, `install-codex`, `ensure-codex-watch`, `install-openclaw`, `install-heartbeat`, `install-listener`, `install-shim`, and adapter docs/plugins wire the same bus behavior into Claude Code, Codex, OpenClaw, generic/cloud agents, and ChatGPT facade usage.
- ChatGPT facade code exposes an HTTP/OpenAPI adapter that can create/update tasks and read status with token validation.

Continuity bridge:

- `fulcra_coord.continuity` writes checkpoint JSON with the same schema version and top-level shape as `fulcra-continuity` without importing the standalone package.
- `resume --with-continuity` can include latest same-agent continuity checkpoint summaries for active/waiting tasks.

## 4. Scope classification - tag every capability CORE / SUPPORTING / PERIPHERAL-or-creep with a one-line rationale

CORE:

- Task lifecycle (`start`, `update`, `block`, `pause`, `snapshot`, `done`, `abandon`) - this is the minimum durable ledger needed for multi-agent work to survive restarts and handoffs.
- Directives and inbox (`tell`, `broadcast`, `assign`, `inbox`) - the product is coordination, so agent-to-agent work routing is central.
- Materialized views (`status`, `agents`, `resume`, `needs-me`, view builders) - without fast shared read surfaces, the bus becomes write-only history instead of operational state.
- Optimistic writes, merge/reconcile, operation markers, and cache - cross-machine coordination needs conflict tolerance and repair; otherwise concurrent agents silently lose work.
- Identity and human-handle management - agent and human addressing are primitives, not preferences.
- Presence and `connect --can-review` - live reviewer/work recipient selection is now part of the routing contract.
- Review routing/verdict bus (`request-review`, `review-done`, route events, reroute sweep) - the current rules make independent review a first-class workflow, and the implementation is intentionally forge-agnostic.

SUPPORTING:

- Heartbeat/listener installers - these make the core bus usable while sessions are idle or crash-prone, but they are runtime plumbing around the bus.
- `health` - important operational monitoring for the coordination system, but not required to express work.
- `digest` and `install-digest` - useful for human situational awareness at scale; not necessary for single-task coordination.
- `doctor` and `capabilities` - essential onboarding/diagnostic affordances, but not core coordination data.
- Agent Tasks annotations - valuable timeline observability, yet explicitly best-effort and default-off/persisted config.
- Broadcast age-out, broadcast expiry, hot/cold retention, `search --archived`, and `restore` - necessary once the bus has real volume; supporting maintenance rather than the product's first-order job.
- Continuity bridge (`snapshot`, `pause --snapshot`, `resume --with-continuity`) - strongly related to handoff, but the code deliberately treats standalone continuity as independent and optional.
- Notification webhook/native desktop surfaces - improve latency and operator awareness; coordination still works through inbox/session-start surfaces without them.

PERIPHERAL-or-creep:

- Runtime-specific installers and adapter instructions for Claude Code, Codex, OpenClaw, generic cloud, and ChatGPT - valuable distribution glue, but the breadth risks making `fulcra-coord` own every runtime's lifecycle quirks.
- OpenClaw plugin source materialization - a packaging convenience that sits far from the core bus; could become maintenance-heavy if OpenClaw APIs move.
- ChatGPT facade HTTP app/OpenAPI - plausible adapter, but it is an extra service layer over a CLI package and may deserve its own adapter package once real use grows.
- Operator digest as Fulcra timeline publishing - useful, but it expands the product from coordination ledger into reporting/observability product.
- Agent Tasks annotation writer - similarly expands into timeline instrumentation and token/API transport handling.
- Extensive installer scheduling machinery (`launchd`, cron, logs dirs, load/uninstall variants) - needed for current deployment, but it is OS-integration surface area that could crowd the core if not kept as adapter/supporting.

## 5. Maturity / proven-ness read - code-level evidence of real use vs speculative surface

`fulcra-coord` looks like a real, heavily exercised product. Evidence: version is `0.12.0`; the package has a large command surface in `entry.py`; the README and SKILL files are detailed and consistent with current commands; the tests cover schema, views, CLI fake backend flows, conflict detection, merge behavior, identity, inbox, listener, installers, annotations, digest, health, retention, review routing, forge-agnostic constraints, continuity bridge, and ChatGPT facade. The test tree is about 16k lines across coord, facade, and continuity-related tests, with roughly 15.9k of those lines in coord/facade tests.

The product also shows scars from real live-bus use: many comments name concrete bug classes such as directive loss, view rebuild failures, stale timestamp comparisons, identity clobbering, upload partials, broken list-file parsing, lost lifecycle annotations on partial view writes, dry-run side effects, and shared-account GitHub review limitations. That is strong evidence that `fulcra-coord` has been used against a live coordination bus and hardened by review.

The highest-risk maturity issue is scope breadth, not absence of code. The core bus is proven, but the package has accumulated operators' digest publishing, annotations, notifications, scheduler installers, multiple runtime adapters, OpenClaw plugin source, ChatGPT facade code, continuity interop, and review workflow policy. Those are useful, but the product identity can blur unless the core remains "durable coordination bus" and adapter/reporting surfaces remain optional edges.

The review workflow is mature in policy intent and has tests, but it is socially dependent: bus verdicts and agent identity matter more than GitHub approvals because agents may share the same GitHub account. That is honest and pragmatic, but it means correctness depends on agents following the bus protocol, not only on code.

The continuity bridge is deliberately by-convention and stdlib-only. It has parity tests with `fulcra-continuity`, but cross-agent discovery is limited: same-agent latest lookup is built in, while cross-agent pickup depends on the producer providing checkpoint path/JSON. That is a real constraint, not a finished universal resume layer.

## 6. Open questions about product identity

- Is `fulcra-coord` primarily a coordination protocol/CLI, or is it becoming a full operator platform with digest, annotations, runtime installers, notifications, and adapters?
- Which adapter surfaces should stay in the core package, and which should split into separate adapter packages or plugin bundles?
- Should review routing remain inside coord as a core workflow, or become a specialized policy module on top of generic capability routing?
- What is the minimum stable public API beyond the CLI: task schema, view schema, route events, continuity paths, or Python functions?
- How much of the product's correctness is expected to be enforceable in code versus carried by agent instructions and bus conventions?
- Should ChatGPT facade and OpenClaw plugin code be considered product commitments or prototypes used to validate runtime reach?
- Is continuity a supporting bridge inside coord, or should coord only store checkpoint references and leave checkpoint creation/retention/search to `fulcra-continuity`?
- What is the long-term boundary between "operator visibility" and "coordination"? `health` is close to reliability; `digest` and annotations start looking like a reporting suite.
