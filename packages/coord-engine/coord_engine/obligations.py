"""The mandatory obligation fold — "do I owe anything?" as a terminal state.

Normative source: ``reports/2026-07-29-coordv3-r2-spec-codex-coder.md``, item 3:

    The protocol still needs one normative command that answers whether an agent
    owes work across directives, reviews, tasks, blocks, reminders, and
    role-routed duties. That fold must return DATA/CLEAR only after all component
    listings and documents are known. Any unreadable component makes the answer
    UNKNOWN. A wake that reads an empty queue but does not perform this
    reconciliation cannot prove there is no work.

Why this exists when ``needs-me`` already reads the same components
------------------------------------------------------------------
``needs-me`` folds tasks, role routing, pending reviews and forge feedback, and
it is honest about degradation — it prepends marker ROWS when a component could
not be read. But a marker row is an annotation on an answer, and an annotation
can be read past: a caller that counts rows, greps for its own slug, or renders
"nothing assigned" sees an empty-looking result with a note attached. The note is
advisory; the emptiness looks authoritative.

This fold makes the incompleteness **structural** instead. There is no
"CLEAR, but" state to misread — if any component is unreadable, CLEAR is not a
value the fold can return. That is the difference between reporting doubt and
being unable to report certainty, and it is the whole point of item 3.

The component registry
----------------------
``OBLIGATION_COMPONENTS`` is deliberately ONE constant in ONE place. The
component set is the correctness-critical input here: a fold that omits a
component reports CLEAR while the agent owes work, which is precisely the failure
this slice exists to end. Keeping it as a named registry means a wrong or
incomplete set is a one-line correction visible in review, rather than a missing
branch buried in control flow.

PROVENANCE, stated plainly because it matters: this set began as a reconstruction
from the r2 spec sentence quoted above, unconfirmed against the execution plan's
owner text (that document was overwritten before slice 3 could be cross-checked
against it).

``forge_feedback`` was ADDED by review (codex-reviewer, PR 501): unacknowledged
forge feedback is already a durable obligation surfaced by ``needs-me`` and
``briefing``, and its absence here meant the fold could return CLEAR while that
work was owed. That is the exact failure this module warns about two paragraphs
up — a component nobody named reports nothing and looks identical to one with
nothing to report — and it was caught by a reviewer reading the surface, not by
any test here. Worth remembering the next time this list changes: the registry
is only as good as the last independent read of what an agent can owe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class ObligationState(str, Enum):
    """Terminal states. Four, and never collapsed into two.

    ``CLEAR`` is a positive claim — "every component was consulted and none owes
    this agent anything". It is only reachable when nothing is degraded, which is
    enforced in :func:`fold` rather than left to the caller.
    """

    DATA = "DATA"          # at least one component owes this agent work
    CLEAR = "CLEAR"        # every component consulted, nothing owed
    UNKNOWN = "UNKNOWN"    # a component could not be consulted
    INVALID = "INVALID"    # a component was consulted and its data is malformed


class ProbeState(str, Enum):
    """What one component reported."""

    OK = "ok"
    UNREADABLE = "unreadable"   # transport/auth/network — retry might help
    MALFORMED = "malformed"     # bytes exist and do not parse — retry never helps


@dataclass
class ProbeResult:
    """One component's answer: its state plus whatever it says is owed."""

    state: ProbeState
    owed: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""


#: One component of the obligation surface.
#:
#: ``probe`` takes no arguments by the time :func:`fold` sees it — callers bind
#: transport/team/agent with a closure or ``functools.partial``. That keeps this
#: module free of transport knowledge and makes every component independently
#: faultable in tests, which is what the fail-closed gates need.
@dataclass(frozen=True)
class Component:
    name: str
    probe: Callable[[], ProbeResult]


#: THE component set. See the module docstring on provenance before editing.
#:
#: Order is stable and alphabetical so a degraded list is deterministic — a
#: fold whose diagnostics reorder between runs is a fold nobody can diff.
OBLIGATION_COMPONENTS: tuple[str, ...] = (
    "blocks",
    "directives",
    "forge_feedback",
    "reminders",
    "reviews",
    "role_duties",
    "tasks",
)


