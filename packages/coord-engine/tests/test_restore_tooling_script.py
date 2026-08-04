"""The coord-boss self-heal checks currency, not mere file existence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "scripts/coord-boss/restore-tooling.sh"
FILES = ("linear-sync.sh", "restore-tooling.sh")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    stash_dir = tmp_path / "stash"
    bin_dir.mkdir()
    stash_dir.mkdir()
    _write_executable(bin_dir / "fulcra-api", """#!/bin/bash
set -eu
cp "$MANIFEST_FIXTURE" "$4"
""")
    _write_executable(bin_dir / "coord-engine", """#!/bin/bash
set -eu
dest="${!#}"
mkdir -p "$dest"
for name in linear-sync.sh restore-tooling.sh; do
  cp "$STASH_FIXTURE/$name" "$dest/$name"
  chmod +x "$dest/$name"
done
""")
    return bin_dir, stash_dir


def _manifest(stash_dir: Path) -> dict[str, object]:
    return {"files": {
        name: {
            "sha256": hashlib.sha256((stash_dir / name).read_bytes()).hexdigest(),
            "exec": True,
        }
        for name in FILES
    }}


def test_restore_tooling_replaces_stale_executable_files(tmp_path: Path) -> None:
    bin_dir, stash_dir = _fake_commands(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    for name in FILES:
        _write_executable(stash_dir / name, f"current {name}\n")
        _write_executable(destination / name, f"stale {name}\n")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(stash_dir)), encoding="utf-8")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               MANIFEST_FIXTURE=str(manifest_path), STASH_FIXTURE=str(stash_dir))

    completed = subprocess.run(
        ["bash", str(SCRIPT), str(destination)], env=env,
        capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert "restored and verified 2 file(s)" in completed.stdout
    for name in FILES:
        assert (destination / name).read_text(encoding="utf-8") == f"current {name}\n"


def test_restore_tooling_fails_closed_on_unreadable_manifest(tmp_path: Path) -> None:
    bin_dir, stash_dir = _fake_commands(tmp_path)
    destination = tmp_path / "destination"
    for name in FILES:
        _write_executable(stash_dir / name, f"current {name}\n")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("not json\n", encoding="utf-8")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               MANIFEST_FIXTURE=str(manifest_path), STASH_FIXTURE=str(stash_dir))

    completed = subprocess.run(
        ["bash", str(SCRIPT), str(destination)], env=env,
        capture_output=True, text=True, check=False)

    assert completed.returncode != 0
    assert "manifest unreadable" in completed.stderr
