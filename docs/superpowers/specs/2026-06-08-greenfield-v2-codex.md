# 1. Thesis / Organizing Principle

Rebuild `fulcra-coord` as a small durable coordination kernel, not as a bundle of runtime tricks. The product is cross-platform agent coordination over Fulcra Files: independent agents can discover work, claim work, hand work off, request adversarial review, deliver verdicts, and let the operator see what is alive, blocked, or drifting, with no broker, no shared process, no SSH mesh, and no assumption that two agents can call each other directly.

The organizing principle should be: immutable intent/events first, projections second, runtime adapters last. Fulcra Files is the only shared store, so the core must treat every runtime as unreliable, intermittently awake, and possibly writing through stale views. Platform lifecycle hooks, scheduled jobs, browser facades, digest emitters, and annotations are all useful, but none of them should own the task truth.

This keeps the proven core:

- Durable task/directive lifecycle on a shared file bus.
- Cheap materialized views for inbox/resume/status.
- Presence and liveness so work routes to agents that are actually around.
- Forge-agnostic review handshake: `request-review` and `review-done` are bus facts, not GitHub UI facts.
- Cross-agent patterns like blind parallel design, directed handoff, author/reviewer separation, and review fixes committed by a different identity.

It also names the scope creep accurately. Annotations, digest, and health are not mistakes; together they are the operator visibility product. The ChatGPT facade is not just instructions; it is a real HTTP service adapter. `fulcra-continuity` is not currently an integrated product; the code has a separate package while coord reimplements enough of its schema in a stdlib bridge. The rebuild should make these boundaries explicit instead of leaving them as accidental imports, duplicated schemas, and side effects buried in core commands.

# 2. Substrate + Data Model (Concurrency Under No-CAS Fulcra Files)

The no-CAS constraint should drive the entire data model. Do not design around "update the task JSON safely"; design around "append facts that can be merged deterministically." Mutable task bodies and materialized views can exist, but only as caches or convenience snapshots. They must never be the only source of truth for a transition.

Use record families under `/coordination`, each made of immutable event shards plus optional latest snapshots:

- `events/<record-family>/<event-id>.json`: immutable facts. Event IDs include actor, monotonic-ish timestamp, random suffix, and idempotency key.
- `tasks/<task-id>.json`: latest task snapshot cache, rebuilt from task events.
- `directives/<directive-id>.json`: first-class directed work, not "a task with assignee plus policy." A directive may reference a task, artifact, or operator ask.
- `reviews/<artifact-id>/...`: `ReviewArtifact`, `ReviewRequest`, `ReviewDecision`, and `ReviewFix` records.
- `presence/<agent-slug>.json`: latest lease cache plus lease events, with expiry derived from runtime capability and heartbeat cadence.
- `continuity/checkpoint-refs/<task-id>.json`: references to checkpoints, not embedded checkpoint payloads.
- `visibility/health/*.json`, `visibility/digests/*.json`, `visibility/annotations/*.json`: reporting outputs, not task truth.
- `views/*.json`: materialized query products: inboxes, summaries, needs-me, agents, active reviews, recent events.

Every command writes a small event first, then best-effort snapshots/views. If the snapshot upload fails but the event exists, reconcile can recover. If the view write loses a concurrent update, reconcile can recover. If two agents claim the same directive, the reducer compares events and applies deterministic conflict rules, then emits a `ConflictObserved` event or marks one claim superseded.

The reducer is the kernel. It consumes events, validates transitions, and produces projections. Its rules should be boring and testable:

- Event IDs are globally unique; duplicate idempotency keys from the same actor collapse.
- Terminal task states win over nonterminal updates unless a later explicit reopen event exists.
- Claim conflicts are resolved by task priority, directive routing, actor liveness at claim time, and event timestamp only as the last tie-breaker.
- Views are caches. A missing or malformed view is degraded service, not data loss.
- Snapshots are hints. A malformed task body cannot crash reconcile; it surfaces as health debt.
- Reads prefer views for speed but can fall back to event/snapshot rebuild when correctness matters.

This model keeps the current operational strength, but removes the write-path anxiety caused by aggregate summaries, per-task files, caches, and views all acting partly authoritative. The single truth becomes the event family plus reducer version.

# 3. Package / Layer Architecture Including Visibility and Continuity

