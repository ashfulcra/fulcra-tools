"""`tell --closes` — a reply closes the directive it answers.

Measured 2026-08-08: 912 of 919 proposed board items were dispatch residue.
`respond` has always closed, and a spread sample of 25 across eight owners found
ZERO response shards -- nobody was failing to comply. Everyone replies with
`tell`, which opened a NEW directive instead of closing the one it answered, so
every exchange netted +2 open items instead of 0.

The mechanism is `--closes`; the cure is the breadcrumb printing that command
with the slug already filled in, at the moment the closing move is wanted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from coord_engine import cli, okf, tasks
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
PINNED_NOW = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _args(**kw):
    ns = argparse.Namespace(team=TEAM, assignee="them", title="my reply",
                            priority="P2", workstream=None, summary="s",
                            next=None, sender="me", closes=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _seed_directive(t, slug="original-ask-abc123"):
    _, content = tasks.new_task_doc(
        "original ask", now="2026-08-07T00:00:00Z", status="proposed",
        priority="P1", owner="them", assignee="me", kind="directive")
    t.put(cli._task_path(TEAM, slug), content)
    return slug


def test_a_reply_closes_the_directive_it_answers():
    t = FakeTransport()
    slug = _seed_directive(t)
    assert cli.cmd_tell(_args(closes=slug), t) == 0
    doc = t.store[cli._task_path(TEAM, slug)]
    fm = okf.parse_frontmatter(doc)
    assert fm["status"] == "done", "the answered directive is still open"
    # apply_update records evidence as a note in the BODY, not frontmatter, and
    # it REFUSES a done without evidence ("done requires evidence"), so this
    # also pins that the close cannot degrade into a bare status flip.
    assert "answered by" in doc and "my-reply-" in doc, (
        "closure must name WHICH reply answered it — the artifact, not a flag"
    )


def test_without_closes_nothing_is_closed():
    """Non-vacuity: the flag is what closes, not the act of telling."""
    t = FakeTransport()
    slug = _seed_directive(t)
    assert cli.cmd_tell(_args(), t) == 0
    fm = okf.parse_frontmatter(t.store[cli._task_path(TEAM, slug)])
    assert fm["status"] == "proposed"


def test_an_unresolvable_slug_fails_LOUD_and_closes_nothing(capsys):
    """Ghost-close is the failure `respond` already guards: a close written
    against a name nobody owns leaves the real directive open forever while the
    sender believes it handled."""
    t = FakeTransport()
    rc = cli.cmd_tell(_args(closes="a-title-not-a-slug"), t)
    assert rc == 1
    err = capsys.readouterr().err
    assert "NOTHING was closed" in err
    assert "hash-suffixed slug" in err, "the message must say how to get it right"


def test_the_reply_survives_a_failed_close():
    """Durable-first: the reply stands even when the close fails, so the row
    stays visibly open rather than closed with no answer behind it."""
    t = FakeTransport()
    cli.cmd_tell(_args(closes="nope"), t)
    replies = [p for p in t.store if p.startswith(f"team/{TEAM}/task/my-reply-")]
    assert replies, "the reply was discarded when the close failed"


def test_the_SENDER_is_never_handed_the_closing_command(capsys):
    """Surface matters, and my first version got it wrong (coord-opus-worker).

    `_replies_breadcrumb` prints on the SENDER's terminal. A closing command
    there is handed to the one agent with NO standing to close the row —
    runnable, plausible, and it would close a directive nobody answered. That is
    the ghost-closure `respond` fails loud to prevent.
    """
    t = FakeTransport()
    cli.cmd_tell(_args(), t)
    out = capsys.readouterr().out
    assert "--closes" not in out, (
        "the dispatch path offered the sender a command that would close their "
        "own unanswered directive"
    )


def test_the_RECIPIENT_sees_the_closing_command_with_the_slug(capsys):
    """The cure belongs where the recipient reads the ask with the slug on
    screen. NOT `queue`: its text output is a byte-identical contract for shell
    consumers on BOTH streams, pinned by two golden tests that caught the
    attempt. `needs-me` is the other such surface and carries no pin."""
    cli.print_close_hint({"kind": "directive", "owner": "them",
                          "id": "the-ask-abc123", "team": TEAM})
    out = capsys.readouterr().out
    assert "--closes the-ask-abc123" in out, "the slug must be filled in"
    assert f"tell {TEAM} them" in out, "the reply must be addressed to the ASKER"


def test_a_broadcast_and_a_plain_task_offer_no_close(capsys):
    """A broadcast has no single row to close; a plain task is not an ask."""
    cli.print_close_hint({"kind": "directive", "owner": "*",
                          "id": "all-hands", "team": TEAM})
    cli.print_close_hint({"kind": "task", "owner": "them",
                          "id": "some-task", "team": TEAM})
    assert "--closes" not in capsys.readouterr().out
