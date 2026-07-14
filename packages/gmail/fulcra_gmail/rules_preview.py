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


def preview(
    rule_dict: dict,
    candidates: list[dict],
    account_id: str,
    positives: set[str],
    negatives: set[str],
    *,
    sample_limit: int = 10,
) -> PreviewResult:
    (rule,) = rules.parse_rules([rule_dict])  # raises ValueError on bad rule
    res = PreviewResult()
    matched_ids: list[str] = []
    for msg in candidates:
        mid = msg.get("id", "")
        from_header = convert.get_header(msg.get("payload", {}), "From") or ""
        if not _query_from_ok(rule.match, from_header):
            continue
        decision = rules.evaluate(rule, msg, account_id=account_id)
        if not decision.matched:
            continue
        matched_ids.append(mid)
        if len(res.sample) < sample_limit:
            res.sample.append({
                "message_id": mid,
                "from": convert.get_header(msg.get("payload", {}), "From") or "",
                "subject": convert.get_header(msg.get("payload", {}), "Subject") or "",
            })
    res.match_count = len(matched_ids)
    res.positives_caught = [m for m in matched_ids if m in positives]
    res.negatives_caught = [m for m in matched_ids if m in negatives]
    return res
