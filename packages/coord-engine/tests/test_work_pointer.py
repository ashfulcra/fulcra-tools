"""A per-agent work pointer, so the read side stops sweeping to answer.

U8, second instance: a listing cannot date a collection, so "has this agent done
work recently" cost one listing per review directory — 438 of them on the live
store. Measured from merged main, a 120s budget (6x the default) still could not
finish. 593 reordered the halves and took coverage from 2 agents to 11, which is
mitigation; this is the fix. One read per agent instead of O(reviews).

coord-boss's four constraints, 2026-08-09, and why each is a test here:

  1. ONE WRITE SITE — stamped from the 590 activity chokepoint, never per-verb.
     Pointer coverage then INHERITS 590's classification, so a newly added write
     verb stamps by default. The "someone forgot the stamp" failure is designed
     out rather than tested for.
  2. 585/588 refusal semantics EXACTLY — written only after the artifact
     persists; a missing pointer is UNKNOWN and never stale; a FAILED update
     removes the stale pointer rather than leaving it to be believed; monotonic.
  3. Transitional — a pointer-less agent reads UNKNOWN and falls back to the
     593 sweep, which becomes the pointer-less-only path.
  4. kind + path + ts, so freshness is ATTRIBUTABLE. Without this,
     codex-reviewer's row said "whichever artifact the scan reached"; with it,
     the row can say "verdict, 20h".
"""

from __future__ import annotations

import json

from coord_engine import cli
from coord_engine.transport import TransportError

TEAM = "r"
AGENT = "worker-1"
PTR = f"team/{TEAM}/_coord/agents/{AGENT}/LATEST-work.json"


class _Store:
    def __init__(self, seed=None, fail_writes=(), raise_writes=()):
        self.store = dict(seed or {})
        self.fail_writes = set(fail_writes)
        self.raise_writes = set(raise_writes)
        self.deleted: list[str] = []
        self.writes: list[str] = []

    def read(self, path):
        return self.store.get(path)

    def list_dir(self, path):
        return []

    def write(self, path, text):
        self.writes.append(path)
        if path in self.raise_writes:
            raise TransportError(f"write blew up: {path}")
        if path in self.fail_writes:
            return False          # the transport's quiet-failure contract
        self.store[path] = text
        return True

    def delete(self, path):
        self.deleted.append(path)
        self.store.pop(path, None)
        return True


def _ptr(store):
    raw = store.read(PTR)
    return json.loads(raw) if raw else None


# --- constraint 4: the pointer is attributable -------------------------------

def test_the_pointer_carries_kind_path_and_ts():
    """Without all three the pointer answers "something happened" — which is
    what the sweep already said badly. `kind` is what makes a reviewer's row say
    "verdict, 20h" instead of naming whichever artifact was reached first."""
    store = _Store()
    assert cli._stamp_work_pointer(
        store, TEAM, AGENT, kind="verdict",
        path=f"team/{TEAM}/review/pr-1/verdicts/abc--{AGENT}.md",
        now_iso="2026-08-09T11:20:00Z") is True
    p = _ptr(store)
    assert p["kind"] == "verdict"
    assert p["path"].endswith(f"abc--{AGENT}.md")
    assert p["ts"] == "2026-08-09T11:20:00Z"


# --- constraint 2: 585/588 refusal semantics ---------------------------------

def test_a_failed_pointer_write_REMOVES_the_stale_pointer():
    """588's rule, and the one that matters most.

    A pointer that silently keeps an OLD value after a failed update is worse
    than no pointer: readers believe a stale fact with no way to tell. If the
    update cannot land, the pointer must go, so the reader degrades to UNKNOWN
    and falls back to the sweep.
    """
    store = _Store(seed={PTR: json.dumps(
        {"kind": "report", "path": "old.md", "ts": "2026-08-01T00:00:00Z"})},
        fail_writes={PTR})
    ok = cli._stamp_work_pointer(store, TEAM, AGENT, kind="verdict",
                                 path="new.md", now_iso="2026-08-09T11:20:00Z")
    assert ok is False
    assert PTR in store.deleted, (
        "a failed update must remove the stale pointer; leaving 2026-08-01 "
        "readable would have every reader believe a fact that is now wrong")
    assert store.read(PTR) is None


