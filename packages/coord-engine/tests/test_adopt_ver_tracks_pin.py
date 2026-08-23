"""A pin move must change the adoption-claim generation.

codex-reviewer, 681 r1. `VER` is embedded in `SLUG`, and `SLUG` keys the durable
claim marker `_coord/bus-v3/adopted/<slug>.txt`. When `VER` is hand-set it stops
tracking `PIN`: the 2.0.3 and 2.0.4 pins both shipped `pp-0a093dba` — a value
matching NEITHER pin's sha — and five agents were holding
`adopted-pp-0a093dba-<agent>-rc0.txt` in the store. Every one of them would have
read its own previous marker on the 2.0.4 rollout and skipped the claim.

The install still reaches the new PIN, so nothing looks broken; only the
adoption CENSUS goes dark, precisely when a pin PR exists to move the fleet.
That is the failure mode these tests exist to make impossible: they assert the
derivation, not one correct value, because a correct value is exactly what the
last two releases also had at the moment they were written.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "adopt-latest.sh"


def _assign_lines() -> str:
    """The PIN and VER assignments, lifted verbatim from the shipped script."""
    text = SCRIPT.read_text()
    lines = [ln for ln in text.splitlines()
             if re.match(r'^(PIN|VER)=', ln)]
    assert len(lines) == 2, f"expected one PIN= and one VER=, got {lines}"
    assert lines[0].startswith("PIN="), "PIN must be assigned before VER"
    return "\n".join(lines)


def _ver_for(pin: str) -> str:
    """Evaluate the shipped VER expression against a substituted PIN."""
    body = re.sub(r'^PIN=.*$', f'PIN="{pin}"', _assign_lines(), flags=re.M)
    out = subprocess.run(["sh", "-c", body + '\nprintf %s "$VER"'],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_moving_the_pin_moves_the_claim_generation():
    """THE regression: two different pins may never yield the same VER."""
    a = _ver_for("0976cd815d6f88a02adca00e10b6a9eb265b8939")
    b = _ver_for("57909052144bd8b250a88f475b2826be2b70606f")
    assert a != b, (
        "VER did not change with PIN, so the claim marker key is reused and "
        "every agent with a matching prior outcome skips its adoption claim")


def test_ver_is_derived_from_the_pin_not_hand_set():
    """A literal VER is the defect itself — it is correct only until the next
    pin move, which is when nobody is looking at it."""
    ver_line = [ln for ln in SCRIPT.read_text().splitlines()
                if ln.startswith("VER=")][0]
    assert "$" in ver_line and "PIN" in ver_line, (
        f"VER is hand-set ({ver_line!r}); it must be derived from PIN so a pin "
        f"move cannot silently reuse the previous claim generation")


def test_the_derived_ver_actually_contains_the_pin_prefix():
    """Derivation must be FROM the pin, not merely non-constant — a counter or
    a timestamp would pass the inequality test while losing the link to the
    build the claim is about."""
    pin = "0976cd815d6f88a02adca00e10b6a9eb265b8939"
    assert pin[:8] in _ver_for(pin)


def test_the_shipped_ver_matches_the_shipped_pin():
    """End to end on the real file, so a future hand-edit that reintroduces a
    literal is caught even if it happens to differ from the previous one."""
    text = SCRIPT.read_text()
    pin = re.search(r'^PIN="([0-9a-f]{40})"', text, re.M).group(1)
    out = subprocess.run(["sh", "-c", _assign_lines() + '\nprintf %s "$VER"'],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == f"pp-{pin[:8]}", (
        f"shipped VER {out.stdout!r} does not track shipped PIN {pin[:8]}")
