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


# --- the broadcast recipient token -----------------------------------------
#
# The directed path above was tested when the companion shipped; the fleet-wide
# path was not, and that is the whole of this defect. `broadcast` addresses the
# TASK plane's everyone-token "*", the queue filter keeps only (agent, "all"),
# so every fleet-wide directive landed on the live channel in a correct v:1
# shape and was dropped by every reader. The sender saw a slug and rc 0.
#
# The load-bearing test is the last one: it does not assert on a translated
# string, it runs the actual reader over the actual emitted note.

def test_broadcast_translates_the_task_token_to_the_event_token(emitted):
    args = _args()
    args._directive_outcome = "written"
    cli._emit_dispatch_companion(None, args, slug="fleet-notice-abc123",
                                 assignee=records.TASK_EVERYONE)
    assert len(emitted) == 1
    assert emitted[0]["to"] == records.BROADCAST


def test_a_directed_assignee_is_never_rewritten(emitted):
    """Translation must apply to the everyone-token and nothing else."""
    args = _args()
    args._directive_outcome = "written"
    cli._emit_dispatch_companion(None, args, slug="s", assignee="codex-coder")
    assert emitted[0]["to"] == "codex-coder"


def test_the_two_planes_use_different_everyone_tokens():
    """Guards the premise. If these ever converge the translation is dead code,
    and a future reader deleting it should be told by a test, not by an outage."""
    assert records.TASK_EVERYONE != records.BROADCAST


@pytest.mark.parametrize("reader", ["coord-boss", "codex-coder", "arc-maintainer"])
def test_the_broadcast_note_actually_reaches_an_arbitrary_reader(reader):
    """END TO END, and the only test here that would have caught the defect.

    Every other assertion in this file compares a value the fix produces
    against a value the fix chose. This one builds the note the companion
    sends and runs `records.events_for` — the real filter, the one the
    recipient runs — over a record carrying it. A note the reader drops is not
    a delivery, however correct it looks at the sender.
    """
    note = records.build_payload(
        to=cli._companion_recipient(records.TASK_EVERYONE), kind="directive",
        priority="P0", slug="fleet-notice-abc123",
        ptr="task/fleet-notice-abc123.md")
    got = records.events_for([{"id": "r1", "note": note}], reader)
    assert [e["slug"] for e in got] == ["fleet-notice-abc123"]


def test_the_untranslated_token_is_what_a_reader_drops():
    """Positive control for the test above: without the translation the very
    same note reaches nobody. This is the outage, reproduced in one assert."""
    note = records.build_payload(
        to=records.TASK_EVERYONE, kind="directive", priority="P0",
        slug="fleet-notice-abc123", ptr="task/fleet-notice-abc123.md")
    assert records.events_for([{"id": "r1", "note": note}], "coord-boss") == []
