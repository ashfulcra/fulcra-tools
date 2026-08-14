# Collect, Coord Engine, and Bus Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Goal:** Build a clean-room implementation that interoperates with the shipped
Fulcra Agent Coordination Bus and provides the Collect plugin host without relying
on a broker, resident coordination daemon, or hidden model inference.

**Architecture:** Implement protocol rules as pure Python modules, place Fulcra
I/O behind a bounded subprocess transport, and expose deterministic CLI folds over
canonical record/file state. Build Collect as a separate Python 3.11 daemon whose
plugins execute behind declared contracts and subprocess isolation. Treat records
as signals, documents and evidence shards as durable authority, and projections as
rebuildable caches.

**Tech Stack:** Python 3.10+ for `coord-engine`; Python 3.11+, FastAPI, Pydantic,
Click, SQLite, keyring, httpx, and uvicorn for `fulcra-collect`; pytest for tests;
`fulcra-api` for Fulcra transport; Markdown/YAML-compatible OKF documents for
durable state.

**Global Constraints:**

- Implement against the design in
  `docs/superpowers/specs/2026-08-14-collect-coord-bus-rebuild-design.md`.
- Preserve unknown document fields and ignore unknown event versions rather than
  guessing.
- Never activate cursor schema 2 on the current last-writer-wins file transport.
- No secret may cross argv, stdout, logs, repo files, Bus notes, or task bodies.
- Every remote operation and aggregate fold has a positive finite deadline.
- Every task ends with focused tests and a small commit.
- The paths below are the exact target paths in the clean-room repository. Do not
  copy implementation source from the reference checkout; use it only as an
  interoperability oracle and fixture producer.

---

## Task 1: Establish Package Skeletons and Shared Test Fixtures

**Files:**
- Create: `pyproject.toml`
- Create: `packages/coord-engine/pyproject.toml`
- Create: `packages/coord-engine/coord_engine/__init__.py`
- Create: `packages/collect/pyproject.toml`
- Create: `packages/collect/fulcra_collect/__init__.py`
- Create: `tests/fixtures/bus-v1-event.json`
- Create: `tests/fixtures/task-active.md`
- Create: `tests/fixtures/review-register.md`
- Create: `tests/test_fixture_contract.py`

- [ ] **Step 1: Write the failing fixture smoke test**

```python
def test_reference_fixtures_are_present_and_versioned():
    event = json.loads(Path("tests/fixtures/bus-v1-event.json").read_text())
    assert event["v"] == 1
    assert event["kind"] == "directive"
    assert "type: Task" in Path("tests/fixtures/task-active.md").read_text()
```

- [ ] **Step 2: Run the smoke test and confirm missing-file failure**

Run: `uv run pytest tests/test_fixture_contract.py -q`

Expected: FAIL with `FileNotFoundError` for the first fixture.

- [ ] **Step 3: Add minimal packages and sanitized reference fixtures**

Pin `coord-engine` to Python `>=3.10` with no runtime dependencies. Pin Collect to
Python `>=3.11` with its declared daemon dependencies. Fixtures contain no team,
agent, machine, or account-specific values.

- [ ] **Step 4: Verify package import and fixtures**

Run: `uv run pytest tests/test_fixture_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add pyproject.toml packages tests && git commit -m "build: scaffold coord and collect rebuild"`

## Task 2: Build the Bounded Fulcra Transport

**Files:**
- Create: `packages/coord-engine/coord_engine/budget.py`
- Create: `packages/coord-engine/coord_engine/transport.py`
- Create: `packages/coord-engine/tests/test_budget.py`
- Create: `packages/coord-engine/tests/test_transport.py`

- [ ] **Step 1: Write failing tests for finite controls and classified reads**

Cover positive finite env parsing, malformed fallback, subprocess timeout, process
group termination, `missing` versus `error`, JSON envelope state inspection, and
write verification failure.

```python
def test_transport_error_is_not_missing(fake_runner):
    fake_runner.returns(rc=1, stdout='{"state":"INVALID","error_code":"AUTH"}')
    assert Transport(fake_runner).read_classified("team/x/task/a.md") == (None, "error")
```

- [ ] **Step 2: Confirm the tests fail on missing modules**