@dataclass
class ObligationResult:
    state: ObligationState
    owed: list[dict[str, Any]] = field(default_factory=list)
    #: Components that could not be consulted, sorted. Non-empty implies the
    #: state is UNKNOWN — never CLEAR.
    degraded: list[str] = field(default_factory=list)
    #: Components whose data was consulted and does not parse, sorted.
    malformed: list[str] = field(default_factory=list)
    #: Components successfully consulted, sorted. Present so a caller can prove
    #: coverage rather than infer it from the absence of complaints.
    consulted: list[str] = field(default_factory=list)
    #: component name -> WHY it degraded or was malformed. A report that names
    #: which components failed but not why is one nobody can act on: an
    #: exhausted budget and an unreachable store render identically without it,
    #: and they have opposite remedies.
    details: dict[str, str] = field(default_factory=dict)

    @property
    def can_claim_clear(self) -> bool:
        """True only when every component was consulted and none owed work."""
        return self.state is ObligationState.CLEAR

    def _labelled(self, name: str) -> str:
        why = self.details.get(name)
        return f"{name} ({why})" if why else name

    def reason(self) -> str:
        """One line a human or a log can act on."""
        if self.state is ObligationState.UNKNOWN:
            names = self.degraded or ["(unnamed component)"]
            return ("cannot prove anything about: "
                    + ", ".join(self._labelled(n) for n in names))
        if self.state is ObligationState.INVALID:
            return "malformed component data: " + ", ".join(self.malformed)
        if self.state is ObligationState.DATA:
            return f"{len(self.owed)} obligation(s) owed"
        return f"nothing owed across {len(self.consulted)} component(s)"


def fold(components: list[Component], *,
         expected: Optional[tuple[str, ...]] = None) -> ObligationResult:
    """Reconcile every component into ONE terminal state, failing closed.

    Rules, each with the failure it prevents:

    * Any unreadable component ⇒ **UNKNOWN**. Not "CLEAR with a warning": a
      caller that can read past the warning will, eventually, on the day it
      matters.
    * Any malformed component and nothing unreadable ⇒ **INVALID**. Distinct
      because the remedy is distinct — a human fixes the file; no retry will.
    * Unreadable outranks malformed when both occur. Both preclude CLEAR, and
      "I could not consult everything" is the weaker, more honest claim of the
      two. Both lists are still reported so the fixable one stays visible.
    * A component missing from ``expected`` ⇒ **UNKNOWN**, even if every
      component present reported OK. A fold that silently skips a component is
      indistinguishable from one that found nothing there, and that is the exact
      confusion item 3 exists to remove.
    * Work found in a degraded fold is still returned. Partial data is useful;
      pretending it is the whole answer is not.
    """
    consulted: list[str] = []
    degraded: list[str] = []
    malformed: list[str] = []
    owed: list[dict[str, Any]] = []
    details: dict[str, str] = {}

    for component in components:
        try:
            result = component.probe()
        except Exception as exc:  # a probe that raises is a probe that failed
            degraded.append(component.name)
            details[component.name] = f"probe raised {type(exc).__name__}"
            continue
        if result.state is ProbeState.UNREADABLE:
            degraded.append(component.name)
            if result.detail:
                details[component.name] = result.detail
        elif result.state is ProbeState.MALFORMED:
            malformed.append(component.name)
            if result.detail:
                details[component.name] = result.detail
        else:
            consulted.append(component.name)
        owed.extend(result.owed)

    # A component that was never offered is not a component that reported
    # nothing. Treat the omission as the doubt it is.
    if expected is not None:
        present = {c.name for c in components}
        missing = sorted(set(expected) - present)
        degraded.extend(missing)
        for name in missing:
            details.setdefault(name, "component was never offered to the fold")

    degraded, malformed = sorted(set(degraded)), sorted(set(malformed))

    if degraded:
        state = ObligationState.UNKNOWN
    elif malformed:
        state = ObligationState.INVALID
    elif owed:
        state = ObligationState.DATA
    else:
        state = ObligationState.CLEAR

    return ObligationResult(state=state, owed=owed, degraded=degraded,
                            malformed=malformed, consulted=sorted(consulted),
                            details=details)
