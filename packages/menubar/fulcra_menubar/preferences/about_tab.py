"""About tab — versions, paths, Open Logs, Launch-at-login."""
from __future__ import annotations

import importlib.metadata as _im
import subprocess
from pathlib import Path

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSBezelStyleRounded, NSSwitch, NSTextField, NSView, NSMakeRect,
)
from Foundation import NSObject  # type: ignore[import-not-found]

from fulcra_collect import config as _config

from ..daemon_client import DaemonClient, DaemonUnavailable
from ..theme import colors, palette, typography


def make_about_tab(*, client: DaemonClient):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 440))

    try:
        app_version = _im.version("fulcra-menubar")
    except _im.PackageNotFoundError:
        app_version = "0.1.0"

    try:
        version_reply = client.version()
        daemon_version = version_reply.get("daemon_version", "unknown")
        plugin_versions = version_reply.get("plugins", {})
    except DaemonUnavailable:
        daemon_version = "(daemon stopped)"
        plugin_versions = {}

    def row(label_text: str, value_text: str, y: float):
        l = NSTextField.labelWithString_(label_text)
        l.setFont_(typography.small())
        l.setTextColor_(colors.text_secondary())
        l.setFrame_(NSMakeRect(16, y, 220, 16))
        view.addSubview_(l)
        v = NSTextField.labelWithString_(value_text)
        v.setFont_(typography.small())
        v.setTextColor_(colors.text())
        v.setFrame_(NSMakeRect(240, y, 400, 16))
        view.addSubview_(v)

    row("App version", app_version, 410)
    row("Daemon version", daemon_version, 390)
    row("Config", str(_config.config_dir() / "config.toml"), 360)
    row("State directory", str(_config.config_dir() / "state"), 340)

    plugin_y = 300
    plugin_header = NSTextField.labelWithString_("Plugin versions")
    plugin_header.setFont_(typography.body())
    plugin_header.setTextColor_(colors.text())
    plugin_header.setFrame_(NSMakeRect(16, plugin_y, 400, 18))
    view.addSubview_(plugin_header)
    plugin_y -= 22
    for pid in sorted(plugin_versions):
        row(f"  {pid}", plugin_versions[pid], plugin_y)
        plugin_y -= 18

    # Open logs.
    logs_btn = NSButton.alloc().initWithFrame_(NSMakeRect(16, 60, 200, 28))
    logs_btn.setTitle_("Open Activity Logs")
    logs_btn.setBezelStyle_(NSBezelStyleRounded)

    def on_logs(_s):
        # The daemon's launchd log path; falls back to Console.app open.
        log = Path.home() / "Library" / "Logs" / "com.fulcradynamics.collect.log"
        subprocess.Popen(["open", "-a", "Console", str(log) if log.exists() else "/var/log/system.log"])
    _T.attach(logs_btn, on_logs)
    view.addSubview_(logs_btn)

    # Launch at login toggle.
    launch_label = NSTextField.labelWithString_("Launch at login")
    launch_label.setFont_(typography.body())
    launch_label.setTextColor_(colors.text())
    launch_label.setFrame_(NSMakeRect(16, 24, 400, 18))
    view.addSubview_(launch_label)

    launch_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(560, 20, 50, 22))
    launch_switch.setState_(1 if _is_login_item() else 0)

    def on_launch_change(sender):
        if sender.state():
            _register_login_item()
        else:
            _unregister_login_item()
    _T.attach(launch_switch, on_launch_change)
    view.addSubview_(launch_switch)

    return view


def _is_login_item() -> bool:
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        return False
    svc = SMAppService.mainAppService()
    return svc.status() == 1  # SMAppServiceStatusEnabled


def _register_login_item() -> None:
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        return
    svc = SMAppService.mainAppService()
    err = None
    svc.registerAndReturnError_(err)


def _unregister_login_item() -> None:
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        return
    svc = SMAppService.mainAppService()
    err = None
    svc.unregisterAndReturnError_(err)


class _T:
    _retain: list = []

    @classmethod
    def attach(cls, control, fn):
        class _Target(NSObject):
            def call_(self, sender):
                fn(sender)
        target = _Target.alloc().init()
        control.setTarget_(target)
        control.setAction_("call:")
        cls._retain.append(target)
