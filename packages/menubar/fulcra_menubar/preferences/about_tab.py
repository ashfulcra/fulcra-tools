"""About tab — versions, paths, Open Logs, Launch-at-login.

Layout (top → bottom in screen space, i.e. high-y → low-y in AppKit coords):

  ┌─ Action row (y≈400) ──────────────────────────────────────────────────┐
  │  [Open Activity Logs]   Launch at login  [switch]                     │
  │                         Open Fulcra Collect automatically…            │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ Identity block (y≈330–370) ─────────────────────────────────────────┐
  │  App version       x.y.z                                              │
  │  Daemon version    x.y.z                                              │
  │  Config            ~/.config/fulcra-collect/config.toml               │
  │  State directory   ~/.config/fulcra-collect/state                     │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ Plugin versions (NSScrollView, fills remaining space) ───────────────┐
  │  Plugin versions                                                      │
  │    plugin.id   x.y.z                                                  │
  │    …                                                                  │
  └───────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import importlib.metadata as _im
import subprocess
from pathlib import Path

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSBezelStyleRounded, NSScrollView, NSSwitch,
    NSTextField, NSView, NSMakeRect, NSLineBreakByTruncatingMiddle,
)

from fulcra_collect import config as _config

from .._objc_targets import attach as _attach
from ..daemon_client import DaemonClient, DaemonUnavailable
from ..theme import colors, typography

_TAB_W = 640.0
_TAB_H = 440.0


def make_about_tab(*, client: DaemonClient):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _TAB_W, _TAB_H))
    # Paint the root view white so dark-mode system chrome can never bleed
    # through a transparent container — the same fix applied to all three tabs.
    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(colors.bg().CGColor())

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

    # ------------------------------------------------------------------
    # Action row — sits at the TOP of the tab (high y in AppKit coords).
    # Left side: Open Activity Logs button.
    # Right side: Launch-at-login label + switch + explanatory caption.
    # ------------------------------------------------------------------
    ACTION_Y = 396  # baseline for the action row

    logs_btn = NSButton.alloc().initWithFrame_(NSMakeRect(16, ACTION_Y, 160, 28))
    logs_btn.setTitle_("Open Activity Logs")
    logs_btn.setBezelStyle_(NSBezelStyleRounded)

    def on_logs(_s):
        # The daemon's launchd log path; falls back to Console.app open.
        log = Path.home() / "Library" / "Logs" / "com.fulcradynamics.collect.log"
        subprocess.Popen(
            ["open", "-a", "Console", str(log) if log.exists() else "/var/log/system.log"]
        )
    _attach(logs_btn, on_logs)
    view.addSubview_(logs_btn)

    launch_label = NSTextField.labelWithString_("Launch at login")
    launch_label.setFont_(typography.body())
    launch_label.setTextColor_(colors.text())
    launch_label.setFrame_(NSMakeRect(280, ACTION_Y + 5, 200, 18))
    view.addSubview_(launch_label)

    launch_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(490, ACTION_Y + 2, 50, 22))
    launch_switch.setState_(1 if _is_login_item() else 0)

    def on_launch_change(sender):
        if sender.state():
            _register_login_item()
        else:
            _unregister_login_item()
    _attach(launch_switch, on_launch_change)
    view.addSubview_(launch_switch)

    # Caption 18pt below the action row — explains the toggle.
    launch_caption = NSTextField.labelWithString_(
        "Open Fulcra Collect automatically when you log in to your Mac."
    )
    launch_caption.setFont_(typography.small())
    launch_caption.setTextColor_(colors.text_secondary())
    launch_caption.setFrame_(NSMakeRect(280, ACTION_Y - 16, 344, 16))
    view.addSubview_(launch_caption)

    # ------------------------------------------------------------------
    # Thin separator line (via a plain view) between action row and identity.
    # ------------------------------------------------------------------
    sep = NSView.alloc().initWithFrame_(NSMakeRect(16, ACTION_Y - 28, _TAB_W - 32, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(colors.text_secondary().colorWithAlphaComponent_(0.2).CGColor())
    view.addSubview_(sep)

    # ------------------------------------------------------------------
    # Identity block — App version, Daemon version, Config, State.
    # ------------------------------------------------------------------
    def _info_row(label_text: str, value_text: str, y: float):
        lbl = NSTextField.labelWithString_(label_text)
        lbl.setFont_(typography.small())
        lbl.setTextColor_(colors.text_secondary())
        lbl.setFrame_(NSMakeRect(16, y, 140, 16))
        view.addSubview_(lbl)
        v = NSTextField.labelWithString_(value_text)
        v.setFont_(typography.small())
        v.setTextColor_(colors.text())
        v.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
        v.setFrame_(NSMakeRect(160, y, _TAB_W - 176, 16))
        view.addSubview_(v)

    _info_row("App version", app_version,     ACTION_Y - 56)
    _info_row("Daemon version", daemon_version, ACTION_Y - 76)
    _info_row("Config",        str(_config.config_dir() / "config.toml"), ACTION_Y - 96)
    _info_row("State directory", str(_config.config_dir() / "state"),    ACTION_Y - 116)

    # ------------------------------------------------------------------
    # Plugin-versions list — wrapped in an NSScrollView so it never
    # overflows into the action row above it or the window chrome below.
    # ------------------------------------------------------------------
    SCROLL_TOP = ACTION_Y - 140   # top of the scroll area (y of its frame)
    SCROLL_H   = SCROLL_TOP       # fill remaining space down to y=0
    SCROLL_Y   = 0                # bottom of the scroll area

    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(0, SCROLL_Y, _TAB_W, SCROLL_H)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)

    # Build the inner content view — height grows with plugin count.
    row_height = 18
    header_height = 28
    n_plugins = len(plugin_versions)
    content_h = max(header_height + n_plugins * row_height + 8, SCROLL_H)

    inner = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _TAB_W, content_h))
    inner.setWantsLayer_(True)
    inner.layer().setBackgroundColor_(colors.bg().CGColor())

    # Header — "Plugin versions" — sits at the top of the inner view.
    # The inner view is unflipped (y=0 at bottom) so the header is at the top.
    hdr_y = content_h - header_height
    plugin_header = NSTextField.labelWithString_("Plugin versions")
    plugin_header.setFont_(typography.body())
    plugin_header.setTextColor_(colors.text())
    plugin_header.setFrame_(NSMakeRect(16, hdr_y, 400, 18))
    inner.addSubview_(plugin_header)

    # Plugin rows, rendered top-down.
    plugin_y = hdr_y - row_height
    for pid in sorted(plugin_versions):
        lbl = NSTextField.labelWithString_(f"  {pid}")
        lbl.setFont_(typography.small())
        lbl.setTextColor_(colors.text_secondary())
        lbl.setFrame_(NSMakeRect(16, plugin_y, 220, 16))
        inner.addSubview_(lbl)
        v = NSTextField.labelWithString_(plugin_versions[pid])
        v.setFont_(typography.small())
        v.setTextColor_(colors.text())
        v.setFrame_(NSMakeRect(240, plugin_y, 400, 16))
        inner.addSubview_(v)
        plugin_y -= row_height

    scroll.setDocumentView_(inner)
    view.addSubview_(scroll)

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
