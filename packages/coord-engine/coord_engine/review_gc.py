"""``review gc`` — retire register entries that can NEVER settle.

WHY THIS EXISTS, and why it is not tidiness. coord-boss's 2026-08-07 finding:
the review projection reports ``scanned 124/143, budget_cut: true``, so 19
entries are never projected and the fold degrades to a raw scan. Roughly 33 of
those 143 are permanently unsatisfiable — ``listen``-watcher reviews for a
subsystem retired 2026-07-27, most of whose heads no longer exist as objects.
Dead work is displacing live work in the only fold that tells reviewers what
they owe, and it has been quietly making every reviewer's ``needs-me`` less
trustworthy the whole time.

THE PREDICATE IS NON-SETTLEABILITY, NOT HEAD-LIVENESS. My first sketch said
"never touch an entry with a live head", and coord-boss found the counterexample
by hitting it: a review re-routed to a different reviewer leaves an entry with a
LIVE head whose required set can never produce a verdict, because the register
(correctly) refuses to mutate a required set on an existing slug. Head-liveness
would have left exactly that entry taxing every projection pass forever.

FAIL CLOSED, NON-NEGOTIABLE. Only an AFFIRMATIVE "this object does not exist"
may retire an entry. A head that cannot be resolved because git is missing, the
store is degraded, or the probe errored is UNKNOWN — and UNKNOWN keeps the entry
alive. Getting this backwards would gc live obligations, which is strictly worse
than the rot it cleans: rot degrades a fold, a wrongly-closed review silently
drops work someone is waiting on.

A DISTINCT TERMINAL STATE. Retired entries get ``.gc-closed``, never
``.settled``. The register must keep "reviewed and approved" separable from
"abandoned when the subsystem was retired" — that distinction is precisely what
the unsatisfiable-obligations report needed to make its own case, and collapsing
the two would destroy the evidence for the next such argument.

SUPERSESSION IS DECLARED, NEVER INFERRED. Routing supersession is a fact about
intent, not about bytes: nothing in the register distinguishes "re-routed" from
"still waiting". So it is closed only when the review doc SAYS so
(``superseded_by: <slug>`` in frontmatter). Guessing here would retire live
reviews whose reviewer is merely slow, which this fleet spent a week learning is
a different thing from absent.
"""

from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple, Optional

#: ``review-request/v1`` docs carry no ``head:`` key — the head rides inside the
#: ``of:`` prose as ``… @ head <sha> …``. Those are the OLD entries, which is to
#: say most of the rot. Extracting it is strictly opt-in and strictly
#: unambiguous: the pattern must be an explicit "head <40-hex>", and if the
#: field yields anything other than EXACTLY ONE distinct candidate the entry
#: stays UNKNOWN. Reading the wrong sha out of prose would retire a live review,
#: which is the one outcome worse than leaving the rot in place.
_PROSE_HEAD = re.compile(r"\bhead\s+([0-9a-fA-F]{40})\b")

#: The repositories an ``of:`` field names. A forge URL is unambiguous; bare
#: ``owner/repo`` prose is only accepted when a PR marker follows it, because
#: ``of: branch claude/fulcra-worker-setup-6zxa8l`` is a real register value and
#: is a BRANCH, not a repository. Reading that as a repo would be inventing an
#: identity — the same move this guard exists to stop.
_OF_URL = re.compile(
    r"github\.com[/:]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?=[/\s\"'#]|$)")
_OF_PROSE = re.compile(
    r"\b([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)\s+(?:PR|pull)\b", re.I)

#: Terminal marker for a gc-retired entry. Deliberately NOT ``.settled``.
GC_MARKER = ".gc-closed"

#: Marker schema, so a reader can tell a retirement from a fold cache.
GC_SCHEMA = "coord.review-gc-closed.v1"

#: Frontmatter key by which a review doc declares it was superseded.
SUPERSEDED_KEY = "superseded_by"

#: Classifications. Only RETIRABLE ones are ever written.
LIVE = "live"
DEAD_HEAD = "dead-head"
SUPERSEDED = "superseded"
UNKNOWN = "unknown"
ALREADY_CLOSED = "already-closed"
SETTLED = "settled"

#: The two states gc may act on. Everything else is left strictly alone.
RETIRABLE = (DEAD_HEAD, SUPERSEDED)


def repos_from_of(text: Any) -> frozenset:
    """Every ``owner/repo`` the ``of:`` field names, lowercased.

    A SET, not one value: a PR routinely names both the upstream repo and the
    fork the branch lives in, and either is a repository in which that head
    legitimately exists. Empty means the review does not say where its work
    lives — which is not a repo match, and therefore not grounds to retire it.
    """
    if not isinstance(text, str):
        return frozenset()
    found = {f"{o}/{r}".lower() for o, r in _OF_URL.findall(text)}
    found |= {f"{o}/{r}".lower() for o, r in _OF_PROSE.findall(text)}
    return frozenset(found)


def head_from_prose(text: Any) -> Optional[str]:
    """The head named in a v1 ``of:`` string, or None unless it is unambiguous."""
    if not isinstance(text, str):
        return None
    found = {m.lower() for m in _PROSE_HEAD.findall(text)}
    return found.pop() if len(found) == 1 else None


class Entry(NamedTuple):
    """One register entry, as much as the caller could read of it."""
    slug: str
    head: Optional[str]
    superseded_by: Optional[str]
    settled: bool
    gc_closed: bool
    #: Repositories this review's head could live in, from ``of:``. Empty means
    #: the review never says, which is UNKNOWN — never an invitation to guess.
    repos: frozenset = frozenset()


