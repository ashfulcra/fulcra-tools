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
    """When a bottom stack exists (credentials, or a scheduled Run-now slot),
    the row clears both stacks plus the gap. When there is NO bottom stack
    (manual/service plugin, no credentials), the content is top-anchored and
    the row adds only the small bottom margin — no reserved slot, no gap."""
    height = _plugin_row_height(
        scheduled=scheduled, desc_h=desc_h, n_credentials=n
    )
    top = _top_stack(scheduled=scheduled, desc_h=desc_h)
    has_bottom_stack = bool(n) or scheduled
    if has_bottom_stack:
        bottom = _bottom_stack(scheduled=scheduled, n_credentials=n)
        assert height - top >= bottom
        assert height - top - bottom == pytest.approx(_ROW_STACK_GAP)
    else:
        # Top-anchored only: just a small bottom margin beneath the desc.
        assert height - top == pytest.approx(_ROW_BOTTOM_MARGIN)


def test_non_scheduled_no_cred_row_is_compact():
    """A manual/service plugin with no credentials reserves only a small bottom
    margin below its description — no credential/Run-now slot, no stack gap.
    This is the fix for the loose Plugins-tab spacing (big gaps between rows)."""
    manual = _plugin_row_height(scheduled=False, desc_h=32.0, n_credentials=0)
    # Exactly name band + description + the small bottom margin. Nothing else.
    assert manual == _ROW_NAME_H + 32.0 + _ROW_BOTTOM_MARGIN
    # And a scheduled row is taller by the interval block + Run-now slot + gap,
    # minus the bottom margin the manual row already includes.
    scheduled = _plugin_row_height(scheduled=True, desc_h=32.0, n_credentials=0)
    assert scheduled - manual == (
        _ROW_INTERVAL_BLOCK + _ROW_CRED_BASE + _ROW_STACK_GAP - _ROW_BOTTOM_MARGIN
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
