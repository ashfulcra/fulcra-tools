"""The rumps.App subclass.

Hosts the model layer, wires the status item, opens the popover on
click. Sleep/wake observers and preferences land in later tasks.
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
        self.popover = PopoverRoot(self.model, self.client)
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

        self._prefs_controller = None

        self.menu = ["Open Fulcra Collect", "Preferences…", None, "Quit"]

    @rumps.clicked("Open Fulcra Collect")
    def _open(self, _sender) -> None:
        try:
            btn = self._nsapp.nsstatusitem.button()
        except AttributeError:
            return
        self.popover.toggle(btn)
        self.poller.set_popover_open(self.popover.is_shown)

    @rumps.clicked("Preferences…")
    def _open_prefs(self, _sender) -> None:
        from .preferences.window import PreferencesController
        if self._prefs_controller is None:
            self._prefs_controller = PreferencesController.create(
                model=self.model, client=self.client, centre=self.notifications,
            )
        self._prefs_controller.window().makeKeyAndOrderFront_(None)
        from AppKit import NSApp  # type: ignore[import-not-found]
        NSApp.activateIgnoringOtherApps_(True)

    @rumps.clicked("Quit")
    def _quit(self, _sender) -> None:
        from .popover.bootstrap import cancel_pending
        cancel_pending()
        rumps.quit_application()

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

    def _poll_once(self) -> None:
        try:
            reply = self.client.status()
        except DaemonUnavailable:
            self.model.mark_daemon_stopped()
            return
        self.model.update_from_status(reply)