Run: `uv run pytest packages/coord-engine/tests/test_budget.py packages/coord-engine/tests/test_transport.py -q`

Expected: FAIL during import.

- [ ] **Step 3: Implement `Deadline`, `env_float`, and `FulcraTransport`**

Expose `read_classified(path) -> tuple[str | None, Literal["data","missing","error"]]`,
`write_verified(path, content) -> bool`, `list(path)`,
`get_records(data_type, start, end, api_version)`, and
`data_updates(start, end)`. Invoke `fulcra-api` with argv arrays, no shell, a
remaining-operation timeout, and bounded captured output.

- [ ] **Step 4: Verify transport behavior**

Run: `uv run pytest packages/coord-engine/tests/test_budget.py packages/coord-engine/tests/test_transport.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): add bounded classified transport"`

## Task 3: Implement Channel Authority and Event Envelopes

**Files:**
- Create: `packages/coord-engine/coord_engine/records.py`
- Create: `packages/coord-engine/tests/test_records.py`
- Create: `packages/coord-engine/tests/test_records_authority.py`

- [ ] **Step 1: Write failing envelope round-trip tests**

Test compact stable JSON, recognized kinds, `to=all`, optional pointers, writer
stamps, unknown version rejection, malformed control-note rejection, sender
attribution, duplicate record ids, and deterministic ordering.

- [ ] **Step 2: Write failing authority tests**

Test legacy readable config, complete versioned config, partial authority INVALID,
minimum reader/writer floors, environment transport override without persistence,
and default API version `v1alpha1`.

- [ ] **Step 3: Implement pure record and authority functions**

Expose `build_payload`, `parse_payload`, `events_for`, `sender_of`, `load_config`,
`config_path`, `engine_stamp`, and `observed_version_warnings`. Keep payload
version, protocol version, and cursor schema version separate.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest packages/coord-engine/tests/test_records.py packages/coord-engine/tests/test_records_authority.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): define bus v1 event authority"`

## Task 4: Implement Cursor Schema 1 and Queue Failure Semantics

**Files:**
- Create: `packages/coord-engine/coord_engine/cursor.py`
- Create: `packages/coord-engine/coord_engine/queue.py`
- Create: `packages/coord-engine/tests/test_cursor.py`
- Create: `packages/coord-engine/tests/test_queue.py`

- [ ] **Step 1: Write failing queue tests**

Cover own-plus-broadcast filtering, successful advancement, peek without advancement,
UNKNOWN read without advancement, poison reporting, cursor corruption refusal,
foreign consume audit requirement, and duplicate retry handling.

- [ ] **Step 2: Write a failing schema-2 gate test**

```python
def test_schema_two_refuses_without_proven_cas():
    with pytest.raises(CursorGateError, match="CAS"):
        activate_cursor_schema(2, transport_capabilities={"cas": False})
```

- [ ] **Step 3: Implement queue and cursor functions**

Expose `read_cursor`, `write_cursor_verified`, `read_queue`, `peek_queue`, and
`consume_foreign_queue`. Cursor updates happen only after a DATA/CLEAR read has
been fully classified and filtered.

- [ ] **Step 4: Verify queue semantics**

Run: `uv run pytest packages/coord-engine/tests/test_cursor.py packages/coord-engine/tests/test_queue.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): add fail-closed queue cursors"`

## Task 5: Implement OKF Parsing and the Task State Machine

**Files:**
- Create: `packages/coord-engine/coord_engine/okf.py`
- Create: `packages/coord-engine/coord_engine/model.py`
- Create: `packages/coord-engine/coord_engine/tasks.py`
- Create: `packages/coord-engine/tests/test_okf.py`
- Create: `packages/coord-engine/tests/test_tasks.py`

- [ ] **Step 1: Write failing parser and preservation tests**

Test typed frontmatter, scalar/list normalization, unknown-field preservation, and
refusal of malformed Task documents.

- [ ] **Step 2: Write failing lifecycle tests**

Cover every legal and illegal transition, terminal immutability, evidence-required
completion, evidence-required terminal creation, block `blocked_on` plus `unlock`,
supersession of live copies, and collision-safe agent shard keys.

- [ ] **Step 3: Implement pure task functions**

