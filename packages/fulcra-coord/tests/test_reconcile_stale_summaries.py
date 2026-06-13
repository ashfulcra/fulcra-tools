"""Reconcile must repair views from task files, not stale view-derived ids."""

from types import SimpleNamespace
from unittest import mock

from fulcra_coord import cache, cli, remote, schema, views
from fulcra_coord.io import _load_task_summaries


def test_reconcile_repairs_stale_summary_entry_from_task_file(coord_backend):
    task = schema.make_task(
        title="claim or redirect",
        workstream="coordination",
        agent="agent-a",
    )
    task["id"] = "TASK-20260613-stale-summary-live-repro"
    stale = dict(task)
    stale["status"] = "proposed"
    stale["updated_at"] = "2026-06-13T00:35:23.143159Z"

    fresh = dict(task)
    fresh["status"] = "done"
    fresh["updated_at"] = "2026-06-13T00:37:44.372470Z"
    fresh["done"] = {"done_at": fresh["updated_at"], "by": "arc"}

    # Local cache has the stale body, matching the live failure. The task file on
    # the bus is fresh, but the stale views do not name the id in index/search/next.
    cache.write_cached_task(stale)
    assert remote.upload_json(
        fresh, remote.task_remote_path(fresh["id"]), backend=coord_backend)

    stale_summaries = views.build_all_views([stale])["summaries"]
    assert remote.upload_json(
        stale_summaries, remote.view_remote_path("summaries"),
        backend=coord_backend)
    assert remote.upload_json(
        {"schema": "fulcra.coordination.index.v1", "view": "index",
         "active": [], "recent_done": []},
        remote.view_remote_path("index"), backend=coord_backend)
    assert remote.upload_json(
        {"schema": "fulcra.coordination.search.v1", "view": "search-index",
         "records": []},
        remote.view_remote_path("search-index"), backend=coord_backend)
    assert remote.upload_json(
        {"schema": "fulcra.coordination.next.v1", "view": "next",
         "tasks": []},
        remote.view_remote_path("next"), backend=coord_backend)

    assert cli.cmd_reconcile(SimpleNamespace(), backend=coord_backend) == 0

    repaired = remote.download_json(
        remote.view_remote_path("summaries"), backend=coord_backend)
    entry = next(s for s in repaired["summaries"] if s["id"] == fresh["id"])
    assert entry["status"] == "done"
    assert entry["updated_at"] == fresh["updated_at"]


def test_reconcile_retries_success_shaped_summaries_upload_miss(coord_backend):
    task = schema.make_task(
        title="fresh task omitted by stale summaries",
        workstream="coordination",
        agent="agent-a",
    )
    task["id"] = "TASK-RECONCILE-SUMMARIES-VERIFY"
    assert remote.upload_json(task, remote.task_remote_path(task["id"]),
                              backend=coord_backend)

    stale_summaries = views.build_all_views([])["summaries"]
    assert remote.upload_json(stale_summaries, remote.view_remote_path("summaries"),
                              backend=coord_backend)

    real_upload = remote.upload_json
    summaries_path = remote.view_remote_path("summaries")
    skipped_once = {"done": False}

    def upload_json(data, path, *, backend=None, timeout=None):
        if path == summaries_path and not skipped_once["done"]:
            skipped_once["done"] = True
            return True
        return real_upload(data, path, backend=backend, timeout=timeout)

    with mock.patch("fulcra_coord.cli.remote.upload_json",
                    side_effect=upload_json):
        assert cli.cmd_reconcile(SimpleNamespace(), backend=coord_backend) == 0

    repaired = remote.download_json(summaries_path, backend=coord_backend)
    ids = {s.get("id") for s in repaired.get("summaries", [])}
    assert task["id"] in ids
    assert skipped_once["done"], "test must exercise the success-shaped miss"


def test_read_overlays_newer_cached_summaries_after_remote_rewind(coord_backend):
    task = schema.make_task(
        title="Review https://github.com/ashfulcra/fulcra-tools/pull/213",
        workstream="fulcra-coord",
        agent="maintainer",
    )
    task["id"] = "TASK-20260613-review-rewind"
    stale = dict(task)
    stale["assignee"] = "Ashs-MBP-Work:Codex-Review-Workbook"
    stale["updated_at"] = "2026-06-13T09:10:00.000000Z"
    fresh = dict(task)
    fresh["assignee"] = "claude-code:ArcBot:Arc-Code-Review"
    fresh["updated_at"] = "2026-06-13T09:15:00.000000Z"

    remote.upload_json(
        views.build_summaries([stale], updated_at="2026-06-13T09:10:00.000000Z"),
        remote.view_remote_path("summaries"),
        backend=coord_backend,
    )
    cache.write_cached_view(
        "summaries",
        views.build_summaries([fresh], updated_at="2026-06-13T09:15:00.000000Z"),
    )

    got = _load_task_summaries(backend=coord_backend)
    entry = next(s for s in got if s["id"] == task["id"])
    assert entry["assignee"] == "claude-code:ArcBot:Arc-Code-Review"
    assert entry["updated_at"] == fresh["updated_at"]


