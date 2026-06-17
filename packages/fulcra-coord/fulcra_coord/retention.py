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

import json
from datetime import datetime, timezone
from typing import Any, Optional

from . import cache, remote, views, identity, schema
from . import env_int, remote_root
from .events import _is_snapshot_payload
from .io import _confirmed_absent, _load_task_summaries
from .writepipe import _write_task_and_views
from .output import info as _info, print_json as _print_json, err as _err
from .timeutil import iso_z as _iso_z, now_iso as _now_iso
# Direct store-module import for the transport's failure observable and the
# not-found classifier — same pattern (and rationale) as io.py:
# ``last_download_error`` must be read as a LIVE module attribute, and
# ``remote``'s re-exports would not track the store mutating it. No new
# dependency edge: retention already reaches the store through ``remote``.
from fulcra_coord_files import store as _files_store


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


def _cold_copy_state(path: str, *, backend: Optional[list[str]] = None) -> str:
    """Tombstone-aware presence verdict for a COLD (archive-side) path:
    ``"present"`` / ``"absent"`` / ``"unknown"``.

    WHY a bare stat cannot answer this (the F7-UNDOING hazard, 2026-06-11 —
    the same stat-dishonesty class as the tombstone-absence fix, opposite
    direction): the platform delete is SOFT, so after ``cmd_restore`` deletes
    the archive copy, stat on the archive path STILL answers from version
    history. A gate reading stat-non-None as "cold copy present" sees that
    tombstone as an already-archived body — so the next retention pass on a
    re-aged restored task (reachable since #172 ages from ``restored_at``)
    skipped the fresh upload, its stat-based verify passed vacuously, and it
    deleted the hot copy: body GONE from the hot path with only a tombstone
    in the archive, silently undoing the restore.

    The verdicts (io._confirmed_absent's tombstone signature applied to the
    presence question — note the OPPOSITE fail-safe direction: absence checks
    fail toward "unconfirmable, do nothing destructive"; this presence check
    fails toward "unknown, KEEP the hot copy"):

    * stat misses ⇒ ``absent``. Same verdict the old gate gave; a fresh
      upload is the safe direction even if the miss was transport weather —
      it just rewrites the current body.
    * stat answers and the download reads a JSON dict ⇒ ``present``. A cold
      copy only counts when it could actually serve a future restore, so a
      readable-but-corrupt body reads as ``absent`` (overwrite-with-fresh —
      a successful read is never a tombstone, so overwriting is safe).
    * stat answers, the download fails with a POSITIVE not-found-class error
      and the bus probes reachable ⇒ ``absent``: the tombstone signature.
      The "cold copy" is a soft-delete marker, not an archived body.
    * any other failure (transient weather, silent/unknown stderr,
      unreachable bus, raising probe) ⇒ ``unknown``: unconfirmable. The
      caller must DEFER the task this pass — fail toward keeping the hot
      copy, never toward deleting it.

    COST: the download probe is spent only when stat answers (the
    already-archived / tombstoned minority), one read per candidate on a
    daily background pass — the right trade for a no-loss gate."""
    try:
        if remote.stat(path, backend=backend) is None:
            return "absent"
        raw = remote.download(path, backend=backend)
        if raw is not None:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return "absent"  # readable-but-corrupt: overwrite with fresh
            return "present" if isinstance(body, dict) else "absent"
        err = _files_store.last_download_error
        if _files_store._is_not_found_failure(err):
            # Tombstone-shaped — but only a reachable bus makes it a verdict.
            return "absent" if remote.probe_reachable(backend) else "unknown"
        return "unknown"
    except Exception:
        return "unknown"


