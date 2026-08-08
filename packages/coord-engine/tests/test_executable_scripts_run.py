"""Every file the repo marks executable must actually BE executable.

Found by codex-reviewer on the wall-14/15 branch: `scripts/coord/router-watchdog.py`
shipped with mode 100755 and a usage line documenting `router-watchdog.py <config>`,
and no shebang. Direct execution fails in the kernel before a single line of the
script runs — so the liveness check that exists to prove a service is alive could
not itself start, and the failure looks like the script "not working" rather than
like a missing first line.

This is the same shape as the walls the script was written for: a mechanism that
is present, configured, and unable to run. The test therefore executes each file
BY PATH rather than through an interpreter, because running it as
`python3 script.py` is exactly the check that cannot see this defect.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]


def _executable_scripts() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for d in ("scripts",):
        root = _REPO / d
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and os.access(p, os.X_OK):
                out.append(p)
    return out


def test_there_are_executable_scripts_to_check():
    """Vacuity guard: an empty parametrize would make the suite below green
    while checking nothing, which is the failure mode this file is about."""
    assert _executable_scripts(), "no executable scripts found — the scan is broken"


@pytest.mark.parametrize("script", _executable_scripts(), ids=lambda p: p.name)
def test_an_executable_script_has_a_shebang(script: pathlib.Path):
    first = script.read_bytes()[:2]
    assert first == b"#!", (
        f"{script.relative_to(_REPO)} is mode {oct(script.stat().st_mode)[-3:]} "
        f"but has no shebang — executing it by path fails before it runs"
    )


@pytest.mark.parametrize("script", _executable_scripts(), ids=lambda p: p.name)
def test_an_executable_script_can_be_LAUNCHED_by_path(script: pathlib.Path):
    """Launch it and assert the OS could start it.

    We do not assert on the exit code: these scripts talk to a store and will
    fail for their own reasons under test. What must not happen is a failure to
    EXEC — OSError, or the shell's syntax error from a shebang-less file being
    handed to /bin/sh.
    """
    try:
        cp = subprocess.run([str(script)], capture_output=True, text=True,
                            timeout=20, cwd=str(_REPO))
    except OSError as exc:            # ENOEXEC etc — the defect this catches
        pytest.fail(f"{script.relative_to(_REPO)} could not be executed: {exc}")
    except subprocess.TimeoutExpired:
        return                        # it started; that is all this asserts
    combined = (cp.stdout or "") + (cp.stderr or "")
    assert "syntax error" not in combined.lower(), (
        f"{script.relative_to(_REPO)} was interpreted by the shell — "
        f"missing or wrong shebang:\n{combined[:400]}"
    )
