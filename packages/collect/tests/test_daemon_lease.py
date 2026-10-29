"""Profile-local lifetime leases, using only owned temporary files/children."""
from __future__ import annotations

import errno
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


def _lease():
    from fulcra_collect import daemon_lease
    return daemon_lease


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(home))
    return home


def _close(lease):
    # No production release API: only tests may retire their own synthetic fd.
    os.close(lease.fd)


def test_same_profile_refuses_without_truncating_stale_bytes(profile):
    lock = profile / "daemon.lock"
    lock.write_bytes(b'{"pid":999999999,"stale":true}\n')
    lock.chmod(0o600)
    before = lock.stat().st_ino
    lease = _lease().acquire_daemon_lease(profile)
    try:
        with pytest.raises(_lease().DaemonAlreadyRunning):
            _lease().acquire_daemon_lease(profile)
        assert lock.read_bytes() == b'{"pid":999999999,"stale":true}\n'
        assert lock.stat().st_ino == before
        assert not os.get_inheritable(lease.fd)
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    finally:
        _close(lease)
    again = _lease().acquire_daemon_lease(profile)
    try:
        assert lock.stat().st_ino == before
    finally:
        _close(again)


def test_separate_profiles_can_hold_leases(profile, tmp_path):
    other = tmp_path / "other"
    first = _lease().acquire_daemon_lease(profile)
    try:
        second = _lease().acquire_daemon_lease(other)
        try:
            assert stat.S_IMODE(other.stat().st_mode) == 0o700
            assert os.fstat(first.fd).st_ino != os.fstat(second.fd).st_ino
        finally:
            _close(second)
    finally:
        _close(first)


@pytest.mark.parametrize("bad", ["symlink", "hardlink", "directory", "fifo", "mode", "owner"])
def test_unsafe_lock_refuses_without_repair(profile, monkeypatch, bad):
    mod = _lease()
    lock = profile / "daemon.lock"
    target = profile / "target"
    target.write_bytes(b"preserve")
    target.chmod(0o600)
    if bad == "symlink":
        lock.symlink_to(target)
    elif bad == "hardlink":
        os.link(target, lock)
    elif bad == "directory":
        lock.mkdir()
    elif bad == "fifo":
        os.mkfifo(lock, 0o600)
    else:
        lock.write_bytes(b"preserve")
        lock.chmod(0o644 if bad == "mode" else 0o600)
        if bad == "owner":
            real_fstat = mod.os.fstat
            def foreign_owner(fd):
                value = real_fstat(fd)
                if stat.S_ISREG(value.st_mode):
                    fields = list(value)
                    fields[4] = os.getuid() + 1
                    return os.stat_result(fields)
                return value
            monkeypatch.setattr(mod.os, "fstat", foreign_owner)
    before = lock.lstat()
    with pytest.raises(mod.DaemonLeaseUnavailable):
        mod.acquire_daemon_lease(profile)
    after = lock.lstat()
    assert (after.st_ino, after.st_mode, after.st_nlink) == (before.st_ino, before.st_mode, before.st_nlink)
    assert target.read_bytes() == b"preserve"


@pytest.mark.parametrize("bad", ["symlink", "mode", "regular"])
def test_unsafe_profile_refuses_without_repair(profile, tmp_path, bad):
    candidate = tmp_path / "bad"
    if bad == "symlink":
        candidate.symlink_to(profile)
    elif bad == "regular":
        candidate.write_bytes(b"preserve")
    else:
        candidate.mkdir(mode=0o755)
        candidate.chmod(0o755)
    before = candidate.lstat()
    with pytest.raises(_lease().DaemonLeaseUnavailable):
        _lease().acquire_daemon_lease(candidate)
    assert candidate.lstat() == before
    assert not (profile / "daemon.lock").exists()


@pytest.mark.parametrize("failure", ["open", "fstat", "flock", "flock-eagain", "flock-eacces", "inheritable", "stat"])
def test_failure_closes_every_opened_descriptor(profile, monkeypatch, failure):
    mod = _lease()
    opened = []
    real_open, real_fstat = os.open, os.fstat
    def tracking_open(path, flags, *args, **kwargs):
        if failure == "open" and path == "daemon.lock":
            raise PermissionError(errno.EACCES, "private path")
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd
    monkeypatch.setattr(mod.os, "open", tracking_open)
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "private path must not escape")
    if failure.startswith("flock"):
        def fail_flock(*args):
            code = {"flock": errno.EIO, "flock-eagain": errno.EAGAIN,
                    "flock-eacces": errno.EACCES}[failure]
            raise OSError(code, "private path must not escape")
        monkeypatch.setattr(mod.fcntl, "flock", fail_flock)
    elif failure == "stat":
        real_stat = os.stat
        def fail_lock_stat(path, *args, **kwargs):
            if path == "daemon.lock" and "dir_fd" in kwargs:
                return fail()
            return real_stat(path, *args, **kwargs)
        monkeypatch.setattr(mod.os, "stat", fail_lock_stat)
    elif failure != "open":
        monkeypatch.setattr(mod.os, {"fstat": "fstat", "inheritable": "set_inheritable"}[failure], fail)
    expected = mod.DaemonAlreadyRunning if failure in ("flock-eagain", "flock-eacces") else mod.DaemonLeaseUnavailable
    with pytest.raises(expected) as exc:
        mod.acquire_daemon_lease(profile)
    assert "private path" not in str(exc.value)
    assert opened
    for fd in opened:
        with pytest.raises(OSError) as closed:
            real_fstat(fd)
        assert closed.value.errno == errno.EBADF


