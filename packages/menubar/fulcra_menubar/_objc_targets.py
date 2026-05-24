"""Shared NSObject target proxy for routing AppKit button/switch
actions into Python callables.

PyObjC's class registry requires each NSObject subclass name to be
unique globally. Earlier copies of this helper defined a local NSObject
subclass inside an attach() method body, which worked for the first call
but collided with the ObjC runtime on the second invocation (the second
call tried to register a different class body under the same already-
registered name, raising ``objc.error: _T is overriding existing
Objective-C class``).

This module defines ONE class at module scope. The Python callable is
stored per-instance so each attach() creates a new *instance*, not a new
class. Callers retain targets on a module-level list so they are not
garbage-collected (AppKit controls hold only a weak reference to their
target).
"""
from __future__ import annotations

from collections.abc import Callable

import objc  # type: ignore[import-not-found]
from Foundation import NSObject  # type: ignore[import-not-found]


class _CallableTarget(NSObject):
    """An NSObject whose ``call_:`` selector invokes a stored Python
    callable with the AppKit sender."""

    def initWithCallable_(self, callable_):  # type: ignore[override]
        self = objc.super(_CallableTarget, self).init()
        if self is None:
            return None
        self._callable = callable_  # type: ignore[attr-defined]
        return self

    def call_(self, sender):
        self._callable(sender)


_retain: list = []  # keeps targets alive; AppKit controls hold only weak refs


def attach(
    control,
    callable_: Callable[[object], None],
    *,
    action: str = "call:",
) -> None:
    """Wire an AppKit control's target/action to a Python callable.

    The callable receives the AppKit sender as its single argument.

    Parameters
    ----------
    control:
        Any AppKit control that responds to ``setTarget_`` / ``setAction_``
        (NSButton, NSSwitch, NSTextField, …).
    callable_:
        Python callable invoked when the control fires its action.  It
        receives the AppKit sender object as its only argument.
    action:
        ObjC selector string to register (default ``"call:"``).  Pass a
        different selector only when the control fires a non-standard action
        (e.g. ``"textChanged:"`` for NSTextField delegate-style usage).
    """
    target = _CallableTarget.alloc().initWithCallable_(callable_)
    control.setTarget_(target)
    control.setAction_(action)
    _retain.append(target)
