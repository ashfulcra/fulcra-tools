"""Short shared leases binding task consent checks to external mutations."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
import threading
import time

from .config import config_dir

_record_scopes = threading.local()


@contextmanager
def _file_lock(path, deadline, *, mode=fcntl.LOCK_SH):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
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
    with _file_lock(root / '.account-transition.lock', deadline):
        with _file_lock(root / 'config.lock', deadline):
            yield


@contextmanager
def account_refresh_scope(*, timeout_s=30):
    """Fence credential read/write against interactive sign-in, without config locks.

    Refresh may be needed inside a settings-exclusive commit; acquiring the
    settings lock again there would deadlock. Account transitions own this
    persistent account lock exclusively through their token mutation.
    """
    root = config_dir()
    deadline = time.monotonic() + min(max(float(timeout_s), 0), 30)
    with _file_lock(root / '.account-transition.lock', deadline):
        yield


@contextmanager
def settings_update_scope(*, timeout_s=30):
    """Fence one settings commit after external destination proof completes."""
    root = config_dir()
    deadline = time.monotonic() + min(max(float(timeout_s), 0), 30)
    with _file_lock(root / '.account-transition.lock', deadline):
        with _file_lock(root / 'config.lock', deadline, mode=fcntl.LOCK_EX):
            yield


@contextmanager
def record_mutation_scope(account_id: str, *, timeout_s=30):
    """Serialize record writes/deletes for one account on this host.

    Acquire task_mutation_scope first when both are needed. This lease does not
    claim atomic ordering with browsers or writers on another host.
    """
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("record mutation requires a captured account")
    key = hashlib.sha256(account_id.encode()).hexdigest()
    held = getattr(_record_scopes, "held", None)
    if held is None:
        held = _record_scopes.held = set()
    owner = os.getpid(), str(config_dir()), key
    if owner in held:
        yield
        return
    deadline = time.monotonic() + min(max(float(timeout_s), 0), 30)
    with _file_lock(config_dir() / (".record-mutation-" + key + ".lock"), deadline, mode=fcntl.LOCK_EX):
        held.add(owner)
        try:
            yield
        finally:
            held.remove(owner)
