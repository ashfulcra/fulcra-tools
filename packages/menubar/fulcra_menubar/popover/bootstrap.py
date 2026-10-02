"""The 'Daemon not running' card that replaces the plugin list when
the control socket is unreachable. Single CTA: 'Install & start daemon'
writes the launchd plist for the copy of ``fulcra-collect`` inside this app,
registers it as a Login Item when macOS supports that, and starts it with the
same lifecycle code used by the daemon controls.
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

from AppKit import (  # type: ignore[import-not-found]
    NSBezelStyleRounded, NSButton, NSTextField,
    NSView, NSMakeRect,
)

from .._objc_targets import attach as _attach
from ..theme import colors, typography

def cancel_pending() -> None:
    """Compatibility hook for the app's quit path.

    Install now uses bounded synchronous lifecycle calls on a daemon thread,
    so there is no child process owned by this module to cancel.
    """


def make_bootstrap_card(width: float, height: float) -> NSView:
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(colors.bg().CGColor())

    title = NSTextField.labelWithString_("Fulcra Collect is not running.")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, height - 56, width - 32, 22))
    view.addSubview_(title)

    body = NSTextField.labelWithString_(
        "The Fulcra Collect daemon hosts your local importers and is "
        "required for this menubar."
    )
    body.setFont_(typography.body())
    body.setTextColor_(colors.text_secondary())
    body.setFrame_(NSMakeRect(16, height - 110, width - 32, 40))
    view.addSubview_(body)

    button = NSButton.alloc().initWithFrame_(NSMakeRect(
        (width - 200) / 2, height - 160, 200, 28,
    ))
    button.setBezelStyle_(NSBezelStyleRounded)

    from .. import daemon_lifecycle
    executable = daemon_lifecycle.expected_executable()
    if Path(executable).is_file() or shutil.which(executable):
        button.setTitle_("Install & start daemon")
    else:
        button.setTitle_("Install fulcra-collect first")
        button.setEnabled_(False)
    view.addSubview_(button)

    log = NSTextField.labelWithString_("")
    log.setFont_(typography.mono())
    log.setTextColor_(colors.text_tertiary())
    log.setFrame_(NSMakeRect(16, 16, width - 32, height - 196))
    log.setLineBreakMode_(0)  # word-wrap
    view.addSubview_(log)

    def on_click(_sender):
        log.setStringValue_("Running…")
        def work():
            try:
                daemon_lifecycle.install()
                daemon_lifecycle.start()
                output = "Daemon installed and started."
            except Exception as exc:
                output = f"{type(exc).__name__}: {exc}"
            # Update label on main thread.
            from AppKit import NSOperationQueue  # type: ignore[import-not-found]
            def main():
                log.setStringValue_(output[:400])
            NSOperationQueue.mainQueue().addOperationWithBlock_(main)

        threading.Thread(target=work, daemon=True).start()

    _attach(button, on_click)
    return view

