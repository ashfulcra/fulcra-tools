from __future__ import annotations

import re

from fulcra_menubar.theme import palette

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def test_palette_tokens_are_all_valid_hex():
    for name, value in palette.tokens().items():
        if name == "BRAND_GRADIENT":
            assert isinstance(value, tuple) and len(value) == 3
            for stop in value:
                assert HEX_RE.match(stop), f"{name} stop {stop!r} is not #RRGGBB"
        else:
            assert HEX_RE.match(value), f"{name} = {value!r} is not #RRGGBB"


def test_palette_tokens_are_unique_per_role():
    role_values = {
        k: v for k, v in palette.tokens().items()
        if k not in {"BRAND_GRADIENT"}
    }
    duplicates = [k for k, v in role_values.items()
                  if list(role_values.values()).count(v) > 1]
    assert duplicates == [], f"duplicate hex values in palette: {duplicates}"


def test_bg_is_pure_white():
    assert palette.BG == "#FFFFFF"