def test_a_RAISING_pointer_write_also_removes_it():
    """Same rule for the raising path — `write` returning False and `write`
    throwing are the same event to a reader."""
    store = _Store(seed={PTR: json.dumps(
        {"kind": "report", "path": "old.md", "ts": "2026-08-01T00:00:00Z"})},
        raise_writes={PTR})
    assert cli._stamp_work_pointer(store, TEAM, AGENT, kind="verdict",
                                   path="new.md",
                                   now_iso="2026-08-09T11:20:00Z") is False
    assert PTR in store.deleted


def test_the_pointer_is_MONOTONIC():
    """An out-of-order stamp must not walk the pointer backwards. Two hosts can
    act in either order; the pointer records the NEWEST work, not the last
    writer to arrive."""
    store = _Store()
    cli._stamp_work_pointer(store, TEAM, AGENT, kind="verdict", path="new.md",
                            now_iso="2026-08-09T11:20:00Z")
    cli._stamp_work_pointer(store, TEAM, AGENT, kind="report", path="old.md",
                            now_iso="2026-08-01T00:00:00Z")
    assert _ptr(store)["ts"] == "2026-08-09T11:20:00Z"
    assert _ptr(store)["kind"] == "verdict"


def test_an_unparseable_existing_pointer_is_overwritten_not_trusted():
    """Corrupt state must not freeze the pointer forever: an unreadable prior
    value cannot win a monotonic comparison it is not eligible for."""
    store = _Store(seed={PTR: "{not json"})
    assert cli._stamp_work_pointer(store, TEAM, AGENT, kind="verdict",
                                   path="new.md",
                                   now_iso="2026-08-09T11:20:00Z") is True
    assert _ptr(store)["ts"] == "2026-08-09T11:20:00Z"


# --- constraint 3: transitional, pointer-less agents stay UNKNOWN ------------

def test_a_missing_pointer_reads_UNKNOWN_never_absent():
    """The whole fleet is pointer-less on day one. A missing pointer must mean
    "ask the sweep", never "this agent did nothing" — the same rule as 585's
    missing health pointer."""
    store = _Store()
    assert cli._read_work_pointer(store, TEAM, AGENT) is None


def test_a_pointer_read_that_FAILS_is_also_UNKNOWN():
    """Unreadable is not empty. `read` returning None conflates missing with
    transport failure, and neither licenses an absence claim."""
    class _Dead(_Store):
        def read(self, path):
            return None
    assert cli._read_work_pointer(_Dead(), TEAM, AGENT) is None


def test_a_corrupt_pointer_reads_UNKNOWN_rather_than_raising():
    """One bad pointer must not break the fold for every other agent."""
    assert cli._read_work_pointer(_Store(seed={PTR: "{not json"}),
                                  TEAM, AGENT) is None


# --- constraint 1: ONE write site, and it must actually be reached -----------

def test_a_real_write_verb_stamps_the_pointer_through_the_chokepoint(monkeypatch):
    """The wiring test, driven through `cli.main`.

    Twice before I have built a decision function, unit-tested it, and left it
    unconnected. So this asserts a REAL verb produces a REAL pointer, and it
    goes through the same chokepoint that classifies activity — which is the
    whole point of constraint 1: coverage is inherited, not remembered.
    """
    from coord_engine_test_helpers import FakeTransport
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-9")
    t = FakeTransport()
    assert cli.main(["tell", TEAM, "amy", "ship it"], transport=t) == 0
    raw = t.store.get(f"team/{TEAM}/_coord/agents/worker-9/LATEST-work.json")
    assert raw, f"no pointer written; paths: {sorted(t.store)}"
    doc = json.loads(raw)
    assert doc["agent"] == "worker-9"
    assert doc["kind"] == "tell"
    assert doc["ts"]


def test_a_READ_verb_stamps_nothing(monkeypatch):
    """The other direction, inherited for free: a read is not work, so it must
    not leave a pointer claiming otherwise."""
    from coord_engine_test_helpers import FakeTransport
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-9")
    t = FakeTransport()
    cli.main(["presence", "show", TEAM], transport=t)
    assert not any("LATEST-work.json" in p for p in t.store), (
        f"a read verb wrote a work pointer: {sorted(t.store)}")
