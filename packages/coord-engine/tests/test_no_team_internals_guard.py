"""The guard script must be green ON THE COMMITTED TREE, and able to go red.

codex-reviewer, 581 r1: the first version passed its self-test and then flagged
its OWN source, because the self-test fixture spelled a public address as a
literal in a tracked file. I had verified it while the script was still
untracked — the check and the thing checked were in different states.

So this runs the real script against the real repo, which is the only shape
that would have caught that.
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "no-team-internals.sh"


def _run(*args):
    return subprocess.run(["sh", str(SCRIPT), *args], cwd=str(REPO),
                          capture_output=True, text=True)


def test_the_guard_is_green_on_the_committed_tree():
    r = _run()
    assert r.returncode == 0, (
        "the guard must pass on the tree that contains it — including its own "
        f"source:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    assert "self-test OK" in r.stdout


def test_the_self_test_alone_also_passes():
    """Separable, because a red self-test and a red scan mean different things:
    one says the guard is broken, the other says the tree is dirty."""
    r = _run("--self-test")
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_run_states_its_own_coverage_gap():
    """The IP arm skips generated geometry. A cap nobody can see reads as
    coverage, so the run prints what it excluded."""
    r = _run()
    assert "IP arm excludes" in r.stdout and ".svg" in r.stdout
