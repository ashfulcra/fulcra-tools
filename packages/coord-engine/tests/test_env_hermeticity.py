"""Wall: the suite's answer must not depend on who is running it.

Twenty-five tests once failed if and only if ``FULCRA_COORD_AGENT`` was set —
and line one of every agent's standing wake prompt is
``export FULCRA_COORD_AGENT=<identity>``. So the documented procedure made a
green tree report 25 failures, and whether the suite passed depended on whose
shell invoked it.

The conftest fixture fixes those tests. This file is the wall that keeps them
fixed, and it exists because the obvious alternative is not a test at all:
asserting that the fixture deletes the variables would pin the *mechanism*
while the thing anyone cares about — that the outcome is the same either way —
could rot underneath it. A test that cannot observe the failure it is named
after is a diagnostic, not a wall. So this runs real tests, twice, in a
subprocess, and compares outcomes.

It also failed to be caught by every control you would reach for: stashing the
diff, merging main, and probing ``origin/main`` in a clean worktree all agreed
the tree was broken, because all three inherited the same polluted shell.
Controls that share the contaminant cannot detect the contaminant. Only running
the same code under two deliberately different environments can.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

from coord_engine.cli import INHERITED_ENV
from coord_engine import classifier

#: Files that actually broke under an inherited environment, so the wall
#: exercises the real regressions rather than a sample chosen for convenience.
#: The first four broke on identity; the rest broke on an exported channel, and
#: every one of those is a test whose premise is an ABSENT or unreadable records
#: config that the variable then supplies.
SUBSET = (
    "test_dispatch_companion.py",
    "test_records_write.py",
    "test_records_transactional.py",
    "test_queue_contract_engine_adapter.py",
    "test_authority_currency.py",
    "test_bus_tags.py",
    "test_directive_collision.py",
    "test_json_purity.py",
    "test_queue_terminal_states.py",
    "test_read_retry.py",
)

#: Measured, NOT yet walled. Recorded here rather than in someone's memory,
#: because a coverage gap nobody can see reads as coverage.
#:
#: ``COORD_TRANSPORT_HTTP`` perturbs the suite, but there at least some failures
#: are the variable doing its job — a test asserting "no subprocess" SHOULD fail
#: when you disable the HTTP path. Separating those from real leaks is real work
#: and it has not been done, so it is named here rather than waved through.
#:
#: ``COORD_RECORDS_TYPE`` used to sit in this list and is now walled: all 8 of
#: its failures turned out to be tests whose premise is an absent config, with
#: no legitimate case among them. Checked, not assumed — a sibling in the same
#: family, ``COORD_RECORDS_API_VERSION``, was measured and leaks NOTHING, which
#: is why it is in neither list.
NOT_YET_WALLED = ("COORD_TRANSPORT_HTTP",)

_TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _run_subset(overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run SUBSET in a child process under a deliberately controlled env."""
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_ENV}
    env.update(overrides)
    # -p no:cacheprovider: the child must not race the parent run's .pytest_cache
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *[str(_TESTS_DIR / name) for name in SUBSET]],
        env=env, capture_output=True, text=True, cwd=str(_TESTS_DIR),
    )


def test_the_suite_answers_the_same_with_and_without_an_exported_identity():
    # ~5s: two real subprocess pytest runs. Deliberately not marked slow or
    # opt-in — a wall that only runs when someone remembers to ask for it is
    # not a wall, and this one costs less than the day it already saved.
    clean = _run_subset({})
    assert clean.returncode == 0, (
        "the control run must be GREEN, or this wall proves nothing about the "
        f"variable:\n{clean.stdout[-3000:]}"
    )

    dirty = _run_subset(dict(INHERITED_ENV))
    assert dirty.returncode == 0, (
        "these tests fail when the caller has exported a coordination identity "
        "— which every wake prompt tells every agent to do, so the suite is "
        "reporting failure on healthy code for anyone following the documented "
        f"procedure:\n{dirty.stdout[-3000:]}"
    )


def test_the_walled_variables_are_the_ones_the_fixture_clears():
    """The wall and the fixture must name the SAME variables.

    Two hand-maintained lists drift, and the drift is silent: a variable added
    to the fixture but not the wall is unprotected while looking protected. This
    is cheap precisely because it only guards that coupling — it is not the
    wall, and it is not a substitute for the run above.
    """
    assert INHERITED_ENV, "the fixture must declare what it clears"
    assert set(INHERITED_ENV).isdisjoint(NOT_YET_WALLED), (
        "a variable cannot be both walled and listed as an unwalled gap"
    )


def test_identity_resolver_catches_environment_identity_losing_to_persisted_identity():
    """The env-over-persisted mutation would reintroduce host self-contests."""
    assert classifier.resolve_identity(
        environ={"FULCRA_COORD_AGENT": "exported-session"},
        persisted="old-persisted-session",
        hostname=lambda: "host",
    ) == "exported-session"
