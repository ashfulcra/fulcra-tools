"""Dir-only orphan classification must not consume the TAIL scan budget.

THE ROW (`reviews-fold-degraded-on-two-independent-boxes-...`, P1, blocked four
days): dir-only orphan classification eats the first half of the review tail
deadline, so the remaining tail over ~350 review docs truncates at 18-19 and
`review-orphan-degraded` and `review-fold-degraded` co-occur across hosts. It is
also why `obligations` never reaches rc=0 on any host.

WHY THE COMBINED CASE. The two markers co-occur, and a test for either alone
passes while the pair still fails: classify-truncation alone is just a
visibility gap, and tail-truncation alone is the documented, expected budget
cut. The defect is tail truncation CAUSED BY classify — which only shows up when
you hold the tail and the budget fixed and vary the orphan count.

MEASURED HERE (cost 0.1s/op, 60-doc tail, `_pending_reviews_for`):

     0 orphans -> tail_scanned = 25
     5 orphans -> tail_scanned = 22
    15 orphans -> tail_scanned = 17
    30 orphans -> tail_scanned = 13   + review-orphan-degraded

Monotonic, and 30 orphans HALVE the tail. Classification is charged to the tail's
budget via `tail_dl.reserve(0.5)`, so work that has nothing to do with the tail
decides how much of it gets scanned.

Uses the existing `ReviewClock` harness (fake monotonic clock, injected per-op
cost) rather than a new one — real sleeps against a monotonic deadline cannot
exhaust a budget deterministically, and building a second harness is how I
burned four attempts before reading this one.
"""

from test_review_head_of_line import (  # noqa: F401 — the existing harness
    ReviewClock, _pin, _put_pending_review, _directive_row)

from coord_engine import cli

COST = 0.1
TAIL = 60
BUDGET = 5.0


def _board(t, *, n_orphan):
    """A tail of real review docs, plus `n_orphan` dir-only slugs.

    A dir-only slug is a `verdicts/` directory with NO `<slug>.md` review doc —
    that is what makes it an orphan needing classification.
    """
    for i in range(n_orphan):
        t.put(f"team/r/review/orph{i:03d}/verdicts/v.md", "---\ntype: Verdict\n---\n")
    for i in range(TAIL):
        _put_pending_review(t, f"pr{i:03d}", "someone-else")
    return [_directive_row("pr000", "alice")]


def _tail_scanned(monkeypatch, *, n_orphan):
    t = ReviewClock()
    rows = _board(t, n_orphan=n_orphan)
    t.cost = COST
    _pin(monkeypatch, t)
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows,
                                   deadline_seconds=BUDGET)
    fold = [o for o in out if o.get("type") == "review-fold-degraded"]
    orphan = any(o.get("type") == "review-orphan-degraded" for o in out)
    return (fold[0]["scanned"] if fold else TAIL), orphan


def test_the_combined_case_reproduces_at_all():
    """Guard on the fixture itself: if this stops producing BOTH markers the
    regression below is vacuous and would pass while the defect stands."""
    import pytest
    scanned, orphan = _tail_scanned(pytest.MonkeyPatch(), n_orphan=30)
    assert orphan, "fixture no longer truncates classification — test is vacuous"
    assert scanned < TAIL, "fixture no longer truncates the tail — test is vacuous"


def test_classification_does_not_steal_the_tail_budget(monkeypatch):
    """THE REGRESSION. Same tail, same budget, same per-op cost — only the number
    of dir-only orphans changes. Stable dir classification is not tail work, so
    the tail must not shrink because orphans exist.

    Fails today: 13 scanned with 30 orphans against 25 with none.
    """
    baseline, _ = _tail_scanned(monkeypatch, n_orphan=0)
    starved, orphan_degraded = _tail_scanned(monkeypatch, n_orphan=30)
    assert orphan_degraded, "expected the classify phase to be truncated here"
    assert starved >= baseline, (
        f"dir-only orphan classification consumed the tail scan budget: "
        f"{starved} slugs scanned with 30 orphans vs {baseline} with none, at "
        f"identical tail size, budget and per-op cost")
