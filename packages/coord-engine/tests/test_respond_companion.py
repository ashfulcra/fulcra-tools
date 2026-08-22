"""`respond` must DELIVER and must not lie about it.

Found 2026-08-08 the hard way. coord-boss asked me to stop re-emitting a pin
claim (18:15). I answered via `coord-engine respond` at 18:57 — task `done`, a
3KiB shard under `_coord/responses/`. At 20:14 coord-boss asked again at higher
priority, believing no answer had come, and attributed the silence to their own
earlier message. It had reached my queue; I had read it and replied.

`cmd_respond` wrote the shard, closed the task, and printed

    response recorded — the owner's queue surfaces it

while emitting NO bus event. The queue reads EVENTS; a shard in a prefix the
queue never walks cannot appear there. So the asker's only signal was the task
quietly leaving `proposed` — absence, not an answer. Using the verb correctly
produced silence, which made "answered" indistinguishable from "ignored": the
false-clear class this fleet keeps killing, on the REPLY leg.

Note the asymmetry it closed: `tell --closes` already delivered, because the
reply IS a tell and tells emit companions. Plain `respond` did not — so the two
closure paths had opposite notification behaviour and the recommended one was
the silent one.

SCOPE ADDITION (coord-boss, 2026-08-08): `transport.write` returns False on
failure rather than raising, so `respond` could fail to write the shard, print
success, and exit 0. Same family as PR 583. The write returns are checked here
too — an unwritten response must be loud, not cheerful.
"""
from __future__ import annotations

import argparse

import pytest

from coord_engine import cli, records, tasks
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
SLUG = "please-stop-the-thing-abc123"
CFG = {"data_type": "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041",
       "api_version": "v1alpha1"}


@pytest.fixture
def emitted(monkeypatch):
    calls: list[dict] = []

    def _emit(transport, cfg, **kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(records, "emit_event", _emit)
    monkeypatch.setattr(
        records, "load_config_classified", lambda t, team: (CFG, "ok"))
    return calls


def _seed(t, *, owner="coord-boss", assignee="coord-opus-worker"):
    _, content = tasks.new_task_doc(
        "please stop the thing", now="2026-08-08T18:15:00Z", status="proposed",
        priority="P2", owner=owner, assignee=assignee, kind="directive")
    t.put(cli._task_path(TEAM, SLUG), content)
    return t


def _args(**kw):
    base = dict(team=TEAM, name=SLUG, outcome="cannot — it is line 163 of your script",
                evidence=None, agent="coord-opus-worker")
    base.update(kw)
    return argparse.Namespace(**base)


# --- delivery -------------------------------------------------------------

def test_respond_emits_a_companion_event_to_the_ASKER(emitted):
    """THE test that did not exist. Without it the asker learns nothing."""
    t = _seed(FakeTransport())
    assert cli.cmd_respond(_args(), t) == 0
    assert len(emitted) == 1, (
        "respond emitted no event — the asker's queue reads events, so a shard "
        "alone reaches nobody and silence is indistinguishable from ignored"
    )
    call = emitted[0]
    assert call["to"] == "coord-boss", "the event must go to the ASKER (owner)"
    assert call["kind"] == "response"
    assert call["slug"] == SLUG, "the asker must tie it back to their ask"


def test_the_event_points_at_the_response_shard(emitted):
    """A substantive response carries a ptr; an envelope is not an answer."""
    t = _seed(FakeTransport())
    cli.cmd_respond(_args(), t)
    ptr = emitted[0]["ptr"]
    assert "_coord/responses/" in ptr and SLUG in ptr, f"ptr {ptr!r} must resolve"
    assert not ptr.startswith(f"team/{TEAM}/"), "ptr is team-relative"


def test_answering_your_OWN_directive_notifies_nobody_but_still_closes(emitted, capsys):
    # Round 3 (2026-08-21 pilot probe): "nobody to tell" used to mean "emit
    # nothing", and a self-answered task then replayed open in the stream fold
    # forever. The close still emits — addressed to yourself, discharging your
    # own copy — while the delivery line stays honest about the owner's queue.
    t = _seed(FakeTransport(), owner="coord-opus-worker")
    assert cli.cmd_respond(_args(), t) == 0
    assert len(emitted) == 1
    call = emitted[0]
    assert call["to"] == "coord-opus-worker", "self-addressed: nobody else is told"
    assert call["for_agent"] == "coord-opus-worker"
    assert "NOT delivered to the owner's queue" in capsys.readouterr().out, (
        "the delivery line must not claim an owner was notified")


def test_a_bus_failure_NEVER_fails_the_respond(monkeypatch):
    """The shard is the record; the event is delivery. Same doctrine as `tell`."""
    monkeypatch.setattr(records, "load_config_classified",
                        lambda t, team: (CFG, "ok"))

    def _boom(transport, cfg, **kw):
        raise RuntimeError("bus down")

    monkeypatch.setattr(records, "emit_event", _boom)
    t = _seed(FakeTransport())
    assert cli.cmd_respond(_args(), t) == 0
    assert t.read(cli._task_path(TEAM, SLUG))


def test_the_line_does_NOT_claim_delivery_when_nothing_was_delivered(
        monkeypatch, capsys):
    """The reassurance is what let this survive. No bus config => no delivery."""
    monkeypatch.setattr(records, "load_config_classified",
                        lambda t, team: (None, "absent"))
    t = _seed(FakeTransport())
    assert cli.cmd_respond(_args(), t) == 0
    out = capsys.readouterr().out
    assert "the owner's queue surfaces it" not in out, (
        "printed a delivery guarantee while emitting nothing — this exact line "
        "is why a responded-to directive was re-asked twice"
    )
    assert "NOT delivered" in out


# --- the write-return half (coord-boss scope addition) --------------------

class _ShardWriteFails(FakeTransport):
    """`transport.write` returns False on failure rather than raising."""

    def write(self, path, content):
        if "/_coord/responses/" in path:
            return False
        return super().write(path, content)


def test_a_failed_SHARD_write_is_loud_and_nonzero(capsys):
    """An unwritten response that exits 0 is the worst outcome: the responder
    believes they answered and the asker never hears anything at all."""
    t = _seed(_ShardWriteFails())
    rc = cli.cmd_respond(_args(), t)
    assert rc != 0, "a response that was never written must not exit 0"
    err = capsys.readouterr().err
    assert "response NOT recorded" in err


class _TaskWriteFails(FakeTransport):
    def write(self, path, content):
        if path.endswith(f"/task/{SLUG}.md"):
            return False
        return super().write(path, content)


def test_a_failed_CLOSE_write_is_loud_and_nonzero(capsys):
    """`apply_update` succeeding is not the close landing — the write can still
    return False, leaving the directive open while we print '(closed)'."""
    t = _seed(_TaskWriteFails())
    rc = cli.cmd_respond(_args(), t)
    assert rc != 0, "a close that never landed must not report success"
    combined = capsys.readouterr()
    assert "not closed" in (combined.out + combined.err)
