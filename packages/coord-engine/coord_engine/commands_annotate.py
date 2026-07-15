"""coord-engine annotate commands — the activity-annotation projection.

Extracted verbatim from ``cli.py`` (behavior-preserving module split). Shared
cli-level helpers (``_now``/``_iso``/``_host``/``_log``) and the patch-sensitive
``_emit_projection_spec`` are reached through the ``cli`` module: the projector is
defined here but ``cmd_annotate_project`` calls ``cli._emit_projection_spec`` so
``monkeypatch.setattr(cli, "_emit_projection_spec", …)`` still steers it (cli
re-exports the name). Dispatch stays wired in ``cli.build_parser``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import annotate as annotate_mod
from . import cli


# --- annotate (activity-annotation projection) ---

def _emit_projection_spec(spec: Any, *, agent: str) -> bool:
    """Hand ONE projected AnnotationSpec to the hardened fulcra_common writer.

    Best-effort: coord-engine is stdlib-only, so the writer package (and the
    fulcra-api CLI / token it needs) may be entirely absent — that degrades to
    False, never an exception, so `annotate project` still exits 0 on a host
    without the writer. Reuses the writer's typed-record POST path; never opens a
    second one."""
    try:
        from fulcra_common import annotations as _ann
    except Exception:
        return False
    try:
        return bool(_ann.emit_projection_annotation(
            note=spec.note, tags=list(spec.tags), recorded_at=spec.ts,
            id=spec.id, agent=agent))
    except Exception:
        return False


def cmd_annotate_resolution(args: argparse.Namespace, transport: Any) -> int:
    """Set the team's projection RESOLUTION level on the bus (any host's
    heartbeat reads it). Live set today: off, transitions. An unknown level is
    rejected (exit 2) — the axis is a level string, so future levels are additive."""
    level = args.level
    if level not in annotate_mod.LEVELS:
        print(f"unknown resolution; known: {', '.join(annotate_mod.LEVELS)}",
              file=sys.stderr)
        return 2
    annotate_mod.write_resolution(transport, args.team, level)
    print(f"annotate resolution for team/{args.team} -> {level}"
          + ("" if level in annotate_mod.LIVE_PROJECTING else " (no projection)"))
    return 0


def cmd_annotate_status(args: argparse.Namespace, transport: Any) -> int:
    """Print the team's resolution level + the projection cursor position."""
    team = args.team
    res = annotate_mod.read_resolution(transport, team)
    cursor = annotate_mod.read_cursor(transport, team)
    last_ts = cursor.get("last_ts")
    seen = cursor.get("seen_ids") or []
    if args.json:
        print(json.dumps({"team": team, "resolution": res,
                          "projecting": res in annotate_mod.LIVE_PROJECTING,
                          "last_ts": last_ts, "seen_ids": len(seen)}, indent=2))
    else:
        proj = "" if res in annotate_mod.LIVE_PROJECTING else "  (no projection)"
        print(f"annotate — team/{team}: resolution={res}{proj}")
        print(f"  cursor: last_ts={last_ts or '(none)'}, "
              f"{len(seen)} id(s) in the replay window")
    return 0


def cmd_annotate_project(args: argparse.Namespace, transport: Any) -> int:
    """Fold the freshly-reconciled structured transitions onto the operator's
    timeline. Reads reconcile's just-written pending transitions + the cursor
    from the bus, calls the pure fold, emits each spec via the hardened writer,
    and advances the cursor. Refuses (exit 0) when the team has not opted in."""
    team = args.team
    res = annotate_mod.read_resolution(transport, team)
    if res not in annotate_mod.LIVE_PROJECTING:
        print(f"projection off (resolution={res or 'absent'}) — "
              f"opt in with `annotate resolution {team} transitions`")
        return 0
    pending = annotate_mod.read_pending(transport, team)
    cursor = annotate_mod.read_cursor(transport, team)
    now = cli._iso(cli._now())
    specs, full_cursor = annotate_mod.project(pending, cursor, team=team, now=now)
    agent = cli._host()
    # Emit each spec and record which ids ACTUALLY landed — the cursor advances
    # PER LANDED SPEC, not all-or-nothing. A transient partial writer failure
    # (some specs land, some fail) must not re-project the succeeded specs next
    # run (that would manufacture duplicates on the no-dedup endpoint), nor drop
    # the failed ones (that would lose a transition).
    landed_ids = {s.id for s in specs if cli._emit_projection_spec(s, agent=agent)}
    emitted = len(landed_ids)

    def _persist_cursor(new_cursor: dict) -> None:
        # A swallowed cursor-write failure is a DUPLICATE vector now that pending
        # merges-and-carries: the same landed ids stay in pending, and without an
        # advanced cursor to dedup them the next beat re-emits to the no-dedup
        # endpoint. Surface it loudly rather than silently drop the write.
        if not annotate_mod.write_cursor(transport, team, new_cursor):
            cli._log.warn("annotate project: cursor write FAILED — landed ids may "
                      "re-project (duplicate risk)", team=team,
                      last_ts=new_cursor.get("last_ts"),
                      seen_ids=len(new_cursor.get("seen_ids") or []))

    if not specs or emitted == len(specs):
        # All landed (or none to emit): the fold's cursor already folds every
        # emitted id into seen_ids and advances the watermark — persist it.
        _persist_cursor(full_cursor)
    elif emitted:
        # Partial success: fold ONLY the landed ids into the cursor and hold the
        # watermark at the oldest failed spec, so the next heartbeat re-projects
        # exactly the un-landed specs and nothing else.
        partial = annotate_mod.cursor_for_landed(
            pending, cursor, landed_ids, team=team, now=now)
        _persist_cursor(partial)
    # else (emitted == 0 with specs present): total writer failure — leave the
    # cursor untouched so every spec retries next run (over-capture beats loss).
    print(f"projected {emitted}/{len(specs)} transition(s) for team/{team}")
    return 0


