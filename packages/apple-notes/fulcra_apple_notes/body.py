"""Decode an Apple Notes body blob into markdown.

Wire shape used by the decoder:

    NoteStoreProto { Document document = 2 }
    Document       { int32 version = 2; Note note = 3 }
    Note           { string note_text = 2; repeated AttributeRun runs = 5 }
    AttributeRun   { int32 length = 1; ParagraphStyle style = 2;
                     string link = 9; AttachmentInfo attachment = 12 }
    ParagraphStyle { int32 style_type = 1; int32 indent = 4; Checklist cl = 5 }
    Checklist      { bytes uuid = 1; int32 done = 2 }
    AttachmentInfo { string attachment_identifier = 1; string type_uti = 2 }

Attribute runs are CHARACTER RANGES over note_text, not per-paragraph
records, so paragraph style has to be recovered by walking cumulative run
lengths and asking which run covers each paragraph's first character.
Attachments appear in the text as U+FFFC (OBJECT REPLACEMENT CHARACTER);
the run covering that character carries the attachment's identifier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import zlib

from . import protobuf as pb

OBJECT_REPLACEMENT = "￼"

# ParagraphStyle.style_type values -> markdown prefix for the paragraph.
STYLE_TITLE = 0
STYLE_HEADING = 1
STYLE_SUBHEADING = 2
STYLE_MONOSPACED = 4
STYLE_DOTTED = 100
STYLE_DASHED = 101
STYLE_NUMBERED = 102
STYLE_CHECKBOX = 103

_BLOCK_PREFIX = {
    STYLE_TITLE: "# ",
    STYLE_HEADING: "## ",
    STYLE_SUBHEADING: "### ",
    STYLE_DOTTED: "- ",
    STYLE_DASHED: "- ",
    STYLE_NUMBERED: "1. ",
}


class BodyDecodeError(ValueError):
    """Raised when a body blob cannot be decoded at all."""


@dataclass
class Run:
    length: int
    style_type: int | None = None
    indent: int = 0
    checked: bool | None = None
    link: str = ""
    attachment_id: str = ""
    type_uti: str = ""


@dataclass
class DecodedBody:
    text: str = ""
    markdown: str = ""
    runs: list[Run] = field(default_factory=list)
    attachment_ids: list[str] = field(default_factory=list)
    """Attachment identifiers in the order they appear in the body."""


def decompress(blob: bytes) -> bytes:
    """Unwrap the gzip container around a note body.

    Not every row is gzipped (short/legacy rows can be stored raw), so a
    non-gzip blob is returned untouched rather than treated as an error.
    """
    if len(blob) >= 2 and blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob)
        except (OSError, EOFError, zlib.error) as exc:
            raise BodyDecodeError(f"gzip decompress failed: {exc}") from exc
    return blob


def _parse_runs(note_msg: dict) -> list[Run]:
    runs: list[Run] = []
    for raw in note_msg.get(5, []):
        if not isinstance(raw, (bytes, bytearray)):
            continue
        try:
            run_msg = pb.parse(bytes(raw))
        except pb.ProtobufError:
            continue
        run = Run(length=int(pb.first(run_msg, 1, 0) or 0))
        style = pb.submessage(run_msg, 2)
        if style is not None:
            st = pb.first(style, 1)
            run.style_type = int(st) if isinstance(st, int) else None
            run.indent = int(pb.first(style, 4, 0) or 0)
            checklist = pb.submessage(style, 5)
            if checklist is not None:
                run.checked = bool(pb.first(checklist, 2, 0))
        run.link = pb.text(run_msg, 9)
        attachment = pb.submessage(run_msg, 12)
        if attachment is not None:
            run.attachment_id = pb.text(attachment, 1)
            run.type_uti = pb.text(attachment, 2)
        runs.append(run)
    return runs


def decode(blob: bytes) -> DecodedBody:
    """Decode a ZICNOTEDATA.ZDATA blob into text, runs, and markdown."""
    raw = decompress(blob)
    if not raw:
        return DecodedBody()
    try:
        store = pb.parse(raw)
    except pb.ProtobufError as exc:
        raise BodyDecodeError(f"not protobuf: {exc}") from exc
    document = pb.submessage(store, 2)
    if document is None:
        raise BodyDecodeError("no Document (field 2) in NoteStoreProto")
    note = pb.submessage(document, 3)
    if note is None:
        raise BodyDecodeError("no Note (field 3) in Document")

    text = pb.text(note, 2)
    runs = _parse_runs(note)
    decoded = DecodedBody(text=text, runs=runs)
    decoded.markdown, decoded.attachment_ids = _to_markdown(text, runs)
    return decoded


def _run_at(runs: list[Run], offset: int) -> Run | None:
    """The run covering character `offset`, walking cumulative lengths."""
    cursor = 0
    for run in runs:
        if offset < cursor + run.length:
            return run
        cursor += run.length
    return None


def _attachments_in(runs: list[Run], start: int, end: int) -> list[str]:
    """Attachment ids on runs overlapping the character range [start, end)."""
    found: list[str] = []
    cursor = 0
    for run in runs:
        run_end = cursor + run.length
        if run.attachment_id and cursor < end and run_end > start:
            found.append(run.attachment_id)
        cursor = run_end
    return found


def _to_markdown(text: str, runs: list[Run]) -> tuple[str, list[str]]:
    """Render note text plus paragraph styles as markdown.

    Inline character formatting (bold, italic, colour) is deliberately not
    rendered in v1: it lives on the same runs, but interleaving it with
    block structure needs range-splitting that would risk mangling the text,
    and the text itself is what the user actually needs first.
    """
    out_lines: list[str] = []
    ordered_attachments: list[str] = []
    offset = 0
    numbered_counter = 0
    for line in text.split("\n"):
        line_start = offset
        line_end = offset + len(line)
        offset = line_end + 1  # +1 for the newline we split on

        run = _run_at(runs, line_start)
        style = run.style_type if run else None

        # Attachment placeholders: replace each U+FFFC with a link to the
        # asset we exported, in the order the runs report them.
        if OBJECT_REPLACEMENT in line:
            ids = _attachments_in(runs, line_start, max(line_end, line_start + 1))
            rebuilt = []
            idx = 0
            for ch in line:
                if ch == OBJECT_REPLACEMENT:
                    aid = ids[idx] if idx < len(ids) else ""
                    idx += 1
                    if aid:
                        ordered_attachments.append(aid)
                        rebuilt.append(f"{{{{attachment:{aid}}}}}")
                    else:
                        rebuilt.append("*(attachment)*")
                else:
                    rebuilt.append(ch)
            line = "".join(rebuilt)

        if style == STYLE_NUMBERED:
            numbered_counter += 1
        else:
            numbered_counter = 0

        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue

        indent = "    " * (run.indent if run else 0)
        if style == STYLE_CHECKBOX:
            box = "[x]" if (run and run.checked) else "[ ]"
            out_lines.append(f"{indent}- {box} {stripped}")
        elif style == STYLE_NUMBERED:
            out_lines.append(f"{indent}{numbered_counter}. {stripped}")
        elif style == STYLE_MONOSPACED:
            out_lines.append(f"{indent}    {line.rstrip()}")
        elif style in _BLOCK_PREFIX:
            out_lines.append(f"{indent}{_BLOCK_PREFIX[style]}{stripped}")
        else:
            out_lines.append(f"{indent}{stripped}" if indent else stripped)

    return "\n".join(out_lines).strip(), ordered_attachments
