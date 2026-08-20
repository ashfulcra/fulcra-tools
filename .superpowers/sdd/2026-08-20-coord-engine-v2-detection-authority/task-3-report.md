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

## Fix Round 1

### Status

Resolved the five critical/important findings and the adjacent sealed-batch
mutability issue.

### Covering tests

- `test_file_without_a_proven_immutable_identity_makes_the_batch_unknown`
- `test_lifecycle_timestamps_are_validated_normalized_and_sorted_temporally`
- `test_record_cursor_requires_an_attested_exact_boundary_and_supported_channel`
- `test_sealed_batch_does_not_expose_mutable_coverage_or_envelope`
- `test_records_cursor_without_a_server_attested_boundary_is_unknown`
- `test_reconcile_never_bypasses_the_detector_with_a_legacy_raw_feed`
- `test_untrusted_batch_full_recovery_does_not_advance_the_watermark`
- `test_stale_cursor_drift_recovery_keeps_the_existing_feed_frontier`

### Red evidence

Commands run before the corresponding production edits:

```text
uv run --package coord-engine pytest packages/coord-engine/tests/test_v2_change_detection.py packages/coord-engine/tests/test_reconcile_incremental.py::test_reconcile_never_bypasses_the_detector_with_a_legacy_raw_feed -q
uv run --package coord-engine pytest packages/coord-engine/tests/test_transport_parse.py::test_records_cursor_without_a_server_attested_boundary_is_unknown -q
uv run --package coord-engine pytest packages/coord-engine/tests/test_reconcile_incremental.py::test_untrusted_batch_full_recovery_does_not_advance_the_watermark -q
uv run --package coord-engine pytest packages/coord-engine/tests/test_reconcile_incremental.py::test_stale_cursor_drift_recovery_keeps_the_existing_feed_frontier -q
uv run --package coord-engine pytest packages/coord-engine/tests/test_v2_change_detection.py::test_lifecycle_timestamps_are_validated_normalized_and_sorted_temporally -q
```

1. The new detector regression run failed 6 tests before implementation:
   fallback `file:<path>:<state>:<at>` identities were trusted, offset
   timestamps were not canonicalized, list-shaped record cursors were accepted,
   unsupported record-count channels were clear, mutable batch mappings could
   be rewritten, and reconcile called the legacy raw `updates` seam.
2. `test_records_cursor_without_a_server_attested_boundary_is_unknown` failed
   because JSONL rows were returned with a locally derived endpoint rather than
   a server-attested `after`/`through` boundary.
3. `test_untrusted_batch_full_recovery_does_not_advance_the_watermark` failed
   with watermark `2026-07-01T12:30:00Z` instead of the trusted prior frontier.
4. `test_stale_cursor_drift_recovery_keeps_the_existing_feed_frontier` failed
   with watermark `2026-07-01T19:00:00Z` instead of `2026-07-01T12:00:00Z`.
5. Fractional-timestamp normalization initially failed by truncating
   `.123456` from the canonical UTC instant.

### Implementation

- File rows now require a concrete immutable `update_id`, `id`, or
  `version_id`; absence is `UNKNOWN`.
- Lifecycle instants are parsed as timezone-aware ISO-8601 values, normalized
  to UTC without losing fractional precision, and sorted on canonical time.
- Record cursor materialization accepts only an exact-count, server-attested
  `{after, through, records}` window matching the requested watermark; any
  missing, excess, or out-of-window row is `UNKNOWN`. The current JSONL CLI
  response lacks that attestation and therefore fails closed.
- Unsupported record-count channel keys are `UNKNOWN`; `ChangeBatch` output
  mappings are recursively frozen.
- Reconcile has no legacy `updates` fallback: absent `data_updates` performs
  named full recovery. Untrusted and stale-cursor recovery preserves the prior
  watermark while repairing canonical rows.

### Verification

- Focused command:
  `uv run --package coord-engine pytest packages/coord-engine/tests/test_v2_change_detection.py packages/coord-engine/tests/test_transport_parse.py packages/coord-engine/tests/test_reconcile_incremental.py packages/coord-engine/tests/test_reconcile.py -q`
  → `96 passed in 0.16s`.
- Full: `uv run --package coord-engine pytest packages/coord-engine/tests -q`
  exited 0.
- `git diff --check` exited 0.

### Commit SHA

Implementation: `ea6b25a9` (`coord-engine: fail closed change detector`).

### Self-review

- The Unit 2 classified canonical-read seam is unchanged; no new canonical
  `None` interpretation was added. Detector `None` handling applies only to
  its own explicit feed/cursor transport contract and always yields UNKNOWN.
- The named full-scan and drift branches remain. Their cursor publication now
  preserves the old frontier on detector doubt and on stale drift recovery, so
  recovery cannot claim ordinary feed completeness.

### Concerns

- A coordination record-count signal cannot currently become `DATA` against
  the JSONL `get-records` CLI alone because it has no server-attested cursor
  boundary. This is intentional fail-closed behavior until the transport can
  return the documented cursor envelope.
