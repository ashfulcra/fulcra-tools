"""`obligations --stream` — follow the signal to the doc, never scan the corpus.

Ash's design, asked for over six weeks before it was built: the bus payload has
always carried `ptr`, so the signal already names the document an obligation
lives in. The default path ignores it and folds the whole fleet, which is why it
degrades — measured on the live store 2026-08-29: 111.0s rc 3 with 5 of 7 probes
UNREADABLE, against 3,159 task docs and 950 review entries, versus 10.3s rc 0 for
this path.

THE COST CLAIM IS TESTED, not just the answer: these assert the number of reads
and that no listing of the task corpus happens at all. A fold that returned the
right answer by scanning would pass an answer-only test and defeat the purpose.
"""

import argparse
import json

import pytest

from coord_engine import cli, records
from test_reconcile import FakeTransport


TEAM = "fulcra"
AGENT = "coord-boss"
CFG = "team/fulcra/_coord/bus-v3/records.json"
DTYPE = "MomentAnnotation/test"


def _doc(status="active", priority="P1", blocked_on=None):
    lines = ["---", "type: Task", "title: A thing", "id: a-thing",
             f"status: {status}", f"priority: {priority}", "owner: someone"]
    if blocked_on:
        lines.append(f"blocked_on: {blocked_on}")
    lines += ["---", "", "# A thing", ""]
    return "\n".join(lines)


class StreamTransport(FakeTransport):
    """FakeTransport plus a scripted record window; counts reads and listings."""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.reads: list[str] = []
        self.listings: list[str] = []
        self.put(CFG, json.dumps({"data_type": DTYPE, "api_version": "v0"}))

    def records(self, data_type, since, until):
        return self.window

    def read(self, path):
        self.reads.append(path)
        return super().read(path)

    def list_dir(self, prefix):
        self.listings.append(prefix)
        return super().list_dir(prefix)

    def task_doc_reads(self):
        return [p for p in self.reads if "/task/" in p]


def _event(kind="directive", slug="a-thing", ptr="task/a-thing.md", **kw):
    note = records.build_payload(to=AGENT, kind=kind, priority="P1",
                                 slug=slug, ptr=ptr, **kw)
    return {"id": f"{kind}-{slug}-{kw}", "recorded_at": "2026-08-29T19:00:00Z",
            "note": note}


