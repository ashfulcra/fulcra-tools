"""Fulcra coordination-bus remote layer.

This module is now split into two concerns:

  * **Transport** (put/get/stat/list/delete of immutable blobs) lives in the
    standalone ``fulcra_coord_files`` package and is RE-EXPORTED here unchanged.
    The re-export is load-bearing back-compat: every coord module imports these
    as ``remote.<name>``, and the test suite patches them as
    ``fulcra_coord.remote.<name>`` (``monkeypatch.setattr(remote, "upload_json",
    ...)``). Re-exported names are real module attributes, so both keep working.
    See ``fulcra_coord_files.store`` for the full NO-CAS contract.

  * **Path layout** (which ``remote_root()``-anchored path a given record lives
    at) STAYS here, below, because it is coordination-bus policy that depends on
    ``remote_root()`` from ``fulcra_coord.__init__`` — not transport.

Backend resolution, timeouts, and I/O semantics are documented on the transport
in ``fulcra_coord_files.store``; nothing about them changed in the extraction.
"""

from __future__ import annotations

import concurrent.futures

# ``subprocess`` is imported (and not used directly here) purely so that
# ``fulcra_coord.remote.subprocess`` resolves: several tests patch the transport
# at the syscall boundary via ``mock.patch("fulcra_coord.remote.subprocess.run")``.
# Because ``subprocess`` is a process-global singleton module, patching
# ``remote.subprocess.run`` patches the SAME module object that
# ``fulcra_coord_files.store`` calls through — so the re-exported leaf transport
# (``list_files``, ``check_cli_available`` …) observes the mock unchanged.
import subprocess  # noqa: F401  — re-exported as a test patch point
from typing import Any, Optional

from . import remote_root

# Re-export the full transport surface from the extracted package. ALL moved
# names are bound here — including the private helpers (``_backend_cmd``,
# ``cli_base_cmd``, ``_read_timeout``, ``_write_timeout``, ``_reconcile_timeout``,
# ``_parse_stat``) — because other coord modules reach for them via
# ``remote.<name>`` (e.g. ``annotations.py`` uses ``remote.cli_base_cmd()`` and
# ``remote._write_timeout()``) and tests patch them on this module.
#
# NB: ``list_json`` is deliberately NOT re-exported from the store — it is
# re-implemented below as a thin wrapper, see that function for why.
from fulcra_coord_files.store import (  # noqa: F401  (re-exported for callers + test patch surface)
    _backend_cmd,
    _parse_stat,
    _read_timeout,
    _reconcile_timeout,
    _write_timeout,
    check_cli_available,
    check_file_commands,
    cli_base_cmd,
    delete,
    download,
    download_json,
    list_files,
    stat,
    stat_changed,
    upload,
    upload_json,
)
from fulcra_coord_files.store import (
    check_remote_access as _store_check_remote_access,
)
from fulcra_coord_files.store import (
    probe_reachable as _store_probe_reachable,
)


# ---------------------------------------------------------------------------
# Composite transport that must dispatch through THIS module's bindings
# ---------------------------------------------------------------------------