def _archive_task(task: dict[str, Any], *, backend: Optional[list[str]] = None) -> bool:
    """Crash-safely MOVE a terminal+aged task out of the hot path into the cold
    archive. Returns True on a completed (or already-complete) move, False if the
    move could not be safely completed (caller logs + retries next pass).

    ORDER (no-loss by construction): upload archive body -> VERIFY it landed
    READABLE (download — never a bare stat, which a tombstone answers; see
    _cold_copy_state) -> only THEN delete tasks/<id>.json -> write the per-id
    index shard. A crash anywhere leaves the body in BOTH places (a
    recoverable duplicate), never lost. IDEMPOTENT: if a READABLE archive body
    already exists we skip the upload, still ensure the original is deleted
    and the shard exists, so archiving an already-archived id (or finishing a
    crashed move) is a no-op. A TOMBSTONED archive path (cmd_restore's
    soft-deleted cold copy — the F7-undoing hazard) counts as ABSENT, so a
    re-aged restored task gets a FRESH upload instead of losing its hot copy
    behind a stat that answered from version history; an UNCONFIRMABLE cold
    state (transient probe failure) defers the task this pass, hot copy kept.

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
        # (0) COLD-COPY PROBE (tombstone discipline — see _cold_copy_state):
        # "present" means a READABLE archived body, never a bare stat answer.
        # An unconfirmable verdict defers the whole task this pass: every
        # branch below is gated on this answer, and guessing it is exactly
        # how the stat gate deleted hot copies behind tombstones.
        cold_state = _cold_copy_state(archive_path, backend=backend)
        if cold_state == "unknown":
            return False  # defer; fail toward keeping the hot copy
        archive_exists = cold_state == "present"
        # (0b) PHANTOM GUARD: only proceed if the hot copy exists OR the move is
        # already (partly) done (archive body present). Neither => stale-cache
        # phantom: evict the local copy and skip — never write a phantom archive.
        if not archive_exists and remote.stat(task_path, backend=backend) is None:
            cache.delete_cached_task(tid)
            return False
        # (1) ensure the body is in the archive (idempotent): upload only if absent.
        if not archive_exists:
            if not remote.upload_json(task, archive_path, backend=backend):
                return False
            # (2) VERIFY the body just uploaded is READABLE before any delete —
            # the no-loss gate. A stat is NOT enough: a tombstoned archive path
            # answers stat from version history, so a lying/failed upload over
            # a tombstone would stat-verify vacuously and step (3) would
            # destroy the only readable copy. (On the archive_exists branch the
            # probe above already proved readability — no second download.)
            if remote.download_json(archive_path, backend=backend) is None:
                return False
        # (3) only now remove the hot copy (idempotent: a missing original is fine).
        if remote.stat(task_path, backend=backend) is not None:
            remote.delete(task_path, backend=backend)
        # (4) write the append-only index shard if absent-or-unreadable
        # (idempotent). Same tombstone hole as the body gate: cmd_restore
        # soft-deletes the shard, so a stat gate would read the tombstone as
        # "shard exists" and skip the rewrite — leaving the re-archived task
        # invisible to `search --archived` and unrestorable. Require a READABLE
        # shard; rewriting over a transient read failure is harmless (same id,
        # same path, fresh archived_at).
        if remote.download_json(remote.archive_index_path(tid), backend=backend) is None:
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
                            backend: Optional[list[str]] = None
                            ) -> tuple[bool, Optional[dict[str, Any]]]:
    """First-host-wins daily throttle for the retention pass — the digest-marker
    dedup pattern (cf. _record_digest_marker), one rolling file keyed by date-INSIDE-the-JSON.

    Read retention/last-run.json: if its date == today (UTC) another host already
    ran today -> claimed False (skip). Else write {date, by, at} and re-read; if a
    different host's stamp won the claim, claimed False (they run, we skip). Files
    has no CAS, so two hosts can rarely both see today's marker absent and both
    proceed — ACCEPTED and harmless (mirrors the digest marker): the archive step
    is idempotent + per-task, so a double-run just re-archives already-archived
    ids as no-ops. The marker is a THROTTLE, not a lock. Any error -> skip (never
    risk an unbounded concurrent pass; next tick/day retries). Never raises.

    Returns ``(claimed, marker)`` — ``marker`` is the freshest last-run record
    this claim OBSERVED or WROTE (None when unknown), threaded up through
    ``_run_retention`` so the health record reuses it instead of re-downloading
    retention/last-run.json every tick (perf, 2026-06-11 loop-2 pass #6)."""
    try:
        path = remote.retention_marker_path(now)
        today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        existing = remote.download_json(path, backend=backend)
        if existing is not None and existing.get("date") == today:
            return False, (existing if isinstance(existing, dict) else None)
        me = identity.resolve_agent()
        marker = {
            "schema": "fulcra.coordination.retention_marker.v1",
            "date": today, "by": me,
            "at": _iso_z(now),
        }
        if not remote.upload_json(marker, path, backend=backend):
            # Our stamp didn't land: the OLD marker (if any) stays the truth.
            return False, (existing if isinstance(existing, dict) else None)
        # Re-read: if a racing host stamped TODAY's marker instead of ours, yield
        # to them. Only a same-day, different-host stamp counts as losing the race
        # — a re-read still showing an OLDER date just means our write isn't
        # reflected yet (or a stale read), which must NOT make us yield our claim.
        confirm = remote.download_json(path, backend=backend)
        if (confirm is not None and confirm.get("date") == today
                and confirm.get("by") not in (me, None)):
            # The racing host's stamp is the freshest last-run truth.
            return False, (confirm if isinstance(confirm, dict) else None)
        return True, marker
    except Exception:
        return False, None


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
    """Delete spent daily dedup markers older than the marker-retention window:
    digest markers (digest/markers/) AND role vacancy-escalation markers
    (roles/<name>/escalations/<YYYY-MM-DD>.json — 2026-06-11 wave: minted daily
    per vacant role by _maybe_escalate_role_vacancy and previously never swept,
    so they accumulated forever and every roles listing paid for the pile).

    Markers are regenerable guards with NO history value, so they are deleted
    (platform soft-delete keeps them restorable), not archived. Both predicates
    (views.is_prunable_marker / views.is_prunable_escalation_marker) FAIL SAFE:
    a path they can't date is KEPT, never pruned — and the escalation predicate
    additionally matches ONLY the .../escalations/<date>.json shape, so role
    records and lease files sharing the roles/ prefix can never enter the
    delete set. Best-effort and INDEPENDENT per family: each listing failure
    prunes nothing from ITS family without aborting the other; one failed
    delete is skipped, not fatal. Returns the total count deleted."""
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
    try:
        # One recursive listing of roles/ (the live list contract): the
        # escalation predicate's strict path-shape match does the filtering.
        for path in remote.list_files(remote.roles_prefix(), backend=backend):
            if views.is_prunable_escalation_marker(path, now):
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


def _continuity_keep() -> int:
    """How many of the NEWEST checkpoints to keep per task: env
    FULCRA_COORD_CONTINUITY_KEEP (default 10), floored at 1.

    The floor is load-bearing: a 0 / negative override must NEVER delete the only
    (newest) checkpoint — continuity always keeps at least the latest archive so a
    resuming agent has something to read. env_int already falls back to the default
    on a non-numeric value; max(1, ...) clamps the explicit-but-too-small case."""
    return max(1, env_int("FULCRA_COORD_CONTINUITY_KEEP", 10))


def _walk_continuity_checkpoint_dirs(backend: Optional[list[str]], *,
                                     deadline: Optional[float] = None
                                     ) -> dict[str, list[str]]:
    """Partition ONE recursive listing of the continuity tree into
    ``{checkpoints-dir: [file paths under it]}``.

    THE CONTRACT (2026-06-11 wave, from the 2026-06-10 measured pass — the same
    listing contract load_loop_records' top-level path filter is built on, and
    the shape the fake test backend's rglob reproduces): the live ``fulcra file
    list`` returns a RECURSIVE listing of FILES under the prefix, with NO
    directory entries. The previous walker descended trailing-slash directory
    entries level by level — a contract the live backend does not implement —
    so in PRODUCTION it found zero children and ``_prune_continuity_checkpoints``
    never deleted anything: the unbounded checkpoints/ growth the GC exists to
    bound was silently un-GC'd. Partitioning one listing by path segments fixes
    that AND costs one list call instead of O(directories).

    TOLERANT of a backend that DOES emit directory entries (trailing slash):
    a dir entry whose last segment is ``checkpoints`` registers the dir (with
    no children of its own — a dir entry is never a delete candidate); its
    files arrive as separate file entries. Both contracts therefore produce
    the same partition.

    Best-effort: a failed listing yields an empty partition rather than
    raising (the caller is wrapped too, so nothing escapes into reconcile).
    When reconcile supplies a deadline, the same budget floor as the prune
    loop gates the single listing — a spent budget must not buy a large
    listing it can't act on."""
    import time
    budget_floor = (deadline - _RETENTION_DEADLINE_HEADROOM_SECONDS
                    if deadline is not None else None)
    found: dict[str, list[str]] = {}
    if budget_floor is not None and time.monotonic() >= budget_floor:
        return found
    root = f"{remote_root()}/continuity"
    try:
        entries = remote.list_files(root, backend=backend)
    except Exception:
        return found
    prefix = root if root.endswith("/") else root + "/"
    for entry in entries:
        is_dir = entry.endswith("/")
        path = entry.rstrip("/")
        if not path.startswith(prefix):
            continue  # defensive: never partition a path outside the tree
        segments = path[len(prefix):].split("/")
        if is_dir:
            if segments and segments[-1] == "checkpoints":
                found.setdefault(path, [])
            continue
        # A file: find the checkpoints dir it sits under. The LAST `checkpoints`
        # ancestor wins — the file's immediate prunable directory — so a
        # pathological task literally named "checkpoints" still partitions to
        # the dir its archives actually live in.
        for i in range(len(segments) - 2, -1, -1):
            if segments[i] == "checkpoints":
                chk_dir = prefix + "/".join(segments[: i + 1])
                found.setdefault(chk_dir, []).append(path)
                break
    return found


def _prune_continuity_checkpoints(now: datetime, *,
                                  backend: Optional[list[str]] = None,
                                  deadline: Optional[float] = None) -> int:
    """Prune old continuity checkpoint archives: keep the newest
    _continuity_keep() per task's ``checkpoints/`` dir, delete the rest.

    WHY this exists: continuity.write_checkpoint writes an immutable, uniquely
    named archive (CHK-<stamp>-<task>-<hex>.json) on EVERY snapshot (every
    SessionEnd + PreCompact + openclaw compaction since #92). ``latest.json``
    overwrites in place and is fine; it's ``checkpoints/`` that grows UNBOUNDED.
    The other pruners never touch continuity/**, so this is the GC that bounds it.

    HOW:
      * ONE RECURSIVE LISTING (_walk_continuity_checkpoint_dirs) partitioned by
        path segments yields every ``checkpoints/`` dir WITH its files — the
        live listing contract is recursive files-only (see the walker's
        docstring; the old per-directory descent matched no real backend and
        this GC silently never ran in production).
      * Per dir, filter to its ``CHK-*.json`` files and sort by filename
        DESCENDING. The <stamp> is a zero-padded lexically-sortable timestamp,
        so filename sort == chronological sort: index 0 is NEWEST. Keep the
        first `keep`, delete the rest (oldest-first).
      * NEVER deletes ``latest.json`` (or anything not matching ``CHK-``) — only
        immutable checkpoint archives are prunable; latest.json is the live
        pointer a resuming agent reads.
      * BUDGET/CAP, mirroring the other prune steps: stop deleting AND stop
        walking once the wall-clock budget is nearly spent (when a `deadline` is
        supplied), and cap total deletions per run at _retention_max_per_run()
        (reusing the same knob — no second cap). Soft-deletes are recoverable, so
        a partial pass is safe; the remainder drains next run.

    BEST-EFFORT: the whole body is wrapped so it NEVER raises into _run_retention
    (which must never raise into the reconcile tick). A single delete failure is
    skipped, not fatal. Returns the count deleted."""
    import time
    deleted = 0
    try:
        keep = _continuity_keep()
        cap = _retention_max_per_run()
        budget_floor = (deadline - _RETENTION_DEADLINE_HEADROOM_SECONDS
                        if deadline is not None else None)
        if cap <= 0:
            return 0
        if budget_floor is not None and time.monotonic() >= budget_floor:
            return 0
        partition = _walk_continuity_checkpoint_dirs(backend, deadline=deadline)
        # Sorted iteration: a capped/deadline-shortened pass deletes a
        # deterministic subset across machines, not a dict-order lottery.
        for chk_dir in sorted(partition):
            if deleted >= cap:
                break
            if budget_floor is not None and time.monotonic() >= budget_floor:
                break
            entries = partition[chk_dir]
            # Only immutable checkpoint archives are prunable. A bare ``latest.json``
            # (or any non-CHK file) is excluded HERE so it can never enter the
            # delete set — the load-bearing safety property.
            archives = [p for p in entries
                        if p.endswith(".json")
                        and p.rsplit("/", 1)[-1].startswith("CHK-")]
            if len(archives) <= keep:
                continue
            # Newest first: filename (= padded stamp) sorts chronologically.
            archives.sort(reverse=True)
            stale = archives[keep:]  # everything past the newest `keep`
            for path in stale:
                if deleted >= cap:
                    break
                if budget_floor is not None and time.monotonic() >= budget_floor:
                    break
                try:
                    if remote.delete(path, backend=backend):
                        deleted += 1
                except Exception:
                    continue
    except Exception:
        pass
    return deleted


def _eventlog_keep() -> int:
    """How many of the NEWEST event shards to keep per LIVE task: env
    FULCRA_COORD_EVENTLOG_KEEP (default 20), floored at 1.

    The floor is load-bearing: a 0 / negative override must NEVER let the prune
    window dip below the latest snapshot's safety. The prune always computes
    ``keep_from = min(snap_idx, max(0, len(pairs) - keep))`` so the keep window
    is only ever an ADDITIONAL guard on top of "never delete the latest snapshot
    or anything after it"; the ``max(0, ...)`` clamps ``len - keep`` to a
    non-negative slice start, so a ``keep`` larger than the event count can never
    produce a negative ``keep_from`` (which would slice off the TAIL and delete
    the snapshot). A 0 window paired with a tiny event list could still surprise
    a future reader, so we clamp to 1 to keep the contract obvious: at minimum we
    retain one recent event beyond the structural snapshot floor. env_int already
    falls back to the default on a non-numeric value; max(1, ...) clamps the
    explicit-but-too-small case."""
    return max(1, env_int("FULCRA_COORD_EVENTLOG_KEEP", 20))


def _prune_event_log(all_tasks: list[dict[str, Any]], now: datetime, *,
                     backend: Optional[list[str]] = None,
                     deadline: Optional[float] = None) -> int:
    """Bound the unbounded event-log growth (Root cause B).

    WHY this exists: the event-sourcing dual-write (_write_task_and_views)
    appends an immutable shard at events/tasks/<id>/<event_id>.json on EVERY
    task mutation, FOREVER. No other pruner touches the events/ family, so the
    log grows without bound (B1) and every archived task orphans its whole shard
    tree (B2) — read_events/fold_task degrade O(mutations-per-task) on the live
    bus. This is the GC that bounds both.

    HOW (two branches per task dir under events/tasks/):

      * ORPHAN (B2): a task dir whose id is NOT in the live set AND whose hot
        file (tasks/<id>.json) is CONFIRMED absent — via io._confirmed_absent
        (stat miss + bus probes reachable), NOT a bare stat-None, which the
        transport also returns on a failed read (F6) — belongs to an
        archived/deleted task: delete ALL its shards. The positive confirmation
        guards both a PARTIAL all_tasks (hot file still exists -> task merely
        missing from this caller's list) and a transport blip (absence
        unknowable): in either case SKIP — never prune a possibly-live task's
        tree; an unconfirmable orphan simply defers to the next healthy pass.

      * LIVE (B1): for a task still in the live set, keep the LATEST snapshot
        event + the most recent _eventlog_keep() events; delete only shards
        STRICTLY OLDER than the latest snapshot. A snapshot is self-complete
        (fold_task replaces accumulated state wholesale on a snapshot), so every
        event before the latest snapshot is stale. A DELTA-ONLY task (no snapshot
        anywhere) is NEVER pruned (fail-safe — each delta may carry a unique field
        never re-set, so dropping any delta could lose fold state).

    FOLD-EQUIVALENCE BOUNDARY: for the CURRENT writer the fold output is unchanged
    after the prune. _write_task_and_views mints a fresh ``idempotency_key``
    (= op_id = uuid4 hex) per write and emits a FULL snapshot every time, so a
    pruned pre-snapshot event can never be the first-seen copy of a surviving
    event's identity. The one THEORETICAL divergence: fold_task dedups by
    (actor, idempotency_key), first-in-sort-order wins; a post-snapshot delta that
    shared an (actor, idempotency_key) pair with a PRE-snapshot event would, once
    the pre-snapshot copy is pruned, become first-seen and newly-applied — a
    different folded state. The unique-per-op writer never emits such a duplicate
    pair, so this branch is unreachable today; it is documented so a future writer
    that reuses idempotency keys across the snapshot boundary doesn't silently
    break the guarantee.

    Shards are sorted by the SAME key fold_task uses — (at, event_id) — so
    "latest snapshot" and the keep window line up exactly with the reducer.

    BUDGET/CAP, mirroring the other prune steps (_prune_continuity_checkpoints):
    stop deleting once the wall-clock budget is nearly spent (when a `deadline`
    is supplied) and cap total deletions per run at _retention_max_per_run()
    (reusing the same knob — no second cap). Soft-deletes are recoverable, so a
    partial pass is safe; the remainder drains next run.

    BEST-EFFORT: the whole body is wrapped so it NEVER raises into _run_retention
    (which must never raise into the reconcile tick). A single task's
    listing/delete failure is skipped (per-item try/except), not fatal. Returns
    the count deleted."""
    import time
    deleted = 0
    try:
        keep = _eventlog_keep()
        cap = _retention_max_per_run()
        budget_floor = (deadline - _RETENTION_DEADLINE_HEADROOM_SECONDS
                        if deadline is not None else None)
        if cap <= 0:
            return 0
        if budget_floor is not None and time.monotonic() >= budget_floor:
            return 0

        # The live set: ids of every task the caller currently knows about. A
        # task dir whose id is here is LIVE (B1 window prune); one that's absent
        # is a candidate ORPHAN (B2), confirmed only by a positive stat-miss.
        live_ids = {t.get("id") for t in all_tasks if t.get("id")}

        events_root = f"{remote.remote_root()}/events/tasks/"
        try:
            children = remote.list_files(events_root, backend=backend)
        except Exception:
            return deleted

        task_ids: list[str] = []
        seen_task_ids: set[str] = set()
        for child in children:
            # Support both list contracts seen in the repo:
            # - mocked non-recursive listings expose task dirs with trailing "/"
            # - the fake/real file-oriented backend can expose recursive shard paths
            #   such as events/tasks/<task_id>/<event_id>.json.
            task_id = ""
            if child.endswith("/"):
                task_id = child.rstrip("/").rsplit("/", 1)[-1]
            elif child.startswith(events_root):
                rest = child[len(events_root):]
                if "/" in rest:
                    task_id = rest.split("/", 1)[0]
            if task_id and task_id not in seen_task_ids:
                seen_task_ids.add(task_id)
                task_ids.append(task_id)

        for task_id in task_ids:
            if deleted >= cap:
                break
            if budget_floor is not None and time.monotonic() >= budget_floor:
                break
            try:
                if task_id not in live_ids:
                    # B2 ORPHAN branch — but ONLY on POSITIVE absence. F6
                    # (2026-06-11 wave): the old gate was `stat is not None ->
                    # skip`, but the transport collapses "read FAILED" into the
                    # same None as "confirmed gone" — so one transient 504
                    # (after retry) read as proof of absence, and compounded
                    # with a partial all_tasks load this branch DELETED a live
                    # task's entire fold source. _confirmed_absent (the
                    # role_ops C1 / write-path idiom) requires the stat miss
                    # AND a reachable bus before anything destructive may act
                    # on "absent"; the probe spawn is spent only on the
                    # stat-miss path. Unconfirmable -> SKIP this task this
                    # pass (defer); a genuinely archived tree drains on the
                    # next healthy pass. A still-present hot file likewise
                    # means the task may be live and merely missing from a
                    # partial all_tasks: SKIP it.
                    if not _confirmed_absent(remote.task_remote_path(task_id),
                                             backend=backend):
                        continue
                    # Enumerate via list_files, NOT list_json: the orphan branch
                    # deletes the WHOLE tree and never inspects a payload, while
                    # list_json silently drops any shard whose JSON doesn't parse
                    # to a dict — so a corrupt/half-written shard in an archived
                    # task's tree would survive forever (incomplete GC, the B2
                    # corrupt-shard hole). list_files sees every file; filter to
                    # .json so a stray non-shard file is never deleted.
                    try:
                        shards = [p for p in remote.list_files(
                            remote.events_prefix(task_id), backend=backend)
                            if p.endswith(".json")]
                    except Exception:
                        continue
                    for path in shards:
                        if deleted >= cap:
                            break
                        if (budget_floor is not None
                                and time.monotonic() >= budget_floor):
                            break
                        try:
                            if remote.delete(path, backend=backend):
                                deleted += 1
                        except Exception:
                            continue
                    continue

                # B1 LIVE branch — window-prune everything strictly before the
                # latest snapshot.
                pairs = remote.list_json(
                    remote.events_prefix(task_id), backend=backend)
                if not pairs:
                    continue
                # Sort by the SAME key fold_task uses so "latest snapshot" and the
                # keep window align exactly with the reducer's view of the stream.
                pairs.sort(key=lambda pr: (pr[1].get("at", ""),
                                           pr[1].get("event_id", "")))
                snap_idx = -1
                for i, (_path, rec) in enumerate(pairs):
                    if _is_snapshot_payload(rec.get("payload") or {}):
                        snap_idx = i
                if snap_idx < 0:
                    # Delta-only task: dropping any delta could lose fold state.
                    # Fail-safe — never prune.
                    continue
                # Keep everything from the latest snapshot onward AND at least the
                # most recent `keep` events. keep_from is the first index we KEEP;
                # min() guarantees we never delete at/after the latest snapshot.
                keep_from = min(snap_idx, max(0, len(pairs) - keep))
                for path, _rec in pairs[:keep_from]:
                    if deleted >= cap:
                        break
                    if (budget_floor is not None
                            and time.monotonic() >= budget_floor):
                        break
                    try:
                        if remote.delete(path, backend=backend):
                            deleted += 1
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return deleted


def _expire_stale_broadcasts(all_tasks: list[dict[str, Any]], now: datetime, *,
                             backend: Optional[list[str]] = None,
                             deadline: Optional[float] = None) -> int:
    """Auto-EXPIRE stale never-claimed broadcasts: transition each
    views.is_expirable_broadcast task proposed->abandoned, so the existing
    cold-archive sweeps it out of the hot path on a LATER pass (it can't archive
    same-tick — archive eligibility ages from the abandon timestamp, which we set
    to `now`). Recoverable via `restore`. Returns the count actually abandoned.

    Why this exists: broadcasts age out of the live INBOX at 3d (a read filter) but
    otherwise live on the bus forever, so `status` drowns in stale "X is LIVE"
    fan-out. This is the GC that finally clears them.

    Discipline mirrors the archive loop in _run_retention:
      * BUDGET/CAP: stop once _retention_max_per_run() expirations are done, or
        (only when a deadline was supplied) once the wall-clock budget is nearly
        spent — so this composes with reconcile's ceiling instead of overrunning it.
      * PER-ITEM ISOLATION: one task's transition/write failure is skipped, never
        fatal. A NeedsReconcile means the task BODY was written (views merely
        lagged), so it IS expired and counts; a ConflictError / any other error
        means the write did NOT land, so we skip it WITHOUT counting (it retries
        next pass).
    """
    import time
    budget_floor = (deadline - _RETENTION_DEADLINE_HEADROOM_SECONDS
                    if deadline is not None else None)
    cap = _retention_max_per_run()
    expired = 0
    for t in all_tasks:
        if expired >= cap:
            break
        if budget_floor is not None and time.monotonic() >= budget_floor:
            break
        if not views.is_expirable_broadcast(t, now):
            continue
        try:
            new_task = schema.apply_transition(
                t, "abandoned", by="reconcile-retention",
                reason="Auto-expired: stale broadcast (proposed, never claimed, "
                       "older than the broadcast-expiry window).",
                dt=now)
            if _write_task_and_views(new_task, backend=backend, command="abandon"):
                expired += 1
        except schema.NeedsReconcile:
            # The body WAS written (only the view rebuild lagged) — the broadcast
            # is abandoned on the bus, so count it. The next reconcile heals views.
            expired += 1
        except Exception:
            # Any other failure (TransitionError / SchemaError / ConflictError /
            # transport) => the body did NOT land; skip without counting and let
            # the next pass retry. One bad task never aborts the sweep.
            continue
    return expired


def _close_stale_messages(all_tasks: list[dict[str, Any]], now: datetime, *,
                          backend: Optional[list[str]] = None,
                          deadline: Optional[float] = None) -> int:
    """Auto-CLOSE aged delivered MESSAGE-CLASS directives: transition each
    views.is_closable_message task proposed->done (evidence names the TTL), so
    the existing cold-archive sweeps it out of the hot path on a LATER pass
    (archive eligibility ages from the done timestamp, which we set to `now`).
    Recoverable via `restore` after that archive. Returns the count closed.

    Why this exists: tells / FYIs / verdict echoes carry information and expect
    nothing back, so NOBODY ever marks them done — they sat status=proposed
    forever (2026-06-11: 211 of ~480 hot tasks), monotonically bloating every
    listing the platform gateway serves under its ~15s limit. Broadcast expiry
    (above) clears the fan-out flavor; this is the GC for the one-to-one
    flavor. The transition is `done` (not abandoned): a delivered message DID
    its job — closing it is completion, not abandonment — and the evidence
    string preserves the audit trail through the normal done machinery.

    WHAT IS DELIBERATELY NEVER AUTO-CLOSED (the predicate's exclusions, pinned
    by tests): anything with expects_response truthy — THE CLOSED-LOOP
    GUARANTEE, a loop stays open until a bus-native response, period;
    review/dispatch/question/signoff (and unknown) loop kinds; broadcasts
    (their own expiry pass); kind:idea backlog items; self-owned work tasks
    (no assignee); non-proposed statuses; undatable created_at (parse-don't-
    compare — keep what we can't date).

    Discipline mirrors _expire_stale_broadcasts exactly:
      * BUDGET/CAP: stop once _retention_max_per_run() closes are done, or
        (only when a deadline was supplied) once the wall-clock budget is nearly
        spent — composes with reconcile's ceiling instead of overrunning it.
      * PER-ITEM ISOLATION: one task's transition/write failure is skipped,
        never fatal. A NeedsReconcile means the task BODY was written (views
        merely lagged), so it IS closed and counts; a ConflictError / any other
        error means the write did NOT land, so we skip it WITHOUT counting (it
        retries next pass).
    """
    import time
    budget_floor = (deadline - _RETENTION_DEADLINE_HEADROOM_SECONDS
                    if deadline is not None else None)
    cap = _retention_max_per_run()
    ttl_days = views._message_ttl_days()
    # :g renders the default as "7", not "7.0" — the evidence string is read
    # by humans auditing why a message closed.
    evidence = (f"delivered message auto-closed after {ttl_days:g} days "
                "(message-class TTL)")
    closed = 0
    for t in all_tasks:
        if closed >= cap:
            break
        if budget_floor is not None and time.monotonic() >= budget_floor:
            break
        if not views.is_closable_message(t, now):
            continue
        try:
            new_task = schema.apply_transition(
                t, "done", by="reconcile-retention",
                evidence=evidence,
                verification_level="automated",
                dt=now)
            if _write_task_and_views(new_task, backend=backend, command="done"):
                closed += 1
        except schema.NeedsReconcile:
            # The body WAS written (only the view rebuild lagged) — the message
            # is closed on the bus, so count it. The next reconcile heals views.
            closed += 1
        except Exception:
            # Any other failure (TransitionError / SchemaError / ConflictError /
            # transport) => the body did NOT land; skip without counting and let
            # the next pass retry. One bad task never aborts the sweep.
            continue
    return closed


def _prune_provenance_sidecars(all_tasks: list[dict[str, Any]], *,
                               deadline: Optional[float] = None) -> int:
    """Prune orphaned ``*.prov.json`` provenance sidecars under ``cache.meta_dir()``.

    WHY this exists (root cause A leftover): ``cache.write_provenance`` writes a
    ``<key>.prov.json`` sidecar (key = ``cache._prov_key(task_id)``) for every
    task body read in events-mode, each holding a full ``fold_base`` body.
    ``clear_provenance`` drops one after a successful upload, but a task that
    simply ages out (archived / deleted remotely) never gets its sidecar cleared,
    so the family grows without bound on a long-lived host. This is the GC that
    bounds it.

    HOW:
      * Build ``live_keys`` = the ``_prov_key`` of every LIVE task id (tasks
        missing an ``id`` contribute nothing — they can't anchor a live sidecar).
      * List ``meta_dir()`` for ``*.prov.json`` files; a file's key is its name
        with the ``.prov.json`` suffix stripped. If that key is NOT in
        ``live_keys`` the sidecar belongs to a task no longer in the live set ->
        unlink it.

    SAFETY (load-bearing): these are LOCAL files — deleted with ``Path.unlink``,
    NEVER ``remote.delete``. ONLY ``*.prov.json`` orphans are touched: the
    ``*.stat.json`` meta sidecars (hash-keyed over MIXED task+view paths, so not
    safely orphan-prunable, and tiny — deliberately out of scope) and anything
    else in ``meta_dir()`` are never matched. A live task's prov sidecar always
    survives.

    BUDGET/CAP, mirroring the sibling prune passes: cap deletions per run at
    ``_retention_max_per_run()`` and stop once the wall-clock budget is nearly
    spent (when a ``deadline`` is supplied) so this composes with reconcile's
    ceiling. BEST-EFFORT: the whole body is wrapped so it NEVER raises into
    ``_run_retention`` (which must never raise into the reconcile tick); a single
    delete failure is skipped, not fatal. Returns the count deleted."""
    import time
    deleted = 0
    try:
        cap = _retention_max_per_run()
        if cap <= 0:
            return 0
        budget_floor = (deadline - _RETENTION_DEADLINE_HEADROOM_SECONDS
                        if deadline is not None else None)
        if budget_floor is not None and time.monotonic() >= budget_floor:
            return 0
        meta = cache.meta_dir()
        if not meta.exists():
            return 0
        live_keys = {cache._prov_key(t["id"]) for t in all_tasks
                     if isinstance(t, dict) and t.get("id")}
        for path in meta.glob("*.prov.json"):
            if deleted >= cap:
                break
            if budget_floor is not None and time.monotonic() >= budget_floor:
                break
            # Strip the ``.prov.json`` suffix to recover the sidecar's key.
            key = path.name[: -len(".prov.json")]
            if key in live_keys:
                continue  # belongs to a live task — keep it
            try:
                path.unlink()
                deleted += 1
            except OSError:
                # One bad unlink never aborts the sweep; the next pass retries.
                continue
    except Exception:
        pass
    return deleted


def _run_retention(all_tasks: list[dict[str, Any]], *, now: datetime,
                   deadline: float, backend: Optional[list[str]] = None) -> dict[str, Any]:
    """The retention pass, folded into reconcile. Best-effort: NEVER raises into
    the reconcile tick — any failure returns a result dict, logged by the caller.
    Returns {"skipped": True} when throttled/errored, else
    {"archived": N, "deferred": D, "expired_broadcasts": E,
    "closed_messages": T, "pruned_markers": M,
    "pruned_presence": K, "pruned_health": H, "pruned_continuity": C,
    "pruned_events": V, "pruned_provenance": P}. Either shape MAY carry
    "retention_marker" — the freshest retention/last-run.json record the
    throttle claim observed or wrote — so cmd_reconcile's health record reuses
    it instead of paying a third download per tick (perf loop-2 #6); absent
    when the pass never reached the marker (budget gate / claim error).

    1. THROTTLE: _claim_retention_marker(now) — first host today wins; others skip.
    2. ARCHIVE up to _retention_max_per_run() archivable tasks (views.
       is_archivable_task), stopping early when the TIME BUDGET (caller's
       reconcile `deadline` minus a few seconds' headroom) is nearly spent. The
       remainder is DEFERRED (counted + logged) and drains next pass.
    3. PRUNE spent markers + dead presence + dead health records (the last two on
       the same presence-retention window, so a decommissioned host's presence and
       health records drop in lockstep), plus old continuity checkpoint archives
       (keep the newest _continuity_keep() per task; the recursive sweep that
       bounds the unbounded checkpoints/ growth), plus old event-log shards
       (Root cause B: window-prune live tasks below their latest snapshot and GC
       orphaned archived-task shard trees, bounding the unbounded events/ growth),
       plus orphaned LOCAL `*.prov.json` provenance sidecars (root cause A
       leftover: a sidecar whose task aged out of the live set is unlinked,
       bounding the unbounded meta/ growth).
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
        claim = _claim_retention_marker(now, backend=backend)
        # Tolerate a bare bool: several tests (and any external monkeypatch)
        # stub _claim_retention_marker with True/False. The threaded marker is
        # a perf hand-off (saves the health record's re-download — loop-2 #6),
        # never load-bearing, so an unknown marker just means the caller falls
        # back to its own read.
        claimed, marker = (claim if isinstance(claim, tuple)
                           else (bool(claim), None))
        if not claimed:
            out: dict[str, Any] = {"skipped": True}
            if marker is not None:
                out["retention_marker"] = marker
            return out
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

    # Expire stale never-claimed broadcasts AFTER the archive loop: the archive
    # candidate list above was computed from the PRE-expiry task states, so running
    # expire afterward leaves it unchanged. A just-abandoned broadcast can't archive
    # this tick anyway (archive ages from the abandon timestamp = now), so it drains
    # on a later pass. Same budget/cap discipline as archive.
    expired_broadcasts = _expire_stale_broadcasts(
        all_tasks, now, backend=backend, deadline=deadline)

    # Close aged delivered message-class directives (tells / FYIs / verdict
    # echoes — never anything expecting a response: the closed-loop guarantee).
    # Same placement rationale as broadcast expiry: the archive candidate list
    # above was computed from PRE-close states, and a just-closed message can't
    # archive this tick anyway (archive ages from the done timestamp = now), so
    # it drains on a later pass. Same budget/cap discipline.
    closed_messages = _close_stale_messages(
        all_tasks, now, backend=backend, deadline=deadline)

    pruned_markers = _prune_markers(now, backend=backend)
    pruned_presence = _prune_dead_presence(now, backend=backend)
    pruned_health = _prune_dead_health(now, backend=backend)
    # Continuity checkpoint sweep: threads the SAME `deadline` through so the
    # recursive walk + deletes compose with reconcile's budget instead of
    # overrunning it. Best-effort; never raises into this pass.
    pruned_continuity = _prune_continuity_checkpoints(
        now, backend=backend, deadline=deadline)
    # Event-log sweep (Root cause B): bound the unbounded events/tasks/ growth —
    # window-prune live tasks below their latest snapshot, GC orphaned archived
    # task shard trees. Threads the SAME `deadline` so it composes with the
    # budget; best-effort, never raises into this pass.
    pruned_events = _prune_event_log(
        all_tasks, now, backend=backend, deadline=deadline)
    # Provenance-sidecar sweep (root cause A leftover): delete orphaned LOCAL
    # `<key>.prov.json` sidecars whose task is no longer in the live set, bounding
    # the unbounded meta/ growth. Threads the SAME `deadline` so it composes with
    # the budget; LOCAL files (Path.unlink, not remote.delete); best-effort, never
    # raises into this pass.
    pruned_provenance = _prune_provenance_sidecars(all_tasks, deadline=deadline)
    return {"archived": archived, "deferred": deferred,
            "expired_broadcasts": expired_broadcasts,
            "closed_messages": closed_messages,
            "pruned_markers": pruned_markers, "pruned_presence": pruned_presence,
            "pruned_health": pruned_health,
            "pruned_continuity": pruned_continuity,
            "pruned_events": pruned_events,
            "pruned_provenance": pruned_provenance,
            # The marker this run just stamped — threaded to the health record
            # so the tick never re-downloads retention/last-run.json (#6).
            "retention_marker": marker}



# ---------------------------------------------------------------------------
# Archive query / restore commands (search --archived, restore)
# ---------------------------------------------------------------------------

def cmd_search(args: Any, backend: Optional[list[str]] = None) -> int:
    """Search tasks by text across title, summary, tags."""
    query = args.query
    out_format = getattr(args, "format", "table")

    idx = cache.read_cached_view("search-index")
    if idx:
        records = idx.get("records", [])
        q = query.lower()
        results = []
        for r in records:
            text = " ".join([
                r.get("title", ""),
                r.get("summary", ""),
                r.get("workstream", ""),
                r.get("owner_agent", ""),
                " ".join(r.get("tags", [])),
            ]).lower()
            if q in text:
                results.append(r)
    else:
        # No cached search-index — search the summaries aggregate. search_tasks
        # reads title/current_summary/workstream/owner_agent/tags, all present on
        # a summary; no task body fetch. Falls back to a full load on an older bus.
        all_tasks = _load_task_summaries(backend=backend)
        results = views.search_tasks(query, all_tasks)

    # --archived (alias --all): additionally scan the cold archive index shards.
    # Default search stays hot-only (fast); the archive is O(archived) and paid
    # only when explicitly requested. Matches on the same fields as hot search.
    if getattr(args, "archived", False):
        q = query.lower()
        seen = {r.get("id") for r in results}
        for shard in _list_index_shards(backend=backend):
            if shard.get("id") in seen:
                continue
            text = " ".join([shard.get("title", ""), shard.get("workstream", ""),
                             shard.get("owner_agent", "")]).lower()
            if q in text:
                results.append({
                    "id": shard.get("id", ""), "title": shard.get("title", ""),
                    "status": shard.get("status", ""), "priority": "",
                    "workstream": shard.get("workstream", ""),
                    "owner_agent": shard.get("owner_agent", ""),
                    "archived": True, "archive_path": shard.get("archive_path", ""),
                })

    if out_format == "json":
        _print_json({"query": query, "count": len(results), "results": results})
        return 0

    if not results:
        _info(f"No tasks found matching {query!r}.")
        return 0

    _info(f"\n{len(results)} task(s) matching {query!r}:\n")
    for r in results:
        status = r.get("status", "?")
        task_id = r.get("id", "?")
        title = r.get("title", "")[:60]
        priority = r.get("priority", "??")
        print(f"  [{status}] [{priority}] {task_id[:28]}  {title}")
        # Search results may come from cached search-index ("summary") or
        # from task_summary() dicts ("current_summary") — handle both.
        summary_text = (r.get("summary") or r.get("current_summary") or "").strip()
        if summary_text:
            print(f"          {summary_text[:80]}")
    print()
    return 0


def cmd_restore(args: Any, backend: Optional[list[str]] = None) -> int:
    """Restore a cold-archived task back into the hot path — and make it STICK.

    Reverses _archive_task: reads the task's archive/index/<id>.json shard for
    its archive_path, downloads the archived body, stamps ``restored_at``,
    uploads it back to tasks/<id>.json, VERIFIES the hot copy landed READABLE
    (a download, never a bare stat — the hot path is tombstoned from the
    original archive move, so a stat answers from version history even when
    the upload never landed), then deletes the ARCHIVE BODY and finally the
    index shard. The NEXT reconcile
    re-includes it in views (the body is back in the tasks/ listing the
    self-heal enumerates). Nothing is one-way. NOTE this is a bus-level MOVE,
    independent of the platform 'fulcra file restore' (which restores a deleted
    file's prior VERSION by UUID); archived tasks were moved, not deleted, so
    we move them back ourselves.

    F7 (2026-06-11 wave) — two holes made restore SILENTLY UNDO ITSELF within
    ~24h unless the operator also transitioned the task:

      * The ARCHIVE BODY was left in place, so the next daily retention pass
        saw archive_exists=True on the still-terminal task, skipped the upload,
        and DELETED the hot copy again — re-archiving the restore. The cold
        copy is now deleted (after the hot verify — _archive_task's no-loss
        ordering, mirrored in reverse: the irreversible delete runs strictly
        after a positive read-back of the destination).
      * The task remained terminal+AGED (it ages from done_at), so even with
        the body deleted it re-qualified instantly. ``restored_at`` is stamped
        on the hot body and ``views.is_archivable_task`` ages from
        max(done_at, restored_at) — the operator gets a FULL fresh retention
        window before the task can cold-store again.

    ORDERING / FAILURE DISCIPLINE: hot upload -> verify -> delete archive body
    -> delete index shard. A failed/uncertain archive-body delete ERRORS OUT
    with the shard KEPT: deleting the shard while a stale cold copy lingers
    would let a later idempotent _archive_task resurrect the STALE body over
    post-restore edits (archive_exists skips the fresh upload). With the shard
    kept, the operator simply re-runs restore — the hot copy is already in
    place, so the retry only finishes the cold-side cleanup. A crash between
    the two deletes leaves shard-without-body, which a retry reports loudly
    (body missing) while the restored hot copy keeps working."""
    tid = args.task_id
    shard = _read_index_shard(tid, backend=backend)
    if not shard:
        _err(f"No archived task {tid!r} (no archive/index/{tid}.json shard).")
        return 1
    archive_path = shard.get("archive_path") or remote.archive_task_path(tid, "")
    body = remote.download_json(archive_path, backend=backend)
    if not body:
        _err(f"Archived body for {tid!r} not found at {archive_path}.")
        return 1
    # Stamp the restore moment BEFORE the upload so the hot body carries it
    # atomically — a crash right after the upload still leaves a body that
    # ages from the restore, never one the next pass re-archives instantly.
    body["restored_at"] = _now_iso()
    task_path = remote.task_remote_path(tid)
    if not remote.upload_json(body, task_path, backend=backend):
        _err(f"Failed to restore body for {tid!r}.")
        return 1
    # VERIFY the hot body is READABLE — never a bare stat. The hot path was
    # soft-deleted by the original archive move, so its tombstone answers stat
    # from version history regardless of whether the upload above landed; a
    # stat-based verify is vacuous there, and the archive-body delete below
    # would then destroy the ONLY readable copy (the mirror of _archive_task's
    # readable-body verify; one extra download per restore is nothing).
    if remote.download_json(task_path, backend=backend) is None:
        _err(f"Restore of {tid!r} did not verify (hot body unreadable); "
             "archive body and index shard left intact.")
        return 1
    # Hot copy verifiably landed — only now is the cold-side delete safe.
    try:
        body_deleted = remote.delete(archive_path, backend=backend)
    except Exception:
        body_deleted = False
    if not body_deleted:
        _err(f"Restored {tid} to {task_path}, but the archive body at "
             f"{archive_path} could not be deleted — index shard kept; re-run "
             "restore to finish the move (otherwise the next retention pass "
             "would re-archive the stale cold copy).")
        return 1
    remote.delete(remote.archive_index_path(tid), backend=backend)
    _info(f"Restored {tid} to {task_path}. Run reconcile to re-incorporate into views.")
    return 0
