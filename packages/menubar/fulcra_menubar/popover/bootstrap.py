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

# Module-level registry of in-flight install subprocesses. The app's _quit
# handler calls cancel_pending() to terminate them before exit — otherwise
# Python's daemon-thread teardown kills the Python wrapper but not the OS
# process beneath, leaving a partial install orphaned under launchd.
_pending_procs: list[subprocess.Popen] = []


def _run_step(cmd: list[str]) -> tuple[int, str]:
    """Run a subprocess command; track the Popen on _pending_procs so
    app quit can terminate it. Returns (returncode, combined output)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    _pending_procs.append(proc)
    try:
        out, _ = proc.communicate(timeout=30)
        return proc.returncode, out.strip()
    finally:
        try:
            _pending_procs.remove(proc)
        except ValueError:
            pass


def cancel_pending() -> None:
    """Terminate any in-flight install subprocesses. Called by the
    app's _quit handler before rumps.quit_application()."""
    for proc in list(_pending_procs):
        try:
            proc.terminate()
        except Exception:
            pass


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
                rc1, p1_out = _run_step(["fulcra-collect", "service", "install"])
                if rc1 != 0:
                    output = (
                        f"ERROR: Step 1 (install) failed with exit code {rc1}."
                        + (f"\n{p1_out}" if p1_out else "")
                    )
                else:
                    rc2, p2_out = _run_step(["fulcra-collect", "service", "start"])
                    if rc2 != 0:
                        output = (
                            f"ERROR: Step 2 (start) failed with exit code {rc2}."
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
