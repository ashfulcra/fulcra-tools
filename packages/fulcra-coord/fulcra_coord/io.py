"""Shared remote-task load/cache layer for fulcra-coord.

The cache-seeded, remote-index-driven, parallel-body-fetch loaders that every
read command and the view builders sit on top of: ``_load_all_tasks`` (full
bodies), ``_load_task_summaries`` (the compact fast path), and
``_load_summaries_for_rebuild`` (the write-path rebuild source, with its
summaries-aggregate fast path plus the S2 self-heal), backed by
``_cache_remote_task`` / ``_load_task`` and the ``_updated_at_key`` ordering
helper.

Extracted from cli.py behind stable re-exports — cli imports these names back so
every internal caller and test patch target keeps resolving. This module depends
only on lower layers (cache, remote, schema, views) and never imports cli, so
there is no import cycle.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
from typing import Any, Optional

from . import cache, read_source, remote, schema, views


def _cache_remote_task(task_id: str, backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Reconstruct a remote task body, cache it, and cache the FILE's stat meta.

    The single funnel every task-BODY read passes through (both ``_load_task``
    and the bulk ``_load_all_tasks``). It resolves the body from one of two
    sources, governed by the per-host ``read_source()`` knob (Phase-2b cutover):

    * ``'file'`` (DEFAULT) — download the mutable ``tasks/<id>.json``. This is
      the pre-cutover behaviour, byte-identical and default-on so an operator
      sees zero change unless they opt in.
    * ``'events'`` — best-effort fold the task's event log; use the fold ONLY
      when ``fold_is_complete`` (a full-task snapshot was applied). On a
      delta-only / empty / errored fold the body stays ``None`` and we fall
      through to the file. This is the read cutover: it changes what a read
      returns, so it is incompleteness-and-error-safe by construction.

    CRITICAL — the stat meta is ALWAYS the FILE's stat, even when the body came
    from the fold. The write path (``writepipe._write_task_and_views``) reads
    ``cache.read_meta`` on ``tasks/<id>.json`` for optimistic-concurrency; if
    events-mode skipped the file stat, the next write would lose its baseline.
    Phase-2b changes READS only — the write-path concurrency stat stays
    file-sourced.
    """
    task_path = remote.task_remote_path(task_id)
    task: Optional[dict[str, Any]] = None

    # READ cutover: events source folds the event log when it's a complete
    # snapshot, else leaves task=None to fall through to the file below. Lazy
    # import keeps these substrate modules off io's import-time path and sidesteps
    # any cycle risk (io sits ABOVE events/eventlog; the layering fitness test
    # only forbids the reverse). Best-effort: ANY fold/read error → file fallback.
    if read_source() == "events":
        try:
            from . import eventlog, events
            folded = events.fold_task(eventlog.read_events(task_id, backend=backend))
            if events.fold_is_complete(folded):
                task = folded
        except Exception:
            task = None  # fall through to the file — never let a fold error read-fail

    # File source (default, OR events-mode fallback when the fold was
    # incomplete/absent/errored). This is the authoritative mutable snapshot.
    if task is None:
        task = remote.download_json(task_path, backend=backend)
        if not task:
            return None

    cache.write_cached_task(task)
    # Always stat the FILE (not the fold) so the write-path concurrency baseline
    # stays correct regardless of where the body was sourced from.
    task_stat = remote.stat(task_path, backend=backend)
    if task_stat:
        cache.write_meta(task_path, task_stat)
    return task


