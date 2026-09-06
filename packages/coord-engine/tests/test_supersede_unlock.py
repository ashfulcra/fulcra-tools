"""D3 (supersede) + D4 (block --unlock) — respec 2026-07-28 adoptions."""
import argparse
from datetime import datetime, timezone

import pytest

from coord_engine import cli, tasks


NOW = "2026-07-28T21:00:00Z"


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    monkeypatch.setattr(
        cli, "_now",
        lambda: datetime(2026, 7, 28, 21, 0, 0, tzinfo=timezone.utc))


def _doc(status="waiting"):
    return (f"---\ntype: Task\ntitle: T\nstatus: {status}\npriority: P2\n"
            f"id: t1\n---\n\nbody\n")


# --- apply_update level -----------------------------------------------------

def test_supersede_closes_from_any_live_state():
    for status in ("proposed", "active", "waiting", "blocked"):
        out = tasks.apply_update(_doc(status), now=NOW, status="done",
                                 superseded_by="new-slug",
                                 evidence="superseded by new-slug")
        assert "superseded_by: new-slug" in out
        assert "status: done" in out


def test_plain_done_still_respects_the_status_machine():
    with pytest.raises(tasks.TaskError):
        tasks.apply_update(_doc("waiting"), now=NOW, status="done",
                           evidence="e")  # no superseded_by -> machine holds


def test_unlock_field_written():
    out = tasks.apply_update(_doc("active"), now=NOW, status="blocked",
                             blocked_on="ash", unlock="merge PR 999")
    assert "unlock: merge PR 999" in out
    assert "blocked_on: ash" in out


# --- cmd level --------------------------------------------------------------

class T:
    def __init__(self, doc):
        self.doc = doc
        self.written = None

    def read(self, path):
        return self.doc

    def write(self, path, content):
        self.written = content
        return True


def _args(**kw):
    d = dict(team="acme", name="t1", verb="x")
    d.update(kw)
    return argparse.Namespace(**d)


def test_cmd_block_requires_unlock(capsys):
    rc = cli.cmd_task_block(_args(blocked_on="ash", on_user=None, unlock=None), T(_doc("active")))
    assert rc == 1
    assert "--unlock" in capsys.readouterr().err


def test_cmd_block_with_unlock_writes_field(capsys):
    t = T(_doc("active"))
    rc = cli.cmd_task_block(_args(blocked_on="ash", on_user=None,
                                  unlock="Ash merges PR 999"), t)
    assert rc == 0
    assert "unlock: Ash merges PR 999" in t.written


def test_cmd_supersede(capsys):
    t = T(_doc("waiting"))
    rc = cli.cmd_task_supersede(_args(by="pr-500", reason=None), t)
    assert rc == 0
    assert "superseded_by: pr-500" in t.written
    assert "superseded by pr-500" in t.written


def test_supersede_cannot_rewrite_terminal_states():
    # pr-491 round-1 finding: terminal dispositions are immutable — a
    # supersession may close live work, never rewrite closed work.
    for status in ("done", "abandoned"):
        with pytest.raises(tasks.TaskError):
            tasks.apply_update(_doc(status), now=NOW, status="done",
                               superseded_by="new-slug",
                               evidence="superseded by new-slug")
