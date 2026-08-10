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


def accepted_vocabulary() -> str:
    """The verdict tokens that count, rendered for an error message.

    Public because a caller that has to reach into ``_APPROVE`` to tell a
    reviewer why their verdict was ignored will eventually drift from it — and
    a stale list in that message is worse than none, since it sends the reviewer
    to re-file with another token that also does not count.
    """
    return (f"{'|'.join(sorted(_APPROVE))} (approve) / "
            f"{'|'.join(sorted(_CHANGES))} (changes)")


def normalize_head(value: Any) -> Optional[str]:
    """Return a canonical exact commit id, or ``None``.

    BUS-86 review rounds are keyed by a full Git object id, never by a moving
    branch name or abbreviated SHA. SHA-1 and SHA-256 object ids are accepted.
    """
    head = str(value or "").strip().lower()
    return head if _EXACT_HEAD.fullmatch(head) else None


#: The APPEND-ONLY verdict suffix: `--<iso>-<digest>` before `.md`.
#:
#: Two forms are first-class, forever (coord-boss ruling b99fb8da):
#:   PLAIN   `<head>--<reviewer>.md`                  — hand-writers, unchanged
#:   APPEND  `<head>--<reviewer>--<iso>-<digest>.md`  — the verb
#:
#: The verb uses the append form because this store has no create-if-absent and
#: no versioned write, so writing a SHARED name is check-then-write and cannot
#: protect evidence: codex-reviewer reproduced a concurrent CHANGES being
#: overwritten by APPROVE with rc 0 (595 r2). A unique name never touches an
#: existing file, which closes verb-vs-verb AND verb-vs-hand races without any
#: store primitive. The plain form keeps working untouched — no migration, and
#: nobody writing shards by hand breaks on ship day.
_APPEND_SUFFIX = re.compile(
    r"--(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)-(?P<digest>[0-9a-f]{6,16})$")


def verdict_filename(reviewer: str, *, head: Optional[str] = None,
                     ts: Optional[str] = None,
                     digest: Optional[str] = None) -> str:
    """Filename for one requirement's verdict in the active review round.

    With ``ts``+``digest`` this is the APPEND-ONLY form — a name no other writer
    can be holding. Without them it is the historical plain form, which stays
    valid for hand-writers.
    """
    if head and ts and digest:
        return f"{head}--{reviewer}--{ts}-{digest}.md"
    return f"{head}--{reviewer}.md" if head else f"{reviewer}.md"


def parse_verdict_filename(
    name: str, *, head: Optional[str] = None
) -> Optional[tuple[str, Optional[str]]]:
    """``(reviewer, ts_or_None)`` for a verdict filename, or ``None``.

    ``ts`` is present only for the append-only form; a plain shard carries its
    time in frontmatter (or, failing that, the listing mtime), because it was
    written before the name had anywhere to put it.
    """
    if not name.endswith(".md"):
        return None
    stem = name[:-3]
    if head:
        prefix = f"{head}--"
        if not stem.startswith(prefix):
            return None
        rest = stem[len(prefix):]
        if not rest:
            return None
        m = _APPEND_SUFFIX.search(rest)
        if m:
            reviewer = rest[:m.start()]
            return (reviewer, m.group("ts")) if reviewer else None
        return (rest, None)
    # Legacy unkeyed review: the historical `<reviewer>.md` layout only.
    return (stem, None) if "--" not in stem else None


def reviewer_from_filename(name: str, *, head: Optional[str] = None) -> Optional[str]:
    """Decode the requirement token from a verdict filename for ``head``.

    Head-keyed reviews ignore every superseded head before reading its shard.
    Legacy unkeyed reviews retain the historical ``<reviewer>.md`` layout.
    """
    parsed = parse_verdict_filename(name, head=head)
    return parsed[0] if parsed else None


def fold_newest_per_reviewer(
    rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Keep the newest shard per reviewer; return ``(kept, folded_away)``.

    Rows carry ``reviewer``, ``name``, and ``sort_key``. Newest wins, ties break
    deterministically on the name so two hosts folding the same directory always
    agree.

    The count is returned rather than swallowed because SUPERSESSION MUST BE
    AUDITABLE (coord-boss constraint 4): a reader who is told "APPROVED" while
    three shards were silently discarded has been handed the same affirmative
    falsehood this whole cycle has been about. `review status` says how many it
    folded away, which is also the reviewer's correction path — a correction is
    a new file, and the original evidence stays on disk.
    """
    best: dict[str, dict[str, Any]] = {}
    folded = 0
    for row in sorted(rows, key=lambda r: (r.get("sort_key") or "",
                                           r.get("name") or "")):
        prior = best.get(row["reviewer"])
        if prior is not None:
            folded += 1
        best[row["reviewer"]] = row
    return [best[k] for k in sorted(best)], folded


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
