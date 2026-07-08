"""Local-mirror sync — explicit direction, git-style, no guessing.

Bidirectional auto-merge needs a base version to detect conflicts; without one
an 'automatic' sync silently clobbers somebody. So the engine only offers
explicit ``push`` (local wins) and ``pull`` (remote wins), each transferring
only files whose content actually differs. The file store's version history is
the recovery path if a direction was chosen wrongly.

Engagement trees are small (dozens of files), so change detection is a full
content compare — dead simple beats clever here.

Known v1 limits:

- Symlinked directories under the local mirror are not followed (``os.walk``
  default), so their contents are silently excluded from a push.
- If a transport read returns None mid-pull, that file is excluded from the
  pass. The transport contract can't distinguish "missing" from a transient
  read error, so a blip means the file is skipped this round rather than
  failing loudly; a re-run picks it up once the read succeeds.
"""

from __future__ import annotations

import os
from typing import Any

from .engagement import remote_path


class SyncError(RuntimeError):
    pass


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


# engagement.md is exclusively machine-managed via `fde-engine phase` (see
# engagement.py). A push must never touch it: the local mirror is just a
# working copy, and if it's stale (last pulled before another session
# advanced the phase), pushing its copy of engagement.md would silently
# revert phase/history that session wrote remotely. Pull may still bring a
# fresh copy down — only the upload direction is blocked.
_MACHINE_MANAGED = "engagement.md"


def _validate_rel(rel: str) -> None:
    """The remote listing is treated as untrusted input here: a traversal
    segment or absolute path in a listed entry name must not let a pull
    write outside local_dir onto the caller's filesystem."""
    if os.path.isabs(rel) or any(seg == ".." for seg in rel.split("/")):
        raise SyncError(
            f"pull rejected remote entry {rel!r} — path escapes the local mirror"
        )


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
            _validate_rel(rel)
            if entry.get("is_dir"):
                pending.append(rel if rel.endswith("/") else rel + "/")
                continue
            content = transport.read(remote_path(slug, rel))
            if content is not None:
                out[rel] = content
    return out


def push(transport, slug: str, local_dir: str) -> dict[str, Any]:
    """Upload local files whose content differs from remote. Local wins,
    except engagement.md, which is never uploaded (see _MACHINE_MANAGED)."""
    if not os.path.isdir(local_dir):
        raise SyncError(
            f"local dir {local_dir} does not exist — nothing to push "
            f"(wrong --dir or CWD?)"
        )
    pushed, skipped, excluded = [], 0, []
    for rel, content in sorted(_local_files(local_dir).items()):
        if rel == _MACHINE_MANAGED:
            excluded.append(rel)
            continue
        if transport.read(remote_path(slug, rel)) == content:
            skipped += 1
            continue
        if not transport.write(remote_path(slug, rel), content):
            # A partial push is not rolled back — files already uploaded are
            # good; the error names where it stopped so a re-run can finish.
            raise SyncError(
                f"push failed at {rel} — {len(pushed)} file(s) already pushed; "
                f"re-run `fde-engine sync <slug> push` after fixing the cause"
            )
        pushed.append(rel)
    return {"pushed": pushed, "skipped": skipped, "excluded": excluded}


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
        try:
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            # Typical case: remote has build/logs/x.md but local `build` is a
            # plain file (IsADirectoryError/NotADirectoryError). Surface an
            # actionable message instead of a raw traceback.
            raise SyncError(
                f"pull failed writing {rel}: {exc} — a local file/directory "
                f"is in the way; move it aside and re-run pull"
            ) from exc
        pulled.append(rel)
    return {"pulled": pulled, "skipped": skipped}
