"""Additive, hash-based upload engine shared by local-source plugins."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Iterable

from .scan import ScannedFile


@dataclass(frozen=True)
class SyncResult:
    uploaded: int = 0
    unchanged: int = 0
    previewed: int = 0


def _state_key(item: ScannedFile) -> str:
    digest = hashlib.sha256(item.relative_path.as_posix().encode("utf-8")).hexdigest()
    return f"file:{digest}"


def _remote_path(root: str, item: ScannedFile, *, force_markdown: bool) -> str:
    if not root.startswith("/") or ".." in PurePosixPath(root).parts:
        raise ValueError("destination root must be an absolute safe path")
    relative = PurePosixPath(item.relative_path.as_posix())
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("source relative path escaped its selected folder")
    if force_markdown:
        relative = relative.with_suffix(".md")
    return str(PurePosixPath(root.rstrip("/")) / relative)


def sync_files(
    files: Iterable[ScannedFile],
    *,
    writer,
    state_get: Callable[[str, object], object],
    state_set: Callable[[str, object], None],
    destination_root: str,
    render_text: Callable[[ScannedFile], str] | None = None,
    force_markdown: bool = False,
    dry_run: bool = False,
) -> SyncResult:
    planned = [
        (item, _remote_path(destination_root, item, force_markdown=force_markdown))
        for item in files
    ]
    destinations = [remote for _item, remote in planned]
    if len(destinations) != len(set(destinations)):
        raise ValueError("two source files map to the same destination")
    uploaded = unchanged = previewed = 0
    for item, remote in planned:
        key = _state_key(item)
        prior = state_get(key, None)
        if isinstance(prior, dict) and prior.get("digest") == item.digest and prior.get("remote") == remote:
            unchanged += 1
            continue
        if dry_run:
            previewed += 1
            continue
        if render_text is None:
            writer.upload_file(item.path, remote)
        else:
            writer.write_text(remote, render_text(item))
        state_set(
            key,
            {
                "digest": item.digest,
                "relative_path": item.relative_path.as_posix(),
                "remote": remote,
            },
        )
        uploaded += 1
    return SyncResult(uploaded=uploaded, unchanged=unchanged, previewed=previewed)
