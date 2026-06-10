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

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import remote, schema
from .output import warn as _warn


def _now_z() -> str:
    """Current UTC instant as an ISO-8601 ``...Z`` stamp (the bus's clock
    format). Inlined like loop_ops._now_z to keep the low-layer import
    surface minimal."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z")


def read_role(name: str, *, backend: Optional[list[str]] = None
              ) -> Optional[dict[str, Any]]:
    """One role's registry record, or None (absent/unreadable — best-effort)."""
    try:
        rec = remote.download_json(remote.role_record_path(name), backend=backend)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


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
    an absent registry record SELF-REGISTERS as a minimal role (empty
    instructions) with a warn; the operator fleshes it out later via
    ``roles set``. The lease shard is keyed by the claiming agent, so a
    re-claim refreshes the claimer's OWN file only (idempotent; never evicts
    other holders).

    Exclusive policy: an existing lease from ANOTHER agent gets a warn (the
    claim still lands — a stale holder is claimable, and freshness is judged
    at read time by ``roles.role_status``, which is also where a genuinely
    CONTESTED double-hold surfaces on the board/health)."""
    try:
        if not name or not str(name).strip() or not agent:
            return False
        role = read_role(name, backend=backend)
        if role is None:
            _warn(f"claim: role '{name}' is not registered — self-registering "
                  "a minimal record (add instructions via `roles set`)")
            role = schema.make_role(str(name).strip(), "")
            upsert_role(role, backend=backend)   # best-effort; claim proceeds
        if role.get("policy") == "exclusive":
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
            "role": role.get("name") or str(name).strip(),
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
