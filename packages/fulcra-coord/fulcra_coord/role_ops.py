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

Everything here is best-effort never-raise (True/False, record-or-None, []),
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
        return None             # both probes agree: confirmed absent
    except Exception:
        return READ_ERROR


def list_roles(*, backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Every TOP-LEVEL role registry record, sorted by name. Best-effort: [].

    TOP-LEVEL-ONLY FILTER (the load_loop_records rule, restated): the roles
    prefix holds per-role SUBTREES (``<name>/leases/``, ``<name>/escalations/``)
    beside the registry records; only a path that, after stripping the prefix,
    has no further ``/`` and ends in ``.json`` is a registry record — a lease
    shard counted as a role would inflate the registry."""
    prefix = remote.roles_prefix()
    try:
        listed = remote.list_json(prefix, backend=backend)
    except Exception:
        return []
    records: list[dict[str, Any]] = []
    for path, rec in listed:
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if "/" in rel:
            continue  # lease/escalation shard — never a registry record
        if not rel.endswith(".json"):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return sorted(records, key=lambda r: r.get("name") or "")


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
                ) -> list[dict[str, Any]]:
    """Every lease shard for a role, sorted by (at, path stem) — the same
    machine-agnostic stable order as read_loop_responses. Best-effort: [].
    Raw records only: freshness is judged up-layer by roles.role_status
    against the presence roster (see the module docstring)."""
    try:
        records = remote.list_json(remote.role_leases_prefix(name),
                                   backend=backend)
    except Exception:
        return []
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
            others = sorted({
                lease.get("agent") for lease in read_leases(name, backend=backend)
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
