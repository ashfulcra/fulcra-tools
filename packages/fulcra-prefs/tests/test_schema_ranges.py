"""strength and confidence range validation.

The injected block tells the consuming agent "weights in [-1, 1]" and compile
selects winners by `effective_weight * confidence`. An out-of-range value
silently breaks both contracts: a confidence of 99 lets a junk signal dominate
selection, and a strength of 9.99 renders as `[+9.99]` in the bootstrap block.
Reject at construction, the same way half_life_days is rejected, so a poisoned
signal never reaches the cache.
"""
import pytest

from test_schema import make_signal


@pytest.mark.parametrize("strength", [-1.0, -0.5, 0.0, 0.5, 1.0])
def test_strength_in_range_accepted(strength):
    assert make_signal(strength=strength).strength == strength


@pytest.mark.parametrize("strength", [1.0001, 9.99, -1.0001, -50.0])
def test_strength_out_of_range_rejected(strength):
    with pytest.raises(ValueError):
        make_signal(strength=strength)


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_confidence_in_range_accepted(confidence):
    assert make_signal(confidence=confidence).confidence == confidence


@pytest.mark.parametrize("confidence", [-0.0001, -1.0, 1.0001, 99.0])
def test_confidence_out_of_range_rejected(confidence):
    with pytest.raises(ValueError):
        make_signal(confidence=confidence)
