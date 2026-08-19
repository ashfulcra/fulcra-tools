"""The RECORD FIELD CONTRACT — one place, verified against the real transport.

Why this module exists at all, rather than each caller doing `row["id"]`:

    A `record_id`-vs-`id` mismatch poisoned every row of a fold while its suite
    stayed green, because the suite's fake emitted the field the fold wanted
    instead of the field the transport actually returns (the PR 175 rounds).

So the names live here once, and `tests/test_wire_contract.py` asserts them
against a REAL captured `fulcra-api get-records` row rather than a hand-built
dict. If the platform renames a field, that test fails — not a hundred call
sites silently reading `None`.

Observed 2026-08-18 from a live `MomentAnnotation/<channel>` read, top level:

    id, metadata, note, recorded_at, source_id, source_name, sources, tags

Note what is NOT there: `record_id`, `timestamp`, `body`, `author`.
"""
from typing import Any, Optional

#: Stable identity of a record. THE dedup key for at-least-once queue folds.
#: It is `id`. It has never been `record_id`; that name is the bug this module
#: exists to make impossible.
F_ID = "id"
#: ISO-8601 instant the platform stamped. Not `timestamp`, not `ts`.
F_RECORDED_AT = "recorded_at"
#: Payload. For bus/mesh traffic this is a JSON *string*, not an object.
F_NOTE = "note"
#: List of source identifiers. Mixed: reverse-DNS platform sources plus, when a
#: writer supplied one, a bare agent name.
F_SOURCES = "sources"
F_TAGS = "tags"

#: Every field this module promises to find on a record row.
REQUIRED_FIELDS = (F_ID, F_RECORDED_AT, F_NOTE, F_SOURCES)


def record_id(row: dict[str, Any]) -> Optional[str]:
    """The dedup key, or None when absent.

    None is a real answer meaning "this row cannot be deduped", NOT a synonym
    for a missing key we can paper over — a caller that cannot identify a row
    must treat its queue position as UNKNOWN rather than replay or skip it.
    """
    v = row.get(F_ID)
    return str(v) if v else None


def recorded_at(row: dict[str, Any]) -> Optional[str]:
    v = row.get(F_RECORDED_AT)
    return str(v) if v else None


def note_text(row: dict[str, Any]) -> Optional[str]:
    v = row.get(F_NOTE)
    return v if isinstance(v, str) and v.strip() else None


def sender(row: dict[str, Any]) -> Optional[str]:
    """The writing agent: the BARE (non-reverse-DNS) entry in ``sources``.

    Returns None when no bare entry exists — which happens for real: rows written
    by platform projections carry only reverse-DNS sources. That is UNKNOWN
    authorship, and the caller must say so rather than pick the first element
    and call it a name. Verified against a live projection row on 2026-08-18.
    """
    for s in row.get(F_SOURCES) or []:
        s = str(s)
        if s and "." not in s:
            return s
    return None


def missing_fields(row: dict[str, Any]) -> list[str]:
    """Which promised fields this row lacks. Empty list == contract satisfied.

    Used by `mesh doctor` to fail LOUD on a transport whose shape drifted,
    instead of folding a wall of silent Nones.
    """
    return [f for f in REQUIRED_FIELDS if f not in row]


def parse_time(value: Optional[str]) -> Optional["datetime.datetime"]:
    """Parse a row's ``recorded_at`` into an aware UTC datetime, or None.

    None means UNPARSEABLE and callers must treat it as UNKNOWN — never as
    "sorts first" or "sorts last", both of which are silent guesses about
    position. Real values seen live carry either microseconds or not, and end
    in ``+00:00``; a trailing ``Z`` is accepted too.
    """
    import datetime as _dt
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive timestamp does not identify an absolute instant, and assuming
        # UTC is a silent guess that can reorder rows against a peer running in
        # any other zone (codex-coder on bd747f37). UNKNOWN, not "probably UTC".
        return None
    return parsed.astimezone(_dt.timezone.utc)


def sort_key(row) -> Optional[tuple]:
    """The TOTAL order key for a row: ``(recorded_at, id)``.

    None when the row cannot be placed — no id, or a timestamp that is absent,
    unparseable, or timezone-naive.

    WHY A SECONDARY KEY (codex-coder on bd747f37). An earlier version accepted
    equal timestamps as "monotonic non-decreasing", reasoning that two records
    can share a second and that rejecting ties would degrade a healthy read.
    Both halves were true and the conclusion was still wrong: timestamps alone
    are a PARTIAL order, so any permutation of a tied group passes the check,
    and the last row of a tied group is an arbitrary choice of cursor anchor —
    which is the loss-and-replay defect all over again, just narrowed to ties.

    The record id breaks ties deterministically. It is ours to compute, so the
    order does not depend on the platform sorting ties any particular way — an
    assumption this package has now been burned by twice.
    """
    when = parse_time(recorded_at(row) if isinstance(row, dict) else None)
    rid = record_id(row) if isinstance(row, dict) else None
    if when is None or not rid:
        return None
    return (when, rid)


def total_order(rows: list) -> Optional[list]:
    """Rows in canonical ``(recorded_at, id)`` order, or None if any row cannot
    be placed.

    Returning a canonical order rather than merely checking the transport's is
    the point: a cursor advanced to the last element of THIS list anchors to the
    newest record by an order we computed, so a descending or disordered
    response can no longer choose the anchor for us.
    """
    keys = [sort_key(r) for r in rows]
    if any(k is None for k in keys):
        return None
    return [r for _, r in sorted(zip(keys, rows), key=lambda pair: pair[0])]
