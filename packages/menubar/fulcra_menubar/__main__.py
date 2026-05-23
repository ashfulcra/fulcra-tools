"""Entry point for `python -m fulcra_menubar` and the `fulcra-menubar`
script. Builds and runs the rumps app."""
from __future__ import annotations

import sys


def main() -> int:
    if sys.platform != "darwin":
        print("Fulcra Collect menubar runs only on macOS.", file=sys.stderr)
        return 1
    from .app import FulcraMenubarApp  # local import — keeps PyObjC out of test imports
    FulcraMenubarApp().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
