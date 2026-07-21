"""coord-engine env-var config — the single home for the engine's tuning knobs.

ONE parser policy, applied everywhere, so the family of budgets/timeouts can never
drift apart in NaN/inf handling or flag-vs-env fallback (the drift codex flagged when
these bodies were copy-pasted per-helper). A knob is a **positive-finite** number,
resolved ``override > env[name] > env[alias…] > default``; anything unparseable, NaN,
non-finite, or not greater than ``minimum`` falls back to ``default`` — a bad env value
must NEVER disable a bound or make an op hang.

The canonical catalogue of every ``COORD_*`` knob (name, default, unit, what it bounds)
and the ``FULCRA_COORD_*`` legacy-prefix rule lives in
[`coord-engine/README.md`](../README.md) → *Environment / tuning*. Keep that table and
this module in lockstep (there is a docs-vs-code test that fails if a documented name is
not read by the code).

stdlib-only; these functions never raise.
"""

from __future__ import annotations

import math
import os
from typing import Optional, Sequence


def _resolve_raw(
    name: str, override: Optional[object], aliases: Sequence[str]
) -> Optional[object]:
    """First present of: an explicit ``override`` (a flag / constructor arg — wins over
    the environment), ``env[name]`` (the canonical var), then each legacy ``alias`` in
    order. ``None`` means "nothing configured" (use the default)."""
    if override is not None:
        return override
    raw = os.environ.get(name)
    if raw is not None:
        return raw
    for alt in aliases:
        raw = os.environ.get(alt)
        if raw is not None:
            return raw
    return None


def env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    override: Optional[object] = None,
    aliases: Sequence[str] = (),
) -> float:
    """A positive-finite float knob. See the module docstring for the policy.

    ``minimum`` is the strict floor (``v > minimum``); ``override`` is an explicit
    flag/constructor value that wins over the environment; ``aliases`` are legacy env
    names read (in order) only when ``name`` is unset."""
    raw = _resolve_raw(name, override, aliases)
    if raw is None:
        return default
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v) or not (v > minimum):  # NaN, ±inf, <= minimum -> default
        return default
    return v


def env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    override: Optional[object] = None,
    aliases: Sequence[str] = (),
) -> int:
    """A positive int knob (same policy as :func:`env_float`, integer-valued)."""
    raw = _resolve_raw(name, override, aliases)
    if raw is None:
        return default
    try:
        v = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return v if v > minimum else default


def retention_days(default: float, *, override: Optional[object] = None) -> float:
    """Retention is on by default, with an explicit ``0`` as the kill switch.

    Unlike budgets, zero is meaningful here. Invalid/non-finite/negative values
    fail safe to the enabled default rather than silently disabling cleanup.
    """
    raw = _resolve_raw(
        "COORD_RETENTION_DAYS", override, ("FULCRA_COORD_RETENTION_DAYS",)
    )
    if raw is None:
        return default
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default