def _load_all_tasks(backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Load tasks from cache, refreshing remote-indexed tasks when available."""
    cached = cache.list_cached_tasks()
    idx = remote.download_json(remote.view_remote_path("index"), backend=backend)
    if idx is None:
        return cached

    remote_ids = {s["id"] for s in idx.get("active", []) + idx.get("recent_done", [])}
    search_idx = remote.download_json(remote.view_remote_path("search-index"), backend=backend)
    if search_idx:
        # Cache the fresh remote search-index so cmd_search doesn't use a stale local copy.
        # Without this, status+search would show stale results for remotely-updated tasks.
        cache.write_cached_view("search-index", search_idx)
        remote_ids.update(r["id"] for r in search_idx.get("records", []) if r.get("id"))

    # The index seeds only active + recent_done ids; PROPOSED (and waiting) tasks
    # ride only on the search-index. If the search-index fetch fails or is absent,
    # a remote-only proposed directive would be invisible here — so it would be
    # silently dropped from every rebuilt view (recompute could lose a pending
    # directive). The `next` view contains exactly the proposed+waiting set, so
    # fold its ids in as a second, independent source for those statuses.
    next_view = remote.download_json(remote.view_remote_path("next"), backend=backend)
    if next_view:
        remote_ids.update(t["id"] for t in next_view.get("tasks", []) if t.get("id"))
    # Skip any id-less cached body (A2): an older/imperfect bus can leave a
    # cached file whose JSON lacks "id". Bracket access here raised KeyError that
    # propagated uncaught through _load_summaries_for_rebuild ->
    # _write_task_and_views, crashing every write command. A body with no id has
    # no stable key anyway, so dropping it is the correct, lossless choice.
    task_map: dict[str, dict[str, Any]] = {
        tid: t for t in cached if (tid := t.get("id"))
    }

    # Fetch each remote task body CONCURRENTLY (PERF). Each fetch is one
    # independent `fulcra file download` subprocess (~1.3s) writing to a
    # distinct per-id cache file — there is no shared mutable state, so a thread
    # pool is safe and collapses N serial round-trips into a single batch's
    # wall-time. This is the root-cause fix for the reconcile heartbeat blowing
    # past its 90s timeout (76 sequential fetches measured at ~96s). Semantics
    # are preserved exactly: a None result (404/error) is skipped, order is
    # irrelevant (results dedup into task_map by id), and the local-cache base
    # already seeded above survives any id the index doesn't name.
    if remote_ids:
        max_workers = min(16, max(4, len(remote_ids)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_cache_remote_task, tid, backend=backend): tid
                for tid in remote_ids
            }
            for fut in concurrent.futures.as_completed(futures):
                # Mirror the old loop's best-effort guard: a single failed fetch
                # must not abort the whole load. _cache_remote_task already
                # returns None on a missing/empty body; catching here covers an
                # unexpected raise (network blowup) so it can't escape the pool.
                try:
                    t = fut.result()
                except Exception:
                    t = None
                # Same id-less guard as the cached seed above (A2): a remote
                # body that came back without an id has no stable key and must
                # not crash the merge.
                if t:
                    rid = t.get("id")
                    if rid:
                        task_map[rid] = t

    return list(task_map.values())


def _load_task_summaries(backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Load the compact task-summary list WITHOUT fetching task bodies.

    The performance fast-path for reads (status/agents/needs-me/resume/search/
    inbox): one download of ``views/summaries.json`` replaces ``_load_all_tasks``'
    N+3 round-trips (index + search-index + next, then one body fetch per task).
    Every read command and the view builders operate on summary dicts, which now
    carry every field they read (schema.task_summary was enriched with
    ``last_touched_by`` and a flattened ``done_at`` for exactly this).

    BACKWARD COMPAT: a bus that predates this aggregate has no
    ``views/summaries.json``. When the download is absent/None we FALL BACK to the
    full ``_load_all_tasks`` path and summarize locally — correctness over speed —
    so an older bus keeps working (just without the speedup) until its next write
    materializes the aggregate."""
    summaries_view = remote.download_json(
        remote.view_remote_path("summaries"), backend=backend)
    if summaries_view and summaries_view.get("summaries") is not None:
        return summaries_view["summaries"]
    # Older bus: no aggregate yet — fall back to the authoritative full load.
    return [schema.task_summary(t) for t in _load_all_tasks(backend=backend)]


def _load_summaries_for_rebuild(
    task: dict[str, Any], *, backend: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """The write-path view-rebuild source: the summaries aggregate with the
    just-written task's summary upserted in.

    Downloads the authoritative ``views/summaries.json`` (NOT the local cache —
    another agent may have written since we loaded), replaces the entry for this
    task by id (or appends it if new), and returns the merged summary list. Since
    build_all_views gives identical output from summaries as from full bodies,
    this list is a complete view source without re-fetching any task body.

    BACKWARD COMPAT: when the aggregate is absent (older bus that never wrote it),
    fall back to the full ``_load_all_tasks`` path and summarize locally —
    correctness over speed — so views still rebuild from the complete task set.
    The fallback returns summaries too, so build_all_views sees a uniform shape.

    ROBUSTNESS (S2): the single aggregate is one file under last-writer-wins, so
    a concurrent peer's write could leave the copy we downloaded missing a task.
    Two layers recover it: (1) we union in our local cached task summaries,
    freshest-per-id by ``updated_at`` (never resurrecting a stale local copy over
    a newer remote one); and (2) the SELF-HEAL below — we enumerate the durable
    per-task files and re-include any whose file exists but whom the aggregate
    dropped. Because the task files are the un-clobberable source of truth, a
    dropped task is recovered on the very NEXT write by any agent, not only on a
    full ``reconcile``.

    ACK PRESERVATION (B1): ``acked_by`` on the just-written task is recomputed
    from its event log, which is truncated to the last MAX_EVENTS_INLINE events.
    A heavily-acked broadcast can scroll an ``inbox_ack`` out of that window, so
    the body-derived acks may be INCOMPLETE. The durable aggregate entry holds
    the previously-recorded acks, so we UNION them into the written task's summary
    rather than letting a recompute silently drop an ack (which would re-surface a
    directive an agent already cleared)."""
    summaries_view = remote.download_json(
        remote.view_remote_path("summaries"), backend=backend)
    if not (summaries_view and summaries_view.get("summaries") is not None):
        # Older bus: no aggregate. Rebuild from the authoritative full task set so
        # a fresh machine doesn't truncate views; the just-written task is already
        # cached and thus present in _load_all_tasks' result.
        return [schema.task_summary(t) for t in _load_all_tasks(backend=backend)]

    # Start from the downloaded aggregate, keyed by id.
    by_id: dict[str, dict[str, Any]] = {
        s["id"]: s for s in summaries_view["summaries"] if s.get("id")
    }

    # S2 layer 1: union local cached task summaries, freshest-by-updated_at wins,
    # so a task this agent knows about that a raced aggregate dropped is recovered
    # — without ever overwriting a newer remote record with a stale local one.
    for t in cache.list_cached_tasks():
        # task_summary is now defensive (renders a partial body with "" defaults
        # rather than KeyError-ing), so a corrupt cached body is SURFACED, not
        # dropped. This try/except is kept as belt-and-suspenders: any OTHER
        # unexpected failure summarizing one entry must not crash the whole
        # rebuild AFTER the task body uploaded (which would leave stale views with
        # no needs_reconcile marker) — skip the offending entry, keep the rest.
        try:
            s = schema.task_summary(t)
        except Exception:
            continue
        if not s.get("id"):
            continue  # a body with no id can't key the aggregate; skip safely
        prev = by_id.get(s["id"])
        if prev is None or _updated_at_key(s) > _updated_at_key(prev):
            by_id[s["id"]] = s

    # S2 layer 2 — SELF-HEAL: the per-task FILES are the durable, un-clobberable
    # truth (each owned by one agent). Enumerate them and recover any id whose
    # file exists but is absent from the aggregate AND our cache — i.e. a task a
    # concurrent write dropped from summaries.json. This heals the drop on THIS
    # write (seconds) instead of leaving it invisible until a 90s reconcile. One
    # `list` call; a body is fetched ONLY for an id nothing else already covers,
    # so steady-state cost is ~0 fetches. Best-effort and ADD-only: a failed or
    # empty listing contributes nothing and can never make the rebuild worse than
    # the aggregate alone.
    try:
        prefix = f"{remote.remote_root()}/tasks/"
        for path in remote.list_files(prefix, backend=backend):
            if not path.endswith(".json"):
                continue
            tid = path.rsplit("/", 1)[-1][: -len(".json")]
            if not tid or tid in by_id:
                continue
            try:
                body = _cache_remote_task(tid, backend=backend)
                if body and body.get("id"):
                    by_id[body["id"]] = schema.task_summary(body)
            except Exception:
                continue
    except Exception:
        pass  # listing is best-effort; never break a task write over it

    # Upsert the just-written task. B1: preserve any acks the truncated event log
    # can no longer prove by unioning the prior known acked_by set.
    this_summary = schema.task_summary(task)
    prior = by_id.get(task["id"])
    if prior:
        this_summary["acked_by"] = sorted(
            set(this_summary.get("acked_by", []) or [])
            | set(prior.get("acked_by", []) or [])
        )
    by_id[task["id"]] = this_summary

    return list(by_id.values())


def _load_task(task_id: str, *, backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Load a specific task from cache or remote."""
    t = cache.read_cached_task(task_id)
    if t is not None:
        return t
    return _cache_remote_task(task_id, backend=backend)


def _updated_at_key(task: dict[str, Any]) -> datetime:
    """Parsed, tz-aware ``updated_at`` for newer/older comparison, epoch on miss.

    BUG 1 (mixed-precision data loss): a raw STRING compare of two ``updated_at``
    values mis-orders timestamps in the same second when one was emitted with
    microsecond=0 (``...:45Z``) and the other with microseconds>0
    (``...:45.000001Z``) — lexically ``.`` < ``Z`` so the truly-newer fractional
    timestamp wrongly sorts BEFORE the whole-second one, and the merge would
    silently drop the newer side's field edits. The emission fix gives all NEW
    timestamps fixed-width microseconds, but mixed-precision data already on the
    bus must still compare correctly, so the merge compares PARSED datetimes via
    the shared ``views._parse_dt`` (which coerces naive->UTC). A missing or
    unparseable timestamp sorts oldest (epoch) so a clock-less side never wins."""
    dt = views._parse_dt(task.get("updated_at", ""))
    return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)
