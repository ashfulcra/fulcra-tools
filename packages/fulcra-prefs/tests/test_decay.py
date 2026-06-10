from datetime import datetime, timezone
from fulcra_prefs.decay import effective_weight, is_stale, STALE_FACT_DAYS
from test_schema import make_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def test_zero_age_weight_equals_strength():
    s = make_signal(observed_at="2026-06-10T12:00:00+00:00")
    assert effective_weight(s, NOW) == 0.8

def test_one_half_life_halves_weight():
    s = make_signal(observed_at="2026-03-12T12:00:00+00:00", half_life_days=90.0)
    assert abs(effective_weight(s, NOW) - 0.4) < 1e-9

def test_negative_strength_decays_toward_zero_not_positive():
    s = make_signal(strength=-0.8, observed_at="2026-03-12T12:00:00+00:00",
                    half_life_days=90.0)
    assert abs(effective_weight(s, NOW) + 0.4) < 1e-9

def test_no_half_life_means_no_decay():
    s = make_signal(half_life_days=None, observed_at="2020-01-01T00:00:00+00:00")
    assert effective_weight(s, NOW) == 0.8

def test_staleness_flag_only_for_undecaying_old_signals():
    old = "2020-01-01T00:00:00+00:00"
    assert is_stale(make_signal(half_life_days=None, observed_at=old), NOW)
    assert not is_stale(make_signal(half_life_days=90.0, observed_at=old), NOW)
    fresh = "2026-06-01T00:00:00+00:00"
    assert not is_stale(make_signal(half_life_days=None, observed_at=fresh), NOW)
    assert STALE_FACT_DAYS == 180