def _args(**kw):
    base = dict(team=TEAM, agent=AGENT, stream=True, lookback_hours=168, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _run(transport, capsys, **kw):
    rc = cli.cmd_obligations_stream(_args(**kw), transport)
    return rc, capsys.readouterr()


def test_a_directive_becomes_an_owed_row_via_its_ptr(capsys):
    t = StreamTransport([_event()])
    t.put(f"team/{TEAM}/task/a-thing.md", _doc())
    rc, out = _run(t, capsys)
    assert rc == 0
    assert "1 owed" in out.out and "a-thing" in out.out


def test_it_reads_ONLY_the_document_the_signal_points_at(capsys):
    """THE COST CLAIM. Ten unrelated tasks exist; one is signalled. A fold that
    got the right answer by scanning would pass an answer-only assertion, so
    assert the reads and the absence of any task-corpus listing."""
    t = StreamTransport([_event()])
    t.put(f"team/{TEAM}/task/a-thing.md", _doc())
    for i in range(10):
        t.put(f"team/{TEAM}/task/unrelated-{i}.md", _doc())
    rc, out = _run(t, capsys)
    assert rc == 0
    assert t.task_doc_reads() == [f"team/{TEAM}/task/a-thing.md"]
    assert not [p for p in t.listings if p.endswith("/task/")], t.listings


def test_an_fyi_opens_nothing(capsys):
    """Measured 2026-08-21: most of 92 stream-only 'opens' were FYIs replayed as
    permanent obligations. A notification is not an obligation."""
    t = StreamTransport([_event(fyi=True)])
    t.put(f"team/{TEAM}/task/a-thing.md", _doc())
    rc, out = _run(t, capsys)
    assert rc == 0 and "0 owed" in out.out
    assert t.task_doc_reads() == []


def test_a_response_in_the_window_discharges_the_directive(capsys):
    t = StreamTransport([_event(), _event(kind="response")])
    t.put(f"team/{TEAM}/task/a-thing.md", _doc())
    rc, out = _run(t, capsys)
    assert rc == 0 and "0 owed" in out.out


def test_a_terminal_document_is_not_owed_even_with_an_open_event(capsys):
    """The doc is the truth; the event is delivery. A close that never emitted
    still shows in the document."""
    t = StreamTransport([_event()])
    t.put(f"team/{TEAM}/task/a-thing.md", _doc(status="done"))
    rc, out = _run(t, capsys)
    assert rc == 0 and "0 owed" in out.out


def test_an_unreadable_ptr_is_UNKNOWN_never_discharged(capsys):
    """NEGATIVE CONTROL and the one that matters most: absent-or-unreadable must
    never read as 'nothing owed'. That conflation is the failure this whole lane
    exists to remove."""
    t = StreamTransport([_event()])  # no document written
    rc, out = _run(t, capsys)
    assert rc == 3
    assert "cannot claim discharged" in out.err


def test_an_unreadable_window_is_UNKNOWN_not_empty(capsys):
    t = StreamTransport(None)
    rc, out = _run(t, capsys)
    assert rc == 3 and "UNKNOWN" in out.err


def test_the_gap_before_the_window_is_never_silent_without_a_checkpoint(capsys):
    """Without a checkpoint this path answers for its window only, and must say
    so rather than reading as clear. (Originally asserted the pre-checkpoint
    wording; the invariant is the same — the gap is named, never implied.)"""
    t = StreamTransport([])
    rc, out = _run(t, capsys)
    assert "NO CHECKPOINT" in out.err and "UNKNOWN" in out.err


def test_blocked_on_is_carried_onto_the_owed_row(capsys):
    t = StreamTransport([_event()])
    t.put(f"team/{TEAM}/task/a-thing.md", _doc(blocked_on="user:ash"))
    rc, out = _run(t, capsys)
    assert "blocked_on=user:ash" in out.out


# --- the checkpoint: completeness, not just speed ----------------------------

CKPT = f"team/{TEAM}/_coord/agents/{AGENT}/obligations-checkpoint.json"


def _checkpoint(as_of="2026-08-29T12:00:00Z", open_rows=None):
    return json.dumps({"v": 1, "as_of": as_of, "seeded_by": "corpus-fold",
                       "open": open_rows if open_rows is not None else []})


def test_a_checkpointed_obligation_survives_into_an_empty_window(capsys):
    """THE COMPLETENESS CLAIM: an obligation opened before the window is owed
    via the checkpoint, with zero window events."""
    t = StreamTransport([])
    t.put(CKPT, _checkpoint(open_rows=[{"slug": "old-thing",
                                        "ptr": "task/old-thing.md",
                                        "priority": "P1"}]))
    t.put(f"team/{TEAM}/task/old-thing.md", _doc())
    rc, out = _run(t, capsys)
    assert rc == 0
    assert "1 owed" in out.out and "old-thing" in out.out
    assert "NO CHECKPOINT" not in out.err


def test_a_window_response_discharges_a_checkpointed_obligation(capsys):
    t = StreamTransport([_event(kind="response", slug="old-thing",
                                ptr="task/old-thing.md")])
    t.put(CKPT, _checkpoint(open_rows=[{"slug": "old-thing",
                                        "ptr": "task/old-thing.md"}]))
    rc, out = _run(t, capsys)
    assert rc == 0 and "0 owed" in out.out


def test_the_window_starts_at_the_checkpoint_not_the_queue_cursor(capsys):
    """The queue cursor tracks the DELIVERY consumer and can be ahead of the
    last obligation fold; starting there would skip events the queue consumed
    but this fold never saw."""
    t = StreamTransport([])
    t.put(CKPT, _checkpoint(as_of="2026-08-29T12:00:00Z"))
    t.put(records.cursor_path(TEAM, AGENT),
          json.dumps({"v": 1, "last_read": "2026-08-29T18:00:00Z", "seen_ids": []}))
    rc, out = _run(t, capsys)
    assert "[2026-08-29T12:00:00Z," in out.out


def test_a_clean_stream_fold_advances_the_checkpoint(capsys):
    t = StreamTransport([_event()])
    t.put(f"team/{TEAM}/task/a-thing.md", _doc())
    t.put(CKPT, _checkpoint())
    rc, out = _run(t, capsys)
    assert rc == 0
    saved = json.loads(t.store[CKPT])
    assert saved["seeded_by"] == "stream-fold"
    assert saved["as_of"] > "2026-08-29T12:00:00Z"
    assert [r["slug"] for r in saved["open"]] == ["a-thing"]


def test_an_unclean_fold_does_NOT_advance_the_checkpoint(capsys):
    """NEGATIVE CONTROL, the one that matters: an unresolved ptr means the open
    set is not fully known, and a checkpoint that guesses converts one bad read
    into a durable lie."""
    t = StreamTransport([_event()])  # doc never written -> ptr unreadable
    t.put(CKPT, _checkpoint())
    rc, out = _run(t, capsys)
    assert rc == 3
    saved = json.loads(t.store[CKPT])
    assert saved["seeded_by"] == "corpus-fold", "checkpoint must be untouched"
    assert saved["as_of"] == "2026-08-29T12:00:00Z"


def test_a_malformed_checkpoint_is_ignored_never_trusted(capsys):
    """A parse error must not read as 'nothing was owed as of then'."""
    t = StreamTransport([])
    t.put(CKPT, "{not json")
    rc, out = _run(t, capsys)
    assert "NO CHECKPOINT" in out.err
