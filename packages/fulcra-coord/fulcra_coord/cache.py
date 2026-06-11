"""Local cache management for fulcra-coord.

Cache layout under ${XDG_CACHE_HOME:-~/.cache}/fulcra-coord/:
  roots/<root-slug>/         — per-remote-root cache (isolated; no cross-root bleed)
    tasks/TASK-*.json        — cached task files for this root
    views/                   — cached view files (index, active, next, etc.)
    meta/                    — last-known remote stat metadata (keyed by path hash)
    ops/                     — in-flight operation markers
  sessions/                  — session->task pointers (GLOBAL; keyed by session id)
  ops.log                    — local JSONL ops log (global)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

# The bus timestamp convention lives in ONE place — timeutil (a pure stdlib
# leaf, so this adds no upward dependency for low-layer cache). Bound under
# the historical local name: the ops-log writer below and the mixed-precision
# format pin (test_emitted_timestamps_always_have_six_digit_microseconds)
# reach it as ``cache._now_iso``.
from .timeutil import now_iso as _now_iso


def cache_root() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "fulcra-coord"


def _root_slug() -> str:
    """Filesystem-safe slug of the current FULCRA_COORD_REMOTE_ROOT.

    The task/view/meta/ops caches are scoped under this so the local cache is
    isolated per remote root. Without it, tasks and views from one root bleed
    into another's `status`/`reconcile` — e.g. a `/coordination-demo` seed
    contaminating the production `/coordination` views on the same machine.
    Mirrors the root resolution in fulcra_coord.remote_root (env read directly
    here to avoid an import cycle).
    """
    root = os.environ.get("FULCRA_COORD_REMOTE_ROOT", "/coordination").strip()
    root = (root or "/coordination").strip("/")
    slug = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in root)
    return slug or "coordination"


def _root_cache() -> Path:
    return cache_root() / "roots" / _root_slug()


def tasks_dir() -> Path:
    return _root_cache() / "tasks"


def views_dir() -> Path:
    return _root_cache() / "views"


def meta_dir() -> Path:
    return _root_cache() / "meta"


def ops_dir() -> Path:
    return _root_cache() / "ops"


def annotations_dir() -> Path:
    """Per-root store of lifecycle-annotation idempotency markers.

    Kept OUT of the task JSON on purpose: the task file is the shared,
    merge-sensitive coordination artifact, and bolting a per-machine "have I
    annotated this yet" flag onto it would (a) pollute the cross-agent payload
    and (b) get tangled in the merge logic. The marker is a purely local concern
    — "did THIS machine already emit an annotation for THIS transition" — so it
    lives in the local cache alongside op markers, scoped per remote root."""
    return _root_cache() / "annotations"


def fallback_throttle_path() -> Path:
    """The per-host direct-listing-fallback throttle marker (the 2026-06-11
    stampede breaker — see ``io._load_task_summaries``).

    PLACEMENT — per HOST, per remote ROOT, deliberately:

    * Per host: the XDG cache root is keyed by OS user + machine, never by
      agent identity (nothing under ``cache_root()`` is identity-scoped), so
      every listener/process this user runs on this host — the operator's Mac
      runs EIGHT — shares ONE marker. That is the whole point: the stampede
      is a per-host phenomenon (one host saturating the gateway with its own
      concurrent fallbacks), so the claim must be host-wide. Caveat: listeners
      running as DIFFERENT OS users (or with divergent ``XDG_CACHE_HOME``)
      would each get their own marker — acceptable; the fleet here runs one
      user per host.
    * Per remote root (under ``roots/<slug>``): throttling one bus's fallback
      must never gate a different bus on the same machine — the staleness
      being repaired is per-bus state."""
    return _root_cache() / "fallback-throttle.json"


def sessions_dir() -> Path:
    # Intentionally GLOBAL (not per-root): session pointers are keyed by the
    # globally-unique CLAUDE_CODE_SESSION_ID / FULCRA_COORD_SESSION_KEY and
    # carry their own root, so a lifecycle hook can resolve a session's task
    # regardless of which root env it runs under.
    d = cache_root() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ops_log_path() -> Path:
    return cache_root() / "ops.log"


def ensure_dirs() -> None:
    for d in (tasks_dir(), views_dir(), meta_dir(), ops_dir()):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Annotation idempotency markers
# ---------------------------------------------------------------------------

def _annotation_marker_path(key: str) -> Path:
    import hashlib
    digest = hashlib.sha1(key.encode()).hexdigest()[:24]
    return annotations_dir() / f"ANN-{digest}"


def has_annotation_marker(key: str) -> bool:
    """True if an annotation has already been emitted for `key`.

    `key` encodes (task_id, lifecycle, transition-anchor) so a write-retry of the
    same transition is recognized as already-done and not re-annotated."""
    return _annotation_marker_path(key).exists()


def write_annotation_marker(key: str) -> None:
    """Record that an annotation for `key` was emitted. Touch-only (empty file);
    presence is the whole signal. Best-effort: a failure here just means a
    possible duplicate annotation later, never a failed task op."""
    try:
        annotations_dir().mkdir(parents=True, exist_ok=True)
        _annotation_marker_path(key).write_text("")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Cache read/write
# ---------------------------------------------------------------------------

def read_cached_task(task_id: str) -> Optional[dict[str, Any]]:
    path = tasks_dir() / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_cached_task(task: dict[str, Any]) -> None:
    ensure_dirs()
    path = tasks_dir() / f"{task['id']}.json"
    path.write_text(json.dumps(task, indent=2))


def delete_cached_task(task_id: str) -> None:
    """Evict a task from the local cache. Best-effort and idempotent (a missing
    entry is a no-op). Used by retention's archive MOVE: once a terminal task's
    body has left the remote tasks/ tree, the archiving host MUST also drop its
    local copy. Otherwise _load_all_tasks — which seeds task_map from
    list_cached_tasks() and only ever ADDS remote ids — would keep re-including
    the archived task and rebuild it straight back into the authoritative
    summaries.json/views (resurrecting it fleet-wide), the exact hot-path
    exclusion the move exists to achieve."""
    path = tasks_dir() / f"{task_id}.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_cached_view(name: str) -> Optional[dict[str, Any]]:
    """name e.g. 'index', 'active', 'next', 'recently-done', 'search-index'"""
    path = views_dir() / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_cached_view(name: str, data: dict[str, Any]) -> None:
    ensure_dirs()
    path = views_dir() / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def list_cached_tasks() -> list[dict[str, Any]]:
    d = tasks_dir()
    if not d.exists():
        return []
    tasks = []
    for p in sorted(d.glob("TASK-*.json")):
        try:
            tasks.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return tasks


# ---------------------------------------------------------------------------
# View upload fingerprints (skip-unchanged view uploads)
# ---------------------------------------------------------------------------
#
# Per view name, the sha256 hexdigest of the view content as LAST CONFIRMED
# uploaded (writepipe._view_fingerprint defines the exact digest). The write
# path and reconcile skip a view upload when the freshly-rebuilt content
# matches this digest — the fix for the all-views fan-out (~55 uploads per
# logical write on the live bus, growing with fleet size) that turned every
# write into a NeedsReconcile burst under backend 504-weather (2026-06-10).
#
# WHY a separate store, and not "compare against the cached view":
# write_cached_view is deliberately written for EVERY view regardless of
# upload success (a failed upload still caches, so local readers see the
# freshest build). "content == cached view" therefore does NOT imply "remote
# is current" — a previous failed upload poisons that inference. This store
# is SUCCESS-ONLY by contract: a fingerprint is written exclusively after a
# confirmed upload, so a digest match here really does mean the bytes are on
# the remote (as far as this host ever observed).
#
# LOCAL-ONLY per-host bookkeeping, like the meta/ stats and prov sidecars:
# never uploaded, scoped per remote root (no cross-root bleed), and safe to
# delete at any time — the only cost of a lost fingerprint is one redundant
# re-upload.

def view_fingerprints_dir() -> Path:
    return _root_cache() / "view-fingerprints"


def _view_fingerprint_path(view_name: str) -> Path:
    # View names carry "/" (workstreams/ws, agents/<id>) and agent ids carry
    # ":" — sanitize for the filesystem, but keep a short hash of the RAW name
    # so two names that sanitize identically (agents/a:b vs agents/a-b) can
    # never alias onto one file and cross-poison each other's skip decisions.
    import hashlib
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in view_name)
    digest = hashlib.sha1(view_name.encode()).hexdigest()[:8]
    return view_fingerprints_dir() / f"{safe}-{digest}"


def read_view_fingerprint(view_name: str) -> Optional[str]:
    """Digest of `view_name`'s last CONFIRMED upload, or None (never uploaded
    successfully / unreadable). None always means "upload it" — the safe
    direction, mirroring read_meta's corrupt-is-absent stance."""
    path = _view_fingerprint_path(view_name)
    if not path.exists():
        return None
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def write_view_fingerprint(view_name: str, digest: str) -> None:
    """Record `view_name`'s content digest after a CONFIRMED upload.

    Callers must only invoke this on upload success — writing it on failure
    would re-create exactly the poisoned-cache trap this store exists to
    avoid. Best-effort like write_annotation_marker: a failure here just means
    one redundant re-upload on the next write, never a failed task op."""
    try:
        view_fingerprints_dir().mkdir(parents=True, exist_ok=True)
        _view_fingerprint_path(view_name).write_text(digest)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Remote stat metadata (version tracking for optimistic concurrency)
