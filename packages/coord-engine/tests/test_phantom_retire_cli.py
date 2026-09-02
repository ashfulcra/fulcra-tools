"""`task retire-phantom` — the verb, driven at the real entry point.

A passing test on `phantom.retirement_decision` says nothing about whether the
CLI calls it. I have shipped an unwired decision function twice, so these drive
`cli.main([...])` and assert on what lands in the store.

Retiring is not discharging: the record says the backing document is absent, and
must never read as "the work was done".
"""
from __future__ import annotations

import json

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

BUS = json.dumps({"data_type": "MomentAnnotation/test-bus",
                  "api_version": "v1alpha1"})


class BusTransport(FakeTransport):
    """FakeTransport that can actually publish records. Plain FakeTransport
    cannot, and a retirement whose close event fails is deliberately NOT a
    success — see test_a_failed_close_event_is_NOT_reported_as_success."""

    def __init__(self):
        super().__init__()
        self.emitted: list[str] = []

    def record_write(self, data_type, api_version, note, sender, **kw):
        self.emitted.append(note)
        return True


def _team(*, with_others: bool = True) -> BusTransport:
    t = BusTransport()
    t.put("team/r/_coord/bus-v3/records.json", BUS)
    if with_others:
        for n in ("kept-a", "kept-b", "kept-c"):
            t.put(f"team/r/task/{n}.md", "---\ntype: Task\nstatus: proposed\n---\n")
    return t


def _records(t):
    return [p for p in t.store if "/_coord/retired/" in p]


def test_retires_a_slug_absent_from_a_listing_that_returned_entries(capsys):
    t = _team()
    rc = cli.main(["task", "retire-phantom", "r", "gone", "--agent", "alice"],
                  transport=t)
    assert rc == 0, capsys.readouterr()
    recs = _records(t)
    assert len(recs) == 1 and recs[0].endswith("gone.md"), recs


def test_the_record_carries_BOTH_the_absence_and_the_same_pass_control():
    t = _team()
    cli.main(["task", "retire-phantom", "r", "gone", "--agent", "alice"], transport=t)
    doc = t.store[_records(t)[0]]
    assert "absent from a listing" in doc
    assert "listing returned 3 entries" in doc


def test_the_record_says_ABSENT_not_DONE():
    """The single most important property: a future reader must not read this
    as the work having been completed."""
    t = _team()
    cli.main(["task", "retire-phantom", "r", "gone", "--agent", "alice"], transport=t)
    doc = t.store[_records(t)[0]].lower()
    assert "absent" in doc
    assert "not that its work was done" in doc or "not discharged" in doc


def test_REFUSES_when_the_slug_is_actually_present(capsys):
    t = _team()
    t.put("team/r/task/kept-a.md", "---\ntype: Task\n---\n")
    rc = cli.main(["task", "retire-phantom", "r", "kept-a", "--agent", "alice"],
                  transport=t)
    assert rc != 0
    assert not _records(t), "retired a task whose document exists"
    assert "not a phantom" in capsys.readouterr().err.lower()


def test_REFUSES_on_an_EMPTY_listing(capsys):
    """UNKNOWN is not absence. An empty task dir would otherwise retire
    everything at once."""
    t = _team(with_others=False)
    rc = cli.main(["task", "retire-phantom", "r", "gone", "--agent", "alice"],
                  transport=t)
    assert rc != 0
    assert not _records(t)


def test_REFUSES_when_the_listing_RAISES(capsys):
    """The raising listing is the whole evidence base. If it raises we know
    nothing, and a retirement on no knowledge drops a live obligation."""
    t = _team()

    def _boom(prefix):
        raise OSError("store unavailable")

    t.list_dir = _boom  # type: ignore[assignment]
    rc = cli.main(["task", "retire-phantom", "r", "gone", "--agent", "alice"],
                  transport=t)
    assert rc != 0
    assert not _records(t), "retired an obligation on an unreadable store"


def test_it_emits_a_close_event_so_the_FOLD_retires_it():
    """The demotion half. The fold's open set is built from directive events and
    closed by response events, so the close event IS the demotion — no new
    annotation channel, and no enumeration added to the fold path."""
    seen = []

    class _Bus(FakeTransport):
        def record_write(self, data_type, api_version, note, sender, **kw):
            seen.append(note)
            return True

    t = _Bus()
    t.put("team/r/_coord/bus-v3/records.json", BUS)
    for n in ("kept-a", "kept-b"):
        t.put(f"team/r/task/{n}.md", "---\ntype: Task\n---\n")
    cli.main(["task", "retire-phantom", "r", "gone", "--agent", "alice"], transport=t)
    assert seen, "no event emitted — the fold will keep the obligation open forever"
    assert any("gone" in str(n) for n in seen), seen


def test_a_failed_close_event_is_NOT_reported_as_success(capsys):
    """The record can land while the fold stays open. That is a PARTIAL result
    and must not exit 0 — otherwise the obligation is still in the fold while
    the operator has been told it was retired."""
    t = FakeTransport()          # deliberately cannot publish records
    t.put("team/r/_coord/bus-v3/records.json", BUS)
    for n in ("kept-a", "kept-b"):
        t.put(f"team/r/task/{n}.md", "---\ntype: Task\n---\n")
    rc = cli.main(["task", "retire-phantom", "r", "gone", "--agent", "alice"],
                  transport=t)
    assert rc != 0, "a record-written-but-fold-open result claimed success"
    assert _records(t), "the durable record should still have landed"
    assert "fold NOT updated" in capsys.readouterr().err
