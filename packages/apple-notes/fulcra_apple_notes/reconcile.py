"""Two-way reconciliation between Apple Notes and the vault.

Detection is separated from writeback on purpose. Deciding *what* changed
needs no special permission and is fully testable; pushing an edit back
into Apple Notes needs TCC Automation consent and can destroy data if done
carelessly. Keeping them apart means the risky half can be refused,
dry-run, or deferred without losing the useful half.

Three inputs per note, and all three are required to classify correctly:

  apple   -- the note as it exists in the store right now
  vault   -- the note file as it exists in the vault right now
  state   -- what the last sync wrote (hash + Apple mod date)

Comparing only two of them cannot tell an edit from a stale copy: if the
vault differs from Apple, that alone does not say WHICH side moved.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from . import render

_FENCE_RE = re.compile(
    r"<!--\s*section:" + re.escape(render.SECTION_SLUG) +
    r"\s+owner:[^>]+?-->\n(?P<body>.*?)\n<!--\s*/section:"
    + re.escape(render.SECTION_SLUG) + r"\s*-->",
    re.DOTALL)
_FM_RE = re.compile(r"\A---\n(?P<fm>.*?)\n---\n", re.DOTALL)


class Status(str, Enum):
    UNCHANGED = "unchanged"
    APPLE_CHANGED = "apple_changed"      # normal one-way case
    VAULT_EDITED = "vault_edited"        # writeback candidate
    CONFLICT = "conflict"                # both sides moved
    NEW_IN_APPLE = "new_in_apple"
    MISSING_IN_VAULT = "missing_in_vault"
    DELETED_IN_APPLE = "deleted_in_apple"


@dataclass
class Change:
    uuid: str
    status: Status
    path: str = ""
    title: str = ""
    detail: str = ""
    has_attachments: bool = False


def parse_vault_note(markdown: str) -> tuple[dict, str | None]:
    """Return (frontmatter fields, fenced body) for a vault note.

    A body of None means the owner fence is absent -- the note was
    restructured by hand, and must never be silently overwritten.
    """
    fields: dict = {}
    fm = _FM_RE.search(markdown or "")
    if fm:
        for line in fm.group("fm").split("\n"):
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            fields[key.strip()] = value
    match = _FENCE_RE.search(markdown or "")
    return fields, (match.group("body") if match else None)


def classify(*, uuid: str, apple_hash: str | None, apple_modified: str | None,
             vault_text: str | None, state_entry: dict | None,
             assume_vault_unchanged: bool = False) -> Change:
    """Decide what happened to one note since the last sync.

    ``assume_vault_unchanged`` requires an independently proven content
    baseline, such as an unchanged server version. A listing timestamp is
    insufficient. Production reconciliation reads each body and does not
    enable this shortcut; otherwise an absent body means a missing file.
    """
    if state_entry is None:
        return Change(uuid=uuid, status=Status.NEW_IN_APPLE)

    path = state_entry.get("path", "")
    title = state_entry.get("title", "")
    synced_hash = state_entry.get("hash")
    synced_modified = state_entry.get("modified")

    if apple_hash is None:
        return Change(uuid=uuid, status=Status.DELETED_IN_APPLE,
                      path=path, title=title)
    if vault_text is None and assume_vault_unchanged:
        apple_moved = (apple_hash != synced_hash) or (apple_modified != synced_modified)
        return Change(uuid=uuid,
                      status=Status.APPLE_CHANGED if apple_moved else Status.UNCHANGED,
                      path=path, title=title)
    if vault_text is None:
        return Change(uuid=uuid, status=Status.MISSING_IN_VAULT,
                      path=path, title=title,
                      detail="the vault file is gone; Apple Notes still has it")

    _fields, body = parse_vault_note(vault_text)
    if body is None:
        # No fence: a human restructured the file. Treat as edited, never
        # as unchanged -- overwriting it would discard their restructuring.
        return Change(uuid=uuid, status=Status.VAULT_EDITED, path=path,
                      title=title,
                      detail="owner fence missing; file was restructured by hand")

    vault_hash = render.content_hash(body)
    vault_moved = vault_hash != synced_hash
    apple_moved = (apple_hash != synced_hash) or (apple_modified != synced_modified)

    if vault_moved and apple_moved:
        return Change(uuid=uuid, status=Status.CONFLICT, path=path, title=title,
                      detail="both the vault copy and the Apple note changed "
                             "since the last sync")
    if vault_moved:
        return Change(uuid=uuid, status=Status.VAULT_EDITED, path=path,
                      title=title, detail="edited in the vault")
    if apple_moved:
        return Change(uuid=uuid, status=Status.APPLE_CHANGED, path=path,
                      title=title, detail="changed in Apple Notes")
    return Change(uuid=uuid, status=Status.UNCHANGED, path=path, title=title)


def summarize(changes: list[Change]) -> dict[str, int]:
    counts = {s.value: 0 for s in Status}
    for change in changes:
        counts[change.status.value] += 1
    return counts
