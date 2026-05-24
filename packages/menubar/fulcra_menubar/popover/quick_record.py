"""The popover's primary view: a list of user-recordable Fulcra
annotations the user logs often. Each row is one Moment annotation
definition with a one-tap "Record" button that writes the moment
immediately via the daemon's record_annotation UDS command.

Today the list is sourced from the daemon's quick_record_list UDS
command, which fetches Moment annotations from Fulcra sorted by
created_at desc. v1.5 should sort by recent-use and let the user
pin/hide entries via a Preferences tab.
"""
from __future__ import annotations

from typing import Callable

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSBezelStyleRounded, NSColor, NSScrollView,
    NSTextField, NSView, NSMakeRect,
)

from .._objc_targets import attach as _attach
from ..daemon_client import DaemonClient
from ..model import StatusModel
from ..theme import colors, typography


def make_quick_record_view(
    *,
    client: DaemonClient,
    model: StatusModel,
    on_view_status: Callable[[], None],
    width: float,
) -> NSView:
    """Build the quick-record primary view.

    Parameters
    ----------
    client:
        DaemonClient used to fetch definitions and record annotations.
    model:
        Shared StatusModel (unused by the current view but kept for
        future use, e.g. refreshing on daemon reconnect).
    on_view_status:
        Callback invoked when the user taps the "View Status" button to
        switch to the plugin-status secondary view.
    width:
        Popover content width in points.
    """
    HEIGHT = 360.0
    HEADER_H = 40.0
    FOOTER_H = 40.0
    BODY_H = HEIGHT - HEADER_H - FOOTER_H

    root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, HEIGHT))
    root.setWantsLayer_(True)
    root.layer().setBackgroundColor_(colors.bg().CGColor())

    # ── Header: "What do you want to log?" ─────────────────────────────────
    header = NSView.alloc().initWithFrame_(NSMakeRect(0, HEIGHT - HEADER_H, width, HEADER_H))
    header.setWantsLayer_(True)
    header.layer().setBackgroundColor_(colors.bg().CGColor())

    title = NSTextField.labelWithString_("What do you want to log?")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, 10, width - 32, 22))
    header.addSubview_(title)

    # Thin separator below header
    sep_top = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 1))
    sep_top.setWantsLayer_(True)
    sep_top.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    header.addSubview_(sep_top)
    root.addSubview_(header)

    # ── Scrollable annotation list ─────────────────────────────────────────
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(0, FOOTER_H, width, BODY_H)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)

    # Use a flipped view so rows render top-to-bottom (y=0 at top).
    from ..preferences.plugins_tab import _FlippedView
    content = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 0))
    content.setWantsLayer_(True)
    content.layer().setBackgroundColor_(colors.bg().CGColor())
    scroll.setDocumentView_(content)
    root.addSubview_(scroll)

    # ── Footer: "View Status →" button ────────────────────────────────────
    footer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, FOOTER_H))
    footer.setWantsLayer_(True)
    footer.layer().setBackgroundColor_(colors.bg().CGColor())

    # Thin separator above footer
    sep_bot = NSView.alloc().initWithFrame_(NSMakeRect(0, FOOTER_H - 1, width, 1))
    sep_bot.setWantsLayer_(True)
    sep_bot.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    footer.addSubview_(sep_bot)

    status_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 132, 8, 116, 24))
    status_btn.setTitle_("View Status →")
    status_btn.setBezelStyle_(NSBezelStyleRounded)
    _attach(status_btn, lambda _s: on_view_status())
    footer.addSubview_(status_btn)
    root.addSubview_(footer)

    # ── Populate the content view ─────────────────────────────────────────

    def rebuild():
        for sv in list(content.subviews()):
            sv.removeFromSuperview()

        try:
            reply = client.quick_record_list()
        except Exception as exc:
            reply = {"ok": False, "error": str(exc), "definitions": []}

        if not reply.get("ok"):
            err_label = NSTextField.labelWithString_(
                f"Quick record unavailable: {reply.get('error', 'unknown')}"
            )
            err_label.setFont_(typography.small())
            err_label.setTextColor_(colors.text_secondary())
            err_label.setFrame_(NSMakeRect(16, 4, width - 32, 32))
            content.addSubview_(err_label)
            content.setFrame_(NSMakeRect(0, 0, width, 40))
            return

        defs = reply.get("definitions", [])
        if not defs:
            empty = NSTextField.labelWithString_(
                "No Moment annotations yet. Create one at "
                "fulcra-dynamics.com to see it here."
            )
            empty.setFont_(typography.small())
            empty.setTextColor_(colors.text_secondary())
            # Word-wrap: NSLineBreakByWordWrapping = 0
            empty.setLineBreakMode_(0)
            empty.setFrame_(NSMakeRect(16, 4, width - 32, 40))
            content.addSubview_(empty)
            content.setFrame_(NSMakeRect(0, 0, width, 48))
            return

        ROW_H = 44.0
        y = 0.0
        for d in defs:
            row = _make_row(d, width, ROW_H, client=client)
            row.setFrame_(NSMakeRect(0, y, width, ROW_H))
            content.addSubview_(row)
            y += ROW_H
        content.setFrame_(NSMakeRect(0, 0, width, max(y, BODY_H)))

    rebuild()
    return root


def _make_row(
    definition: dict,
    width: float,
    height: float,
    *,
    client: DaemonClient,
) -> NSView:
    """One annotation-definition row with name label + Record button."""
    row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    row.setWantsLayer_(True)
    row.layer().setBackgroundColor_(colors.bg().CGColor())

    # Bottom hairline separator
    sep = NSView.alloc().initWithFrame_(NSMakeRect(16, 0, width - 16, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    row.addSubview_(sep)

    # Annotation name
    name_label = NSTextField.labelWithString_(
        definition.get("name", "(unnamed)")
    )
    name_label.setFont_(typography.body())
    name_label.setTextColor_(colors.text())
    # Vertically centred in the row
    name_label.setFrame_(NSMakeRect(16, (height - 18) / 2, width - 120, 18))
    row.addSubview_(name_label)

    # "Record" button, right-aligned
    btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - 96, (height - 24) / 2, 80, 24)
    )
    btn.setTitle_("Record")
    btn.setBezelStyle_(NSBezelStyleRounded)

    def _on_click(_sender, def_id=definition.get("id", "")):
        if def_id:
            try:
                client.record_annotation(def_id)
            except Exception:
                pass  # graceful: daemon may have stopped; user will see it restart

    _attach(btn, _on_click)
    row.addSubview_(btn)

    return row
