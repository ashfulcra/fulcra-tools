# Unit 4 report — Immutable Generation Builder and Publication Fence

## Scope

Implemented Unit 4 on `codex/engine-v2-spec`, based on
`ec790f3a3925daf0d96591373c97cb19f4f7e8fa`.

## Implementation

- Added `coord_engine.generation`: pure compact/key-sorted generation
  construction, digest-addressed IDs, content digests, required section state,
  current-manifest validation, write/read verification, interruption handling,
  and a conditional-write publication fence where the transport provides one.
- Reconcile now seals all required task/review/forge/role/presence/
  acknowledgement/response sections before advancing
  `_coord/projections/current.json`. Generation files are written and verified
  first; trusted incomplete builds return degraded and preserve the old current
  manifest.
- Recovery state is kept beneath an immutable build identity at
  `_coord/projections/builds/<base-generation>.json`; it resumes only while its
  base generation remains current.
- Added a digest-verified `projection.generation_section` read path. Existing
  `summaries.json` readers retain their compatibility fence until explicitly
  migrated; generation metadata is ignored by change detection.
- Updated `AGENTS.md` with the publication ordering, incomplete-build, and new
  reader behavior.

## Test-first evidence

1. Added `tests/test_v2_generation.py` before `generation.py` existed.
   `pytest .../test_v2_generation.py -q` failed at collection with
   `ImportError: cannot import name 'generation'`.
2. Added the reconcile publication test before the reconcile integration.
   The targeted test failed because `generation.load_current(...)` returned
   `None`.
3. Added the projection validation test before `generation_section` existed.
   It failed with `AttributeError: module 'coord_engine.projection' has no
   attribute 'generation_section'`.
4. Implemented the minimal behavior to make each red test pass, then kept the
   pre-existing projection/reconcile contracts green.

## Verification

- Focused: `uv run --project packages/coord-engine pytest
  packages/coord-engine/tests/test_v2_generation.py
  packages/coord-engine/tests/test_projection.py
  packages/coord-engine/tests/test_reconcile.py
  packages/coord-engine/tests/test_v2_change_detection.py -q`
  — `147 passed in 0.31s`.
- Full package suite: `uv run --project packages/coord-engine pytest
  packages/coord-engine/tests -q` — exit 0 (fresh run after implementation).
- Compile check: `uv run --project packages/coord-engine python -m compileall
  -q packages/coord-engine/coord_engine` — exit 0.
- Whitespace: `git diff --check` — exit 0.

## Self-review

- Generation identity uses only prior generation, source watermark, normalized
  sealed `ChangeBatch`, schema version, and engine version; host/session data
  and writer timestamps are removed from sealed section bytes.
- `current.json` has exactly the five required fields and is accepted only when
  its referenced generation's bytes match its digest.
- All required section states must be `CLEAR` or `DATA`; `UNKNOWN`, `NOT_RUN`,
  or missing sections cannot publish.
- A generation write that is interrupted before the manifest cannot replace the
  former current view. Conditional transports reject a changed manifest rather
  than overwriting a racing winner; legacy transports retain a double-read
  fence and do not claim freshness (Unit 5 owns freshness policy).
- Audited current readers: `fresh_section`/`feed_fresh_section` and their CLI
  consumers still use `summaries.json`; this change preserves that contract and
  provides the verified generation path for incremental migration.

## Concern

The deployed Fulcra File API still lacks server-side conditional upload, so
strict production reconciliation safely refuses generation publication until
that platform capability is delivered. The compatibility aggregate remains
available to existing readers, but it is not a newer authoritative generation.

## Fix Round 1

### Critical 1 — untrusted detection no longer reports success

- Covering tests: `tests/test_reconcile.py::test_reconcile_unknown_detector_returns_degraded_without_advancing_current`.
- Change: strict/production transports now stop immediately with a degraded,
  nonzero reconcile result on an untrusted `ChangeBatch`; no aggregate or
  current generation advances. The legacy aggregate fixture seam is explicitly
  retained only for unmigrated reader-contract tests.
- Command: `uv run --project packages/coord-engine pytest packages/coord-engine/tests/test_v2_generation.py packages/coord-engine/tests/test_v2_change_detection.py packages/coord-engine/tests/test_projection.py packages/coord-engine/tests/test_reconcile.py packages/coord-engine/tests/test_transport.py -q`
- Output: `193 passed in 5.23s`.

### Critical 2 — production transport cannot impersonate CAS

- Covering tests: `tests/test_v2_generation.py::test_manifest_publish_fails_closed_without_a_proven_conditional_write`, `tests/test_transport.py::test_conditional_write_fails_closed_when_file_api_has_no_atomic_cas`.
- Change: `generation.publish` requires `write_if_unchanged`; no double-read
  fallback remains. `FulcraFileTransport.write_if_unchanged` explicitly returns
  false because the deployed File API is last-writer-wins and exposes no atomic
  conditional upload. This leaves the old manifest current rather than claiming
  a publication the server cannot prove.
- Command/output: same focused command; `193 passed in 5.23s`.

### Important 3 — complete sections recursively read canonical bytes

- Covering tests: `tests/test_v2_generation.py::test_each_required_section_has_its_own_deadline_and_unknown_never_seals`, `tests/test_projection.py::test_generation_reader_rejects_a_not_run_required_section`.
- Change: namespace builders recurse through every directory, read every file
  under their own `Deadline`, retain `CLEAR`/`DATA`/`NOT_RUN`/`UNKNOWN` as
  distinct states, and never seal `NOT_RUN` or unreadable content.
