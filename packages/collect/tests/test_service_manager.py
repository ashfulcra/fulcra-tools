"""The launchd/systemd installer for the fulcra-collect daemon."""
from __future__ import annotations

from pathlib import Path

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


def test_launchd_plist_sets_an_explicit_PATH_with_local_and_homebrew():
    """Defence-in-depth for the launchd restricted-PATH gotcha (see service_manager
    docstring). The daemon's PATH must include ~/.local/bin (uv tool install
    target) AND both homebrew prefixes (Apple Silicon /opt/homebrew, Intel
    /usr/local) so any future shell-out that forgets `_find_fulcra_cli` still
    finds the `fulcra` CLI under launchd."""
    plist = service_manager.render_launchd_plist(executable="/opt/venv/bin/fulcra-collect")
    assert "<key>EnvironmentVariables</key>" in plist
    assert "<key>PATH</key>" in plist
    home = str(Path.home())
    assert f"{home}/.local/bin" in plist
    assert "/opt/homebrew/bin" in plist  # Apple Silicon brew
    assert "/usr/local/bin" in plist     # Intel brew + general
    assert "/usr/bin" in plist           # baseline that launchd already gave us


def test_systemd_unit_sets_an_explicit_PATH_with_local_and_homebrew():
    """Same defence-in-depth on Linux: ~/.local/bin + linuxbrew + standard PATH
    so a future shell-out that forgets the helper still finds the CLI."""
    unit = service_manager.render_systemd_unit(executable="/opt/venv/bin/fulcra-collect")
    home = str(Path.home())
    assert f"Environment=PATH={home}/.local/bin:" in unit
    assert "/home/linuxbrew/.linuxbrew/bin" in unit
    assert "/usr/local/bin" in unit
    assert "/usr/bin" in unit


def test_install_writes_the_file_for_this_platform(tmp_path, monkeypatch):
    written = {}

    def fake_write(path, content):
        written["path"] = path
        written["content"] = content

    monkeypatch.setattr(service_manager, "_write_unit", fake_write)
    path = service_manager.install(executable="/opt/venv/bin/fulcra-collect")
    assert written["content"]
    assert path == written["path"]
