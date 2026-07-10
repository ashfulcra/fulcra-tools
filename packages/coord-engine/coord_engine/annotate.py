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
  * a **cursor** ``{last_ts, seen_ids[], seen_ts{}}`` records what's been
    projected. The ``last_ts`` watermark plus a **skew lookback** define the
    boundary: a transition is emitted iff its ``ts`` is at or after
    ``last_ts - skew_margin`` AND its id is not already in ``seen_ids``. The
    lookback (not a strict ``> last_ts``) is what lets a genuinely-new transition
    that shares the watermark's ts still land, and lets ``seen_ids`` dedup the
    replay band; ``seen_ids`` is pruned by TIME — every id whose transition ts is
    within ``skew_margin`` of the (advanced) watermark is retained, everything
    older is dropped because the boundary itself already suppresses it. The margin
    is keyed to reconcile's ``FAST_PATH_SKEW_MARGIN_SECONDS`` (the same clock-skew
    budget the fast path trusts). ``seen_ts`` carries the ts of each retained id so
    the time-prune is exact; a legacy cursor without it (or an id whose ts is
    unknown/unparseable) keeps the id defensively rather than risking a re-emit.

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

``ts`` CONTRACT (required of Task 2's caller): ``ts`` MUST be a **normalized,
lexicographically-comparable ISO-8601 string** — UTC with a trailing ``Z``,
zero-padded fields (e.g. ``2026-07-09T09:00:00Z``). Both the watermark ordering
(a plain string ``>``) and the new skew-margin arithmetic (parse-to-datetime)
rely on this normalization; a non-normalized ts (local offset, missing padding)
can mis-order the watermark or fail to parse. Unparseable ts never crash and are
never silently dropped — they degrade to "emit / keep" — but they defeat the
margin math, so keep ts normalized upstream.

Optional keys enrich the human ``note``:

    {"title": str, "assignee": str, "next_action": str}

A row missing any required key (or not a dict) is **malformed**: skipped +
reported, never fatal, and the good rows around it still advance the cursor.

SEAM NOTE (Task 1 -> Task 2): ``kind`` / ``task_id`` on an emitted
:class:`AnnotationSpec` are writer-call **metadata**, NOT served record keys —
Task 2 must NOT forward them into the MomentAnnotation body (the closed typed
schema silently strips any non-served top-level key). They inform the write call
(tags, dedup) but the human-visible content lives entirely in ``note``.

Stdlib-only (plus the intra-package skew constant); folds pure; never crashes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .log import Logger, get_logger
from .reconcile import FAST_PATH_SKEW_MARGIN_SECONDS

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

#: The replay / dedup window, in seconds, for BOTH the boundary filter and the
#: ``seen_ids`` prune. Keyed to reconcile's ``FAST_PATH_SKEW_MARGIN_SECONDS`` — the
#: same clock-skew budget the fast path trusts between hosts — so projection and
#: reconcile agree on how far a transition's ts may lag the watermark and still be
#: a live candidate. A transition is emitted iff its ts is at/after
#: ``watermark - SKEW_MARGIN_SECONDS`` and its id is unseen; ``seen_ids`` retains
#: exactly the ids whose ts falls inside this band of the advanced watermark
#: (older ids need no retention — the boundary already suppresses them). Bounding
#: by TIME (not a fixed count) means an id can never be evicted while it is still
#: inside the replay band, which is what closes the double-write a count-prune
#: reopened. See ``reconcile.FAST_PATH_SKEW_MARGIN_SECONDS``.
SKEW_MARGIN_SECONDS = FAST_PATH_SKEW_MARGIN_SECONDS

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

def _parse_ts(ts: Any) -> Optional[datetime]:
    """Parse a normalized ISO-8601 ts string into a naive-UTC datetime, or None.

    Only used for the skew-margin arithmetic — the id and the watermark ordering
    keep the ORIGINAL ts string untouched. Accepts a trailing ``Z``; an aware ts
    is normalized to UTC and made naive so all comparisons share one frame. Any
    non-string / unparseable input returns None (callers treat None as
    "keep / emit", never as a reason to drop a transition). Never raises."""
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _normalize_cursor(cursor: Any) -> dict[str, Any]:
    """Coerce any input into a well-formed ``{last_ts, seen_ids[], seen_ts{}}``.

    None / garbage / partial cursors degrade to a fresh cursor rather than
    raising, so a corrupted persisted cursor never wedges the heartbeat. A legacy
    cursor without ``seen_ts`` normalizes to an empty map — its ids simply have
    unknown ts and are retained defensively by the prune."""
    if not isinstance(cursor, dict):
        return {"last_ts": None, "seen_ids": [], "seen_ts": {}}
    last_ts = cursor.get("last_ts")
    if last_ts is not None and not isinstance(last_ts, str):
        last_ts = str(last_ts)
    seen = cursor.get("seen_ids")
    if isinstance(seen, list):
        seen = [str(x) for x in seen]
    else:
        seen = []
    seen_ts_raw = cursor.get("seen_ts")
    seen_ts: dict[str, str] = {}
    if isinstance(seen_ts_raw, dict):
        for k, v in seen_ts_raw.items():
            if v is not None:
                seen_ts[str(k)] = str(v)
    return {"last_ts": last_ts, "seen_ids": seen, "seen_ts": seen_ts}


def _iter_transition_rows(transitions: Any, *, team: str, log: Logger) -> list[Any]:
    """Materialize ``transitions`` into a list of rows, accepting ANY iterable.

    The prior ``isinstance(transitions, (list, tuple))`` guard silently treated a
    generator (a perfectly valid transition source) as empty. This accepts any
    iterable — generators included — while still rejecting the genuinely-wrong
    shapes: ``None`` is empty; a str/bytes/mapping (iterating those yields chars /
    keys, not rows) or a non-iterable is treated as empty AND logged once, so a
    mis-typed caller is visible rather than silently no-op. Never raises."""
    if transitions is None:
        return []
    if isinstance(transitions, (str, bytes, dict)):
        log.warn("annotate: transitions is not a sequence of rows; treating as empty",
                 team=team, type=type(transitions).__name__)
        return []
    try:
        return list(transitions)
    except TypeError:
        log.warn("annotate: transitions is not iterable; treating as empty",
                 team=team, type=type(transitions).__name__)
        return []


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

    Emits one :class:`AnnotationSpec` per transition whose ts is *within the skew
    lookback of the cursor watermark* (``ts >= last_ts - SKEW_MARGIN_SECONDS``)
    AND whose id is *not already in ``seen_ids``*, and returns the advanced
    cursor. Deterministic: the same transitions from the same starting cursor
    always yield the same ids, so a crash mid-run or a re-run never double-writes.

    The skew lookback (not a strict ``> last_ts``) closes two defects a strict
    boundary + count-prune left open: (a) a genuinely-new transition sharing the
    watermark's ts is no longer silently dropped — it clears ``>= watermark -
    skew`` and, being a newcomer, is not in ``seen_ids``; (b) a stale re-fire can
    no longer re-emit after being evicted — ``seen_ids`` is pruned by TIME, so an
    id is never dropped while its ts is still inside the replay band, and any
    re-fire older than the band is suppressed by the boundary itself.

    ``now`` is accepted for signature symmetry with the writer path and future
    use (e.g. stamping); the id keys on the transition's own ts, never ``now``,
    so projection is independent of when the heartbeat happens to run.
    """
    return _fold(transitions, cursor, team=team, now=now, log=log, landed=None)


def cursor_for_landed(
    transitions: Any,
    cursor: Any,
    landed_ids: Any,
    *,
    team: str,
    now: str,
    log: Optional[Logger] = None,
) -> dict[str, Any]:
    """The cursor to PERSIST after a PARTIAL writer success — pure, never raises.

    ``project`` emits candidate specs assuming they all land; the CLI then calls
    the writer per spec and passes the ids that ACTUALLY landed here. Re-folds the
    same ``transitions`` from the same starting ``cursor``, but only folds a
    LANDED spec's id into ``seen_ids`` and holds the watermark at/below the oldest
    UN-landed spec's ts. The effect: every succeeded spec is dedup'd (never
    re-emitted), while every failed spec stays a live candidate next run (its id
    absent from ``seen_ids`` AND its ts still inside the skew boundary) — so a
    transient partial failure re-projects ONLY the specs that did not land, never
    manufacturing a duplicate and never losing a transition.

    Re-folds (a second in-memory pass over ``transitions``) rather than
    post-patching ``project``'s cursor, so the ``seen_ids`` TIME-prune runs
    against the correct (held-back) watermark — patching afterwards would prune
    against the wrong boundary and could reopen a re-emit."""
    landed = {str(x) for x in landed_ids} if landed_ids is not None else set()
    _, new_cursor = _fold(transitions, cursor, team=team, now=now, log=log, landed=landed)
    return new_cursor


def _fold(
    transitions: Any,
    cursor: Any,
    *,
    team: str,
    now: str,
    log: Optional[Logger],
    landed: Optional[set[str]],
) -> tuple[list[AnnotationSpec], dict[str, Any]]:
    """Shared core of :func:`project` / :func:`cursor_for_landed`.

    ``landed`` gates whether an emitted spec is treated as PERSISTED:
      * ``None`` — every emitted spec is assumed to land (``project``'s posture):
        its id folds into ``seen_ids`` and the watermark may advance past it.
      * a set — only ids IN the set are folded into ``seen_ids``; an emitted spec
        whose id is absent is a FAILED write, so its id is left out AND the
        watermark is held at/below its ts (via ``failed_floor``) so the boundary
        can't suppress its re-emit next run.
    """
    log = log or get_logger("annotate")
    norm = _normalize_cursor(cursor)
    watermark = norm["last_ts"]
    seen: list[str] = list(norm["seen_ids"])
    seen_set: set[str] = set(seen)
    # id -> original ts string, for the time-based prune. Seed from the persisted
    # cursor; refresh/extend as we (re-)encounter ids this run.
    id_ts: dict[str, str] = dict(norm["seen_ts"])

    watermark_dt = _parse_ts(watermark)
    margin = timedelta(seconds=SKEW_MARGIN_SECONDS)

    specs: list[AnnotationSpec] = []
    new_watermark = watermark
    skipped = 0
    # Oldest ts (as a normalized string) among specs that were emitted but did NOT
    # land — the watermark must not advance past it, or the boundary could drop the
    # retry. Only parseable ts participate (an unparseable-ts spec always re-emits
    # regardless of the watermark, so it needs no floor and must not corrupt it).
    failed_floor: Optional[str] = None

    for row in _iter_transition_rows(transitions, team=team, log=log):
        txn = _parse_transition(row)
        if txn is None:
            skipped += 1
            log.warn("annotate: skipping malformed transition row", team=team,
                     row=repr(row)[:200])
            continue

        ts = txn["ts"]
        ts_dt = _parse_ts(ts)
        # Advance the watermark for EVERY well-formed row, emitted or not — this
        # is what makes the re-run idempotent (next run's watermark suppresses
        # everything this run saw). Lexicographic on the normalized ISO string.
        if new_watermark is None or ts > new_watermark:
            new_watermark = ts

        ann_id = _stable_id(team, txn["task_id"], txn["kind"], ts)
        # Remember this id's ts whether we emit or dedup it — keeps the prune exact
        # for ids that arrive already in seen_ids (e.g. an async replay).
        id_ts[ann_id] = ts

        if ann_id in seen_set:
            continue

        # Boundary: emit iff ts is at/after (watermark - skew). A None watermark
        # (fresh cursor) emits everything; an unparseable watermark or ts can't be
        # margin-compared, so we KEEP (emit) rather than risk dropping a real
        # transition — never silently lose one.
        if watermark is None:
            after_watermark = True
        elif watermark_dt is None or ts_dt is None:
            after_watermark = True
        else:
            after_watermark = ts_dt >= (watermark_dt - margin)
        if not after_watermark:
            continue

        specs.append(AnnotationSpec(
            id=ann_id,
            note=_build_note(txn),
            tags=[DEFINITION_TAG, txn["kind"]],
            kind=txn["kind"],
            task_id=txn["task_id"],
            ts=ts,
        ))
        if landed is None or ann_id in landed:
            seen.append(ann_id)
            seen_set.add(ann_id)
        elif ts_dt is not None and (failed_floor is None or ts < failed_floor):
            # A failed write with a parseable ts: hold the watermark back so this
            # transition stays inside the boundary and re-projects next run.
            failed_floor = ts

    # Hold the watermark at/below the oldest un-landed spec so the boundary cannot
    # suppress its retry; only lowers (never raises) the watermark, and only under
    # a partial failure (``project`` passes ``landed=None`` -> no failed_floor).
    if failed_floor is not None and (new_watermark is None or failed_floor < new_watermark):
        new_watermark = failed_floor

    # Prune seen_ids by TIME: retain every id whose ts is within the skew margin
    # of the (possibly held-back) watermark. Ids older than that need no retention
    # — the boundary already suppresses their re-fire. An id whose ts is unknown
    # (legacy cursor) or unparseable is kept defensively (dropping it could reopen
    # a re-emit); such ids simply carry no ``seen_ts`` entry.
    new_watermark_dt = _parse_ts(new_watermark)
    lower_bound = (new_watermark_dt - margin) if new_watermark_dt is not None else None
    kept: list[str] = []
    kept_ts: dict[str, str] = {}
    for sid in seen:
        sts = id_ts.get(sid)
        sdt = _parse_ts(sts) if sts is not None else None
        if lower_bound is None or sdt is None:
            keep = True  # can't evaluate -> retain (never drop defensively)
        else:
            keep = sdt >= lower_bound
        if keep:
            kept.append(sid)
            if sts is not None:
                kept_ts[sid] = sts

    if specs or skipped:
        log.info("annotate: projected transitions", team=team,
                 emitted=len(specs), skipped=skipped, last_ts=new_watermark,
                 retained_ids=len(kept))

    new_cursor: dict[str, Any] = {"last_ts": new_watermark, "seen_ids": kept}
    # Omit an empty seen_ts so a legacy / fresh cursor round-trips unchanged.
    if kept_ts:
        new_cursor["seen_ts"] = kept_ts
    return specs, new_cursor


# ---------------------------------------------------------------------------
# Bus-persisted config + cursor + pending transitions (Task 2 seam)
# ---------------------------------------------------------------------------
#
# The pure fold above is I/O-free. These helpers are the thin, never-raising
# transport seam the CLI (`annotate resolution/status/project`) and the reconcile
# heartbeat share. They live on the BUS (not local disk) on purpose — per Ash's
# binding decision the RESOLUTION level is stored on the team's store so ANY
# host's heartbeat reads the same opt-in, unlike the writer's machine-local
# on/off config. All take a duck-typed transport (``read``/``write``) and
# tolerate absent/garbage state (a corrupt cursor never wedges the heartbeat).

#: The resolution axis is a LEVEL string, not a boolean — future levels
#: (tools / io / reasoning ...) are additive. Today exactly these two are live.
LEVELS = ("off", "transitions")

#: Levels that actually project. ``off`` and absent both mean "no projection".
LIVE_PROJECTING = frozenset({"transitions"})

#: Schema tag on the persisted pending-transitions artifact.
_PENDING_SCHEMA = "coord.teams.annotate.pending.v1"


def _annotate_prefix(team: str) -> str:
    return f"team/{team}/_coord/annotate/"


def resolution_path(team: str) -> str:
    return _annotate_prefix(team) + "resolution"


def cursor_path(team: str) -> str:
    return _annotate_prefix(team) + "cursor.json"


def pending_path(team: str) -> str:
    return _annotate_prefix(team) + "pending.json"


def read_resolution(transport: Any, team: str) -> str:
    """The team's projection resolution level from the bus, or ``"off"`` when
    absent/unreadable/unknown. Mirrors the writer's plain-text config posture.
    Never raises."""
    try:
        raw = transport.read(resolution_path(team))
    except Exception:
        return "off"
    if not raw:
        return "off"
    level = str(raw).strip()
    return level if level in LEVELS else "off"


def write_resolution(transport: Any, team: str, level: str) -> bool:
    """Persist the resolution ``level`` to the bus (plain text). Never raises."""
    try:
        return bool(transport.write(resolution_path(team), level + "\n"))
    except Exception:
        return False


def read_cursor(transport: Any, team: str) -> dict[str, Any]:
    """The persisted projection cursor, or a fresh cursor when absent/garbage.
    (The fold's ``_normalize_cursor`` is the second line of defense.) Never raises."""
    fresh = {"last_ts": None, "seen_ids": [], "seen_ts": {}}
    try:
        raw = transport.read(cursor_path(team))
        if not raw:
            return fresh
        data = json.loads(raw)
    except Exception:
        return fresh
    return data if isinstance(data, dict) else fresh


def write_cursor(transport: Any, team: str, cursor: dict[str, Any]) -> bool:
    """Persist the advanced cursor to the bus (JSON). Never raises."""
    try:
        return bool(transport.write(cursor_path(team), json.dumps(cursor, indent=2)))
    except Exception:
        return False


def read_pending(transport: Any, team: str) -> list[Any]:
    """The structured transitions reconcile last persisted for projection, or
    ``[]`` when absent/garbage. Never raises."""
    try:
        raw = transport.read(pending_path(team))
        if not raw:
            return []
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        txns = data.get("transitions")
        return txns if isinstance(txns, list) else []
    return data if isinstance(data, list) else []


def _pending_dedup_id(team: str, row: Any) -> Optional[str]:
    """The projection id a transition ROW folds to — the SAME
    ``_stable_id(team, task_id, kind, ts)`` the fold computes, via the SAME
    ``_parse_transition`` normalization. Used to reconcile prior pending rows
    against the cursor's ``seen_ids`` in the merge. ``None`` for a malformed row
    (the fold would skip it too, so carrying it is pointless). Never raises."""
    txn = _parse_transition(row)
    if txn is None:
        return None
    return _stable_id(team, txn["task_id"], txn["kind"], txn["ts"])


def write_pending(transport: Any, team: str, transitions: list[Any], *, now: str) -> bool:
    """Persist structured transitions for a later ``annotate project`` (a SEPARATE
    CLI invocation on the heartbeat, so transitions must round-trip through the
    bus). MERGE-AND-CARRY, never a blind overwrite: the deployed topology runs
    ``reconcile && annotate project`` every beat, so a reconcile that fires
    between two projections must NOT wipe a transition the fold is still holding
    live (an un-landed row after a partial/failed writer pass). Both loss
    sequences the review flagged are closed here:

      * single-host — a new transition forces a full pass whose diff is just
        ``[new]``; the prior failed row would vanish under a blind overwrite;
      * empty-diff — a pass that observes no change diffs ``[]``; a blind
        overwrite would replace a live pending with ``[]``.

    So: union this pass's ``transitions`` with prior pending rows whose id is NOT
    in the cursor's ``seen_ids`` (i.e. not yet landed) and not already superseded
    by a row in this pass. NEVER replace a non-empty pending with an empty set —
    if the merge is empty but prior pending was not, leave it be. Carried stale
    rows are safe: the cursor dedups landed ids, the fold's skew boundary
    suppresses ancient ones, and re-reading an all-landed pending is a proven
    no-op. Never raises."""
    try:
        new_rows = list(transitions)
        prior = read_pending(transport, team)
        seen = set((read_cursor(transport, team) or {}).get("seen_ids") or [])
        new_ids = {i for i in (_pending_dedup_id(team, r) for r in new_rows) if i}
        carried = []
        for row in prior:
            rid = _pending_dedup_id(team, row)
            if rid is None or rid in seen or rid in new_ids:
                continue  # malformed / already landed / superseded this pass
            carried.append(row)
        merged = new_rows + carried
        if not merged and prior:
            # Nothing new and everything prior has landed (or was malformed):
            # do NOT wipe a non-empty pending — a re-read of it is a no-op, and
            # not-overwriting closes the empty-diff drop unconditionally.
            return True
        doc = {"schema": _PENDING_SCHEMA, "generated_at": now, "transitions": merged}
        return bool(transport.write(pending_path(team), json.dumps(doc, indent=2)))
    except Exception:
        return False