def list_json(
    prefix: str,
    *,
    backend: Optional[list[str]] = None,
    suffix: str = ".json",
    max_workers: int = 8,
) -> list[tuple[str, dict[str, Any]]]:
    """List ``prefix`` and PARALLEL-download every file ending in ``suffix``,
    returning ``[(path, record), ...]`` for each path whose JSON parsed to a dict.

    Behaviorally identical to ``fulcra_coord_files.store.list_json`` (same
    ordering, dict-guard, best-effort isolation). It is re-implemented HERE rather
    than re-exported for ONE reason: it is the only transport primitive that
    composes other transport primitives (``list_files`` + ``download_json``), and
    every existing consumer + test patches those callees on the ``remote`` module
    (``mock.patch("fulcra_coord.remote.list_files", ...)`` / ``...download_json``)
    and expects ``list_json`` to honour the patch. A re-export of the store's
    ``list_json`` would close over the store's OWN ``list_files``/``download_json``,
    so a ``remote``-level patch wouldn't reach it. Resolving the callees from this
    module's globals (``list_files(...)`` / ``download_json(...)``) preserves that
    patch surface exactly. See the store version for the full contract docstring.
    """
    try:
        paths = [p for p in list_files(prefix, backend=backend) if p.endswith(suffix)]
    except Exception:
        return []
    if not paths:
        return []
    results: dict[str, dict[str, Any]] = {}
    workers = min(max_workers, max(2, len(paths)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(download_json, path, backend=backend): path for path in paths
        }
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                rec = fut.result()
            except Exception:
                rec = None
            if isinstance(rec, dict):
                results[path] = rec
    return [(path, results[path]) for path in paths if path in results]


# ---------------------------------------------------------------------------
# Transport calls that need the bus's remote_root() bound in
# ---------------------------------------------------------------------------
# ``probe_reachable`` and ``check_remote_access`` are transport, but their target
# path is coordination-bus policy (``remote_root()``). We keep the transport free
# of that policy — otherwise it would import back into ``fulcra_coord`` and create
# a coord -> files -> coord cycle — by injecting ``remote_root()`` here. Behavior
# is identical to the pre-extraction functions for every caller, who reaches them
# as ``remote.probe_reachable(backend=...)`` / ``remote.check_remote_access(...)``.

def probe_reachable(backend: Optional[list[str]] = None) -> bool:
    """Cheap liveness probe against the coordination root. See
    :func:`fulcra_coord_files.store.probe_reachable` for the full rationale
    (disambiguates "empty but reachable" from "unreachable"). Binds the bus's
    ``remote_root()`` as the list target."""
    return _store_probe_reachable(backend, root=remote_root())


def check_remote_access(backend: Optional[list[str]] = None) -> tuple[bool, str]:
    """Verify remote access by stat-ing ``{remote_root()}/index.json``. See
    :func:`fulcra_coord_files.store.check_remote_access`. Binds the bus's
    well-known index path as the probe target."""
    return _store_check_remote_access(backend, probe_path=f"{remote_root()}/index.json")


# ---------------------------------------------------------------------------
# Remote path helpers
# ---------------------------------------------------------------------------

def task_remote_path(task_id: str) -> str:
    return f"{remote_root()}/tasks/{task_id}.json"


def view_remote_path(name: str) -> str:
    """name: index, active, next, recently-done, search-index"""
    if name == "index":
        return f"{remote_root()}/index.json"
    return f"{remote_root()}/views/{name}.json"


def workstream_remote_path(workstream: str) -> str:
    return f"{remote_root()}/workstreams/{workstream}.json"


def agent_remote_path(agent: str) -> str:
    return f"{remote_root()}/agents/{agent}.json"


def presence_remote_path(agent_slug: str) -> str:
    """Per-agent presence record path. Takes an ALREADY-SLUGGED agent id (via
    views.agent_slug) so the colons in a raw ``kind:host:repo`` id never reach a
    filename — mirroring how the inbox views are keyed by slug. Only that agent
    writes this file, so there is zero cross-agent write contention."""
    return f"{remote_root()}/presence/{agent_slug}.json"


def presence_view_path() -> str:
    """The aggregate presence roster (``views/presence.json``) — the one file the
    read commands (presence/agents/resume) load, rebuilt by reconcile and
    refreshed opportunistically on connect. Mirrors view_remote_path's layout."""
    return f"{remote_root()}/views/presence.json"


def archive_task_path(task_id: str, month: str) -> str:
    """Cold-archive body path: archive/tasks/<YYYY-MM>/<id>.json. Month is the
    done/abandoned month, so the archive is browsable by when work finished."""
    return f"{remote_root()}/archive/tasks/{month}/{task_id}.json"


def archive_index_path(task_id: str) -> str:
    """Per-id cold-index SHARD path. Append-only, one distinct path per task —
    NO shared archive/index.json, because Files has no CAS and a shared mutable
    index would let concurrent archivers clobber each other's appends."""
    return f"{remote_root()}/archive/index/{task_id}.json"


def archive_index_prefix() -> str:
    """List prefix for the cold-index shards (search --archived, restore lookup)."""
    return f"{remote_root()}/archive/index/"


def retention_marker_path(now: Any) -> str:
    """First-host-wins daily throttle marker. ONE path per day (date is INSIDE
    the JSON, not the filename) so today's run reads a stable path and any host
    claims the SAME file — the digest-marker first-writer-wins pattern, but a
    single rolling file rather than per-window."""
    return f"{remote_root()}/retention/last-run.json"


def digest_markers_prefix() -> str:
    """List prefix for digest dedup markers (marker prune)."""
    return f"{remote_root()}/digest/markers/"


def presence_prefix() -> str:
    """List prefix for per-agent presence records (dead-presence prune)."""
    return f"{remote_root()}/presence/"


def health_remote_path(host_slug: str) -> str:
    """Per-host self-reported health record path. Takes an ALREADY-SLUGGED id
    (views.agent_slug). Only that host writes its own file -> zero cross-host
    write contention (the no-CAS-safe per-file pattern, same as presence)."""
    return f"{remote_root()}/health/{host_slug}.json"


def health_prefix() -> str:
    """List prefix for per-host health records (health command + retention prune)."""
    return f"{remote_root()}/health/"
