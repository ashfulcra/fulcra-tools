"""Main-thread dispatch helper for view-layer observer callbacks.

AppKit mandates that all UI mutations (NSImage, NSStatusItem, NSPopover,
NSView, etc.) happen on the main thread. The background polling thread
drives StatusModel._notify(), which in turn calls every registered observer.
Without this helper those observers would run on the polling thread and
trigger undefined / crashing behaviour.

Usage (decorator form)::

    @on_main_thread
    def _apply(self) -> None:
        ...  # safe to touch AppKit here

Usage (inline wrap at observer registration)::

    model.add_observer(on_main_thread(lambda _m: self._apply()))

The decorator is a no-op on non-Darwin platforms so that pure-model tests
stay synchronous and never need to import AppKit.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TypeVar

_F = TypeVar("_F", bound=Callable)


def on_main_thread(fn: _F) -> _F:
    """Wrap *fn* so its body executes on the AppKit main thread.

    On non-Darwin (CI, Linux) the original function is returned unchanged
    so tests remain synchronous.
    """
    if sys.platform != "darwin":
        return fn
    try:
        from AppKit import NSOperationQueue  # type: ignore[import-not-found]
    except ImportError:
        return fn

    def wrapped(*args, **kwargs):
        NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: fn(*args, **kwargs)
        )

    # Preserve function identity attributes for easier debugging.
    wrapped.__name__ = getattr(fn, "__name__", repr(fn))
    wrapped.__qualname__ = getattr(fn, "__qualname__", repr(fn))
    return wrapped  # type: ignore[return-value]
