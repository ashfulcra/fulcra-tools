"""temp_signal_id must include the value in a signal's identity.

The id was derived from (key, observed_at, platform) only. A batch/drain
captures every item with the same `now`, so two same-key, same-platform items
with different values collided on one id — the second overwrote the first's
cache shard and compile deduped them to one, silently losing a signal.
"""
from fulcra_prefs.schema import temp_signal_id

ARGS = ("dining.cuisine.thai", "2026-06-01T12:00:00+00:00", "claude-code")


def test_temp_id_distinguishes_by_value():
    assert temp_signal_id(*ARGS, {"liked": True}) != temp_signal_id(*ARGS, {"liked": False})


def test_temp_id_same_for_identical_value():
    assert temp_signal_id(*ARGS, {"liked": True}) == temp_signal_id(*ARGS, {"liked": True})
