"""Full-recompute compile: signals -> compiled docs. Pure function of
(signals, now). Stable signal-id sort happens BEFORE conflict resolution —
that ordering is part of the reviewed determinism contract (PR #146)."""
from __future__ import annotations
from datetime import datetime
from .decay import effective_weight, is_stale
from .schema import Signal, SCHEMA_V


def _signal_ids(sig: Signal) -> set[str]:
    return {x for x in (sig.id, *sig.source_ids) if x}


def _live_signals(signals: list[Signal]) -> list[Signal]:
    superseded = {s.supersedes for s in signals if s.supersedes}
    return [s for s in signals if not (_signal_ids(s) & superseded)]


def _entry(sig: Signal, weight: float, n: int, now: datetime) -> dict:
    e = {"value": sig.value, "weight": weight, "confidence": sig.confidence,
         "observed_at": sig.observed_at, "n_signals": n,
         "sources": [sig.platform]}
    if is_stale(sig, now):
        e["stale"] = True
    return e


def _reduce(signals: list[Signal], now: datetime) -> dict:
    by_key: dict[str, list[Signal]] = {}
    for s in signals:
        by_key.setdefault(s.key, []).append(s)
    keys: dict[str, dict] = {}
    for key, group in by_key.items():
        # Stable id sort first — conflict resolution must not depend on input order.
        group = sorted(group, key=lambda s: s.id or "")
        # Selection is weighted by confidence so a low-confidence INFERRED signal
        # (auto-captured) never silently overrides a high-confidence EXPLICIT one
        # of similar strength. The EMITTED weight stays the raw effective weight;
        # confidence only influences which signal wins. Deterministic: ties fall
        # back to observed_at, then the id pre-sort above.
        best = max(group, key=lambda s: (abs(effective_weight(s, now)) * s.confidence,
                                         s.observed_at))
        keys[key] = _entry(best, effective_weight(best, now), len(group), now)
    return keys


def compile_signals(signals: list[Signal], now: datetime) -> dict:
    live = [s for s in _live_signals(signals) if s.kind in ("preference", "fact")]
    compiled_at = now.isoformat()
    global_keys = _reduce([s for s in live if s.scope == "global"], now)
    docs = {"global": {"v": SCHEMA_V, "compiled_at": compiled_at, "keys": global_keys},
            "platforms": {}}
    platforms = sorted({s.scope.split(":", 1)[1] for s in live
                        if s.scope.startswith("platform:")})
    for p in platforms:
        overlay = _reduce([s for s in live if s.scope == f"platform:{p}"], now)
        merged = dict(global_keys)
        merged.update(overlay)          # platform beats global
        docs["platforms"][p] = {"v": SCHEMA_V, "compiled_at": compiled_at,
                                "keys": merged}
    return docs
