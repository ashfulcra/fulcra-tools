"""Short shared leases binding task consent checks to external mutations."""
from contextlib import contextmanager
import fcntl
import os
import time

from .config import config_dir


@contextmanager
def _shared_lock(path, deadline):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('configuration mutation lease unavailable') from None
                time.sleep(min(0.05, remaining))
        yield
    finally:
        os.close(fd)


@contextmanager
def task_mutation_scope(*, timeout_s=30):
    """Hold shared account/config locks through a final check and external call.

    Exclusive configuration/account transitions use these same persistent lock
    paths. Account first matches their ordering and avoids a lock inversion.
    Wait is bounded; no files are removed or config/token state modified here.
    """
    root = config_dir()
    deadline = time.monotonic() + min(max(float(timeout_s), 0), 30)
    with _shared_lock(root / '.account-transition.lock', deadline):
        with _shared_lock(root / 'config.lock', deadline):
            yield
