"""Cooperative, local per-profile daemon exclusion until process exit.

The lock inode is persistent. Never delete it to recover a daemon, and never
infer holder health from it. Legacy daemons and standalone workers are unleased.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import stat


class DaemonAlreadyRunning(RuntimeError):
    """Another cooperating serve owns this profile (health is unknown)."""


class DaemonLeaseUnavailable(RuntimeError):
    """Profile ownership could not be established; startup must refuse."""


@dataclass(frozen=True)
class DaemonLease:
    # Raw integer deliberately has no close/__del__/context-manager: even an
    # early serve unwind or discarded Daemon must retain ownership until exit.
    fd: int


def acquire_daemon_lease(profile: Path) -> DaemonLease:
    """Acquire a private persistent inode, nonblocking, without any takeover.

    ``profile`` is the effective path already selected by config.profile_path;
    do not resolve it again or repair existing paths. Failed contenders close
    all descriptors. A successful descriptor is non-inheritable across exec.
    """
    directory_fd = None
    lease_fd = None
    try:
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_fd = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        directory = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) != 0o700):
            raise DaemonLeaseUnavailable("daemon startup refused: profile lease unavailable")
        lease_fd = os.open("daemon.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                           | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
        os.set_inheritable(lease_fd, False)
        opened = os.fstat(lease_fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600):
            raise DaemonLeaseUnavailable("daemon startup refused: profile lease unavailable")
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise DaemonAlreadyRunning("daemon startup refused: profile lease busy") from None
            raise
        named = os.stat("daemon.lock", dir_fd=directory_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise DaemonLeaseUnavailable("daemon startup refused: profile lease unavailable")
        result = DaemonLease(lease_fd)
        # Retire ownership BEFORE closing: an error can mean the numeric fd
        # was already closed/reused. Never retry that uncertain descriptor.
        retired_directory_fd, directory_fd = directory_fd, None
        os.close(retired_directory_fd)
        lease_fd = None
        return result
    except (OSError, ValueError, NotImplementedError):
        raise DaemonLeaseUnavailable("daemon startup refused: profile lease unavailable") from None
    finally:
        # Each remaining owned fd gets exactly one independent close attempt.
        # A cleanup error overrides even busy with the static unavailable
        # contract; its private detail must not escape this boundary.
        cleanup_failed = False
        for retired_fd in (lease_fd, directory_fd):
            if retired_fd is not None:
                try:
                    os.close(retired_fd)
                except OSError:
                    cleanup_failed = True
        if cleanup_failed:
            raise DaemonLeaseUnavailable("daemon startup refused: profile lease unavailable") from None
