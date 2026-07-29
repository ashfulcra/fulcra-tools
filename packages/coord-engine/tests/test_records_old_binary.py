"""Side-by-side isolation gate against the actual coord-engine v1.7.2 source.

The fixture is a byte-stable ``git archive`` of the signed repository tag
``coord-engine-v1.7.2``.  It runs in a separate interpreter with only that
archive on ``PYTHONPATH``; this is not the new engine pretending its version is
old.  The injected filesystem transport replaces only the network/store edge.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile

from coord_engine import records


FIXTURE = (
    Path(__file__).parent / "fixtures" /
    "coord-engine-v1.7.2-source.tar.gz"
)
FIXTURE_SHA256 = "18813ea216572ab9586e6fd9a623a5478cc94db233b77df93109402212b6b3c7"


OLD_ENGINE_RUNNER = r"""
import json
from pathlib import Path
import sys

from coord_engine import __version__
from coord_engine import cli, records


class FilesystemTransport:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, name):
        return self.root / name

    def read(self, name):
        path = self._path(name)
        return path.read_text() if path.exists() else None

    def read_classified(self, name):
        value = self.read(name)
        return (value, "ok") if value is not None else (None, "absent")

    def write(self, name, content):
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return True

    def records(self, data_type, since, until):
        return []


root = Path(sys.argv[1])
transport = FilesystemTransport(root)
assert __version__ == "1.7.2", __version__
assert records.cursor_path("r", "amy").endswith(
    "_coord/agents/amy/records-cursor.json")
raise SystemExit(cli.main(["queue", "r", "--agent", "amy"],
                          transport=transport))
"""


def test_actual_v172_binary_cannot_mutate_generation_scoped_v2_cursor(tmp_path):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256

    source = tmp_path / "old"
    source.mkdir()
    with tarfile.open(FIXTURE, "r:gz") as archive:
        if sys.version_info >= (3, 12):
            archive.extractall(source, filter="data")
        else:  # Python 3.10/3.11 do not expose the safe filter parameter.
            for member in archive.getmembers():
                assert (source / member.name).resolve().is_relative_to(
                    source.resolve())
            archive.extractall(source)
    package_root = (
        source / "coord-engine-v1.7.2" / "packages" / "coord-engine"
    )

    store = tmp_path / "store"
    config = store / records.config_path("r")
    config.parent.mkdir(parents=True)
    config.write_text('{"data_type":"MomentAnnotation/x"}')
    v2 = store / records.v2_cursor_path("r", "amy", 7)
    v2.parent.mkdir(parents=True)
    v2_before = '{"v":2,"generation":7,"committed":"safe"}'
    v2.write_text(v2_before)

    before = _snapshot(store)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(package_root)
    env.pop("FULCRA_COORD_AGENT", None)
    completed = subprocess.run(
        [sys.executable, "-c", OLD_ENGINE_RUNNER, str(store)],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    legacy = store / records.cursor_path("r", "amy")
    assert legacy.exists(), "actual v1.7.2 engine did not exercise its writer"
    assert '"v":1' in legacy.read_text()
    assert v2.read_text() == v2_before

    # Write-set CLOSURE, not just "this one path survived".
    #
    # Checking a single v2 path proves generation 7's cursor at that exact
    # location is safe. It cannot prove the old engine left everything else
    # alone — another generation's path, a future v2 layout, any file nobody
    # thought to assert on. Closure is the property the isolation argument
    # actually rests on: once the write-set is pinned, ANY namespace disjoint
    # from it is unreachable by this binary, including layouts not yet
    # designed. That generality is what slice 5's legacy-state migration will
    # need, and it is exactly where an unanticipated path shows up.
    touched = _touched_since(store, before)
    assert touched == {records.cursor_path("r", "amy")}, (
        "a pre-slice-1 engine touched something outside its legacy cursor: "
        f"{sorted(touched)}. The slice-1 isolation argument assumes this set is "
        "closed; if it grew, every v2 namespace must be re-checked for overlap "
        "before cursor v2 is activated."
    )


def _snapshot(root):
    """Content of every file under ``root``, keyed by store-relative path."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def _touched_since(root, before):
    """Store-relative paths created or modified since ``before``.

    Content comparison rather than mtime: a same-second rewrite is invisible to
    mtime on coarse-granularity filesystems, and a write the harness cannot see
    is the one failure mode that would make this gate lie in the safe-looking
    direction.
    """
    after = _snapshot(root)
    return {
        path for path, blob in after.items() if before.get(path) != blob
    } | {path for path in before if path not in after}

