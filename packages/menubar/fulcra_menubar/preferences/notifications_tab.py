"""Notifications tab — filled in Task 17."""
from __future__ import annotations

from AppKit import NSTextField, NSView, NSMakeRect  # type: ignore[import-not-found]

from ..notifications import NotificationCentre
from ..theme import colors, typography


def make_notifications_tab(*, centre: NotificationCentre):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 440))
    label = NSTextField.labelWithString_("Notifications — coming in Task 17.")
    label.setFont_(typography.body())
    label.setTextColor_(colors.text_secondary())
    label.setFrame_(NSMakeRect(16, 400, 600, 18))
    view.addSubview_(label)
    return view
