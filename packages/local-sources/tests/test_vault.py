from pathlib import Path

import pytest

from fulcra_local_sources import vault


def test_upload_failure_does_not_expose_absolute_local_path(tmp_path: Path, monkeypatch):
    source = tmp_path / "private-name.md"
    source.write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(vault, "_cli", lambda: ["synthetic-fulcra"])

    class Result:
        returncode = 1
        stdout = ""
        stderr = f"could not read {source}"

    monkeypatch.setattr(vault.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(vault.VaultError) as caught:
        vault.VaultWriter().upload_file(source, "/vault/synthetic/private-name.md")
    assert str(tmp_path) not in str(caught.value)
    assert "<local-file>" in str(caught.value)
