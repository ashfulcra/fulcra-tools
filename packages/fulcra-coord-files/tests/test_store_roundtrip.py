"""Round-trip tests for the fulcra-coord-files object-store transport.

These exercise the moved transport against the SAME stateful fake backend the
coord suite uses (``packages/fulcra-coord/tests/fake_fulcra_backend.py``), driven
via an explicit ``backend=`` list so no global env state is needed. The fake maps
remote paths under ``FULCRA_FAKE_ROOT`` to real local files, so an upload then a
download is a true wire round-trip through the subprocess transport.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import fulcra_coord_files as files

# The fake backend lives in the sibling fulcra-coord package's tests dir. We
# resolve it relative to THIS file (…/fulcra-coord-files/tests/) so the path is
# stable regardless of the pytest invocation's working directory.
FAKE = (
    Path(__file__).resolve().parents[2]
    / "fulcra-coord"
    / "tests"
    / "fake_fulcra_backend.py"
)


def _backend(tmp_path: Path) -> list[str]:
    """Build the explicit backend command and point the fake at ``tmp_path``.

    The fake reads its state root from ``FULCRA_FAKE_ROOT`` in the environment,
    so we set it here; each test gets a fresh ``tmp_path``, keeping runs isolated.
    """
    os.environ["FULCRA_FAKE_ROOT"] = str(tmp_path)
    return [sys.executable, str(FAKE)]


def test_upload_download_json_roundtrips(tmp_path):
    """upload_json then download_json returns the identical payload."""
    B = _backend(tmp_path)
    assert files.upload_json({"a": 1}, "/coordination/x.json", backend=B) is True
    got = files.download_json("/coordination/x.json", backend=B)
    assert got == {"a": 1}


def test_list_json_returns_all_uploaded(tmp_path):
    """Two uploads under a prefix are both enumerated + parsed by list_json."""
    B = _backend(tmp_path)
    base = "/coordination/events/tasks/T1"
    assert files.upload_json({"e": 1}, f"{base}/e1.json", backend=B) is True
    assert files.upload_json({"e": 2}, f"{base}/e2.json", backend=B) is True

    results = files.list_json(f"{base}/", backend=B)
    payloads = sorted((rec["e"] for _, rec in results))
    assert payloads == [1, 2]
    paths = {path for path, _ in results}
    assert f"{base}/e1.json" in paths
    assert f"{base}/e2.json" in paths
