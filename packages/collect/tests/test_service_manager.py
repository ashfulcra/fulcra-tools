"""The launchd/systemd installer for the fulcra-collect daemon."""
from __future__ import annotations

from fulcra_collect import service_manager


def test_launchd_plist_runs_the_daemon():
    plist = service_manager.render_launchd_plist(executable="/opt/venv/bin/fulcra-collect")
    assert "com.fulcra.collect" in plist
    assert "/opt/venv/bin/fulcra-collect" in plist
    assert "<string>daemon</string>" in plist
    assert "RunAtLoad" in plist and "KeepAlive" in plist


def test_systemd_unit_runs_the_daemon():
    unit = service_manager.render_systemd_unit(executable="/opt/venv/bin/fulcra-collect")
    assert "ExecStart=/opt/venv/bin/fulcra-collect daemon" in unit
    assert "Restart=always" in unit


def test_install_writes_the_file_for_this_platform(tmp_path, monkeypatch):
    written = {}

    def fake_write(path, content):
        written["path"] = path
        written["content"] = content

    monkeypatch.setattr(service_manager, "_write_unit", fake_write)
    path = service_manager.install(executable="/opt/venv/bin/fulcra-collect")
    assert written["content"]
    assert path == written["path"]
