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


# --- delivery is not the same fact as document existence -------------------
#
# codex-reviewer, 682 r1. The first emit was attempted ONLY inside
# `if transport.read(dst) is None`. A sweep that wrote the document but could
# not emit — no bus config, a failed record write, a crash between the two —
# left the daily marker in place, and every later sweep short-circuited on that
# marker without ever reaching the emit again. The first failure was PERMANENT:
# the vacancy stayed invisible to every fold even after the bus came back, which
# is exactly the incident the emit was added to close.
#
# They reproduced it with the fixtures above: run once with no bus, add the
# config, run again -> `already escalated today` and zero events. Both runs rc 0.

def _add_bus(t: _BusTransport) -> None:
    t.put("team/r/_coord/bus-v3/records.json", BUS_CONFIG)


def test_a_vacancy_undelivered_while_the_bus_was_down_is_delivered_on_recovery(capsys):
    """THE regression codex-reviewer asked for."""
    t = _team(with_bus=False)
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert not _events(t), "no bus, so nothing should have been published yet"
    assert [p for p in t.store if "/task/role-vacant-" in p]

    _add_bus(t)
    capsys.readouterr()
    assert cli.main(["escalate", "r"], transport=t) == 0
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert len(evs) == 1, (
        "the bus recovered and the vacancy is still not on the stream — the "
        "first delivery failure was permanent")
    assert evs[0]["to"] == "alice"
    assert evs[0]["slug"].startswith("role-vacant-")
    assert "redelivery SUCCEEDED" in capsys.readouterr().err


def test_redelivery_reuses_the_original_slug_so_one_obligation_stays_one():
    """The fold keys `open` on the slug. A retry that minted a new slug would
    open a SECOND obligation nobody could discharge — which is why retrying is
    only safe because it is slug-idempotent."""
    t = _team(with_bus=False)
    cli.main(["escalate", "r"], transport=t)
    doc = [p for p in t.store if "/task/role-vacant-" in p][0]
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    ev = [e for e in _events(t) if e["kind"] == "directive"][0]
    assert doc.endswith(f"{ev['slug']}.md")
    assert len([p for p in t.store if "/task/role-vacant-" in p]) == 1


def test_once_delivered_a_later_sweep_does_not_republish():
    """Redelivery is driven by delivery STATE, not by running again."""
    t = _team()
    cli.main(["escalate", "r"], transport=t)
    assert len([e for e in _events(t) if e["kind"] == "directive"]) == 1
    t.records.clear()
    cli.main(["escalate", "r"], transport=t)
    cli.main(["escalate", "r"], transport=t)
    assert not [e for e in _events(t) if e["kind"] == "directive"]


def test_a_configured_bus_that_keeps_failing_is_counted_as_undelivered(capsys):
    """An unattended caller keys on rc. A retry that silently failed and then
    reported a clean sweep would be the 577 laundering defect again.

    The bus is CONFIGURED here and its writes fail — a real delivery failure,
    which is a different thing from a team that has no event plane at all."""
    t = _team()
    t.record_write = lambda *a, **k: False  # type: ignore[assignment]
    cli.main(["escalate", "r"], transport=t)
    capsys.readouterr()
    rc = cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err
    assert "redelivery FAILED" in err, err
    assert rc == 3, "a vacancy nobody's fold can see must not report rc 0"


