"""The rumps.App subclass.

Hosts the model layer, wires the status item, opens the popover on
left-click (via a direct NSStatusItem button target — no rumps menu),
and delegates preferences to a separate NSWindowController.

UX model
--------
- Left-click on the menubar icon opens the popover immediately.
- The popover header contains a small gear (⚙) icon; clicking it opens
  the Preferences window.
- The popover footer contains a "Quit" button.
- No rumps menu is attached to the status item (cleared via NSStatusItem.setMenu_(None)).
"""
from __future__ import annotations

import logging
import threading

import rumps  # type: ignore[import-not-found]

from .daemon_client import DaemonClient, DaemonUnavailable
from .model import StatusModel
from .polling import PollingScheduler
from .popover.root import PopoverRoot
from .status_item import StatusItemController

logger = logging.getLogger("fulcra_menubar")


class FulcraMenubarApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Fulcra Collect", icon=None, quit_button=None)
        self.client = DaemonClient()
        self.model = StatusModel()
        self.status_item = StatusItemController(self, self.model)
        self._prefs_controller = None

        # rumps would otherwise show its default menu on left-click;
        # clear it so our setTarget_/setAction_ takes effect.
        self._nsapp.nsstatusitem.setMenu_(None)

        self.popover = PopoverRoot(
            self.model, self.client,
            on_preferences=self._open_prefs,
            on_quit=self._quit,
        )
        self.poller = PollingScheduler(on_tick=self._poll_once)
        self.poller.set_popover_open(False)
        threading.Thread(target=self.poller.run, daemon=True).start()
        self._install_sleep_wake_observers()

        from .notifications import NotificationCentre
        self.notifications = NotificationCentre(post=self._post_notification)
        self._request_notification_authorization()
        # Hook failure-threshold transitions to notifications.
        self.model.add_failure_transition_observer(
            lambda pid: self.notifications.notify_failure(pid, "consecutive failures ≥ 3")
        )

        # Wire the status-item button to open the popover directly on left-click.
        # This replaces the old rumps menu approach.  We retain the target object
        # on self so PyObjC doesn't garbage-collect it.
        self._status_target = _install_click_target(self)

    # ── Popover ───────────────────────────────────────────────────────────────

    def _open_popover(self) -> None:
        """Open (or toggle) the popover anchored to the status item button."""
        try:
            btn = self._nsapp.nsstatusitem.button()
        except AttributeError:
            return
        self.popover.toggle(btn)
        self.poller.set_popover_open(self.popover.is_shown)

    # ── Preferences ──────────────────────────────────────────────────────────

    def _open_prefs(self) -> None:
        """Open the Preferences window (lazily created on first use)."""
        from .preferences.window import make_preferences_controller
        if self._prefs_controller is None:
            self._prefs_controller = make_preferences_controller(
                model=self.model, client=self.client, centre=self.notifications,
            )
        self._prefs_controller.window().makeKeyAndOrderFront_(None)
        from AppKit import NSApp  # type: ignore[import-not-found]
        NSApp.activateIgnoringOtherApps_(True)

    # ── Quit ─────────────────────────────────────────────────────────────────

    def _quit(self) -> None:
        from .popover.bootstrap import cancel_pending
        cancel_pending()
        rumps.quit_application()

    # ── Notifications ─────────────────────────────────────────────────────────

    def _request_notification_authorization(self) -> None:
        try:
            from UserNotifications import (  # type: ignore[import-not-found]
                UNAuthorizationOptionAlert, UNAuthorizationOptionSound,
                UNUserNotificationCenter,
            )
        except ImportError:
            return
        centre = UNUserNotificationCenter.currentNotificationCenter()
        opts = UNAuthorizationOptionAlert | UNAuthorizationOptionSound

        def handler(granted, err):
            if err is not None:
                logger.warning("UN authorization error: %s", err)
        centre.requestAuthorizationWithOptions_completionHandler_(opts, handler)

    def _post_notification(self, title: str, body: str) -> None:
        try:
            from UserNotifications import (  # type: ignore[import-not-found]
                UNMutableNotificationContent, UNNotificationRequest,
                UNUserNotificationCenter,
            )
        except ImportError:
            print(f"[notify] {title}: {body}")
            return
        import uuid
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None,
        )
        UNUserNotificationCenter.currentNotificationCenter() \
            .addNotificationRequest_withCompletionHandler_(request, None)

    # ── Sleep / wake observers ────────────────────────────────────────────────

    def _install_sleep_wake_observers(self) -> None:
        """Register NSWorkspace sleep/wake observers so the poller pauses on sleep.

        On wake, PollingScheduler.resume() fires the next tick immediately so
        stale status is cleared within ~2 s of unlock instead of waiting for
        the next 10 s heartbeat.

        Silently skipped on non-Darwin platforms where AppKit/Foundation are
        unavailable.
        """
        try:
            from AppKit import NSWorkspace  # type: ignore[import-not-found]
            from Foundation import NSObject  # type: ignore[import-not-found]
        except ImportError:
            return

        centre = NSWorkspace.sharedWorkspace().notificationCenter()
        outer = self

        class _Listener(NSObject):
            def onSleep_(self, _n):
                outer.poller.suspend()

            def onWake_(self, _n):
                outer.poller.resume()

        self._sleep_listener = _Listener.alloc().init()
        centre.addObserver_selector_name_object_(
            self._sleep_listener, "onSleep:",
            "NSWorkspaceWillSleepNotification", None,
        )
        centre.addObserver_selector_name_object_(
            self._sleep_listener, "onWake:",
            "NSWorkspaceDidWakeNotification", None,
        )

    # ── Poll ──────────────────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        try:
            reply = self.client.status()
        except DaemonUnavailable:
            self.model.mark_daemon_stopped()
            return
        self.model.update_from_status(reply)


# ── Status-item click target ───────────────────────────────────────────────────

def _install_click_target(app: FulcraMenubarApp):
    """Wire the NSStatusItem button so a left-click opens the popover directly.

    Returns the target object which the caller must retain on ``self`` so
    PyObjC doesn't garbage-collect it before the button fires.

    The pattern mirrors ``_RowTarget`` in ``popover/plugin_row.py``: an
    NSObject subclass whose ``open_:`` selector is set as the button's action.
    """
    try:
        import objc  # type: ignore[import-not-found]
        from Foundation import NSObject  # type: ignore[import-not-found]
        btn = app._nsapp.nsstatusitem.button()
    except AttributeError:
        # Running in a test/non-macOS environment without a real status item.
        return None

    class _StatusItemTarget(NSObject):
        def initWithApp_(self, app_ref):
            self = objc.super(_StatusItemTarget, self).init()
            if self is None:
                return None
            self._app = app_ref
            return self

        def open_(self, sender):
            self._app._open_popover()

    target = _StatusItemTarget.alloc().initWithApp_(app)
    btn.setTarget_(target)
    btn.setAction_("open:")
    return target
