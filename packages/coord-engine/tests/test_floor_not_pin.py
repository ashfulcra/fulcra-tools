"""`current_engine_version` is a FLOOR, and the rendering must say so.

coord-boss's ruling, 2026-08-09: the field is semantically a minimum — the
comparison is at-or-above, so an engine at or past it is accepted — and **the
name is the bug**. The field stays (wire compatibility); what it RENDERS
changes.

Why this is not cosmetic. Two different authorities were both being called
"the pin":

  - the **commit SHA** in `_coord/bus-v3/adopt-latest.sh` — what to install;
  - this **semver field** in `_coord/bus-v3/records.json` — the minimum engine
    the fleet accepts.

The collision cost real time. coord-boss cut a pin at `d3f0aaa5`, adopted it,
then reported `doctor --self` still saying "the fleet authority pin is v1.10.0"
and asked where that read from — reasonably expecting the pin they had just
moved. It reads the other authority entirely, which adoption cannot touch.

And it hid a second fact: the engine had been `1.11.0` at all three recent pins
while the field said `1.10.0`, so the floor had been trailing the shipped engine
for three pin moves. Because the check is at-or-above, everyone rendered
CURRENT and nothing surfaced it.
"""

from __future__ import annotations

from coord_engine import records

FIELD = records.CURRENT_ENGINE_FIELD


def test_the_field_name_is_unchanged_for_wire_compatibility():
    """Only the RENDERING moves. Every host reads this key; renaming it would
    make every un-upgraded engine see an authority with no floor at all."""
    assert FIELD == "current_engine_version"


def test_a_current_engine_is_told_it_meets_a_FLOOR_not_a_pin():
    state, line = records.authority_currency_state(
        {FIELD: "1.10.0"}, engine_version="1.11.0")
    assert state == "current"
    assert "minimum" in line or "floor" in line, (
        f"the rendering must name this a floor/minimum, not a pin: {line!r}")
    assert "pin" not in line.lower(), (
        "'pin' collides with the adopt-latest.sh COMMIT that agents actually "
        f"call the pin, which is the confusion this fixes: {line!r}")


def test_the_stale_warning_also_says_floor():
    """The warning is where an operator acts, so it must name the right
    authority — telling someone their engine is below 'the pin' sends them to
    look at the wrong file."""
    line = records.authority_currency({FIELD: "1.11.0"}, engine_version="1.6.6")
    assert line is not None
    assert "minimum" in line or "floor" in line, line
    assert "pin" not in line.lower(), line


def test_the_rendering_still_names_the_two_versions():
    """The rename must not cost information: both numbers still appear, or an
    operator cannot tell how far behind they are."""
    _, line = records.authority_currency_state(
        {FIELD: "1.10.0"}, engine_version="1.11.0")
    assert "1.11.0" in line and "1.10.0" in line, line


def test_at_or_above_is_still_the_accepted_relation():
    """Pinning the semantics the name now describes: equal passes, above passes,
    below warns. If this ever became strict equality the word 'floor' would be
    a lie."""
    for own, expect in (("1.10.0", "current"), ("1.11.0", "current"),
                        ("1.9.9", "stale")):
        state, _ = records.authority_currency_state(
            {FIELD: "1.10.0"}, engine_version=own)
        assert state == expect, f"engine {own} vs floor 1.10.0 -> {state}"


def test_an_absent_floor_is_unknown_not_current():
    """Unchanged contract, re-pinned here because the rename touches these
    lines: no floor declared means comparison is impossible, which is not the
    same as being current."""
    state, line = records.authority_currency_state({}, engine_version="1.11.0")
    assert state == "unknown"
    assert "not the same as current" in line or "impossible" in line, line
