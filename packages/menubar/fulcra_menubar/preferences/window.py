"""The Preferences window — NSWindowController hosting an NSTabView.

Tabs: Plugins, Notifications, About. Each tab is a separate NSView
factory in its own module; this file just wires them up.
"""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSBackingStoreBuffered, NSTabView, NSTabViewItem, NSTitledWindowMask,
    NSWindow, NSWindowController, NSClosableWindowMask, NSMiniaturizableWindowMask,
    NSMakeRect,
)

from ..daemon_client import DaemonClient
from ..model import StatusModel
from ..notifications import NotificationCentre


WIDTH = 640.0
HEIGHT = 480.0


class PreferencesController(NSWindowController):
    @classmethod
    def create(cls, *, model: StatusModel, client: DaemonClient,
                centre: NotificationCentre) -> "PreferencesController":
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask,
            NSBackingStoreBuffered, False,
        )
        window.setTitle_("Fulcra Collect — Preferences")
        window.center()

        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT - 22))

        from .plugins_tab import make_plugins_tab
        from .notifications_tab import make_notifications_tab
        from .about_tab import make_about_tab

        plugins_view = make_plugins_tab(model=model, client=client)
        notifs_view = make_notifications_tab(centre=centre)
        about_view = make_about_tab(client=client)

        for label, view in (
            ("Plugins", plugins_view),
            ("Notifications", notifs_view),
            ("About", about_view),
        ):
            item = NSTabViewItem.alloc().initWithIdentifier_(label)
            item.setLabel_(label)
            item.setView_(view)
            tabs.addTabViewItem_(item)

        window.contentView().addSubview_(tabs)

        controller = cls.alloc().initWithWindow_(window)
        return controller