def _holder(profile, pipe):
    # Spawn imports this test module (stdlib/pytest only); no app/native imports.
    os.environ["FULCRA_COLLECT_HOME"] = profile
    os.environ["FULCRA_ACCESS_TOKEN"] = "synthetic-test-token"
    lease = _lease().acquire_daemon_lease(Path(profile))
    pipe.send(("held", os.fstat(lease.fd).st_ino, os.get_inheritable(lease.fd)))
    if not pipe.poll(8):
        raise TimeoutError("owned holder not stopped")
    pipe.recv()
    # Deliberately no release: kernel owns lifetime through actual process exit.


def _start_holder(profile):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_holder, args=(str(profile), child))
    try:
        proc.start()
        child.close()
        assert parent.poll(8), "owned holder failed to become ready"
        assert parent.recv()[0] == "held"
        return proc, parent
    except BaseException:
        child.close()
        _reap(proc, parent)
        raise


def _reap(proc, pipe):
    if proc.is_alive():
        proc.terminate()
    proc.join(5)
    if proc.is_alive():
        proc.kill()
        proc.join(5)
    assert not proc.is_alive()
    pipe.close()
    proc.close()


@pytest.mark.parametrize("crash", [False, True], ids=["normal-exit", "abrupt-death"])
def test_child_lifetime_and_kernel_release(profile, crash):
    proc, pipe = _start_holder(profile)
    inode = (profile / "daemon.lock").stat().st_ino
    try:
        with pytest.raises(_lease().DaemonAlreadyRunning):
            _lease().acquire_daemon_lease(profile)
        if crash:
            proc.kill()
        else:
            pipe.send("exit")
        proc.join(5)
        assert not proc.is_alive()
        again = _lease().acquire_daemon_lease(profile)
        try:
            assert (profile / "daemon.lock").stat().st_ino == inode
        finally:
            _close(again)
    finally:
        _reap(proc, pipe)


