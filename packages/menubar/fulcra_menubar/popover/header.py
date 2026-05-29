"""The popover header: title + status pill + optional gear (preferences) button."""
from __future__ import annotations

import subprocess
from typing import Callable, Optional

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSColor, NSImage, NSTextField, NSView, NSMakeRect,
)

from .._daemon_url import daemon_url
from .._dispatch import on_main_thread
from ..model import OverallState, StatusModel
from .._objc_targets import attach as _attach
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
    on_preferences: Optional[Callable[[str | None], None]] = None,
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

    # Title shrunk from w=220 to w=180 to make room for the "?" docs button
    # at x=200. "Fulcra Collect" at typography.title() (16pt) measures ~115pt,
    # so 180pt has comfortable headroom.
    title = NSTextField.labelWithString_("Fulcra Collect")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, 28, 180, 22))

    subtitle = NSTextField.labelWithString_("")
    subtitle.setFont_(typography.small())
    subtitle.setTextColor_(colors.text_secondary())
    subtitle.setFrame_(NSMakeRect(16, 8, 280, 16))

    # Status pill kept at width=100 (ends at x=320). The "?" docs button used
    # to sit at x=305 and overlapped the pill's right ~15pt — causing wide
    # pill strings like "●  Daemon stopped" or "●  9 failing" to render their
    # last 1-2 glyphs under the button. Fix: move the docs button to x=200
    # (between title and pill) instead of shrinking the pill, which would
    # truncate "Daemon stopped" outright at this font size.
    pill = NSTextField.labelWithString_("")
    pill.setFont_(typography.small())
    pill.setAlignment_(2)  # right-aligned
    pill.setFrame_(NSMakeRect(220, 28, 100, 22))  # ends at x=320

    view.addSubview_(title)
    view.addSubview_(subtitle)
    view.addSubview_(pill)

    # The pill is a plain label; overlay a transparent button at the same
    # frame so the whole "● N failing" area is clickable and opens the web
    # dashboard (which lists every plugin + health — where you go to deal
    # with failing collectors). Clickable in all states; opening the
    # dashboard is always reasonable. daemon_url honours the [daemon]
    # web_port override.
    pill_btn = NSButton.alloc().initWithFrame_(NSMakeRect(220, 28, 100, 22))
    pill_btn.setTitle_("")
    pill_btn.setBordered_(False)
    pill_btn.setTransparent_(True)  # invisible; pill label shows through
    pill_btn.setToolTip_("Open the dashboard")

    def _open_dashboard(_sender):
        subprocess.run(["open", daemon_url("/")], check=False)
    _attach(pill_btn, _open_dashboard)
    view.addSubview_(pill_btn)

    # "?" docs button — opens the daemon's in-app docs page in the system
    # browser. Added in SP4 (drift audit 2026-05-27) so users can reach the
    # data-sources docs without context-switching to the dashboard.
    # Layout: title(16..196) ___ ?(200..220) pill(220..320) ___ gear(330..350).
    # Originally placed at x=305 (between pill and gear) but that overlapped
    # the pill's right edge by 15pt and clipped wide status text. Moved to
    # x=200 — between the (shortened) title and the pill — which preserves
    # both pill capacity and the gear's corner position. Style mirrors the
    # gear: NSBezelStyleInline (11) borderless + SF Symbol "questionmark.circle"
    # so the pair reads as a matched set of unobtrusive header icons.
    docs_btn = NSButton.alloc().initWithFrame_(NSMakeRect(200, 30, 20, 20))
    docs_btn.setBezelStyle_(11)
    docs_btn.setBordered_(False)
    docs_image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        "questionmark.circle", "Open docs"
    )
    if docs_image is not None:
        docs_btn.setImage_(docs_image)
    else:
        docs_btn.setTitle_("?")  # fallback for older macOS without SF Symbols
    docs_btn.setToolTip_("Open docs in browser")

    def _open_docs(_sender):
        # The daemon serves the in-app docs view at /?route=docs (the
        # URL-param handler added in SP4 task 1). subprocess open is the
        # standard macOS default-browser opener; check=False because a
        # failure to launch the browser shouldn't crash the menubar.
        # daemon_url() respects the user's [daemon] web_port override
        # (vs. the prior hardcoded 9292 that silently broke for users
        # who picked a different port).
        subprocess.run(
            ["open", daemon_url("/?route=docs")],
            check=False,
        )

    _attach(docs_btn, _open_docs)
    view.addSubview_(docs_btn)

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

        _attach(gear_btn, lambda _sender: on_preferences())
        view.addSubview_(gear_btn)

    def refresh(_m=None):
        text, color_hex = _STATE_LABEL.get(model.overall, _STATE_LABEL[OverallState.UNKNOWN])
        if model.overall is OverallState.FAILING and model.failing_count > 1:
            text = f"{model.failing_count} failing"
        pill.setStringValue_("●  " + text)
        pill.setTextColor_(_color(color_hex))
        # Count by `collect_mode` so the header summary speaks the same
        # language as the popover body, which now groups plugins into
        # Live (continuous) / Live (polled) / Historical (one-shot). The
        # old kind-based wording ("N scheduled · M services · X manual")
        # was leftover SP3 drift — see SP3 final review I1 (2026-05-27).
        n = len(model.plugins)
        continuous = sum(1 for p in model.plugins if p.collect_mode == "live_continuous")
        polled = sum(1 for p in model.plugins if p.collect_mode == "live_polled")
        historical = sum(1 for p in model.plugins if p.collect_mode == "historical")
        # Empty buckets are skipped so the user doesn't see e.g. "0 polled"
        # when they have no polled plugins enabled.
        parts = [f"{n} plugins"]
        if continuous:
            parts.append(f"{continuous} live")
        if polled:
            parts.append(f"{polled} polled")
        if historical:
            parts.append(f"{historical} one-shot")
        subtitle.setStringValue_(" · ".join(parts))

    refresh()
    model.add_observer(on_main_thread(refresh))
    return view


def _color(hex_value: str):
    h = hex_value.lstrip("#")
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0, 1.0,
    )


