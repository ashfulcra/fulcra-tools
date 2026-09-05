"""``obligations --repair-unknown`` — clearing a carried UNKNOWN without the walk.

THE DEADLOCK THIS VERB EXISTS TO BREAK, measured on the live store 2026-09-04.
The stream fold copies ``unknown_components`` forward on every advance and
nothing else writes the field, so the only way to clear one was a full
``--seed-checkpoint`` — a corpus walk over every component. The component stuck
UNKNOWN on this fleet is ``reviews``, whose corpus probe cannot finish inside a
budget (104 of 364 slugs at fold 300 / briefing 600). So the only thing that
could clear the UNKNOWN was the thing that could not run, and every stream
answer carried a caveat that looked permanent when it was really unattempted.

These drive ``cli.main`` and assert on the STORE, not only on the decision
function: three NameError-class defects in this repo have passed full unit
suites and been caught only by running the verb.
"""

from __future__ import annotations

import datetime
import json

import pytest

from coord_engine import cli, obligations as obligations_mod
from coord_engine_test_helpers import FakeTransport

TEAM = "fulcra"
AGENT = "coord-boss"
CKPT = f"team/{TEAM}/_coord/agents/{AGENT}/obligations-checkpoint.json"


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: datetime.datetime(
        2026, 9, 4, 1, 0, 0, tzinfo=datetime.timezone.utc))


def _checkpoint(unknown=("reviews",), open_rows=(("kept-row", "P1"),)):
    return json.dumps({
        "v": 1,
        "as_of": "2026-09-04T00:16:42.491666Z",
        "seeded_by": "stream-fold",
        "unknown_components": sorted(unknown),
        "open": [{"slug": s, "ptr": f"task/{s}.md", "priority": p}
                 for s, p in open_rows],
    }, sort_keys=True)


def _transport(unknown=("reviews",), open_rows=(("kept-row", "P1"),)):
    t = FakeTransport()
    t.put(CKPT, _checkpoint(unknown, open_rows))
    return t


def _probes(monkeypatch, **states):
    """Replace the probe registry with named probes of scripted state.

    Values are either ``None`` (OK, nothing owed), a list of owed rows (OK with
    work), or a string (UNREADABLE with that detail).
    """
    P, S = obligations_mod.ProbeResult, obligations_mod.ProbeState

    def make(value):
        if isinstance(value, str):
            return lambda: P(state=S.UNREADABLE, detail=value)
        return lambda: P(state=S.OK, owed=list(value or []))

    comps = [obligations_mod.Component(name=n, probe=make(v))
             for n, v in states.items()]
    monkeypatch.setattr(cli, "_obligation_probes",
                        lambda *a, **kw: comps)


def _run(transport, extra=()):
    return cli.main(["obligations", TEAM, "--agent", AGENT,
                     "--repair-unknown", *extra], transport=transport)


def _stored(transport):
    return json.loads(transport.read(CKPT))


def test_a_component_that_now_reads_is_CLEARED_from_the_checkpoint(capsys, monkeypatch):
    t = _transport()
    _probes(monkeypatch, reviews=None)
    rc = _run(t)
    assert rc == 0
    assert _stored(t)["unknown_components"] == []
    assert "cleared: reviews" in capsys.readouterr().out


def test_clearing_MERGES_the_rows_it_found_instead_of_dropping_them(capsys, monkeypatch):
    """The false clear this verb could most easily manufacture. Marking a
    surface covered while discarding the work it found would be worse than
    leaving it UNKNOWN: the caveat at least told the truth."""
    t = _transport()
    _probes(monkeypatch, reviews=[{"slug": "owed-review", "ptr": "task/x.md",
                                   "priority": "P1"}])
    rc = _run(t)
    assert rc == 0
    stored = _stored(t)
    slugs = {r["slug"] for r in stored["open"]}
    assert slugs == {"kept-row", "owed-review"}, stored
    assert stored["unknown_components"] == []


def test_a_component_that_still_cannot_be_read_STAYS_unknown_and_rc_is_3(capsys, monkeypatch):
    t = _transport()
    _probes(monkeypatch, reviews="probe budget exhausted")
    rc = _run(t)
    assert rc == 3
    err = capsys.readouterr().err
    assert "still UNKNOWN: reviews" in err
    assert "probe budget exhausted" in err
    assert _stored(t)["unknown_components"] == ["reviews"]


def test_nothing_cleared_and_nothing_merged_leaves_the_checkpoint_BYTE_UNCHANGED(monkeypatch):
    """A repair that achieved nothing must not rewrite the document. Rewriting
    it would change `seeded_by` and make a failed repair look like a fresh one
    in any later audit."""
    t = _transport()
    before = t.read(CKPT)
    _probes(monkeypatch, reviews="still down")
    assert _run(t) == 3
    assert t.read(CKPT) == before


def test_it_probes_ONLY_the_carried_unknown_not_the_whole_registry(monkeypatch):
    """The cost claim, and the reason a subset probe is not just a smaller
    version of the same attempt: the stuck component gets the entire budget
    instead of whatever survives its six siblings."""
    ran: list[str] = []
    P, S = obligations_mod.ProbeResult, obligations_mod.ProbeState

    def make(name):
        def probe():
            ran.append(name)
            return P(state=S.OK, owed=[])
        return probe

    comps = [obligations_mod.Component(name=n, probe=make(n))
             for n in ("blocks", "directives", "reviews", "tasks")]
    monkeypatch.setattr(cli, "_obligation_probes", lambda *a, **kw: comps)
    assert _run(_transport()) == 0
    assert ran == ["reviews"], ran


def test_a_name_the_registry_no_longer_offers_stays_unknown_with_its_own_reason(capsys, monkeypatch):
    """'Not offered' and 'read and failed' have different remedies, so they get
    different sentences. Clearing it would claim coverage never established."""
    t = _transport(unknown=("reviews", "retired_surface"))
    _probes(monkeypatch, reviews=None)
    rc = _run(t)
    assert rc == 3
    err = capsys.readouterr().err
    assert "still UNKNOWN: retired_surface" in err
    assert "not offered by this engine's probe registry" in err
    assert _stored(t)["unknown_components"] == ["retired_surface"]


def test_a_checkpoint_with_no_unknowns_is_a_no_op_at_rc_0(capsys, monkeypatch):
    t = _transport(unknown=())
    _probes(monkeypatch, reviews=None)
    assert _run(t) == 0
    assert "nothing to repair" in capsys.readouterr().out


def test_no_checkpoint_at_all_is_UNKNOWN_not_success(capsys, monkeypatch):
    t = FakeTransport()
    _probes(monkeypatch, reviews=None)
    assert _run(t) == 3
    assert "no readable checkpoint" in capsys.readouterr().err


def test_as_of_is_NOT_advanced_because_this_run_folds_no_events(monkeypatch):
    """The repair makes no claim about time. Advancing `as_of` would silently
    declare the window between the old instant and now to have been folded."""
    t = _transport()
    _probes(monkeypatch, reviews=None)
    assert _run(t) == 0
    assert _stored(t)["as_of"] == "2026-09-04T00:16:42.491666Z"


def test_a_failed_checkpoint_write_reports_UNKNOWN_and_does_not_claim_repair(capsys, monkeypatch):
    t = _transport()
    _probes(monkeypatch, reviews=None)
    monkeypatch.setattr(cli, "_save_obligations_checkpoint",
                        lambda *a, **kw: False)
    assert _run(t) == 3
    assert "checkpoint write FAILED" in capsys.readouterr().err
