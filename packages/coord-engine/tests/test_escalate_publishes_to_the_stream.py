"""An engine-minted vacancy must reach the plane folds actually read.

coord-maintainer, 2026-08-23T06:50Z, is the incident. They tested whether a
ROLE VACANT row existed by reading their `owed` fold, saw nothing, and concluded
the alarm had gone silent on a role 44 hours past its SLA. The row existed --
minted 00:41:50Z and addressed to them. The obligation had simply never been
published to the event plane, because `cmd_escalate` wrote the document with a
bare `transport.write` and the only "emit" in its whole body is `emit_envelope`,
a stdout summary line.

Under the FILE plane this was invisible: `needs-me` enumerated task docs, so it
found engine-written ones. Under the STREAM plane a fold's `open` set is built
ONLY from directive events, so a P1 minted by the engine entered nobody's fold.

The second half of the same incident is the OUTPUT: `escalated=0` collapsed
"everyone is attending", "the scan could not see far enough", and "already
escalated today" into one integer, and a careful reader running the right
experiment drew a confident wrong conclusion from it.
"""

from __future__ import annotations

import json

from coord_engine import cli, records
from coord_engine_test_helpers import FakeTransport

ROLE_DOC = "---\ntype: Role\nsla_hours: 12\nmaintainer: alice\n---\n"
STALE_LEASE = ("---\ntype: Lease\nagent: bob\n"
               "timestamp: 2020-01-01T00:00:00Z\n---\n")
BUS_CONFIG = json.dumps({"data_type": "MomentAnnotation/test-bus",
                         "api_version": "v1alpha1"})


class _BusTransport(FakeTransport):
    """FakeTransport plus a record sink, so emitted events are inspectable."""

    def __init__(self):
        super().__init__()
        self.records: list[dict] = []

    def record_write(self, data_type, api_version, note, sender, **kw):
        self.records.append({"note": note, "sender": sender})
        return True


def _team(*, with_bus: bool = True) -> _BusTransport:
    t = _BusTransport()
    t.put("team/r/roles/reviewer.md", ROLE_DOC)
    t.put("team/r/roles/reviewer/leases/bob.md", STALE_LEASE)
    if with_bus:
        t.put("team/r/_coord/bus-v3/records.json", BUS_CONFIG)
    return t


def _events(t: _BusTransport) -> list[dict]:
    out = []
    for r in t.records:
        ev = records.parse_payload(r["note"])
        if ev is not None:
            out.append(ev)
    return out


def test_a_minted_vacancy_is_published_as_a_directive_event():
    """THE regression: the document alone is not delivery under the stream."""
    t = _team()
    assert cli.main(["escalate", "r"], transport=t) == 0
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert len(evs) == 1, (
        "the vacancy P1 was written but never published — no fold will open it")
    ev = evs[0]
    assert ev["to"] == "alice", "the event must reach the doc's assignee"
    assert ev["slug"].startswith("role-vacant-"), ev["slug"]
    assert ev.get("ptr") == f"task/{ev['slug']}.md"
    assert not ev.get("fyi"), "a vacancy asks for something — it is not an FYI"


def test_the_published_slug_is_the_document_slug():
    """A close is matched by slug, so an event naming a different one would open
    an obligation that nothing could ever discharge."""
    t = _team()
    cli.main(["escalate", "r"], transport=t)
    ev = [e for e in _events(t) if e["kind"] == "directive"][0]
    written = [p for p in t.store if "/task/role-vacant-" in p]
    assert len(written) == 1
    assert written[0].endswith(f"{ev['slug']}.md")


def test_a_suppressed_re_escalation_publishes_nothing():
    """The event rides the WRITE, not the sweep: a role already escalated today
    must not re-open the obligation every pass."""
    t = _team()
    cli.main(["escalate", "r"], transport=t)
    first = len([e for e in _events(t) if e["kind"] == "directive"])
    t.records.clear()
    cli.main(["escalate", "r"], transport=t)
    assert first == 1
    assert not [e for e in _events(t) if e["kind"] == "directive"], (
        "the repeat sweep re-published the vacancy — one alarm, one obligation")


def test_a_bus_that_is_absent_never_loses_the_vacancy(capsys):
    """The document is the durable obligation; the event is delivery. No bus
    must still mint the P1 — and must SAY the fold will not see it."""
    t = _team(with_bus=False)
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert [p for p in t.store if "/task/role-vacant-" in p], (
        "a missing bus config silently dropped the vacancy record")
    err = capsys.readouterr().err
    assert "file plane only" in err and "stream fold" in err


# --- the OUTPUT half: three states must not share one integer ---------------

def test_the_attendance_verdict_is_named_per_role(capsys):
    """UNKNOWN is not NOT-FOUND. With no reviews to scan, attendance cannot be
    established, and the sweep must say so rather than let `escalated=N` imply
    it looked and found nothing."""
    t = _team()
    cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err
    assert "reviewer attendance UNKNOWN-within-budget" in err, err


def test_a_found_holder_verdict_is_reported_as_found(capsys):
    t = _team()
    v = "team/r/review/some-pr/verdicts/abc--bob.md"
    t.put(v, "---\nverdict: approved\n---\n")
    t.mtimes[v] = cli._now().strftime("%Y-%m-%d %I:%M%p UTC")
    cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err
    assert "reviewer attendance FOUND" in err, err


def test_already_escalated_today_says_so_instead_of_going_silent(capsys):
    """THE reading that misled a careful agent: a silent skip plus `0 escalated`
    is indistinguishable from an alarm that failed to fire."""
    t = _team()
    cli.main(["escalate", "r"], transport=t)
    capsys.readouterr()
    cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err
    assert "already escalated today" in err, err
    assert "repeat suppressed" in err, err
