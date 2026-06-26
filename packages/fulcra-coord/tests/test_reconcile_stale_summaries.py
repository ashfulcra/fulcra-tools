"""Reconcile must repair views from task files, not stale view-derived ids."""

from datetime import datetime, timedelta, timezone
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

    # `now` close to the fixed dates so TASK-OPEN is RECENT (within grace) — the
    # multi-host race protection must still reject dropping a recent open task.
    now = datetime(2026, 6, 13, 9, 30, tzinfo=timezone.utc)
    assert "would drop open task TASK-OPEN" in (
        cli._summaries_upload_would_clobber(candidate, current, now) or ""
    )

    older_open = dict(open_task)
    older_open["updated_at"] = "2026-06-13T09:10:00.000000Z"
    candidate = views.build_summaries(
        [older_open, done_task], updated_at="2026-06-13T09:20:00.000000Z")

    assert "would rewind task TASK-OPEN" in (
        cli._summaries_upload_would_clobber(candidate, current, now) or ""
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

    # `now` close to the fixed dates so TASK-REMOTE is RECENT (within grace) — a
    # recent remote-only open task is a multi-host race and must be preserved.
    now = datetime(2026, 6, 13, 10, 40, tzinfo=timezone.utc)
    merged = cli._merge_summaries_for_upload(candidate, current, now)
    ids = {s["id"] for s in merged["summaries"]}
    assert ids == {"TASK-LOCAL", "TASK-REMOTE"}
    assert cli._summaries_upload_would_clobber(merged, current, now) is None


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

    # `now` close to the fixed dates so TASK-OPEN is RECENT — a newer candidate
    # that drops a recent open task is still a clobber and must be rejected.
    now = datetime(2026, 6, 13, 9, 30, tzinfo=timezone.utc)
    assert "would drop open task TASK-OPEN" in (
        cli._summaries_upload_would_clobber(candidate, current, now) or ""
    )


def _open_summary(task_id, updated_at):
    task = schema.make_task(
        title="open task",
        workstream="fulcra-coord",
        agent="agent-a",
    )
    task["id"] = task_id
    task["updated_at"] = updated_at
    return task


def test_summaries_merge_drops_stale_orphan_but_keeps_recent_race():
    """A current-only open row OLDER than grace is an orphan (body gone) → dropped;
    a current-only open row WITHIN grace is a multi-host race → kept."""
    local = _open_summary("TASK-LOCAL", "2026-06-13T12:00:00.000000Z")
    stale_orphan = _open_summary("TASK-STALE", "2026-06-13T00:00:00.000000Z")
    recent_race = _open_summary("TASK-RECENT", "2026-06-13T11:55:00.000000Z")

    candidate = views.build_summaries(
        [local], updated_at="2026-06-13T12:00:00.000000Z")
    current = views.build_summaries(
        [stale_orphan, recent_race], updated_at="2026-06-13T11:55:00.000000Z")

    # grace default 2.0h; relative to now, TASK-STALE is 12h old (orphan),
    # TASK-RECENT is 5min old (race).
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    merged = cli._merge_summaries_for_upload(candidate, current, now)
    ids = {s["id"] for s in merged["summaries"]}
    assert ids == {"TASK-LOCAL", "TASK-RECENT"}
    assert "TASK-STALE" not in ids


def test_summaries_guard_allows_stale_orphan_drop_rejects_recent_drop():
    """The clobber guard allows dropping a STALE current-only open row (orphan
    prune) but still rejects dropping a RECENT one (race protection)."""
    stale_orphan = _open_summary("TASK-STALE", "2026-06-13T00:00:00.000000Z")
    recent_race = _open_summary("TASK-RECENT", "2026-06-13T11:55:00.000000Z")
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    current_stale = views.build_summaries(
        [stale_orphan], updated_at="2026-06-13T11:55:00.000000Z")
    candidate = views.build_summaries(
        [], updated_at="2026-06-13T12:00:00.000000Z")
    assert cli._summaries_upload_would_clobber(
        candidate, current_stale, now) is None

    current_recent = views.build_summaries(
        [recent_race], updated_at="2026-06-13T11:55:00.000000Z")
    assert "would drop open task TASK-RECENT" in (
        cli._summaries_upload_would_clobber(candidate, current_recent, now) or ""
    )


def test_summary_orphan_grace_hours_knob_default_and_override(monkeypatch):
    monkeypatch.delenv("FULCRA_COORD_SUMMARY_ORPHAN_GRACE_HOURS", raising=False)
    assert cli._summary_orphan_grace_hours() == 2.0
    monkeypatch.setenv("FULCRA_COORD_SUMMARY_ORPHAN_GRACE_HOURS", "6.5")
    assert cli._summary_orphan_grace_hours() == 6.5


def test_summaries_undatable_current_only_row_is_kept_by_both():
    """Never drop what we can't date — an undatable current-only open row is
    KEPT by the merge (unioned) and still triggers the clobber guard."""
    local = _open_summary("TASK-LOCAL", "2026-06-13T12:00:00.000000Z")
    undatable = _open_summary("TASK-UNDATED", "not-a-timestamp")

    candidate = views.build_summaries(
        [local], updated_at="2026-06-13T12:00:00.000000Z")
    current = views.build_summaries(
        [undatable], updated_at="2026-06-13T12:00:00.000000Z")
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    merged = cli._merge_summaries_for_upload(candidate, current, now)
    assert "TASK-UNDATED" in {s["id"] for s in merged["summaries"]}

    drop_candidate = views.build_summaries(
        [], updated_at="2026-06-13T12:00:00.000000Z")
    assert "would drop open task TASK-UNDATED" in (
        cli._summaries_upload_would_clobber(drop_candidate, current, now) or ""
    )


def test_summaries_row_aged_exactly_grace_is_kept_by_both():
    """Boundary: `age == grace` is KEPT (strict `age > grace`). Pins the
    boundary so a future `>`->`>=` flip is caught — an exactly-grace current-only
    open row must still be protected by both functions."""
    grace = cli._summary_orphan_grace_hours()  # default 2.0h
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    # updated_at == now - grace, to the microsecond.
    edge = now - timedelta(hours=grace)
    edge_iso = edge.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    boundary = _open_summary("TASK-EDGE", edge_iso)

    current = views.build_summaries(
        [boundary], updated_at="2026-06-13T12:00:00.000000Z")
    candidate = views.build_summaries(
        [], updated_at="2026-06-13T12:00:00.000000Z")

    merged = cli._merge_summaries_for_upload(candidate, current, now)
    assert "TASK-EDGE" in {s["id"] for s in merged["summaries"]}
    assert "would drop open task TASK-EDGE" in (
        cli._summaries_upload_would_clobber(candidate, current, now) or ""
    )


def test_summaries_merge_output_passes_its_own_guard_same_tick():
    """End-to-end: run the real merge->guard sequence on one input. A stale
    orphan pruned by the merge must NOT then be vetoed by the guard on the
    merged output — pins the merge<->guard agreement production relies on (they
    sample `now` independently; age-only-grows makes it safe, untested before)."""
    real = _open_summary("TASK-REAL", "2026-06-13T11:59:00.000000Z")
    stale_orphan = _open_summary("TASK-ORPHAN", "2026-06-13T00:00:00.000000Z")
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    candidate = views.build_summaries(
        [real], updated_at="2026-06-13T12:00:00.000000Z")
    current = views.build_summaries(
        [stale_orphan, real], updated_at="2026-06-13T11:59:00.000000Z")

    merged = cli._merge_summaries_for_upload(candidate, current, now)
    assert "TASK-ORPHAN" not in {s["id"] for s in merged["summaries"]}
    # The merged (orphan-free) output must pass its own clobber guard.
    assert cli._summaries_upload_would_clobber(merged, current, now) is None
