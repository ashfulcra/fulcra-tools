"""Role registry + lease I/O: claim/release/read and the operator registry CRUD.

The I/O half of roles-as-durable-identity (spec 2026-06-10) — the thin layer
over the pure ``roles.py`` folds, following the loops.py/loop_ops.py split.
Two write surfaces with different authority:

  * **Registry records** (``roles/<name>.json``) are the operator's durable
    role definitions — the AUTHORITATIVE write. ``upsert_role`` verifies
    after write via ``remote.stat`` so a claimed-successful upload that never
    landed reads as failure.
  * **Lease shards** (``roles/<name>/leases/<agent-slug>.json``) are one file
    PER CLAIMING AGENT — a re-claim overwrites only the claimer's OWN file
    (idempotent refresh), two agents never collide, and the holder union =
    list the prefix (the directive ack sub-log pattern; no CAS needed).

Everything here is best-effort never-raise (True/False, record-or-None, [],
or the :data:`READ_ERROR` sentinel where a caller must be able to tell a
failed read apart from confirmed absence — read_role and read_leases),
because claims ride the ``connect`` session-boot path and reads ride the
report-only health/board surfaces — none of which may crash on a flaky bus.

Layering: imports schema/remote/output — never cli/views/lifecycle/inbox/
presence/query (fitness-pinned in tests/test_roles.py like loop_ops.py).
Liveness judgment does NOT live here: freshness needs the presence thresholds
(views policy), so the read surfaces up-layer (cli/query) feed these raw
records to the pure ``roles.role_status`` fold with injected thresholds.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import remote, schema
from .output import warn as _warn
# The bus clock format ("UTC, microsecond precision, trailing Z") has ONE home:
# timeutil — a pure stdlib leaf, so binding it here costs no layering edge (the
# role_ops import pin forbids only up-layer modules). Bound under the local
# historical name; this replaced an inlined duplicate of the same function.
from .timeutil import now_iso as _now_z


class _RegistryReadError:
    """Sentinel type for :data:`READ_ERROR` — see read_role."""

    def __repr__(self) -> str:  # diagnosable in test failures / debug prints
        return "<role registry READ_ERROR>"


#: 2026-06-11 bug hunt C1 (P0): the "this read FAILED" sentinel, distinct from
#: None ("this record is confirmed absent"). read_role used to collapse both
#: into None, so ONE transient transport failure made a registered role look
#: unregistered — and the self-registering call sites (claim_role, and worse,
#: continuity's set_role_checkpoint_ref on EVERY session exit via park) then
#: wholesale-replaced the operator's rich role definition with a minimal
#: make_role(name, ""). Callers must treat READ_ERROR as "do not write".
READ_ERROR = _RegistryReadError()


def read_role(name: str, *, backend: Optional[list[str]] = None
              ) -> Any:
    """One role's registry record (dict), None on CONFIRMED absence, or the
    :data:`READ_ERROR` sentinel on a transport/read failure.

    Absent-vs-error disambiguation (2026-06-11 bug hunt C1): the transport's
    download returns None for both "no such file" and "download failed", so a
    failed download is followed by a ``remote.stat`` probe — a visible stat
    means the record EXISTS but could not be read (error, never absence). Only
    when BOTH probes agree the record isn't there do we report None. A raising
    transport reads as error too (fail-safe: a writer acting on "absent" must
    never be acting on a guess)."""
    path = remote.role_record_path(name)
    try:
        rec = remote.download_json(path, backend=backend)
        if isinstance(rec, dict):
            return rec
        if remote.stat(path, backend=backend) is not None:
            return READ_ERROR   # record exists but is unreadable right now
        if not remote.probe_reachable(backend):
            return READ_ERROR   # bus dark: absence is unconfirmable
        return None             # probes agree and the bus answered: absent
    except Exception:
        return READ_ERROR


def load_roles_with_leases(
    *, backend: Optional[list[str]] = None, include_leases: bool = True,
) -> list[tuple[dict[str, Any], Any]]:
    """Every TOP-LEVEL role registry record WITH its lease sub-log, from ONE
    listing of the roles/ prefix: ``[(registry_record, leases), ...]`` sorted
    by role name, where ``leases`` is the sorted shard list, ``[]`` for a
    confirmed-empty sub-log, or :data:`READ_ERROR`.

    FILTER BEFORE DOWNLOAD (perf, 2026-06-11 loop-2 pass): the read surfaces
    (role health on every reconcile tick, the board's Roles section, ``roles``)
    used to pay ``list_roles`` — ``remote.list_json`` over the whole prefix,
    downloading every lease shard and escalation marker only to discard them —
    PLUS one ``read_leases`` per role (a re-list and a re-download of the very
    shards just thrown away). With R roles, L lease shards and E escalation
    markers that was 1+R listings and R+2L+E downloads per render; this fold
    is 1 listing and R+L downloads. The listing is partitioned by PATH first
    (the load_loop_records rule): top-level ``<slug>.json`` = registry record,
    ``<slug>/leases/*.json`` = lease shard, anything else (escalation markers,
    future sub-logs) is never downloaded.

    READ_ERROR DISCIPLINE PRESERVED (#171/F4 — load-bearing): a lease shard
    that was LISTED but would not download (or parsed to a non-dict) makes
    that role's ``leases`` the :data:`READ_ERROR` sentinel, never ``[]`` —
    one transport blip must not fold a HELD role to VACANT. Unlike
    ``read_leases``, an EMPTY lease set here needs no ``probe_reachable``
    spend: the role's registry record came back from the SAME successful
    listing, so the bus demonstrably answered and the emptiness is confirmed
    by construction.

    Best-effort at the edges exactly like ``list_roles``: a failed LISTING
    enumerates nothing (``[]``), and a registry record whose own download
    fails is dropped — along with its leases — from this glance (per-item
    isolation; the role re-appears next read). ``include_leases=False`` skips
    every shard download (``leases`` is None) — the ``list_roles`` fast path.
    """
    import concurrent.futures
    prefix = remote.roles_prefix()
    try:
        listed = remote.list_files(prefix, backend=backend)
    except Exception:
        return []
    registry_paths: list[str] = []
    lease_paths: dict[str, list[str]] = {}
    for path in listed:
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if not rel.endswith(".json"):
            continue
        if "/" not in rel:
            registry_paths.append(path)
            continue
        slug, _, sub = rel.partition("/")
        sub_dir, _, leaf = sub.partition("/")
        if sub_dir == "leases" and leaf and "/" not in leaf:
            lease_paths.setdefault(slug, []).append(path)
        # escalations/ markers (and any future per-role subtree) are pruned
        # HERE, by path — no read surface folds them, so they cost nothing.
    if not registry_paths:
        return []
    wanted = list(registry_paths)
    if include_leases:
        # Only shards under a LISTED registry record are fetched: an orphan
        # lease subtree (registry record deleted) has no role to fold into.
        registry_slugs = {Path(p).stem for p in registry_paths}
        for slug in sorted(registry_slugs):
            wanted.extend(lease_paths.get(slug, []))
    # Pooled download of exactly the surviving paths (the list_json pool
    # shape): independent subprocesses, no shared state. Callees resolve via
    # the remote module so test patches on remote.download_json still apply.
    results: dict[str, Any] = {}
    workers = min(8, max(2, len(wanted)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(remote.download_json, p, backend=backend): p
            for p in wanted
        }
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                results[path] = fut.result()
            except Exception:
                results[path] = None   # reads as a failed download below
    out: list[tuple[dict[str, Any], Any]] = []
    for rpath in registry_paths:
        rec = results.get(rpath)
        if not isinstance(rec, dict):
            continue  # per-item isolation: an unreadable record is dropped
        if not include_leases:
            out.append((rec, None))
            continue
        shards = lease_paths.get(Path(rpath).stem, [])
        if any(not isinstance(results.get(sp), dict) for sp in shards):
            # F4: a listed-but-unreadable shard is a read ERROR for the whole
            # role — partial lease truth must never masquerade as the union.
            out.append((rec, READ_ERROR))
            continue
        events = [(results[sp].get("at", "") or "", Path(sp).stem, results[sp])
                  for sp in shards]
        events.sort(key=lambda t: (t[0], t[1]))
        out.append((rec, [shard for _at, _stem, shard in events]))
    out.sort(key=lambda pair: pair[0].get("name") or "")
    return out


def list_roles(*, backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Every TOP-LEVEL role registry record, sorted by name. Best-effort: [].

    TOP-LEVEL-ONLY FILTER (the load_loop_records rule, restated): the roles
    prefix holds per-role SUBTREES (``<name>/leases/``, ``<name>/escalations/``)
    beside the registry records; only a path that, after stripping the prefix,
    has no further ``/`` and ends in ``.json`` is a registry record — a lease
    shard counted as a role would inflate the registry. Rides the partitioned
    ``load_roles_with_leases`` fold with the shard downloads SKIPPED, so a
    registry glance costs 1 listing + R downloads (it used to download every
    lease shard and escalation marker too, only to throw them away)."""
    return [rec for rec, _leases in
            load_roles_with_leases(backend=backend, include_leases=False)]


def upsert_role(record: dict[str, Any], *,
                backend: Optional[list[str]] = None) -> bool:
    """Write one role's registry record (operator CRUD). Best-effort bool.

    This is the AUTHORITATIVE write of the role layer, so it is the one place
    that verifies after write: a ``remote.stat`` probe must confirm the record
    actually landed — an upload that claimed success but cannot be stat'd
    reads as failure, so the operator re-runs instead of trusting a phantom
    registry entry."""
    try:
        name = (record or {}).get("name")
        if not name:
            return False
        path = remote.role_record_path(name)
        if not remote.upload_json(record, path, backend=backend):
            return False
        return remote.stat(path, backend=backend) is not None
    except Exception:
        return False


def read_leases(name: str, *, backend: Optional[list[str]] = None
                ) -> Any:
    """Every lease shard for a role, sorted by (at, path stem) — the same
    machine-agnostic stable order as read_loop_responses. Returns ``[]`` only
    for a CONFIRMED-empty sub-log, or the :data:`READ_ERROR` sentinel when
    the lease state could not be read. Raw records only: freshness is judged
    up-layer by roles.role_status against the presence roster (see the module
    docstring).

    2026-06-11 roles/presence read-error audit (F4): this used to return []
    on ANY failure — and [] folds to VACANT in roles.role_status with
    ``vacant_since`` = the role's (old) ``created_at``, so ONE failed lease
    listing pushed a false "Role VACANT past SLA" P1 directive onto the
    maintainer's plate. Same C1 discipline as read_role: a shard that was
    LISTED but would not download is an error outright; an EMPTY listing is
    trusted only after ``probe_reachable`` confirms the bus answered
    (list_files swallows transport failures into [] — emptiness alone proves
    nothing). The probe is spent on the empty path only."""
    try:
        records, complete = remote.list_json_checked(
            remote.role_leases_prefix(name), backend=backend)
        if not complete:
            return READ_ERROR   # listing failed, or a listed shard wouldn't read
        if not records and not remote.probe_reachable(backend):
            return READ_ERROR   # "empty" from an unreachable bus is a guess
    except Exception:
        return READ_ERROR
    events: list[tuple[str, str, dict[str, Any]]] = []
    for path, rec in records:
        if isinstance(rec, dict):
            events.append((rec.get("at", "") or "", Path(path).stem, rec))
    events.sort(key=lambda t: (t[0], t[1]))
    return [rec for _at, _stem, rec in events]


def claim_role(name: str, agent: str, *,
               backend: Optional[list[str]] = None) -> bool:
    """Claim a role for ``agent``: write its per-agent lease shard. Best-effort.

    A claim must NOT fail on an unregistered role — ``connect --role X`` runs
    on session boot against buses whose operator never wrote a registry — so
    a CONFIRMED-absent registry record SELF-REGISTERS as a minimal role (empty
    instructions) with a warn; the operator fleshes it out later via
    ``roles set``. A registry record that exists but cannot be read right now
    (READ_ERROR — 2026-06-11 bug hunt C1) is NEVER self-registered over: the
    lease still lands, the registry is left alone. The lease shard is keyed by
    the claiming agent, so a re-claim refreshes the claimer's OWN file only
    (idempotent; never evicts other holders).

    Exclusive policy: an existing lease from ANOTHER agent gets a warn (the
    claim still lands — a stale holder is claimable, and freshness is judged
    at read time by ``roles.role_status``, which is also where a genuinely
    CONTESTED double-hold surfaces on the board/health)."""
    try:
        if not name or not str(name).strip() or not agent:
            return False
        role = read_role(name, backend=backend)
        if role is READ_ERROR:
            # 2026-06-11 bug hunt C1 (P0): a transient read failure must NOT
            # look like absence — self-registering here used to upsert a
            # minimal make_role(name, "") OVER the operator's rich record.
            # Claim the lease anyway (the per-agent shard is clobber-free and
            # the session genuinely holds the role) but leave the registry
            # strictly alone; the exclusive-policy check is skipped because
            # the policy is unknowable without the record (contested holds
            # still surface at read time via roles.role_status).
            _warn(f"claim: role '{name}' registry record could not be read — "
                  "claiming the lease without touching the registry")
            role = None
        elif role is None:
            # CONFIRMED absent (download + stat probe agree): self-register —
            # connect --role X must work on buses whose operator never wrote
            # a registry (existing behavior, now gated on confirmed absence).
            _warn(f"claim: role '{name}' is not registered — self-registering "
                  "a minimal record (add instructions via `roles set`)")
            role = schema.make_role(str(name).strip(), "")
            upsert_role(role, backend=backend)   # best-effort; claim proceeds
        if role is not None and role.get("policy") == "exclusive":
            existing_leases = read_leases(name, backend=backend)
            if existing_leases is READ_ERROR:
                # F4: the contested check is ADVISORY — an unreadable lease
                # sub-log must not fail the claim (the per-agent shard below
                # is clobber-free regardless). Skip the warn; a genuine
                # double-hold still surfaces at read time via role_status.
                _warn(f"claim: lease listing for '{name}' could not be read — "
                      "skipping the exclusive-policy check")
                existing_leases = []
            others = sorted({
                lease.get("agent") for lease in existing_leases
                if lease.get("agent") and lease.get("agent") != agent
            })
            if others:
                _warn(f"claim: role '{name}' is exclusive and already has "
                      f"lease(s) from {others} — CONTESTED if their presence "
                      "is fresh (see `fulcra-coord roles`)")
        lease = {
            "schema": schema.ROLE_LEASE_SCHEMA,
            "role": (role or {}).get("name") or str(name).strip(),
            "agent": agent,
            "at": _now_z(),
        }
        return bool(remote.upload_json(
            lease, remote.role_lease_path(name, agent), backend=backend))
    except Exception:
        return False


def release_role(name: str, agent: str, *,
                 backend: Optional[list[str]] = None) -> bool:
    """Release ``agent``'s own lease on a role (delete its shard). Best-effort:
    False when there was nothing to release or the delete failed. Only the
    caller's own per-agent file is touched — releasing a shared role never
    evicts the other holders (per-agent shards, by construction)."""
    try:
        return bool(remote.delete(remote.role_lease_path(name, agent),
                                  backend=backend))
    except Exception:
        return False
