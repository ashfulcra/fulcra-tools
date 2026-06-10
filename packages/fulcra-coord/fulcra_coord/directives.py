"""Pure task→directive mapping for the Phase 3b strangler-fig dual-write.

fulcra-coord historically models a directive ("agent A tells agent B to do X")
as a LEGACY task-with-assignee: a ``proposed`` task whose ``assignee`` is the
target agent and whose ``owner_agent`` is the issuer. Phase 3a added a
first-class ``Directive`` record (``schema.make_directive``); Phase 3b makes the
directive-creating commands ADDITIVELY mirror each such task into a
``directives/<id>.json`` record, best-effort, with ZERO behaviour change — the
legacy task stays authoritative and nothing READS directives for correctness yet.

This module is the PURE mapping core, deliberately testable from plain task
dicts with no I/O. LAYERING: it may import only DOWN/peer modules
(``schema`` / ``remote`` / ``timeutil`` and pure constants from ``routing`` /
``views``); it MUST NOT import the up-layer (``lifecycle`` / ``cli`` / ``views``
builders that pull I/O / ``writepipe`` / ``inbox``) — the package fitness test
enforces this. We import only the leaf TAG CONSTANTS and the ``BROADCAST``
sentinel from ``routing`` / ``views`` (both pure values, not behaviour), so the
mapping stays a pure function of its input.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import schema
from . import remote
from . import log as ops_log


def _now_z() -> str:
    """Current UTC instant as an ISO-8601 ``...Z`` stamp (the bus's clock format).

    Inlined here rather than importing ``timeutil`` so ``directives`` keeps its
    minimal low-layer import surface (schema / remote / log only)."""
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")

# Pure leaf constants, re-declared locally to respect LAYERING. ``views`` is an
# up-layer module (forbidden by the package fitness test), so we MUST NOT import
# it — even just for ``views.BROADCAST``. ``routing.REVIEW_TAG`` is pure, but
# ``routing`` imports ``views`` transitively, so we keep the dependency surface
# minimal and re-declare the three constants here with a sync note. These are
# bare strings, not behaviour; the risk is drift, not coupling, so the keep-in-
# sync comments below are the contract.
#
# Keep in sync with:
#   views.BROADCAST              == "*"
#   routing.REVIEW_TAG           == "kind:review"
#   routing_ops.REVIEW_VERDICT_TAG == "kind:review-verdict"
BROADCAST = "*"
REVIEW_TAG = "kind:review"
_VERDICT_TAG = "kind:review-verdict"


def _has_inbox_ack(task: dict[str, Any]) -> bool:
    """True iff some agent has inbox-acked this directive.

    The ack lives in the event log (``inbox_ack`` events). On a compact summary
    (no events) the same fact is flattened into ``acked_by`` — read either so the
    mapper works on both a full task body and a summary.
    """
    if task.get("acked_by"):
        return True
    return any(e.get("type") == "inbox_ack" for e in task.get("events", []))


def _was_claimed(task: dict[str, Any]) -> bool:
    """True iff this directive was ever claimed/picked-up by the assignee.

    "Claimed" = the task left ``proposed`` at least once. We detect it from the
    event log: a real pickup appends a status-transition event whose ``type`` is
    a VALID status (e.g. ``active``/``waiting``/``blocked``/``done``). A directive
    that only ever has its ``created`` event (and maybe an ``inbox_ack``) was
    never claimed. We also treat an inbox_ack as a claim-ish signal for the
    "never claimed" determination below (an acked directive is not orphaned).
    """
    for e in task.get("events", []):
        etype = e.get("type")
        if etype in schema.VALID_STATUSES and etype != "proposed":
            return True
    return False


def _directive_status_for(task: dict[str, Any]) -> str:
    """Map a legacy task's state onto a directive status (pure, testable).

    The directive status vocabulary (proposed/delivered/acked/acted/expired) is
    SMALLER than the task status machine — directives are communication
    primitives, not lifecycle-tracked work. The map:

      * ``proposed``                       → ``proposed``  (issued, not yet seen)
      * some agent has inbox-acked it      → ``acked``     (seen, not yet acted)
      * ``active`` / ``waiting`` / ``blocked`` → ``acted``  (recipient is acting)
      * ``done``                           → ``acted``     (terminal-ack) IF it
        was ever claimed; ``expired`` if it was never claimed (a directive that
        got closed out without anyone picking it up never resulted in action)
      * ``abandoned``                      → ``expired``   (dropped, no action)

    The ack check is evaluated FIRST for a still-``proposed`` task: an ack is a
    stronger signal than the bare proposed status (the recipient has seen it).
    For a task that already moved into an active/terminal state the status itself
    is the dominant signal, so we evaluate ack only on the proposed branch.
    """
    status = task.get("status", "proposed")

    if status == "proposed":
        # An ack on a still-proposed directive means "seen but not yet claimed".
        return "acked" if _has_inbox_ack(task) else "proposed"

    if status in ("active", "waiting", "blocked"):
        return "acted"

    if status == "done":
        # Terminal-ack: done-after-claim is the recipient having acted.
        # Done WITHOUT ever being claimed (no pickup, no ack) is an expiry —
        # the directive was closed administratively, not acted upon.
        if _was_claimed(task) or _has_inbox_ack(task):
            return "acted"
        return "expired"

    if status == "abandoned":
        return "expired"

    # Defensive default for any unexpected status: treat as proposed (the safe,
    # non-terminal directive state) rather than raise — the dual-write is
    # best-effort and must never break a real task.
    return "proposed"


def _acked_by_from_task(task: dict[str, Any]) -> list[str]:
    """The set of agents who inbox-acked this directive, sorted.

    Reads the flattened ``acked_by`` (summary form) when present, else derives it
    from ``inbox_ack`` events on a full body — symmetric with task_summary.
    """
    if task.get("acked_by"):
        return sorted({a for a in task["acked_by"] if a})
    return sorted({
        e.get("by") for e in task.get("events", [])
        if e.get("type") == "inbox_ack" and e.get("by")
    })


def _directive_type_for(task: dict[str, Any]) -> str:
    """Pick the directive_type for a legacy directive-task.

    * ``broadcast`` when ``assignee == "*"`` (the BROADCAST wildcard).
    * ``review``    when the task carries the ``kind:review`` membership tag.
    * ``verdict``   when it carries the ``kind:review-verdict`` tag.
    * ``tell``      otherwise (the one-to-one default).

    Broadcast is checked first because a broadcast review (rare) is still
    fundamentally fan-out — audience ``*`` is the dominant routing fact.
    """
    if task.get("assignee") == BROADCAST:
        return "broadcast"
    tags = task.get("tags") or []
    if _VERDICT_TAG in tags:
        return "verdict"
    if REVIEW_TAG in tags:
        return "review"
    return "tell"


def _stable_directive_id(task_id: str) -> str:
    """Deterministic directive id for the mirror of legacy task ``task_id``.

    STORAGE MODEL A (LWW snapshot): a directive-task maps to EXACTLY ONE
    ``directives/<id>.json`` record. Every dual-write of the same task — the
    initial tell/broadcast/request-review/review-done AND every later edit that
    re-creates the directive (a re-assign that changes the audience, a status
    change) — must land on the SAME directive file so the latest write wins and
    overwrites rather than spawning a fresh random-id duplicate per edit.

    ``schema.make_directive_id`` uses a RANDOM suffix (collision-resistance for
    independently-authored directives), which is exactly wrong here: a re-assign
    would mint a second file and the directive store would accumulate stale
    duplicates of the same logical directive. So we derive the id deterministically
    from the task id instead: ``DIR-T-<task_id>``. The mirror is 1:1 with its task,
    so keying on the task id is both stable and unique.
    """
    return f"DIR-T-{task_id}"


def stable_directive_id(task_id: str) -> str:
    """Public accessor for the deterministic directive id of a legacy task.

    The up-layer hooks (``inbox.cmd_inbox --ack`` -> durable directive ack,
    ``routing_ops`` -> routing sub-log) need the SAME ``DIR-T-<task_id>`` id the
    dual-write uses, so the per-agent ack files and route shards land under the
    directive that mirrors the task. Thin public wrapper over the internal
    ``_stable_directive_id`` so callers don't reach for the underscore name."""
    return _stable_directive_id(task_id)


# ---------------------------------------------------------------------------
# Append-only SUB-LOG API (Phase 3b Task 2) — ack + routing persistence.
#
# WHY a sub-log and not a field on the single directive record: see the long note
# on remote.directive_acks_prefix. The bus has no compare-and-swap, so a
# read-modify-write of one record loses concurrent writes. Each writer here writes
# its OWN file (per-agent ack file / per-event route shard); the union is the
# list-the-prefix read. ALL of these are LOW-LAYER and BEST-EFFORT: they never
# raise (a transport failure degrades to False / []), so a sub-log miss can never
# break the authoritative task write, the inbox-ack, or a route command.
# ---------------------------------------------------------------------------

def write_directive_ack(
    directive_id: str, agent: str, *, backend: Optional[list[str]] = None
) -> bool:
    """Durably record ONE agent's ack of a directive (per-agent file; idempotent).

    Uploads ``{agent, at}`` to ``directive_ack_path(id, agent)`` — a file keyed by
    the acking agent, so re-acking overwrites only that agent's own file and two
    different agents acking the same (e.g. broadcast) directive NEVER clobber each
    other. This is the clobber-safe, durable per-agent ack. Best-effort: returns
    True on a confirmed upload, False on any failure (never raises)."""
    try:
        record = {"agent": agent, "at": _now_z()}
        return bool(remote.upload_json(
            record, remote.directive_ack_path(directive_id, agent), backend=backend))
    except Exception:
        return False


def read_directive_acks(
    directive_id: str, *, backend: Optional[list[str]] = None
) -> list[str]:
    """The sorted UNION of agents who have acked a directive (list the acks prefix).

    Reads every per-agent ack file under the acks prefix and returns the distinct
    ``agent`` ids, sorted. This is the durable ack truth — it can't shrink when one
    agent re-acks, because each agent owns its own file. Best-effort: a
    missing/empty prefix or any read failure -> ``[]`` (never raises)."""
    try:
        records = remote.list_json(remote.directive_acks_prefix(directive_id),
                                   backend=backend)
    except Exception:
        return []
    agents: set[str] = set()
    for _path, rec in records:
        if isinstance(rec, dict) and rec.get("agent"):
            agents.add(rec["agent"])
    return sorted(agents)


def append_directive_route(
    directive_id: str, route_event: dict[str, Any], *,
    backend: Optional[list[str]] = None,
) -> bool:
    """Append ONE route event to a directive's routing sub-log (append-only shard).

    Uploads ``route_event`` to ``directive_route_path(id, <event_id>)``. The shard
    key is the event's own ``event_id`` (or its ``route_id``, the field a route
    event built by ``routing.make_route_event`` carries) or, failing both, a fresh
    uuid — so each route decision lands as its OWN file and concurrent re-routes
    never overwrite one another. Best-effort: returns True on a confirmed upload,
    False on any failure (never raises)."""
    try:
        event_id = (route_event.get("event_id") or route_event.get("route_id")
                    or uuid.uuid4().hex)
        return bool(remote.upload_json(
            route_event, remote.directive_route_path(directive_id, str(event_id)),
            backend=backend))
    except Exception:
        return False


def read_directive_routing(
    directive_id: str, *, backend: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Every route-event shard for a directive, sorted by (at, event id).

    Lists the routing prefix and returns the route-event dicts in stable order —
    by their ``at`` stamp, ties broken by the shard's event id (the filename stem,
    which is the event_id/route_id) so the order is machine-agnostic (the bus has
    no global clock). Best-effort: a missing prefix or any read failure -> ``[]``."""
    try:
        records = remote.list_json(remote.directive_routing_prefix(directive_id),
                                   backend=backend)
    except Exception:
        return []
    events: list[tuple[str, str, dict[str, Any]]] = []
    for path, rec in records:
        if isinstance(rec, dict):
            events.append((rec.get("at", "") or "", Path(path).stem, rec))
    events.sort(key=lambda t: (t[0], t[1]))
    return [rec for _at, _eid, rec in events]


def directive_from_task(task: dict[str, Any]) -> dict[str, Any]:
    """Build a first-class Directive record mirroring a legacy directive-task.

    ADDITIVE / best-effort: this is the payload the Phase 3b dual-write uploads
    alongside the authoritative task. It NEVER mutates ``task``.

    Field mapping:
      * ``directive_type`` via ``_directive_type_for`` (broadcast/review/verdict/tell)
      * ``from_agent`` = task ``owner_agent``; ``audience`` = task ``assignee``
      * title / summary / next_action / priority / workstream / not_before / due
        carried straight across
      * ``status`` via ``_directive_status_for``
      * ``task_id`` = the legacy task's id (the dual-write back-reference)
      * ``acked_by`` injected from the task's inbox_ack events (make_directive
        always starts it empty, so we set it on the built record)
      * ``artifact_ref`` from review metadata (``task["pr"]`` carried by
        request-review) when present, else None

    make_directive REQUIRES a non-empty workstream and non-empty from/audience.
    A real directive-task always has those, but to guarantee the mapper NEVER
    raises on a live task we fall back: workstream → task workstream or
    ``"general"``; from/audience → ``"unknown"`` / ``"unknown"`` as a last resort
    (a task missing owner_agent/assignee is malformed, but the best-effort
    dual-write must degrade, not crash the authoritative write).
    """
    workstream = (task.get("workstream") or "").strip() or "general"
    from_agent = (task.get("owner_agent") or "").strip() or "unknown"
    audience = task.get("assignee")
    audience = (audience.strip() if isinstance(audience, str) else "") or "unknown"

    # Deterministic id keyed on the originating task (storage model A — see
    # _stable_directive_id) so re-writes OVERWRITE the same record (LWW). If the
    # task is malformed with no id, fall back to make_directive's random id rather
    # than crash the best-effort mirror.
    task_id = task.get("id")
    directive_id = _stable_directive_id(task_id) if task_id else None

    # artifact_ref: request-review stores the opaque review ref verbatim in
    # task["pr"]; surface it as a structured ref when present. None otherwise.
    artifact_ref = None
    pr = task.get("pr")
    if pr is not None:
        artifact_ref = {"ref": str(pr)}
        if task.get("repo"):
            artifact_ref["repo"] = task["repo"]

    directive_type = _directive_type_for(task)
    loop_kind = None
    loop_state = None
    loop_expects_response = False
    loop_sla_hours = None
    if directive_type == "review":
        loop_kind = "review"
        loop_state = "requested"
        loop_expects_response = True
        loop_sla_hours = 24

    directive = schema.make_directive(
        directive_type=directive_type,
        from_agent=from_agent,
        audience=audience,
        title=task.get("title") or "(untitled)",
        workstream=workstream,
        summary=task.get("current_summary", "") or "",
        next_action=task.get("next_action", "") or "",
        priority=task.get("priority", "P2") or "P2",
        status=_directive_status_for(task),
        artifact_ref=artifact_ref,
        not_before=task.get("not_before"),
        due=task.get("due"),
        task_id=task_id,
        directive_id=directive_id,
        kind=loop_kind,
        state=loop_state,
        expects_response=loop_expects_response,
        sla_hours=loop_sla_hours,
    )
    # make_directive starts acked_by empty; carry the task's acks onto the mirror
    # so the directive reflects who has already acknowledged it.
    acked = _acked_by_from_task(task)
    if acked:
        directive["acked_by"] = acked
    return directive


def dual_write(
    task: dict[str, Any], *, command: str, backend: Optional[list[str]] = None
) -> None:
    """Shared Phase 3b best-effort directive dual-write — the WRITE half.

    ADDITIVELY mirror a directive-creating ``task`` into a first-class
    ``directives/<id>.json`` record. This is the single low-layer implementation
    of the strangler-fig dual-write so EVERY directive-creating command (tell /
    broadcast / assign / request-review / review-done) shares one writer rather
    than each re-implementing the upload + ops-log-on-miss dance.

    WHY this lives in ``directives.py`` (the low layer) and NOT as a helper
    imported from ``lifecycle``: ``lifecycle`` already imports
    ``routing_ops._escalate_review_to_human`` at module load, so a module-level
    ``from . import lifecycle`` in ``routing_ops`` would close a
    ``lifecycle → routing_ops → lifecycle`` import cycle. Putting the writer here
    — where ``routing_ops`` and ``lifecycle`` BOTH already depend down onto
    ``directives`` — lets both peers reuse it with zero new edges and no cycle.
    ``directives.py`` stays layering-clean: it imports only ``schema`` / ``remote``
    / ``log`` (ops_log), never ``lifecycle`` / ``cli`` / ``views`` / ``writepipe``
    / ``inbox`` / ``routing_ops`` (enforced by ``test_directives_imports_no_up_layer_module``).

    BEST-EFFORT — this NEVER fails or alters the authoritative legacy task write.
    Callers invoke it ONLY AFTER the task body has already landed, so the legacy
    write is committed regardless of what happens here. Any failure (a raising or
    False-returning upload, a mapping error) is swallowed and recorded in the ops
    log as ``directive_write_failed`` so migration parity can be audited — exactly
    mirroring the event-append best-effort posture in writepipe.
    """
    try:
        directive = directive_from_task(task)
        # Fold the durable SUB-LOG truth into the LWW snapshot before uploading.
        # directive_from_task stays PURE (task-derived acks only); the union with
        # the append-only sub-log happens HERE, in the I/O layer, so the snapshot
        # reflects every durable ack — even ones the task's capped inline event log
        # has since dropped. The snapshot's acked_by therefore NEVER shrinks below
        # the sub-log union, and routing reflects every persisted route shard.
        # Best-effort per side: a sub-log read failure leaves the task-derived
        # acked_by / empty routing as-is (never worse than today).
        directive_id = directive.get("id")
        if directive_id:
            try:
                sublog_acks = read_directive_acks(directive_id, backend=backend)
                if sublog_acks:
                    union = set(directive.get("acked_by") or []) | set(sublog_acks)
                    directive["acked_by"] = sorted(union)
            except Exception:
                pass  # leave task-derived acked_by untouched
            try:
                routes = read_directive_routing(directive_id, backend=backend)
                if routes:
                    directive["routing"] = routes
            except Exception:
                pass  # leave routing as make_directive's empty default
        ok = remote.upload_json(
            directive, remote.directive_remote_path(directive["id"]), backend=backend
        )
        if not ok:
            try:
                ops_log.log_op(command, task.get("id"),
                               status="directive_write_failed",
                               error="Directive upload returned false")
            except Exception:
                pass
    except Exception as exc:
        # Best-effort: the legacy task write already succeeded. Record the miss
        # in the ops log (Phase 3b's job is to validate the dual-write), guarded
        # so even the logging cannot break the authoritative write.
        try:
            ops_log.log_op(command, task.get("id"),
                           status="directive_write_failed", error=str(exc))
        except Exception:
            pass
