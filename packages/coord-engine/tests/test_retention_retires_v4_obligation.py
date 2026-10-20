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


# --- a failed close is retried on later passes, never lost ---------------------
# Measured 2026-10-20 00:10Z: a pinned host archived five tasks and wrote no v4 closes. The close
# failure was a one-line warning and no later pass revisited the archived docs, so the opens stayed.

PENDING = "team/r/_coord/retention/pending-v4-close/"


def test_a_failed_close_is_queued_and_the_warning_says_so():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    res = _run(t)
    marker = json.loads(t.store[PENDING + "quiet-ask.json"])
    assert (marker["slug"], marker["ptr"], marker["priority"]) == (
        "quiet-ask", "task/archive/2020-01/quiet-ask.md", "P1")
    assert any("v4 close FAILED" in w and "queued for retry" in w for w in res["warnings"]), res["warnings"]


def test_a_successful_close_leaves_no_marker():
    t = RecordingTransport()
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    assert [p for p in t.store if p.startswith(PENDING)] == []


def test_the_next_pass_retries_even_when_the_daily_throttle_skips_retention():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    t.records_written.clear()
    t.record_ok = True
    _run(t)  # same `today`: the archive sub-pass is throttled, the retry is not
    closes = _v4_closes(t)
    assert [(c["slug"], c["to"], c["ptr"]) for c in closes] == [
        ("quiet-ask", "all", "task/archive/2020-01/quiet-ask.md")]
    assert PENDING + "quiet-ask.json" not in t.store


def test_a_pass_that_died_before_its_close_is_finished_by_the_next():
    """The marker is written BEFORE the close, so a pass killed in between still leaves it."""
    t = RecordingTransport()
    t.put("team/r/task/archive/2020-01/slept.md", "---\nstatus: proposed\n---\nb")
    t.put(PENDING + "slept.json", json.dumps({"slug": "slept", "ptr": "task/archive/2020-01/slept.md",
                                              "priority": "P2"}))
    _run(t)
    assert [c["slug"] for c in _v4_closes(t)] == ["slept"]
    assert PENDING + "slept.json" not in t.store


def test_a_close_that_keeps_failing_stays_queued_and_loud():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    res = _run(t)
    assert PENDING + "quiet-ask.json" in t.store
    assert any("FAILED again" in w and "quiet-ask" in w for w in res["warnings"]), res["warnings"]


def test_a_restored_task_is_never_closed_by_a_retry():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    t.put("team/r/task/quiet-ask.md", "---\nstatus: proposed\nassignee: some-agent\n---\nback")
    t.records_written.clear()
    t.record_ok = True
    _run(t)
    assert _v4_closes(t) == []
    assert PENDING + "quiet-ask.json" not in t.store