Build the repo around import boundaries that match product boundaries.

`fulcra-coord-core`

- Owns schemas for task, directive, review, presence lease, event envelope, reducer state, and projection interfaces.
- Owns transition validation, merge/reconcile, query projection generation, and review routing.
- Depends only on stdlib plus the minimal Fulcra Files client interface.
- Does not import annotations, digest, FastAPI, platform adapters, GitHub, browser code, launchd, cron, or `fulcra-continuity` implementation details.

`fulcra-coord-files`

- Owns Fulcra Files transport, timeout behavior, list/download/upload/delete normalization, local cache policy, and degraded-mode reads.
- Exposes a simple object-store API to core.
- Has the no-CAS contract documented in types and tests.

`fulcra-coord-runtime`

- Owns adapter installers and runtime contracts for Claude Code, Codex, OpenClaw, Hermes, ChatGPT/custom GPT, CI, and generic ephemeral cloud agents.
- Provides `RuntimeCapability` records and `install` / `doctor` / `self-incorporate` entrypoints.
- Can call core commands but cannot mutate task JSON directly.

`fulcra-coord-review`

- Can be a core submodule or package, but the review protocol is first-class.
- Owns artifact identity normalization, request routing, reviewer liveness ranking, reviewer-fix signoff state, and `review-done`.
- Must remain forge-agnostic. GitHub PRs are one artifact kind, not the protocol.

`fulcra-coord-visibility`

- This is a first-class product layer, not cleanup code.
- Owns operator-facing health, digest, annotations, timelines, `needs-me`, attention summaries, dashboards, and reporting exports.
- Reads core projections and emits visibility records. It may write health/report events, but it must not change task lifecycle except through public core commands.
- Contains annotation transports, digest scheduling, dashboards, and HTTP/reporting integrations.

`fulcra-continuity-core`

- Owns the checkpoint schema, checkpoint references, portable artifact metadata, and lookup contracts.
- Coord should depend on this tiny schema package or shared schema module rather than reimplementing a parallel stdlib bridge.
- Continuity remains a boundary-checkpoint product, not a log of every coord event. It writes at compaction, handoff, overnight idle, explicit "done for now", and platform-specific session boundaries.

`fulcra-chatgpt-facade`

- Owns the real HTTP/OpenAPI service for ChatGPT/custom GPT surfaces.
- Translates HTTP calls into coord core commands and visibility reports.
- Does not import reducer internals and does not become a second coordination protocol.

This split makes operator visibility first-class without letting visibility concerns retangle the core. The core produces facts and projections; visibility turns them into human-readable, annotated, scheduled, and dashboarded signals.

# 4. Cross-Platform Runtime Contract

Every participating agent must publish a `RuntimeCapability` and pass a runtime contract check before it is considered live. The contract should include:

- `startup_context`: can inject bus state at session start.
- `pre_context_loss`: can checkpoint before compaction or summarization.
- `session_end`: can park or checkpoint at true session end.
- `turn_stop`: fires every model turn, not necessarily session end.
- `resident_process`: can run a daemon or gateway.
- `native_scheduler`: can wake itself on a schedule without host cron.
- `host_scheduler_required`: needs launchd, cron, systemd timer, CI, or an external automation.
- `out_of_band_delivery`: can notify an already-open session.
- `self_update_mode`: none, prompt, pinned, signed, or managed.
- `max_silence`: expected heartbeat/listener interval before liveness degrades.

Reliable listening is two separate promises: the agent notices directed work eventually, and the currently active session receives enough context to act. Some runtimes can do both natively; others can only satisfy the first promise with a host scheduler and the second promise on next session start.

Claude Code

Claude Code has documented lifecycle hooks. Official docs say `SessionStart` runs when a new or resumed session starts and can add context; `PreCompact` and `Stop` are lifecycle events in the same hook system. Source: https://code.claude.com/docs/en/hooks. Claude also now has cloud Routines: Anthropic describes routines as Claude Code automations configured with prompt/repo/connectors and run on a schedule, API call, or event, on Claude Code web infrastructure, so they do not depend on the laptop being open. Sources: https://claude.com/blog/introducing-routines-in-claude-code and https://platform.claude.com/docs/en/api/claude-code/routines-fire. The Claude Help Center also describes `/loop` as a bundled local scheduling command. Source: https://support.claude.com/en/articles/14554000-claude-code-power-user-tips.

