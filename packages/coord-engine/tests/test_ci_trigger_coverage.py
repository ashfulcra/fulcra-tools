"""Every package with tests must be reachable by the job that runs them.

THE FAILURE THIS EXISTS FOR (#523, 2026-08-06). `macos.yml`'s test job runs
``pytest packages/`` — the entire workspace, ~4479 tests. But it TRIGGERS on an
enumerated path list. A package absent from that list can be changed with no
test job at all, while its tests keep passing many times a day on other people's
PRs. That is how a tested function was deleted with nothing red: the tests
existed, ran constantly, and never once ran on the PR that removed it.

The gap is invisible by construction — a green board is the symptom. Three
agents (myself included) then misread it in the other direction, inferring from
the trigger list that the tests were never executed at all. Both errors came
from reading the workflow config as a proxy for what CI does. This test asserts
the property directly instead.

Placed in coord-engine deliberately: `uv-workspace.yml` has no path filter and
runs this suite on EVERY pull request, so the guard fires even when the macOS
job does not. A guard living only behind the trigger it polices could not catch
the trigger going wrong.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MACOS_WORKFLOW = REPO / ".github" / "workflows" / "macos.yml"


def _trigger_paths(text: str) -> list[list[str]]:
    """Every ``paths:`` block's globs, in order, as a list per block.

    Deliberately a line scan rather than a YAML parse: coord-engine is
    stdlib-only and this must not add a dependency to run.
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped in ("paths:", "paths-ignore:"):
            current = []
            blocks.append(current)
            continue
        if current is not None:
            m = re.match(r"^-\s*['\"](.+)['\"]$", stripped)
            if m:
                current.append(m.group(1))
            elif stripped and not stripped.startswith("#"):
                current = None
    return blocks


def _covers(globs: list[str], package: str) -> bool:
    """Would a change inside ``packages/<package>/`` match any glob?

    ``fnmatch``'s ``*`` spans ``/``, which makes it a close enough stand-in for
    GitHub's ``**`` for the shapes used here.
    """
    probe = f"packages/{package}/some_file.py"
    return any(fnmatch.fnmatch(probe, g) for g in globs)


def _packages_with_tests() -> list[str]:
    return sorted(
        p.parent.name for p in (REPO / "packages").glob("*/tests") if p.is_dir()
    )


def test_the_workflow_and_its_packages_are_actually_there():
    """Positive control: an empty scan must never be able to read as a pass."""
    assert MACOS_WORKFLOW.is_file(), MACOS_WORKFLOW
    packages = _packages_with_tests()
    assert len(packages) > 5, f"suspiciously few test packages found: {packages}"
    assert _trigger_paths(MACOS_WORKFLOW.read_text()), "no paths: block parsed"


def test_macos_job_still_runs_the_whole_tree():
    """The guard below is only meaningful while the job runs every package.
    If this ever narrows to an allow-list, the trigger check stops being
    sufficient and this file must be rewritten, not deleted."""
    assert re.search(r"pytest\s+packages/\s", MACOS_WORKFLOW.read_text()), (
        "macos.yml no longer runs `pytest packages/`; trigger coverage is no "
        "longer equivalent to test coverage — rewrite this guard"
    )


@pytest.mark.parametrize("package", _packages_with_tests())
def test_every_tested_package_triggers_the_suite(package):
    blocks = _trigger_paths(MACOS_WORKFLOW.read_text())
    uncovered = [i for i, globs in enumerate(blocks) if not _covers(globs, package)]
    assert not uncovered, (
        f"packages/{package}/ has tests but does not match the trigger paths of "
        f"block(s) {uncovered} in macos.yml. Its tests would run on everyone "
        f"else's PRs and never on a PR that changes it — the #523 failure. Add "
        f"a matching glob to EVERY paths: block."
    )
