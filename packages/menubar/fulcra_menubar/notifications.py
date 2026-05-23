"""Failure-notification de-dup logic.

Each notification category is rate-limited to at most one post per
hour. The actual macOS notification is injected as a callback — the
tests pass a recorder; on macOS the app passes a PyObjC wrapper around
UNUserNotificationCenter.

This module imports no PyObjC. The real `post` implementation lives in
app.py and uses pyobjc-framework-UserNotifications.
"""
from __future__ import annotations

import time as _time
from collections.abc import Callable

DEDUP_WINDOW_S = 3600.0

_DAEMON_STOPPED_KEY = ("_daemon", "_stopped")


class NotificationCentre:
    def __init__(
        self,
        *,
        post: Callable[[str, str], None],
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._post = post
        self._monotonic = monotonic or _time.monotonic
        self._last_posted_at: dict[tuple[str, str], float] = {}
        self.mute_all = False

    def notify_failure(self, plugin_id: str, error: str) -> None:
        self._maybe_post(
            key=("failure", plugin_id),
            title=f"{plugin_id} is failing",
            body=error or "Fulcra Collect plugin has failed 3 times in a row.",
        )

    def notify_daemon_stopped(self) -> None:
        self._maybe_post(
            key=_DAEMON_STOPPED_KEY,
            title="Fulcra Collect daemon stopped",
            body="The background daemon is no longer running. Open the "
                 "menubar to start it again.",
        )

    def _maybe_post(self, *, key: tuple[str, str], title: str, body: str) -> None:
        if self.mute_all:
            return
        now = self._monotonic()
        last = self._last_posted_at.get(key, -DEDUP_WINDOW_S - 1)
        if now - last < DEDUP_WINDOW_S:
            return
        self._last_posted_at[key] = now
        self._post(title, body)
