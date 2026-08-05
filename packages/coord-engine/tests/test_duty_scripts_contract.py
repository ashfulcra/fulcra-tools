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


def test_queue_sweep_actually_prints_events_from_a_nonempty_window(tmp_path):
    """The bootstrap wrapper delegates to the transactional engine unchanged."""
    import subprocess
    script = _SCRIPTS_DIR / "queue-sweep.sh"
    stub = tmp_path / "coord-engine"
    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' "
        "'{\"kind\":\"directive\",\"slug\":\"fleet-wide\"}' "
        "'{\"type\":\"queue-delivery\",\"token\":\"tok\",\"event_count\":1}'\n"
    )
    stub.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(["bash", str(script), "tester"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "fleet-wide" in proc.stdout, "broadcast event not surfaced"
    assert '"token":"tok"' in proc.stdout


def test_queue_sweep_never_auto_commits_the_printed_token(tmp_path):
    """Print-before-process must not be recreated in the bootstrap wrapper."""
    import subprocess
    script = _SCRIPTS_DIR / "queue-sweep.sh"
    calls = tmp_path / "calls"
    stub = tmp_path / "coord-engine"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        "printf '%s\\n' "
        "'{\"type\":\"queue-delivery\",\"token\":\"tok\",\"event_count\":0}'\n"
    )
    stub.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(["bash", str(script), "tester"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0
    assert calls.read_text().strip() == "queue fulcra --agent tester --json"


def test_queue_sweep_transport_failure_is_degraded_rc3_not_quiet(tmp_path):
    """A failed engine read retains its rc and the literal failure doctrine."""
    import subprocess
    script = _SCRIPTS_DIR / "queue-sweep.sh"
    stub = tmp_path / "coord-engine"
    stub.write_text("#!/bin/sh\necho 'transport down' >&2\nexit 3\n")
    stub.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(["bash", str(script), "tester"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 3
    assert "QUEUE READ FAILED (rc=3)" in proc.stderr
    assert proc.stdout == "", "a failed window must not print events"
