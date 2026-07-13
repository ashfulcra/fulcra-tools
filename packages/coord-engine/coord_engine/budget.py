"""coord-engine wake-budget mechanics — the single home for the deadline/degraded
pattern the bounded fan-out folds share.

Distinct from :mod:`coord_engine.config` (which owns the env *parsers* that turn a
knob into a number): this module owns the *mechanics* that spend that number —
the absolute-``time.monotonic()`` deadline, the after-op expiry check, the
reserved sub-budget, the degraded row, and the degraded-line renderer. The two
coexist and are imported together: ``config.env_float("COORD_OVERLAY_BUDGET", …)``
produces the seconds; ``budget.Deadline.open(seconds)`` spends them.

Why a shared helper: the deadline check was hand-rolled at ~19 sites as
``deadline is not None and time.monotonic() >= deadline`` (two sites drifted to a
bare ``>``), the degraded row was rebuilt inline at ~11 sites, and three
``_*_degraded_line`` renderers were byte-for-byte the same template. Duplication
that shape invites semantic drift (a NaN/inf wording or a ``>`` vs ``>=`` boundary
that differs by helper). One helper, used everywhere, makes the family move as a
unit — and is the ship-gate shape for any NEW bounded fan-out (see AGENTS.md).

**Deadline discipline** (the invariant every fold upholds): a deadline is an
absolute ``time.monotonic()`` instant, or ``None`` for "no bound". It is checked
BOTH before and after each blocking transport op — a strict wall-clock bound is
impossible without cancellable transport, so the guarantee is that an overrun is
DETECTED immediately after the op that caused it (a single stalled read can no
longer return a clean row), and overshoot is bounded by ONE transport timeout.

stdlib-only; nothing here raises.
"""

from __future__ import annotations

import time
from typing import Any, Optional


class Deadline:
    """An absolute ``time.monotonic()`` wake budget.

    Construct from an already-computed instant with ``Deadline(instant)`` (the
    receive-a-``deadline``-arg case, ``instant`` may be ``None`` for unbounded), or
    open a fresh one from a seconds budget with :meth:`open`. :meth:`expired` is the
    shared after-op check; :meth:`reserve` carves a sub-budget for a phase that must
    leave time for a later one.
    """

    __slots__ = ("instant", "_budget")

    def __init__(self, instant: Optional[float]) -> None:
        self.instant = instant
        self._budget: Optional[float] = None

    @classmethod
    def open(cls, budget_seconds: Optional[float]) -> "Deadline":
        """Open a deadline ``budget_seconds`` from now (``None`` -> unbounded). The
        budget is retained so :meth:`reserve` can carve a fraction of it."""
        d = cls(None if budget_seconds is None else time.monotonic() + budget_seconds)
        d._budget = budget_seconds
        return d

    def expired(self) -> bool:
        """True once the wall clock reaches the instant. An unbounded (``None``)
        deadline never expires. Uses ``>=`` — the boundary instant counts as spent."""
        return self.instant is not None and time.monotonic() >= self.instant

    def reserve(self, fraction: float) -> "Deadline":
        """A sub-deadline that reserves ``fraction`` of the budget for LATER work,
        giving the current phase the remainder. ``reserve(0.5)`` on a 30s budget
        yields a sub-deadline 15s out — the phase runs to the halfway instant, the
        reserved half is left for the phase that follows. Unbounded or
        instant-only (opened via the bare constructor) deadlines reserve nothing."""
        if self.instant is None or self._budget is None:
            return Deadline(self.instant)
        return Deadline(self.instant - self._budget * fraction)


def degraded_row(
    marker_type: str, scanned: int, total: int, skipped: int = 0
) -> dict[str, Any]:
    """The shared ``{type, scanned, total[, skipped]}`` degraded marker every
    bounded fan-out fold appends when a budget breach / transport failure truncates
    it. ``skipped`` is omitted when zero (a fully-scanned-but-late fold has no
    unreadable slugs to report). ``marker_type`` is the fold's own type string
    (``review-fold-degraded`` / ``forge-degraded`` / ``presence-degraded`` — they
    are deliberately irregular and each caller passes its own)."""
    row: dict[str, Any] = {"type": marker_type, "scanned": scanned, "total": total}
    if skipped:
        row["skipped"] = skipped
    return row


def fold_degraded_line(
    r: dict[str, Any], *, label: str, remedy: str, noun: str
) -> str:
    """Render a :func:`degraded_row` for text output. Byte-identical to the three
    former ``_*_degraded_line`` renderers, parameterised by the only three things
    that differed: the fold ``label``, the ``remedy`` clause, and the skipped
    ``noun``."""
    line = (f"  {label} fold degraded: scanned {r.get('scanned')}/{r.get('total')} "
            f"before budget — {remedy}")
    if r.get("skipped"):
        line += f" ({r['skipped']} {noun}(s) skipped on transport error)"
    return line
