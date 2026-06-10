"""Continuity ↔ roles operations: role checkpoints, claim-time resume, park.

Spec 2026-06-10-continuity-integration-design.md, items 2 (role claim →
resume) and 3 (park hooks). The ROLE is the durable identity (roles spec);
this module gives it a durable "where I left off": the registry record's
``checkpoint_ref`` — RESERVED by the roles spec for exactly this phase —
points at a continuity checkpoint that survives every session death. The
ArcBot remote-control backbone is: spawn session → claim role → resume brief
→ work → checkpoint on park.

Boundaries (the decoupling the spec demands):

  * coord NEVER imports ``fulcra_continuity`` (fitness-pinned in
    tests/test_continuity_integration.py). The checkpoint schema belongs to
    that package; this module touches checkpoints only through the optional
    ``fulcra-continuity`` CLI as a SUBPROCESS, and through coord's own
    stdlib bridge (``continuity.py``) for bus upload/download of opaque
    JSON blobs.
  * refs are OPAQUE STRINGS — stored and printed verbatim, never parsed.

Failure discipline: the resume-print helpers ride the ``connect`` session-boot
and ``roles claim`` paths, and ``park`` rides PreCompact/SessionEnd hooks —
so everything those paths touch is best-effort and timeout-bounded; a
continuity problem must never block a session boot or fail a session exit.

Layering: imports continuity / identity / remote / role_ops / schema / views
and the output leaf — never cli/lifecycle/inbox/presence. The call sites
(cli's ``roles claim``, presence's ``connect``) import DOWN into this module,
which sits beside role_ops/continuity in the low-feature tier, so neither
edge cycles (presence is not imported here).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from . import continuity, schema
from . import role_ops as _role_ops
from .output import err as _err, info as _info, warn as _warn


def set_role_checkpoint_ref(
    name: str, ref: str, *, backend: Optional[list[str]] = None
) -> bool:
    """Point role ``name``'s registry ``checkpoint_ref`` at ``ref``,
    preserving every other field.

    Read-modify-write of the operator's registry record is safe here because
    we mutate ONLY checkpoint_ref (+ updated_at) on the freshly-read record —
    the `roles set` _pick/preserve contract, applied to one field. An absent
    registry record self-registers minimally (the claim_role posture: park /
    checkpoint must work on buses whose operator never wrote a registry).
    Best-effort bool, never raises — this rides the park hook."""
    try:
        if not name or not str(name).strip() or not ref:
            return False
        rec = _role_ops.read_role(name, backend=backend)
        if rec is None:
            _warn(f"checkpoint: role '{name}' is not registered — "
                  "self-registering a minimal record")
            rec = schema.make_role(str(name).strip(), "")
        rec["checkpoint_ref"] = str(ref)
        rec["updated_at"] = datetime.now(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        return _role_ops.upsert_role(rec, backend=backend)
    except Exception:
        return False


def role_resume_lines(
    role: dict[str, Any], *, backend: Optional[list[str]] = None
) -> list[str]:
    """The claim-time resume rendering for one role record: the checkpoint
    ref line plus (when the optional fulcra-continuity CLI can render it) the
    indented resume brief. ``[]`` when the role carries no ref. Best-effort —
    the ref line is the floor, the brief is gravy; never raises."""
    ref = (role or {}).get("checkpoint_ref")
    if not ref:
        return []
    lines = [f"Role '{role.get('name', '?')}' checkpoint: {ref}"]
    try:
        brief = continuity.render_brief_for_ref(str(ref), backend=backend)
        if brief:
            lines.append("Resume brief:")
            lines.extend(f"  {ln}" for ln in brief.rstrip("\n").splitlines())
    except Exception:
        pass
    return lines


def print_role_resume(
    name: str, *, backend: Optional[list[str]] = None
) -> None:
    """Best-effort: read role ``name`` and print its resume lines (no-op when
    the role is absent or carries no checkpoint_ref). The single helper both
    lease paths (`roles claim` and `connect --role`) call so the claim →
    resume behaviour cannot diverge between them."""
    try:
        role = _role_ops.read_role(name, backend=backend)
        if not role:
            return
        for line in role_resume_lines(role, backend=backend):
            _info(f"  {line}")
    except Exception:
        pass


def cmd_checkpoint(args: Any, backend: Optional[list[str]] = None) -> int:
    """``checkpoint --role X [--ref R]`` — read or update a role's durable
    resume point.

    * With ``--ref``: set the role's registry ``checkpoint_ref`` (preserving
      every other field). The ref is an opaque string — usually the remote
      archive path `handoff`/`park` publish, but anything the adopter's
      tooling can resolve later is legal.
    * Without ``--ref``: SHOW the current ref + best-effort rendered brief —
      the read surface, so "where did this role leave off" is one command.

    (Distinct from the existing task-scoped ``snapshot`` command, which
    checkpoints a TASK into the continuity tree; this binds a ref to a ROLE.)
    """
    name = (getattr(args, "role", None) or "").strip()
    if not name:
        _err("checkpoint requires --role <name>.")
        return 1
    ref = getattr(args, "ref", None)

    if ref:
        if not set_role_checkpoint_ref(name, ref, backend=backend):
            _err(f"checkpoint: registry write for role '{name}' could not be "
                 "verified — re-run (the record may not have landed)")
            return 1
        _info(f"Role '{name}' checkpoint_ref -> {ref}")
        return 0

    role = _role_ops.read_role(name, backend=backend)
    if role is None:
        _err(f"checkpoint: role '{name}' is not registered.")
        return 1
    lines = role_resume_lines(role, backend=backend)
    if not lines:
        _info(f"Role '{name}' has no checkpoint_ref.")
        return 0
    for line in lines:
        _info(line)
    return 0
