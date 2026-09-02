"""Retiring an obligation whose backing document does not exist.

A PHANTOM is an open obligation in the stream fold whose task document is gone.
It is undischargeable by the normal path: `tell --closes` resolves the close
THROUGH the document, so the missing document is exactly what prevents closing
it. Measured on team/fulcra 2026-09-02: 5 of 60 owed obligations, one of them a
P0 sitting at the top of the fold on every wake, out-ranking real work.

RETIRING IS NOT DISCHARGING. It records that the backing document is absent. It
never asserts the work in it was done, and the record must read that way to
anybody who finds it later.

THE DESIGN CONSTRAINT (coord-boss, `re-ruling-...-b30ffc7d`). `tell --closes`
reports its failure as "absent OR UNREADABLE" — the same conflation that got
automatic close-emission rejected. It fails safe, but this path must not inherit
the conflation at all, because here the conflation would fail DANGEROUS: it would
retire a live obligation whose document was merely unreadable for a moment.

So the rule is: **only an explicit not-found counts as absence, and only when a
same-pass positive control proves the store was answering correctly at that
moment.** Anything else refuses. UNKNOWN is never rendered as absent.
"""

from __future__ import annotations

from typing import NamedTuple, Optional


class Probe(NamedTuple):
    """One store lookup.

    ``found`` is TRI-STATE and the third state is the point:

    * ``True``  — the document is there.
    * ``False`` — the store explicitly answered "not found".
    * ``None``  — the lookup did not produce an answer (error, timeout,
      unreadable). NOT the same as False, and never collapsed into it.
    """
    found: Optional[bool]
    detail: str = ""


class Decision(NamedTuple):
    retire: bool
    why: str
    evidence: str


def retirement_decision(*, probe: Probe, control: Probe) -> Decision:
    """Decide whether an obligation may be retired as document-absent.

    ``control`` is a lookup of a document known to exist, run in the SAME pass
    as ``probe``. It is what separates "this document is gone" from "the store
    was not answering". Without it a not-found is not evidence of anything, and
    the write it authorises is not cheaply reversible.

    Refuses on every ambiguity. The asymmetry is deliberate: a refusal costs
    another pass, while a wrong retirement silently drops a live obligation that
    nothing will ever surface again.
    """
    if control is None:
        raise ValueError(
            "retirement_decision: control is required. A not-found with no "
            "same-pass control is indistinguishable from a degraded store, and "
            "retiring on it would drop live obligations.")

    ev = (f"probe={_render(probe)}; same-pass control={_render(control)}")

    if control.found is not True:
        return Decision(False, (
            "REFUSED: the same-pass control did not come back clean, so the "
            "store was not demonstrably answering and the target's not-found "
            "proves nothing."), ev)
    if probe.found is True:
        return Decision(False, (
            "REFUSED: the backing document exists — this is not a phantom."), ev)
    if probe.found is None:
        return Decision(False, (
            "REFUSED: the target lookup returned UNKNOWN, not an explicit "
            "not-found. UNKNOWN is not absence."), ev)
    return Decision(True, (
        "RETIRE: the backing document is absent (explicit not-found) and a "
        "same-pass control proves the store was answering. This records the "
        "document's absence — NOT that its work was done."), ev)


def _render(p: Probe) -> str:
    state = {True: "FOUND", False: "NOT_FOUND", None: "UNKNOWN"}[p.found]
    return f"{state}({p.detail})" if p.detail else state


def probe_from_listing(names: "Optional[list[str]]", slug: str) -> Probe:
    """Absence derived from a directory LISTING, not from a read.

    ``transport.read`` returns None for BOTH a missing file and a failed read,
    which is why the existing close path can only report "absent or unreadable".
    ``list_dir`` RAISES on transport failure, so a listing that RETURNS is
    evidence the store answered — and membership in it is then decisive.

    An EMPTY listing is UNKNOWN, never absence. An empty task directory on a
    board carrying hundreds of tasks is not evidence that one slug is gone; it
    is evidence something is wrong, and treating it as absence would retire
    every obligation at once. Callers pass ``None`` when the listing raised.
    """
    if not names:                      # None (raised) or [] (implausible)
        return Probe(found=None,
                     detail="listing empty or unavailable — UNKNOWN, not absence")
    target = f"{slug}.md"
    if target in names or slug in names:
        return Probe(found=True, detail=f"{target} present in listing")
    return Probe(found=False,
                 detail=f"{target} absent from a listing of {len(names)} entries")


def control_from_listing(names: "Optional[list[str]]") -> Probe:
    """The listing is its OWN positive control.

    A listing that returned entries proves the store was answering in the SAME
    pass that established the absence — which is exactly the same-pass control
    the retirement record has to carry, obtained without a second round trip and
    without nominating some other document as a canary.
    """
    if not names:
        return Probe(found=None, detail="no entries — store not demonstrably answering")
    return Probe(found=True, detail=f"listing returned {len(names)} entries")
