"""One retry for a transient single-doc read failure — a blip absorber.

The fail-closed UNKNOWN path is CORRECT and this module does not soften it: a
read that fails twice still returns ``error``, the cursor still stays untouched,
and nothing downstream changes. What it removes is the case measured on
2026-08-05, where codex-coder's tick reported "records config could not be read;
state=UNKNOWN" and coord-boss's host read the same document fine sixty seconds
later — a reader that collided with the VPS heartbeat's hourly rewrite. Nothing
was lost, but the wake report said catastrophe, and an operator reading it a
dozen times a day cannot tell a blip from an outage.

Deliberately NOT an availability layer. ONE retry, short backoff, and the
retry is opt-in per call site rather than wired into ``Transport.read_classified``
— a blanket retry would multiply latency inside the bulk read loops (presence
shards, task documents) exactly when the store is genuinely down, which is when
an operator most needs a fast answer.

Retry requires a CLASSIFIED read. ``Transport.read`` collapses absent and
unreadable into a single None, and retrying that would add the backoff to every
genuinely-absent document on a fresh team — paying the blip tax on the normal
path to no purpose. Where only a plain read is available the behavior is exactly
what it was before this module existed.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from .budget import Deadline
from .log import get_logger

#: Backoff before the single retry, in milliseconds. Long enough to clear a
#: writer's replace window, short enough that a genuinely-dark store still
#: answers promptly.
DEFAULT_RETRY_MS = 2000

#: Any NON-POSITIVE value disables the retry (``0`` is the documented off
#: switch; a negative delay is meaningless and disabling it beats sleeping on
#: an operator's typo). An unparseable value falls back to the default rather
#: than raising: this sits on the read path of every command, and a bad env var
#: must not be able to take the engine down.
ENV_RETRY_MS = "COORD_READ_RETRY_MS"

_log = get_logger("read-retry")


def retry_delay_ms(env: Optional[dict[str, str]] = None) -> int:
    """Configured backoff in ms; ``<= 0`` means "do not retry"."""
    src = os.environ if env is None else env
    raw = (src.get(ENV_RETRY_MS) or "").strip()
    if not raw:
        return DEFAULT_RETRY_MS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_RETRY_MS


def read_classified_retrying(
        reader: Callable[[str], tuple[Optional[str], str]],
        path: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        log: Any = None,
        deadline: Optional[Deadline] = None,
) -> tuple[Optional[str], str]:
    """``reader(path)``, retried ONCE if and only if it returns ``error``.

    Takes the bound reader rather than the transport so it composes with the
    ``getattr(transport, "read_classified", None)`` duck-type every caller
    already uses, and so no fake transport in the suite has to grow a keyword
    argument it never asked for.

    Only ``error`` is retried. ``absent`` and ``invalid`` are ANSWERS — the
    store spoke and said "no document" or handed over bytes that do not parse —
    and asking again would neither change them nor mean anything if it did.

    Exceptions propagate untouched: today's callers wrap this call in their own
    ``except Exception: return None, "error"``, and swallowing a raise here
    would move that decision away from the caller that owns it.

    When supplied, ``deadline`` owns the initial read, backoff, and retry as one
    operation.  An answer that arrives after expiry is still ``error``.
    """
    if deadline is not None and deadline.expired():
        return None, "error"
    raw, status = reader(path)
    if deadline is not None and deadline.expired():
        return None, "error"
    if status != "error":
        return raw, status
    delay_ms = retry_delay_ms()
    if delay_ms <= 0:
        return raw, status
    delay = delay_ms / 1000.0
    if deadline is not None:
        remaining = deadline.remaining()
        if remaining is not None and delay >= remaining:
            return None, "error"
    sleep(delay)
    if deadline is not None and deadline.expired():
        return None, "error"
    raw, status = reader(path)
    if deadline is not None and deadline.expired():
        return None, "error"
    if status != "error":
        # A breadcrumb, not an alarm: silence would hide a live race (the whole
        # point of item 4 of the directive is that we do not yet know whether
        # the store's replace is atomic), and a warning would reproduce exactly
        # the red text this change exists to remove.
        (log or _log).info("transient read failure rescued by retry",
                           path=path, delay_ms=delay_ms)
    return raw, status


def read_retrying(
        transport: Any,
        path: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        log: Any = None,
) -> Optional[str]:
    """Plain-read shape (``str | None``) with the retry when it can be earned.

    A transport that cannot classify its reads gets the pre-existing behavior
    verbatim — see the module docstring on why absence must not pay the backoff.
    """
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        return transport.read(path)
    raw, _status = read_classified_retrying(reader, path, sleep=sleep, log=log)
    return raw