Contract choice: Claude Code gets full adapter support. Use `SessionStart` for presence, inbox/resume, version self-incorporation prompt, and context injection. Use `PreCompact` for continuity checkpoints. Use true stop/session-end hooks only for parking when the platform event is actually a session boundary. Use cloud Routines or `/schedule` for reliable unattended listening when available; otherwise use `install-listener` over launchd/cron. `/loop` is useful for short local loops but should not be the long-term durability layer because it depends on the local running session.

Codex

The local codebase already encodes the important distinction: Codex hooks provide `SessionStart` and `PreCompact`, but coord deliberately does not use `Stop` because Codex `Stop` is turn-scoped and would park active tasks after every assistant turn. The Codex adapter docs also state an already-open Codex Desktop thread cannot be injected into by the listener; a Codex app automation or manual poll is needed for in-thread surfacing. Public OpenAI/Codex material is thinner than the local adapter evidence; the OpenAI Codex help center covers the CLI generally (https://help.openai.com/en/articles/11096431), while upstream Codex issue discussions show hook support exists and has changed over time, including SessionStart/PreCompact and Stop semantics (for example https://github.com/openai/codex/issues/2109).

Contract choice: Codex is not a resident listener. Use `SessionStart` for self-incorporation, presence, and inbox/resume. Use `PreCompact` for checkpoints. Never use turn `Stop` for parking. Reliable listening requires an external wake: launchd/cron/systemd running `fulcra-coord notify-inbox`, a Codex Desktop automation heartbeat in an active thread, or host cron invoking `codex exec` with a bounded poll/review prompt. The runtime contract must say "host scheduler required" unless a specific Codex environment provides a managed automation.

OpenClaw

OpenClaw has a gateway scheduler. Official docs describe cron as built into the Gateway, with persisted jobs under `~/.openclaw/cron/jobs.json`, main-session jobs that enqueue system events for the next heartbeat, isolated sessions that run dedicated agent turns, and wakeups that can request "wake now." Source: https://docs.openclaw.ai/cron. The current repo also has both file-hook and plugin integrations for session start, before compaction, session end, boot, heartbeat prompts, and gateway shutdown.

Contract choice: OpenClaw is native wake-capable. Use the persistent gateway plus cron/heartbeat as the durable listener. Use plugin lifecycle hooks for deterministic session start/end and compaction checkpointing. `install-openclaw --with-heartbeat --with-listener` should become one runtime installation profile, not an optional pile of commands.

NousResearch Hermes

Hermes is not merely an ephemeral sandbox. Official Hermes docs list `cronjob` as a built-in automation/delivery tool, and cron jobs can schedule one-shot or recurring tasks, run in fresh agent sessions, deliver to chat/files/platform targets, and run in no-agent mode for scheduled scripts. Sources: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/ and https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/. The local `fulcra-media` runtime taxonomy also says Hermes has a first-class heartbeat/cron path, while traditional CLI agents use host cron/launchd/systemd.

Contract choice: Hermes is native scheduler-capable. Treat Hermes as able to run recurring bus imports/listeners through its `cronjob` tool, with fresh sessions and optional no-agent checks. Continuity is mandatory when a Hermes run is sandboxed or disposable: it should read checkpoint refs, act, write portable artifacts, and checkpoint before exit.

Ephemeral sandboxes, CI, and generic cloud agents

These are fire-and-forget unless an external orchestrator says otherwise. They should not be considered live after their lease expires. They can run startup poll, claim a bounded task, write events, write a checkpoint, and exit. Listening is the orchestrator's job: CI schedule, cloud cron, queue trigger, or a platform-specific routine. They must publish `host_scheduler_required=true` unless the platform has a real scheduler.

ChatGPT / custom GPT

ChatGPT has no deterministic lifecycle hooks. The real integration should be the facade: an HTTP/OpenAPI service with Fulcra credentials outboard from the model. The custom GPT can report, query, and request actions through the facade, but the facade must translate into core commands. Reliable listening requires an external scheduler hitting the facade or user action; it is not a resident agent.

Version self-incorporation

Every runtime startup must run `doctor` plus `self-incorporate`. That does not mean blind auto-update. The supply-chain policy is:

- The bus publishes a version manifest with schema version, package version, release commit, content hash, compatibility window, and update channel.
- Agents may auto-update only from pinned, signed, or hash-verified sources and only within the compatible schema window.
- Major schema changes require operator approval or a two-phase rollout event.
- Runtime adapters report installed version and reducer version in presence.
- If an agent cannot update safely, it marks itself degraded and refuses to perform writes that require a newer reducer.
- No startup script may run arbitrary `git pull && install` from an unverified branch just because the bus says so.

# 5. What to Keep Unchanged

Keep Fulcra Files as the only shared substrate. The no-broker property is the system's distinctive constraint and its portability win.

Keep the CLI-first, runtime-agnostic command surface. An agent with shell, Fulcra credentials, and no adapter should still coordinate.

Keep materialized views. They are the right read model for inbox/resume/status, as long as they are treated as caches and can be rebuilt.

Keep the task lifecycle vocabulary and most status semantics: proposed, active, waiting/blocked, done, abandoned, plus priority and workstream. Agents already reason with this grammar.

Keep presence leases and liveness-aware reviewer routing. Cross-agent work stalls when assigned to a dead identity; routing to live capable reviewers is core behavior.

Keep `request-review` and `review-done` as bus-native facts. The review control is independent review by another agent identity, not a GitHub button.

Keep the operator plate: `needs-me`, `block --on-user`, and startup banners that put human-blocked items in front of the operator.

Keep reconcile as the safety net. It should rebuild projections, detect malformed records, sweep stale leases/tasks, and report health without being able to corrupt truth.

Keep boundary checkpointing. Continuity should happen at context-loss and handoff boundaries, not after every bus update.

Keep fail-safe behavior. A malformed task, missing title, dropped view, broken annotation transport, or failed digest must degrade visibility, not crash the coordination loop.

# 6. How to Structurally Prevent Scope Creep

Make the dependency graph enforce the design.

Hard import rules:

- `fulcra-coord-core` cannot import runtime adapters, visibility, FastAPI, annotations, digest, GitHub, OpenClaw, Codex, Claude, or concrete schedulers.
- Visibility can import core read models and public commands, but core cannot import visibility.
- Runtime adapters can call public CLI/API commands, not reducer internals.
- ChatGPT facade can call public commands, not mutate files directly.
- Coord can reference continuity through `fulcra-continuity-core` schemas only; it cannot carry a private duplicate schema.

Add package-boundary fitness tests. The current code already has a test pattern worth generalizing: assert import direction, dependency allowlists, adapter isolation, and "no reporting dependencies in core." These tests should fail the build when someone adds an innocent import that turns into the next product knot.

Classify every new feature before implementation:

- Core coordination: task/directive/review/presence/event/reducer/projection.
- Runtime adapter: lifecycle hooks, schedulers, installers, platform docs.
- Visibility: health, digest, annotation, timeline, dashboard, operator reports.
- Continuity: checkpoint schema, refs, portable handoff artifacts.
- Facade: HTTP/custom GPT/third-party ingress.
- Tooling: demos, migration, docs, tests.

If a feature does not fit one bucket, split it. If it needs to write both task truth and operator reports, the task write goes through core and the report goes through visibility. If it needs both lifecycle hooks and checkpoint schema, the runtime layer triggers continuity; it does not own continuity.

Keep reducers pure. Business rules live in reducers and are tested from event sequences. Transports, schedulers, and facades produce commands/events; they do not decide hidden task state.

Keep projections rebuildable. Any view or digest must be disposable. The test should be: delete views, run reconcile from event/snapshot families, and get the same inbox/status/review state.

Keep runtime wake behavior explicit. A runtime is not "integrated" until it declares how it wakes, how it checkpoints before context loss, how it notices directed work while idle, how it self-incorporates versions, and what it cannot do. Unsupported capabilities should be false in `RuntimeCapability`, not hand-waved in prose.

Keep supply-chain checks mandatory. Self-incorporation is valuable because long-lived agents drift. It is dangerous because startup hooks are privileged code execution. The design should make safe update policy a schema field and test fixture, not an operator folk rule.

Finally, keep operator visibility named as product, not as creep. The creep was that reporting code leaked into coordination mechanics. The fix is not to delete health/digest/annotations; it is to put them behind a visibility boundary with its own schema, tests, and failure isolation.
