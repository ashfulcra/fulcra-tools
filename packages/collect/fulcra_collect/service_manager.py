"""Install the fulcra-collect daemon as an OS-level user service.

macOS: a launchd user agent. Linux: a systemd user unit. Adapted from the
attention package's service manager (historical) — same shape, the hub's daemon.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

LAUNCHD_LABEL = "com.fulcra.collect"
SYSTEMD_NAME = "fulcra-collect"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_NAME}.service"


def render_launchd_plist(*, executable: str) -> str:
    log_dir = Path.home() / "Library" / "Logs" / "fulcra-collect"
    # launchd runs daemons with a stripped-down PATH (`/usr/bin:/bin:/usr/sbin:/sbin`)
    # and does NOT source the user's shell profile. That hides `~/.local/bin`
    # (where `uv tool install fulcra-api` puts the `fulcra` CLI) and homebrew
    # (`/opt/homebrew/bin` Apple Silicon, `/usr/local/bin` Intel). Past regressions
    # came from code shelling out via `shutil.which("fulcra")` and silently
    # getting None under launchd; the canonical fix is to route every shell-out
    # through `credentials._find_fulcra_cli()`. This PATH entry is the
    # defence-in-depth: even if a future call site forgets the helper, the
    # CLI is still on the daemon's PATH.
    home = str(Path.home())
    daemon_path = ":".join([
        f"{home}/.local/bin",
        "/opt/homebrew/bin", "/opt/homebrew/sbin",
        "/usr/local/bin",    "/usr/local/sbin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
        <string>daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{daemon_path}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/daemon.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/daemon.err.log</string>
</dict>
</plist>
"""


def render_systemd_unit(*, executable: str) -> str:
    # systemd user services don't auto-include `~/.local/bin` (or the rare
    # Linux user with homebrew at `/home/linuxbrew/.linuxbrew/bin`). Same
    # defence-in-depth as the launchd plist above — see that docstring.
    home = str(Path.home())
    daemon_path = ":".join([
        f"{home}/.local/bin",
        "/home/linuxbrew/.linuxbrew/bin",
        "/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin",
    ])
    return f"""[Unit]
Description=Fulcra Collect hub daemon
After=network.target

[Service]
Type=simple
Environment=PATH={daemon_path}
ExecStart={executable} daemon
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def _write_unit(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, 0o644)


def install(*, executable: str) -> Path:
    """Render and write the service file for this platform; return its path."""
    system = platform.system()
    if system == "Darwin":
        path = launchd_plist_path()
        content = render_launchd_plist(executable=executable)
    elif system == "Linux":
        path = systemd_unit_path()
        content = render_systemd_unit(executable=executable)
    else:
        raise RuntimeError(f"unsupported platform: {system!r}")
    _write_unit(path, content)
    return path
