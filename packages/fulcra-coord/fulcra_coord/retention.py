"""Retention / archival subsystem for fulcra-coord.

This is the cold-storage half of the coordination store: terminal-task
cold-archive (crash-safe move of aged done/abandoned tasks out of the hot
tasks/ tree), the append-only cold-index shards that make ``search --archived``
possible, the dead-marker / dead-presence / dead-health pruners, and the
throttled retention pass that reconcile folds in once per day.

Extracted verbatim from ``cli.py`` behind stable re-exports: ``cli`` still
imports every name below under its historical ``_``-prefixed identifier, so all
internal call sites and the test patch targets keep resolving. This module
depends only on lower layers (cache / remote / views / identity / timeutil and
the ``env_int`` helper) and never imports ``cli`` — so the split introduces no
import cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from . import cache, remote, views, identity
from . import env_int
from .timeutil import iso_z as _iso_z, now_iso as _now_iso


# ---------------------------------------------------------------------------
# Retention / archival: crash-safe move + cold-index shards
# ---------------------------------------------------------------------------

def _archive_month(task: dict[str, Any]) -> str:
    """The <YYYY-MM> the task is archived under: the done/abandoned month, or the
    current month as a fallback. Parsed via views._parse_dt (never lexical)."""
    dt = views._parse_dt(views._done_at(task))
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m")


def _archive_index_shard(task: dict[str, Any], archive_path: str) -> dict[str, Any]:
    """The append-only cold-index shard body for an archived task. Fields are a
    subset of task_summary plus the archive bookkeeping; written once, never
    mutated (one distinct path per id => concurrency-safe, no CAS)."""
    return {
        "schema": "fulcra.coordination.archive_index.v1",
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "status": task.get("status", ""),
        "workstream": task.get("workstream", ""),
        "owner_agent": task.get("owner_agent", ""),
        "done_at": views._done_at(task),
        "archived_at": _now_iso(),
        "archive_path": archive_path,
    }


def _archive_task(task: dict[str, Any], *, backend: Optional[list[str]] = None) -> bool:
    """Crash-safely MOVE a terminal+aged task out of the hot path into the cold
    archive. Returns True on a completed (or already-complete) move, False if the
    move could not be safely completed (caller logs + retries next pass).

    ORDER (no-loss by construction): upload archive body -> VERIFY it landed
    (stat) -> only THEN delete tasks/<id>.json -> write the per-id index shard.
    A crash anywhere leaves the body in BOTH places (a recoverable duplicate),
    never lost. IDEMPOTENT: if the archive body already exists we skip the
    upload, still ensure the original is deleted and the shard exists, so
    archiving an already-archived id (or finishing a crashed move) is a no-op.

    PHANTOM GUARD (B2): require the HOT remote copy (tasks/<id>.json) to exist
    before starting a move — UNLESS the archive body already exists (the
    idempotent / crash-recovery finish). A task present ONLY in this host's stale
    LOCAL cache (deleted remotely by another host, never archived here) would
    otherwise be uploaded as a phantom archive body + shard. The reroute sweep
    already guards this class with `if fresh is None: continue`; archive does the
    same: skip the phantom AND evict it from the local cache so it stops getting
    reloaded by _load_all_tasks (which is cache-seeded).

    BEST-EFFORT: any backend error returns False rather than raising; the only
    irreversible step (delete) runs strictly after a positive read-back."""
    tid = task.get("id")
    if not tid:
        return False
    try:
        archive_path = remote.archive_task_path(tid, _archive_month(task))
        task_path = remote.task_remote_path(tid)
        # (0) PHANTOM GUARD: only proceed if the hot copy exists OR the move is
        # already (partly) done (archive body present). Neither => stale-cache
        # phantom: evict the local copy and skip — never write a phantom archive.
        archive_exists = remote.stat(archive_path, backend=backend) is not None
        if not archive_exists and remote.stat(task_path, backend=backend) is None:
            cache.delete_cached_task(tid)
            return False
        # (1) ensure the body is in the archive (idempotent): upload only if absent.
        if not archive_exists:
            if not remote.upload_json(task, archive_path, backend=backend):
                return False
        # (2) VERIFY it landed before any delete — the no-loss gate.
        if remote.stat(archive_path, backend=backend) is None:
            return False
        # (3) only now remove the hot copy (idempotent: a missing original is fine).
        if remote.stat(task_path, backend=backend) is not None:
            remote.delete(task_path, backend=backend)
        # (4) write the append-only index shard if absent (idempotent).
        if remote.stat(remote.archive_index_path(tid), backend=backend) is None:
            remote.upload_json(_archive_index_shard(task, archive_path),
                               remote.archive_index_path(tid), backend=backend)
        # (5) Evict the local cache copy. The body has left the remote tasks/
        # tree, but _load_all_tasks seeds task_map from the LOCAL cache (and only
        # ever ADDS remote ids, never removes), so the archiving host would
        # otherwise rebuild this id straight back into the authoritative
        # summaries.json/views on its very next reconcile — resurrecting the
        # archived task fleet-wide and defeating the hot-path exclusion the move
        # exists to provide. Best-effort: never affects the move's success.
        cache.delete_cached_task(tid)
        return True
    except Exception:
        return False


def _read_index_shard(task_id: str, *, backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Read one archived task's cold-index shard, or None if not archived."""
    return remote.download_json(remote.archive_index_path(task_id), backend=backend)


