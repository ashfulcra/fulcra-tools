"""Unit tests for coord_engine.budget — the shared wake-budget mechanics.

These pin the Deadline discipline (open/expired/reserve), the degraded-row shape,
and the degraded-line renderer that the bounded fan-out folds share, so the family
can never drift apart again (the drift the extraction was built to end).
"""

import time

from coord_engine import budget
from coord_engine.budget import Deadline


# --- Deadline.expired / open ------------------------------------------------


def test_unbounded_deadline_never_expires():
    assert Deadline(None).expired() is False
    assert Deadline.open(None).expired() is False


def test_open_future_budget_not_yet_expired():
    dl = Deadline.open(60)
    assert dl.expired() is False


def test_past_instant_is_expired():
    assert Deadline(time.monotonic() - 1).expired() is True


def test_open_zero_budget_is_immediately_expired():
    # `>=` boundary: a budget of 0 puts the instant at (essentially) now, and the
    # tiny elapsed time between open() and the check makes it spent. This is the
    # normalized boundary semantics all sites now share.
    dl = Deadline.open(0)
    assert dl.expired() is True


def test_expired_uses_ge_boundary(monkeypatch):
    # At exactly the instant, `>=` counts it spent (the normalization the two `>`
    # sites — overlay, threads — were moved onto).
    fixed = 1000.0
    dl = Deadline(fixed)
    monkeypatch.setattr(budget.time, "monotonic", lambda: fixed)
    assert dl.expired() is True


# --- Deadline.reserve -------------------------------------------------------


def test_reserve_half_carves_the_budget():
    # reserve(0.5) on a 30s budget yields a sub-deadline 15s out (matches the old
    # `classify_deadline = deadline - deadline_seconds/2.0`).
    dl = Deadline.open(30)
    sub = dl.reserve(0.5)
    assert abs((dl.instant - sub.instant) - 15.0) < 1e-6


def test_reserve_quarter():
    dl = Deadline.open(40)
    sub = dl.reserve(0.25)
    assert abs((dl.instant - sub.instant) - 10.0) < 1e-6


def test_reserve_on_unbounded_stays_unbounded():
    assert Deadline.open(None).reserve(0.5).instant is None


def test_reserve_on_bare_instant_reserves_nothing():
    # A Deadline built from a raw instant (the receive-a-deadline-arg case) has no
    # retained budget, so reserve is a no-op passthrough — it can't invent a budget.
    dl = Deadline(1234.0)
    sub = dl.reserve(0.5)
    assert sub.instant == 1234.0


# --- degraded_row -----------------------------------------------------------


def test_degraded_row_omits_skipped_when_zero():
    assert budget.degraded_row("forge-degraded", 2, 5) == {
        "type": "forge-degraded", "scanned": 2, "total": 5}


def test_degraded_row_includes_skipped_when_nonzero():
    assert budget.degraded_row("review-fold-degraded", 3, 7, 1) == {
        "type": "review-fold-degraded", "scanned": 3, "total": 7, "skipped": 1}


def test_degraded_row_key_order_is_stable():
    # JSON output order must stay type/scanned/total/skipped.
    row = budget.degraded_row("presence-degraded", 1, 4, 2)
    assert list(row.keys()) == ["type", "scanned", "total", "skipped"]


# --- fold_degraded_line -----------------------------------------------------


def test_fold_degraded_line_matches_review_wording():
    r = {"type": "review-fold-degraded", "scanned": 2, "total": 5}
    assert budget.fold_degraded_line(
        r, label="review", remedy="run per-slug review status for the rest",
        noun="slug") == (
        "  review fold degraded: scanned 2/5 before budget — "
        "run per-slug review status for the rest")


def test_fold_degraded_line_appends_skipped_suffix():
    r = {"type": "forge-degraded", "scanned": 1, "total": 3, "skipped": 2}
    assert budget.fold_degraded_line(
        r, label="forge", remedy="run forge feedback for the rest",
        noun="PR") == (
        "  forge fold degraded: scanned 1/3 before budget — "
        "run forge feedback for the rest (2 PR(s) skipped on transport error)")


def test_fold_degraded_line_presence_wording():
    r = {"type": "presence-degraded", "scanned": 0, "total": 4, "skipped": 1}
    assert budget.fold_degraded_line(
        r, label="presence",
        remedy="roster may be partial, run `presence show` for the rest",
        noun="shard") == (
        "  presence fold degraded: scanned 0/4 before budget — roster may be "
        "partial, run `presence show` for the rest (1 shard(s) skipped on "
        "transport error)")
