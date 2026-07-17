"""The release gate must never leave a rejected dmg where a release goes.

`gatekeeper_gate.sh` decides whether a signed+notarized dmg ships. A dmg can be
notarized AND stapled and still be REJECTED by Gatekeeper, and a rejected image
left at the canonical release path (`dist/Fulcra Collect.dmg`) is exactly what
someone reaches for to ship. So the retention rule is load-bearing:

  accepted              -> exit 0, success line, artifact stays
  rejected (default)    -> exit != 0, artifact DELETED, no success line
  rejected + override   -> exit != 0, artifact QUARANTINED to *.REJECTED.dmg
                           (never the release path), NOT DISTRIBUTABLE, no
                           success line

These drive the real script with a stubbed `spctl` on PATH — no signing,
notarization, or network involved.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_GATE = Path(__file__).resolve().parents[1] / "scripts" / "gatekeeper_gate.sh"
_SUCCESS_MARKER = "Built (signed + notarized + stapled)"


def _stub_spctl(bin_dir: Path, *, accept: bool) -> None:
    """Put a fake `spctl` on PATH that accepts or rejects."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "spctl"
    stub.write_text("#!/bin/sh\nexit %d\n" % (0 if accept else 3))
    stub.chmod(0o755)


def _run(tmp_path: Path, *, accept: bool, override: bool = False):
    bin_dir = tmp_path / "bin"
    _stub_spctl(bin_dir, accept=accept)
    dmg = tmp_path / "Fulcra Collect.dmg"
    dmg.write_text("pretend-dmg")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    if override:
        env["FULCRA_ALLOW_GATEKEEPER_FAIL"] = "1"
    proc = subprocess.run(
        ["bash", str(_GATE), str(dmg)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return proc, dmg, tmp_path / "Fulcra Collect.REJECTED.dmg"


def test_accepted_ships_and_keeps_the_artifact(tmp_path):
    proc, dmg, rejected = _run(tmp_path, accept=True)
    assert proc.returncode == 0
    assert _SUCCESS_MARKER in proc.stdout
    assert dmg.exists(), "an accepted dmg must remain at the release path"
    assert not rejected.exists()


def test_default_rejection_leaves_no_canonical_release_artifact(tmp_path):
    """The accidental-ship guard: nothing shippable survives a rejection."""
    proc, dmg, rejected = _run(tmp_path, accept=False)
    assert proc.returncode != 0
    assert not dmg.exists(), "a rejected dmg must NOT be left at the release path"
    assert not rejected.exists(), "default rejection retains nothing"
    assert _SUCCESS_MARKER not in proc.stdout


def test_override_quarantines_the_artifact_off_the_release_path(tmp_path):
    proc, dmg, rejected = _run(tmp_path, accept=False, override=True)
    assert proc.returncode != 0
    assert not dmg.exists(), "even under override, nothing may sit at the release path"
    assert rejected.exists(), "override retains the artifact for diagnosis"
    assert "NOT DISTRIBUTABLE" in proc.stderr
    assert _SUCCESS_MARKER not in proc.stdout


@pytest.mark.parametrize("override", [False, True])
def test_rejection_never_prints_the_success_line(tmp_path, override):
    proc, _, _ = _run(tmp_path, accept=False, override=override)
    assert _SUCCESS_MARKER not in proc.stdout
    assert _SUCCESS_MARKER not in proc.stderr
