"""Dual-emit: mirror every bus-v3 write onto bus-v4, the annotation-native plane coord-fold folds.

Plan Task 13 (docs/superpowers/plans/2026-09-04-coord-fold.md; directive 65761fbd). Called ONCE at the end of
``records.emit_event`` — the engine's single write chokepoint — so no verb has to opt in and none can be missed.

Contract, each clause a failure it prevents:
* No bus-v4 config on the store -> no-op (returns False). A hardcoded fallback channel would let a host mirror
  onto a plane nobody folds; the config document is the only authority (G15).
* Mirror failure NEVER fails the v3 write: the v3 record is the delivery the fleet relies on today; the mirror is
  the parallel-bus evidence. This function does not raise.
* Kind map is total over the kinds that carry obligations; anything else is not mirrored:
  directive->open, response->close (of the slug it closes), claim->claim, verdict->note.
* The payload is the eight-field coord-fold event, written literally (v, at, from, to, kind, slug, pri, ptr).

Two writes are NOT v3 events, because no v3 record exists to mirror: ``retire_archived`` (reconcile's retention
pass, for each task it archives) and ``open_task_doc`` (``task restore`` for the task it brings back, ``task
start`` for the task it creates).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

V4_CONFIG = "team/{team}/_coord/bus-v4/records.json"
KIND_MAP = {"directive": "open", "response": "close", "claim": "claim", "verdict": "note"}
PAYLOAD_VERSION = 1
PRIORITIES = ("P0", "P1", "P2", "P3")


def v4_config(transport: Any, team: str) -> Optional[dict[str, str]]:
    """The bus-v4 channel, or None when absent/unreadable/malformed (all three are 'do not mirror')."""
    try:
        body, state = transport.read_classified(V4_CONFIG.format(team=team))
    except Exception:
        return None
    if state != "ok" or not body:
        return None
    try:
        cfg = json.loads(body)
    except ValueError:
        return None
    if not isinstance(cfg, dict) or not cfg.get("data_type") or not cfg.get("api_version"):
        return None
    return {"data_type": str(cfg["data_type"]), "api_version": str(cfg["api_version"])}


def payload(*, at: str, sender: str, to: str, kind: str, slug: str, pri: str, ptr: Optional[str]) -> dict[str, Any]:
    return {"v": PAYLOAD_VERSION, "at": at, "from": sender, "to": to, "kind": kind, "slug": slug, "pri": pri, "ptr": ptr}


def _opens_an_obligation(transport: Any, team: str, ptr: Optional[str]) -> bool:
    """RULING (operator 2026-09-05, coord-boss 6f8121fc class B): an obligation belongs to its ASSIGNEE, and a task with
    no assignee is nobody's. A v3 `directive` is mirrored as a v4 `open` only when the task doc it points at names
    an assignee. The doc is read once, at write time. An unreadable or absent doc mirrors anyway: over-capture
    beats a silent hole, and the assignee filter on the old plane will show the divergence rather than hide it."""
    if not ptr:
        return True
    try:
        from . import okf
        path = ptr if ptr.startswith("team/") else f"team/{team}/{ptr}"
        raw = transport.read(path)
        if raw is None:
            return True
        fm = okf.parse_frontmatter(raw) or {}
        assignee = str(fm.get("assignee") or "").strip()
        if assignee in ("", "-", "None", "null"):
            return False
        # Class C (coord-boss 24d545b0, 2026-09-05): a row that is already TERMINAL when its open is emitted — a DONE
        # report born done, a reply that closes on creation — is not an obligation on the old plane and must not open
        # on v4 either, or it carries an open with no close forever. The engine's own terminal set is the authority.
        from .model import TERMINAL_STATUSES
        return str(fm.get("status") or "").strip() not in TERMINAL_STATUSES
    except Exception:
        return True


def _v4_config_or_state(transport: Any, team: str) -> tuple[str, Optional[dict[str, str]]]:
    """("ok", cfg) | ("absent", None) | ("unknown", None). Only an AFFIRMATIVE absence means "no bus-v4 plane":
    an unreadable or malformed config is UNKNOWN, and a caller that must not skip a write silently treats it as a
    failure. (``v4_config`` folds all three into None, which is right for the mirror and wrong for retention.)"""
    try:
        body, state = transport.read_classified(V4_CONFIG.format(team=team))
    except Exception:
        return "unknown", None
    if state == "absent":
        return "absent", None
    if state != "ok" or not body:
        return "unknown", None
    try:
        cfg = json.loads(body)
    except ValueError:
        return "unknown", None
    if not isinstance(cfg, dict) or not cfg.get("data_type") or not cfg.get("api_version"):
        return "unknown", None
    return "ok", {"data_type": str(cfg["data_type"]), "api_version": str(cfg["api_version"])}


def _write_v4(transport: Any, team: str, *, source: str, sender: str, to: str, kind: str, slug: str, ptr: str,
              priority: str, at: str) -> Optional[bool]:
    """None = no bus-v4 plane (config affirmatively absent); True/False = write verdict. Never raises."""
    try:
        state, cfg = _v4_config_or_state(transport, team)
        if state == "absent":
            return None
        if cfg is None:
            return False
        pri = priority if priority in PRIORITIES else "P2"
        note = json.dumps(payload(at=at, sender=sender, to=to, kind=kind, slug=slug, pri=pri, ptr=ptr), sort_keys=True)
        return bool(transport.record_write(cfg["data_type"], cfg["api_version"], note, source))
    except Exception:
        return False


def retire_archived(transport: Any, team: str, *, sender: str, slug: str, ptr: str, priority: str,
                    at: str) -> Optional[bool]:
    """Close a retention-archived task on bus-v4 for EVERY fold. None = no bus-v4 plane; True/False = write verdict.

    Retention moves a quiet task into ``task/archive/`` and the old plane stops listing it, but it is not a v3 write,
    so nothing passed through ``records.emit_event`` and no v4 close was ever mirrored: every fold that held the open
    owed the row forever (measured 2026-10-16: most of a coordinator's owed rows pointed at archived docs). The close
    goes to ``all`` because retention cannot know which folds hold the open: the assignee's, a role holder's seeded
    copy, or every recipient of a broadcast. An archived task is owed by nobody, and a close of a slug a fold does
    not hold is a no-op there. An unreadable bus-v4 config is a FAILURE (False), never "no plane": the source doc
    is already gone and no later pass revisits it, so a silent skip would leave the open forever. Never raises."""
    return _write_v4(transport, team, source=sender, sender=sender, to="all", kind="close", slug=slug, ptr=ptr,
                     priority=priority, at=at)


def reopen_restored(transport: Any, team: str, *, actor: str, slug: str, doc: str, at: str) -> Optional[bool]:
    """Re-open on bus-v4 a task ``task restore`` brought back from the archive (retention closed it there)."""
    return open_task_doc(transport, team, actor=actor, slug=slug, doc=doc, at=at)


def open_task_doc(transport: Any, team: str, *, actor: str, slug: str, doc: str, at: str) -> Optional[bool]:
    """Open on bus-v4 the obligation a task doc names, for a doc no v3 directive announced.

    Two callers: ``task restore`` (retention closed the task when it archived it) and ``task start`` (the doc is
    born here, and only ``tell`` emitted the directive the mirror turns into an open, so self-created work never
    entered its assignee's fold; coord-boss 33073a42, measured 2026-09-08 on collect-maintainer).

    Same obligation rule as the directive mirror: an unassigned or terminal task opens for nobody (None). The open
    goes to the assignee, or to ``all`` for a broadcast (``*``), and carries the task's OWNER as ``from`` so a
    broadcast still excludes its owner and not whoever ran the verb; the record's source is the actor.
    None = nothing to open or no bus-v4 plane; True/False = write verdict. Never raises."""
    try:
        from . import okf
        from .model import TERMINAL_STATUSES
        fm = okf.parse_frontmatter(doc) or {}
        assignee = str(fm.get("assignee") or "").strip()
        if assignee in ("", "-", "None", "null") or str(fm.get("status") or "").strip() in TERMINAL_STATUSES:
            return None
        to = "all" if assignee == "*" else assignee
        owner = str(fm.get("owner") or "").strip() or actor
        return _write_v4(transport, team, source=actor, sender=owner, to=to, kind="open", slug=slug,
                         ptr=f"task/{slug}.md", priority=str(fm.get("priority") or ""), at=at)
    except Exception:
        return False


def mirror(transport: Any, team: Optional[str], *, sender: str, to: str, kind: str, priority: str, slug: str,
           ptr: Optional[str] = None, recorded_at: Optional[str] = None, fyi: bool = False) -> bool:
    """Mirror one v3 event onto bus-v4. True only if the v4 record write confirmed. Never raises.
    An FYI directive asks for nothing on the old plane (`tell --fyi` opens no obligation there) and so opens
    nothing here either; a directive whose task has no assignee opens for nobody (see _opens_an_obligation)."""
    try:
        if not team or kind not in KIND_MAP or priority not in PRIORITIES or not slug:
            return False
        if kind == "directive" and (fyi or not _opens_an_obligation(transport, team, ptr)):
            return False
        cfg = v4_config(transport, team)
        if cfg is None:
            return False
        at = recorded_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        note = json.dumps(payload(at=at, sender=sender, to=to, kind=KIND_MAP[kind], slug=slug, pri=priority, ptr=ptr),
                          sort_keys=True)
        return bool(transport.record_write(cfg["data_type"], cfg["api_version"], note, sender, recorded_at=recorded_at))
    except Exception:
        return False
