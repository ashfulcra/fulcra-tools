"""MERGE-READY: the verification half of a merge, never the merge itself.

G6 ruling (coord-boss triage 2026-08-11, directive 082d799e): NO to auto-merge — merge-time judgment stays with
the merger. YES to automating the checks a merger repeats by hand before every click:

1. the review register is APPROVED at an exact head, every required approval is a shard that attests that head;
2. three heads are equal — the PR head on the forge, the register head, the head each winning verdict reviewed;
3. CI is green on that PR head.

This module is the pure decision over a review tally (``cli._review_tally``) and a ``gh pr view`` object. The CLI
verb owns the reads, the fail-closed register (an unreadable input is UNKNOWN, never NOT READY and never READY)
and the optional report to the merger's queue.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

PR_FIELDS = "state,isDraft,headRefOid,statusCheckRollup"

#: A completed check whose conclusion is one of these does not block a merge.
PASSING_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


def pr_view(runner: Callable[[list[str]], Optional[str]], url: str) -> Optional[dict[str, Any]]:
    """The PR fields the decision needs, or None when the forge did not answer with a JSON object."""
    raw = runner(["gh", "pr", "view", url, "--json", PR_FIELDS])
    if not raw:
        return None
    try:
        got = json.loads(raw)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def ci_state(rollup: Any) -> tuple[str, list[str]]:
    """("green" | "pending" | "failed" | "none", names of the checks that are not green).

    No checks at all is "none", not green: a PR whose workflows never ran (path filters, a disabled runner, a
    forge that has not reported yet) has proven nothing. A failure outranks a pending check, so a red run is
    reported as red while a sibling is still running."""
    if not isinstance(rollup, list) or not rollup:
        return "none", []
    failed: list[str] = []
    pending: list[str] = []
    for check in rollup:
        if not isinstance(check, dict):
            failed.append("<malformed check>")
            continue
        name = str(check.get("name") or check.get("context") or "<unnamed>")
        if check.get("__typename") == "StatusContext" or ("state" in check and "status" not in check):
            state = str(check.get("state") or "").upper()
            if state == "SUCCESS":
                continue
            (pending if state in ("PENDING", "EXPECTED", "") else failed).append(name)
            continue
        if str(check.get("status") or "").upper() != "COMPLETED":
            pending.append(name)
        elif str(check.get("conclusion") or "").upper() not in PASSING_CONCLUSIONS:
            failed.append(name)
    if failed:
        return "failed", failed
    if pending:
        return "pending", pending
    return "green", []


def evaluate(tally: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
    """Decide MERGE-READY from a fully read tally and a PR object. ``reasons`` names every failed check."""
    reasons: list[str] = []
    head = tally.get("head")
    if not head:
        reasons.append("the review register names no head, so no verdict is bound to the code being merged "
                       "(re-request with --head <sha>)")
    state = tally.get("state")
    if state != "APPROVED":
        pending = ", ".join(tally.get("pending_required") or [])
        changes = ", ".join(tally.get("changes") or [])
        detail = "; ".join(x for x in (f"changes requested: {changes}" if changes else "",
                                       f"awaiting: {pending}" if pending else "") if x)
        reasons.append(f"review is {state}" + (f" ({detail})" if detail else ""))
    for key, label in (("head_mismatched_verdicts", "verdicts attesting another head"),
                       ("unattributable", "unattributable verdict files"),
                       ("unrecognised_verdicts", "unrecognised verdict tokens"),
                       ("malformed_supersedes", "malformed supersession edges")):
        if tally.get(key):
            reasons.append(f"{label} in the register: {len(tally[key])}")
    if head:
        winning = tally.get("winning") or {}
        for reviewer in tally.get("required") or []:
            shard = winning.get(reviewer) or {}
            if shard.get("verdict") == "approve" and not str(shard.get("name") or "").startswith(f"{head}--"):
                reasons.append(f"{reviewer}'s winning approval {shard.get('name')!r} is not a shard of head {head}")
    pr_state = str(pr.get("state") or "")
    if pr_state != "OPEN":
        reasons.append(f"PR is {pr_state or 'in an unknown state'}, not OPEN")
    if pr.get("isDraft"):
        reasons.append("PR is a draft")
    pr_head = str(pr.get("headRefOid") or "").lower()
    if head and pr_head != str(head).lower():
        reasons.append(f"PR head {pr_head or '<none>'} is not the reviewed head {head} "
                       f"(new commits since review: re-request at the new head)")
    ci, names = ci_state(pr.get("statusCheckRollup"))
    if ci == "none":
        reasons.append("CI reported no checks on the PR head")
    elif ci != "green":
        reasons.append(f"CI {ci}: {', '.join(names)}")
    checks = pr.get("statusCheckRollup")
    return {
        "ready": not reasons,
        "reasons": reasons,
        "head": head,
        "pr_head": pr_head or None,
        "ci": ci,
        "checks": len(checks) if isinstance(checks, list) else 0,
        "approvals": list(tally.get("approvals") or []),
    }
