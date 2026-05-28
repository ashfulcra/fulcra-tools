"""The Preferences window — NSWindowController hosting an NSTabView.

Tabs: Plugins, Annotations, Notifications, About. Each tab is a separate
NSView factory in its own module; this file just wires them up.

The Annotations tab (added in SP2, drift audit 2026-05-27) sits between
Plugins and Notifications so the user's mental flow reads "set up your
sources (Plugins) → manage what you're tracking (Annotations) → control
how the app talks to you (Notifications) → app metadata (About)".

Use ``make_preferences_controller(...)`` (module-level function) rather than
``PreferencesController.create(...)`` — PyObjC's NSObject subclass transform
rejects Python keyword-only arguments because ObjC selectors can't represent
them, causing a BadPrototypeError at click time.
"""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSAppearance, NSBackingStoreBuffered, NSTabView, NSTabViewItem, NSTitledWindowMask,
    NSWindow, NSWindowController, NSClosableWindowMask, NSMiniaturizableWindowMask,
    NSMakeRect,
)

from ..daemon_client import DaemonClient
from ..model import StatusModel
from ..notifications import NotificationCentre


WIDTH = 640.0
HEIGHT = 480.0


class PreferencesController(NSWindowController):
    """NSWindowController subclass for the Preferences window.

    Do not add classmethods with keyword-only (*) arguments — PyObjC's NSObject
    transform machinery will raise BadPrototypeError when such a method is
    looked up via the ObjC runtime.  Construction helpers live at module scope
    instead (see ``make_preferences_controller``).
    """


def make_preferences_controller(
    *, model: StatusModel, client: DaemonClient, centre: NotificationCentre
) -> PreferencesController:
    """Build and return a PreferencesController without attaching it to the class.

    Keeping the factory at module scope sidesteps PyObjC's NSObject transform,
    which rejects Python keyword-only arguments (``*,``) because ObjC selectors
    have no equivalent representation.
    """
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, WIDTH, HEIGHT),
        NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask,
        NSBackingStoreBuffered, False,
    )
    window.setTitle_("Fulcra Collect — Preferences")
    window.center()
    # Force light / Aqua appearance so the Preferences window always renders
    # on the brand-mandated white background regardless of system Dark Mode.
    window.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))

    tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT - 22))

    from .plugins_tab import make_plugins_tab
    from .annotations_tab import make_annotations_tab
    from .notifications_tab import make_notifications_tab
    from .about_tab import make_about_tab

    plugins_view = make_plugins_tab(model=model, client=client)
    annotations_view = make_annotations_tab(client=client)
    notifs_view = make_notifications_tab(centre=centre)
    about_view = make_about_tab(client=client)

    for label, view in (
        ("Plugins", plugins_view),
        ("Annotations", annotations_view),
        ("Notifications", notifs_view),
        ("About", about_view),
    ):
        item = NSTabViewItem.alloc().initWithIdentifier_(label)
        item.setLabel_(label)
        item.setView_(view)
        tabs.addTabViewItem_(item)

    window.contentView().addSubview_(tabs)

    controller = PreferencesController.alloc().initWithWindow_(window)
    return controller
