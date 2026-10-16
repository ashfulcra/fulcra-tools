"""Retention archives a task AND retires its bus-v4 obligation.

Measured 2026-10-16: most of a coordinator's owed rows pointed at task docs
retention had archived as quiet `proposed` work. Retention is not a v3 write, so
nothing reached `records.emit_event`, no v4 close was mirrored, and every fold
that held the open owed the row forever. These tests drive the actuator
(`reconcile.reconcile`) end to end against an in-memory store.
"""
import json

from coord_engine import cli, reconcile

from test_reconcile import FakeTransport

V4_TYPE = "MomentAnnotation/test-bus-v4"
OLD = "2020-01-15T00:00:00Z"
OLD_MTIME = "2020-01-15 04:00PM UTC"


class RecordingTransport(FakeTransport):
    def __init__(self, *, v4=True, record_ok=True):
        super().__init__()
        self.records_written = []
        self.record_ok = record_ok
        if v4:
            self.put("team/r/_coord/bus-v4/records.json",
                     json.dumps({"data_type": V4_TYPE, "api_version": "1"}))

    def record_write(self, data_type, api_version, note, source, recorded_at=None, tags=None):
        self.records_written.append({"data_type": data_type, "note": json.loads(note), "source": source})
        return self.record_ok


class UnreadableConfigTransport(RecordingTransport):
    def read_classified(self, path, *, deadline=None):
        if path == "team/r/_coord/bus-v4/records.json":
            return None, "error"
        return super().read_classified(path, deadline=deadline)


class DeleteRefusingTransport(RecordingTransport):
    def delete(self, path):
        if path.startswith("team/r/task/") and "/archive/" not in path:
            return False
        return super().delete(path)


def _quiet_proposed(t, slug, *, priority="P1", status="proposed", mtime=OLD_MTIME, assignee="some-agent"):
    t.put(f"team/r/task/{slug}.md",
          f"---\ntype: Task\ntitle: {slug}\nid: {slug}\nstatus: {status}\npriority: {priority}\n"
          f"owner: asker\nassignee: \"{assignee}\"\ntimestamp: {OLD}\n---\nbody", mtime=mtime)


def _run(t):
    return reconcile.reconcile(t, "r", now="2026-07-01T00:00:00Z", today="2026-07-01", host="reconciler")


def _v4(t, kind):
    return [w["note"] for w in t.records_written if w["data_type"] == V4_TYPE and w["note"].get("kind") == kind]


def _v4_closes(t):
    return _v4(t, "close")


def test_archiving_a_task_writes_one_v4_close_to_every_fold():
    t = RecordingTransport()
    _quiet_proposed(t, "quiet-ask")
    res = _run(t)
    assert "team/r/task/quiet-ask.md" not in t.store
    assert "team/r/task/archive/2020-01/quiet-ask.md" in t.store
    closes = _v4_closes(t)
    assert len(closes) == 1, t.records_written
    c = closes[0]
    assert (c["slug"], c["to"], c["from"], c["pri"], c["ptr"]) == (
        "quiet-ask", "all", "reconciler", "P1", "task/archive/2020-01/quiet-ask.md")
    assert c["v"] == 1 and c["at"] == "2026-07-01T00:00:00Z"
    assert not any("v4 close" in w for w in res["warnings"])


def test_terminal_task_archive_also_retires():
    t = RecordingTransport()
    _quiet_proposed(t, "old-done", status="done")
    _run(t)
    assert [c["slug"] for c in _v4_closes(t)] == ["old-done"]


def test_no_bus_v4_plane_archives_without_writing_or_warning():
    t = RecordingTransport(v4=False)
    _quiet_proposed(t, "quiet-ask")
    res = _run(t)
    assert "team/r/task/archive/2020-01/quiet-ask.md" in t.store
    assert t.records_written == []
    assert not any("v4 close" in w for w in res["warnings"])


def test_a_failed_close_is_a_warning_and_never_undoes_the_archive():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    res = _run(t)
    assert "team/r/task/quiet-ask.md" not in t.store
    assert "team/r/task/archive/2020-01/quiet-ask.md" in t.store
    assert any("v4 close FAILED" in w and "quiet-ask" in w for w in res["warnings"]), res["warnings"]


def test_a_task_whose_move_failed_is_not_closed():
    """Closing a task that is still hot would hide live work."""
    t = DeleteRefusingTransport()
    _quiet_proposed(t, "stuck")
    _run(t)
    assert "team/r/task/stuck.md" in t.store
    assert _v4_closes(t) == []


def test_live_and_recent_work_is_never_closed():
    t = RecordingTransport()
    _quiet_proposed(t, "busy", status="active")
    _quiet_proposed(t, "recently-touched", mtime=None)  # proposed, but the store mtime is recent
    _run(t)
    assert "team/r/task/busy.md" in t.store and "team/r/task/recently-touched.md" in t.store
    assert _v4_closes(t) == []


def test_an_unreadable_bus_v4_config_is_a_failed_close_never_no_plane():
    """The source doc is already gone and no later pass revisits it: skipping silently would leave the open forever."""
    t = UnreadableConfigTransport()
    _quiet_proposed(t, "quiet-ask")
    res = _run(t)
    assert "team/r/task/archive/2020-01/quiet-ask.md" in t.store
    assert t.records_written == []
    assert any("v4 close FAILED" in w and "quiet-ask" in w for w in res["warnings"]), res["warnings"]


# --- task restore re-opens what retention closed -------------------------------

def _archived(t, slug, **kw):
    _quiet_proposed(t, slug, **kw)
    _run(t)
    assert f"team/r/task/archive/2020-01/{slug}.md" in t.store
    t.records_written.clear()


def test_restore_reopens_the_obligation_retention_closed():
    t = RecordingTransport()
    _archived(t, "quiet-ask")
    assert cli.main(["task", "restore", "r", "quiet-ask"], transport=t) == 0
    assert "team/r/task/quiet-ask.md" in t.store
    opens = _v4(t, "open")
    assert len(opens) == 1, t.records_written
    o = opens[0]
    assert (o["slug"], o["to"], o["from"], o["pri"], o["ptr"]) == (
        "quiet-ask", "some-agent", "asker", "P1", "task/quiet-ask.md")


def test_restore_of_a_broadcast_reopens_to_all():
    t = RecordingTransport()
    _archived(t, "fleet-ask", assignee="*")
    assert cli.main(["task", "restore", "r", "fleet-ask"], transport=t) == 0
    assert [(o["to"], o["from"]) for o in _v4(t, "open")] == [("all", "asker")]


def test_restore_of_a_terminal_task_opens_nothing():
    t = RecordingTransport()
    _archived(t, "old-done", status="done")
    assert cli.main(["task", "restore", "r", "old-done"], transport=t) == 0
    assert _v4(t, "open") == []


def test_a_failed_reopen_is_loud_and_nonzero_but_keeps_the_restored_doc(capsys):
    t = RecordingTransport()
    _archived(t, "quiet-ask")
    t.record_ok = False
    assert cli.main(["task", "restore", "r", "quiet-ask"], transport=t) == 3
    assert "team/r/task/quiet-ask.md" in t.store
    assert "bus-v4 open was NOT written" in capsys.readouterr().err
