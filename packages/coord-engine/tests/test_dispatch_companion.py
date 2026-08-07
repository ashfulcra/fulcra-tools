"""The dispatch companion event — defect #2 of the 2026-08-06 delivery outage.

Two independent defects stacked. #1: the activity projection resolved its
channel by the NAME "Agent Tasks", which the 2026-08-04 cutover superseded, so
projections landed on a dead channel. #2, found only after #1's mitigation was
proven: the projection's note is PROSE, and the documented queue filter keeps
only notes parsing as JSON with ``"v":1`` — so a projection on the RIGHT channel
is dropped exactly as faithfully as one on a dead channel.

Fixing #1 alone relocates an invisible record. These tests are about #2: a
`tell` now emits a companion `v:1` event pointing at the durable doc, which is
what actually makes a dispatch visible in the recipient's queue.

The tests that matter most are the ones asserting the companion does NOT fire:
on a dedupe (a second event for an already-delivered slug is indistinguishable
from new work), on a failed write, on @backlog, and that a bus failure never
fails the tell — the durable doc is the truth, the event is delivery.
"""
from __future__ import annotations

import argparse

import pytest

from coord_engine import cli, directives, records


CFG = {"data_type": "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041",
       "api_version": "v1alpha1"}


@pytest.fixture
def emitted(monkeypatch):
    """Capture every records.emit_event call the dispatch path makes."""
    calls: list[dict] = []

    def _emit(transport, cfg, **kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(records, "emit_event", _emit)
    monkeypatch.setattr(
        records, "load_config_classified", lambda t, team: (CFG, "ok"))
    return calls


def _args(**kw):
    base = dict(team="fulcra", priority="P1", sender="coord-boss")
    base.update(kw)
    return argparse.Namespace(**base)


def test_a_verified_dispatch_emits_one_v1_companion(emitted):
    args = _args()
    args._directive_outcome = "written"
    cli._emit_dispatch_companion(None, args, slug="build-thing-abc123",
                                 assignee="coord-opus-worker")
    assert len(emitted) == 1
    call = emitted[0]
    assert call["to"] == "coord-opus-worker"
    assert call["kind"] == "directive"
    assert call["priority"] == "P1"
    assert call["slug"] == "build-thing-abc123"


def test_the_companion_points_at_the_durable_doc(emitted):
    """The event carries no body; the ptr is the whole point — a recipient
    following it lands on the task document the tell actually wrote."""
    cli._emit_dispatch_companion(None, _args(), slug="build-thing-abc123",
                                 assignee="coord-opus-worker")
    assert emitted[0]["ptr"] == "task/build-thing-abc123.md"


def test_the_companion_is_team_scoped_so_it_gets_identity_tags(emitted):
    """records.emit_event is the one tagging chokepoint and it needs the team
    to resolve the registry; omitting it would write the event untagged."""
    cli._emit_dispatch_companion(None, _args(), slug="s", assignee="a")
    assert emitted[0]["team"] == "fulcra"


def test_backlog_capture_emits_nothing(emitted):
    """@backlog awaits no reply and addresses nobody — an event there is noise
    in every queue for work assigned to no one."""
    cli._emit_dispatch_companion(None, _args(), slug="s",
                                 assignee=directives.BACKLOG)
    assert emitted == []


def test_sender_falls_back_to_the_host_rather_than_writing_anonymously(
        emitted, monkeypatch):
    monkeypatch.setattr(cli, "_host", lambda: "some-host")
    cli._emit_dispatch_companion(None, _args(sender=None), slug="s",
                                 assignee="a")
    assert emitted[0]["sender"] == "some-host"


def test_missing_priority_defaults_rather_than_raising(emitted):
    args = argparse.Namespace(team="fulcra", sender="coord-boss")
    cli._emit_dispatch_companion(None, args, slug="s", assignee="a")
    assert emitted[0]["priority"] == "P2"


# --- what must NOT happen -------------------------------------------------

def test_no_records_config_degrades_to_file_plane_without_raising(
        monkeypatch, capsys):
    monkeypatch.setattr(
        records, "load_config_classified", lambda t, team: (None, "absent"))
    called: list = []
    monkeypatch.setattr(records, "emit_event",
                        lambda *a, **k: called.append(1))
    cli._emit_dispatch_companion(None, _args(), slug="s", assignee="a")
    assert called == []
    assert "file plane only" in capsys.readouterr().out


def test_a_raising_bus_never_fails_the_tell(monkeypatch, capsys):
    monkeypatch.setattr(
        records, "load_config_classified", lambda t, team: (CFG, "ok"))

    def _boom(*a, **k):
        raise RuntimeError("bus on fire")

    monkeypatch.setattr(records, "emit_event", _boom)
    # returns None, does not raise: the durable doc is the truth
    assert cli._emit_dispatch_companion(
        None, _args(), slug="s", assignee="a") is None
    assert "file plane only" in capsys.readouterr().err


def test_a_failed_emission_says_so_and_names_the_recovery(monkeypatch, capsys):
    monkeypatch.setattr(
        records, "load_config_classified", lambda t, team: (CFG, "ok"))
    monkeypatch.setattr(records, "emit_event", lambda *a, **k: False)
    cli._emit_dispatch_companion(None, _args(), slug="s", assignee="a")
    out = capsys.readouterr().out
    assert "emission failed" in out
    assert "needs-me" in out  # the backstop that recovered the outage


def test_the_note_shape_passes_the_documented_queue_filter():
    """The whole defect: a note the filter drops is not a delivery. Build the
    payload the companion sends and run the BOOTSTRAP.md predicate on it."""
    import json
    note = records.build_payload(
        to="coord-opus-worker", kind="directive", priority="P1",
        slug="build-thing-abc123", ptr="task/build-thing-abc123.md")
    parsed = json.loads(note)
    assert parsed["v"] == 1
    assert parsed["to"] == "coord-opus-worker"
    assert parsed["ptr"] == "task/build-thing-abc123.md"


def test_a_deferred_directive_emits_no_companion(monkeypatch):
    """`remind` already emits its OWN future-dated record timed to not_before.
    A companion sent NOW would surface the reminder immediately — the one thing
    a reminder must not do. Caught by the existing remind suite, not by me."""
    calls: list = []
    monkeypatch.setattr(records, "emit_event",
                        lambda *a, **k: calls.append(k) or True)
    monkeypatch.setattr(
        records, "load_config_classified", lambda t, team: (CFG, "ok"))
    args = _args(title="t", summary="s", next="n", workstream=None,
                 assignee="a")
    monkeypatch.setattr(cli, "_write_directive",
                        lambda *a, **k: setattr(args, "_directive_outcome",
                                                "written") or 0)
    cli._create_directive(args, None, assignee="a",
                          not_before="2026-09-01T00:00:00Z")
    assert calls == []


# --- the two planes disagree about the word for everyone -------------------
#
# coord-boss P0, 2026-08-07: `broadcast` printed a slug and rc 0 and reached
# NOBODY. The task plane says "*", the event plane says "all"
# (records.BROADCAST), and the reader filter is `to in (agent, BROADCAST)` — so
# an event addressed to "*" matches no one. Neither token is wrong on its own;
# nothing translated between them. Directed dispatch was unaffected because the
# two strings coincide there, which is exactly why this survived being tested.

def test_broadcast_is_translated_to_the_event_plane_token():
    from coord_engine import directives, records

    assert directives.EVERYONE == "*"
    assert records.BROADCAST == "all"
    assert directives.EVERYONE != records.BROADCAST, (
        "if these ever become the same string, the translation below is a no-op "
        "and this test stops protecting anything"
    )


def test_the_reader_filter_would_drop_the_task_plane_token():
    """The mechanism itself, asserted rather than described: an event addressed
    with the TASK plane's everyone-token reaches nobody."""
    from coord_engine import records

    for agent in ("coord-boss", "codex-reviewer", "anyone-at-all"):
        assert "*" not in (agent, records.BROADCAST)
        assert records.BROADCAST in (agent, records.BROADCAST)


def test_a_broadcast_companion_is_addressed_to_the_EVENT_plane_token(emitted):
    """THE regression. `broadcast` assigns the task-plane "*"; the companion
    must emit the event-plane "all" or the reader filter drops it and the
    broadcast reaches nobody while printing a slug and rc 0."""
    cli._emit_dispatch_companion(None, _args(), slug="fleet-wide-thing",
                                 assignee=directives.EVERYONE)
    assert len(emitted) == 1
    assert emitted[0]["to"] == records.BROADCAST, (
        f'emitted to={emitted[0]["to"]!r} — the task-plane token reaches nobody'
    )


def test_a_directed_companion_is_untouched_by_the_translation(emitted):
    """The over-correction guard: only the everyone-token is rewritten. A
    recipient literally named something else must pass through verbatim."""
    for who in ("coord-boss", "codex-reviewer", "all", "star*ish"):
        emitted.clear()
        cli._emit_dispatch_companion(None, _args(), slug="s", assignee=who)
        assert emitted[0]["to"] == who
