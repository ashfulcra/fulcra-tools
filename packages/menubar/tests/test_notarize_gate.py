"""A failed Apple notarization step must leave no release-path artifact."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


_GATE = Path(__file__).resolve().parents[1] / "scripts" / "notarize_dmg.sh"


def _stub_xcrun(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "xcrun"
    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$XCRUN_LOG\"\n"
        "if [ -n \"$FAIL_ON\" ]; then\n"
        "  case \"$*\" in\n"
        "    *\"$FAIL_ON\"*) exit 17 ;;\n"
        "  esac\n"
        "fi\n"
    )
    stub.chmod(0o755)


def _failing_command(bin_dir: Path, name: str) -> None:
    stub = bin_dir / name
    stub.write_text("#!/bin/sh\nexit 13\n")
    stub.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    fail_on: str = "",
    mv_fails: bool = False,
    rm_fails: bool = False,
):
    bin_dir = tmp_path / "bin"
    _stub_xcrun(bin_dir)
    if mv_fails:
        _failing_command(bin_dir, "mv")
    if rm_fails:
        _failing_command(bin_dir, "rm")
    dmg = tmp_path / "Fulcra Collect.dmg"
    dmg.write_text("pretend-dmg")
    log = tmp_path / "xcrun.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XCRUN_LOG": str(log),
        "FAIL_ON": fail_on,
    }
    proc = subprocess.run(
        ["bash", str(_GATE), str(dmg), "test-profile"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    quarantined = tmp_path / "Fulcra Collect.NOT_NOTARIZED.dmg"
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, dmg, quarantined, calls


def test_success_keeps_release_path_after_all_apple_checks(tmp_path):
    proc, dmg, quarantined, calls = _run(tmp_path)

    assert proc.returncode == 0
    assert dmg.read_text() == "pretend-dmg"
    assert not quarantined.exists()
    assert calls == [
        f"notarytool submit {dmg} --keychain-profile test-profile --wait",
        f"stapler staple {dmg}",
        f"stapler validate {dmg}",
    ]


@pytest.mark.parametrize(
    ("fail_on", "expected_calls"),
    [
        ("notarytool submit", 1),
        ("stapler staple", 2),
        ("stapler validate", 3),
    ],
)
def test_apple_failure_quarantines_image_off_release_path(
    tmp_path, fail_on, expected_calls
):
    proc, dmg, quarantined, calls = _run(tmp_path, fail_on=fail_on)

    assert proc.returncode != 0
    assert not dmg.exists()
    assert quarantined.read_text() == "pretend-dmg"
    assert len(calls) == expected_calls
    assert "NOT DISTRIBUTABLE" in proc.stderr


def test_failed_quarantine_move_deletes_canonical_artifact(tmp_path):
    proc, dmg, quarantined, _ = _run(
        tmp_path, fail_on="notarytool submit", mv_fails=True
    )

    assert proc.returncode != 0
    assert not dmg.exists()
    assert not quarantined.exists()
    assert "deleted instead" in proc.stderr
    assert "retained at" not in proc.stderr


def test_reports_canonical_artifact_when_move_and_delete_both_fail(tmp_path):
    proc, dmg, quarantined, _ = _run(
        tmp_path,
        fail_on="notarytool submit",
        mv_fails=True,
        rm_fails=True,
    )

    assert proc.returncode != 0
    assert dmg.exists()
    assert not quarantined.exists()
    assert f"still exists at {dmg}" in proc.stderr
    assert "retained at" not in proc.stderr
