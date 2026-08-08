"""`review close` — closure as an ARTIFACT of merging, not an inference.

A merged PR leaves an immortal obligation: the register keeps asking for a
verdict on a head nobody will ever review again. `review gc` is deliberately NOT
the answer -- its predicate asks whether a HEAD is still alive, PR 551 raised
that bar on purpose, and "the PR merged" is a different question a liveness
probe cannot answer (coord-boss ruling 1, 2026-08-07).

The distinguishing property from `_write_settled_marker`: that one is a CACHE
and swallows its failures. This is a DURABLE RECORD, so a swallowed failure
would leave the row open while reporting closed.
"""

from __future__ import annotations

import argparse
import json

import pytest

from coord_engine import cli, okf
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
SLUG = "some-pr-1a2b3c"
SHA = "a" * 40
PINNED = "2026-08-08T00:00:00Z"


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    import datetime as _dt
    monkeypatch.setattr(cli, "_now", lambda: _dt.datetime(
        2026, 8, 8, 0, 0, tzinfo=_dt.timezone.utc))


def _args(**kw):
    ns = argparse.Namespace(team=TEAM, slug=SLUG, merge_sha=SHA,
                            merged_at=None, reason=None, sender="tester")
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _with_review(t):
    t.put(cli._review_doc_path(TEAM, SLUG),
          okf.render_frontmatter({"type": "Review", "title": SLUG}) + "\n")
    t.put(cli._verdicts_prefix(TEAM, SLUG) + f"{SHA}--codex-reviewer.md", "x")
    return t


def test_a_merged_review_is_closed_with_its_evidence():
    t = _with_review(FakeTransport())
    assert cli.cmd_review_close(_args(), t) == 0
    fm = okf.parse_frontmatter(t.store[cli._settled_marker_path(TEAM, SLUG)])
    assert fm["state"] == "MERGED"
    assert fm["merge_sha"] == SHA, "the marker must carry the evidence, not just a flag"
    assert fm["merged_at"] and fm["closed_by"] == "tester"
    assert fm["reason"], "a closure with no recorded reason is an inference"


@pytest.mark.parametrize("bad", ["a1b2c3d", "main", "", "ZZ" * 20, "a" * 39])
def test_closure_refuses_anything_that_is_not_a_full_sha(bad):
    """An abbreviation or a branch name is an assertion, not evidence."""
    t = _with_review(FakeTransport())
    assert cli.cmd_review_close(_args(merge_sha=bad), t) == 2
    assert cli._settled_marker_path(TEAM, SLUG) not in t.store


def test_closing_a_slug_that_does_not_exist_is_refused():
    """A write into a nonexistent entry succeeds silently and reads exactly
    like a closure."""
    assert cli.cmd_review_close(_args(slug="never-existed"), FakeTransport()) == 2


def test_an_unreadable_verdicts_prefix_is_UNKNOWN_not_closed():
    class Blind(FakeTransport):
        def list_dir(self, prefix):
            raise TransportError("listing down")
    rc = cli.cmd_review_close(_args(), Blind())
    assert rc == 1, "a fold that cannot look must not report a closure"


def test_a_swallowed_write_failure_must_not_report_success():
    """The property that separates this from the settled CACHE."""
    class WriteDrops(FakeTransport):
        def write(self, path, body):
            return None            # silently does nothing
    t = _with_review(WriteDrops())
    rc = cli.cmd_review_close(_args(), t)
    assert rc == 1, (
        "the write vanished and close reported success — the row is open and "
        "the register says otherwise"
    )


# --- the stale-marker case (codex 561 r1) -----------------------------------
#
# `.settled` is very often ALREADY occupied by the fold's APPROVED cache marker
# -- that is the normal state for a terminal review, which is exactly the review
# a merged PR has. So a presence-only read-back passes on the OLD content and
# the command reports a merge sha the durable record does not contain.
#
# Presence is not identity. That distinction is the whole point of this verb.

class WriteSilentlyDrops(FakeTransport):
    def write(self, path, body):
        if path.endswith(cli.SETTLED_MARKER):
            return None           # the write vanishes; everything else works
        return super().write(path, body)


