"""Bounded, read-only traversal for user-selected local folders."""
from __future__ import annotations

import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from .source_io import hash_file, identity, open_regular, open_root


_HIDDEN_DIRS = {".git", ".hg", ".svn", ".obsidian", "__pycache__", "node_modules"}
_SECRET_NAMES = {
    ".env",
    ".env.local",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
_SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".kdbx"}


@dataclass(frozen=True)
class ScanLimits:
    max_files: int = 5_000
    max_file_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_entries: int = 100_000


@dataclass(frozen=True)
class ScannedFile:
    path: Path = field(repr=False)
    relative_path: Path
    size: int
    digest: str
    root: Path = field(repr=False)
    root_identity: tuple[int, int] = field(repr=False)
    identity: tuple[int, ...] = field(repr=False)


@dataclass(frozen=True)
class ScanResult:
    files: tuple[ScannedFile, ...]
    skipped_by_reason: dict[str, int]
    total_bytes: int


def _likely_secret(path: Path) -> bool:
    low = path.name.casefold()
    stem = path.stem.casefold()
    return (low in _SECRET_NAMES or path.suffix.casefold() in _SECRET_SUFFIXES
            or bool(re.match(r"^(credentials?|secrets?|tokens?|service[-_]account|client[-_]secret|"
                             r"(?:access|refresh|oauth)[-_]token)"
                             r"(?:$|[-_.])", stem)))


def _scan_tree(
    source: Path,
    *,
    allowed_extensions: set[str],
    limits: ScanLimits | None = None,
    still_allowed=None,
) -> ScanResult:
    """Return safe files below ``source`` without following links.

    Paths exposed in the result are relative. The absolute path is retained only
    on each file object for the local reader and is excluded from its repr.
    """
    limits = limits or ScanLimits()
    if still_allowed is not None:
        still_allowed()
    root = Path(os.path.abspath(Path(source).expanduser()))
    if root == Path("/"):
        raise ValueError("select a folder below the filesystem root")
    if not root.is_dir():
        raise ValueError("source must be an existing directory")
    extensions = {
        value.casefold() if value.startswith(".") else f".{value.casefold()}"
        for value in allowed_extensions
    }
    if not extensions:
        raise ValueError("at least one allowed extension is required")

    skipped: Counter[str] = Counter()
    files: list[ScannedFile] = []
    total_bytes = 0
    entries = eligible = 0
    with open_root(root) as root_fd:
        root_info = os.fstat(root_fd)
        root_identity = (root_info.st_dev, root_info.st_ino)
        for current, dirnames, filenames, directory_fd in os.fwalk(
                ".", dir_fd=root_fd, follow_symlinks=False):
            if still_allowed is not None:
                still_allowed()
            entries += len(dirnames) + len(filenames)
            if entries > limits.max_entries:
                raise ValueError("selected folder exceeds the traversal entry limit")
            kept_dirs = []
            for name in sorted(dirnames, key=str.casefold):
                if name.startswith(".") or name in _HIDDEN_DIRS or _likely_secret(Path(name)):
                    continue
                if stat.S_ISLNK(os.stat(name, dir_fd=directory_fd,
                                       follow_symlinks=False).st_mode):
                    skipped["symlink"] += 1
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames, key=str.casefold):
                relative = Path(current) / name
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    skipped["symlink"] += 1
                elif not stat.S_ISREG(info.st_mode):
                    skipped["non-regular"] += 1
                elif name.startswith("."):
                    skipped["hidden"] += 1
                elif _likely_secret(relative):
                    skipped["likely-secret"] += 1
                elif relative.suffix.casefold() not in extensions:
                    skipped["unsupported"] += 1
                elif info.st_size > limits.max_file_bytes:
                    skipped["oversized"] += 1
                else:
                    if still_allowed is not None:
                        still_allowed()
                    eligible += 1
                    if eligible > limits.max_files:
                        raise ValueError(f"selected folder exceeds the file limit ({limits.max_files})")
                    try:
                        with open_regular(root_fd, relative) as handle:
                            before = identity(os.fstat(handle.fileno()))
                            digest, size = hash_file(handle, limits.max_file_bytes,
                                                     still_allowed=still_allowed)
                            if before != identity(os.fstat(handle.fileno())) or size != before[2]:
                                raise ValueError("source changed during scan; try again")
                    except OSError:
                        skipped["unreadable"] += 1
                        continue
                    total_bytes += size
                    if total_bytes > limits.max_total_bytes:
                        raise ValueError("selected folder exceeds the total byte limit "
                                         f"({limits.max_total_bytes})")
                    files.append(ScannedFile(path=root / relative, relative_path=relative,
                        size=size, digest=digest, root=root,
                        root_identity=root_identity, identity=before))
    files.sort(key=lambda item: item.relative_path.as_posix().casefold())
    return ScanResult(
        files=tuple(files),
        skipped_by_reason=dict(sorted(skipped.items())),
        total_bytes=total_bytes,
    )


def scan_tree(source: Path, **kwargs) -> ScanResult:
    try:
        return _scan_tree(source, **kwargs)
    except OSError:
        raise ValueError("source unavailable or source changed; select the folder again") from None
