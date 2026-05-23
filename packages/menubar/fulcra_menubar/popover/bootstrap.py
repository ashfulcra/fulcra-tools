"""The 'Daemon not running' card that replaces the plugin list when
the control socket is unreachable. Single CTA: 'Install & start daemon'
runs `fulcra-collect service install && fulcra-collect service start`
in a subprocess on a background thread, captures stdout/stderr, and
shows the output in a small label below the button.
"""
from __future__ import annotations

import shutil
import subprocess
import threading

from AppKit import (  # type: ignore[import-not-found]
    NSBezelStyleRounded, NSButton, NSTextField,
    NSView, NSMakeRect,
)
from Foundation import NSObject  # type: ignore[import-not-found]

from ..theme import colors, typography


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

    if shutil.which("fulcra-collect"):
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
                p1 = subprocess.run(
                    ["fulcra-collect", "service", "install"],
                    capture_output=True, text=True, timeout=30,
                )
                p1_out = (p1.stdout + p1.stderr).strip()
                if p1.returncode != 0:
                    output = (
                        f"ERROR: Step 1 (install) failed with exit code {p1.returncode}."
                        + (f"\n{p1_out}" if p1_out else "")
                    )
                else:
                    p2 = subprocess.run(
                        ["fulcra-collect", "service", "start"],
                        capture_output=True, text=True, timeout=30,
                    )
                    p2_out = (p2.stdout + p2.stderr).strip()
                    if p2.returncode != 0:
                        output = (
                            f"ERROR: Step 2 (start) failed with exit code {p2.returncode}."
                            " Daemon installed but not running; check Console.app log."
                            + (f"\n{p2_out}" if p2_out else "")
                        )
                    else:
                        combined = "\n".join(filter(None, [p1_out, p2_out]))
                        output = combined or "Daemon installed and started."
            except Exception as exc:
                output = f"{type(exc).__name__}: {exc}"
            # Update label on main thread.
            from AppKit import NSOperationQueue  # type: ignore[import-not-found]
            def main():
                log.setStringValue_(output[:400])
            NSOperationQueue.mainQueue().addOperationWithBlock_(main)

        threading.Thread(target=work, daemon=True).start()

    _ButtonTarget.attach(button, on_click)
    return view


class _ButtonTarget:
    _retain: list = []

    @classmethod
    def attach(cls, button, callable_):
        class _T(NSObject):
            def call_(self, sender):
                callable_(sender)
        target = _T.alloc().init()
        button.setTarget_(target)
        button.setAction_("call:")
        cls._retain.append(target)
