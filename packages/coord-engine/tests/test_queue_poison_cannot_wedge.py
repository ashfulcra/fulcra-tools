"""One unrenderable event must never wedge a cursor forever.

MEASURED (coord-boss, 2026-08-10): codex-reviewer's queue held 9 events stuck
from 08:03Z while their store writes and register work continued fine. The cause
was ours: `_print_queue_events` rendered `kind` and `slug` as DIRECT SUBSCRIPTS
while every neighbouring field used `.get()`, so an event missing either raised
KeyError out of the renderer, out of `cmd_queue`, and past the cursor save —
which never ran. At-least-once redelivery then handed back the same window, with
the same poison event, on every subsequent read. Permanently stuck by
construction, with no error path that said so.

coord-boss's constraints, each pinned by a test below:
  1. PER-EVENT guard — poison renders as an explicit POISON line, never crashes,
     never silently skips, and the count is reported so a poisoned window is loud.
  2. CURSOR-SAVE REACHABILITY as an invariant — no path between the read and the
     save may exit without saving, INCLUDING exceptions a later edit introduces.
  3. The at-least-once contract still holds: poison is DELIVERED and CONSUMED.
     Redelivery of poison FOREVER is the bug; redelivery itself is not.

These drive `cli.main(["queue", ...])` end to end. A helper-level test cannot see
this class of defect at all — the wedge lives in the control flow BETWEEN the
renderer and the cursor save, which is exactly where nobody was looking.
"""

from __future__ import annotations

import json

from coord_engine import cli, records
from coord_engine_test_helpers import FakeTransport

TEAM = "r"
AGENT = "rev"


class _WindowTransport(FakeTransport):
    """Serves a fixed record window, so a poison event can be planted mid-read."""

    def __init__(self, window):
        super().__init__()
        self._window = window
        self.cursor_writes = []

    def records(self, data_type, since, until):
        return list(self._window)

    def write(self, path, text):
        if path.endswith(records.cursor_path(TEAM, AGENT)) or "cursor" in path:
            self.cursor_writes.append(text)
        return super().write(path, text)


def _event(**kw):
    base = {"record_id": kw.pop("rid", "r1"), "to": AGENT, "kind": "directive",
            "slug": "s", "priority": "P1", "from": "sender",
            "recorded_at": "2026-08-10T08:03:00Z", "ptr": "p.md"}
    base.update(kw)
    return base


def _run(monkeypatch, window, *, args=None):
    monkeypatch.setenv("FULCRA_COORD_AGENT", AGENT)
    t = _WindowTransport(window)
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          json.dumps({"data_type": "X/1", "api_version": "v1alpha1"}))
    rc = cli.main(args or ["queue", TEAM, "--agent", AGENT], transport=t)
    return rc, t


def _cursor_saved(t):
    return any("last_read" in w for w in t.cursor_writes)


def test_a_poison_event_does_not_prevent_the_cursor_save(monkeypatch, capsys):
    """THE regression. A window whose middle event lacks `kind` must still
    advance coverage — otherwise the same window returns forever."""
    window = [_event(rid="a"), _event(rid="b", kind=None, slug=None),
              _event(rid="c")]
    monkeypatch.setattr(records, "events_for", lambda w, a: list(w))
    rc, t = _run(monkeypatch, window)
    capsys.readouterr()
    assert _cursor_saved(t), (
        "the cursor was NOT saved on a window containing a poison event — the "
        "wedge: at-least-once redelivery returns this same window forever")
    assert rc == 0


def test_an_exception_ANYWHERE_after_the_read_still_saves_the_cursor(monkeypatch):
    """Constraint 2 as a structural invariant, not a promise about today's code.

    The renderer is patched to raise something no per-event guard anticipates.
    Coverage is a fact about what this process RECEIVED; it cannot be contingent
    on what happens afterwards, including exceptions a future edit introduces.
    """
    def _boom(events, *, json_mode):
        raise RuntimeError("a future edit raises here")

    monkeypatch.setattr(cli, "_print_queue_events", _boom)
    monkeypatch.setattr(records, "events_for", lambda w, a: list(w))
    try:
        _rc, t = _run(monkeypatch, [_event(rid="a")])
    except RuntimeError:
        # Propagating is fine and correct — losing coverage is not.
        pass
    else:
        t = t
    assert _cursor_saved(t), (
        "an exception between the read and the save skipped the save; that is "
        "the wedge, reintroduced by any later edit unless the save is in a "
        "finally")


def test_poison_is_RENDERED_and_COUNTED_never_silently_skipped(capsys):
    """Constraint 1 and 3. Trading a crash for a disappearance would be a worse
    bug: the event was delivered, so it must be visible AND counted."""
    poison = cli._print_queue_events(
        [_event(rid="a"), {"record_id": "b"}], json_mode=False)
    out = capsys.readouterr()
    assert poison == 0 or "POISON" in out.err or "?" in out.out
    # A truly unformattable event (a non-dict) must still be counted, not raise.
    poison2 = cli._print_queue_events([object()], json_mode=False)
    err = capsys.readouterr().err
    assert poison2 == 1, "an unrenderable event was silently skipped"
    assert "POISON" in err, f"poison was not rendered loudly: {err!r}"


def test_the_renderer_cannot_raise_on_any_shape():
    """The formatter is the last line of defence for the delivery path; a
    formatter that can fail here IS the bug."""
    for shape in ({}, {"kind": None}, {"slug": None},
                  {"recorded_at": None}, {"recorded_at": 12345},
                  object(), None, {"from": object()}):
        cli._print_queue_events([shape], json_mode=False)


def test_PEEK_still_does_not_advance(monkeypatch, capsys):
    """The one exit that legitimately skips the save must keep skipping it —
    the reachability invariant must not turn a peek into a consume."""
    monkeypatch.setattr(records, "events_for", lambda w, a: list(w))
    _rc, t = _run(monkeypatch, [_event(rid="a")],
                  args=["queue", TEAM, "--agent", AGENT, "--peek"])
    capsys.readouterr()
    assert not _cursor_saved(t), "a peek advanced the cursor"
