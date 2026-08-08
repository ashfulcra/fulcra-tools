"""Every file the repo marks executable must actually BE executable.

Found by codex-reviewer: ``scripts/coord/router-watchdog.py`` shipped with mode
100755 and a usage line documenting ``router-watchdog.py <config>``, and no
shebang. Direct execution fails in the kernel before a single line runs — so the
liveness check that exists to prove a service is alive could not itself start,
and the failure looks like the script "not working" rather than like a missing
first line. Same shape as the walls it was written for: a mechanism present,
configured, and unable to start.

TWO CHECKS, AND THE SPLIT IS THE POINT
--------------------------------------
The **static** check reads bytes and is therefore safe to run against every
executable in the repo. The **behavioural** check starts a process, so it runs
ONLY against an explicit allowlist.

The first version of this file launched every mode-755 file under ``scripts/``
with no arguments — a set that includes setup, update, bootstrap and
maintenance entry points. Running the suite could have installed software,
mutated the host, or touched a live store; ``queue-sweep.sh`` in particular
advances a cursor, and a cursor-advancing read whose output nobody processes
silently discards wake hints. codex-reviewer refused to run those cases and was
right to. A test that must not be run is not a test.

So: prove the property everywhere by reading, and prove executability only where
launching is known to be inert.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]

#: Executables that are SAFE to launch with no arguments: they parse argv,
#: find nothing usable, and exit. Adding to this list is a claim that the file
#: does no I/O and mutates nothing before it validates its arguments — check
#: that by reading it, not by running it and seeing what happens.
_SAFE_TO_LAUNCH = {
    "scripts/coord/router-watchdog.py",
}


def _executable_scripts() -> list[pathlib.Path]:
    root = _REPO / "scripts"
    if not root.is_dir():
        return []
    return [p for p in sorted(root.rglob("*"))
            if p.is_file() and os.access(p, os.X_OK)]


def test_there_are_executable_scripts_to_check():
    """Vacuity guard: an empty parametrize would make the suite below green
    while checking nothing, which is the failure mode this file is about."""
    assert _executable_scripts(), "no executable scripts found — the scan is broken"


@pytest.mark.parametrize("script", _executable_scripts(), ids=lambda p: p.name)
def test_an_executable_script_has_a_shebang(script: pathlib.Path):
    """Static, repo-wide, and reads only. This is the assertion that generalises."""
    first = script.read_bytes()[:2]
    assert first == b"#!", (
        f"{script.relative_to(_REPO)} is mode {oct(script.stat().st_mode)[-3:]} "
        f"but has no shebang — executing it by path fails before it runs"
    )


def test_the_safe_list_names_files_that_exist():
    """A stale allowlist entry would silently shrink the behavioural check to
    nothing while still looking like coverage."""
    for rel in _SAFE_TO_LAUNCH:
        assert (_REPO / rel).is_file(), f"allowlisted script is gone: {rel}"


@pytest.mark.parametrize("rel", sorted(_SAFE_TO_LAUNCH))
def test_an_allowlisted_script_can_be_LAUNCHED_by_path(rel: str, tmp_path):
    """Launch it and assert the OS could START it.

    Running it as ``python3 script.py`` is exactly the check that cannot see a
    missing shebang, so this invokes the path itself. The exit code is not
    asserted — with no arguments the script fails for its own reasons. What must
    not happen is a failure to EXEC: ``OSError``, or the shell's syntax error
    from a shebang-less file handed to ``/bin/sh``.

    ``cwd`` is a tmp dir so nothing it might touch is the real tree.
    """
    script = _REPO / rel
    try:
        cp = subprocess.run([str(script)], capture_output=True, text=True,
                            timeout=20, cwd=str(tmp_path))
    except OSError as exc:            # ENOEXEC etc — the defect this catches
        pytest.fail(f"{rel} could not be executed: {exc}")
    except subprocess.TimeoutExpired:
        return                        # it started; that is all this asserts
    combined = (cp.stdout or "") + (cp.stderr or "")
    assert "syntax error" not in combined.lower(), (
        f"{rel} was interpreted by the shell — missing or wrong shebang:\n"
        f"{combined[:400]}"
    )
