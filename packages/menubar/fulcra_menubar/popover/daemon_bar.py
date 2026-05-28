"""The 'Daemon controls' bar shown above the popover footer.

Surfaces the daemon's lifecycle to the user without making them touch
launchctl by hand:

    Daemon: Running (PID 12345)             [Restart] [Stop]
    [✓] Open at Login

When the daemon is stopped the bar shows a [Start] button instead, and
when the plist isn't installed at all the only button is [Install].

The bar is always visible (sits between the body container and the
footer), and re-queries its state every time the popover is opened so
the user always sees current truth.  We don't poll continuously: the
menubar's main polling loop already detects daemon-stopped via the
control socket, and lifecycle actions are explicit user gestures that
refresh in-place when they fire.

Layout
──────
The bar is 64pt tall (two rows): the status / action row on top, the
Open-at-Login toggle row beneath.  Width matches the popover.

All daemon-touching subprocess calls happen on a background thread so
the UI stays responsive; the result is funneled back to the main thread
via NSOperationQueue to update labels and re-enable buttons.  Errors
post a macOS notification with the underlying message (DaemonLifecycleError
includes the launchctl/SMAppService text verbatim).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Optional

from AppKit import (  # type: ignore[import-not-found]
    NSBezelStyleRounded, NSButton, NSColor, NSMakeRect,
    NSTextField, NSView, NSSwitch,
)

from .._objc_targets import attach as _attach
from ..theme import typography
from .. import daemon_lifecycle as dl

logger = logging.getLogger("fulcra_menubar.popover.daemon_bar")

BAR_HEIGHT = 64.0


def make_daemon_bar(
    *,
    width: float,
    notify: Optional[Callable[[str, str], None]] = None,
) -> NSView:
    """Build the daemon-controls bar.

    Parameters
    ----------
    width:
        Bar width — should match the popover content width (360pt).
    notify:
        Callback ``(title, body)`` for failure notifications.  When None
        (test fixtures), errors are logged but not surfaced.

    Returns
    -------
    NSView
        The bar.  Has a single public method, ``refresh()``, which
        re-queries daemon_lifecycle.status() and re-builds the row.
        Callers (PopoverRoot.toggle) should call it whenever the popover
        opens so the user sees current state.
    """
    view = _DaemonBarView.alloc().initWithFrame_(NSMakeRect(0, 0, width, BAR_HEIGHT))
    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(NSColor.controlBackgroundColor().CGColor())

    # Hairline at the top of the bar to separate it from the body above.
    sep = NSView.alloc().initWithFrame_(NSMakeRect(0, BAR_HEIGHT - 1, width, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    view.addSubview_(sep)

    # ── Row 1: status label + action buttons ──────────────────────────────
    status_label = NSTextField.labelWithString_("Daemon: …")
    status_label.setFont_(typography.small())
    status_label.setFrame_(NSMakeRect(12, 36, 200, 18))
    view.addSubview_(status_label)
    view._status_label = status_label

    # Action buttons live on the right edge.  We allocate three slots
    # (Install / Start / Stop / Restart) and toggle their visibility in
    # refresh() rather than tearing down the bar on every state change.
    btn_w, btn_h = 76.0, 22.0
    gap = 6.0
    # Rightmost slot
    btn_a = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - btn_w - 12, 34, btn_w, btn_h),
    )
    btn_a.setBezelStyle_(NSBezelStyleRounded)
    btn_a.setFont_(typography.small())
    btn_a.setHidden_(True)
    view.addSubview_(btn_a)
    view._btn_a = btn_a

    # Second slot to its left
    btn_b = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - 2 * btn_w - 12 - gap, 34, btn_w, btn_h),
    )
    btn_b.setBezelStyle_(NSBezelStyleRounded)
    btn_b.setFont_(typography.small())
    btn_b.setHidden_(True)
    view.addSubview_(btn_b)
    view._btn_b = btn_b

    # ── Row 2: Open at Login toggle ───────────────────────────────────────
    login_label = NSTextField.labelWithString_("Open at Login")
    login_label.setFont_(typography.small())
    login_label.setFrame_(NSMakeRect(12, 8, 140, 18))
    view.addSubview_(login_label)
    view._login_label = login_label

    # NSSwitch is the macOS 10.15+ toggle widget; falls back to a
    # checkbox-style NSButton if NSSwitch isn't loadable.
    try:
        toggle = NSSwitch.alloc().initWithFrame_(
            NSMakeRect(width - 50, 4, 38, 22),
        )
    except Exception:  # pragma: no cover — defensive, NSSwitch is in 10.15+
        toggle = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - 60, 6, 50, 22),
        )
        toggle.setButtonType_(3)  # NSButtonTypeSwitch
    view.addSubview_(toggle)
    view._login_toggle = toggle

    # ── Wire callbacks ───────────────────────────────────────────────────
    def _on_action_clicked(action_name: str):
        """Common click handler — runs the named action on a background
        thread, then refreshes the bar on the main thread."""
        view._set_buttons_enabled(False)

        def work():
            error_msg: Optional[str] = None
            try:
                if action_name == "install":
                    dl.install()
                elif action_name == "start":
                    dl.start()
                elif action_name == "stop":
                    dl.stop()
                elif action_name == "restart":
                    dl.restart()
            except dl.DaemonLifecycleError as exc:
                error_msg = str(exc)
            except Exception as exc:  # noqa: BLE001 — surface anything
                error_msg = f"{type(exc).__name__}: {exc}"

            from AppKit import NSOperationQueue  # type: ignore[import-not-found]

            def main():
                view._set_buttons_enabled(True)
                if error_msg and notify is not None:
                    notify(f"Daemon {action_name} failed", error_msg)
                elif error_msg:
                    logger.warning("daemon %s failed: %s", action_name, error_msg)
                view.refresh()

            NSOperationQueue.mainQueue().addOperationWithBlock_(main)

        threading.Thread(target=work, daemon=True).start()

    def _on_toggle_clicked(_sender):
        """Toggle Open-at-Login.  Background-threaded; reads the toggle's
        current intended state from the widget (NSSwitch state: 1 = on)."""
        # Capture intended new state BEFORE flipping anything else.
        wants_on = bool(toggle.state())
        view._set_buttons_enabled(False)

        def work():
            error_msg: Optional[str] = None
            try:
                if wants_on:
                    dl.register_login_item()
                else:
                    dl.unregister_login_item()
            except dl.DaemonLifecycleError as exc:
                error_msg = str(exc)

            from AppKit import NSOperationQueue  # type: ignore[import-not-found]

            def main():
                view._set_buttons_enabled(True)
                if error_msg and notify is not None:
                    notify("Login Item change failed", error_msg)
                elif error_msg:
                    logger.warning("login-item toggle failed: %s", error_msg)
                view.refresh()

            NSOperationQueue.mainQueue().addOperationWithBlock_(main)

        threading.Thread(target=work, daemon=True).start()

    view._on_action_clicked = _on_action_clicked
    _attach(toggle, _on_toggle_clicked)

    view.refresh()
    return view


# ──────────────────────────────────────────────────────────────────────────────

# NSView subclass so we can hang Python state off the instance and expose
# a `refresh()` method that PopoverRoot calls each time the popover opens.

try:
    # Foundation / objc are only needed to confirm we're in a PyObjC
    # environment where subclassing NSView at module import time works.
    # On non-macOS the import itself raises ImportError and we fall back
    # to the plain NSView reference below.
    import Foundation  # type: ignore[import-not-found]  # noqa: F401
    import objc  # type: ignore[import-not-found]  # noqa: F401

    class _DaemonBarView(NSView):
        def refresh(self):
            """Re-query daemon_lifecycle and update labels + button visibility.

            Safe to call from the main thread only (touches AppKit views).
            """
            try:
                st = dl.status()
            except Exception:
                logger.exception("daemon_lifecycle.status() raised")
                st = "stopped"

            running, pid = (False, None)
            if st == "running":
                running, pid = dl.is_running()

            # ── Status label text ──────────────────────────────────────
            if st == "running":
                if pid is not None:
                    text = f"Daemon: Running (PID {pid})"
                else:
                    text = "Daemon: Running"
                color = NSColor.systemGreenColor()
            elif st == "stopped":
                text = "Daemon: Stopped"
                color = NSColor.secondaryLabelColor()
            elif st == "needs_approval":
                text = "Daemon: Login-item approval needed"
                color = NSColor.systemOrangeColor()
            else:  # "not_installed"
                text = "Daemon: Not installed"
                color = NSColor.secondaryLabelColor()

            self._status_label.setStringValue_(text)
            self._status_label.setTextColor_(color)

            # ── Button visibility + handlers ───────────────────────────
            # btn_a = rightmost (primary action), btn_b = secondary
            self._btn_a.setHidden_(True)
            self._btn_b.setHidden_(True)

            if st == "not_installed":
                _set_button(self._btn_a, "Install",
                            lambda _s: self._on_action_clicked("install"))
            elif st == "running":
                _set_button(self._btn_a, "Stop",
                            lambda _s: self._on_action_clicked("stop"))
                _set_button(self._btn_b, "Restart",
                            lambda _s: self._on_action_clicked("restart"))
            elif st == "needs_approval":
                # The user must act in System Settings; nothing actionable
                # in-bar.  Hide both action buttons; the toggle row below
                # still shows the unfulfilled "Open at Login" intent.
                pass
            else:  # "stopped"
                _set_button(self._btn_a, "Start",
                            lambda _s: self._on_action_clicked("start"))

            # ── Login-Item toggle state + visibility ───────────────────
            login_st = dl.login_item_status()
            if login_st == "unavailable":
                # macOS too old / non-Darwin; hide the toggle row entirely.
                self._login_toggle.setHidden_(True)
                self._login_label.setHidden_(True)
            else:
                self._login_toggle.setHidden_(False)
                self._login_label.setHidden_(False)
                self._login_toggle.setState_(1 if login_st == "enabled" else 0)
                if login_st == "needs_approval":
                    self._login_label.setStringValue_(
                        "Open at Login (approve in System Settings)"
                    )
                else:
                    self._login_label.setStringValue_("Open at Login")

        def _set_buttons_enabled(self, enabled: bool):
            self._btn_a.setEnabled_(enabled)
            self._btn_b.setEnabled_(enabled)
            self._login_toggle.setEnabled_(enabled)

except ImportError:  # pragma: no cover — non-macOS path; module shouldn't
    # even be imported there, but stay graceful.
    _DaemonBarView = NSView  # type: ignore[assignment,misc]


def _set_button(btn, title: str, on_click):
    """Configure a button with a title + click handler and show it."""
    btn.setTitle_(title)
    _attach(btn, on_click)
    btn.setHidden_(False)
