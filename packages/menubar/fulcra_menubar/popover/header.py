"""The popover header: title + status pill + optional gear (preferences) button."""
from __future__ import annotations

from typing import Callable, Optional

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSColor, NSImage, NSTextField, NSView, NSMakeRect,
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


def make_header(
    model: StatusModel,
    *,
    on_preferences: Optional[Callable[[], None]] = None,
) -> NSView:
    """Build the popover header view.

    Parameters
    ----------
    model:
        The shared StatusModel; the header subscribes to model updates to
        refresh the status pill and subtitle text.
    on_preferences:
        Optional callback invoked when the user clicks the gear icon.  When
        None, no gear button is added (degrades gracefully for test fixtures
        that construct the header without a preferences handler).
    """
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 56))

    title = NSTextField.labelWithString_("Fulcra Collect")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, 28, 220, 22))

    subtitle = NSTextField.labelWithString_("")
    subtitle.setFont_(typography.small())
    subtitle.setTextColor_(colors.text_secondary())
    subtitle.setFrame_(NSMakeRect(16, 8, 280, 16))

    # Status pill ends at x=320 (trimmed from original x=344) to leave room for
    # the gear button at x=330.
    pill = NSTextField.labelWithString_("")
    pill.setFont_(typography.small())
    pill.setAlignment_(2)  # right-aligned
    pill.setFrame_(NSMakeRect(220, 28, 100, 22))  # ends at x=320

    view.addSubview_(title)
    view.addSubview_(subtitle)
    view.addSubview_(pill)

    if on_preferences is not None:
        gear_btn = NSButton.alloc().initWithFrame_(NSMakeRect(330, 30, 20, 20))
        # NSBezelStyleInline (11) renders small and borderless — unobtrusive in
        # the header corner.
        gear_btn.setBezelStyle_(11)
        gear_btn.setBordered_(False)
        gear_image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "gearshape", "Preferences"
        )
        if gear_image is not None:
            gear_btn.setImage_(gear_image)
        else:
            gear_btn.setTitle_("⚙")  # fallback for older macOS without SF Symbols

        _HeaderTarget.attach(gear_btn, lambda _sender: on_preferences())
        view.addSubview_(gear_btn)

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


class _HeaderTarget:
    """AppKit button-target proxy — keeps the Python callable alive via a class-
    level retain list and wires it to the button's ObjC action mechanism.

    Follows the same pattern as ``_RowTarget`` in ``plugin_row.py``.
    """
    _retain: list = []

    @classmethod
    def attach(cls, button, callable_) -> None:
        from Foundation import NSObject  # type: ignore[import-not-found]

        class _T(NSObject):
            def call_(self, sender):
                callable_(sender)

        target = _T.alloc().init()
        button.setTarget_(target)
        button.setAction_("call:")
        cls._retain.append(target)  # prevent GC
