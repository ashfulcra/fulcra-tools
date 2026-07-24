"""Review verdict tally — the deterministic core of the fulcra-agent-review skill.

Requesting a review and submitting a verdict are single-file writes (prose). Folding
multiple reviewers' verdicts into an overall state is a fold → code. Pure functions
here; the I/O wrapper + CLI live in ``cli.py``.
"""

from __future__ import annotations

import re
from typing import Any, Optional

APPROVED = "APPROVED"
CHANGES = "CHANGES"
PENDING = "PENDING"

_APPROVE = {"approve", "approved", "lgtm"}
_CHANGES = {"changes", "request-changes", "reject", "rejected"}
_EXACT_HEAD = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def normalize_head(value: Any) -> Optional[str]:
    """Return a canonical exact commit id, or ``None``.

    BUS-86 review rounds are keyed by a full Git object id, never by a moving
    branch name or abbreviated SHA. SHA-1 and SHA-256 object ids are accepted.
    """
    head = str(value or "").strip().lower()
    return head if _EXACT_HEAD.fullmatch(head) else None


def verdict_filename(reviewer: str, *, head: Optional[str] = None) -> str:
    """Filename for one requirement's verdict in the active review round."""
    return f"{head}--{reviewer}.md" if head else f"{reviewer}.md"


def reviewer_from_filename(name: str, *, head: Optional[str] = None) -> Optional[str]:
    """Decode the requirement token from a verdict filename for ``head``.

    Head-keyed reviews ignore every superseded head before reading its shard.
    Legacy unkeyed reviews retain the historical ``<reviewer>.md`` layout.
    """
    if not name.endswith(".md"):
        return None
    stem = name[:-3]
    if head:
        prefix = f"{head}--"
        if not stem.startswith(prefix):
            return None
        return stem[len(prefix):] or None
    return stem if "--" not in stem else None


def normalize_verdict(v: Optional[str]) -> Optional[str]:
    s = (v or "").strip().lower()
    if s in _APPROVE:
        return "approve"
    if s in _CHANGES:
        return "changes"
    return None


def tally(
    verdicts: list[dict[str, Any]], *, required: Optional[list[str]] = None
) -> dict[str, Any]:
    """Fold reviewer verdicts into an overall state.

    - **CHANGES** if any reviewer requests changes (a single blocker dominates).
    - **APPROVED** if there's at least one approval, no outstanding changes, and —
      when ``required`` reviewers are named — all of them have approved.
    - **PENDING** otherwise (no verdicts, or required reviewers haven't voted).
    """
    by_reviewer: dict[str, str] = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        nv = normalize_verdict(v.get("verdict"))
        who = str(v.get("reviewer") or "")
        if nv and who:
            by_reviewer[who] = nv  # last verdict per reviewer wins
    approvals = [r for r, d in by_reviewer.items() if d == "approve"]
    changes = [r for r, d in by_reviewer.items() if d == "changes"]
    if changes:
        state = CHANGES
    elif approvals and (not required or all(r in approvals for r in required)):
        state = APPROVED
    else:
        state = PENDING
    return {
        "state": state,
        "approvals": sorted(approvals),
        "changes": sorted(changes),
        "required": required or [],
        "pending_required": sorted(r for r in (required or []) if r not in by_reviewer),
    }


def is_pending_for(pending_required: list, agent: str,
                   role_holders: "dict[str, list[str]] | None" = None) -> bool:
    """True iff agent owes a verdict: it is named directly in
    pending_required, or a name there is a ROLE whose fresh lease holders
    (per role_holders) include the agent. Role-routing doctrine: review
    requests SHOULD name roles, not identities — this matcher honors both."""
    for r in pending_required or []:
        if r == agent:
            return True
        if agent in (role_holders or {}).get(r, ()):
            return True
    return False
