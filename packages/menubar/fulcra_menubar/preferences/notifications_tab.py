"""Notifications tab — failure-threshold + mute-all toggles."""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSSwitch, NSTextField, NSView, NSMakeRect,
)

from .._objc_targets import attach as _attach
from ..notifications import NotificationCentre
from ..theme import colors, typography


def make_notifications_tab(*, centre: NotificationCentre):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 440))

    title = NSTextField.labelWithString_("Notify me when a plugin fails repeatedly")
    title.setFont_(typography.body())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, 400, 500, 18))
    view.addSubview_(title)

    note = NSTextField.labelWithString_(
        "After 3 consecutive failures. At most one notification per plugin per hour."
    )
    note.setFont_(typography.small())
    note.setTextColor_(colors.text_secondary())
    note.setFrame_(NSMakeRect(16, 380, 500, 16))
    view.addSubview_(note)

    fail_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(560, 396, 50, 22))
    fail_switch.setState_(0 if centre.mute_all else 1)
    view.addSubview_(fail_switch)

    mute_title = NSTextField.labelWithString_("Mute all notifications")
    mute_title.setFont_(typography.body())
    mute_title.setTextColor_(colors.text())
    mute_title.setFrame_(NSMakeRect(16, 340, 500, 18))
    view.addSubview_(mute_title)

    mute_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(560, 336, 50, 22))
    mute_switch.setState_(1 if centre.mute_all else 0)

    def on_fail_change(sender):
        # We map the "notify on failure" toggle to NOT-mute-all, since
        # mute_all is the master kill-switch. If the user turns failure
        # notifications off, we set mute_all True. If on, we leave
        # mute_all untouched (the master toggle separately controls it).
        if sender.state() == 0:
            centre.mute_all = True
            mute_switch.setState_(1)
    _attach(fail_switch, on_fail_change)

    def on_mute_change(sender):
        centre.mute_all = bool(sender.state())
        if centre.mute_all:
            fail_switch.setState_(0)
        else:
            fail_switch.setState_(1)
    _attach(mute_switch, on_mute_change)
    view.addSubview_(mute_switch)

    return view


