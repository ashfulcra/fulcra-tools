"""Contract test: every test module that defines a module-level ``NOW`` also
PINS the clock. Stops the repo's recurring date-boundary CI-flake class at author
time instead of 72 hours later in someone else's CI.

The flake: a module fixes a ``NOW`` for its DATA, but the code under test computes
windows/staleness from the REAL clock (``cli._now()`` or a module ``_now``). Once
wall-clock time advances past ``NOW + window`` the suite flips RED for good until
someone pins it — test_threads (#378, NOW+3d), then this whole wave. The remedy
(template: ``tests/test_threads.py``) is an autouse fixture that monkeypatches the
relevant ``_now`` to a ``PINNED_NOW`` at/just after ``NOW``; never weaken the
assertion, derive relative ages from ``PINNED_NOW``.

This guard makes the NEXT unpinned NOW-module fail here, immediately: for every
``tests/test_*.py`` that carries a top-level ``NOW =`` / ``_NOW =`` literal, it
requires the module to also contain a ``monkeypatch.setattr(<mod>, "_now", ...)``
— satisfied by the autouse fixture OR by an in-body clock move (a test that MOVES
time pins its own ``_now``, which is still a pin). A module that genuinely must
run against the real clock goes in ``_CLOCK_PIN_EXEMPT`` WITH a reason.

Cheap-beats-clever: grep the tracked test files, match two regexes.
"""

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: a module-level ``NOW =`` / ``_NOW =`` assignment (no leading indent -> top
#: level, never a NOW bound inside a function/class body).
NOW_RE = re.compile(r"^(?:NOW|_NOW)\s*=", re.MULTILINE)

#: a clock pin — ``monkeypatch.setattr(<mod>, "_now", ...)`` — whether in the
#: autouse fixture or in a test body that moves time.
PIN_RE = re.compile(r"""monkeypatch\.setattr\(\s*[\w.]+\s*,\s*["']_now["']""")

#: modules that must run against the real clock and thus cannot pin. Each entry
#: needs a one-line reason. Prefer pinning — this is a last resort. (Empty: the
#: v1.6.6 clock-pin wave pinned all 16 then-unpinned NOW-modules.)
_CLOCK_PIN_EXEMPT: dict[str, str] = {
    # "test_example.py": "exercises real wall-clock drift on purpose (see ...)",
}


def _now_modules():
    """(filename, has_pin) for every tracked ``tests/test_*.py`` that defines a
    top-level NOW literal, excluding this guard itself."""
    out = []
    for p in sorted(TESTS_DIR.glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        text = p.read_text(encoding="utf-8")
        if NOW_RE.search(text):
            out.append((p.name, bool(PIN_RE.search(text))))
    return out


def test_every_now_module_pins_the_clock():
    """No test module may define a module-level NOW without also pinning a
    ``_now`` — the date-boundary flake class is un-mergeable here."""
    unpinned = [
        name for name, pinned in _now_modules()
        if not pinned and name not in _CLOCK_PIN_EXEMPT
    ]
    assert not unpinned, (
        "these test modules define a top-level NOW but never pin the clock "
        "(monkeypatch.setattr(<mod>, \"_now\", ...)): "
        + ", ".join(unpinned)
        + " — add the autouse `_pin_module_clock` fixture (template: "
        "tests/test_threads.py; derive relative ages from PINNED_NOW, never "
        "weaken assertions), or, only if it MUST use the real clock, add it to "
        "_CLOCK_PIN_EXEMPT with a reason."
    )


def test_exemptions_are_real_now_modules():
    """An exemption for a module that no longer defines a NOW (renamed/deleted/
    pinned) is stale — remove it so the allowlist can't rot into a silent hole."""
    now_module_names = {name for name, _ in _now_modules()}
    stale = [name for name in _CLOCK_PIN_EXEMPT if name not in now_module_names]
    assert not stale, (
        "stale _CLOCK_PIN_EXEMPT entries (module gone or no longer defines a "
        "top-level NOW): " + ", ".join(stale) + " — drop them."
    )
