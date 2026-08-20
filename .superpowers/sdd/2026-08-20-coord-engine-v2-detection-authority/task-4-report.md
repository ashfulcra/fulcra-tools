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

The existing transport interface has no universal compare-and-swap write.
`generation.publish` uses `write_if_unchanged` when supplied and otherwise a
double-read fence; a fully atomic stale-writer exclusion on legacy transports
requires the transport-level conditional write planned for the next authority
unit. No partial or unverified generation is published in either mode.