def test_exec_child_does_not_keep_lease_after_holder_exit(profile):
    # Exact synthetic Python exec child, parent-owned and always reaped. Holder
    # acquires then execs a sleeper; a leaked fd would keep this profile busy.
    script = """
import os, sys
from pathlib import Path
from fulcra_collect.daemon_lease import acquire_daemon_lease
lease = acquire_daemon_lease(Path(sys.argv[1]))
print('held', flush=True)
os.execv(sys.executable, [sys.executable, '-c', "import sys; print('exec-ready', flush=True); sys.stdin.readline()"])
"""
    proc = subprocess.Popen([sys.executable, "-c", script, str(profile)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0, env={**os.environ, "FULCRA_COLLECT_HOME": str(profile),
                                            "FULCRA_ACCESS_TOKEN": "synthetic-test-token"})
    try:
        # select bounds both startup handshakes; text buffering avoided by one
        # line per separate process image (the exec occurs after first flush).
        import select
        assert select.select([proc.stdout], [], [], 8)[0]
        assert proc.stdout.readline() == b"held\n"
        assert select.select([proc.stdout], [], [], 8)[0]
        assert proc.stdout.readline() == b"exec-ready\n"
        assert proc.poll() is None
        again = _lease().acquire_daemon_lease(profile)
        try:
            assert os.fstat(again.fd).st_ino == (profile / "daemon.lock").stat().st_ino
        finally:
            _close(again)
        proc.communicate(b"exit\n", timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


def test_garbage_collection_does_not_release_lifetime_lease(profile):
    import gc
    lease = _lease().acquire_daemon_lease(profile)
    fd = lease.fd
    del lease
    gc.collect()
    try:
        with pytest.raises(_lease().DaemonAlreadyRunning):
            _lease().acquire_daemon_lease(profile)
    finally:
        os.close(fd)


def test_busy_failure_closes_contender_fd(profile, monkeypatch):
    mod = _lease()
    held = mod.acquire_daemon_lease(profile)
    opened = []
    real_open = os.open
    def track(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd
    monkeypatch.setattr(mod.os, "open", track)
    try:
        with pytest.raises(mod.DaemonAlreadyRunning):
            mod.acquire_daemon_lease(profile)
        assert len(opened) == 2
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert os.fstat(held.fd).st_ino == (profile / "daemon.lock").stat().st_ino
    finally:
        _close(held)


def test_exec_descendant_does_not_hold_lease_when_holder_exits(profile):
    """Even close_fds=False must not pass the lease to an exec descendant.

    The immediate holder is waited/reaped. Its exact synthetic descendant has
    a four-second self-deadline; a completion line + pipe EOF observes its exit
    after release (the OS reaps the orphan). This is not orphan-worker proof.
    """
    child_script = """
import sys, time
from pathlib import Path
release = Path(sys.argv[1])
print('exec-ready', flush=True)
deadline = time.monotonic() + 4
while not release.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
print('exec-finished', flush=True)
"""
    holder_script = """
import os, socket, subprocess, sys
from pathlib import Path
os.environ['FULCRA_COLLECT_HOME'] = sys.argv[1]
os.environ['FULCRA_ACCESS_TOKEN'] = 'synthetic-test-token'
def forbidden(*args, **kwargs):
    raise AssertionError('external child dependency reached')
os.system = forbidden
socket.socket.connect = forbidden
subprocess.run = forbidden
from fulcra_collect.daemon_lease import acquire_daemon_lease
lease = acquire_daemon_lease(Path(sys.argv[1]))
print('held', flush=True)
# Only this literal Python child is permitted; close_fds=False tests CLOEXEC.
subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[3]],
                 stdin=subprocess.DEVNULL, close_fds=False)
"""
    release = profile / "release-child"
    proc = subprocess.Popen([sys.executable, "-c", holder_script, str(profile),
                             child_script, str(release)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0,
                            env={**os.environ, "FULCRA_COLLECT_HOME": str(profile),
                                 "FULCRA_ACCESS_TOKEN": "synthetic-test-token"})
    try:
        import select
        for expected in (b"held\n", b"exec-ready\n"):
            assert select.select([proc.stdout], [], [], 3)[0]
            assert proc.stdout.readline() == expected
        proc.wait(timeout=3)
        assert proc.returncode == 0
        # Holder exited, but exec child is still waiting and keeps stdout open.
        assert not select.select([proc.stdout], [], [], 0)[0]
        again = _lease().acquire_daemon_lease(profile)
        _close(again)
        release.touch()
        out, err = proc.communicate(timeout=5)
        assert out == b"exec-finished\n" and err == b""
    finally:
        release.touch()
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


@pytest.mark.parametrize("path", ["busy", "unavailable", "success-directory"])
@pytest.mark.parametrize("timing", ["before-close", "real-close-then-raise"])
@pytest.mark.parametrize("targets", ["directory", "both"])
def test_close_errors_are_typed_without_retrying_retired_fds(profile, monkeypatch, path, timing, targets):
    """A close error must not leak detail, skip other fds, or close a reused fd."""
    mod = _lease()
    holder = mod.acquire_daemon_lease(profile) if path == "busy" else None
    real_open, real_close, real_fstat = os.open, os.close, os.fstat
    opened = {}
    calls = []
    replacements = []
    error = None
    returned = None
    unrelated = profile / "unrelated-owned-file"

    def tracking_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        opened[fd] = "lease" if name == "daemon.lock" else "directory"
        return fd

    def bad_close(fd):
        kind = opened[fd]
        calls.append((fd, kind))
        if calls.count((fd, kind)) > 1:
            return real_close(fd)  # Expose a blind retry against reused fd.
        if targets == "both" or kind == "directory":
            if timing == "real-close-then-raise":
                real_close(fd)
                replacement = real_open(unrelated, os.O_RDWR | os.O_CREAT, 0o600)
                replacements.append(replacement)
                assert replacement == fd, "fixture did not reuse the retired numeric fd"
            raise OSError(errno.EIO, "sensitive synthetic close detail")
        return real_close(fd)

    def unavailable(*args):
        raise OSError(errno.EIO, "synthetic acquisition failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(mod.os, "open", tracking_open)
            patch.setattr(mod.os, "close", bad_close)
            if path == "unavailable":
                patch.setattr(mod.fcntl, "flock", unavailable)
            try:
                returned = mod.acquire_daemon_lease(profile)
            except BaseException as exc:
                error = exc
        observed = {
            "typed": type(error).__name__,
            "static": str(error) == "daemon startup refused: profile lease unavailable",
            "directory_attempts": sum(kind == "directory" for _, kind in calls),
            "lease_attempts": sum(kind == "lease" for _, kind in calls),
            "reused_fds_preserved": all(_fd_is_open(fd, real_fstat) for fd in replacements),
        }
        assert observed == {
            "typed": "DaemonLeaseUnavailable", "static": True,
            "directory_attempts": 1, "lease_attempts": 1,
            "reused_fds_preserved": True,
        }
        if holder is not None:
            assert real_fstat(holder.fd).st_ino == (profile / "daemon.lock").stat().st_ino
    finally:
        # A failed close can leave its outcome unknown. These are only our test
        # fds; after restoring the real syscall, retire anything still open.
        for fd in set(opened) | set(replacements):
            if _fd_is_open(fd, real_fstat):
                real_close(fd)
        if returned is not None and returned.fd not in opened:
            real_close(returned.fd)
        if holder is not None:
            real_close(holder.fd)


def _fd_is_open(fd, fstat):
    try:
        fstat(fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF
        return False
    return True
