"""effective_weight must never exceed |strength|.

_age_days goes negative when observed_at is in the future (clock skew between a
capturing device and the compile host). 2**(-negative/hl) is > 1, so the decayed
weight came out ABOVE strength and could wrongly win conflict resolution. A
not-yet-aged signal should decay toward, at most equal, its strength.
"""
from datetime import datetime, timezone

from test_schema import make_signal
from fulcra_prefs.decay import effective_weight

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def test_future_dated_signal_does_not_exceed_strength():
    s = make_signal(strength=0.8, half_life_days=90.0,
                    observed_at="2026-09-12T12:00:00+00:00")  # ~92d in the future
    assert effective_weight(s, NOW) <= 0.8 + 1e-9


def test_past_signal_still_decays_below_strength():
    s = make_signal(strength=0.8, half_life_days=90.0,
                    observed_at="2026-03-14T12:00:00+00:00")  # ~90d in the past
    assert effective_weight(s, NOW) < 0.8
