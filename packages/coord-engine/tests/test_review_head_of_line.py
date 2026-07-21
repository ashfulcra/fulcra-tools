"""Review-fold head-of-line priority (Phase-1 part 2) + the budget-composition fix.

The live incident: `briefing` reports `review fold degraded: scanned 0/207 before
budget`. A 45s review-fold budget at ~0.8s/op should complete ~25 slugs; scanning
ZERO means the review leg's clock was pre-spent before its first slug completed —
the review leg's effective budget was `min(45s, remaining-of-the-shared-briefing-
budget)`, and the earlier legs (presence + role-fold + inbox) drain that shared
budget, so the review leg starts already expired. The caller's OWN pending review
(a third-party maintainer's verdict that sat three days) was invisible.

The fix has two halves that must hold TOGETHER:
  1. A PRIORITY head — review slugs the calling agent is assigned to review (derived
     for free from the review-request DIRECTIVE rows already in the aggregate) — is
     scanned FIRST.
  2. That head gets a budget the earlier briefing legs cannot have already spent, so
     it COMPLETES even when the shared budget is exhausted. A budget cut may then
     only truncate the TAIL.

The two markers are DISTINCT: `review-fold-degraded` (tail truncated — expected)
vs `review-head-degraded` (the caller's own head could not complete — UNKNOWN-loud,
the incident).

Determinism: a fake monotonic clock the transport advances per op (the ClockTransport
idiom), pinned into `budget.time.monotonic`, so "the head completed / the tail was
cut" is an exact fact, never a runtime threshold that flakes under runner load.
"""

import time

from coord_engine import budget, cli
from coord_engine_test_helpers import FakeTransport


class ReviewClock(FakeTransport):
    """Charges `cost` seconds of fake monotonic time per review-namespace op."""

    cost = 0.0

    def __init__(self):
        super().__init__()
        self.clock = 0.0
        self.reads: list[str] = []
        self.lists: list[str] = []

    def _spends(self, path):
        return "/review/" in path

    def _tick(self, path):
        if self._spends(path):
            self.clock += self.cost

    def read(self, path):
        self.reads.append(path)
        self._tick(path)
        return super().read(path)

    def list_dir(self, prefix):
        self.lists.append(prefix)
        self._tick(prefix)
        return super().list_dir(prefix)


def _pin(monkeypatch, t):
    monkeypatch.setattr(budget.time, "monotonic", lambda: t.clock)


def _put_pending_review(t, slug, reviewer):
    """A review doc that gates on `reviewer` with no verdict filed -> PENDING."""
    t.put(f"team/r/review/{slug}.md",
          f"---\ntype: Review\nschema: review-request/v1\nof: url\n"
          f"required: [{reviewer}]\nrequested_by: someone\n---\nReview requested\n")


def _directive_row(slug, assignee):
    """The review-request DIRECTIVE task the requester delivered to the reviewer:
    reconcile indexes it as an aggregate row (title `REVIEW REQUEST: <slug>`,
    assignee = the reviewer). This is the free, zero-transport priority signal."""
    return {"id": f"rr-{slug}", "name": f"rr-{slug}",
            "title": f"REVIEW REQUEST: {slug}", "status": "proposed",
            "assignee": assignee, "priority": "P2", "blocked_on": None, "tags": []}


def _live_shaped(t, *, head_slug="pr-mine", reviewer="alice", n_reviews=200,
                 n_proposed=500):
    """~200 review slugs + ~500 proposed rows — the live board's shape."""
    _put_pending_review(t, head_slug, reviewer)
    for i in range(n_reviews - 1):
        _put_pending_review(t, f"pr{i:03d}", "someone-else")
    rows = [_directive_row(head_slug, reviewer)]
    for i in range(n_proposed):
        rows.append({"id": f"p{i}", "name": f"p{i}", "title": f"proposed {i}",
                     "status": "proposed", "assignee": None, "priority": "P3",
                     "blocked_on": None, "tags": []})
    return rows


def test_caller_head_completes_when_shared_budget_is_spent(capsys, monkeypatch):
    # THE regression, on the live-shaped board. The shared briefing budget is fully
    # spent (deadline == now) before the review leg runs; the tail is uncuttable in
    # this window. The caller's OWN review must STILL be scanned and surfaced.
    t = ReviewClock()
    rows = _live_shaped(t)
    capsys.readouterr()
    t.reads.clear(); t.lists.clear()
    t.cost = 1.0            # 1s of fake time per review op
    _pin(monkeypatch, t)
    # deadline == the current clock -> the SHARED budget has ZERO remaining, exactly
    # the "scanned 0/207" condition. deadline_seconds is the review leg's own 45s.
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows,
                                   deadline_seconds=45.0, deadline=t.clock)
    pend = [r for r in out if r.get("type") == "review-pending"]
    assert any(r["name"] == "pr-mine" for r in pend), \
        f"caller head must COMPLETE even when the shared budget is spent: {out[:3]}"
    # A budget cut may only truncate the TAIL: the tail-degraded marker is present
    # (expected), and it is NOT the head-degraded (UNKNOWN) marker.
    assert any(r.get("type") == "review-fold-degraded" for r in out)
    assert not any(r.get("type") == "review-head-degraded" for r in out), \
        "the head completed, so no head-UNKNOWN marker"


def test_head_scanned_before_tail(capsys, monkeypatch):
    # Ordering, independent of the budget: the caller-assigned slug is read before
    # any non-head slug.
    t = ReviewClock()
    rows = _live_shaped(t, n_reviews=50, n_proposed=10)
    capsys.readouterr()
    t.reads.clear()
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows,
                                   deadline_seconds=45.0)
    head_doc_read = next(i for i, p in enumerate(t.reads)
                         if p == "team/r/review/pr-mine.md")
    first_other = next(i for i, p in enumerate(t.reads)
                       if p.startswith("team/r/review/pr0"))
    assert head_doc_read < first_other, "caller head must be scanned first"


def test_head_that_cannot_complete_is_loud_unknown(capsys, monkeypatch):
    # If even the HEAD cannot complete, that is UNKNOWN — a DISTINCT loud marker,
    # never a silent skip and never conflated with the expected tail truncation.
    t = ReviewClock()
    # Two head slugs for alice; a per-op cost that exhausts a tiny head budget.
    _put_pending_review(t, "pr-mine-a", "alice")
    _put_pending_review(t, "pr-mine-b", "alice")
    _put_pending_review(t, "pr-other", "someone-else")
    rows = [_directive_row("pr-mine-a", "alice"), _directive_row("pr-mine-b", "alice")]
    capsys.readouterr()
    t.reads.clear()
    t.cost = 1.0
    _pin(monkeypatch, t)
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows, deadline_seconds=1.5)
    assert any(r.get("type") == "review-head-degraded" for r in out), \
        f"an incomplete head must surface the distinct UNKNOWN marker: {out}"


def test_no_rows_preserves_legacy_behavior(capsys):
    # Backward compatibility: called without `rows` (the historical signature the
    # existing tests use), the fold behaves exactly as before — no head, no crash.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-x", "--of", "url",
              "--reviewer", "alice"], transport=t)
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice")
    assert any(r.get("type") == "review-pending" and r["name"] == "pr-x"
               for r in out)
