"""Per-agent work EVENTS, so the read side stops sweeping — and cannot race.

U8, second instance: a listing cannot date a collection, so "has this agent done
work recently" cost one listing per review directory — 438 on the live store,
which a 120s budget could not finish. 593 reordered the halves (2 agents -> 11);
this replaces the sweep for agents that have events.

WHY IMMUTABLE EVENTS RATHER THAN ONE POINTER FILE. The first version wrote a
single mutable `LATEST-work.json` guarded by a read-compare-write "monotonic"
check. codex-reviewer reproduced two races against it (594 r1):

  - two hosts both read the old value, and the OLDER stamp could land last,
    overwriting the newer;
  - worse, the failed-write branch deleted the shared path unconditionally, so
    it could ERASE a newer pointer another host had just written.

The store offers no conditional or versioned write, so a shared mutable path
cannot be defended. The fix is not to have one. A writer only ever CREATES its
own event; "newest" is a deterministic fold over ISO-led names; a failed write
leaves every prior event intact and still true.
"""

from __future__ import annotations

import json

from coord_engine import cli
from coord_engine.transport import TransportError

TEAM = "r"
AGENT = "worker-1"
PREFIX = f"team/{TEAM}/_coord/agents/{AGENT}/work/"


class _Store:
    def __init__(self, seed=None, fail_writes_all=False, raise_writes=False):
        self.store = dict(seed or {})
        self.fail_writes_all = fail_writes_all
        self.raise_writes = raise_writes
        self.deleted: list[str] = []

    def read(self, path):
        return self.store.get(path)

    def list_dir(self, prefix):
        return [{"name": p[len(prefix):]} for p in sorted(self.store)
                if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def write(self, path, text):
        if self.raise_writes:
            raise TransportError(f"write blew up: {path}")
        if self.fail_writes_all:
            return False
        self.store[path] = text
        return True

    def delete(self, path):
        self.deleted.append(path)
        self.store.pop(path, None)
        return True


def _event(store, kind="verdict", path="a.md", ts="2026-08-09T11:20:00Z"):
    return cli._stamp_work_pointer(store, TEAM, AGENT, kind=kind, path=path,
                                   now_iso=ts)


# --- codex's two reproductions, as regressions -------------------------------

def test_an_OLDER_stamp_arriving_last_cannot_hide_a_newer_one():
    """codex-reviewer, 594 r1, race one.

    Under read-compare-write, two hosts both read the old value and whichever
    wrote LAST won — so an older timestamp could overwrite a newer one. With
    immutable events there is nothing to overwrite: both exist and the fold
    picks the newest.
    """
    store = _Store()
    _event(store, kind="verdict", path="new.md", ts="2026-08-10T00:00:00Z")
    _event(store, kind="report", path="old.md", ts="2026-08-09T00:00:00Z")
    got = cli._read_work_pointer(store, TEAM, AGENT)
    assert got["ts"] == "2026-08-10T00:00:00Z", (
        f"an older stamp arriving last hid the newer work: {got}")
    assert got["kind"] == "verdict"


def test_a_FAILED_write_deletes_NOTHING():
    """codex-reviewer, 594 r1, race two — the worse one.

    The old failed-write branch deleted the shared pointer unconditionally, so a
    host whose own write failed could erase a NEWER pointer another host had
    just written. Nothing may be deleted on failure; prior events are still
    true, and the reader degrades to slightly-stale rather than wrong.
    """
    store = _Store()
    _event(store, kind="verdict", path="theirs.md", ts="2026-08-10T00:00:00Z")
    store.fail_writes_all = True
    assert _event(store, kind="report", path="mine.md",
                  ts="2026-08-09T00:00:00Z") is False
    assert store.deleted == [], (
        f"a failed write deleted another host's event: {store.deleted}")
    got = cli._read_work_pointer(store, TEAM, AGENT)
    assert got["ts"] == "2026-08-10T00:00:00Z", (
        "the newer event must survive a concurrent failed write")


def test_a_RAISING_write_also_deletes_nothing():
    store = _Store()
    _event(store, kind="verdict", path="theirs.md", ts="2026-08-10T00:00:00Z")
    store.raise_writes = True
    assert _event(store, kind="report", path="mine.md",
                  ts="2026-08-09T00:00:00Z") is False
    assert store.deleted == []
    assert cli._read_work_pointer(store, TEAM, AGENT)["ts"] == "2026-08-10T00:00:00Z"


def test_two_hosts_recording_the_SAME_event_are_idempotent():
    """Same instant, same artifact => same filename and same bytes, so a
    duplicate is a no-op rather than a conflict."""
    store = _Store()
    _event(store, kind="verdict", path="a.md", ts="2026-08-09T11:20:00Z")
    _event(store, kind="verdict", path="a.md", ts="2026-08-09T11:20:00Z")
    assert len(store.list_dir(PREFIX)) == 1


def test_distinct_events_at_the_SAME_instant_both_survive():
    """Two different artifacts recorded in the same second must not collide —
    the digest in the name keeps them distinct."""
    store = _Store()
    _event(store, kind="verdict", path="a.md", ts="2026-08-09T11:20:00Z")
    _event(store, kind="report", path="b.md", ts="2026-08-09T11:20:00Z")
    assert len(store.list_dir(PREFIX)) == 2


# --- the read contract -------------------------------------------------------

def test_no_events_reads_UNKNOWN_never_absent():
    """The whole fleet has no events on day one. That must mean "ask the
    sweep", never "this agent did nothing" — 585's missing-pointer rule."""
    assert cli._read_work_pointer(_Store(), TEAM, AGENT) is None


def test_an_unreadable_listing_reads_UNKNOWN():
    class _Dead(_Store):
        def list_dir(self, prefix):
            raise TransportError("listing down")
    assert cli._read_work_pointer(_Dead(), TEAM, AGENT) is None


def test_a_corrupt_newest_event_reads_UNKNOWN_rather_than_raising():
    """One bad event must not break the fold for every other agent."""
    store = _Store(seed={PREFIX + "2026-08-09T11:20:00Z-deadbeef.json": "{nope"})
    assert cli._read_work_pointer(store, TEAM, AGENT) is None


def test_the_event_carries_kind_path_and_ts():
    """Constraint 4: attributable. `kind` is what lets a row say "verdict, 20h"
    instead of naming whichever artifact a scan happened to reach."""
    store = _Store()
    _event(store, kind="verdict", path="team/r/review/pr-1/verdicts/x--rev.md",
           ts="2026-08-09T11:20:00Z")
    got = cli._read_work_pointer(store, TEAM, AGENT)
    assert got["kind"] == "verdict"
    assert got["path"].endswith("x--rev.md")
    assert got["ts"] == "2026-08-09T11:20:00Z"


def test_pruning_keeps_the_newest_and_never_touches_them():
    store = _Store()
    for i in range(cli.WORK_EVENTS_KEEP + 3):
        _event(store, kind="tell", path=f"{i}.md", ts=f"2026-08-{10+i:02d}T00:00:00Z")
    names = [r["name"] for r in store.list_dir(PREFIX)]
    assert len(names) == cli.WORK_EVENTS_KEEP
    got = cli._read_work_pointer(store, TEAM, AGENT)
    assert got["ts"].startswith(f"2026-08-{10 + cli.WORK_EVENTS_KEEP + 2:02d}")


# --- constraint 1: ONE write site, reached through real dispatch -------------

def test_a_real_write_verb_records_an_event_through_the_chokepoint(monkeypatch):
    """Twice before I built a decision function and left it unconnected, so this
    drives `cli.main` and asserts a REAL verb produces a REAL event."""
    from coord_engine_test_helpers import FakeTransport
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-9")
    t = FakeTransport()
    assert cli.main(["tell", TEAM, "amy", "ship it"], transport=t) == 0
    events = [p for p in t.store
              if p.startswith(f"team/{TEAM}/_coord/agents/worker-9/work/")]
    assert events, f"no work event written; paths: {sorted(t.store)}"
    doc = json.loads(t.store[events[0]])
    assert doc["agent"] == "worker-9" and doc["kind"] == "tell" and doc["ts"]


def test_a_READ_verb_records_nothing(monkeypatch):
    from coord_engine_test_helpers import FakeTransport
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-9")
    t = FakeTransport()
    cli.main(["presence", "show", TEAM], transport=t)
    assert not any("/work/" in p for p in t.store), (
        f"a read verb recorded work: {sorted(t.store)}")


# --- codex's second blocker: the side-channel must not outlive the call ------

def test_the_artifact_side_channel_lives_on_args_and_cannot_leak():
    """codex-reviewer, 594 r1, blocker two.

    The path used to live in a module-global keyed by `id(args)`, cleaned up
    only on the NORMAL dispatch path — so a raising command left its entry
    behind and a later object at the same id could inherit it. State attached to
    the args object dies with the object; there is nothing to clean up, so there
    is no path on which cleanup can be skipped.
    """
    assert not hasattr(cli, "_ACTIVITY_ARTIFACT_PATH"), (
        "the id-keyed global is back; that is the leak")

    import argparse
    args = argparse.Namespace()
    cli.record_activity_artifact(args, "team/r/some/doc.md")
    assert getattr(args, "_activity_artifact_path") == "team/r/some/doc.md"

    # A different invocation shares nothing, even if the first is discarded.
    other = argparse.Namespace()
    assert getattr(other, "_activity_artifact_path", None) is None


def test_a_RAISING_command_leaves_no_artifact_state(monkeypatch):
    """The exception path, end to end: a command that records a path and then
    blows up must not leave anything an unrelated command could inherit."""
    from coord_engine_test_helpers import FakeTransport
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-9")

    def _boom(args, transport):
        cli.record_activity_artifact(args, "team/r/doomed.md")
        raise RuntimeError("command exploded")

    monkeypatch.setattr(cli, "cmd_tell", _boom)
    t = FakeTransport()
    rc = cli.main(["tell", TEAM, "amy", "x"], transport=t)
    assert rc != 0
    assert not any("/work/" in p for p in t.store), (
        "a failed command recorded a work event")
    assert not hasattr(cli, "_ACTIVITY_ARTIFACT_PATH")
