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
# Operation markers (partial upload / needs_reconcile)
# ---------------------------------------------------------------------------

def write_op_marker(op_id: str, data: dict[str, Any]) -> Path:
    ensure_dirs()
    path = ops_dir() / f"OP-{op_id}.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def read_op_marker(op_id: str) -> Optional[dict[str, Any]]:
    path = ops_dir() / f"OP-{op_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


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

def append_ops_log(entry: dict[str, Any]) -> None:
    ensure_dirs()
    entry.setdefault("logged_at", _now_iso())
    with ops_log_path().open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
