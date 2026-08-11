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


# --- delivery must be UNCHANGED, which is the whole safety of this feature ----

class _RecordingTransport(FakeTransport):
    """Captures the companion bus events — the plane the recipient's queue
    actually reads. The plain FakeTransport has no ``record_write``, so tests
    built on it prove the DOC was written and say nothing about delivery."""

    def __init__(self):
        super().__init__()
        self.records_written: list[dict] = []

    def record_write(self, data_type, api_version, note, source,
                     recorded_at=None, tags=None):
        self.records_written.append({"note": note, "source": source})
        return True


def _send_recording(monkeypatch, *extra):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "alice")
    t = _RecordingTransport()
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          json.dumps({"data_type": "X/1", "api_version": "v1alpha1"}))
    rc = cli.main(["tell", TEAM, "bob", "a message", "-s", "body",
                   "--from", "alice", *extra], transport=t)
    return rc, t


def test_an_FYI_STILL_EMITS_the_companion_event_that_delivers_it(monkeypatch):
    """THE risk this feature had to clear, and the one I could not see with the
    plain fake transport.

    `directives.inbox` — the needs-me/inbox view — filters OUT terminal
    rows, which is correct and intended: an FYI is not work that needs
    attention. But delivery to the recipient's QUEUE rides the companion `v:1`
    bus event, not the board status. If closing the row also suppressed that
    event, an FYI would become an INVISIBLE message rather than a quiet one —
    silent loss, strictly worse than the obligation it was meant to remove.

    So assert the event is emitted for an FYI exactly as for an ordinary tell.
    """
    _rc, fyi = _send_recording(monkeypatch, "--fyi")
    _rc2, plain = _send_recording(monkeypatch)
    assert len(fyi.records_written) == 1, (
        f"an --fyi emitted no companion event — the message is not merely "
        f"quiet, it is UNDELIVERED: {fyi.records_written!r}")
    assert len(fyi.records_written) == len(plain.records_written), (
        "an --fyi and an ordinary tell no longer deliver identically")
    assert json.loads(fyi.records_written[0]["note"])["v"] == 1, (
        "the companion note is not the v:1 shape the queue filter keeps")


def test_an_FYI_is_correctly_ABSENT_from_the_needs_attention_view(monkeypatch):
    """The other half of the same property, asserted rather than assumed: the
    row must stay out of the work-that-needs-attention fold. Being delivered and
    being an obligation are different things, and this feature separates them."""
    from coord_engine import directives
    _rc, docs = _send(monkeypatch, "--fyi")
    fm, _ = _fm(docs)
    row = {"status": fm["status"], "assignee": fm["assignee"],
           "name": fm["id"], "priority": fm["priority"]}
    assert directives.inbox([row], {}, "bob") == [], (
        "an FYI showed up as work needing attention — the obligation it was "
        "supposed to stop creating")
    row_open = dict(row, status="proposed")
    assert directives.inbox([row_open], {}, "bob") != [], (
        "the control failed: an OPEN directive must still appear, or the "
        "assertion above proves nothing about status")


# --- mode is IDENTITY: the two must never share a path ------------------------

def _send_on(t, monkeypatch, *extra, title="a message"):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "alice")
    return cli.main(["tell", TEAM, "bob", title, "-s", "body",
                     "--from", "alice", *extra], transport=t)


def _rows(t):
    return {p: v for p, v in t.store.items()
            if p.startswith(f"team/{TEAM}/task/") and p.endswith(".md")}


def _recording():
    t = _RecordingTransport()
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          json.dumps({"data_type": "X/1", "api_version": "v1alpha1"}))
    return t


