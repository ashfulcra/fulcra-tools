"""One row per plugin. 44pt tall. Layout (360pt wide):

  [dot]  Name (truncated)   right_text   [Run now]
         id   (truncated)

Column boundaries (no-overlap):
  dot:        x=16, w=10
  name+id:    x=34, w=200 → ends at x=234
  right_text: x=240, w=70 → ends at x=310  (right-aligned, y=24)
  button:     x=260, w=84 → ends at x=344  (y=10, below right_text)

When both right_text and button are present (enabled scheduled/manual)
they stack vertically in the bottom-right corner.
"""
from __future__ import annotations

from datetime import datetime, timezone

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSColor, NSTextField, NSView, NSMakeRect,
    NSBezelStyleRounded, NSLineBreakByTruncatingTail,
)

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

    name = NSTextField.labelWithString_(snapshot.name)
    name.setFont_(typography.body())
    name.setTextColor_(colors.text() if snapshot.enabled else colors.text_tertiary())
    name.setLineBreakMode_(NSLineBreakByTruncatingTail)
    name.setFrame_(NSMakeRect(34, 22, 200, 18))  # ends at x=234
    view.addSubview_(name)

    pid = NSTextField.labelWithString_(snapshot.id)
    pid.setFont_(typography.small())
    pid.setTextColor_(colors.text_secondary())
    pid.setLineBreakMode_(NSLineBreakByTruncatingTail)
    pid.setFrame_(NSMakeRect(34, 6, 200, 14))  # ends at x=234
    view.addSubview_(pid)

    # right_text sits at x=240, leaving a 6pt gap after the name column (x=234).
    # When a Run-now button is also present, right_text shifts up (y=24) so the
    # two don't overlap vertically.
    #
    # Button visibility rules:
    #   manual    — always visible; daemon never auto-polls, so the button is
    #               the only way to trigger a run regardless of enabled state.
    #   scheduled — visible only when enabled (toggle gates the polling cycle).
    #   service   — never shown; services are daemon-managed, not user-triggered.
    if snapshot.kind == "manual":
        has_button = True
    elif snapshot.kind == "scheduled":
        has_button = snapshot.enabled
    else:  # service
        has_button = False
    rt_y = 24 if has_button else 16
    right_text = NSTextField.labelWithString_(_right_text(snapshot))
    right_text.setFont_(typography.small())
    right_text.setTextColor_(colors.text_secondary())
    right_text.setAlignment_(2)  # right
    right_text.setFrame_(NSMakeRect(240, rt_y, 70, 14))  # x=240, ends at x=310
    view.addSubview_(right_text)

    if has_button:
        button = NSButton.alloc().initWithFrame_(NSMakeRect(260, 8, 84, 22))  # ends x=344
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


