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


def test_a_MISSING_REQUIRED_FIELD_is_poison_counted_and_never_an_ordinary_row(capsys):
    """codex-reviewer, 600 r1 — the event that CAUSED the wedge.

    My first fix rendered `kind`/`slug` through `.get()` with a `?` default, so
    the missing-field event printed as an ordinary row and returned a poison
    count of ZERO: the crash was traded for a quiet lie, and the loud-and-counted
    promise was unmet for exactly the case that motivated the change.

    Worse, my own test permitted it — `assert poison == 0 or "POISON" in err or
    "?" in out` is a disjunction satisfied by every possible outcome. A test that
    cannot fail is worse than no test, because it reads as coverage. This asserts
    BOTH required outcomes and would redden on either.
    """
    for field in ("kind", "slug"):
        bad = _event(rid="x")
        del bad[field]
        poison = cli._print_queue_events([bad], json_mode=False)
        cap = capsys.readouterr()
        assert poison == 1, (
            f"an event missing {field!r} was counted as clean: poison={poison}")
        assert "POISON" in cap.err, (
            f"an event missing {field!r} was not rendered as POISON: {cap.err!r}")
        assert cap.out.strip() == "", (
            f"an event missing {field!r} printed as an ORDINARY row — the quiet "
            f"lie: {cap.out!r}")
        assert field in cap.err, (
            f"the POISON line does not name the missing field: {cap.err!r}")


def test_an_EMPTY_required_field_counts_as_missing(capsys):
    """`kind: ""` carries no more meaning than no `kind` at all."""
    poison = cli._print_queue_events([_event(rid="x", slug="")], json_mode=False)
    cap = capsys.readouterr()
    assert poison == 1 and "POISON" in cap.err, (
        f"an empty required field slipped through as renderable: {cap.out!r}")


def test_a_WELL_FORMED_event_is_not_poison(capsys):
    """The other direction: the validation must not condemn healthy traffic, and
    optional fields keep their honest `?`/`-` defaults."""
    poison = cli._print_queue_events(
        [_event(rid="a"), _event(rid="b", priority=None, ptr=None, **{"from": None})],
        json_mode=False)
    cap = capsys.readouterr()
    assert poison == 0, f"a well-formed event was called poison: {cap.err!r}"
    assert cap.out.count("\n") == 2, f"events were not rendered: {cap.out!r}"


def test_an_unformattable_event_is_counted_not_raised(capsys):
    """A non-dict cannot be validated OR formatted; it must still be counted and
    shown rather than taking the process down."""
    poison = cli._print_queue_events([object()], json_mode=False)
    err = capsys.readouterr().err
    assert poison == 1, "an unrenderable event was silently skipped"
    assert "POISON" in err, f"poison was not rendered loudly: {err!r}"


def test_the_renderer_cannot_raise_on_any_shape():
    """The formatter is the last line of defence for the delivery path; a
    formatter that can fail here IS the bug."""
    for shape in ({}, {"kind": None}, {"slug": None},
                  {"recorded_at": None}, {"recorded_at": 12345},
                  object(), None, {"from": object()}):
        cli._print_queue_events([shape], json_mode=False)


def test_JSON_mode_delivers_poison_visibly_and_does_not_raise(monkeypatch, capsys):
    """codex-reviewer, 600 r2 — the mode automation actually uses.

    Round 2 validated inside the TEXT renderer, which `cmd_queue` calls only
    when `json_mode` is false. So `--json` skipped validation entirely, saved
    the cursor, and then raised inside `_queue_result_envelope` on
    `event["kind"]`: rc 1, no envelope, cursor advanced. The permanent wedge
    traded for SILENT LOSS in the machine-readable channel — strictly worse,
    because a wedge at least stops the line.

    Consume policy, made explicit: poison IS consumed, and that is only
    defensible because it APPEARS in the envelope. The assertions below bind
    those two together — visible, counted, rc 0, cursor advanced.
    """
    window = [_event(rid="a"), _event(rid="b", kind=None), _event(rid="c")]
    monkeypatch.setattr(records, "events_for", lambda w, a: list(w))
    rc, t = _run(monkeypatch, window,
                 args=["queue", TEAM, "--agent", AGENT, "--json"])
    out = capsys.readouterr().out
    assert rc == 0, f"the --json path raised instead of delivering: rc={rc}"

    envelope = json.loads(out.strip().splitlines()[-1])
    assert envelope["poison_count"] == 1, (
        f"the malformed event vanished from the machine-readable channel: "
        f"{envelope}")
    assert [p["id"] for p in envelope["poison"]] == ["b"], (
        f"poison was counted but not identified: {envelope['poison']}")
    assert "missing required field" in envelope["poison"][0]["reason"]
    assert [e["id"] for e in envelope["events"]] == ["a", "c"], (
        "well-formed events must still be delivered alongside poison")
    assert _cursor_saved(t), "coverage was lost on the --json path"


def test_the_envelope_itself_cannot_raise_on_a_malformed_event():
    """Belt to the classifier's braces: the envelope builder used to subscript
    `kind`/`slug` directly, so it was a second place the delivery path could
    throw. A formatter on this path that can fail IS the defect."""
    env = cli._queue_result_envelope(
        [{"record_id": "x"}], cfg={"data_type": "X/1", "api_version": "v1"},
        cursor_path="p", advanced=True)
    assert env["events"][0]["kind"] is None
    assert env["poison_count"] == 0


def test_PEEK_still_does_not_advance(monkeypatch, capsys):
    """The one exit that legitimately skips the save must keep skipping it —
    the reachability invariant must not turn a peek into a consume."""
    monkeypatch.setattr(records, "events_for", lambda w, a: list(w))
    _rc, t = _run(monkeypatch, [_event(rid="a")],
                  args=["queue", TEAM, "--agent", AGENT, "--peek"])
    capsys.readouterr()
    assert not _cursor_saved(t), "a peek advanced the cursor"