# ---------------------------------------------------------------------------

def read_meta(remote_path: str) -> Optional[dict[str, Any]]:
    key = _meta_key(remote_path)
    path = meta_dir() / f"{key}.stat.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_meta(remote_path: str, stat: dict[str, Any]) -> None:
    ensure_dirs()
    key = _meta_key(remote_path)
    path = meta_dir() / f"{key}.stat.json"
    path.write_text(json.dumps(stat))


def _meta_key(remote_path: str) -> str:
    import hashlib
    return hashlib.sha1(remote_path.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Per-body provenance sidecar (read->write hand-off for events-mode soundness)
# ---------------------------------------------------------------------------
#
# Records, per task_id, WHERE the body the read funnel (io._cache_remote_task)
# last returned came from, plus the fold-at-read base when the body was folded.
# The write path (writepipe._write_task_and_views) consults it to decide whether
# a fold-sourced write must do a 3-way merge against the fold base — recovering
# newer file fields a stale/lagging fold would otherwise silently clobber (root
# cause A2). LOCAL-ONLY: this is a per-machine read->write hand-off, never part
# of the shared task payload and NEVER uploaded to the remote bus. It lives in
# the same hashed-key meta dir as read_meta, in a ``.prov.json`` sidecar.
#
# ``prov`` shape:
#   {"source": "file"|"fold",
#    "file_stat_at_read": <stat dict|None>,
#    "fold_base": <clean folded task dict|None>,
#    "fold_complete": bool}

def _prov_key(task_id: str) -> str:
    import hashlib
    return hashlib.sha1(task_id.encode()).hexdigest()[:16]


def write_provenance(task_id: str, prov: dict[str, Any]) -> None:
    ensure_dirs()
    key = _prov_key(task_id)
    path = meta_dir() / f"{key}.prov.json"
    path.write_text(json.dumps(prov))


def read_provenance(task_id: str) -> Optional[dict[str, Any]]:
    key = _prov_key(task_id)
    path = meta_dir() / f"{key}.prov.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # Mirror read_meta: a corrupt sidecar is treated as absent, not a crash.
        return None


def clear_provenance(task_id: str) -> None:
    """Drop a task's provenance sidecar. Best-effort, idempotent (missing → no-op).

    Called after a successful upload so a later file-sourced write doesn't
    inherit stale fold provenance and force a spurious 3-way merge."""
    key = _prov_key(task_id)
    path = meta_dir() / f"{key}.prov.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Operation markers (partial upload / needs_reconcile)
# ---------------------------------------------------------------------------

def write_op_marker(op_id: str, data: dict[str, Any]) -> Path:
    ensure_dirs()
    path = ops_dir() / f"OP-{op_id}.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def list_op_markers() -> list[dict[str, Any]]:
    d = ops_dir()
    if not d.exists():
        return []
    ops = []
    for p in sorted(d.glob("OP-*.json")):
        try:
            ops.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return ops


def clear_op_marker(op_id: str) -> None:
    path = ops_dir() / f"OP-{op_id}.json"
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Ops log (JSONL)
# ---------------------------------------------------------------------------

def _opslog_max_bytes() -> int:
    """Size ceiling for the current ops-log segment before it rotates to ``.1``.

    Read from ``FULCRA_COORD_OPSLOG_MAX_BYTES`` (default 1_000_000), floored at a
    sane minimum so a tiny/garbage override can't degenerate into rotating on
    every single append (which would churn the disk and, worse, keep only the
    last line — defeating the recent-window read Signal C depends on). A
    non-positive override DISABLES rotation entirely (the unbounded legacy
    behaviour, opt-in), letting an operator turn the feature off if needed."""
    from . import env_int
    raw = env_int("FULCRA_COORD_OPSLOG_MAX_BYTES", 1_000_000)
    if raw <= 0:
        return 0  # disabled
    # Floor so a pathologically small value can't rotate after every line.
    return max(4096, raw)


def _rotate_ops_log_if_needed() -> None:
    """Best-effort single-segment size rotation of the current ops log.

    Called by ``append_ops_log`` AFTER the append has already landed, so a
    rotation failure can never lose the entry that triggered it. When the current
    file exceeds ``_opslog_max_bytes()``, rename it onto the ``.1`` sibling
    (``os.replace`` — atomic, overwrites any prior ``.1``). There is NO shared
    open handle across the rename: every append (via ``append_ops_log`` /
    ``log.log_op``) opens the path fresh in append mode, so the next write simply
    re-creates the current file. That is what makes the rotation lossless.

    Wrapped so it NEVER raises into ``append_ops_log`` — the append is the
    load-bearing operation; bounding the file is strictly secondary."""
    try:
        cap = _opslog_max_bytes()
        if cap <= 0:
            return  # rotation disabled
        path = ops_log_path()
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= cap:
            return
        rotated = path.with_name(path.name + ".1")
        # os.replace is atomic and overwrites an existing .1 (single retained
        # segment — we keep exactly the previous file, not an unbounded archive).
        os.replace(path, rotated)
    except Exception:
        # Best-effort: a rotation failure must never break logging.
        pass


def append_ops_log(entry: dict[str, Any]) -> None:
    ensure_dirs()
    entry.setdefault("logged_at", _now_iso())
    # The append MUST succeed first (and on its own) — it is the load-bearing op.
    with ops_log_path().open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    # THEN bound the file. Best-effort: never raises back into the caller.
    _rotate_ops_log_if_needed()


def read_ops_log(since: Optional["datetime"] = None) -> list[dict[str, Any]]:
    """Read the local JSONL ops log back, best-effort and windowed by ``since``.

    SIGNAL C (dual-write liveness): the dual-write append path records an
    ``event_append_failed`` op on every failed event append, but until now those
    entries were write-only — a host whose dual-write is silently failing left no
    visible trace. This reader lets the health record surface a recent
    append-failure count.

    Best-effort by construction (mirrors ``read_meta``): malformed or blank lines
    are skipped, a missing file returns ``[]``, and nothing here ever raises — a
    corrupt ops log must never break the caller (reconcile). When ``since`` is
    given, only entries whose ``logged_at`` parses AND is ``>= since`` are kept;
    an entry with a missing/unparseable timestamp is dropped from a windowed read
    (we can't prove it falls inside the window, and over-counting a stale failure
    as "recent" would be the misleading outcome this signal exists to avoid).
    """
    path = ops_log_path()
    # Read BOTH segments so the recent window isn't truncated the instant a size
    # rotation happens: ``.1`` holds the just-rotated-out (older) lines and the
    # current file holds the newest. Order them OLDEST-first (``.1`` then current)
    # so the returned list stays chronological — Signal C's recent-window filter
    # and any "last N ops" consumer rely on that. Each segment is independently
    # best-effort: a missing or unreadable segment is simply skipped.
    rotated = path.with_name(path.name + ".1")
    out: list[dict[str, Any]] = []
    for seg in (rotated, path):
        if not seg.exists():
            continue
        try:
            text = seg.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            if since is not None:
                dt = _parse_logged_at(entry.get("logged_at"))
                if dt is None or dt < since:
                    continue
            out.append(entry)
    return out


def _parse_logged_at(value: Any) -> Optional["datetime"]:
    """Parse a ``logged_at`` ISO string (UTC, trailing ``Z``) to an aware dt.

    Returns ``None`` on anything unparseable so a windowed read can safely drop
    the entry rather than raise. Kept local to cache (a low-layer module) so the
    ops-log reader needs no upward import for timestamp parsing."""
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime, timezone
    try:
        # append_ops_log stamps "...Z"; fromisoformat wants +00:00.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
