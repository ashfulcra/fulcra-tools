"""Drives periodic status polls.

The schedule has two regimes — 2s while the popover is open (the user
wants live feedback) and 10s while it is closed (just enough to keep
the menubar icon honest and to fire failure notifications). The whole
thing suspends while the machine is asleep; on wake, the next tick
fires immediately so an overdue plugin shows up in seconds.

This is a pure-logic module — `monotonic` and `sleep` are injected so
the tests can use a fake clock. In production, `time.monotonic` and
`time.sleep` are passed in.
"""
from __future__ import annotations

import threading
import time as _time
from collections.abc import Callable

INTERVAL_OPEN_S = 2.0
INTERVAL_CLOSED_S = 10.0


class PollingScheduler:
    def __init__(
        self,
        *,
        on_tick: Callable[[], None],
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._on_tick = on_tick
        self._monotonic = monotonic or _time.monotonic
        self._sleep = sleep or _time.sleep
        self._popover_open = False
        self._stop = False
        self._suspended = False
        self._woke_from_suspend = False
        self._suspended_cond = threading.Condition()
        self._post_tick_hooks: list[Callable[[], None]] = []

    def set_popover_open(self, open_: bool) -> None:
        self._popover_open = open_

    def add_post_tick_hook(self, hook: Callable[[], None]) -> None:
        self._post_tick_hooks.append(hook)

    def suspend(self) -> None:
        with self._suspended_cond:
            self._suspended = True

    def resume(self) -> None:
        with self._suspended_cond:
            if self._suspended:
                self._woke_from_suspend = True
            self._suspended = False
            self._suspended_cond.notify_all()

    def stop(self) -> None:
        self._stop = True
        self.resume()

    def run(self) -> None:
        while not self._stop:
            self._tick()
            if self._stop:
                break
            self._sleep_for_interval()

    def _tick(self) -> None:
        try:
            self._on_tick()
        finally:
            for hook in self._post_tick_hooks:
                hook()

    def _interval(self) -> float:
        return INTERVAL_OPEN_S if self._popover_open else INTERVAL_CLOSED_S

    def _sleep_for_interval(self) -> None:
        with self._suspended_cond:
            while self._suspended:
                self._suspended_cond.wait()
            if self._stop:
                return
            if self._woke_from_suspend:
                # Machine was asleep; fire the next tick immediately
                # so the user sees a fresh status the moment the lid opens.
                self._woke_from_suspend = False
                return
        self._sleep(self._interval())
