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
    """The regression, and the property is BUILT — not `complete`.

    There are two couplings here and only one of them is a bug. The shared
    DEADLINE was the bug: forge inherited an expired one and never ran, which is
    what produced the live `scanned=None, total=None, rows=0` shell. Forge's
    COMPLETENESS legitimately follows the review fold's, because its
    responsibility map is derived from review rows — a partial review set cannot
    yield a complete forge view, and claiming otherwise would be the false-clear
    this codebase keeps hunting.

    So: with a starved review fold, forge must still RUN and produce its map,
    and must still say `complete: false`. My first version of this test asserted
    `complete is True` and conflated the two.
    """
    monkeypatch.setattr(projection, "build_budget", lambda: 0.001)
    monkeypatch.setattr(projection, "forge_budget", lambda: 30.0)
    t = FakeTransport()
    _seed(t, reviews=25)
    reconcile.reconcile(t, TEAM, now=_now(), today=_now()[:10], host="h")

    forge = _forge_of(t)
    assert forge.get("responsible") == {"o-r-1": ["bob"]}, (
        f"forge inherited an exhausted deadline and never ran — the live "
        f"never-built shell: {forge}")
    assert forge.get("complete") is False, (
        "a partial review set must still floor forge; completeness follows the "
        "rows it is derived from")


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
