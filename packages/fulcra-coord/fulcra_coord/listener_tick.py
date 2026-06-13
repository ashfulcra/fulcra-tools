"""Above-core listener tick orchestration.

The durable scheduler's cheap primitive remains ``notify-inbox``. Review-relay
automation needs one extra production-side bridge, ``forge-mirror``, but core
modules are forbidden from importing that bridge. This wrapper sits above core
so an installed listener can opt into:

    forge-mirror once -> notify-inbox

without letting forge polling creep into ``inbox.py`` or ``listener.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from . import forge_mirror as _forge_mirror
from . import inbox as _inbox


def cmd_listener_tick(args: Any, backend: Optional[list[str]] = None) -> int:
    """Run one scheduled listener tick.

    ``--forge-mirror`` is best-effort and never prevents the inbox poll. The
    mirror only appends marked evidence; it never closes review loops.
    """
    if getattr(args, "forge_mirror", False):
        mirror_args = SimpleNamespace(
            once=True,
            repo=getattr(args, "repo", None),
            format=getattr(args, "format", "table"),
        )
        try:
            _forge_mirror.cmd_forge_mirror(mirror_args, backend=backend)
        except Exception:
            pass
    return _inbox.cmd_notify_inbox(args, backend=backend)
