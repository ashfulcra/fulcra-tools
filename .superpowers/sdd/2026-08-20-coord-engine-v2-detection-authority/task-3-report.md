# Task 3 report — Normalized Change Detector

## Status

Implemented and verified.

## Files changed

- `packages/coord-engine/coord_engine/change_detection.py`
- `packages/coord-engine/coord_engine/transport.py`
- `packages/coord-engine/coord_engine/records.py`
- `packages/coord-engine/coord_engine/reconcile.py`
- `packages/coord-engine/tests/test_v2_change_detection.py`
- `packages/coord-engine/tests/test_transport_parse.py`
- `packages/coord-engine/tests/test_reconcile_incremental.py`
- `packages/coord-engine/README.md`

## Red evidence

1. `uv run --package coord-engine pytest packages/coord-engine/tests/test_v2_change_detection.py -q`
   failed 6/6 with `ModuleNotFoundError: No module named
   'coord_engine.change_detection'` before production implementation.
2. `test_incremental_discovery_consumes_one_normalized_batch` failed with
   `feed_envelope_calls == 0`, proving reconcile still used its old raw-feed
   path before integration.
3. `test_records_cursor_uses_one_bounded_record_window` failed with
   `AttributeError: ... has no attribute 'records_cursor'` before its transport
   implementation.

## Implementation decisions

- `ChangeDetector.poll(team, prior_watermark, deadline)` consumes one bounded
  raw envelope, validates it before rows are consumed, normalizes lifecycle
  rows, deduplicates immutable update identities, and sorts deterministically.
- Namespace coverage is explicit and fail-closed. Timeout, malformed envelope,
  unrecognized in-team path, record materialization doubt, or short record
  cursor response yields `UNKNOWN` and blocks trusted watermark advancement.
- Record counts only trigger one bounded cursor read; only concrete `id` plus
  `recorded_at` identities enter the batch.
- Current reconcile transports consume the sealed batch once. Pre-Unit-3
  duck-typed transports retain their established `updates` compatibility branch;
  the real `FulcraFileTransport` uses `data_updates`.

## Verification

- Focused: `20 passed in 0.04s` for detector, transport cursor, and affected
  incremental/fast-path tests.
- Full: `2463 passed, 8 skipped in 95.79s (0:01:35)` for
  `packages/coord-engine/tests`.
- `git diff --check`: exit 0.

## Commit SHA

Implementation: `36cbe576` (`coord-engine: add normalized change detector`).

## Self-review findings

- Verified the detector never manufactures record identities from a count and
  retains the distinct `NOT_RUN`/`CLEAR`/`DATA`/`UNKNOWN` enum facts.
- Verified normal reconcile discovery takes one envelope call; recovery remains
  a named full scan when the batch cannot be trusted.

## Concerns

- The platform envelope's record-count key is currently normalized as
  `record_counts.coordination`; other future channel names intentionally degrade
  to `UNKNOWN` until their immutable cursor semantics are specified.