Expose `new_task_doc`, `apply_update`, `apply_answer`, `slugify`, `agent_key`,
`row_from_frontmatter`, and deterministic `sort_rows`. Preserve original Markdown
body history by appending timestamped transitions.

- [ ] **Step 4: Run lifecycle tests**

Run: `uv run pytest packages/coord-engine/tests/test_okf.py packages/coord-engine/tests/test_tasks.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): add durable task lifecycle"`

## Task 6: Add Durable Messaging, Obligations, and Operator Asks

**Files:**
- Create: `packages/coord-engine/coord_engine/directives.py`
- Create: `packages/coord-engine/coord_engine/responses.py`
- Create: `packages/coord-engine/coord_engine/operator.py`
- Create: `packages/coord-engine/tests/test_directives.py`
- Create: `packages/coord-engine/tests/test_operator.py`

- [ ] **Step 1: Write failing durable-first tests**

Assert `tell` writes and verifies the task before emitting an event, failed task
write emits nothing, bare directive warns, FYI is born terminal with explicit mode,
and only the assignee can close an obligation.

- [ ] **Step 2: Write failing operator-loop tests**

Assert asks are self-contained, not-before items remain upcoming, broadcasts do
not appear as operator blockers, and an answer records text and returns ownership
in one update.

- [ ] **Step 3: Implement messaging services**

Keep orchestration thin around the pure task and event modules. Return structured
results with `ptr`, slug, event record id, and verification state.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest packages/coord-engine/tests/test_directives.py packages/coord-engine/tests/test_operator.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): close durable messaging loops"`

## Task 7: Implement Exact-Head Review Evidence

**Files:**
- Create: `packages/coord-engine/coord_engine/review.py`
- Create: `packages/coord-engine/coord_engine/review_store.py`
- Create: `packages/coord-engine/tests/test_review.py`
- Create: `packages/coord-engine/tests/test_review_store.py`

- [ ] **Step 1: Write failing pure-fold tests**

Cover full SHA-1/SHA-256 validation, verdict vocabulary, CHANGES dominance,
required-reviewer gating, newest-per-reviewer selection, deterministic ties, and
role-based reviewer resolution.

- [ ] **Step 2: Write failing evidence-race tests**

Prove two append-only verdict writes preserve both files, stale cache digests do
not approve, mutable legacy names disable cache short-circuit, wrong-head verdicts
are ignored, and MERGED is explicit evidence.

- [ ] **Step 3: Implement register, shard, and settle functions**

Expose `normalize_head`, `verdict_filename`, `fold_newest_per_reviewer`, `tally`,
`evidence_digest`, `settle_shortcircuit`, `request_review`, `write_verdict`, and
`close_review`. Reject changed artifact references or required sets on one slug.

- [ ] **Step 4: Verify review behavior**

Run: `uv run pytest packages/coord-engine/tests/test_review.py packages/coord-engine/tests/test_review_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): add append-only exact-head review"`

## Task 8: Implement Presence, Engagement, Roles, and Routing

**Files:**
- Create: `packages/coord-engine/coord_engine/presence.py`
- Create: `packages/coord-engine/coord_engine/roles.py`
- Create: `packages/coord-engine/coord_engine/routing.py`
- Create: `packages/coord-engine/tests/test_presence.py`
- Create: `packages/coord-engine/tests/test_roles.py`
- Create: `packages/coord-engine/tests/test_routing.py`

- [ ] **Step 1: Write failing liveness tests**

Cover live/idle/stale grace, malformed engagement, future timestamps, observed
cadence, batch activity, and engine stamps.

- [ ] **Step 2: Write failing lease tests**

Cover `HELD`, `LAPSED`, `VACANT`, `CONTESTED`, `DORMANT`, and `UNKNOWN`; exclusive
versus shared policy; nonce mismatch; and degraded listing that must not become
vacancy.

- [ ] **Step 3: Write failing capability-routing tests**

Route to a fresh capable role holder, refuse on UNKNOWN role state, and use a
deterministic tie-breaker across equally eligible agents.

- [ ] **Step 4: Implement and verify**

Run: `uv run pytest packages/coord-engine/tests/test_presence.py packages/coord-engine/tests/test_roles.py packages/coord-engine/tests/test_routing.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): add liveness-aware role routing"`

