"""Environment-driven activity annotations — the pure projection fold.

``reconcile`` already computes task ``transitions`` and merges them into
``log.md``. This module projects those transitions onto the operator's Fulcra
timeline **mechanically** — from the heartbeat, model-free, platform-agnostic —
so a transition made by ANY agent/host/harness (not just one running the
in-process writer) still lands as a timeline annotation.

This file is the *fold only*: ``project(transitions, cursor, *, team, now)`` is
pure (no I/O, never raises) and deterministic. The CLI wiring, the opt-in gate,
and the actual record write (via the hardened ``fulcra_common.annotations``
writer) live in Task 2's ``cli.py``.

IDEMPOTENCY (the load-bearing property)
---------------------------------------
The typed ingest endpoint has NO server-side dedup and is async, and it silently
strips any non-served top-level key. So the fold must be idempotent on the
client:

  * every projected annotation carries a **deterministic id** keyed on
    ``(team, task_id, kind, ts)`` — a stdlib ``hashlib`` digest, NOT the builtin
    ``hash()`` (which is per-process salted and would differ across hosts/runs);
  * a **cursor** ``{last_ts, seen_ids[]}`` records what's been projected. The
    ``last_ts`` watermark suppresses transitions at/behind it; ``seen_ids`` is a
    bounded recent-id window that ALSO suppresses a re-fire whose ts landed at or
    after the watermark (clock skew / a rewound watermark / a duplicate row).

TRANSITION ROW SHAPE
--------------------
Each transition is a structured dict. reconcile's ``aggregate.diff_rows`` today
renders transitions as markdown *bullet strings* (``* **Update**: [t](t.md) …``)
for ``log.md``; those strings do NOT carry ``task_id`` / ``kind`` / ``ts`` /
``assignee`` / ``next_action``, which the annotation id + note require. So the
fold consumes the *structured* form of a transition — the caller (Task 2) is
responsible for producing it (by extending the diff to emit structured rows, or
by joining parsed ``log.md`` bullets back to their task rows). Required keys:

    {"task_id": str, "kind": str, "ts": str}      # ts = ISO-8601 transition time

Optional keys enrich the human ``note``:

    {"title": str, "assignee": str, "next_action": str}

A row missing any required key (or not a dict) is **malformed**: skipped +
reported, never fatal, and the good rows around it still advance the cursor.

Stdlib-only; folds pure; never crashes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from .log import Logger, get_logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The definition tag every projected annotation groups under — mirrors the
#: in-process writer's ``cli_tags[0]`` ("agent-tasks") so projected moments land
#: on the SAME Agent-Tasks track as writer-emitted ones.
DEFINITION_TAG = "agent-tasks"

#: Provenance marker identifying a moment as coord-*projected* (from the
#: reconcile heartbeat) rather than writer-emitted. Task 2 attaches this to the
#: record's ``sources`` array alongside the resolved definition uuid (which the
#: pure fold cannot know), mirroring the writer's ``sources`` provenance style.
SOURCE_MARKER = "com.fulcradynamics.fulcra-coord.projection"

#: Bound on ``cursor.seen_ids``. seen_ids exists ONLY to catch a re-fire whose
#: ts landed at/after the watermark (async replay, clock skew, a rewound
#: watermark, a duplicate row) — a short-lived race window, not durable history.
#: The ``last_ts`` watermark already suppresses the ordinary case, so this window
#: just needs to comfortably span the transitions a few heartbeats apart could
#: replay. 512 ids covers many heartbeats of a busy team while keeping the
#: persisted cursor small. Pruning keeps the MOST RECENT ids (newest emits are
#: the ones an async replay is most likely to double-fire).
SEEN_IDS_WINDOW = 512

#: Required keys on a structured transition row.
_REQUIRED_KEYS = ("task_id", "kind", "ts")


# ---------------------------------------------------------------------------
# AnnotationSpec — what the fold emits per projected transition
# ---------------------------------------------------------------------------

@dataclass
class AnnotationSpec:
    """One projected timeline annotation, ready for Task 2 to hand to the
    hardened writer.

    Field discipline: ``note`` / ``tags`` map straight onto served
    MomentAnnotation columns ({note, recorded_at, tags, sources, id}); ``ts``
    becomes ``recorded_at`` and ``id`` is the deterministic dedup key. ``kind``
    and ``task_id`` are metadata the writer call needs but that are NOT served as
    their own columns — the human summary (task title + kind + assignee +
    next-action) is folded entirely into ``note``, since ``title`` is silently
    stripped by the typed endpoint.
    """

    id: str
    note: str
    tags: list[str]
    kind: str
    task_id: str
    ts: str


# ---------------------------------------------------------------------------
# Deterministic id
# ---------------------------------------------------------------------------

#: Field separator for the id key — an ASCII unit separator, which can't occur in
#: a team/task-id/kind/ts and so can't blur two distinct keys into one digest.
_KEY_SEP = "\x1f"


def _stable_id(team: str, task_id: str, kind: str, ts: str) -> str:
    """A process-stable annotation id from ``(team, task_id, kind, ts)``.

    Uses ``hashlib.sha256`` — NOT the builtin ``hash()``, which is salted per
    process (``PYTHONHASHSEED``) and would produce a DIFFERENT id on every host
    and every run, defeating cross-host dedup against the no-dedup endpoint."""
    key = _KEY_SEP.join((str(team), str(task_id), str(kind), str(ts)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Cursor + transition normalization (tolerant; never raises)
# ---------------------------------------------------------------------------

def _normalize_cursor(cursor: Any) -> dict[str, Any]:
    """Coerce any input into a well-formed ``{last_ts, seen_ids[]}`` cursor.

    None / garbage / partial cursors degrade to a fresh cursor rather than
    raising, so a corrupted persisted cursor never wedges the heartbeat."""
    if not isinstance(cursor, dict):
        return {"last_ts": None, "seen_ids": []}
    last_ts = cursor.get("last_ts")
    if last_ts is not None and not isinstance(last_ts, str):
        last_ts = str(last_ts)
    seen = cursor.get("seen_ids")
    if isinstance(seen, list):
        seen = [str(x) for x in seen]
    else:
        seen = []
    return {"last_ts": last_ts, "seen_ids": seen}


def _parse_transition(row: Any) -> Optional[dict[str, str]]:
    """Normalize one transition row, or return None if it is malformed.

    Malformed = not a dict, or missing any of ``task_id`` / ``kind`` / ``ts``
    (each must be truthy). Optional ``title`` / ``assignee`` / ``next_action``
    default to sensible fallbacks. Never raises."""
    if not isinstance(row, dict):
        return None
    task_id = row.get("task_id") or row.get("id")
    kind = row.get("kind")
    ts = row.get("ts") or row.get("recorded_at") or row.get("at")
    if not task_id or not kind or not ts:
        return None
    return {
        "task_id": str(task_id),
        "kind": str(kind),
        "ts": str(ts),
        "title": str(row.get("title") or row.get("name") or task_id),
        "assignee": str(row.get("assignee") or ""),
        "next_action": str(row.get("next_action") or ""),
    }


def _build_note(txn: dict[str, str]) -> str:
    """One-line human summary folded ENTIRELY into ``note`` — the only served
    free-text slot (``title`` is stripped by the typed endpoint). Carries task
    title + transition kind + assignee + next-action."""
    parts = [f"{txn['kind']}: {txn['title']}"]
    if txn["assignee"]:
        parts.append(f"assignee: {txn['assignee']}")
    if txn["next_action"]:
        parts.append(f"next: {txn['next_action']}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------

def project(
    transitions: Any,
    cursor: Any,
    *,
    team: str,
    now: str,
    log: Optional[Logger] = None,
) -> tuple[list[AnnotationSpec], dict[str, Any]]:
    """Project task ``transitions`` onto timeline annotations (pure, never raises).

    Emits one :class:`AnnotationSpec` per transition that is *after the cursor
    watermark* AND *not already in ``seen_ids``*, and returns the advanced cursor.
    Deterministic: the same transitions from the same starting cursor always
    yield the same ids, so a crash mid-run or a re-run never double-writes.

    ``now`` is accepted for signature symmetry with the writer path and future
    use (e.g. stamping); the id keys on the transition's own ts, never ``now``,
    so projection is independent of when the heartbeat happens to run.
    """
    log = log or get_logger("annotate")
    norm = _normalize_cursor(cursor)
    watermark = norm["last_ts"]
    seen: list[str] = list(norm["seen_ids"])
    seen_set: set[str] = set(seen)

    specs: list[AnnotationSpec] = []
    new_watermark = watermark
    skipped = 0

    for row in transitions if isinstance(transitions, (list, tuple)) else []:
        txn = _parse_transition(row)
        if txn is None:
            skipped += 1
            log.warn("annotate: skipping malformed transition row", team=team,
                     row=repr(row)[:200])
            continue

        ts = txn["ts"]
        # Advance the watermark for EVERY well-formed row, emitted or not — this
        # is what makes the re-run idempotent (next run's watermark suppresses
        # everything this run saw).
        if new_watermark is None or ts > new_watermark:
            new_watermark = ts

        ann_id = _stable_id(team, txn["task_id"], txn["kind"], ts)

        # Filter: strictly after the watermark AND not already projected.
        after_watermark = watermark is None or ts > watermark
        if not after_watermark or ann_id in seen_set:
            continue

        specs.append(AnnotationSpec(
            id=ann_id,
            note=_build_note(txn),
            tags=[DEFINITION_TAG, txn["kind"]],
            kind=txn["kind"],
            task_id=txn["task_id"],
            ts=ts,
        ))
        seen.append(ann_id)
        seen_set.add(ann_id)

    # Bound the seen_ids window (keep the most-recent ids).
    if len(seen) > SEEN_IDS_WINDOW:
        seen = seen[-SEEN_IDS_WINDOW:]

    if specs or skipped:
        log.info("annotate: projected transitions", team=team,
                 emitted=len(specs), skipped=skipped, last_ts=new_watermark)

    return specs, {"last_ts": new_watermark, "seen_ids": seen}
