"""Entry point for `python -m fulcra_menubar` and the `fulcra-menubar`
script. Builds and runs the rumps app."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _configure_bundle_tls() -> None:
    # The embedded Python support package may have no OpenSSL default CA
    # paths. urllib (used by the SDK's Auth0 flow) does not choose certifi
    # automatically, unlike httpx. Keep verification enabled and supply the
    # shipped Mozilla roots before importing any CLI/worker or GUI code.
    # Explicit operator trust overrides remain authoritative, including a
    # directory-only store; do not silently expand that configured trust set.
    if "SSL_CERT_FILE" in os.environ or "SSL_CERT_DIR" in os.environ:
        return
    import certifi
    roots = certifi.where()
    if not Path(roots).is_file():
        raise RuntimeError("Bundled certificate roots are missing; reinstall Collect.")
    os.environ["SSL_CERT_FILE"] = roots


def main() -> int:
    if sys.argv[1:] == ["--keychain-read"]:
        from fulcra_collect.macos_keychain import child_main
        return child_main()
    # Briefcase's executable is an app launcher, not a Python interpreter.
    # Signed sibling executables route CLI and worker calls through this entry.
    app_bin = Path(sys.executable).parent
    if app_bin.name == "MacOS" and app_bin.parent.name == "Contents":
        os.environ["PATH"] = str(app_bin) + os.pathsep + os.environ.get("PATH", "")
        _configure_bundle_tls()
    if len(sys.argv) > 1 and sys.argv[1] == "--notes-snapshot":
        from fulcra_apple_notes._snapshot import main as snapshot_main
        snapshot_main(sys.argv[2:])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--reminders-bridge":
        from fulcra_apple_reminders._bridge import main as reminders_main
        return reminders_main()
    if len(sys.argv) > 1 and sys.argv[1] in ("--collect", "--fulcra"):
        if sys.argv[1] == "--collect":
            from fulcra_collect.cli import cli
            name = "fulcra-collect"
        else:
            from fulcra_api.cli import cli
            name = "fulcra"
        cli(args=sys.argv[2:], prog_name=name)
        return 0
    if sys.platform != "darwin":
        print("Fulcra Collect menubar runs only on macOS.", file=sys.stderr)
        return 1
    from .app import FulcraMenubarApp  # local import — keeps PyObjC out of test imports
    FulcraMenubarApp().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
