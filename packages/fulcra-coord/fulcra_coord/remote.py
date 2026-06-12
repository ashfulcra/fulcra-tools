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
# ``cli_base_cmd``, ``_read_timeout``, ``_write_timeout``, ``_parse_stat``) —
# because other coord modules reach for them via ``remote.<name>`` (e.g.
# ``annotations.py`` uses ``remote.cli_base_cmd()`` and
# ``remote._write_timeout()``) and tests patch them on this module.
#
# NB: ``list_json`` is deliberately NOT re-exported from the store — it is
# re-implemented below as a thin wrapper, see that function for why.
from fulcra_coord_files.store import (  # noqa: F401  (re-exported for callers + test patch surface)
    _backend_cmd,
    _parse_stat,
    _read_timeout,
    _write_timeout,
    check_cli_available,
    check_file_commands,
    cli_base_cmd,
    delete,
    download,
    download_json,
    list_files,
    # serialize_json is the wire serialization upload_json sends; the
    # writepipe/reconcile skip-unchanged view fingerprint MUST hash this exact
    # serialization (see store.serialize_json for the drift rationale).
    serialize_json,
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
    return list_json_checked(prefix, backend=backend, suffix=suffix,
                             max_workers=max_workers)[0]


def list_json_checked(
    prefix: str,
    *,
    backend: Optional[list[str]] = None,
    suffix: str = ".json",
    max_workers: int = 8,
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """``list_json`` plus a COMPLETENESS verdict: ``(items, complete)``.

    2026-06-11 roles/presence read-error audit (F4/F5): ``list_json``'s
    per-item isolation is deliberately silent — a record whose individual
    download 504s is simply DROPPED. That is the right contract for prunes and
    glance surfaces, but it is how a live reviewer's one failed presence read
    became "absent from the roster" (a reconcile then uploaded the survivors
    as the authoritative aggregate) and how one failed lease listing folded a
    HELD role to VACANT. Consumers whose result feeds a DECISION need to know
    the enumeration was partial; this opt-in variant carries that verdict so
    the many existing ``list_json`` callers keep their contract untouched.

    ``complete`` is False when the listing itself raised OR when any listed
    path's download failed/parsed to a non-dict (a corrupt record is a read
    problem too — fail toward "don't trust this enumeration"). CAVEAT a
    decision-making caller must handle: ``list_files`` swallows transport
    failures into ``[]``, so an EMPTY-and-"complete" result is only
    trustworthy after ``probe_reachable`` confirms the bus answered — the
    probe is the caller's to spend, on the empty path only (read_leases is
    the reference caller)."""
    try:
        paths = [p for p in list_files(prefix, backend=backend) if p.endswith(suffix)]
    except Exception:
        return [], False
    if not paths:
        return [], True
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
    items = [(path, results[path]) for path in paths if path in results]
    return items, len(items) == len(paths)


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


def events_prefix(task_id: str) -> str:
    """List prefix for all event shards belonging to *task_id*.

    Every shard under this prefix is an immutable append: one file per event,
    keyed by ``event_id``, written by ``eventlog.append_event``.  There is no
    shared mutable index under this prefix, so concurrent writers never clobber
    each other's shards (the no-CAS-safe per-file pattern from archive_index).
    """
    return f"{remote_root()}/events/tasks/{task_id}/"


def event_remote_path(task_id: str, event_id: str) -> str:
    """Full remote path for one immutable event shard.

    Path is ``{events_prefix(task_id)}{event_id}.json``.  Because the
    ``event_id`` encodes a microsecond timestamp + random suffix, two concurrent
    writers for the same task will always land on distinct paths — no last-write-
    wins risk even without compare-and-swap on the store.
    """
    return f"{events_prefix(task_id)}{event_id}.json"


def view_remote_path(name: str) -> str:
    """name: index, active, next, recently-done, search-index"""
    if name == "index":
        return f"{remote_root()}/index.json"
    return f"{remote_root()}/views/{name}.json"


def workstream_remote_path(workstream: str) -> str:
    return f"{remote_root()}/workstreams/{workstream}.json"


# (agent_remote_path — f"{remote_root()}/agents/{agent}.json" — was removed in
# the 2026-06-11 perf wave together with the per-agent views it addressed:
# materialized on every write/reconcile, downloaded by nothing. Existing remote
# files under agents/ are inert; bus-state cleanup is deferred pending a Fulcra
# service review.)


def presence_remote_path(agent_slug: str) -> str:
    """Per-agent presence record path. Takes an ALREADY-SLUGGED agent id (via
    views.agent_slug) so the colons in a raw ``kind:host:repo`` id never reach a
    filename — mirroring how the index's inbox counts are keyed by slug. Only
    that agent writes this file, so there is zero cross-agent write contention."""
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


# ---------------------------------------------------------------------------
# Directive path helpers (the directive dual-write's storage tree)
# ---------------------------------------------------------------------------

def directives_prefix() -> str:
    """List prefix for all first-class directive records.

    WHY a dedicated top-level prefix (not ``tasks/directives/``): directives
    have a distinct schema, lifecycle, and retention policy from tasks. Keeping
    them at ``{root}/directives/`` makes it possible to list, scan, and prune
    them independently without touching the task tree — and avoids the path
    ambiguity that would arise if a task id and a directive id ever collided
    under a shared prefix.
    """
    return f"{remote_root()}/directives/"


def directive_remote_path(directive_id: str) -> str:
    """Canonical storage path for a single directive record.

    Mirrors ``task_remote_path`` / ``presence_remote_path`` in structure:
    one file per record, keyed by id, under the directives prefix. Only the
    issuing agent writes this file (the directive dual-write), so there is no
    cross-agent write contention — the same per-entity pattern used for
    presence and agent views.
    """
    return f"{directives_prefix()}{directive_id}.json"


# ---------------------------------------------------------------------------
# Directive SUB-LOG path helpers (append-only ack + routing)
# ---------------------------------------------------------------------------
#
# THE CONCURRENCY CRUX: the bus is a brokerless object store with NO compare-and-
# swap and many concurrent writers. A read-modify-write of the single
# ``directives/<id>.json`` record to ADD an ack would CLOBBER concurrent acks —
# two agents acking the same broadcast at once each read the old record, add only
# their own ack, and the slower upload overwrites (loses) the faster one's.
#
# So acks/routing live in an APPEND-ONLY SUB-LOG under the directive, where every
# writer writes a DISTINCT file — no shared mutable file, no clobber, exactly the
# pattern the event log uses:
#   * ack sub-log:     one file PER ACKING AGENT  -> ``<id>/acks/<agent-slug>.json``
#     (an agent re-acking overwrites only its OWN file = idempotent; two agents
#     never collide; the ack UNION = list the prefix).
#   * routing sub-log: append-only route-event shards -> ``<id>/routing/<event_id>.json``.


def _filename_slug(text: str) -> str:
    """Filename-safe slug for an arbitrary id used as a sub-log basename.

    Agent ids look like ``claude-code:Mac:repo`` — the colons/slashes are not
    portable as path segments, so collapse every non-[a-z0-9-_.] run to a single
    ``-`` and lowercase (so two ids differing only in case can't fork into two
    files). Mirrors ``schema._slugify`` / ``views.agent_slug`` semantics; kept
    LOCAL here so the low-layer ``remote`` module needn't import ``schema`` just
    for a slug. Falls back to ``id`` for an all-punctuation input (never empty,
    which would alias every such id onto one file)."""
    s = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(text).lower())
    return s.strip("-") or "id"


def directive_acks_prefix(directive_id: str) -> str:
    """List prefix for a directive's per-agent ack files (the ack union = list it)."""
    return f"{directives_prefix()}{directive_id}/acks/"


def directive_ack_path(directive_id: str, agent: str) -> str:
    """Storage path for ONE agent's ack of a directive.

    One file per acking agent (keyed by the slugified agent id) so an agent
    re-acking overwrites only its OWN file (idempotent) and two different agents
    NEVER collide — the property that makes 'one agent acking a broadcast must not
    clear it for others' true BY CONSTRUCTION (no shared mutable record)."""
    return f"{directive_acks_prefix(directive_id)}{_filename_slug(agent)}.json"


def directive_routing_prefix(directive_id: str) -> str:
    """List prefix for a directive's append-only route-event shards."""
    return f"{directives_prefix()}{directive_id}/routing/"


def directive_route_path(directive_id: str, event_id: str) -> str:
    """Storage path for ONE route-event shard, keyed by a unique event id.

    Append-only: each route decision lands as its own shard (like the event log),
    so concurrent re-routes never overwrite one another. The caller supplies a
    unique ``event_id`` (the route event's ``event_id``/``route_id`` or a fresh
    uuid) — distinct ids => distinct files => no clobber."""
    return f"{directive_routing_prefix(directive_id)}{event_id}.json"


def directive_responses_prefix(directive_id: str) -> str:
    """Prefix of a directive's RESPONSE sub-log (the loop return leg). One file
    per response event — append-only shards, same clobber-safety rationale as
    the routing sub-log."""
    return f"{directives_prefix()}{directive_id}/responses/"


def directive_response_path(directive_id: str, event_id: str) -> str:
    return f"{directive_responses_prefix(directive_id)}{event_id}.json"


def directive_evidence_prefix(directive_id: str) -> str:
    """Prefix of a directive's EVIDENCE sub-log — the third sub-log, holding
    forge-MIRRORED signals (a PR merge, a forge review verdict). One file per
    mirrored event — append-only shards, same clobber-safety rationale as the
    responses sub-log. Consumed ONLY by detection (out-of-band flags on the
    loop board); the closure fold never reads this prefix — mirrored evidence
    must never close a loop (closure is bus-response-only)."""
    return f"{directives_prefix()}{directive_id}/evidence/"


def directive_evidence_path(directive_id: str, event_id: str) -> str:
    return f"{directive_evidence_prefix(directive_id)}{event_id}.json"


# ---------------------------------------------------------------------------
# Role path helpers (roles-as-durable-identity, spec 2026-06-10)
# ---------------------------------------------------------------------------
#
# THE INVERSION: the ROLE is the durable identity; a session is an ephemeral
# lease on it. The registry record (``roles/<name>.json``) is operator data —
# what the role is for, its standing instructions, vacancy SLA, maintainer.
# Leases live in a per-agent SUB-LOG under the role, exactly like the
# directive ack sub-log: one file PER CLAIMING AGENT, so an agent re-claiming
# overwrites only its OWN lease (idempotent refresh) and two agents claiming
# the same role NEVER collide — no shared mutable holder list, no clobber on
# the CAS-less bus. Lease FRESHNESS is not stored here at all: a lease is
# fresh iff its holder's presence is fresh (no new heartbeat machinery).


def roles_prefix() -> str:
    """List prefix for the role registry. Holds top-level ``<name>.json``
    registry records BESIDE per-role subtrees (``<name>/leases/``,
    ``<name>/escalations/``) — listings must apply the same top-level-only
    filter the directives prefix needs (see role_ops.list_roles)."""
    return f"{remote_root()}/roles/"


def role_record_path(name: str) -> str:
    """Canonical storage path for one role's registry record. Keyed by the
    slugified role name (role names are operator-typed strings; the slug keeps
    arbitrary input portable as a path segment, mirroring the ack sub-log)."""
    return f"{roles_prefix()}{_filename_slug(name)}.json"


def role_leases_prefix(name: str) -> str:
    """List prefix for one role's per-agent lease files (the holder union =
    list it, exactly like the ack union)."""
    return f"{roles_prefix()}{_filename_slug(name)}/leases/"


def role_lease_path(name: str, agent: str) -> str:
    """Storage path for ONE agent's lease on a role.

    One file per claiming agent (keyed by the slugified agent id) so a
    re-claim overwrites only that agent's OWN lease and two different agents
    never collide — 'claiming a shared role must not evict the other holders'
    is true BY CONSTRUCTION (the directive_ack_path property)."""
    return f"{role_leases_prefix(name)}{_filename_slug(agent)}.json"


def role_escalation_marker_path(name: str, day: str) -> str:
    """First-writer-wins DAILY vacancy-escalation marker for one role.

    Keyed by the UTC date (``YYYY-MM-DD``) so a role vacant past its SLA
    escalates to its maintainer ONCE PER DAY, not once per reconcile tick —
    the digest-marker dedup pattern (_claim_digest_marker), applied per role.
    Lives under the role's subtree so the top-level registry filter already
    excludes it from listings."""
    return f"{roles_prefix()}{_filename_slug(name)}/escalations/{day}.json"


def version_manifest_path() -> str:
    """Canonical version-manifest record (``runtime/version.json``) — the bus's
    version POINTER (spec 2026-06-08 safety boundary: the bus says WHICH version
    is canonical, never WHAT to run). Written only by the maintainer's
    ``announce-version`` at release time; read by every session-start /
    listener-tick self-update check. One well-known mutable file (like the
    views): last-writer-wins is correct here because only the maintainer
    writes it and a newer announce SHOULD supersede an older one."""
    return f"{remote_root()}/runtime/version.json"


def health_remote_path(host_slug: str) -> str:
    """Per-host self-reported health record path. Takes an ALREADY-SLUGGED id
    (views.agent_slug). Only that host writes its own file -> zero cross-host
    write contention (the no-CAS-safe per-file pattern, same as presence)."""
    return f"{remote_root()}/health/{host_slug}.json"


def health_prefix() -> str:
    """List prefix for per-host health records (health command + retention prune)."""
    return f"{remote_root()}/health/"
