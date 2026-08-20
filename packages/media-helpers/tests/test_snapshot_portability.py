"""Snapshot copy portability — the 2026-08-20 repo-health find (2ff71e1e).

Both live-SQLite snapshotters shelled `cp -c` unconditionally. The `-c` flag
requests an APFS clonefile(2) and exists only in macOS cp; GNU coreutils
rejects it, so on the Linux fleet runners every apple_tv importer/plugin test
and the apple_podcasts fingerprint test failed with `cp: invalid option -- 'c'`
while macOS CI stayed green — the exact "red on my machine, green in CI" rot
vector the repo-health workstream exists to kill. The copy stays a subprocess
on every platform because the killable timeout around it is load-bearing.
"""
import sys

import pytest

from fulcra_media.importers import apple_podcasts, apple_tv


@pytest.mark.parametrize("mod", [apple_tv, apple_podcasts])
def test_cp_args_clone_on_darwin_plain_elsewhere(mod, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert mod._cp_args() == ["cp", "-c"]
    monkeypatch.setattr(sys, "platform", "linux")
    assert mod._cp_args() == ["cp"]


@pytest.mark.parametrize("mod", [apple_tv, apple_podcasts])
def test_cp_args_run_on_this_platform(mod, tmp_path):
    # Whatever this host is, the chosen args must actually copy a file —
    # the assertion that failed (as `invalid option -- 'c'`) before the fix.
    import subprocess
    src = tmp_path / "src.bin"
    src.write_bytes(b"snapshot me")
    dest = tmp_path / "dest.bin"
    subprocess.run([*mod._cp_args(), str(src), str(dest)],
                   check=True, capture_output=True, timeout=10)
    assert dest.read_bytes() == b"snapshot me"
