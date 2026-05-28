"""Pure hex string constants for the Fulcra Collect menubar.

This module imports nothing macOS-specific. The PyObjC factories that
turn these into NSColor objects live in theme.colors. The hex values
are sampled from the brand reference materials and tuned to read on
white.
"""
from __future__ import annotations

# Non-negotiable: the app's background is pure white.
BG = "#FFFFFF"
BG_ELEV = "#F7F8FA"
BORDER = "#E5E7EB"

TEXT = "#0B0D17"
TEXT_SECONDARY = "#5A6072"
TEXT_TERTIARY = "#9CA3AF"

ACCENT_VIOLET = "#6B5BEE"
ACCENT_VIOLET_HOVER = "#5045E5"
ACCENT_VIOLET_TINT = "#F1EFFE"

ACCENT_MINT = "#2D8267"
ACCENT_MINT_HOVER = "#226A53"
ACCENT_MINT_TINT = "#E5F4EE"

ACCENT_CYAN = "#10C7BE"
ACCENT_CYAN_DEEP = "#0E9E97"

WARNING = "#B7791F"
ERROR = "#DC2626"

# A 3-stop gradient (cyan → mid → violet) — used sparingly on the
# running-pulse layer and the bootstrap card's accent stripe.
BRAND_GRADIENT = ("#10C7BE", "#4F7BE8", "#8B5BEE")


def tokens() -> dict[str, object]:
    """All exported palette values keyed by their name. Used by the
    test suite to verify hex format and uniqueness."""
    return {k: v for k, v in globals().items() if k.isupper()}
