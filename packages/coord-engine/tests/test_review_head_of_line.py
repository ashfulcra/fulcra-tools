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
from coord_engine.transport import TransportError
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


class _ReadNone(FakeTransport):
    """Lists a slug's doc normally but returns None on read — a transport failure
    (read() never raises; None on a listed doc means the read failed)."""

    def __init__(self, *none_paths):
        super().__init__()
        self._none = set(none_paths)

    def read(self, path):
        if path in self._none:
            return None
        return super().read(path)


class _ListRaises(FakeTransport):
    """Raises TransportError on an EXACT listing prefix (a per-slug verdicts
    listing timing out), while every other listing succeeds."""

    def __init__(self, *raise_prefixes):
        super().__init__()
        self._raise = set(raise_prefixes)

    def list_dir(self, prefix):
        if prefix in self._raise:
            raise TransportError("verdicts listing timed out")
        return super().list_dir(prefix)


def test_head_slug_unreadable_doc_is_head_degraded(capsys):
    # P1 part 1: a caller-OWNED head slug whose review doc read returns None is
    # UNKNOWN. It must surface the DISTINCT `review-head-degraded` marker, not vanish
    # into ordinary tail fold degradation (or nothing). No budget pressure here — the
    # failure is the unreadable doc alone.
    doc = cli._review_doc_path("r", "pr-mine")
    t = _ReadNone(doc)
    _put_pending_review(t, "pr-mine", "alice")
    rows = [_directive_row("pr-mine", "alice")]
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows, deadline_seconds=45.0)
    assert any(r.get("type") == "review-head-degraded" for r in out), \
        f"an unreadable caller-owned head slug must be loud UNKNOWN: {out}"


def test_head_slug_transport_error_is_head_degraded(capsys):
    # P1 part 1: a caller-OWNED head slug whose tally raises TransportError (a Task-1
    # per-slug timeout) is UNKNOWN and must surface `review-head-degraded`.
    vp = cli._verdicts_prefix("r", "pr-mine")
    t = _ListRaises(vp)
    _put_pending_review(t, "pr-mine", "alice")
    rows = [_directive_row("pr-mine", "alice")]
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows, deadline_seconds=45.0)
    assert any(r.get("type") == "review-head-degraded" for r in out), \
        f"a head slug whose tally raised must be loud UNKNOWN: {out}"


def test_head_slug_absent_from_listing_is_head_degraded(capsys):
    # P1 part 2: an OPEN caller review directive whose slug has no `.md` in the
    # listing must NOT silently vanish (negative-membership inference from a listing
    # is not proof the obligation is gone). It fails closed: UNKNOWN, surfaced via
    # `review-head-degraded`, and the marker names the missing slug so the caller
    # can act.
    t = FakeTransport()
    # A directive names pr-ghost for alice, but no pr-ghost.md exists in the store.
    rows = [_directive_row("pr-ghost", "alice")]
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows, deadline_seconds=45.0)
    head = [r for r in out if r.get("type") == "review-head-degraded"]
    assert head, f"a caller directive slug absent from the listing must fail closed: {out}"
    assert "pr-ghost" in (head[0].get("missing") or []), \
        f"the head-degraded marker must name the missing slug: {head[0]}"


def test_head_degraded_line_renders_missing(capsys):
    # The renderer surfaces the missing-slug detail so a briefing / needs-me reader
    # (shared dispatch -> identical line) sees WHICH obligation went UNKNOWN.
    line = cli._review_head_degraded_line(
        {"type": "review-head-degraded", "scanned": 0, "total": 0,
         "missing": ["pr-ghost"]})
    assert "pr-ghost" in line and "missing" in line.lower()


# --- Round-3 (codex): PHASE-LOCAL marker accounting ------------------------------
# Three defects in how head/tail markers accounted for scanned/total/skipped:
#   1. head_total excluded missing slugs -> UNKNOWN rendered as completion (0/0, 1/1).
#   2. the head-degraded LINE always blamed "before budget" for non-budget causes.
#   3. the terminal global `skipped` re-emitted a TAIL (review-fold-degraded) marker
#      for a HEAD-only incident with no tail at all -> the same incident twice.
# The fix is phase-local accounting: head markers count only head work (incl. missing
# slugs); the tail marker counts only tail work and fires only on real tail
# degradation. These tests assert the FULL marker payloads, not mere presence.


