"""Retention archives a task AND retires its bus-v4 obligation.

Measured 2026-10-16: most of a coordinator's owed rows pointed at task docs
retention had archived as quiet `proposed` work. Retention is not a v3 write, so
nothing reached `records.emit_event`, no v4 close was mirrored, and every fold
that held the open owed the row forever. These tests drive the actuator
(`reconcile.reconcile`) end to end against an in-memory store.
"""
import json

from coord_engine import reconcile

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


class DeleteRefusingTransport(RecordingTransport):
    def delete(self, path):
        if path.startswith("team/r/task/") and "/archive/" not in path:
            return False
        return super().delete(path)


def _quiet_proposed(t, slug, *, priority="P1", status="proposed", mtime=OLD_MTIME):
    t.put(f"team/r/task/{slug}.md",
          f"---\ntype: Task\ntitle: {slug}\nid: {slug}\nstatus: {status}\npriority: {priority}\n"
          f"assignee: some-agent\ntimestamp: {OLD}\n---\nbody", mtime=mtime)


def _run(t):
    return reconcile.reconcile(t, "r", now="2026-07-01T00:00:00Z", today="2026-07-01", host="reconciler")


def _v4_closes(t):
    return [w["note"] for w in t.records_written if w["data_type"] == V4_TYPE and w["note"].get("kind") == "close"]


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
