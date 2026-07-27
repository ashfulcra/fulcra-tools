"""Contract check: the installed duty-script set must not regrow retired paths.

Bus v3 (operator-ordered 2026-07-27) retired two discovery mechanisms:
resident ``coord-engine listen`` polling loops, and walking the
``/team/<team>/task/`` file tree to find work. A script that quietly
reintroduces either would put an agent back on the read path that degraded
~9 ticks in 10 and hid work. This test scans every shell script that
``scripts/coord-boss/bootstrap.sh`` installs.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "coord-boss"

#: Patterns whose presence means a retired read path came back. Kept narrow on
#: purpose: prose mentions (comments explaining the retirement) are fine, so we
#: match invocation shapes, not words.
_RETIRED_INVOCATIONS = [
    re.compile(r"coord-engine\s+listen\b"),
    re.compile(r"file\s+list\s+/?team/[^ ]*/task/"),
]


def _installed_scripts() -> list[pathlib.Path]:
    if not _SCRIPTS_DIR.is_dir():
        pytest.skip("scripts/coord-boss not present in this checkout")
    return sorted(p for p in _SCRIPTS_DIR.glob("*.sh"))


def test_scripts_dir_is_where_this_test_thinks_it_is():
    assert (_SCRIPTS_DIR / "bootstrap.sh").is_file(), (
        "scripts/coord-boss/bootstrap.sh not found — if the duty scripts "
        "moved, move this contract test's path with them")


def test_no_installed_script_invokes_a_retired_read_path():
    offenders: list[str] = []
    for script in _installed_scripts():
        text = script.read_text(encoding="utf-8")
        for pattern in _RETIRED_INVOCATIONS:
            if pattern.search(text):
                offenders.append(f"{script.name}: matches {pattern.pattern!r}")
    assert not offenders, (
        "retired bus read paths found in installed duty scripts "
        "(bus v3 contract, docs/coord/BUS-V3.md): " + "; ".join(offenders))


def test_retired_scripts_stay_deleted():
    for name in ("listener-loop.sh", "bus-sweep.sh"):
        assert not (_SCRIPTS_DIR / name).exists(), (
            f"{name} was retired by the bus v3 contract and must not return")
