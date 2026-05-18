"""Service manager: launchd (macOS) + systemd user (Linux) generation."""
from __future__ import annotations

from pathlib import Path

import pytest

from fulcra_attention.service_manager import (
    LAUNCHD_LABEL,
    launchd_plist_path,
    render_launchd_plist,
    render_systemd_unit,
    systemd_unit_path,
)


def test_render_launchd_plist_contains_required_keys():
    plist = render_launchd_plist(executable="/opt/homebrew/bin/fulcra-attention")
    assert "<key>Label</key>" in plist
    assert f"<string>{LAUNCHD_LABEL}</string>" in plist
    assert "<key>ProgramArguments</key>" in plist
    assert "<string>/opt/homebrew/bin/fulcra-attention</string>" in plist
    assert "<string>relay</string>" in plist
    assert "<key>RunAtLoad</key>" in plist
    assert "<true/>" in plist
    assert "<key>KeepAlive</key>" in plist


def test_render_launchd_plist_uses_loopback_only_log_paths():
    plist = render_launchd_plist(executable="/usr/local/bin/fulcra-attention")
    assert "StandardOutPath" in plist
    assert "StandardErrorPath" in plist


def test_launchd_plist_path_under_launchagents():
    p = launchd_plist_path()
    assert "Library/LaunchAgents" in str(p)
    assert p.name == "com.fulcra.attention.relay.plist"


def test_render_systemd_unit_basic_structure():
    unit = render_systemd_unit(executable="/usr/local/bin/fulcra-attention")
    assert "[Unit]" in unit
    assert "[Service]" in unit
    assert "[Install]" in unit
    assert "ExecStart=/usr/local/bin/fulcra-attention relay" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit


def test_systemd_unit_path_under_user_systemd():
    p = systemd_unit_path()
    assert ".config/systemd/user" in str(p)
    assert p.name == "fulcra-attention-relay.service"
