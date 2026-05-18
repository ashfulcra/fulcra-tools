"""Generate + install OS-level service definitions for the relay.

macOS: launchd user agent at ~/Library/LaunchAgents/com.fulcra.attention.relay.plist
Linux: systemd user unit at ~/.config/systemd/user/fulcra-attention-relay.service
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

LAUNCHD_LABEL = "com.fulcra.attention.relay"
SYSTEMD_NAME = "fulcra-attention-relay"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_NAME}.service"


def render_launchd_plist(*, executable: str) -> str:
    log_dir = Path.home() / "Library" / "Logs" / "fulcra-attention"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
        <string>relay</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/relay.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/relay.err.log</string>
</dict>
</plist>
"""


def render_systemd_unit(*, executable: str) -> str:
    return f"""[Unit]
Description=Fulcra Attention relay (loopback HTTP for the Chrome ext)
After=network.target

[Service]
Type=simple
ExecStart={executable} relay
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def install(*, executable: str) -> Path:
    """Render and write the appropriate service file; return its path.

    macOS: writes the launchd plist (caller is expected to `launchctl load` it).
    Linux: writes the systemd user unit (caller: `systemctl --user enable --now`).
    """
    system = platform.system()
    if system == "Darwin":
        path = launchd_plist_path()
        content = render_launchd_plist(executable=executable)
    elif system == "Linux":
        path = systemd_unit_path()
        content = render_systemd_unit(executable=executable)
    else:
        raise RuntimeError(f"unsupported platform: {system!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, 0o644)
    return path
