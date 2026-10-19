"""`task start` opens its assignee's bus-v4 obligation (coord-boss 33073a42).

Measured 2026-09-08 on collect-maintainer: six tasks created with `task start`
(owner and assignee both collect-maintainer) were open on the file plane and
absent from the fold. Only `tell` emitted a v3 directive, which the dual-emit
mirror turns into a v4 open; `task start` wrote the doc and nothing else.
"""
from test_retention_retires_v4_obligation import RecordingTransport, V4_TYPE, _v4

from coord_engine import cli


def _task_docs(t):
    return [p for p in t.store if p.startswith("team/r/task/") and p.endswith(".md")]


def test_an_assigned_task_opens_in_the_assignee_fold(monkeypatch):
    monkeypatch.setattr(cli, "_host", lambda: "creator")
    t = RecordingTransport()
    assert cli.main(["task", "start", "r", "Ship the thing", "--assignee", "worker", "-p", "P1"],
                    transport=t) == 0
    [doc] = _task_docs(t)
    slug = doc.rsplit("/", 1)[1][:-3]
    opens = _v4(t, "open")
    assert [(o["slug"], o["to"], o["from"], o["pri"], o["ptr"]) for o in opens] == [
        (slug, "worker", "creator", "P1", f"task/{slug}.md")]
    assert [w["source"] for w in t.records_written if w["data_type"] == V4_TYPE] == ["creator"]


def test_a_self_assigned_task_opens_for_its_creator(monkeypatch):
    monkeypatch.setattr(cli, "_host", lambda: "collect-maintainer")
    t = RecordingTransport()
    assert cli.main(["task", "start", "r", "My own work", "--assignee", "collect-maintainer",
                     "--status", "active"], transport=t) == 0
    assert [o["to"] for o in _v4(t, "open")] == ["collect-maintainer"]


def test_a_broadcast_task_opens_to_all():
    t = RecordingTransport()
    assert cli.main(["task", "start", "r", "Everyone look", "--assignee", "*"], transport=t) == 0
    assert [o["to"] for o in _v4(t, "open")] == ["all"]


def test_an_unassigned_task_opens_for_nobody():
    t = RecordingTransport()
    assert cli.main(["task", "start", "r", "Loose idea"], transport=t) == 0
    assert len(_task_docs(t)) == 1
    assert t.records_written == []


def test_no_bus_v4_plane_is_a_clean_start():
    t = RecordingTransport(v4=False)
    assert cli.main(["task", "start", "r", "Ship it", "--assignee", "worker"], transport=t) == 0
    assert t.records_written == []


def test_a_failed_open_is_loud_and_nonzero_but_keeps_the_doc(capsys):
    t = RecordingTransport(record_ok=False)
    assert cli.main(["task", "start", "r", "Ship it", "--assignee", "worker"], transport=t) == 3
    assert len(_task_docs(t)) == 1
    assert "bus-v4 open was NOT" in capsys.readouterr().err


def test_a_doc_that_did_not_land_opens_nothing():
    class Refusing(RecordingTransport):
        def write(self, path, content):
            return False

    t = Refusing()
    cli.main(["task", "start", "r", "Ship it", "--assignee", "worker"], transport=t)
    assert t.records_written == []