## Task 9: Implement Structured Continuity

**Files:**
- Create: `packages/coord-engine/coord_engine/continuity.py`
- Create: `packages/coord-engine/coord_engine/parking.py`
- Create: `packages/coord-engine/tests/test_continuity.py`
- Create: `packages/coord-engine/tests/test_parking.py`

- [ ] **Step 1: Write failing snapshot tests**

Cover normalized lists, deterministic latest selection, malformed/future timestamp
rejection, tie-breaking, age formatting, and deterministic resume briefs.

- [ ] **Step 2: Write failing park/resume tests**

Require an expiry or operator-lift marker, distinguish snapshot from park, and
verify inherited state against the store before resuming.

- [ ] **Step 3: Implement continuity and parking services**

Use schema `coord.teams.continuity.v1`; emit the optional checkpoint timeline
record only after the durable snapshot is verified.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest packages/coord-engine/tests/test_continuity.py packages/coord-engine/tests/test_parking.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): add verifiable session continuity"`

## Task 10: Implement Projections, Feed Overlay, and Degraded Output

**Files:**
- Create: `packages/coord-engine/coord_engine/projection.py`
- Create: `packages/coord-engine/coord_engine/reconcile.py`
- Create: `packages/coord-engine/coord_engine/query.py`
- Create: `packages/coord-engine/coord_engine/output.py`
- Create: `packages/coord-engine/tests/test_projection.py`
- Create: `packages/coord-engine/tests/test_reconcile.py`
- Create: `packages/coord-engine/tests/test_output_contract.py`

- [ ] **Step 1: Write failing projection eligibility tests**

Reject unknown schema, invalid/future stamp, incomplete build, and stale unproven
view. Accept a complete projection only with a positive feed window and overlay
changed canonical slugs.

- [ ] **Step 2: Write failing bounded-repair tests**

Assert scan budget stops work, reports scanned/total/reason, carries remainder,
and never emits a clean partial result. Prove absence of a janitor changes latency
but not canonical answers.

- [ ] **Step 3: Write strict-consumer tests first**

Parse stdout after every byte truncation point. Require JSON-only stdout,
action-carrying fields, in-envelope degradation, bounded rows/text, and nonzero rc
for documented UNKNOWN states.

- [ ] **Step 4: Implement folds and output envelope**

Build board, needs-me, inbox, obligations, threads, briefing, asks, health, and
review projections from shared primitives. Keep one canonical degraded-row builder.

- [ ] **Step 5: Run and commit**

Run: `uv run pytest packages/coord-engine/tests/test_projection.py packages/coord-engine/tests/test_reconcile.py packages/coord-engine/tests/test_output_contract.py -q`

Expected: PASS.

Run: `git add packages/coord-engine && git commit -m "feat(coord): add feed-first bounded folds"`

## Task 11: Expose the Coord CLI and Acceptance Pair

**Files:**
- Create: `packages/coord-engine/coord_engine/cli.py`
- Create: `packages/coord-engine/coord_engine/acceptance.py`
- Create: `packages/coord-engine/tests/test_cli.py`
- Create: `packages/coord-engine/tests/test_acceptance.py`

- [ ] **Step 1: Write failing parser contract tests**

Require command groups for queue, task, messaging, response, review, roles,
presence, operator asks, continuity, reconcile/views, health, forge evidence,
annotations, stash, wake/router, capacity routing, and acceptance.

- [ ] **Step 2: Write failing two-agent acceptance test**

With a fake Fulcra transport, prove A sends a durable directive, B reads and
responds, A observes closure, B snapshots and parks, and a fresh B resumes.

- [ ] **Step 3: Implement thin command handlers**

Handlers parse arguments, call service functions, and render through `output.py`.
They do not duplicate state machine or fold logic.

- [ ] **Step 4: Verify full Coord unit suite**

Run: `uv run pytest packages/coord-engine/tests -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/coord-engine && git commit -m "feat(coord): expose interoperable coord cli"`

## Task 12: Define the Collect Plugin and Configuration Contracts

