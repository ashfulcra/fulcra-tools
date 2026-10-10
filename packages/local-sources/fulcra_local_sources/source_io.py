"""Read exactly the regular-file bytes approved by a folder scan."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def open_root(root: Path):
    """Open every component without following a newly substituted symlink."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_regular(root_fd: int, relative: Path):
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("source relative path escaped its selected folder")
    directory = os.dup(root_fd)
    fd = None
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("source is not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            yield handle
    finally:
        os.close(directory)
        if fd is not None:
            os.close(fd)


def hash_file(handle, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("source changed or exceeds the file byte limit")
        digest.update(chunk)
    return digest.hexdigest(), size


@contextmanager
def snapshot(item):
    """Stage bounded verified bytes; consumers never reopen the source path."""
    try:
        with open_root(item.root) as root_fd:
            root_info = os.fstat(root_fd)
            if (root_info.st_dev, root_info.st_ino) != item.root_identity:
                raise ValueError("source changed after scan; rescan the selected folder")
            with open_regular(root_fd, item.relative_path) as source:
                if identity(os.fstat(source.fileno())) != item.identity:
                    raise ValueError("source changed after scan; rescan the selected folder")
                with tempfile.TemporaryDirectory(prefix="collect-source-") as directory:
                    staged = Path(directory) / item.relative_path.name
                    digest = hashlib.sha256()
                    size = 0
                    with staged.open("xb") as target:
                        staged.chmod(0o600)
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            size += len(chunk)
                            if size > item.size:
                                raise ValueError("source changed after scan")
                            digest.update(chunk)
                            target.write(chunk)
                    if (size != item.size or digest.hexdigest() != item.digest
                            or identity(os.fstat(source.fileno())) != item.identity):
                        raise ValueError("source changed after scan; rescan the selected folder")
                    yield replace(item, path=staged)
    except OSError:
        raise ValueError("source unavailable or source changed after scan") from None
