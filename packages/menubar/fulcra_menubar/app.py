"""The rumps.App subclass.

Hosts the model layer, wires the status item, opens the popover on
click. Sleep/wake observers, preferences, and the notification post
path land in later tasks.
"""
from __future__ import annotations

import logging
import threading

import rumps  # type: ignore[import-not-found]

from .daemon_client import DaemonClient, DaemonUnavailable
from .model import StatusModel
from .polling import PollingScheduler
from .status_item import StatusItemController

logger = logging.getLogger("fulcra_menubar")


class FulcraMenubarApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Fulcra Collect", icon=None, quit_button=None)
        self.client = DaemonClient()
        self.model = StatusModel()
        self.status_item = StatusItemController(self, self.model)
        self.poller = PollingScheduler(on_tick=self._poll_once)
        self.poller.set_popover_open(False)
        threading.Thread(target=self.poller.run, daemon=True).start()
        self.menu = ["Quit"]

    @rumps.clicked("Quit")
    def _quit(self, _sender):
        rumps.quit_application()

    def _poll_once(self) -> None:
        try:
            reply = self.client.status()
        except DaemonUnavailable:
            self.model.mark_daemon_stopped()
            return
        self.model.update_from_status(reply)
