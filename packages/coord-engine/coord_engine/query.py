"""Read-side query verbs over the aggregate rows (spec §5).

All pure functions of a row list — the CLI loads ``_coord/summaries.json`` once
and calls these. No network, no per-file reads.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from .model import OPEN_STATUSES, sort_rows


def status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(r.get("status")) for r in rows))


def board(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Open work grouped by status (active/waiting/blocked/proposed), sorted."""
    groups: dict[str, list[dict[str, Any]]] = {
        s: [] for s in ("active", "waiting", "blocked", "proposed")
    }
    for r in rows:
        st = r.get("status")
        if st in groups:
            groups[st].append(r)
    return {k: sort_rows(v) for k, v in groups.items()}


def needs_me(
    rows: list[dict[str, Any]], agent: str, *, now: Optional[str] = None,
    held_roles: "Optional[set[str] | list[str]]" = None,
    include_history: bool = False,
) -> list[dict[str, Any]]:
    """Open rows assigned to ``agent``, assigned to a ROLE ``agent`` holds
    (``held_roles``), or naming it in ``blocked_on``, gated on ``not_before`` (an
    item scheduled for the future is hidden until ``now``). A directive already
    acknowledged by ``agent`` is satisfied and absent from the default view.
    ``include_history`` bypasses lifecycle, acknowledgement, and schedule gates.

    ``now`` is an ISO-8601 string; ISO sorts lexically so string compare is a
    valid time compare. If ``now`` is None the gate is skipped.

    ``held_roles`` is the caller's resolved role set (a lease read — see
    ``cli._held_roles_for_rows``); None/empty leaves behavior unchanged. Note this
    is deliberately NOT ``directives.is_directed_at``: needs-me is the fold for
    work that is *yours*, so a broadcast (``*``) still does not enter it.
    """
    roles = set(held_roles or ())
    out: list[dict[str, Any]] = []
    for r in rows:
        if not include_history and r.get("status") not in OPEN_STATUSES:
            continue
        assignee = r.get("assignee")
        blocked_on = r.get("blocked_on") or ""
        if (assignee != agent and assignee not in roles
                and agent not in str(blocked_on)):
            continue
        tags = set(str(t) for t in (r.get("tags") or []))
        if (not include_history and "kind:directive" in tags
                and agent in (r.get("acked_by") or [])):
            continue
        nb = r.get("not_before")
        if not include_history and nb and now is not None and str(nb) > now:
            continue  # scheduled for the future
        out.append(r)
    return sort_rows(out)


#: The typed prefix `--on-user` writes into ``blocked_on`` (``user:<name>``). It
#: marks a block as directed at a HUMAN unambiguously, at zero transport cost — the
#: reserved, un-starvable head of the work-discovery surfaces. Additive: legacy
#: rows carrying a plain ``blocked_on`` value still parse and are classified below.
_USER_PREFIX = "user:"


def blocked_on_human(
    rows: list[dict[str, Any]], *, human: str = "human",
    known_agents: "Optional[set[str]]" = None, roles_unknown: bool = False,
) -> list[dict[str, Any]]:
    """Open rows blocked on a HUMAN — the reserved FIRST section of `briefing` /
    `needs-me`. A PURE fold of the aggregate rows (there is deliberately no
    ``transport`` parameter): the rows are already loaded, so this section can
    NEVER add a transport op, which is what makes it structurally un-starvable by a
    budget cut. A decision parked on a human is the incident this exists to keep
    visible.

    Each surfaced entry is the row enriched with ``type: "blocked-on-human"`` and
    ``blocked_on_user`` (the human it waits on); when the classification had to lean
    on an UNKNOWN agent/role set it also carries ``blocked_on_degraded: True``.

    Classification (ambiguity resolves toward SURFACING — a hidden human-blocked
    item is the incident; a false positive here is only noise):

    * ``blocked_on: user:<name>`` -> HUMAN, unambiguous (the typed case).
    * ``needs:human`` tag, or a ``blocked`` row whose assignee/blocked_on names the
      human -> HUMAN (the legacy operator-ask shape `--on-user` produced before the
      typing, still recognised).
    * a plain non-empty ``blocked_on`` whose tokens are NOT all known agents/roles
      -> HUMAN (surface). ``known_agents`` is the caller's already-resolved identity
      set (row assignees/owners + held roles); when that set is UNKNOWN
      (``roles_unknown`` — the role listing degraded upstream) an unresolved value is
      surfaced WITH ``blocked_on_degraded`` rather than hidden. A value whose every
      token IS a known agent/role is agent-blocked and NOT surfaced.
    """
    known = set(known_agents or ())
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("status") not in OPEN_STATUSES:
            continue
        raw = str(r.get("blocked_on") or "").strip()
        tags = r.get("tags") or []
        assignee = r.get("assignee")
        user: Optional[str] = None
        degraded = False
        tokens = [t for t in raw.replace(",", " ").split() if t]
        if raw.startswith(_USER_PREFIX):
            user = raw[len(_USER_PREFIX):].strip() or human
        elif ("needs:human" in tags
              or (r.get("status") == "blocked" and assignee == human)
              or human in tokens):
            user = human
        elif raw:
            # Legacy plain blocked_on: agent-vs-human by known-membership.
            if any(t not in known for t in tokens):
                user = raw
                degraded = roles_unknown  # UNKNOWN set -> surface WITH the note
            # else: every token is a known agent/role -> agent-blocked, not human.
        if user is None:
            continue
        row = dict(r)
        row["type"] = "blocked-on-human"
        row["blocked_on_user"] = user
        if degraded:
            row["blocked_on_degraded"] = True
        out.append(row)
    return sort_rows(out)


def asks(rows: list[dict[str, Any]], *, now: str, human: str = "human") -> list[dict[str, Any]]:
    """Waiting-for-operator asks, OLDEST FIRST (age drives nagging): open rows
    that are blocked-on-human — needs:human tag, or blocked with the human as
    assignee, or blocked_on naming the human. Each row gains age_hours."""
    from .roles import age_hours

    out = []
    for r in rows:
        if r.get("status") not in OPEN_STATUSES:
            continue
        tags = r.get("tags") or []
        hit = ("needs:human" in tags
               or (r.get("status") == "blocked"
                   and (r.get("assignee") == human
                        or human in str(r.get("blocked_on") or "").replace(",", " ").split())))
        if not hit:
            continue
        age = age_hours(r.get("timestamp"), now)
        row = dict(r)
        row["age_hours"] = None if age == float("inf") else round(age, 1)
        out.append(row)
    # unknown-age asks sort LAST deliberately (a malformed timestamp shouldn't
    # outrank datable asks in the nag order; it still appears in every pull)
    return sorted(out, key=lambda r: -(r.get("age_hours") if r.get("age_hours") is not None else -1.0))


def search(rows: list[dict[str, Any]], q: str) -> list[dict[str, Any]]:
    """Substring match over id/title/description/tags (case-insensitive)."""
    ql = (q or "").lower().strip()
    if not ql:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        hay = " ".join(
            str(x)
            for x in (
                r.get("id"),
                r.get("title"),
                r.get("description"),
                " ".join(str(t) for t in (r.get("tags") or [])),
            )
        ).lower()
        if ql in hay:
            out.append(r)
    return sort_rows(out)
