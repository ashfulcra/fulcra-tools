"""The v4 read model: a consumer's fold IS their "blocked on me" set.

Every test here pins a rule the v3 reader got wrong and paid for in production.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from coord_tracker_bridge.fold_source import (
    FoldRow,
    FoldSourceAdapter,
    FoldSourceError,
    parse_open,
)
from coord_tracker_bridge.model import CapabilityState

NOW = datetime(2026, 9, 5, 19, tzinfo=timezone.utc)


class FakeReader:
    """A real transport shape, not a mock: read_classified returns the same
    (body, state) triplet the CLI reader does, so the adapter's own branches run."""

    def __init__(self, docs: dict[str, str] | None = None, errors: set[str] = frozenset()):
        self.docs = docs or {}
        self.errors = set(errors)
        self.reads: list[str] = []

    def read_classified(self, path):
        self.reads.append(path)
        if path in self.errors:
            return None, "error"
        if path in self.docs:
            return self.docs[path], "ok"
        return None, "absent"


def checkpoint(open_rows: dict) -> dict:
    return {"v": 1, "cursor": "2026-09-05T19:00:00Z", "open": open_rows,
            "unread_events": 0, "unreadable_pointers": [], "seen": [],
            "generation": 3, "writer": "someone"}


def row(sender="coord-boss", pri="P1", ptr="team/fulcra/task/a.md", at="2026-09-05T18:00:00Z", **kw):
    return {"pri": pri, "from": sender, "ptr": ptr, "at": at, **kw}


CHECKPOINT_PATH = "team/fulcra/member/ash/fold/checkpoint.json"


def reader_with(open_rows=None, docs=None, errors=frozenset()):
    """A reader serving ash's checkpoint plus whatever pointer documents.

    The checkpoint is served as JSON at its REAL path, so coord_fold's own
    `checkpoint.load` runs for real against it -- the point is to exercise the
    actual boundary, not a stand-in for it.
    """

    if open_rows is None:
        open_rows = {"a-slug": row()}
    all_docs = {CHECKPOINT_PATH: json.dumps(checkpoint(open_rows))}
    all_docs.update(docs or {})
    return FakeReader(all_docs, errors)


def adapter(reader, **kw):
    return FoldSourceAdapter(
        team="fulcra", consumer="ash", reader=reader, writer=None,
        read_only=True, clock=lambda: NOW, **kw
    )


# ── parsing the boundary ─────────────────────────────────────────────────────

def test_an_absent_open_set_is_never_an_empty_fold():
    """"Nothing is blocked on you" is the most misleading thing this bridge can
    say. An absent `open` is a checkpoint we could not read, not a clear queue."""

    with pytest.raises(FoldSourceError):
        parse_open({"v": 1, "cursor": "x"})


@pytest.mark.parametrize("bad", [
    {"pri": "P1", "from": "a", "ptr": "p"},              # no at
    {"pri": "P1", "from": "a", "at": "t"},               # no ptr
    {"pri": "P1", "ptr": "p", "at": "t"},                # no from -- nobody to answer
    {"from": "a", "ptr": "p", "at": "t"},                # no pri
    "not an object",
])
def test_an_unreadable_row_raises_rather_than_shrinking_the_queue(bad):
    """A row we cannot read is not a row that is not owed. Skipping it quietly
    shrinks the operator's queue, which is the silent-drop this package has now
    fixed at four separate boundaries."""

    with pytest.raises(FoldSourceError):
        parse_open({"open": {"a-slug": bad}})


def test_rows_parse_into_a_typed_shape_not_scraped_text():
    rows = parse_open({"open": {"b": row(), "a": row(sender="codex-coder", pri="P0")}})
    assert [r.slug for r in rows] == ["a", "b"]          # deterministic order
    assert rows[0] == FoldRow(slug="a", pri="P0", sender="codex-coder",
                              ptr="team/fulcra/task/a.md", at="2026-09-05T18:00:00Z")


# ── what the snapshot means ──────────────────────────────────────────────────

def test_every_row_in_a_consumers_fold_is_blocked_on_that_consumer():
    """BY CONSTRUCTION, not by convention. The v3 reader had to infer this from
    a `blocked_on: user:x` field that most rows did not carry consistently, and
    the inference is why the reader and the view could disagree at all."""

    reader = reader_with(docs={"team/fulcra/task/a.md": "# Needs a spend decision\n\nbody"})
    snap = adapter(reader).snapshot()

    assert len(snap.items) == 1
    item = snap.items[0]
    assert item.blocked_on_user == "ash"
    assert item.lane == "asks"
    assert item.title == "Needs a spend decision"
    assert item.owner == "coord-boss"      # who gets unblocked by the answer


def test_the_slug_is_the_identity_with_no_fold_in_it():
    """v3 put the FOLD in the namespace, so one row surfaced in two folds became
    two cards -- 30 cards for 15 rows, measured. A v4 slug is the row."""

    reader = reader_with(docs={"team/fulcra/task/a.md": "# T"})
    snap = adapter(reader).snapshot()
    identity = snap.items[0].source
    assert identity.item_id == "a-slug"
    assert identity.namespace == "fulcra/ash"


def test_a_row_whose_pointer_is_MISSING_still_appears():
    """The fold says the row is real. An absent document is not evidence that
    nobody is waiting -- it is a document we do not have."""

    snap = adapter(reader_with()).snapshot()
    assert [i.title for i in snap.items] == ["a-slug"]
    assert "could not be read" in snap.items[0].description


def test_an_UNREADABLE_pointer_costs_completeness_but_an_absent_one_does_not():
    """The distinction this package keeps paying for: "the document says
    nothing" and "the read did not answer" are different facts."""

    absent = adapter(reader_with()).snapshot()
    assert absent.complete is True

    errored = adapter(reader_with(errors={"team/fulcra/task/a.md"})).snapshot()
    assert errored.complete is False
    assert errored.capabilities["asks"] is CapabilityState.DEGRADED
    assert [d.code for d in errored.diagnostics] == ["pointer-unreadable"]


def test_read_only_refuses_an_identity_that_has_never_folded():
    """"fresh" means nobody has folded for this identity, so an empty open set
    is UNKNOWN rather than measured. Projecting it as empty would close every
    card the operator has -- the same shape as the absence-close that once
    queued 52 live cards."""

    class Fresh(FakeReader):
        def read_classified(self, path):
            return None, "absent"

    with pytest.raises(FoldSourceError):
        adapter(Fresh()).snapshot()
