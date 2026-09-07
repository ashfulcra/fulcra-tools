"""Render an Apple note as a vault markdown file.

Two constraints shape the output:

* The vault may already belong to the user and to other agents. Everything
  this plugin generates therefore lives inside ONE owner-fenced section, so
  a human (or another agent) can add prose above or below it and have that
  survive every later sync.
* v1 syncs one way, but the state a two-way sync needs has to be recorded
  from the very first write or existing notes can never be reconciled
  later. So each file carries the note's UUID, its Apple mod date, and a
  content hash of what we last wrote.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import re
import unicodedata

OWNER = "fulcra-collect/apple-notes"
SECTION_SLUG = "apple-note"
OPEN_FENCE = f"<!-- section:{SECTION_SLUG} owner:{OWNER} -->"
CLOSE_FENCE = f"<!-- /section:{SECTION_SLUG} -->"

NOTES_DIR = "notes/apple"
ATTACHMENTS_DIR = f"{NOTES_DIR}/_attachments"

_SLUG_STRIP = re.compile(r"[^a-zA-Z0-9]+")
_ATTACHMENT_TOKEN = re.compile(r"\{\{attachment:([^}]+)\}\}")

# Obsidian renders these inline; anything else is linked rather than embedded.
_EMBEDDABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".pdf"}


def content_hash(markdown_body: str) -> str:
    """Hash of the body we wrote, for change detection in both directions."""
    return hashlib.sha256(markdown_body.encode("utf-8")).hexdigest()[:16]


def slugify(title: str, *, fallback: str = "untitled") -> str:
    normalized = unicodedata.normalize("NFKD", title or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:60] or fallback


def note_filename(uuid: str, title: str) -> str:
    """A stable, human-readable filename.

    The UUID suffix is what makes it stable: two notes can share a title,
    and a note's title can change. The vault spec forbids renaming without
    link repair, so once a file exists under a name we keep that name and
    only update the frontmatter title -- the suffix guarantees we never need
    to rename for uniqueness.
    """
    return f"{NOTES_DIR}/{slugify(title)}-{uuid[:8].lower()}.md"


def attachment_path(media_uuid: str, filename: str) -> str:
    safe = _SLUG_STRIP.sub("-", filename or "").strip("-") or "file"
    # Keep the real extension: Obsidian embeds by extension.
    dot = (filename or "").rfind(".")
    ext = (filename or "")[dot:].lower() if dot > 0 else ""
    if ext and not safe.lower().endswith(ext.replace(".", "-")):
        stem = safe
    else:
        stem = safe[: len(safe) - len(ext.replace(".", "-"))] or "file"
    return f"{ATTACHMENTS_DIR}/{media_uuid}/{stem}{ext}"


def _fm_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text == "" or re.search(r'[:#\[\]{}",\n]', text) or text.strip() != text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        lines.append(f"{key}: {_fm_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def resolve_attachments(markdown: str, links: dict[str, str]) -> str:
    """Replace {{attachment:<id>}} tokens with vault embeds or links.

    A token whose attachment has no backing file (inline tables, drawings)
    degrades to a visible marker rather than vanishing, so the note never
    silently loses the fact that something was there.
    """
    def repl(match: re.Match) -> str:
        target = links.get(match.group(1))
        if not target:
            return "*(unsupported attachment)*"
        ext = target[target.rfind("."):].lower() if "." in target else ""
        name = target.rsplit("/", 1)[-1]
        if ext in _EMBEDDABLE:
            return f"![{name}]({_encode(target)})"
        return f"[{name}]({_encode(target)})"
    return _ATTACHMENT_TOKEN.sub(repl, markdown)


def _encode(path: str) -> str:
    return path.replace(" ", "%20")


def render_note(*, uuid: str, title: str, folder: str, body_markdown: str,
                modified: datetime | None, created: datetime | None,
                deleted: bool, synced_at: datetime,
                attachment_links: dict[str, str],
                missing_attachments: int = 0) -> str:
    """Build the complete markdown file for one Apple note."""
    body = resolve_attachments(body_markdown, attachment_links)
    fields = {
        "title": title or "Untitled",
        "source": "apple-notes",
        "apple-uuid": uuid,
        "apple-folder": folder or "",
        "apple-modified": modified.isoformat() if modified else None,
        "apple-created": created.isoformat() if created else None,
        "apple-hash": content_hash(body),
        "apple-deleted": deleted,
        "synced-at": synced_at.isoformat(),
        "updated-by": OWNER,
    }
    parts = [frontmatter(fields), ""]
    if deleted:
        parts.append(
            "> [!warning] This note was deleted in Apple Notes.\n"
            "> The copy below is the last version this sync saw. It is kept "
            "deliberately -- the sync marks deletions, it never deletes.\n")
    parts.append(OPEN_FENCE)
    parts.append(body if body.strip() else "*(empty note)*")
    parts.append(CLOSE_FENCE)
    parts.append("")
    if missing_attachments:
        parts.append(
            f"> {missing_attachments} attachment(s) on this note have no "
            "backing file in Apple Notes (inline tables, links or drawings) "
            "and could not be exported.\n")
    parts.append("## Log")
    parts.append(f"- {synced_at.date().isoformat()} synced from Apple Notes by {OWNER}")
    parts.append("")
    return "\n".join(parts)