def test_FYI_THEN_ASK_does_not_swallow_the_real_ask(monkeypatch):
    """codex-reviewer, 605 r2 — the dangerous ordering.

    `payload`/`slug` were computed BEFORE `--fyi` was consulted, so the same text
    sent as a notification and then as a genuine ask landed on ONE path. The ask
    deduped onto the `done` row and emitted no companion event: real work,
    silently absent from BOTH the obligation plane and the recipient's event
    window. That is worse than the bug this feature fixes.
    """
    t = _recording()
    _send_on(t, monkeypatch, "--fyi")
    _send_on(t, monkeypatch)                     # same text, as a real ask

    docs = _rows(t)
    assert len(docs) == 2, (
        f"the ask collided with the notification on one path: {list(docs)}")
    statuses = sorted((okf.parse_frontmatter(c) or {})["status"]
                      for c in docs.values())
    assert statuses == ["done", "proposed"], (
        f"the two modes did not both survive: {statuses}")
    assert len(t.records_written) == 2, (
        "the real ask emitted no companion event — invisible to the recipient's "
        "queue, which is silent loss of genuine work")


def test_ASK_THEN_FYI_does_not_falsify_the_no_obligation_promise(monkeypatch):
    """The mirror ordering: the FYI deduped onto the `proposed` row, so the
    promise that a notification opens nothing was simply untrue."""
    t = _recording()
    _send_on(t, monkeypatch)
    _send_on(t, monkeypatch, "--fyi")

    docs = _rows(t)
    assert len(docs) == 2, (
        f"the notification collided with the open ask: {list(docs)}")
    assert sorted((okf.parse_frontmatter(c) or {})["status"]
                  for c in docs.values()) == ["done", "proposed"]


def test_an_ORDINARY_directive_slug_is_UNCHANGED_by_this_feature():
    """Only the notification case may append a marker. If ordinary hashes moved,
    every slug already in the store would stop deduping and the whole fleet
    would re-deliver its history."""
    base = cli._directive_payload("t", "s", "n", "bob")
    assert base == ("t", "s", "n", "bob"), (
        f"the ordinary payload shape changed: {base!r}")
    assert cli._directive_payload("t", "s", "n", "bob", fyi=True) != base


def test_mode_is_read_from_a_TAG_not_inferred_from_status():
    """A COMPLETED ordinary directive is terminal too. Inferring mode from status
    would make every finished ask start reading as a notification — the same
    two-states-into-one collapse this fix removes."""
    _s1, fyi_doc = tasks.new_task_doc(
        "t", now="2026-08-11T00:00:00Z", status="done", evidence="e",
        assignee="bob", summary="s", next_action="n", kind="directive", fyi=True)
    _s2, done_ask = tasks.new_task_doc(
        "t", now="2026-08-11T00:00:00Z", status="done", evidence="e",
        assignee="bob", summary="s", next_action="n", kind="directive")
    assert cli._doc_payload(fyi_doc) != cli._doc_payload(done_ask), (
        "a completed ASK and a notification computed the SAME identity — a "
        "finished ask would be mistaken for an FYI")


def test_later_REFUSES_fyi_rather_than_silently_losing_the_idea(monkeypatch, capsys):
    """codex-reviewer, 605 r2. `later` captures to @backlog — an audience the
    companion emitter skips — and the backlog fold shows OPEN rows. So a
    `later --fyi` would be born terminal, hidden from the backlog view, and
    delivered to nobody: the captured idea silently vanishes."""
    monkeypatch.setenv("FULCRA_COORD_AGENT", "alice")
    t = FakeTransport()
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          json.dumps({"data_type": "X/1", "api_version": "v1alpha1"}))
    rc = cli.main(["later", TEAM, "an idea", "-s", "body", "--fyi"], transport=t)
    err = capsys.readouterr().err
    assert rc != 0, "later --fyi was accepted; the captured idea is now invisible"
    assert "not meaningful" in err and "backlog" in err, (
        f"the refusal does not explain itself: {err!r}")
    assert not _rows(t), "a refused capture still wrote a doc"


def test_later_WITHOUT_fyi_still_works(monkeypatch):
    """The counterweight: the refusal must not break ordinary backlog capture."""
    monkeypatch.setenv("FULCRA_COORD_AGENT", "alice")
    t = FakeTransport()
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          json.dumps({"data_type": "X/1", "api_version": "v1alpha1"}))
    rc = cli.main(["later", TEAM, "an idea", "-s", "body"], transport=t)
    assert rc == 0 and len(_rows(t)) == 1


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