def _preexisting_approved_marker(t):
    t.put(cli._settled_marker_path(TEAM, SLUG),
          okf.render_frontmatter({"schema": "review-settled/v1",
                                  "state": "APPROVED", "ts": "2026-08-01T00:00:00Z"}))


def test_a_dropped_write_over_an_APPROVED_cache_marker_is_caught():
    """The exact shape codex named: the marker exists, so presence passes."""
    t = _with_review(WriteSilentlyDrops())
    _preexisting_approved_marker(t)
    rc = cli.cmd_review_close(_args(), t)
    assert rc == 1, (
        "the write vanished, the stale APPROVED marker satisfied a presence "
        "check, and close reported success with a merge sha that is not in the "
        "durable record"
    )


def test_a_dropped_write_over_an_OLDER_merged_closure_is_caught():
    """Same defect one step subtler: state already says MERGED, so a
    state-only check would pass while the SHA is somebody else's."""
    t = _with_review(WriteSilentlyDrops())
    t.put(cli._settled_marker_path(TEAM, SLUG),
          okf.render_frontmatter({"schema": "review-settled/v1",
                                  "state": "MERGED", "merge_sha": "b" * 40,
                                  "ts": "2026-08-01T00:00:00Z"}))
    assert cli.cmd_review_close(_args(), t) == 1, (
        "state matched but the merge sha did not — the closure names the wrong "
        "commit"
    )


def test_closing_over_a_stale_marker_SUCCEEDS_when_the_write_lands():
    """Non-vacuity: a pre-existing marker must not itself block a real close,
    or the verb would be unusable on precisely the reviews it targets."""
    t = _with_review(FakeTransport())
    _preexisting_approved_marker(t)
    assert cli.cmd_review_close(_args(), t) == 0
    fm = okf.parse_frontmatter(t.store[cli._settled_marker_path(TEAM, SLUG)])
    assert fm["state"] == "MERGED" and fm["merge_sha"] == SHA


# --- same-SHA stale provenance (codex 561 r2) -------------------------------
#
# My r1 fix hand-picked `state` and `merge_sha`. But the verb's contract records
# merged_at, closed_by and reason too, so re-closing the SAME sha with corrected
# provenance could silently drop the write, match on the two fields I happened
# to check, and exit 0 while the requested evidence never landed.
#
# A hand-picked subset goes stale the moment a field is added. The comparison is
# now derived from the payload; these pin that.

@pytest.mark.parametrize("stale_field, stale_value", [
    ("merged_at", "2001-01-01T00:00:00Z"),
    ("closed_by", "somebody-else"),
    ("reason", "an older reason"),
])
def test_same_sha_but_stale_provenance_is_caught(stale_field, stale_value):
    """state and merge_sha both MATCH — only the provenance differs."""
    t = _with_review(WriteSilentlyDrops())
    marker = {"schema": "review-settled/v1", "state": "MERGED",
              "merge_sha": SHA, "merged_at": PINNED, "closed_by": "tester",
              "reason": "PR merged; the head will not be reviewed again",
              "ts": PINNED}
    marker[stale_field] = stale_value
    t.put(cli._settled_marker_path(TEAM, SLUG), okf.render_frontmatter(marker))

    rc = cli.cmd_review_close(_args(reason="PR merged; the head will not be "
                                           "reviewed again"), t)
    assert rc == 1, (
        f"{stale_field} was stale but state and merge_sha matched, so the "
        f"closure reported success with evidence that never landed"
    )


def test_the_check_is_derived_from_the_payload_not_a_hardcoded_list():
    """Non-staleness: every field the verb writes must be compared.

    If someone adds a field to the marker and the comparison keeps its own
    list, this verb silently stops verifying it — which is precisely how r1's
    fix was already incomplete when it shipped.
    """
    import inspect
    src = inspect.getsource(cli.cmd_review_close)
    assert "marker.items()" in src, (
        "the read-back comparison must enumerate the payload's own fields"
    )
