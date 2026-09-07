"""Forge must not starve behind the review fold in a shared budget.

MEASURED, not theorised (coord-maintainer, 2026-08-10, live store): the review
fold cut at 192/219 and the forge section came back `scanned=None, total=None,
rows=0` — never built at all, on every pass, so every consumer paid a 74.8s raw
fallback just to discover it. The cause was one line: `proj_dl` was a single
`Deadline` object passed to BOTH builders, so the review fold spent it first and
forge inherited an already-expired deadline.

That is not a slow-forge problem. It is a never-forge one, and it is
deterministic — a section that always runs last inside a shared budget starves
every time, not occasionally. AGENTS.md already says a budget cut may only
truncate the tail; this is the same rule one level up, applied BETWEEN sections
instead of within one.

These drive `reconcile.reconcile` end to end, because the defect lives in the
call site's deadline plumbing: a test of either builder alone passes happily
while production never builds one of them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from coord_engine import projection, reconcile
from coord_engine_test_helpers import FakeTransport

TEAM = "r"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(t, *, reviews: int):
    """A register big enough that a tiny review budget cannot finish it."""
    for i in range(reviews):
        t.put(f"team/{TEAM}/review/pr-{i}.md",
              "---\ntype: Review\nrequired: alice\n---\n")
        t.put(f"team/{TEAM}/review/pr-{i}/verdicts/alice.md",
              "---\ntype: Verdict\nreviewer: alice\nverdict: changes\n---\n")
    t.put(f"team/{TEAM}/_coord/forge/watch/o-r-1.md",
          "---\ntype: Watch\nurl: https://github.com/o/r/pull/1\nagent: bob\n---\n")


def _forge_of(t):
    import json
    agg = json.loads(t.store[f"team/{TEAM}/_coord/summaries.json"])
    return agg.get(projection.FORGE_KEY) or {}


def test_forge_is_BUILT_even_when_the_review_fold_exhausts_its_budget(monkeypatch):
    """The regression, asserted DETERMINISTICALLY: two distinct Deadlines.

    There are two couplings here and only one of them is a bug. The shared
    DEADLINE was the bug: forge inherited an expired one and never ran, which is
    what produced the live `scanned=None, total=None, rows=0` shell. Forge's
    COMPLETENESS legitimately follows the review fold's, because its
    responsibility map is derived from review rows — a partial review set cannot
    yield a complete forge view, and claiming otherwise would be the false-clear
    this codebase keeps hunting.

    ASSERTED ON THE DEADLINE OBJECTS, not on wall clock. My first version starved
    the review fold with a 0.001s budget and asserted the resulting section
    state. It passed alone and FAILED in the full suite, because whether a
    0.001s deadline actually expires mid-fold depends on machine timing and on
    what other tests have done to the clock — a flaky test guarding a starvation
    fix is worse than no test. The defect was precisely "one Deadline object
    passed twice", so that is what this asserts: two distinct objects, and the
    one forge receives is not already spent.
    """
    seen: list = []
    real_forge = projection.build_forge_projection
    real_reviews = projection.build_review_projection

    def spy_reviews(*a, **kw):
        seen.append(("reviews", kw.get("deadline")))
        return real_reviews(*a, **kw)

    def spy_forge(*a, **kw):
        seen.append(("forge", kw.get("deadline")))
        return real_forge(*a, **kw)

    monkeypatch.setattr(projection, "build_review_projection", spy_reviews)
    monkeypatch.setattr(projection, "build_forge_projection", spy_forge)
    monkeypatch.setattr(projection, "forge_budget", lambda: 30.0)

    t = FakeTransport()
    _seed(t, reviews=6)
    reconcile.reconcile(t, TEAM, now=_now(), today=_now()[:10], host="h")

    kinds = dict(seen)
    assert "reviews" in kinds and "forge" in kinds, f"a builder never ran: {seen}"
    assert kinds["forge"] is not kinds["reviews"], (
        "forge was handed the SAME Deadline object as the review fold — the "
        "shared-budget defect: whatever reviews spends, forge never gets")
    assert kinds["forge"].expired() is False, (
        "forge's deadline was already expired when it received it")


def test_forge_COMPLETES_when_the_review_fold_does(monkeypatch):
    """And the fix must not leave forge permanently floored: give reviews enough
    budget to finish and forge completes with them."""
    monkeypatch.setattr(projection, "build_budget", lambda: 60.0)
    monkeypatch.setattr(projection, "forge_budget", lambda: 30.0)
    t = FakeTransport()
    _seed(t, reviews=4)
    reconcile.reconcile(t, TEAM, now=_now(), today=_now()[:10], host="h")
    forge = _forge_of(t)
    assert forge.get("complete") is True, f"forge never converges: {forge}"
    assert forge.get("responsible") == {"o-r-1": ["bob"]}


def test_the_review_fold_still_gets_ITS_budget(monkeypatch):
    """The other direction: decoupling must not quietly shrink reviews."""
    monkeypatch.setattr(projection, "build_budget", lambda: 60.0)
    monkeypatch.setattr(projection, "forge_budget", lambda: 0.001)
    t = FakeTransport()
    _seed(t, reviews=6)
    reconcile.reconcile(t, TEAM, now=_now(), today=_now()[:10], host="h")

    import json
    agg = json.loads(t.store[f"team/{TEAM}/_coord/summaries.json"])
    reviews = agg.get(projection.REVIEWS_KEY) or {}
    assert reviews.get("complete") is True, (
        f"the review fold lost its own budget to the split: {reviews}")


def test_the_two_budgets_are_independent_knobs():
    """A shared default would reintroduce the coupling by the back door."""
    assert projection.DEFAULT_FORGE_BUDGET != projection.DEFAULT_BUILD_BUDGET
    assert projection.forge_budget() == projection.DEFAULT_FORGE_BUDGET
    assert projection.build_budget() == projection.DEFAULT_BUILD_BUDGET


# --- coord-boss ruling 30be1f8e (2026-09-05): forge lists the parent feedback dir once and per-PR only for the PRs
# that appear there; an empty or failed parent listing falls back to the per-PR loop (a listing cannot prove absence).

def _counting(t):
    calls = []
    orig = t.list_dir
    def list_dir(prefix):
        calls.append(prefix); return orig(prefix)
    t.list_dir = list_dir
    return calls


def _rows(*slugs):
    return [{"artifact": s, "requested_by": "alice"} for s in slugs]


def test_forge_lists_only_the_prs_that_have_a_feedback_dir_when_the_parent_listing_names_them():
    t = FakeTransport()
    t.put(f"team/{TEAM}/_coord/forge/feedback/o-r-1/f1.md", "---\nauthor: bob\n---\n")
    t.put(f"team/{TEAM}/_coord/forge/feedback/o-r-3/f2.md", "---\nauthor: carol\n---\n")
    calls = _counting(t)
    sec = projection.build_forge_projection(t, TEAM, now=_now(), review_rows=_rows("o-r-1", "o-r-2", "o-r-3", "o-r-4"),
                                            reviews_complete=True, prior=None, deadline=reconcile.Deadline.open(None))
    assert sec["complete"] is True and sorted(sec["responsible"]) == ["o-r-1", "o-r-2", "o-r-3", "o-r-4"]
    assert sorted(sec["feedback"]) == ["o-r-1", "o-r-3"] and sec["feedback"]["o-r-3"][0]["author"] == "carol"
    fb_calls = [c for c in calls if "/forge/feedback/" in c]
    assert fb_calls == [f"team/{TEAM}/_coord/forge/feedback/", f"team/{TEAM}/_coord/forge/feedback/o-r-1/",
                        f"team/{TEAM}/_coord/forge/feedback/o-r-3/"]          # parent once; o-r-2 and o-r-4 never listed


def test_an_empty_parent_listing_proves_nothing_so_every_pr_is_still_listed():
    t = FakeTransport()                                                          # no feedback anywhere
    calls = _counting(t)
    sec = projection.build_forge_projection(t, TEAM, now=_now(), review_rows=_rows("o-r-1", "o-r-2"),
                                            reviews_complete=True, prior=None, deadline=reconcile.Deadline.open(None))
    assert sec["complete"] is True and sec["feedback"] == {}
    fb_calls = [c for c in calls if "/forge/feedback/" in c]
    assert fb_calls == [f"team/{TEAM}/_coord/forge/feedback/", f"team/{TEAM}/_coord/forge/feedback/o-r-1/",
                        f"team/{TEAM}/_coord/forge/feedback/o-r-2/"]          # fallback: per-PR loop unchanged


def test_a_failing_parent_listing_falls_back_to_the_per_pr_loop_and_reports_what_the_loop_reports():
    t = FakeTransport()
    t.put(f"team/{TEAM}/_coord/forge/feedback/o-r-1/f1.md", "---\nauthor: bob\n---\n")
    orig = t.list_dir
    def list_dir(prefix):
        if prefix.endswith("/forge/feedback/"):
            from coord_engine.transport import TransportError
            raise TransportError("parent boom")
        return orig(prefix)
    t.list_dir = list_dir
    sec = projection.build_forge_projection(t, TEAM, now=_now(), review_rows=_rows("o-r-1", "o-r-2"),
                                            reviews_complete=True, prior=None, deadline=reconcile.Deadline.open(None))
    assert sec["complete"] is True and sorted(sec["feedback"]) == ["o-r-1"]   # per-PR listings answered; nothing hidden
