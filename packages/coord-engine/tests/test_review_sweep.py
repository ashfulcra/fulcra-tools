"""``sweep_disposition`` — what a residue sweep may close, and why.

The tests that carry this file are the REFUSALS. The sweep closes review-request
rows in bulk, so the expensive failure is not "residue survives another pass", it
is "a row was closed while its review still owed a verdict" — which is precisely
what a `.settled` marker can hide: a stale cache made an earlier reader report an
agent owed nothing while the newest verdict was CHANGES.

Load-bearing cases:

- an unreadable listing closes NOTHING (UNKNOWN is not empty),
- a terminal marker is found by ASKING THE SET, so a marker added tomorrow is
  honoured without editing the sweep — the failure that made `.gc-closed`
  invisible for a whole round was each reader hard-coding one filename,
- `.settled` is never honoured on presence: it goes through the shared
  `settle_shortcircuit`, so a stale or unbound marker leaves the row OPEN,
- an APPROVED marker with an `evidence` KEY that is merely empty is
  engine-written and merely unresolved, while one with NO evidence key at all
  cannot have been written by any engine path and is QUARANTINED. The difference
  is a key that exists, not a value that is truthy.
"""
from __future__ import annotations

import pytest

from coord_engine import review, review_gc

SETTLED = ".settled"

#: A shard name that satisfies the append-only suffix rule, so a digest over
#: this listing is a valid fingerprint at all.
SHARD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa--2026-08-20T21:00:00Z-deadbeef.md"


def test_unreadable_listing_closes_nothing():
    d = review_gc.sweep_disposition(
        None, settled_marker=SETTLED, listing_ok=False)
    assert d.kind == review_gc.SWEEP_UNKNOWN
    assert "closes nothing" in d.why


def test_non_enumerable_listing_closes_nothing():
    d = review_gc.sweep_disposition(object(), settled_marker=SETTLED)
    assert d.kind == review_gc.SWEEP_UNKNOWN


@pytest.mark.parametrize("marker", sorted(review_gc.TERMINAL_MARKERS))
def test_every_terminal_marker_closes_and_is_named(marker):
    d = review_gc.sweep_disposition({marker}, settled_marker=SETTLED)
    assert d.kind == review_gc.SWEEP_CLOSE
    assert marker in d.why, "the closure must name the marker it acted on"


def test_a_marker_added_to_the_set_is_honoured_without_editing_the_sweep(
        monkeypatch):
    # The whole point of asking the SET. If this test needs the sweep edited to
    # pass, the sweep has re-created the bug that hid `.gc-closed` for a round.
    monkeypatch.setattr(
        review_gc, "TERMINAL_MARKERS",
        frozenset(review_gc.TERMINAL_MARKERS | {".future-marker"}))
    d = review_gc.sweep_disposition({".future-marker"},
                                         settled_marker=SETTLED)
    assert d.kind == review_gc.SWEEP_CLOSE
    assert ".future-marker" in d.why


def test_no_markers_at_all_stays_open():
    d = review_gc.sweep_disposition({SHARD}, settled_marker=SETTLED)
    assert d.kind == review_gc.SWEEP_OPEN


def test_settled_merged_closes():
    d = review_gc.sweep_disposition(
        {SETTLED}, settled_marker=SETTLED,
        marker_fm={"state": "MERGED", "merge_sha": "a" * 40})
    assert d.kind == review_gc.SWEEP_CLOSE
    assert "merged" in d.why


def test_settled_cache_closes_and_records_that_the_digest_was_recomputed():
    names = {SETTLED, SHARD}
    fm = {"state": review.APPROVED,
          "evidence": review.evidence_digest(names)}
    d = review_gc.sweep_disposition(
        names, settled_marker=SETTLED, marker_fm=fm)
    assert d.kind == review_gc.SWEEP_CLOSE
    assert "recomputed" in d.why and "matched" in d.why


def test_a_stale_digest_does_not_close_the_row():
    # The bug this sweep must not re-introduce: presence would have closed it.
    names = {SETTLED, SHARD}
    fm = {"state": review.APPROVED, "evidence": "0" * 16}
    d = review_gc.sweep_disposition(
        names, settled_marker=SETTLED, marker_fm=fm)
    assert d.kind == review_gc.SWEEP_UNRESOLVED


def test_an_empty_evidence_KEY_is_engine_written_and_merely_unresolved():
    # `settled_marker_fields` writes `evidence: ""` when it has no digest, and
    # the renderer omits None but not "". So the key EXISTS: engine provenance.
    d = review_gc.sweep_disposition(
        {SETTLED, SHARD}, settled_marker=SETTLED,
        marker_fm={"state": review.APPROVED, "evidence": ""})
    assert d.kind == review_gc.SWEEP_UNRESOLVED


def test_a_missing_evidence_KEY_is_quarantined_not_merely_unresolved():
    # No engine write path can produce this shape. Six of these are live in the
    # store, written 2026-08-26 and 2026-08-28.
    d = review_gc.sweep_disposition(
        {SETTLED, SHARD}, settled_marker=SETTLED,
        marker_fm={"state": review.APPROVED})
    assert d.kind == review_gc.SWEEP_UNKNOWN_PROVENANCE
    assert "quarantined" in d.why


def test_terminal_marker_wins_over_a_settle_marker_in_the_same_listing():
    d = review_gc.sweep_disposition(
        {review_gc.CONCLUDED_MARKER, SETTLED}, settled_marker=SETTLED,
        marker_fm={"state": review.APPROVED})
    assert d.kind == review_gc.SWEEP_CLOSE
    assert review_gc.CONCLUDED_MARKER in d.why


def test_the_answer_is_machine_readable_so_a_run_can_split_its_closures():
    """A scheduled run reports closures BY CAUSE. If it had to parse its own
    prose to do that, the report would drift from the rule silently."""
    names = {SETTLED, SHARD}
    merged = review_gc.sweep_disposition(
        {SETTLED}, settled_marker=SETTLED,
        marker_fm={"state": "MERGED", "merge_sha": "a" * 40})
    cache = review_gc.sweep_disposition(
        names, settled_marker=SETTLED,
        marker_fm={"state": review.APPROVED,
                   "evidence": review.evidence_digest(names)})
    terminal = review_gc.sweep_disposition(
        {review_gc.CONCLUDED_MARKER}, settled_marker=SETTLED)
    assert merged.answer == review.SETTLE_MERGED
    assert cache.answer == review.SETTLE_CACHE
    assert terminal.answer == review_gc.CONCLUDED_MARKER
    # Distinct causes must not collapse to one bucket.
    assert len({merged.answer, cache.answer, terminal.answer}) == 3
