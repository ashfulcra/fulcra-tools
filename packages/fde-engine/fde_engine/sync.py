"""Local-mirror sync — explicit direction, git-style, no guessing.

Bidirectional auto-merge needs a base version to detect conflicts; without one
an 'automatic' sync silently clobbers somebody. So the engine only offers
explicit ``push`` (local wins) and ``pull`` (remote wins), each transferring
only files whose content actually differs. The file store's version history is
the recovery path if a direction was chosen wrongly.

Engagement trees are small (dozens of files), so change detection is a full
content compare — dead simple beats clever here.
"""

from __future__ import annotations

import os
from typing import Any

from .engagement import remote_path


def _local_files(local_dir: str) -> dict[str, str]:
    """rel-path -> content for every non-hidden file under local_dir."""
    out: dict[str, str] = {}
    for root, dirs, files in os.walk(local_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, local_dir).replace(os.sep, "/")
            with open(full, encoding="utf-8") as fh:
                out[rel] = fh.read()
    return out


def _remote_files(transport, slug: str) -> dict[str, str]:
    """rel-path -> content for every file under the engagement's remote tree."""
    out: dict[str, str] = {}
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        prefix = remote_path(slug, rel_dir).rstrip("/") + "/"
        for entry in transport.list_dir(prefix):
            name = entry["name"]
            rel = f"{rel_dir}{name}" if rel_dir else name
            if entry.get("is_dir"):
                pending.append(rel if rel.endswith("/") else rel + "/")
                continue
            content = transport.read(remote_path(slug, rel))
            if content is not None:
                out[rel] = content
    return out


def push(transport, slug: str, local_dir: str) -> dict[str, Any]:
    """Upload local files whose content differs from remote. Local wins."""
    pushed, skipped = [], 0
    for rel, content in sorted(_local_files(local_dir).items()):
        if transport.read(remote_path(slug, rel)) == content:
            skipped += 1
            continue
        transport.write(remote_path(slug, rel), content)
        pushed.append(rel)
    return {"pushed": pushed, "skipped": skipped}


def pull(transport, slug: str, local_dir: str) -> dict[str, Any]:
    """Download remote files whose content differs from local. Remote wins."""
    pulled, skipped = [], 0
    for rel, content in sorted(_remote_files(transport, slug).items()):
        full = os.path.join(local_dir, rel.replace("/", os.sep))
        try:
            with open(full, encoding="utf-8") as fh:
                if fh.read() == content:
                    skipped += 1
                    continue
        except OSError:
            pass  # missing locally -> pull it
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        pulled.append(rel)
    return {"pulled": pulled, "skipped": skipped}
