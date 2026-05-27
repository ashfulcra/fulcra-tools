"""One row per plugin. 44pt tall. Layout (360pt wide):

  [dot]  Name (truncated)   right_text                       [ Disable ] [ Configure ] [ Run now ]
         id   (truncated)

Column boundaries (no-overlap):
  dot:        x=16,  w=10
  name+id:    x=34,  w=108 → ends at x=142
  right_text: x=146, w=198 → ends at x=344  (right-aligned, y=28)
  Disable:   x=148, w=60,  y=8, h=22 → ends at x=208  (only when snap.enabled)
  Configure: x=212, w=72,  y=8, h=22 → ends at x=284  (always shown)
  Run now:   x=288, w=58,  y=8, h=22 → ends at x=346  (when has_button)

SP4 task 3 (drift audit 2026-05-27) added the Configure + Disable buttons
to mirror the dashboard's {Run now, Configure, Disable} action set. The
right_text moves to y=28 (top half of the row) so it doesn't collide
with the button row at y=8..30. The name/id column was narrowed from
w=200 to w=108 to make room for three buttons in the right column —
truncated names lose a few characters but the bottom-right action set
is more valuable per the drift audit's user-research read.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSColor, NSTextField, NSView, NSMakeRect,
    NSBezelStyleRounded, NSLineBreakByTruncatingTail,
)

from fulcra_collect import config as _config

from ..daemon_client import DaemonClient
from ..model import PluginSnapshot, StatusModel
from .._objc_targets import attach as _attach
from ..theme import colors, palette, typography

ROW_HEIGHT = 44


def make_row(snapshot: PluginSnapshot, *, client: DaemonClient,
              model: StatusModel, width: float) -> NSView:
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, ROW_HEIGHT))

    dot = _status_dot(snapshot, model)
    dot.setFrame_(NSMakeRect(16, 18, 10, 10))
    view.addSubview_(dot)

    # SP4 task 3 (2026-05-27): name+id column narrowed from w=200 to w=108
    # so three buttons fit in the right column (Disable | Configure | Run now).
    # Long plugin names truncate a few characters earlier — acceptable cost
    # for surfacing the per-plugin action set in the popover.
    name = NSTextField.labelWithString_(snapshot.name)
    name.setFont_(typography.body())
    name.setTextColor_(colors.text() if snapshot.enabled else colors.text_tertiary())
    name.setLineBreakMode_(NSLineBreakByTruncatingTail)
    name.setFrame_(NSMakeRect(34, 22, 108, 18))  # ends at x=142
    view.addSubview_(name)

    pid = NSTextField.labelWithString_(snapshot.id)
    pid.setFont_(typography.small())
    pid.setTextColor_(colors.text_secondary())
    pid.setLineBreakMode_(NSLineBreakByTruncatingTail)
    pid.setFrame_(NSMakeRect(34, 6, 108, 14))  # ends at x=142
    view.addSubview_(pid)

    # Run-now visibility rules:
    #   manual    — always visible; daemon never auto-polls, so the button is
    #               the only way to trigger a run regardless of enabled state.
    #   scheduled — visible only when enabled (toggle gates the polling cycle).
    #   service   — never shown; services are daemon-managed, not user-triggered.
    if snapshot.kind == "manual":
        has_run_now = True
    elif snapshot.kind == "scheduled":
        has_run_now = snapshot.enabled
    else:  # service
        has_run_now = False

    # right_text sits in the top half of the row at y=28, right-aligned so its
    # tail hugs the right edge. Buttons occupy y=8..30 below it. Pre-SP4 it
    # used to share the bottom-right corner with the single Run-now button,
    # but adding two more buttons forced it up.
    right_text = NSTextField.labelWithString_(_right_text(snapshot))
    right_text.setFont_(typography.small())
    right_text.setTextColor_(colors.text_secondary())
    right_text.setAlignment_(2)  # right
    right_text.setFrame_(NSMakeRect(146, 28, 198, 12))  # ends at x=344, y=28..40
    view.addSubview_(right_text)

    # Disable button — mirrors the Preferences tab's enable-switch flow
    # (preferences/plugins_tab.py:279-290). Only shown when the plugin is
    # currently enabled (disabling a disabled plugin would be a no-op).
    # Added in SP4 (drift audit 2026-05-27) so popover users can disable
    # a noisy plugin without opening Preferences.
    if snapshot.enabled:
        disable_btn = NSButton.alloc().initWithFrame_(NSMakeRect(148, 8, 60, 22))
        disable_btn.setTitle_("Disable")
        disable_btn.setBezelStyle_(NSBezelStyleRounded)

        def _on_disable(_sender, plugin_id=snapshot.id):
            # Same flow Preferences uses: mutate local config, save, ask
            # daemon to reload. The daemon then refuses to schedule the
            # plugin going forward.
            cfg = _config.load()
            cfg.disable(plugin_id)
            _config.save(cfg)
            client.reload()

        _attach(disable_btn, _on_disable)
        view.addSubview_(disable_btn)

    # Configure button — opens the web UI wizard for this plugin in the
    # system browser. Shown for every plugin (enabled or disabled). Per
    # user Q5 from the SP4 scoping pass: always open the web UI wizard
    # rather than re-implementing the 11+ wizard step kinds (OAuth, file
    # upload, definition picker, health check, etc.) in PyObjC.
    # Requires SP4 task 1's URL-param handler in app.js so the URL routes
    # into the wizard.
    configure_btn = NSButton.alloc().initWithFrame_(NSMakeRect(212, 8, 72, 22))
    configure_btn.setTitle_("Configure")
    configure_btn.setBezelStyle_(NSBezelStyleRounded)

    def _on_configure(_sender, plugin_id=snapshot.id):
        url = f"http://127.0.0.1:9292/?route=configure&plugin={plugin_id}"
        subprocess.run(["open", url], check=False)

    _attach(configure_btn, _on_configure)
    view.addSubview_(configure_btn)

    if has_run_now:
        button = NSButton.alloc().initWithFrame_(NSMakeRect(288, 8, 58, 22))  # ends x=346
        button.setTitle_("Run now")
        button.setBezelStyle_(NSBezelStyleRounded)

        def _on_click(_sender):
            try:
                client.run(snapshot.id)
            finally:
                model.mark_in_flight(snapshot.id)

        _attach(button, _on_click)
        view.addSubview_(button)

    return view


def _right_text(s: PluginSnapshot) -> str:
    if s.kind == "service":
        if s.last_outcome == "error":
            return "Crashed"
        return "Running"
    if not s.last_run:
        return "Never run"
    return _relative(s.last_run)


def _relative(iso: str) -> str:
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - when
    sec = int(delta.total_seconds())
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def _status_dot(s: PluginSnapshot, _model: StatusModel) -> NSView:
    """Three-tier status dot, mirrors the dashboard's per-plugin pill.

    The dashboard's `pillFor(plugin)` in `packages/web-ui/dist/static/
    dashboard.js` is the source of truth for this mapping (SP3 D4 in the
    2026-05-27 menubar drift audit). Branches, in priority order:

      not enabled                      → gray   (Disabled)
      consecutive_failures >= 3        → red    (Failing)
      last_outcome == "running"        → violet (Running)
      last_outcome == "done"           → mint   (Healthy)
      last_outcome in {error, timeout} → amber  (Failed — run again)
                                                 only reached when
                                                 consecutive_failures < 3
      last_run is None                 → gray   (Not run yet)

    The previous two-tier mapping flattened the 1-2-failures case into
    "red", which hid the difference between a transient blip (Failed —
    run again) and a persistent failure (>=3, real attention). Matching
    the dashboard keeps the popover and the web UI internally consistent
    with each other and with the menubar icon's badge thresholds (see
    `status_item.py:130-137`, which also splits on `failing_critical`
    vs `failing_warning`).
    """
    if not s.enabled:
        color_hex = palette.TEXT_TERTIARY
    elif s.consecutive_failures >= 3:
        color_hex = palette.ERROR
    elif s.last_outcome == "running":
        color_hex = palette.ACCENT_VIOLET
    elif s.last_outcome == "done":
        color_hex = palette.ACCENT_MINT
    elif s.last_outcome in ("error", "timeout"):
        # 1-2 consecutive failures: softer warning, not the persistent-red.
        color_hex = palette.WARNING
    else:
        # Enabled but never run (last_run is None and last_outcome is None).
        color_hex = palette.TEXT_TERTIARY
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    view.setWantsLayer_(True)
    from Quartz import CALayer  # type: ignore[import-not-found]
    layer = CALayer.layer()
    layer.setBackgroundColor_(_to_cg(color_hex))
    layer.setCornerRadius_(5.0)
    layer.setFrame_(view.bounds())
    view.setLayer_(layer)
    return view


def _to_cg(hex_value: str):
    h = hex_value.lstrip("#")
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0, 1.0,
    ).CGColor()