def test_an_unknown_hot_path_keeps_the_marker_and_closes_nothing():
    class HotUnknown(RecordingTransport):
        def read_classified(self, path, *, deadline=None):
            if path == "team/r/task/quiet-ask.md":
                return None, "error"
            return super().read_classified(path, deadline=deadline)

    t = HotUnknown(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    t.records_written.clear()
    t.record_ok = True
    res = _run(t)
    assert _v4_closes(t) == []
    assert PENDING + "quiet-ask.json" in t.store
    assert any("UNKNOWN" in w and "quiet-ask" in w for w in res["warnings"]), res["warnings"]


def test_a_malformed_marker_is_kept_and_reported():
    t = RecordingTransport()
    t.put(PENDING + "junk.json", "not json")
    res = _run(t)
    assert PENDING + "junk.json" in t.store
    assert any("junk.json" in w and "UNKNOWN" in w for w in res["warnings"]), res["warnings"]


def test_restore_clears_a_pending_close_before_reopening():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    assert PENDING + "quiet-ask.json" in t.store
    t.record_ok = True
    assert cli.main(["task", "restore", "r", "quiet-ask"], transport=t) == 0
    assert PENDING + "quiet-ask.json" not in t.store


# --- review round 1 (Greptile on PR 832) ---------------------------------------

class OrderTransport(RecordingTransport):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.ops = []

    def write(self, path, content):
        self.ops.append(("write", path))
        return super().write(path, content)

    def delete(self, path):
        self.ops.append(("delete", path))
        return super().delete(path)


def test_the_close_is_queued_before_the_source_is_deleted():
    """A pass killed between the source delete and the close must still leave the debt recorded."""
    t = OrderTransport()
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    marker_write = t.ops.index(("write", PENDING + "quiet-ask.json"))
    source_delete = t.ops.index(("delete", "team/r/task/quiet-ask.md"))
    assert marker_write < source_delete


def test_a_task_whose_close_cannot_be_queued_stays_hot():
    class NoMarker(RecordingTransport):
        def write(self, path, content):
            if path.startswith(PENDING):
                return False
            return super().write(path, content)

    t = NoMarker()
    _quiet_proposed(t, "quiet-ask")
    res = _run(t)
    assert "team/r/task/quiet-ask.md" in t.store
    assert "team/r/task/archive/2020-01/quiet-ask.md" not in t.store
    assert _v4_closes(t) == []
    assert any("quiet-ask" in w and "kept hot" in w for w in res["warnings"]), res["warnings"]


def test_a_failed_restore_keeps_the_queued_close():
    class NoHotWrite(RecordingTransport):
        def write(self, path, content):
            if path == "team/r/task/quiet-ask.md":
                return False
            return super().write(path, content)

    t = NoHotWrite(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    assert cli.main(["task", "restore", "r", "quiet-ask"], transport=t) == 1
    assert PENDING + "quiet-ask.json" in t.store


def test_a_restore_that_lands_during_a_retried_close_is_reopened_after_it():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    t.records_written.clear()
    t.record_ok = True
    doc = t.store["team/r/task/archive/2020-01/quiet-ask.md"]
    real_write = t.record_write

    def restore_mid_close(data_type, api_version, note, source, recorded_at=None, tags=None):
        ok = real_write(data_type, api_version, note, source, recorded_at=recorded_at, tags=tags)
        if json.loads(note).get("kind") == "close":
            t.put("team/r/task/quiet-ask.md", doc)  # task restore ran between our check and our close
        return ok

    t.record_write = restore_mid_close
    _run(t)
    kinds = [(w["note"]["kind"], w["note"]["to"]) for w in t.records_written
             if w["data_type"] == V4_TYPE and w["note"]["slug"] == "quiet-ask"]
    assert kinds == [("close", "all"), ("open", "some-agent")]


def test_retries_are_capped_per_pass_and_the_rest_reported():
    t = RecordingTransport(record_ok=False)
    for i in range(55):
        t.put(f"{PENDING}s{i:02d}.json", json.dumps({"slug": f"s{i:02d}", "ptr": f"task/archive/2020-01/s{i:02d}.md"}))
    res = _run(t)
    attempts = [w for w in t.records_written if w["data_type"] == V4_TYPE]
    assert len(attempts) == 50
    assert any("cap reached" in w and "5 of 55" in w for w in res["warnings"]), res["warnings"]


# --- review round 2 (codex-reviewer on PR 832) ----------------------------------

ACK = "team/r/_coord/acks/quiet-ask/some-agent.md"
RESPONSE = "team/r/_coord/responses/quiet-ask/some-agent.md"


def test_a_task_whose_close_cannot_be_queued_keeps_its_coordination_shards_hot():
    class NoMarker(RecordingTransport):
        def write(self, path, content):
            if path.startswith(PENDING):
                return False
            return super().write(path, content)

    t = NoMarker()
    _quiet_proposed(t, "quiet-ask")
    t.put(ACK, "acknowledged")
    t.put(RESPONSE, "accepted")
    _run(t)
    assert "team/r/task/quiet-ask.md" in t.store
    assert ACK in t.store and RESPONSE in t.store, sorted(t.store)
    assert not any("/_coord/archive/" in p for p in t.store), sorted(t.store)


def test_the_close_is_queued_before_any_shard_moves():
    t = OrderTransport()
    _quiet_proposed(t, "quiet-ask")
    t.put(ACK, "acknowledged")
    _run(t)
    marker_write = t.ops.index(("write", PENDING + "quiet-ask.json"))
    shard_delete = t.ops.index(("delete", ACK))
    assert marker_write < shard_delete


def _restore_during_close(t, *, open_ok):
    doc = t.store["team/r/task/archive/2020-01/quiet-ask.md"]
    real_write = t.record_write

    def write(data_type, api_version, note, source, recorded_at=None, tags=None):
        ok = real_write(data_type, api_version, note, source, recorded_at=recorded_at, tags=tags)
        kind = json.loads(note).get("kind")
        if kind == "close":
            t.put("team/r/task/quiet-ask.md", doc)
            t.store.pop(PENDING + "quiet-ask.json", None)  # task restore clears the marker after its move
        return ok if kind == "close" or open_ok() else False

    t.record_write = write


def test_a_failed_reopen_after_a_racing_restore_stays_queued_until_the_open_lands():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    t.records_written.clear()
    t.record_ok = True
    opens_work = [False]
    _restore_during_close(t, open_ok=lambda: opens_work[0])
    res = _run(t)
    assert PENDING + "quiet-ask.json" in t.store, "a restore deleted the marker; the guard must re-write it"
    assert any("re-open FAILED" in w and "quiet-ask" in w for w in res["warnings"]), res["warnings"]

    t.records_written.clear()
    opens_work[0] = True
    res = _run(t)
    kinds = [w["note"]["kind"] for w in t.records_written
             if w["data_type"] == V4_TYPE and w["note"]["slug"] == "quiet-ask"]
    assert kinds == ["open"], "a hot task is re-opened, never closed again"
    assert PENDING + "quiet-ask.json" not in t.store


def test_a_hot_task_whose_reopen_keeps_failing_keeps_its_marker():
    t = RecordingTransport(record_ok=False)
    _quiet_proposed(t, "quiet-ask")
    _run(t)
    t.put("team/r/task/quiet-ask.md", t.store["team/r/task/archive/2020-01/quiet-ask.md"])
    t.records_written.clear()
    res = _run(t)  # record_ok is still False: the open fails
    assert [w["note"]["kind"] for w in t.records_written if w["data_type"] == V4_TYPE] == ["open"]
    assert PENDING + "quiet-ask.json" in t.store
    assert any("re-open FAILED" in w and "quiet-ask" in w for w in res["warnings"]), res["warnings"]


def test_an_unknown_hot_path_right_after_the_close_stays_queued():
    class UnknownAfterClose(RecordingTransport):
        closed = False

        def record_write(self, data_type, api_version, note, source, recorded_at=None, tags=None):
            if json.loads(note).get("kind") == "close":
                self.closed = True
            return super().record_write(data_type, api_version, note, source, recorded_at=recorded_at, tags=tags)

        def read_classified(self, path, *, deadline=None):
            if path == "team/r/task/quiet-ask.md" and self.closed and not self.healed:
                return None, "error"
            return super().read_classified(path, deadline=deadline)

    t = UnknownAfterClose()
    t.healed = False
    _quiet_proposed(t, "quiet-ask")
    res = _run(t)
    assert PENDING + "quiet-ask.json" in t.store
    assert any("UNKNOWN" in w and "quiet-ask" in w for w in res["warnings"]), res["warnings"]

    t.healed = True
    t.records_written.clear()
    _run(t)
    assert PENDING + "quiet-ask.json" not in t.store
    assert [c["slug"] for c in _v4_closes(t)] == ["quiet-ask"]