- Command/output: same focused command; `193 passed in 5.23s`.

### Important 4 — recovery state is exact-build scoped

- Covering tests: `tests/test_v2_generation.py::test_progress_build_id_binds_base_watermark_and_normalized_updates`, `tests/test_v2_generation.py::test_recovery_progress_resumes_only_the_exact_immutable_build_id`.
- Change: progress paths now use a digest over prior generation, feed watermark,
  normalized batch, and schema; a different watermark cannot load or overwrite
  the previous build's recovery frontier.
- Command/output: same focused command; `193 passed in 5.23s`.

### Important 5 — manifest watermark is feed evidence

- Covering test: `tests/test_v2_change_detection.py::test_attested_feed_frontier_is_sealed_separately_from_event_timestamps`.
- Change: `ChangeBatch` carries an optional normalized, monotonic server-attested
  `through` frontier. Strict publication rejects batches without it and never
  substitutes the latest event timestamp or host clock.
- Command: `uv run --project packages/coord-engine pytest packages/coord-engine/tests/test_v2_change_detection.py::test_attested_feed_frontier_is_sealed_separately_from_event_timestamps -q`.
- Output: `1 passed in 0.01s`.

### Important 6 — missing regression coverage

- Covering tests added in `tests/test_v2_generation.py`, `tests/test_projection.py`,
  `tests/test_reconcile.py`, and `tests/test_transport.py` as listed above.
- Command/output: focused command; `193 passed in 5.23s`.

### Important 7 — documentation contract kept in sync

- Updated `AGENTS.md`, `packages/coord-engine/README.md`, and
  `docs/coord/OUTPUT-CONTRACT.md` to state immutable-manifest ordering,
  attested-frontier/conditional-write requirements, nonzero failure behavior,
  and the temporary `summaries.json` compatibility status.

### Round verification

- Full command: `uv run --project packages/coord-engine pytest packages/coord-engine/tests -q` — exit 0.
- `git diff --check` — exit 0.

## Fix Round 2

### Critical 1 — compatibility cannot bypass untrusted detection

- Covering tests: `packages/coord-engine/tests/test_reconcile.py::test_reconcile_untrusted_batch_is_degraded_even_with_legacy_compatibility`; `packages/coord-engine/tests/test_reconcile_incremental.py::test_feed_unavailable_fails_closed_without_advancing_current`; `packages/coord-engine/tests/test_reconcile_incremental.py::test_reconcile_never_bypasses_the_detector_with_a_legacy_raw_feed`.
- Change: `reconcile()` now returns a degraded result before any task or
  aggregate work whenever `ChangeBatch.trusted` is false, irrespective of a
  transport compatibility attribute. A missing attested feed frontier is also
  degraded. Incremental fixtures now use an attested `through` and proven CAS
  seam for successful paths; unavailable feeds preserve the prior sealed view.
- Red command/output: `PYTHONPATH=packages/coord-engine pytest -q packages/coord-engine/tests/test_reconcile.py::test_reconcile_untrusted_batch_is_degraded_even_with_legacy_compatibility packages/coord-engine/tests/test_v2_generation.py::test_each_required_section_has_its_own_deadline_and_exhaustion_never_seals packages/coord-engine/tests/test_v2_generation.py::test_generation_sections_reject_unparseable_canonical_bytes` — `3 failed` before implementation.

### Important 3 — canonical bytes are parsed and typed states remain distinct

- Covering tests: `packages/coord-engine/tests/test_v2_generation.py::test_generation_sections_reject_unparseable_canonical_bytes`; `packages/coord-engine/tests/test_v2_generation.py::test_each_required_section_has_its_own_deadline_and_exhaustion_never_seals`.
- Change: every recursive canonical inventory read parses OKF frontmatter before
  adding its original bytes and parsed document to the sealed value. Invalid or
  expired reads produce `UNKNOWN`. Detector coverage now returns `CLEAR` when
  appropriate rather than coercing it to `DATA`; fixed task/review/forge
  sections validate their canonical folded shape and derive `CLEAR` or `DATA`
  from content while preserving `NOT_RUN` and `UNKNOWN` fail-closed states.

### Important 6 — all seven independent section deadlines are exercised

- Covering test: `packages/coord-engine/tests/test_v2_generation.py::test_each_required_section_has_its_own_deadline_and_exhaustion_never_seals`.
- Change: `_generation_sections()` opens a named deadline for every required
  section: tasks, reviews, forge, roles, presence, acknowledgments, and
  responses. The regression drives the production inventory reader until the
  roles deadline expires, proves the remaining six deadlines opened distinctly,
  and proves the resulting generation cannot seal.

### Verification

- Focused command: `uv run --package coord-engine pytest -q packages/coord-engine/tests/test_v2_generation.py packages/coord-engine/tests/test_reconcile.py packages/coord-engine/tests/test_reconcile_incremental.py packages/coord-engine/tests/test_projection.py packages/coord-engine/tests/test_role_inboxes.py packages/coord-engine/tests/test_threads.py packages/coord-engine/tests/test_json_purity.py packages/coord-engine/tests/test_annotate_project.py packages/coord-engine/tests/test_authority_currency.py packages/coord-engine/tests/test_bus_tags.py`.
- Focused output: `387 passed in 2.57s`.
- Full command: `uv run --package coord-engine pytest -q packages/coord-engine/tests`.
- Full output: `2494 passed, 8 skipped in 83.43s`.
- Whitespace command: `git diff --check`.
- Whitespace output: exit 0.
