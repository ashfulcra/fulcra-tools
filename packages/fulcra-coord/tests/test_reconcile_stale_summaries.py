"""Reconcile must repair views from task files, not stale view-derived ids."""

from types import SimpleNamespace

from fulcra_coord import cache, cli, remote, schema, views


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