class Verdict(NamedTuple):
    """What gc decided about one entry, and the evidence for it."""
    slug: str
    state: str
    reason: str

    @property
    def retirable(self) -> bool:
        return self.state in RETIRABLE


#: ``head_exists(sha) -> True | False | None``. True = the object is present,
#: False = AFFIRMATIVELY absent, None = could not tell. None must never retire
#: anything; see the module docstring.
HeadProbe = Callable[[str], Optional[bool]]


def classify(entry: Entry, *, head_exists: HeadProbe,
             local_repo: Optional[str]) -> Verdict:
    """Decide one entry. Retires only on affirmative evidence.

    ``local_repo`` is the ``owner/repo`` the probe can actually speak for — the
    invoking checkout's origin, or an operator's explicit assertion. It has no
    default ON PURPOSE: every caller on the destructive path must state which
    repository its evidence comes from, and a default would let a caller keep
    the old behaviour silently.
    """
    if entry.gc_closed:
        return Verdict(entry.slug, ALREADY_CLOSED, "already retired by gc")
    if entry.settled:
        # A settled review is finished work with a real outcome. gc has no
        # business touching it: it is not taxing anything a reader misreads,
        # and overwriting its marker would erase the outcome.
        return Verdict(entry.slug, SETTLED, "settled — a real outcome, not rot")
    if entry.superseded_by:
        return Verdict(entry.slug, SUPERSEDED,
                       f"declared superseded_by: {entry.superseded_by} — its "
                       f"required set can never verdict this slug")
    if not entry.head:
        # No head at all is malformed, not dead. A human wrote these bytes and
        # an engine that "cleans up" unparseable evidence destroys the only
        # record of what went wrong.
        return Verdict(entry.slug, UNKNOWN,
                       "no active head in the review doc — malformed, not dead; "
                       "a human reads this one")
    # WHICH repository the probe is standing in. `git cat-file -e` answers "is
    # this object HERE", and the shallow/partial guards ask whether HERE is
    # complete — but a COMPLETE clone of the WRONG repository proves absence
    # exactly as poorly as an incomplete clone of the right one. Measured: a
    # scan from a fulcra-tools checkout called an agent-skills review dead at a
    # head that existed in its own checkout AND on the forge, one hour after it
    # shipped. So HERE has to include WHICH here.
    if not local_repo:
        return Verdict(entry.slug, UNKNOWN,
                       "the invoking repository could not be identified — "
                       "absence is unprovable from an unknown vantage point")
    if not entry.repos:
        return Verdict(entry.slug, UNKNOWN,
                       "the review does not name the repository its head lives "
                       "in — malformed, not dead")
    if local_repo.lower() not in entry.repos:
        return Verdict(entry.slug, UNKNOWN,
                       f"head belongs to {'/'.join(sorted(entry.repos)[:1])}, "
                       f"but this checkout is {local_repo} — a foreign repo "
                       f"cannot witness absence")

    present = head_exists(entry.head)
    if present is None:
        return Verdict(entry.slug, UNKNOWN,
                       f"head {entry.head[:12]} could not be resolved (probe "
                       f"unavailable or errored) — UNKNOWN keeps it alive")
    if present:
        return Verdict(entry.slug, LIVE, f"head {entry.head[:12]} exists")
    return Verdict(entry.slug, DEAD_HEAD,
                   f"head {entry.head[:12]} does not exist as an object")


def plan(entries: list[Entry], *, head_exists: HeadProbe,
         local_repo: Optional[str]) -> list[Verdict]:
    """Classify every entry, in register order. Pure: writes nothing."""
    return [classify(e, head_exists=head_exists, local_repo=local_repo)
            for e in entries]


def summarize(verdicts: list[Verdict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.state] = counts.get(v.state, 0) + 1
    return counts


def marker_body(verdict: Verdict, *, now: str, by: str) -> str:
    """The ``.gc-closed`` document. Carries its own evidence so a human meeting
    it later can tell WHY without re-deriving anything."""
    import json
    return json.dumps({
        "schema": GC_SCHEMA,
        "state": verdict.state,
        "reason": verdict.reason,
        "slug": verdict.slug,
        "closed_by": by,
        "ts": now,
    }, indent=2)


def render_plan(verdicts: list[Verdict], *, applying: bool) -> str:
    """Human-readable plan. A verb that retires review obligations should have
    to show its work before it is allowed to act."""
    counts = summarize(verdicts)
    retirable = [v for v in verdicts if v.retirable]
    unknown = [v for v in verdicts if v.state == UNKNOWN]
    lines = [
        f"review gc — {len(verdicts)} entr(ies) scanned: "
        + ", ".join(f"{k}={counts[k]}" for k in sorted(counts))
    ]
    for v in retirable:
        lines.append(f"  {'RETIRE' if applying else 'would retire'} "
                     f"{v.slug} — {v.reason}")
    for v in unknown:
        # UNKNOWN is printed, always. A gc pass that quietly skipped what it
        # could not classify would look identical to one with nothing to skip.
        lines.append(f"  keep {v.slug} — UNKNOWN: {v.reason}")
    if not retirable:
        lines.append("  nothing retirable")
    if not applying and retirable:
        lines.append("  (dry run — re-run with --apply to write markers)")
    return "\n".join(lines)