def _list_index_shards(*, backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """List every cold-index shard (archive/index/<id>.json) as parsed dicts.

    Best-effort: a failed listing or a single unreadable shard contributes
    nothing rather than raising. O(archived) — paid ONLY on the opt-in cold path
    (search --archived), never on hot reads. Uses remote.list_json (parallel
    list+download); the fake backend's recursive list returns exactly the shard
    files under the prefix."""
    out: list[dict[str, Any]] = []
    try:
        for _, shard in remote.list_json(remote.archive_index_prefix(), backend=backend):
            if shard.get("id"):
                out.append(shard)
    except Exception:
        pass
    return out


def _claim_retention_marker(now: datetime, *,
                            backend: Optional[list[str]] = None) -> bool:
    """First-host-wins daily throttle for the retention pass — the digest-marker
    pattern (_claim_digest_marker), one rolling file keyed by date-INSIDE-the-JSON.

    Read retention/last-run.json: if its date == today (UTC) another host already
    ran today -> return False (skip). Else write {date, by, at} and re-read; if a
    different host's stamp won the claim, return False (they run, we skip). Files
    has no CAS, so two hosts can rarely both see today's marker absent and both
    proceed — ACCEPTED and harmless (mirrors the digest marker): the archive step
    is idempotent + per-task, so a double-run just re-archives already-archived
    ids as no-ops. The marker is a THROTTLE, not a lock. Any error -> skip (never
    risk an unbounded concurrent pass; next tick/day retries). Never raises."""
    try:
        path = remote.retention_marker_path(now)
        today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        existing = remote.download_json(path, backend=backend)
        if existing is not None and existing.get("date") == today:
            return False
        me = identity.resolve_agent()
        marker = {
            "schema": "fulcra.coordination.retention_marker.v1",
            "date": today, "by": me,
            "at": _iso_z(now),
        }
        if not remote.upload_json(marker, path, backend=backend):
            return False
        # Re-read: if a racing host stamped TODAY's marker instead of ours, yield
        # to them. Only a same-day, different-host stamp counts as losing the race
        # — a re-read still showing an OLDER date just means our write isn't
        # reflected yet (or a stale read), which must NOT make us yield our claim.
        confirm = remote.download_json(path, backend=backend)
        if (confirm is not None and confirm.get("date") == today
                and confirm.get("by") not in (me, None)):
            return False
        return True
    except Exception:
        return False


def _retention_max_per_run() -> int:
    """Per-run archive cap: env FULCRA_COORD_RETENTION_MAX_PER_RUN (default 200).
    A huge first backlog drains over several daily passes rather than blowing
    reconcile's deadline. Non-numeric -> default (best-effort, never crashes)."""
    # env_int already falls back to the default on a non-numeric value; the
    # max(0, ...) clamp keeps a negative override from disabling archival silently.
    return max(0, env_int("FULCRA_COORD_RETENTION_MAX_PER_RUN", 200))


# Wall-clock seconds of headroom to leave before reconcile's deadline. Archiving
# stops once less than this remains, so the view uploads + presence rebuild that
# already ran keep their result and reconcile returns inside its 90s ceiling.
_RETENTION_DEADLINE_HEADROOM_SECONDS = 5.0


def _prune_markers(now: datetime, *, backend: Optional[list[str]] = None) -> int:
    """Delete spent digest dedup markers older than the marker-retention window.

    Lists digest/markers/, deletes each path views.is_prunable_marker flags.
    Markers are regenerable guards with NO history value, so they are deleted
    (platform soft-delete keeps them restorable), not archived. is_prunable_marker
    FAILS SAFE: a path it can't date (no embedded YYYY-MM-DD) is KEPT, never
    pruned. Best-effort: a failed listing prunes nothing; one failed delete is
    skipped, not fatal. Returns the count deleted."""
    n = 0
    try:
        for path in remote.list_files(remote.digest_markers_prefix(), backend=backend):
            if not path.endswith(".json"):
                continue
            if views.is_prunable_marker(path, now):
                try:
                    if remote.delete(path, backend=backend):
                        n += 1
                except Exception:
                    continue
    except Exception:
        pass
    return n


def _prune_dead_presence(now: datetime, *, backend: Optional[list[str]] = None) -> int:
    """Delete per-agent presence records for long-departed agents.

    Lists presence/, downloads each record, deletes those
    views.is_prunable_presence flags (last_seen older than the presence-retention
    window). is_prunable_presence FAILS SAFE: a record with a missing/unparseable
    last_seen is KEPT, never pruned. Presence is a live SNAPSHOT, not history, so
    it's deleted (platform soft-delete keeps it restorable), not archived; a
    pruned agent also drops from the presence aggregate on the next rebuild
    (already a derived view — no extra code). Best-effort, per-item isolated.
    Returns the count deleted."""
    n = 0
    try:
        for path, rec in remote.list_json(remote.presence_prefix(), backend=backend):
            try:
                if views.is_prunable_presence(rec, now):
                    if remote.delete(path, backend=backend):
                        n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


def _prune_dead_health(now: datetime, *, backend: Optional[list[str]] = None) -> int:
    """Delete per-host health records for long-departed hosts — in lockstep with
    _prune_dead_presence (same window), so a decommissioned host's presence AND
    health records disappear together. views.is_prunable_health FAILS SAFE: an
    undatable record is KEPT, never pruned. Best-effort, per-item isolated;
    platform soft-delete keeps a pruned record restorable. Returns count deleted."""
    n = 0
    try:
        for path, rec in remote.list_json(remote.health_prefix(), backend=backend):
            try:
                if views.is_prunable_health(rec, now):
                    if remote.delete(path, backend=backend):
                        n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


def _run_retention(all_tasks: list[dict[str, Any]], *, now: datetime,
                   deadline: float, backend: Optional[list[str]] = None) -> dict[str, Any]:
    """The retention pass, folded into reconcile. Best-effort: NEVER raises into
    the reconcile tick — any failure returns a result dict, logged by the caller.
    Returns {"skipped": True} when throttled/errored, else
    {"archived": N, "deferred": D, "pruned_markers": M, "pruned_presence": K,
    "pruned_health": H}.

    1. THROTTLE: _claim_retention_marker(now) — first host today wins; others skip.
    2. ARCHIVE up to _retention_max_per_run() archivable tasks (views.
       is_archivable_task), stopping early when the TIME BUDGET (caller's
       reconcile `deadline` minus a few seconds' headroom) is nearly spent. The
       remainder is DEFERRED (counted + logged) and drains next pass.
    3. PRUNE spent markers + dead presence + dead health records (the last two on
       the same presence-retention window, so a decommissioned host's presence and
       health records drop in lockstep).
    Per-item isolation: one task's archive failure is skipped, not fatal. The
    `deadline` is reconcile's existing deadline local, so the budget COMPOSES with
    (never double-counts) reconcile's 90s ceiling."""
    import time
    # Budget gate FIRST, before any I/O: if reconcile has already spent most of
    # its deadline (e.g. a slow view-upload phase), don't even attempt the
    # throttle-marker read/write — that I/O would itself risk overrunning the
    # ceiling. The next tick (with a fresh budget) picks it up. Composes with,
    # never double-counts, reconcile's deadline.
    budget_floor = deadline - _RETENTION_DEADLINE_HEADROOM_SECONDS
    if time.monotonic() >= budget_floor:
        return {"skipped": True}
    try:
        if not _claim_retention_marker(now, backend=backend):
            return {"skipped": True}
    except Exception:
        return {"skipped": True}

    cap = _retention_max_per_run()
    candidates = [t for t in all_tasks if views.is_archivable_task(t, now)]
    archived = 0
    deferred = 0
    for t in candidates:
        if archived >= cap or time.monotonic() >= budget_floor:
            deferred += 1
            continue
        try:
            if _archive_task(t, backend=backend):
                archived += 1
            else:
                deferred += 1  # transient failure; retried next pass
        except Exception:
            deferred += 1

    pruned_markers = _prune_markers(now, backend=backend)
    pruned_presence = _prune_dead_presence(now, backend=backend)
    pruned_health = _prune_dead_health(now, backend=backend)
    return {"archived": archived, "deferred": deferred,
            "pruned_markers": pruned_markers, "pruned_presence": pruned_presence,
            "pruned_health": pruned_health}
