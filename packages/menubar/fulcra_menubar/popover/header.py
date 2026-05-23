"""The popover header: title + status pill."""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSColor, NSTextField, NSView, NSMakeRect,
)

from .._dispatch import on_main_thread
from ..model import OverallState, StatusModel
from ..theme import colors, palette, typography


_STATE_LABEL = {
    OverallState.HEALTHY: ("Healthy", palette.ACCENT_MINT),
    OverallState.RUNNING: ("Running…", palette.ACCENT_VIOLET),
    OverallState.FAILING: ("Failing", palette.ERROR),
    OverallState.DAEMON_STOPPED: ("Daemon stopped", palette.TEXT_TERTIARY),
    OverallState.UNKNOWN: ("Connecting…", palette.TEXT_TERTIARY),
}


def make_header(model: StatusModel) -> NSView:
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 56))

    title = NSTextField.labelWithString_("Fulcra Collect")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, 28, 220, 22))

    subtitle = NSTextField.labelWithString_("")
    subtitle.setFont_(typography.small())
    subtitle.setTextColor_(colors.text_secondary())
    subtitle.setFrame_(NSMakeRect(16, 8, 280, 16))

    pill = NSTextField.labelWithString_("")
    pill.setFont_(typography.small())
    pill.setAlignment_(2)  # right-aligned
    pill.setFrame_(NSMakeRect(220, 28, 124, 22))

    view.addSubview_(title)
    view.addSubview_(subtitle)
    view.addSubview_(pill)

    def refresh(_m=None):
        text, color_hex = _STATE_LABEL.get(model.overall, _STATE_LABEL[OverallState.UNKNOWN])
        if model.overall is OverallState.FAILING and model.failing_count > 1:
            text = f"{model.failing_count} failing"
        pill.setStringValue_("●  " + text)
        pill.setTextColor_(_color(color_hex))
        n = len(model.plugins)
        scheduled = sum(1 for p in model.plugins if p.kind == "scheduled")
        services = sum(1 for p in model.plugins if p.kind == "service")
        manual = sum(1 for p in model.plugins if p.kind == "manual")
        subtitle.setStringValue_(
            f"{n} plugins · {scheduled} scheduled · {services} services · {manual} manual"
        )

    refresh()
    model.add_observer(on_main_thread(refresh))
    return view


def _color(hex_value: str):
    h = hex_value.lstrip("#")
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0, 1.0,
    )