def test_a_team_with_no_event_plane_is_not_an_incident_every_sweep(capsys):
    """No bus-v3 config is a deployment shape, not a delivery failure. Failing
    closed on it would make rc 3 permanent for file-plane-only teams, and an
    alarm that fires on every run is worth as much as no alarm."""
    t = _team(with_bus=False)
    assert cli.main(["escalate", "r"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["escalate", "r"], transport=t) == 0, (
        "a team with no event plane took rc 3 on a routine repeat sweep")
    assert "no event plane" in capsys.readouterr().err


def test_a_marker_from_an_engine_with_no_delivery_record_still_redelivers(capsys):
    """Markers written before delivery state existed carry no slug. Absent is
    UNKNOWN, never 'delivered' — the day's two candidate titles are probed to
    find the document that actually exists."""
    t = _team(with_bus=False)
    cli.main(["escalate", "r"], transport=t)
    # simulate a pre-delivery-state engine: drop the delivery record entirely
    for k in [p for p in list(t.store) if "escalation-delivery" in p]:
        del t.store[k]
    _add_bus(t)
    capsys.readouterr()
    assert cli.main(["escalate", "r"], transport=t) == 0
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert len(evs) == 1, (
        "a legacy marker with no delivery record was treated as delivered")


# --- the delivery marker is EVIDENCE, so it is validated like evidence ------
#
# codex-reviewer, 682 r2. `_read_escalation_delivery` returned any JSON object
# and callers tested `dstate.get("delivered")` by TRUTHINESS, so the malformed
# marker {"delivered": "false"} -- a non-empty string -- read as confirmed
# delivery. The sweep printed "event delivered", returned rc 0, and permanently
# suppressed the retry with no directive event anywhere. Same permanent loss the
# r1 and r2 fixes each closed, reached through a third door: a malformed record
# accepted as proof.

import pytest


def _delivery_path(role: str = "reviewer") -> str:
    from coord_engine import cli as _cli
    return _cli._escalation_delivery_path("r", role, _cli._now().strftime("%Y-%m-%d"))


def _mint_without_bus() -> _BusTransport:
    t = _team(with_bus=False)
    cli.main(["escalate", "r"], transport=t)
    return t


@pytest.mark.parametrize("payload", [
    '{"delivered": "false", "slug": "role-vacant-x", "to": "alice"}',
    '{"delivered": "true", "slug": "role-vacant-x", "to": "alice"}',
    '{"delivered": 1, "slug": "role-vacant-x", "to": "alice"}',
    '{"delivered": true}',
    '{"delivered": true, "slug": "", "to": "alice"}',
    '{"delivered": true, "slug": "role-vacant-x"}',
    '{"delivered": true, "slug": "role-vacant-x", "to": ""}',
    '{"delivered": true, "slug": 7, "to": "alice"}',
    'not json at all',
    '[]',
    '{}',
])
def test_a_malformed_delivery_marker_is_unknown_so_redelivery_runs(payload):
    """THE regression: none of these may be read as confirmed delivery."""
    t = _mint_without_bus()
    t.put(_delivery_path(), payload)
    _add_bus(t)
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert [e for e in _events(t) if e["kind"] == "directive"], (
        f"marker {payload!r} was accepted as proof of delivery — the vacancy is "
        f"permanently absent from the stream")


def test_a_wellformed_delivered_marker_is_still_honoured(capsys):
    """Validation must not turn every marker into UNKNOWN — that would
    re-publish the obligation on every sweep forever."""
    t = _team()
    cli.main(["escalate", "r"], transport=t)
    t.records.clear()
    capsys.readouterr()
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert not [e for e in _events(t) if e["kind"] == "directive"]
    assert "event delivered" in capsys.readouterr().err


def test_a_wellformed_not_delivered_marker_redelivers_with_its_slug():
    """delivered:false is VALID evidence — of non-delivery. Its slug must be
    reused rather than discarded, so the retry re-opens the same obligation."""
    t = _mint_without_bus()
    doc = [p for p in t.store if "/task/role-vacant-" in p][0]
    slug = doc.rsplit("/", 1)[1][:-3]
    t.put(_delivery_path(),
          json.dumps({"delivered": False, "slug": slug, "to": "alice"}))
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert len(evs) == 1 and evs[0]["slug"] == slug


# --- routing evidence comes from the DOCUMENT, never from the marker --------
#
# codex-reviewer, 682 r3. r2 validated the marker's SHAPE and then still routed
# on its contents. A well-typed marker naming a slug that does not exist and a
# recipient nobody assigned made the sweep emit a directive to that stranger,
# pointing at a nonexistent task, report redelivery success, and write a
# delivered marker that suppressed every retry — while the real vacancy sat
# under its own slug, still absent from the stream of the agent it was for.
#
# Type-checking a claim is not verifying it.

def _real_slug(t: _BusTransport) -> str:
    return [p for p in t.store if "/task/role-vacant-" in p][0].rsplit("/", 1)[1][:-3]


def test_a_marker_naming_a_nonexistent_task_never_routes_to_its_recipient():
    """THE regression: a well-typed lie must not become a real directive."""
    t = _mint_without_bus()
    real = _real_slug(t)
    t.put(_delivery_path(), json.dumps(
        {"delivered": False, "slug": "role-vacant-nonexistent", "to": "mallory"}))
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert not [e for e in evs if e["to"] == "mallory"], (
        "emitted a directive to a recipient the vacancy document never named")
    assert not [e for e in evs if e["slug"] == "role-vacant-nonexistent"], (
        "emitted a pointer to a task that does not exist")
    # and the REAL obligation still reaches its real assignee
    assert [e for e in evs if e["slug"] == real and e["to"] == "alice"], (
        "the real vacancy is still absent from its intended recipient's stream")


def test_a_marker_recipient_that_disagrees_with_the_document_loses(capsys):
    """Right slug, wrong recipient: the document's assignee is authoritative."""
    t = _mint_without_bus()
    real = _real_slug(t)
    t.put(_delivery_path(),
          json.dumps({"delivered": False, "slug": real, "to": "mallory"}))
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert len(evs) == 1 and evs[0]["to"] == "alice", evs
    assert "routing on the document" in capsys.readouterr().err


def test_an_unreadable_vacancy_document_fails_closed_without_claiming_delivery(capsys):
    """If no document can say who the vacancy is for, redelivery must not
    invent a recipient — and must not record a delivery that did not happen."""
    t = _mint_without_bus()
    for p in [k for k in list(t.store) if "/task/role-vacant-" in k]:
        del t.store[p]
    t.put(_delivery_path(), json.dumps(
        {"delivered": False, "slug": "role-vacant-gone", "to": "mallory"}))
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    assert not [e for e in _events(t) if e["kind"] == "directive"]
    assert "state UNKNOWN" in capsys.readouterr().err
    state = json.loads(t.store[_delivery_path()])
    assert state["delivered"] is False, "recorded a delivery that never happened"


def test_a_vacancy_document_with_no_readable_frontmatter_is_unknown():
    """A document we cannot parse cannot name its own assignee."""
    t = _mint_without_bus()
    doc = [p for p in t.store if "/task/role-vacant-" in p][0]
    t.put(doc, "this is not frontmatter")
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    assert not [e for e in _events(t) if e["kind"] == "directive"]


def test_a_marker_naming_a_valid_but_unrelated_task_never_routes_it(capsys):
    """codex-reviewer, 682 r4. r4 verified the marker's slug resolved to a REAL
    task with a readable assignee — but not that it was THIS ROLE'S vacancy. So
    a marker naming any valid task routed that task to its assignee, recorded
    success, and left the real vacancy unannounced.

    Three rounds tried to validate the proposed slug and each found another hole
    (nonexistent, wrong-recipient, valid-but-unrelated). The candidate set is
    derived from role/date/SLA, so the marker is not consulted for routing."""
    t = _mint_without_bus()
    real = _real_slug(t)
    t.put("team/r/task/unrelated-real-task.md",
          "---\ntype: Task\nassignee: mallory\nstatus: proposed\n"
          "priority: P1\n---\n# unrelated\n")
    t.put(_delivery_path(), json.dumps(
        {"delivered": False, "slug": "unrelated-real-task", "to": "mallory"}))
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert not [e for e in evs if e["slug"] == "unrelated-real-task"], (
        "a marker routed an unrelated real task as if it were the vacancy")
    assert not [e for e in evs if e["to"] == "mallory"], evs
    assert [e for e in evs if e["slug"] == real and e["to"] == "alice"], (
        "the real vacancy is still absent from its intended recipient's stream")
    assert "not a vacancy slug derived for this role and date" in capsys.readouterr().err


def test_a_delivered_true_marker_for_an_unrelated_task_does_not_suppress(capsys):
    """codex-reviewer, 682 r5. r5 fixed the REDELIVERY path to derive its own
    candidates and left the SUPPRESSION fast path trusting `delivered` alone —
    so the same unrelated-task marker with the opposite boolean still
    short-circuited, reported "event delivered", emitted nothing, and left the
    real vacancy permanently absent.

    One predicate now answers "is this proof", so the two consumers cannot
    diverge again."""
    t = _mint_without_bus()
    real = _real_slug(t)
    t.put("team/r/task/unrelated-real-task.md",
          "---\ntype: Task\nassignee: mallory\nstatus: proposed\n"
          "priority: P1\n---\n# unrelated\n")
    t.put(_delivery_path(), json.dumps(
        {"delivered": True, "slug": "unrelated-real-task", "to": "mallory"}))
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert [e for e in evs if e["slug"] == real and e["to"] == "alice"], (
        "a delivered:true marker for an unrelated task suppressed the real "
        "vacancy's redelivery — permanent loss")
    assert not [e for e in evs if e["to"] == "mallory"], evs


def test_a_delivered_true_marker_for_the_real_vacancy_still_suppresses(capsys):
    """The predicate must still confirm genuine delivery, or every sweep
    re-publishes forever."""
    t = _team()
    cli.main(["escalate", "r"], transport=t)
    t.records.clear()
    capsys.readouterr()
    cli.main(["escalate", "r"], transport=t)
    assert not [e for e in _events(t) if e["kind"] == "directive"]
    assert "event delivered" in capsys.readouterr().err


def test_a_delivered_marker_with_the_right_slug_but_wrong_recipient_is_not_proof():
    """codex-reviewer, 682 r6. The predicate proved the SLUG and ignored `to`,
    so a marker carrying the real vacancy slug with `to: mallory` was accepted
    as proof that alice's vacancy had reached the stream — no directive emitted,
    redelivery permanently suppressed.

    Seven rounds each proved a SUBSET of what "this marker is proof" requires.
    Confirmation is now equality against a freshly resolved (slug, assignee), so
    there is no field left to forget."""
    t = _mint_without_bus()
    real = _real_slug(t)
    t.put(_delivery_path(),
          json.dumps({"delivered": True, "slug": real, "to": "mallory"}))
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert [e for e in evs if e["slug"] == real and e["to"] == "alice"], (
        "a wrong-recipient marker was accepted as proof — alice's vacancy is "
        "permanently absent from her stream")


def test_confirmation_is_equality_against_the_document_not_the_marker():
    """A marker that matches the resolved (slug, assignee) exactly IS proof —
    otherwise every sweep republishes forever."""
    t = _team()
    cli.main(["escalate", "r"], transport=t)
    real = _real_slug(t)
    state = json.loads(t.store[_delivery_path()])
    assert state == {"delivered": True, "slug": real, "to": "alice"}, state
    t.records.clear()
    cli.main(["escalate", "r"], transport=t)
    assert not [e for e in _events(t) if e["kind"] == "directive"]


# --- redelivery must never resurrect an answered obligation ----------------
#
# coord-maintainer, 2026-08-23, on live 2.0.5. Making opens emit had an
# unintended mirror: during the broken window the open never reached the stream,
# so when an agent CLOSED the row their close had nothing to answer and emitted
# nothing. Redelivery then replayed the open into a fold that had never seen a
# close for it — terminal in the document, OPEN in the stream, permanently.
#
# The `abandoned` case is strictly worse than `done` and is why this refuses ANY
# terminal state rather than emitting a compensating close: `abandoned -> done`
# is an illegal transition, so a resurrected abandoned row cannot be discharged
# by any action its holder can take. A P1 they must carry and may not answer.

def _terminalise(t: _BusTransport, status: str) -> str:
    doc = [p for p in t.store if "/task/role-vacant-" in p][0]
    body = t.store[doc].replace("status: proposed", f"status: {status}")
    assert f"status: {status}" in body, body[:200]
    t.put(doc, body)
    return doc.rsplit("/", 1)[1][:-3]


@pytest.mark.parametrize("status", ["done", "abandoned"])
def test_redelivery_never_replays_an_open_for_a_terminal_document(status):
    """THE regression, both terminal states."""
    t = _mint_without_bus()
    _terminalise(t, status)
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    assert not [e for e in _events(t) if e["kind"] == "directive"], (
        f"a {status} obligation was replayed as an open — its holder cannot "
        f"discharge it, and for 'abandoned' no legal transition exists at all")


@pytest.mark.parametrize("status", ["done", "abandoned"])
def test_a_terminal_document_is_not_recorded_as_a_delivery(status):
    """Refusing to resurrect must not launder into 'delivered'. UNKNOWN is the
    honest state: there was nothing live to deliver."""
    t = _mint_without_bus()
    _terminalise(t, status)
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    state = t.store.get(_delivery_path())
    if state is not None:
        assert json.loads(state).get("delivered") is not True, state


def _other_candidate(t: _BusTransport, real: str) -> str:
    """The day's OTHER candidate slug — the title branches on `attended`, so a
    role has exactly two possible vacancy slugs per day."""
    from coord_engine import cli as _cli
    cands = _cli._vacancy_slug_candidates(
        "reviewer", _cli._now().strftime("%Y-%m-%d"), 12.0)
    other = [c for c in cands if c != real]
    assert other, cands
    return other[0]


def test_a_live_vacancy_beside_a_terminal_one_still_gets_delivered():
    """Skipping a terminal candidate must not abandon the search — the day's
    other candidate title may hold the live row.

    codex-reviewer, 685 r2: this test previously created only ONE document and
    so never exercised its own stated scenario. Both candidates now exist."""
    t = _mint_without_bus()
    real = _real_slug(t)
    other = _other_candidate(t, real)
    # the OTHER candidate is the terminal one; the real row stays live
    t.put(f"team/r/task/{other}.md",
          "---\ntype: Task\nassignee: alice\nstatus: done\n"
          "priority: P1\n---\n# answered\n")
    _add_bus(t)
    cli.main(["escalate", "r"], transport=t)
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert [e for e in evs if e["slug"] == real], (
        "a live vacancy beside a terminal sibling stopped being delivered")


@pytest.mark.parametrize("other_body", [
    "not-frontmatter",
    "---\ntype: Task\nstatus: proposed\npriority: P1\n---\n# no assignee\n",
])
def test_an_unresolvable_sibling_outranks_a_terminal_candidate(other_body, capsys):
    """THE r2 regression. One candidate answered, the other EXISTING but
    unreadable, must not report "already answered" and exit clean — the
    unreadable one may still be a live P1."""
    t = _mint_without_bus()
    real = _terminalise(t, "done")
    t.put(f"team/r/task/{_other_candidate(t, real)}.md", other_body)
    _add_bus(t)
    rc = cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err
    assert "already answered" not in err, (
        "a terminal candidate outvoted an unreadable sibling — absence of "
        "evidence was treated as evidence that nothing is owed")
    assert "state UNKNOWN" in err, err
    assert rc == 3, "an unresolvable candidate must fail the sweep closed"


# --- the self-addressed path is the one that skipped the guard -------------
#
# codex-reviewer, 685 r1 P1. The terminal filter lived in _resolve_vacancy, and
# the self-addressed branch emitted the slug computed further up — bypassing the
# resolver entirely. A self-addressed vacancy writes no daily marker by design,
# so it lands in that branch EVERY sweep, and it republished its own terminal
# task as a fresh open: exactly the permanently-undischargeable row this change
# exists to prevent, recreated by the one path that skipped the guard.

SELF_ROLE_DOC = "---\ntype: Role\nsla_hours: 12\nmaintainer: arcbot\n---\n"
SELF_LEASE = ("---\ntype: Lease\nagent: arcbot\n"
              "timestamp: 2020-01-01T00:00:00Z\n---\n")


def _self_addressed_team() -> _BusTransport:
    """maintainer IS the lapsed holder — the closed-loop case, which writes no
    daily marker and therefore retries on every sweep."""
    t = _BusTransport()
    t.put("team/r/roles/arc.md", SELF_ROLE_DOC)
    t.put("team/r/roles/arc/leases/arcbot.md", SELF_LEASE)
    t.put("team/r/_coord/bus-v3/records.json", BUS_CONFIG)
    return t


@pytest.mark.parametrize("status", ["done", "abandoned"])
def test_the_self_addressed_retry_never_resurrects_a_terminal_task(status):
    """THE r1 P1 regression, both terminal states."""
    t = _self_addressed_team()
    cli.main(["escalate", "r"], transport=t)
    doc = [p for p in t.store if "/task/role-vacant-" in p][0]
    t.put(doc, t.store[doc].replace("status: proposed", f"status: {status}"))
    t.records.clear()
    cli.main(["escalate", "r"], transport=t)
    assert not [e for e in _events(t) if e["kind"] == "directive"], (
        f"the self-addressed retry republished a {status} vacancy as a fresh "
        f"open — undischargeable for 'abandoned'")


def test_the_self_addressed_retry_still_redelivers_a_live_vacancy():
    """Terminal-awareness must not break the closed-loop retry it was added to."""
    t = _self_addressed_team()
    t.record_write = lambda *a, **k: False  # type: ignore[assignment]
    cli.main(["escalate", "r"], transport=t)
    slug = [p for p in t.store if "/task/role-vacant-" in p][0].rsplit("/", 1)[1][:-3]
    del t.record_write  # bus recovers
    t.records.clear()
    cli.main(["escalate", "r"], transport=t)
    evs = [e for e in _events(t) if e["kind"] == "directive"]
    assert [e for e in evs if e["slug"] == slug and e["to"] == "arcbot"], evs


# --- a completed obligation is not an undelivered one ----------------------
#
# codex-reviewer, 685 r1 P2. Skipping a terminal document made
# _redeliver_escalation return False, which the caller read as a delivery
# FAILURE: it printed that the vacancy remained invisible, incremented
# `undelivered`, and returned rc 3 on every future sweep. A permanent false
# alarm manufactured by correctly refusing to resurrect.

@pytest.mark.parametrize("status", ["done", "abandoned"])
def test_a_terminal_vacancy_is_not_reported_as_undelivered(status, capsys):
    t = _mint_without_bus()
    _terminalise(t, status)
    _add_bus(t)
    rc = cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err
    assert "redelivery FAILED" not in err, err
    assert "already answered" in err, err
    assert "undelivered=0" in err, err
    assert rc == 0, "a completed obligation must not fail the sweep closed"
