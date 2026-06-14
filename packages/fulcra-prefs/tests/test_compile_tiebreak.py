"""Conflict tie-break must compare real instants, not raw ISO strings.

When two signals for a key have equal abs(effective_weight)*confidence, the
tie falls back to recency. Comparing observed_at as strings ranks by
lexicographic order, which disagrees with real chronology across timezone
offsets — an older instant can sort ahead of a newer one.
"""
from datetime import datetime, timezone

from test_schema import make_signal
from fulcra_prefs.compileprefs import compile_signals

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def test_tie_break_uses_real_instant_across_tz_offsets():
    # equal abs(weight)*confidence (no decay). b is the NEWER instant (11:00Z)
    # but its raw string sorts BELOW a's (10:00Z) lexicographically.
    a = make_signal(id="rec-a", value={"v": "older"}, strength=0.5,
                    confidence=1.0, half_life_days=None,
                    observed_at="2026-06-09T10:00:00+00:00")
    b = make_signal(id="rec-b", value={"v": "newer"}, strength=0.5,
                    confidence=1.0, half_life_days=None,
                    observed_at="2026-06-09T06:00:00-05:00")
    docs = compile_signals([a, b], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"v": "newer"}
