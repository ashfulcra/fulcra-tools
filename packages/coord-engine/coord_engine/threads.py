"""Dropped-threads classification — the PURE fold (no transport).

Design: ``docs/superpowers/specs/2026-07-11-dropped-threads-design.md`` (codex-
APPROVED). Answers "what work-in-progress has Ash dropped?" over a NEUTRAL row
shape a single bus adapter (in cli.py) produces — so adding a GitHub/fulcra-pm
source later is a new adapter emitting the same rows, never a rewrite.

A thread is dropped in exactly THREE modes; classification is MUTUALLY EXCLUSIVE,
one mode per thread, FIRST MATCH WINS:

  1. **intent carve-out (mode 3)** — an item carrying an ``intent:<principal>``
     tag is ONLY EVER a mode-3 candidate: INVISIBLE until its window
     (``intent_by`` if declared, else capture + ``intent_grace_hours``) passes,
     mode 3 after — UNLESS followed up. Its assignee/age never trigger modes 1-2
     (an unripe commitment must not surface at all — the nagging-failure guard).
     Follow-up suppression (any ONE suffices): status advanced past ``proposed``;
     a response shard exists; a ``followed-up-by:<slug>`` tag is present. Carried
     on the row as ``followup: {status, responded, followup_ref}``.
  2. **blocked-on-principal (mode 2)** — a NON-intent item whose progress waits on
     the principal (``assignee: ash``, ``blocked-on:ash`` tag, or a ``needs:human``
     block naming him). Surfaced IMMEDIATELY, no aging; being awaited-now dominates
     aged silence. ``evidence`` notes the age when it ALSO exceeds the silence
     window.
  3. **started-then-silent (mode 1)** — a remaining principal item (owns / last
     touched) whose ash-activity is older than the silence window. Deliberately
     parked items (``@backlog`` audience, a future ``not_before``) are excluded by
     construction.

Output object per thread: ``{mode, id, title, age, window, evidence}`` — ``age`` in
days (float; days since the mode's reference instant), ``window`` the ISO threshold
instant (mode 3 window / mode 1 silence cutoff; None for mode 2), ``evidence`` an
HONEST string naming the signals that fired and FLAGGING timestamp-fallback
attribution. The list is grouped by mode ascending, oldest-first within a mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .model import TERMINAL_STATUSES

__all__ = ["classify"]


def _parse(ts: Optional[str]) -> Optional[datetime]:
    """ISO-8601 (``Z`` or offset) -> aware UTC datetime, or None. Never raises —
    an unparseable/blank ts is simply "unknown", never a crash."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _age_days(ref: datetime, now: datetime) -> float:
    return (now - ref).total_seconds() / 86400.0


def _tags(row: dict[str, Any]) -> list[str]:
    tags = row.get("tags")
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _followup(row: dict[str, Any]) -> dict[str, Any]:
    fu = row.get("followup")
    if not isinstance(fu, dict):
        fu = {}
    return {
        "status": str(fu.get("status") or "proposed"),
        "responded": bool(fu.get("responded")),
        "followup_ref": fu.get("followup_ref") or None,
    }


def _is_followed_up(fu: dict[str, Any]) -> bool:
    """Three-signal suppression (spec follow-up contract): ANY one discharges the
    intent. (a) status advanced past ``proposed``; (b) a response shard exists;
    (c) a ``followed-up-by:<slug>`` tag names the discharging artifact."""
    return fu["status"] != "proposed" or fu["responded"] or bool(fu["followup_ref"])


def classify(
    rows: list[dict[str, Any]],
    *,
    now: str,
    silence_days: float,
    intent_grace_hours: float,
) -> list[dict[str, Any]]:
    """Classify neutral adapter rows into dropped-thread objects. PURE: no
    transport, no clock — ``now`` is injected, so this is exhaustively unit-
    testable. See the module docstring for the row shape + mode contract.

    ``silence_days`` bounds mode-1 aging + the mode-2 "also aged" note;
    ``intent_grace_hours`` is the mode-3 window when an intent declares no
    ``intent_by``. Rows whose signals match no mode (fresh owned item, unripe
    intent, item still being worked) yield NOTHING — never a false drop."""
    now_dt = _parse(now)
    if now_dt is None:  # a caller with an unparseable clock gets nothing, not a crash
        return []
    silence = timedelta(days=float(silence_days))
    grace = timedelta(hours=float(intent_grace_hours))
    out: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        # Parked by construction: @backlog audience, or a future not_before gate.
        if row.get("parked"):
            continue
        nb = _parse(row.get("not_before"))
        if nb is not None and nb > now_dt:
            continue

        rid = str(row.get("id") or "")
        title = str(row.get("title") or rid)

        # --- Mode 3 carve-out: an intent item is ONLY EVER mode 3 --------------
        if row.get("intent"):
            declared = _parse(row.get("declared_window"))
            if declared is not None:
                window = declared
                window_note = f"declared window {_iso(window)}"
            else:
                captured = _parse(row.get("captured_ts"))
                if captured is None:
                    continue  # no window, no capture time -> cannot ripen; stay silent
                window = captured + grace
                window_note = (f"window {_iso(window)} "
                               f"(capture+{_fmt_num(intent_grace_hours)}h)")
            if window > now_dt:
                continue  # UNRIPE -> invisible entirely (never mode 1/2)
            if _is_followed_up(_followup(row)):
                continue  # followed up -> discharged, suppressed
            age = _age_days(window, now_dt)
            out.append({
                "mode": 3, "id": rid, "title": title,
                "age": round(age, 2), "window": _iso(window),
                "evidence": f"intent past {window_note}; not followed up",
            })
            continue

        # --- Non-intent -------------------------------------------------------
        if str(row.get("status") or "") in TERMINAL_STATUSES:
            continue  # a closed item is not a dropped thread

        # Mode 2: blocked-on-principal dominates aged silence, no aging.
        if row.get("blocked_on_principal"):
            signal = str(row.get("blocked_signal") or "blocked on ash")
            act = _parse(row.get("ash_activity_ts"))
            age = _age_days(act, now_dt) if act is not None else None
            evidence = f"blocked on ash ({signal})"
            if age is not None and (now_dt - act) >= silence:
                evidence += (f"; also silent {_fmt_num(age)}d "
                             f"(exceeds {_fmt_num(silence_days)}d window)")
            out.append({
                "mode": 2, "id": rid, "title": title,
                "age": round(age, 2) if age is not None else None,
                "window": None, "evidence": evidence,
            })
            continue

        # Mode 1: remaining principal items age into silence.
        act = _parse(row.get("ash_activity_ts"))
        if act is None:
            continue  # no usable activity ts -> cannot prove staleness; no false drop
        if (now_dt - act) < silence:
            continue  # still fresh -> not dropped
        age = _age_days(act, now_dt)
        if row.get("ash_activity_attributed", True):
            source = str(row.get("ash_activity_source") or "ash activity")
            attribution = f"attributed via {source}"
        else:
            attribution = "no ash-attributable event — item timestamp fallback"
        out.append({
            "mode": 1, "id": rid, "title": title,
            "age": round(age, 2), "window": _iso(act + silence),
            "evidence": f"silent {_fmt_num(age)}d since {_iso(act)}; {attribution}",
        })

    # Group by mode ascending; oldest-first (largest age) within a mode, None last.
    out.sort(key=lambda o: (o["mode"], -(o["age"] if o["age"] is not None else -1.0)))
    return out


def _fmt_num(n: float) -> str:
    """Compact one-decimal render for ages/windows in evidence (``5.0``, ``1.5``)."""
    return f"{float(n):.1f}"
