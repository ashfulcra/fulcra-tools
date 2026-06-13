from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import directives, loop_snapshots, loops, remote, schema


def _review_task(*, task_id="TASK-LOOP-SNAP", status="proposed"):
    task = schema.make_task(
        title="Review https://example.test/pr/1",
        workstream="general",
        agent="author:h:r",
        owner_agent="author:h:r",
        assignee="reviewer:h:r",
        task_id=task_id,
    )
    task["status"] = status
    task["tags"] = sorted(set(schema.build_tags(
        status=status,
        workstream=task["workstream"],
        agent=task["owner_agent"],
        kind="ops",
        priority=task["priority"],
    ) + ["kind:review"]))
    return task


def test_open_loop_snapshot_overlays_done_backing_task(coord_backend):
    stale_task = _review_task(status="proposed")
    stale_record = directives.directive_from_task(stale_task)
    done_task = _review_task(status="done")
    remote.upload_json(
        done_task, remote.task_remote_path(done_task["id"]), backend=coord_backend)

    records = loop_snapshots.overlay_open_records_from_tasks(
        [stale_record], backend=coord_backend, fetch_missing=True)

    assert records[0]["state"] == "closed"
    assert not loops.is_open_loop(records[0])


def test_closed_loop_snapshot_does_not_fetch_backing_task(coord_backend, monkeypatch):
    record = directives.directive_from_task(_review_task(status="done"))

    def fail_download(*args, **kwargs):
        raise AssertionError("closed records should not need task readback")

    monkeypatch.setattr(remote, "download_json", fail_download)

    assert loop_snapshots.overlay_open_records_from_tasks(
        [record], backend=coord_backend) == [record]


def test_missing_summary_can_fetch_open_backing_task(coord_backend):
    stale_task = _review_task(task_id="TASK-MISSING-SUMMARY", status="proposed")
    stale_record = directives.directive_from_task(stale_task)
    done_task = _review_task(task_id="TASK-MISSING-SUMMARY", status="done")
    remote.upload_json(
        done_task, remote.task_remote_path(done_task["id"]), backend=coord_backend)

    records = loop_snapshots.overlay_open_records_from_tasks(
        [stale_record],
        backend=coord_backend,
        tasks=[],
        fetch_missing=True,
    )

    assert records[0]["state"] == "closed"
    assert not loops.is_open_loop(records[0])
