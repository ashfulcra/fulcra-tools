"""A single transient UNKNOWN must not cost the whole fleet the forge section.

MEASURED (coord-maintainer, 2026-08-10, live store): the review fold landed at
222/223 — ONE slug UNKNOWN in one pass — while every one of the 223 resolved
fine when scanned individually with budget excluded. There was no broken
directory. `complete` is literally `unknown == 0`, so one transient read denied
the whole section, and because the forge section's completeness follows this
one, it denied forge to every consumer until a later pass happened to converge.

coord-boss WITHDREW the alternative (a tolerance that calls one-short complete)
on the grounds that it manufactures exactly the false-clear this codebase hunts.
The direction taken instead: re-scan the small remainder ONCE inside the same
pass. A transient converts; a real failure stays UNKNOWN and the section stays
honestly incomplete.

These drive `build_review_projection` end to end with a slug that fails ONCE and
then succeeds — a helper-level test cannot see a retry that never fires, which
is the whole class of defect this week kept producing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from coord_engine import projection
from coord_engine.budget import Deadline
from coord_engine_test_helpers import FakeTransport

TEAM = "r"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(t, n):
    for i in range(n):
        t.put(f"team/{TEAM}/review/pr-{i}.md",
              "---\ntype: Review\nrequired: alice\n---\n")
        t.put(f"team/{TEAM}/review/pr-{i}/verdicts/alice.md",
              "---\ntype: Verdict\nreviewer: alice\nverdict: changes\n---\n")


class _FlakyRead(FakeTransport):
    """Fails the FIRST read of one slug's review doc, then behaves normally.

    That is the shape of the live failure: a transient, not a broken directory.
    """

    def __init__(self, flaky_slug, *, fail_times=1):
        super().__init__()
        self.flaky = f"team/{TEAM}/review/{flaky_slug}.md"
        self.left = fail_times
        self.reads_of_flaky = 0

    def read(self, path):
        if path == self.flaky:
            self.reads_of_flaky += 1
            if self.left > 0:
                self.left -= 1
                return None          # `read` returns None on failure by contract
        return super().read(path)


def _build(t, prior=None, budget=120.0):
    return projection.build_review_projection(
        t, TEAM, now=_now(), prior=prior, settled_index=set(),
        deadline=Deadline.open(budget))


def test_a_TRANSIENT_unknown_is_retried_and_the_pass_completes():
    """The regression. One slug's doc read fails once; the retry converts it and
    the section is complete in the SAME pass, rather than denying forge until
    some later pass happens to succeed."""
    t = _FlakyRead("pr-3")
    _seed(t, 8)
    sec = _build(t)
    assert t.reads_of_flaky >= 2, (
        f"the failing slug was never re-scanned: reads={t.reads_of_flaky}")
    assert sec["complete"] is True, (
        f"a single transient still denied the whole section: "
        f"scanned={sec['scanned']}/{sec['total']}")
    assert sec["scanned"] == sec["total"] == 8
    assert len(sec["rows"]) == 8
    assert len([r for r in sec["rows"] if r["name"] == "pr-3"]) == 1, (
        "the retried slug was duplicated in rows")


def test_a_PERSISTENT_failure_stays_unknown_and_the_section_stays_incomplete():
    """The other direction, and the one that matters most: the retry must not
    become a way to launder a real failure into a clean section."""
    t = _FlakyRead("pr-3", fail_times=99)
    _seed(t, 8)
    sec = _build(t)
    assert sec["complete"] is False, (
        "a persistently unreadable slug was reported as a complete section — "
        "the false-clear this retry was explicitly not allowed to create")
    assert sec["scanned"] == 7 and sec["total"] == 8


def test_a_LARGE_remainder_is_NOT_retried():
    """A crowd of UNKNOWNs means the budget ran out, not that reads flickered.
    Re-scanning it would spend the next section's time redoing work that simply
    needs another pass."""
    t = _FlakyRead("pr-0", fail_times=99)
    # Make several slugs fail by pointing the flaky path at a shared prefix:
    # simplest honest construction is a transport that fails many docs.
    class _ManyFail(FakeTransport):
        def __init__(self):
            super().__init__()
            self.retried = 0
        def read(self, path):
            if any(path.endswith(f"/pr-{i}.md") for i in range(5)):
                self.retried += 1
                return None
            return super().read(path)
    t2 = _ManyFail()
    _seed(t2, 8)
    sec = _build(t2)
    assert sec["complete"] is False
    # 5 failures > RETRY_UNKNOWN_MAX, so each was read exactly once.
    assert t2.retried == 5, (
        f"a large remainder was retried anyway: {t2.retried} reads for 5 slugs")


def test_the_retry_uses_the_SAME_deadline_OBJECT_as_the_main_scan(monkeypatch):
    """PR 599's lesson, pinned on the object rather than on a timing outcome.

    My first version of this test opened the fold with an already-spent deadline
    and asserted the section came back incomplete. It PASSED against a mutant
    that gave the retry its own fresh 999s budget — because an expired deadline
    trips `budget_cut` in the main loop, which blocks the retry before the
    deadline question is ever reached. The test could not distinguish the thing
    its name promised, which is the exact defect class this week kept producing.

    The property is object identity: a retry that opens its own budget is the
    shared-budget defect wearing a retry hat (599: one Deadline handed to two
    consumers starved the second). So assert every scan — first pass and retry
    alike — receives the SAME object.
    """
    seen = []
    real = projection._scan_review_slug

    def spy(transport, team, slug, entry, *, now, deadline):
        seen.append((slug, id(deadline)))
        return real(transport, team, slug, entry, now=now, deadline=deadline)

    monkeypatch.setattr(projection, "_scan_review_slug", spy)
    t = _FlakyRead("pr-3")
    _seed(t, 8)
    sec = _build(t)

    assert sec["complete"] is True, "the retry did not fire, so this proves nothing"
    retried = [d for slug, d in seen if slug == "pr-3"]
    assert len(retried) == 2, f"expected one retry of pr-3, got {len(retried)}"
    assert retried[0] == retried[1], (
        "the retry received a DIFFERENT Deadline object than the first scan — "
        "a retry with its own budget is the 599 shared-budget defect again")
    assert len({d for _, d in seen}) == 1, (
        "more than one Deadline object was in play across the pass")


def test_the_retry_cap_is_a_named_constant_not_a_literal():
    assert projection.RETRY_UNKNOWN_MAX == 3
