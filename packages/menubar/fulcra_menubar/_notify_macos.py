"""macOS notification posting via UserNotifications, guarded for dev runs.

``UNUserNotificationCenter.currentNotificationCenter()`` raises an
NSInternalInconsistencyException ("bundleProxyForCurrentProcess is nil") when
the running process has no app-bundle identifier — e.g. when the menubar app
is launched as ``python -m fulcra_menubar`` from a venv rather than as a built
``.app``. That exception is thrown inside a ``dispatch_once`` block and aborts
the process via ``libc++abi``; it is **not** catchable from Python (a
``try/except`` around the call does not help). The only safe approach is to
check for a bundle identifier first and skip the framework entirely when it is
absent, degrading to a stdout line.

PyObjC is imported lazily inside the functions so this module stays importable
on non-macOS CI (where the menubar test suite still runs the pure-logic tests).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("fulcra_menubar")


def running_in_app_bundle() -> bool:
    """True only when the process has a real app-bundle identifier.

    This is the exact precondition ``UNUserNotificationCenter`` requires; when
    it is False the framework call aborts the process, so callers must skip it.
    """
    try:
        from Foundation import NSBundle  # type: ignore[import-not-found]
    except ImportError:
        return False
    return NSBundle.mainBundle().bundleIdentifier() is not None


def request_authorization() -> None:
    """Ask for alert/sound notification permission. No-op when unbundled."""
    if not running_in_app_bundle():
        return
    from UserNotifications import (  # type: ignore[import-not-found]
        UNAuthorizationOptionAlert, UNAuthorizationOptionSound,
        UNUserNotificationCenter,
    )
    centre = UNUserNotificationCenter.currentNotificationCenter()
    opts = UNAuthorizationOptionAlert | UNAuthorizationOptionSound

    def handler(granted, err):
        if err is not None:
            logger.warning("UN authorization error: %s", err)

    centre.requestAuthorizationWithOptions_completionHandler_(opts, handler)


def post_notification(title: str, body: str) -> None:
    """Post a user notification. Falls back to stdout when unbundled."""
    if not running_in_app_bundle():
        print(f"[notify] {title}: {body}")
        return
    import uuid

    from UserNotifications import (  # type: ignore[import-not-found]
        UNMutableNotificationContent, UNNotificationRequest,
        UNUserNotificationCenter,
    )
    content = UNMutableNotificationContent.alloc().init()
    content.setTitle_(title)
    content.setBody_(body)
    request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
        str(uuid.uuid4()), content, None,
    )
    UNUserNotificationCenter.currentNotificationCenter() \
        .addNotificationRequest_withCompletionHandler_(request, None)
