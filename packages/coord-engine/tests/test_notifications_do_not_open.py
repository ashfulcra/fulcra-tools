"""A message that asks for nothing must not open an obligation.

MEASURED on the live bus (coord-maintainer, 2026-08-11): 1250 proposed board
rows, of which 1239 carried `kind:directive`. They are delivered MESSAGES, not
proposals. Two agents authored 79% of them. Nothing was wrong with anyone's
discipline — `tell` mints every message as `proposed`, and only the RECIPIENT
can close it, so a report or an acknowledgement becomes a permanent open row
that its assignee cannot discharge because there is nothing to do.

The ratchet is worse than a leak. A reply sent with `--closes` closes its parent
correctly AND mints a fresh open row back at the sender, so two agents who both
behave perfectly still net one permanent open row per exchange. Verified against
two of my own replies, both of which closed their parents and both of which were
sitting open in the pile at the time of measurement.

This is Ruling 1's sibling one plane over (PR 561: a merged PR closes its review
as an ARTIFACT of the merge). Closure belongs to the terminal event, not to a
separate discipline step nobody performs. A notification's terminal event is its
DELIVERY — so it is born closed.

These drive `cli.main(["tell", ...])` end to end, because the defect is in what
the delivery path WRITES, and a helper-level test would not see the row that
lands on the board.
"""

from __future__ import annotations

import json

import pytest

from coord_engine import cli, model, okf, tasks
from coord_engine_test_helpers import FakeTransport

TEAM = "r"


def _send(monkeypatch, *extra, sender="alice", assignee="bob"):
    monkeypatch.setenv("FULCRA_COORD_AGENT", sender)
    t = FakeTransport()
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          json.dumps({"data_type": "X/1", "api_version": "v1alpha1"}))
    rc = cli.main(["tell", TEAM, assignee, "a message", "-s", "body",
                   "--from", sender, *extra], transport=t)
    docs = {p: v for p, v in t.store.items()
            if p.startswith(f"team/{TEAM}/task/") and p.endswith(".md")}
    return rc, docs


def _fm(docs):
    assert len(docs) == 1, f"expected exactly one task doc, got {list(docs)}"
    path, content = next(iter(docs.items()))
    return okf.parse_frontmatter(content) or {}, content


def test_an_FYI_is_delivered_but_never_enters_the_open_pile(monkeypatch):
    """THE regression. The row must exist and be durable — a bodiless note is
    not allowed on this bus — but it must not be an open obligation."""
    rc, docs = _send(monkeypatch, "--fyi")
    assert rc == 0
    fm, content = _fm(docs)
    assert fm["status"] not in model.OPEN_STATUSES, (
        f"an FYI opened an obligation the recipient can never discharge: "
        f"status={fm['status']!r}")
    assert fm["status"] in model.TERMINAL_STATUSES
    assert "kind:directive" in (fm.get("tags") or []), (
        "the FYI stopped being a directive doc — delivery must be unchanged")
    assert fm["assignee"] == "bob" and fm["owner"] == "alice"


def test_an_FYI_still_records_WHY_it_is_closed(monkeypatch):
    """A row born terminal with no reason is exactly the silent-state problem
    this codebase keeps fixing. The doc must say what happened."""
    _rc, docs = _send(monkeypatch, "--fyi")
    _fm_, content = _fm(docs)
    assert "no action was requested" in content, (
        f"a born-closed row carries no reason: {content!r}")


def test_an_ORDINARY_tell_still_opens(monkeypatch):
    """The other direction, and the one that matters most: a real ask must
    still become a real obligation. A flag that quietly closed everything
    would erase the fleet's work tracking."""
    _rc, docs = _send(monkeypatch)
    fm, _ = _fm(docs)
    assert fm["status"] == "proposed", (
        f"an ordinary directive stopped opening: status={fm['status']!r}")
    assert fm["status"] in model.OPEN_STATUSES


def test_the_FYI_row_is_INVISIBLE_to_the_open_board(monkeypatch):
    """The measurable outcome coord-boss asked for: board legibility. Status is
    the mechanism; staying out of the board fold is the POINT, so assert the
    point rather than only the mechanism."""
    _rc, docs = _send(monkeypatch, "--fyi")
    fm, _ = _fm(docs)
    row = {"status": fm["status"]}
    assert row["status"] not in model.OPEN_STATUSES


# --- the creation-side hole this change had to close first --------------------

def test_a_doc_CREATED_terminal_requires_evidence():
    """`apply_update` has always enforced "done requires evidence", but only on
    the UPDATE path — a doc could be BORN terminal carrying no reason at all.
    Creating terminal rows is precisely what the notification path does, so the
    rule has to hold at both entry points or it does not hold."""
    with pytest.raises(tasks.TaskError, match="requires evidence"):
        tasks.new_task_doc("t", now="2026-08-11T00:00:00Z", status="done")
    with pytest.raises(tasks.TaskError, match="requires reason"):
        tasks.new_task_doc("t", now="2026-08-11T00:00:00Z", status="abandoned")


def test_blank_evidence_does_not_satisfy_the_rule():
    """Whitespace is not a reason."""
    with pytest.raises(tasks.TaskError):
        tasks.new_task_doc("t", now="2026-08-11T00:00:00Z", status="done",
                           evidence="   ")


def test_creating_an_OPEN_doc_still_needs_no_evidence():
    """The rule must bind terminal creation only — every ordinary dispatch on
    this bus creates a proposed row with no evidence and must keep working."""
    _slug, content = tasks.new_task_doc("t", now="2026-08-11T00:00:00Z")
    assert "status: proposed" in content


def test_a_terminal_doc_records_its_reason_in_the_body():
    _slug, content = tasks.new_task_doc(
        "t", now="2026-08-11T00:00:00Z", status="done", evidence="because X")
    assert "created done (evidence: because X)" in content
    _slug, content = tasks.new_task_doc(
        "t", now="2026-08-11T00:00:00Z", status="abandoned", evidence="because Y")
    assert "created abandoned (reason: because Y)" in content, (
        "an abandoned doc labelled its reason as 'evidence' — the update path "
        "distinguishes them and creation must agree")
