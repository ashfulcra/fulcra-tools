"""Private propagation of the controlled compiler operation's original 300s bound.

No persisted authority, environment knob, or independent work scheduler lives here.
Outside a controlled operation the existing per-reader/per-command bounds apply.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time


class OperationDeadlineExceeded(RuntimeError):
    """The original controlled operation can no longer do or admit work."""


@dataclass(frozen=True)
class OperationDeadline:
    expires: float

    def check(self):
        if time.monotonic() >= self.expires:
            raise OperationDeadlineExceeded('UNKNOWN: controlled operation deadline')

    def remaining(self):
        self.check()
        remaining = self.expires - time.monotonic()
        if remaining <= 0:
            raise OperationDeadlineExceeded('UNKNOWN: controlled operation deadline')
        return remaining

    def clamp(self, inner_deadline):
        self.check()
        return min(self.expires, inner_deadline)


_CURRENT = ContextVar('compiler_operation_deadline', default=None)


def current_operation_deadline():
    return _CURRENT.get()


@contextmanager
def compiler_operation():
    inherited = current_operation_deadline()
    deadline = inherited or OperationDeadline(time.monotonic() + 300.0)
    deadline.check()
    token = _CURRENT.set(deadline)
    try:
        yield deadline
        deadline.check()
    finally:
        _CURRENT.reset(token)
