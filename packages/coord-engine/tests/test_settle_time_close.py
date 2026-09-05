"""Settle-time close — the DECISION verbs close their own request rows.

The residue this exists to prevent is a review that finished and a request row
that stayed open: 198 such rows were live in team/acme, 156 of them behind a
terminal marker. The scheduled sweep cleans that up; this closes the tap.

Only the decision verbs (`review close`, `review conclude`) do it. The fold and
projection settle writers are CACHES inside build paths — `projection.py` calls
`transport.write` exactly once in the whole module and never touches `task/` —
so making them mutate task state would turn a slow reconcile into an
unexplained task-state change. Those rows stay the sweep's job, permanently.

These tests exercise the WIRING, not the decision function. The last change in
this area passed thirteen unit tests with the command completely broken by a
NameError, because the decision function was tested and the actuator was not.
Removing either call below must fail this file.
"""
from __future__ import annotations

import argparse

import pytest

from coord_engine import cli, okf
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
SLUG = "some-pr-1a2b3c"
SHA = "a" * 40
ROW_ID = "review-request-some-pr-1a2b3c-deadbeef"


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    import datetime as _dt
    monkeypatch.setattr(cli, "_now", lambda: _dt.datetime(
        2026, 8, 8, 0, 0, tzinfo=_dt.timezone.utc))


def _with_review(t):
    t.put(cli._review_doc_path(TEAM, SLUG),
          okf.render_frontmatter({"type": "Review", "title": SLUG}) + "\n")
    t.put(cli._verdicts_prefix(TEAM, SLUG) + f"{SHA}--codex-reviewer.md", "x")
    return t


def _row(**kw):
    r = {"id": ROW_ID, "title": cli._REVIEW_REQUEST_TITLE_PREFIX + SLUG,
         "status": "proposed", "assignee": "codex-reviewer"}
    r.update(kw)
    return r


@pytest.fixture
def wired(monkeypatch):
    """Capture what settle-time close asks `task done` to close."""
    calls: list = []

    def fake_done(ns, transport):
        calls.append(ns)
        return 0

    monkeypatch.setattr(cli, "cmd_task_done", fake_done)
    return calls


def _rows(monkeypatch, rows, ok=True, reason=""):
    monkeypatch.setattr(cli, "_load_rows_status",
                        lambda t, team, **kw: (rows, ok, reason))


def _close_args(**kw):
    ns = argparse.Namespace(team=TEAM, slug=SLUG, merge_sha=SHA,
                            merged_at=None, reason=None, sender="tester")
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_review_close_closes_its_request_row(monkeypatch, wired):
    _rows(monkeypatch, [_row()])
    assert cli.cmd_review_close(_close_args(), _with_review(FakeTransport())) == 0
    assert [c.name for c in wired] == [ROW_ID]
    assert "MERGED" in wired[0].evidence, "the closure must record why"


def _with_unbound_review(t):
    """`review conclude` is only for rows no closure can bind evidence to, so
    the doc carries NO head and the verdict uses the legacy unbound naming."""
    t.put(cli._review_doc_path(TEAM, SLUG),
          okf.render_frontmatter({"type": "Review", "title": SLUG}) + "\n")
    t.put(cli._verdicts_prefix(TEAM, SLUG) + "codex-reviewer.md",
          okf.render_frontmatter({"verdict": "APPROVED",
                                  "reviewer": "codex-reviewer"}) + "\n")
    return t


def test_review_conclude_closes_its_request_row(monkeypatch, wired):
    _rows(monkeypatch, [_row()])
    ns = argparse.Namespace(team=TEAM, slug=SLUG, sender="tester", reason=None)
    assert cli.cmd_review_conclude(ns, _with_unbound_review(FakeTransport())) == 0
    assert [c.name for c in wired] == [ROW_ID]
    assert "concluded" in wired[0].evidence


def test_a_row_for_a_different_slug_is_left_alone(monkeypatch, wired):
    _rows(monkeypatch, [_row(title=cli._REVIEW_REQUEST_TITLE_PREFIX + "other")])
    assert cli.cmd_review_close(_close_args(), _with_review(FakeTransport())) == 0
    assert wired == [], "settle-time close must match its OWN slug only"


def test_an_already_terminal_row_is_not_closed_again(monkeypatch, wired):
    _rows(monkeypatch, [_row(status="done")])
    assert cli.cmd_review_close(_close_args(), _with_review(FakeTransport())) == 0
    assert wired == []


def test_a_degraded_rows_fold_closes_nothing_and_says_so(
        monkeypatch, wired, capsys):
    _rows(monkeypatch, [_row()], ok=False, reason="summaries unreadable")
    assert cli.cmd_review_close(_close_args(), _with_review(FakeTransport())) == 0
    assert wired == [], "a partial view of the rows must close none of them"
    err = capsys.readouterr().err
    assert "review residue" in err, "a silent skip rebuilds the backlog"


def test_unreadable_rows_do_not_fail_the_verified_closure(
        monkeypatch, wired, capsys):
    def boom(t, team, **kw):
        raise TransportError("rows down")
    monkeypatch.setattr(cli, "_load_rows_status", boom)
    rc = cli.cmd_review_close(_close_args(), _with_review(FakeTransport()))
    assert rc == 0, ("the marker is the durable truth and it was verified; "
                     "bookkeeping must not fail a real closure")
    assert "review residue" in capsys.readouterr().err


def test_a_failed_row_close_is_loud_and_still_does_not_fail_the_verb(
        monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_task_done", lambda ns, t: 3)
    _rows(monkeypatch, [_row()])
    rc = cli.cmd_review_close(_close_args(), _with_review(FakeTransport()))
    assert rc == 0
    assert "did NOT close" in capsys.readouterr().err
