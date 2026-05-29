"""Geometry tests for the Plugins-tab row-height calculation.

A plugin row stacks content from BOTH ends — name + description (+ the
scheduled-only interval block) grow down from the top, while the credential
rows / Run-now button grow up from the bottom. The height must clear the sum
of both stacks. A regression here (height formula drifting from where the row
actually draws its subviews) produced two visible bugs: a phantom interval
gap under every non-scheduled row, and credential fields colliding with the
description once that gap was naively removed. These tests pin the invariant.
"""
import pytest

from fulcra_menubar.preferences.plugins_tab import (
    _ROW_BOTTOM_MARGIN,
    _ROW_CRED_BASE,
    _ROW_CRED_STEP,
    _ROW_INTERVAL_BLOCK,
    _ROW_NAME_H,
    _ROW_STACK_GAP,
    _plugin_row_height,
)


def _top_stack(*, scheduled: bool, desc_h: float) -> float:
    """Height the top-anchored content occupies (name + desc + interval)."""
    return _ROW_NAME_H + desc_h + (_ROW_INTERVAL_BLOCK if scheduled else 0)


def _bottom_stack(*, scheduled: bool, n_credentials: int) -> float:
    """Height the bottom-anchored content occupies (creds / Run-now / margin)."""
    if n_credentials:
        return _ROW_CRED_BASE + _ROW_CRED_STEP * n_credentials
    return _ROW_CRED_BASE if scheduled else _ROW_BOTTOM_MARGIN


# Representative matrix: both kinds × no/short/tall descriptions × 0/1/3 creds.
_CASES = [
    (scheduled, desc_h, n)
    for scheduled in (True, False)
    for desc_h in (0.0, 32.0, 80.0)
    for n in (0, 1, 3)
]


@pytest.mark.parametrize("scheduled,desc_h,n", _CASES)
def test_row_height_clears_both_stacks(scheduled, desc_h, n):
    """The row must be tall enough that the bottom stack never overlaps the
    top stack — i.e. (height - top_stack) leaves at least the gap above the
    bottom stack."""
    height = _plugin_row_height(
        scheduled=scheduled, desc_h=desc_h, n_credentials=n
    )
    top = _top_stack(scheduled=scheduled, desc_h=desc_h)
    bottom = _bottom_stack(scheduled=scheduled, n_credentials=n)
    # The top stack's lowest edge sits at (height - top); the bottom stack's
    # highest edge sits at `bottom`. They must not cross, with the gap spare.
    assert height - top >= bottom
    assert height - top - bottom == pytest.approx(_ROW_STACK_GAP)


def test_non_scheduled_no_cred_row_is_compact():
    """A manual/service plugin with no credentials should NOT reserve the
    scheduled-only interval block — that phantom gap was the original bug."""
    manual = _plugin_row_height(scheduled=False, desc_h=32.0, n_credentials=0)
    scheduled = _plugin_row_height(scheduled=True, desc_h=32.0, n_credentials=0)
    # The scheduled row is taller by exactly the interval block plus the
    # difference between the Run-now slot and the bare bottom margin.
    assert scheduled - manual == _ROW_INTERVAL_BLOCK + (
        _ROW_CRED_BASE - _ROW_BOTTOM_MARGIN
    )


def test_each_credential_adds_one_step():
    """Adding a credential grows the row by exactly one credential step,
    regardless of kind."""
    for scheduled in (True, False):
        one = _plugin_row_height(
            scheduled=scheduled, desc_h=0.0, n_credentials=1
        )
        two = _plugin_row_height(
            scheduled=scheduled, desc_h=0.0, n_credentials=2
        )
        assert two - one == _ROW_CRED_STEP
