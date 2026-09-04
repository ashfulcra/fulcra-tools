"""Three more places where a 500 used to read as "the file isn't there".

`transport.read` returns None for a missing file and for a failed one alike;
`read_classified` separates them and says so in its own contract. The
2026-09-04 outage — HTTP 500 for an hour on every path three levels deep,
which is where role docs, task docs and vacancy rows all live — turned that
collapse from a latent defect into a live one. `roles claim` was fixed when it
was caught in the act (04b12fc1); these are the three siblings whose
consequence is a WRITE rather than a wrong sentence, found by auditing the
callers that fork on None:

* `acceptance peer_parks` creates an ephemeral role doc when it reads none —
  so an unreadable read overwrote a live role doc with a stub carrying its own
  maintainer and SLA;
* `task restore` refuses to move onto an occupied hot path — so an unreadable
  destination read as free, and the restore clobbered a live doc;
* `escalate` mints the day's vacancy row when it reads none — so an outage
  during the daily pass produced duplicate P1 rows AND duplicate bus events,
  fleet-wide.

Each of these fails toward doing MORE, which is why none of them would have
announced itself.
"""

from __future__ import annotations

import datetime

import pytest

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

TEAM = "fulcra"


class ReadsError(FakeTransport):
    """Reads fail the way the outage failed: error, never 'absent'."""

    def __init__(self, *, failing_prefix=""):
        super().__init__()
        self.failing_prefix = failing_prefix
        self.writes: list[str] = []

    def _fails(self, path):
        return path.startswith(self.failing_prefix)

    def read_classified(self, path, *, deadline=None):
        if self._fails(path):
            return None, "error"
        return super().read_classified(path)

    def read(self, path):
        return None if self._fails(path) else super().read(path)

    def write(self, path, content):
        self.writes.append(path)
        return super().write(path, content)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: datetime.datetime(
        2026, 9, 4, 4, 0, 0, tzinfo=datetime.timezone.utc))


def _task_doc(title="live work"):
    return "\n".join(["---", "type: Task", f"title: {title}", "id: keeper",
                      "status: active", "priority: P1", "owner: coord-boss",
                      "---", "", "# keeper", ""])


def test_restore_refuses_when_it_cannot_tell_whether_the_hot_path_is_occupied(capsys):
    """The destructive one. 'I could not read the destination' is not 'the
    destination is free', and a restore over a live doc is unrecoverable."""
    t = ReadsError(failing_prefix=f"team/{TEAM}/task/keeper.md")
    t.put(f"team/{TEAM}/task/keeper.md", _task_doc())
    t.put(f"team/{TEAM}/task/archive/2026-08/keeper.md", _task_doc("archived"))
    rc = cli.main(["task", "restore", TEAM, "keeper"], transport=t)
    err = capsys.readouterr().err
    assert rc == 3, err
    assert "UNKNOWN, not free" in err
    assert f"team/{TEAM}/task/keeper.md" not in t.writes


def test_restore_still_moves_when_the_hot_path_is_genuinely_free():
    """Positive control: the guard must not block the ordinary case."""
    t = FakeTransport()
    t.put(f"team/{TEAM}/task/archive/2026-08/keeper.md", _task_doc("archived"))
    rc = cli.main(["task", "restore", TEAM, "keeper"], transport=t)
    assert rc == 0
    assert t.read(f"team/{TEAM}/task/keeper.md") is not None


def test_restore_still_refuses_a_genuinely_occupied_hot_path(capsys):
    t = FakeTransport()
    t.put(f"team/{TEAM}/task/keeper.md", _task_doc())
    t.put(f"team/{TEAM}/task/archive/2026-08/keeper.md", _task_doc("archived"))
    rc = cli.main(["task", "restore", TEAM, "keeper"], transport=t)
    assert rc == 1
    assert "already exists in the hot path" in capsys.readouterr().err