**Files:**
- Create: `packages/collect/fulcra_collect/plugin.py`
- Create: `packages/collect/fulcra_collect/registry.py`
- Create: `packages/collect/fulcra_collect/config.py`
- Create: `packages/collect/fulcra_collect/credentials.py`
- Create: `packages/collect/tests/test_plugin.py`
- Create: `packages/collect/tests/test_registry.py`
- Create: `packages/collect/tests/test_config.py`
- Create: `packages/collect/tests/test_credentials.py`

- [ ] **Step 1: Write failing plugin validation tests**

Require independent execution kind and collect mode, interval only for scheduled
plugins, typed settings, scoped credentials, setup steps, and optional health,
permission, OAuth, and freshness callbacks.

- [ ] **Step 2: Write failing registry isolation tests**

One load error, wrong object, or duplicate id is reported and excluded without
preventing valid plugins. Test both Python entry points and frozen manifest input.

- [ ] **Step 3: Write failing secret-boundary tests**

`set_setting` refuses credential keys, unknown keys, invalid enums/ports, and
unknown plugins. Keychain writes select plugin or user scope and never echo values.

- [ ] **Step 4: Implement and verify**

Run: `uv run pytest packages/collect/tests/test_plugin.py packages/collect/tests/test_registry.py packages/collect/tests/test_config.py packages/collect/tests/test_credentials.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/collect && git commit -m "feat(collect): define isolated plugin contract"`

## Task 13: Build Collect State, Workers, Scheduling, and Supervision

**Files:**
- Create: `packages/collect/fulcra_collect/db.py`
- Create: `packages/collect/fulcra_collect/state.py`
- Create: `packages/collect/fulcra_collect/worker.py`
- Create: `packages/collect/fulcra_collect/runner.py`
- Create: `packages/collect/fulcra_collect/scheduler.py`
- Create: `packages/collect/fulcra_collect/supervisor.py`
- Create: `packages/collect/fulcra_collect/freshness.py`
- Create: `packages/collect/tests/test_db.py`
- Create: `packages/collect/tests/test_worker.py`
- Create: `packages/collect/tests/test_runner.py`
- Create: `packages/collect/tests/test_scheduler.py`
- Create: `packages/collect/tests/test_supervisor.py`
- Create: `packages/collect/tests/test_freshness.py`

- [ ] **Step 1: Write failing state isolation and atomicity tests**

Cover migrations, plugin namespace isolation, KV size/key limits, atomic update,
dedup claim/unclaim, parent-only outcome writes, and persisted watermarks.

- [ ] **Step 2: Write failing worker protocol tests**

Require JSON-line progress/annotation/result events, stdout quarantine for plugin
prints, secret redaction, timeout termination, optional credentials, account
fingerprint preflight, and definition revalidation behavior.

- [ ] **Step 3: Write failing scheduler/supervisor/freshness tests**

Cover due ordering, offline deferral, restart backoff, clean shutdown, accepted
versus attempted annotations, source timestamp freshness, and UNKNOWN before first
evidence.

- [ ] **Step 4: Implement and verify**

Run: `uv run pytest packages/collect/tests/test_db.py packages/collect/tests/test_worker.py packages/collect/tests/test_runner.py packages/collect/tests/test_scheduler.py packages/collect/tests/test_supervisor.py packages/collect/tests/test_freshness.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/collect && git commit -m "feat(collect): isolate plugin execution and state"`

## Task 14: Build the Collect Daemon, API, Control Socket, and CLI

**Files:**
- Create: `packages/collect/fulcra_collect/daemon.py`
- Create: `packages/collect/fulcra_collect/web.py`
- Create: `packages/collect/fulcra_collect/control.py`
- Create: `packages/collect/fulcra_collect/routes/status.py`
- Create: `packages/collect/fulcra_collect/routes/plugins.py`
- Create: `packages/collect/fulcra_collect/routes/oauth.py`
- Create: `packages/collect/fulcra_collect/routes/activity.py`
- Create: `packages/collect/fulcra_collect/cli.py`
- Create: `packages/collect/fulcra_collect/__main__.py`
- Create: `packages/collect/tests/test_daemon.py`
- Create: `packages/collect/tests/test_web.py`
- Create: `packages/collect/tests/test_control.py`
- Create: `packages/collect/tests/test_cli.py`
- Create: `packages/collect/tests/test_end_to_end.py`

