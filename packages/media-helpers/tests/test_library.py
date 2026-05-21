import subprocess
from pathlib import Path

import pytest

from fulcra_media import library


def test_is_fulcra_uri_true():
    assert library.is_fulcra_uri("fulcra:/takeouts/x.csv")
    assert library.is_fulcra_uri("fulcra:/x.csv")


def test_is_fulcra_uri_false():
    assert not library.is_fulcra_uri("/tmp/x.csv")
    assert not library.is_fulcra_uri("takeouts/x.csv")
    assert not library.is_fulcra_uri("")


def test_resolve_local_path_passes_through(tmp_path: Path):
    p = tmp_path / "file.csv"
    p.write_text("hi")
    out = library.resolve(str(p))
    assert out == p
    assert out.read_text() == "hi"


def test_resolve_local_path_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        library.resolve(str(tmp_path / "does-not-exist.csv"))


def test_resolve_fulcra_uri_shells_out(mocker, tmp_path: Path):
    """fulcra:/x.csv -> `fulcra file download /x.csv <tempfile>` is called."""
    calls = []

    def fake_run(cmd, **kwargs):
        # Mock writes the expected contents to the tempfile (last argument)
        Path(cmd[-1]).write_bytes(b"downloaded contents")
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    mocker.patch("subprocess.run", side_effect=fake_run)
    result = library.resolve("fulcra:/takeouts/file.csv")
    assert result.read_bytes() == b"downloaded contents"
    assert calls[0][:3] == ["fulcra", "file", "download"]
    assert calls[0][3] == "/takeouts/file.csv"
    # The last arg is the local tempfile path
    assert calls[0][4] == str(result)


def test_resolve_fulcra_uri_propagates_subprocess_failure(mocker):
    err = subprocess.CalledProcessError(returncode=2, cmd=["fulcra", "file", "download"])
    mocker.patch("subprocess.run", side_effect=err)
    with pytest.raises(RuntimeError, match="fulcra file download"):
        library.resolve("fulcra:/missing.csv")
