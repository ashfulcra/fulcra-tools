"""Live precision check for a draft rule (pure over pre-fetched candidates).

The route layer fetches candidate messages (``build_query`` → ``list_message_ids``
→ ``get_message``); this module runs the SAME engine decision (``rules.evaluate``)
over them so the preview equals what the poller would do, and cross-references the
operator's ✓/✗ selections. No network, no logging of content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import convert, rules

#: ``from:`` / ``-from:`` operators pulled out of the server ``match`` query so
#: the pure preview can honor the sender constraint locally (candidates handed
#: to this function are not pre-filtered by the server ``q``). Only the sender
#: operator is modeled — it's the dominant derived term; other query operators
#: are left to the server ``q`` at route time.
_FROM_TERM_RE = re.compile(r"(-?)from:(\S+)", re.IGNORECASE)


@dataclass
class PreviewResult:
    match_count: int = 0
    sample: list[dict] = field(default_factory=list)
    positives_caught: list[str] = field(default_factory=list)
    negatives_caught: list[str] = field(default_factory=list)


def _query_from_ok(match_query: str, from_header: str) -> bool:
    """Apply the ``from:``/``-from:`` operators of ``match_query`` to a From value.

    Returns False when a required ``from:`` substring is absent or an excluded
    ``-from:`` substring is present (case-insensitive substring test, matching
    Gmail's forgiving sender matching well enough for a precision preview).
    """
    hay = (from_header or "").lower()
    for neg, term in _FROM_TERM_RE.findall(match_query or ""):
        needle = term.lower()
        present = needle in hay
        if neg and present:
            return False
        if not neg and not present:
            return False
    return True


def _matches(rule, msg: dict, account_id: str) -> bool:
    """True iff ``msg`` is an effective match — the sender-query filter AND the
    engine post-filter decision (:func:`rules.evaluate`)."""
    from_header = convert.get_header(msg.get("payload", {}), "From") or ""
    if not _query_from_ok(rule.match, from_header):
        return False
    return rules.evaluate(rule, msg, account_id=account_id).matched


def _matched_ids(rule, pool: list[dict], account_id: str) -> list[str]:
    """Ordered, de-duplicated ids from ``pool`` that match ``rule``."""
    out: list[str] = []
    seen: set[str] = set()
    for msg in pool:
        mid = msg.get("id", "")
        if mid in seen:
            continue
        seen.add(mid)
        if _matches(rule, msg, account_id):
            out.append(mid)
    return out


def preview(
    rule_dict: dict,
    candidates: list[dict],
    account_id: str,
    positives: set[str],
    negatives: set[str],
    *,
    sample_limit: int = 10,
    label_candidates: list[dict] | None = None,
) -> PreviewResult:
    """Precision-check a draft rule.

    ``candidates`` is the bounded server-query page — it drives ``match_count``
    and ``sample`` (a representative window of the inbox).

    ``label_candidates`` are the operator's ✓/✗ messages fetched DIRECTLY by id
    (independent of the truncated query page); they drive
    ``positives_caught``/``negatives_caught`` so a labeled example that matches
    the draft but sorts past page 1 is still verified. Defaults to
    ``candidates`` when not supplied (keeps the pure-unit contract).
    """
    (rule,) = rules.parse_rules([rule_dict])  # raises ValueError on bad rule
    res = PreviewResult()
    matched_ids: list[str] = []
    for msg in candidates:
        mid = msg.get("id", "")
        if not _matches(rule, msg, account_id):
            continue
        matched_ids.append(mid)
        if len(res.sample) < sample_limit:
            res.sample.append({
                "message_id": mid,
                "from": convert.get_header(msg.get("payload", {}), "From") or "",
                "subject": convert.get_header(msg.get("payload", {}), "Subject") or "",
            })
    res.match_count = len(matched_ids)

    label_pool = candidates if label_candidates is None else label_candidates
    label_matched = _matched_ids(rule, label_pool, account_id)
    res.positives_caught = [m for m in label_matched if m in positives]
    res.negatives_caught = [m for m in label_matched if m in negatives]
    return res
