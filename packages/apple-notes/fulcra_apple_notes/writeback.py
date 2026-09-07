"""Push a vault-side edit back into Apple Notes.

Route: AppleScript. Writing to NoteStore.sqlite directly is not an option --
the store is a Core Data database backed by CloudKit sync, so a foreign
write would fight the sync engine, miss the protobuf body encoding, and
risk corrupting a database whose only copy is the user's.

UNVERIFIED, and treated as true until proven otherwise: setting a note's
body via AppleScript replaces the note's entire content, which is expected
to drop any attachments it holds. This has NOT been confirmed on a real
note, because doing so requires TCC Automation consent that was not
available. Until it is verified, writeback REFUSES any note with
attachments (``allow_attachment_loss`` overrides, deliberately verbose).

Everything here is dry-run by default. Bulk body replacement can lose content and cannot be automatically undone.
"""
from __future__ import annotations

from dataclasses import dataclass
import html
import re
import subprocess

APPLESCRIPT_TIMEOUT_S = 20


class WritebackError(RuntimeError):
    """Writeback failed."""


class AutomationDenied(WritebackError):
    """macOS did not grant Automation access to Notes.

    Distinct from a generic failure: nothing retries its way out of this,
    a human has to approve the prompt on the machine itself.
    """


@dataclass
class WriteResult:
    uuid: str
    ok: bool
    skipped: str = ""
    error: str = ""


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_CHECK_RE = re.compile(r"^(\s*)-\s+\[([ xX])\]\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)-\s+(.*)$")
_NUMBER_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")


def markdown_to_html(markdown: str) -> str:
    """Convert our rendered markdown back to the HTML Notes accepts.

    Deliberately small: it handles exactly the block constructs this
    plugin emits. Anything richer risks silently restructuring the user's
    note, and a lossy round trip through a general converter would be
    worse than a plain one.
    """
    out: list[str] = []
    in_list = False
    for line in (markdown or "").split("\n"):
        # Images and links became vault paths on the way out; they are
        # meaningless inside Apple Notes, so keep the visible text only.
        line = _LINK_RE.sub(lambda m: m.group(1) or m.group(2), line)

        heading = _HEADING_RE.match(line)
        check = _CHECK_RE.match(line)
        bullet = _BULLET_RE.match(line)
        number = _NUMBER_RE.match(line)

        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<br>")
            continue
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{html.escape(heading.group(2))}</h{level}>")
            continue
        if check:
            if not in_list:
                out.append("<ul>")
                in_list = True
            mark = "☑︎ " if check.group(2).lower() == "x" else "☐ "
            out.append(f"<li>{mark}{html.escape(check.group(3))}</li>")
            continue
        if bullet or number:
            if not in_list:
                out.append("<ul>")
                in_list = True
            text = (bullet or number).group(2 if bullet else 3)
            out.append(f"<li>{html.escape(text)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<div>{html.escape(line)}</div>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _applescript(script: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True,
            timeout=APPLESCRIPT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        # A hung AppleEvent is what an unanswered consent prompt looks like
        # from here: the dialog is on the machine's screen, waiting.
        raise AutomationDenied(
            "AppleScript to Notes timed out. This is what an unanswered "
            "Automation consent prompt looks like: approve it in System "
            "Settings > Privacy & Security > Automation, then retry."
        ) from exc
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        low = stderr.lower()
        if "-1743" in stderr or "not allowed" in low or "not authorized" in low:
            raise AutomationDenied(
                "macOS denied Automation access to Notes. Approve it in "
                "System Settings > Privacy & Security > Automation.")
        if "-1712" in stderr or "timed out" in low:
            raise AutomationDenied(
                "AppleEvent timed out talking to Notes — an Automation "
                "consent prompt is most likely waiting on the machine.")
        raise WritebackError(stderr[-400:] or "osascript failed")
    return (result.stdout or "").strip()


def probe() -> tuple[bool, str]:
    """Can we drive Notes at all? Returns (available, explanation)."""
    try:
        count = _applescript(
            'with timeout of 15 seconds\n'
            'tell application "Notes" to return count of notes\n'
            'end timeout')
    except AutomationDenied as exc:
        return False, str(exc)
    except WritebackError as exc:
        return False, f"Notes scripting failed: {exc}"
    return True, f"Notes is scriptable ({count} notes visible)"


def write_note_body(pk: int, markdown: str, *, dry_run: bool = True) -> WriteResult:
    """Replace one Apple note's body, addressed by Core Data primary key."""
    body_html = markdown_to_html(markdown)
    if dry_run:
        return WriteResult(uuid=str(pk), ok=True, skipped="dry-run")
    escaped = body_html.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'with timeout of 15 seconds\n'
        'tell application "Notes"\n'
        f'  set theNotes to (every note whose id ends with "/ICNote/p{pk}")\n'
        '  if (count of theNotes) is 0 then error "note not found" number 8001\n'
        f'  set body of item 1 of theNotes to "{escaped}"\n'
        '  return "ok"\n'
        'end tell\n'
        'end timeout')
    try:
        _applescript(script)
    except WritebackError as exc:
        return WriteResult(uuid=str(pk), ok=False, error=str(exc))
    return WriteResult(uuid=str(pk), ok=True)


def plan_writeback(changes, *, notes_by_uuid, attachments_by_note,
                   allow_attachment_loss: bool = False):
    """Decide which vault edits may be pushed back, and refuse the rest.

    Refusals are the point of this function. A note is skipped when it
    holds attachments (a body replacement is expected to drop them), when
    both sides moved (a conflict is not ours to resolve), or when the note
    is no longer in the store.
    """
    allowed, refused = [], []
    for change in changes:
        note = notes_by_uuid.get(change.uuid)
        reason = ""
        if change.status.value != "vault_edited":
            reason = f"status is {change.status.value}, not a vault edit"
        elif note is None:
            reason = "note is no longer in the Apple Notes store"
        elif attachments_by_note.get(change.uuid) and not allow_attachment_loss:
            n = len(attachments_by_note[change.uuid])
            reason = (f"note has {n} attachment(s); replacing its body is "
                      "expected to drop them (unverified — pass "
                      "allow_attachment_loss to override)")
        if reason:
            refused.append((change, reason))
        else:
            allowed.append((change, note))
    return allowed, refused
