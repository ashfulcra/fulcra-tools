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

# Tab name -> NSTabView index, in the exact order tabs are added below.
# Used by the "open Preferences to Annotations" deep link from the popover.
_TAB_INDEX = {"plugins": 0, "annotations": 1, "notifications": 2, "about": 3}


def tab_index(name: str | None) -> int | None:
    """Return the NSTabView index for a tab name, or None for unknown/None.

    The map is anchored to the add-order in ``make_preferences_controller``
    (Plugins=0, Annotations=1, Notifications=2, About=3).  Callers that need
    to jump straight to a specific tab (e.g. the popover's 'Choose tracks to
    pin…' CTA) use this rather than hard-coding a raw integer so a future
    re-order only requires updating ``_TAB_INDEX`` and this function.
    """
    if name is None:
        return None
    return _TAB_INDEX.get(name)


class PreferencesController(NSWindowController):
    """NSWindowController subclass for the Preferences window.

    Do not add classmethods with keyword-only (*) arguments — PyObjC's NSObject
    transform machinery will raise BadPrototypeError when such a method is
    looked up via the ObjC runtime.  Construction helpers live at module scope
    instead (see ``make_preferences_controller``).
    """

    def select_tab(self, name):
        """Select the tab with the given name (no-op for unknown/None).

        The ``_tabs`` attribute is set by ``make_preferences_controller``
        after the NSTabView is fully configured.  The ``getattr`` guard is
        defensive — if somehow select_tab is called before ``_tabs`` is
        attached (shouldn't happen in practice) the call silently does nothing.
        """
        idx = tab_index(name)
        if idx is not None and getattr(self, "_tabs", None) is not None:
            self._tabs.selectTabViewItemAtIndex_(idx)


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
    controller._tabs = tabs  # plain attribute; used by select_tab
    return controller