def test_missing_only_head_total_counts_missing(capsys):
    # DEFECT 1: a caller obligation whose slug has NO doc in the listing is the ONLY
    # head work. head_total must COUNT it (1, not 0) so the marker reads 0/1 (nothing
    # scanned of one owed) — never 0/0, which implies there was nothing to scan. And
    # a head-only incident emits NOTHING else — no phantom review-fold-degraded.
    t = FakeTransport()
    rows = [_directive_row("ghost", "alice")]
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows, deadline_seconds=45.0)
    assert out == [{"type": "review-head-degraded", "scanned": 0, "total": 1,
                    "missing": ["ghost"]}], out


def test_unreadable_head_no_tail_emits_only_head_marker(capsys):
    # DEFECT 3: a listed-but-unreadable head slug, a readable pending head sibling,
    # and NO tail. The only rows are the sibling's review-pending and ONE
    # review-head-degraded whose counts reflect the HEAD alone (scanned 2/2, one
    # skipped). The terminal global-`skipped` path must NOT also emit a
    # review-fold-degraded — that marker describes expected TAIL truncation, and
    # there is no tail here.
    doc_a = cli._review_doc_path("r", "pr-mine-a")
    t = _ReadNone(doc_a)
    _put_pending_review(t, "pr-mine-a", "alice")  # unreadable doc -> UNKNOWN head slug
    _put_pending_review(t, "pr-mine-b", "alice")  # readable -> pending head sibling
    rows = [_directive_row("pr-mine-a", "alice"), _directive_row("pr-mine-b", "alice")]
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows, deadline_seconds=45.0)
    assert not any(r.get("type") == "review-fold-degraded" for r in out), \
        f"a head-only incident must NOT emit a tail-truncation marker: {out}"
    pend = [r for r in out if r.get("type") == "review-pending"]
    assert pend == [{"type": "review-pending", "name": "pr-mine-b",
                     "state": "PENDING", "pending_required": ["alice"]}], out
    head = [r for r in out if r.get("type") == "review-head-degraded"]
    assert head == [{"type": "review-head-degraded", "scanned": 2, "total": 2,
                     "skipped": 1}], out
    assert len(out) == 2, f"exactly the pending sibling + head marker: {out}"


def test_one_listed_one_missing_head(capsys):
    # DEFECT 1 (mixed): one head slug listed + one missing. total==2 (both owed),
    # scanned reflects the one listed slug attempted, and the missing slug is named.
    _put_pending_review(t := FakeTransport(), "pr-mine", "alice")
    rows = [_directive_row("pr-mine", "alice"), _directive_row("pr-ghost", "alice")]
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows, deadline_seconds=45.0)
    head = [r for r in out if r.get("type") == "review-head-degraded"]
    assert len(head) == 1, out
    assert head[0]["total"] == 2, head[0]
    assert head[0]["scanned"] == 1, head[0]
    assert head[0]["missing"] == ["pr-ghost"], head[0]
    assert not any(r.get("type") == "review-fold-degraded" for r in out), out


def test_tail_budget_cut_uses_tail_only_numbers(capsys, monkeypatch):
    # DEFECT 3 (numbers): head fine + tail genuinely budget-cut. The fold marker's
    # scanned/total describe ONLY the tail (0 tail slugs scanned of 199), never the
    # head+tail cumulative counts it used to borrow. And NO review-head-degraded.
    t = ReviewClock()
    rows = _live_shaped(t)  # 1 head (pr-mine) + 199 tail, n_reviews=200
    capsys.readouterr()
    t.reads.clear(); t.lists.clear()
    t.cost = 1.0
    _pin(monkeypatch, t)
    out = cli._pending_reviews_for(t, "r", "alice", rows=rows,
                                   deadline_seconds=45.0, deadline=t.clock)
    assert not any(r.get("type") == "review-head-degraded" for r in out), \
        "the head completed on its fresh budget -> no head marker"
    deg = [r for r in out if r.get("type") == "review-fold-degraded"]
    assert len(deg) == 1, out
    assert deg[0]["total"] == 199, f"tail-only total, not head+tail: {deg[0]}"
    assert deg[0]["scanned"] == 0, f"no tail slug scanned -> 0, not the head count: {deg[0]}"


def test_head_degraded_line_no_before_budget_for_nonbudget_cause(capsys):
    # DEFECT 2: an unreadable / missing head slug is NOT a budget cut. The rendered
    # line must NOT attribute it to "before budget".
    line = cli._review_head_degraded_line(
        {"type": "review-head-degraded", "scanned": 0, "total": 1,
         "missing": ["pr-ghost"]})
    assert "before budget" not in line, line
    assert "UNKNOWN" in line and "pr-ghost" in line, line
