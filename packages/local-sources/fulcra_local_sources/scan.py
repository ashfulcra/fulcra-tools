"""Bounded, read-only traversal for user-selected local folders."""
from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


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


@dataclass(frozen=True)
class ScannedFile:
    path: Path = field(repr=False)
    relative_path: Path
    size: int
    digest: str


@dataclass(frozen=True)
class ScanResult:
    files: tuple[ScannedFile, ...]
    skipped_by_reason: dict[str, int]
    total_bytes: int


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _likely_secret(path: Path) -> bool:
    low = path.name.casefold()
    return low in _SECRET_NAMES or path.suffix.casefold() in _SECRET_SUFFIXES


def scan_tree(
    source: Path,
    *,
    allowed_extensions: set[str],
    limits: ScanLimits | None = None,
) -> ScanResult:
    """Return safe files below ``source`` without following links.

    Paths exposed in the result are relative. The absolute path is retained only
    on each file object for the local reader and is excluded from its repr.
    """
    limits = limits or ScanLimits()
    root = Path(source).expanduser().resolve(strict=False)
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

    candidates: list[Path] = []
    skipped: Counter[str] = Counter()
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for name in sorted(dirnames, key=str.casefold):
            child = current_path / name
            if name.startswith(".") or name in _HIDDEN_DIRS:
                continue
            if child.is_symlink():
                skipped["symlink"] += 1
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames, key=str.casefold):
            path = current_path / name
            if path.is_symlink():
                skipped["symlink"] += 1
            elif name.startswith("."):
                skipped["hidden"] += 1
            elif _likely_secret(path):
                skipped["likely-secret"] += 1
            elif path.suffix.casefold() not in extensions:
                skipped["unsupported"] += 1
            else:
                candidates.append(path)

    candidates.sort(key=lambda value: value.relative_to(root).as_posix().casefold())
    if len(candidates) > limits.max_files:
        raise ValueError(
            f"selected folder exceeds the file limit ({limits.max_files})"
        )

    files: list[ScannedFile] = []
    total_bytes = 0
    for path in candidates:
        try:
            size = path.stat().st_size
        except OSError:
            skipped["unreadable"] += 1
            continue
        if size > limits.max_file_bytes:
            skipped["oversized"] += 1
            continue
        total_bytes += size
        if total_bytes > limits.max_total_bytes:
            raise ValueError(
                f"selected folder exceeds the total byte limit ({limits.max_total_bytes})"
            )
        try:
            digest = _digest(path)
        except OSError:
            skipped["unreadable"] += 1
            total_bytes -= size
            continue
        files.append(
            ScannedFile(
                path=path,
                relative_path=path.relative_to(root),
                size=size,
                digest=digest,
            )
        )
    return ScanResult(
        files=tuple(files),
        skipped_by_reason=dict(sorted(skipped.items())),
        total_bytes=total_bytes,
    )