- [ ] **Step 1: Write failing daemon lifecycle tests**

Require `127.0.0.1:9292`, one daemon lock, control socket cleanup, reload signaling,
tracked worker shutdown, supervised services, and state DB initialization.

- [ ] **Step 2: Write failing API and OAuth tests**

Cover status, plugin contracts, manual run, activity ring buffer, PKCE state,
callback verification, hidden credentials, file upload, and loopback-only binding.

- [ ] **Step 3: Write failing CLI offline/online behavior tests**

Config and credential writes work daemon-down; run/status require the daemon;
worker invocation stays private; command output never includes secret values.

- [ ] **Step 4: Implement and verify**

Run: `uv run pytest packages/collect/tests -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add packages/collect && git commit -m "feat(collect): add local capture daemon"`

## Task 15: Add Feature-Gated Fleet Manifest and Session-Hosted Repair

**Files:**
- Create: `docs/coord/fleet-manifest.schema.json`
- Create: `packages/coord-engine/coord_engine/fleet.py`
- Modify: `packages/coord-engine/coord_engine/transport.py`
- Modify: `packages/coord-engine/coord_engine/reconcile.py`
- Create: `packages/coord-engine/tests/test_fleet.py`
- Create: `packages/coord-engine/tests/test_no_janitor.py`

- [ ] **Step 1: Write failing zero-authority hint tests**

Any record on the directive channel triggers one canonical manifest fetch. Its note
content cannot select a pin, fence, repository, or config. Duplicate hints in one
wake coalesce.

- [ ] **Step 2: Write failing manifest and fence tests**

Accept only canonical origin plus full commit id. Invalid, foreign, abbreviated,
or unavailable manifests hold current state. Below-fence reads work and writes
refuse before transport mutation. Deny `coord-reconcile:*` writers.

- [ ] **Step 3: Write failing no-janitor equivalence tests**

Compare cold canonical folds with warm projections; answers match. Bounded
read-repair leaves visible remainder. Retention batches are session-attributed and
capped.

- [ ] **Step 4: Implement behind `COORD_FLEET_MANIFEST_ENABLED`**

Do not raise the live fleet floor in this task. Feature activation is a separately
reviewed manifest change after compatibility acceptance.

- [ ] **Step 5: Run and commit**

Run: `uv run pytest packages/coord-engine/tests/test_fleet.py packages/coord-engine/tests/test_no_janitor.py -q`

Expected: PASS.

Run: `git add docs/coord packages/coord-engine && git commit -m "feat(coord): gate manifest-driven fleet evolution"`

## Task 16: Prove Interoperability and Shipping State

**Files:**
- Create: `scripts/export-reference-fixtures.sh`
- Create: `scripts/run-live-acceptance.sh`
- Create: `tests/test_reference_interop.py`
- Create: `docs/coord/REBUILD-ACCEPTANCE.md`
- Modify: `packages/coord-engine/README.md`
- Modify: `packages/collect/README.md`

- [ ] **Step 1: Add sanitized reference-oracle fixtures**

Run the pinned reference binary against a disposable team, export event/config/task/
review/continuity artifacts, strip account-specific identifiers, and assert the
clean-room reader produces the same semantic fold.

- [ ] **Step 2: Add bidirectional compatibility tests**

Reference writer -> rebuilt reader and rebuilt writer -> reference reader must pass
for event v1, cursor v1, Task, Review, verdict shards, presence, roles, and
continuity. Compare semantic JSON after removing timestamps and generated ids.

- [ ] **Step 3: Run complete local verification**

Run: `uv run pytest packages/coord-engine/tests packages/collect/tests tests -q`

Expected: PASS with no xfails for shipped protocol behavior.

- [ ] **Step 4: Run the disposable-team live proof**

Run: `scripts/run-live-acceptance.sh`

Expected: two-agent delivery/response/park/resume PASS; Collect test plugin writes
one record; second run deduplicates it; no secrets or private identity values occur
in captured artifacts.

- [ ] **Step 5: Document evidence and commit**

Record exact commands, versions, commit ids, timestamps, and sanitized output in
`docs/coord/REBUILD-ACCEPTANCE.md`.

Run: `git add scripts tests docs packages && git commit -m "test: prove clean-room bus interoperability"`
