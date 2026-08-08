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