def test_read_overlays_cached_open_task_after_fresh_truncated_remote(coord_backend):
    task = schema.make_task(
        title="Review https://github.com/ashfulcra/fulcra-tools/pull/220",
        workstream="fulcra-coord",
        agent="maintainer",
        owner_agent="maintainer",
        assignee="claude-code:ArcBot:Arc-Code-Review",
    )
    task["id"] = "TASK-20260613-review-fresh-truncated"
    task["updated_at"] = "2026-06-13T10:29:00.000000Z"
    task["tags"] = sorted(set(task["tags"] + ["kind:review"]))

    remote.upload_json(
        views.build_summaries([], updated_at="2026-06-13T10:38:00.000000Z"),
        remote.view_remote_path("summaries"),
        backend=coord_backend,
    )
    cache.write_cached_view(
        "summaries",
        views.build_summaries([task], updated_at="2026-06-13T10:37:00.000000Z"),
    )

    got = _load_task_summaries(backend=coord_backend, skip_stale_fallback=True)
    entry = next(s for s in got if s["id"] == task["id"])
    assert entry["assignee"] == "claude-code:ArcBot:Arc-Code-Review"
    assert entry["status"] == "proposed"


def test_summaries_upload_guard_rejects_open_task_drop_and_rewind():
    open_task = schema.make_task(
        title="Keep me visible",
        workstream="fulcra-coord",
        agent="agent-a",
    )
    open_task["id"] = "TASK-OPEN"
    open_task["updated_at"] = "2026-06-13T09:15:00.000000Z"
    done_task = dict(open_task)
    done_task["id"] = "TASK-DONE"
    done_task["status"] = "done"
    done_task["done"] = {"done_at": done_task["updated_at"], "by": "agent-a"}
    done_task["updated_at"] = "2026-06-13T09:00:00.000000Z"

    current = views.build_summaries(
        [open_task, done_task], updated_at="2026-06-13T09:20:00.000000Z")
    candidate = views.build_summaries(
        [done_task], updated_at="2026-06-13T09:20:00.000000Z")

    assert "would drop open task TASK-OPEN" in (
        cli._summaries_upload_would_clobber(candidate, current) or ""
    )

    older_open = dict(open_task)
    older_open["updated_at"] = "2026-06-13T09:10:00.000000Z"
    candidate = views.build_summaries(
        [older_open, done_task], updated_at="2026-06-13T09:20:00.000000Z")

    assert "would rewind task TASK-OPEN" in (
        cli._summaries_upload_would_clobber(candidate, current) or ""
    )


def test_summaries_upload_merge_preserves_remote_only_open_task():
    local_task = schema.make_task(
        title="Local open",
        workstream="fulcra-coord",
        agent="agent-a",
    )
    local_task["id"] = "TASK-LOCAL"
    local_task["updated_at"] = "2026-06-13T10:29:00.000000Z"
    remote_task = schema.make_task(
        title="Remote only open",
        workstream="fulcra-coord",
        agent="agent-b",
    )
    remote_task["id"] = "TASK-REMOTE"
    remote_task["updated_at"] = "2026-06-13T10:38:00.000000Z"

    candidate = views.build_summaries(
        [local_task], updated_at="2026-06-13T10:37:00.000000Z")
    current = views.build_summaries(
        [remote_task], updated_at="2026-06-13T10:38:00.000000Z")

    merged = cli._merge_summaries_for_upload(candidate, current)
    ids = {s["id"] for s in merged["summaries"]}
    assert ids == {"TASK-LOCAL", "TASK-REMOTE"}
    assert cli._summaries_upload_would_clobber(merged, current) is None


def test_summaries_upload_guard_rejects_newer_candidate_that_drops_open_task():
    open_task = schema.make_task(
        title="Still open in the current aggregate",
        workstream="fulcra-coord",
        agent="agent-a",
    )
    open_task["id"] = "TASK-OPEN"
    open_task["updated_at"] = "2026-06-13T09:15:00.000000Z"

    current = views.build_summaries(
        [open_task], updated_at="2026-06-13T09:05:00.000000Z")
    candidate = views.build_summaries(
        [], updated_at="2026-06-13T09:20:00.000000Z")

    assert "would drop open task TASK-OPEN" in (
        cli._summaries_upload_would_clobber(candidate, current) or ""
    )
