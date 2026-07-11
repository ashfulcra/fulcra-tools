"""Gmail ``messages.get(format=full)`` payload → selected-email JSON.

The converter turns one raw Gmail message envelope into a deterministic,
JSON-serializable dict — the artifact that lands in the operator's Fulcra
Files. It is pure and side-effect-free: no network, no logging of content.

What it extracts:

* a fixed **header subset** — ``From``, ``To``, ``Cc``, ``Subject``,
  ``Date``, ``Message-ID`` — RFC 2047-decoded (encoded-words like
  ``=?utf-8?B?…?=`` become plain Unicode);
* the ``text/plain`` and ``text/html`` **bodies**, base64url-decoded, walking
  arbitrarily nested multipart (``multipart/mixed`` wrapping
  ``multipart/alternative`` etc.);
* **attachments = metadata only** — ``filename``, ``mimeType``, ``size``,
  ``attachmentId``. NO bytes: attachment content is deferred to v2, so nothing
  large or sensitive is ever inlined here.

The header/part helpers are shared with :mod:`fulcra_gmail.rules` (the local
post-filters need the same decoded header values and attachment detection).
"""
from __future__ import annotations

import base64
from collections.abc import Iterator
from email.header import decode_header, make_header

#: The only headers copied into the selected-email JSON (canonical names).
HEADER_SUBSET = ("From", "To", "Cc", "Subject", "Date", "Message-ID")


def decode_rfc2047(value: str) -> str:
    """Decode any RFC 2047 encoded-words in a header value to plain Unicode.

    A bare ASCII value passes through unchanged. Malformed encoded-words fall
    back to the raw string rather than raising.
    """
    if not value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return value


def _headers_map(payload: dict) -> dict[str, str]:
    """Case-insensitive ``header-name → RFC2047-decoded value`` for a payload.

    On duplicate header names the FIRST occurrence wins (Gmail preserves order;
    the first is the outermost/primary value).
    """
    out: dict[str, str] = {}
    for entry in payload.get("headers", []) or []:
        name = entry.get("name", "")
        key = name.lower()
        if key in out:
            continue
        out[key] = decode_rfc2047(entry.get("value", "") or "")
    return out


def get_header(payload: dict, name: str) -> str | None:
    """Return one decoded header value (case-insensitive), or ``None``."""
    return _headers_map(payload).get(name.lower())


def _b64url_decode(data: str) -> bytes:
    """Decode Gmail's URL-safe base64 part body, tolerating stripped padding."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def walk_parts(payload: dict) -> Iterator[dict]:
    """Yield ``payload`` and every descendant part, depth-first."""
    yield payload
    for part in payload.get("parts", []) or []:
        yield from walk_parts(part)


def _is_attachment(part: dict) -> bool:
    """A part is an attachment iff it has a non-empty filename or an
    ``attachmentId`` (bytes fetched separately)."""
    if part.get("filename"):
        return True
    return bool((part.get("body") or {}).get("attachmentId"))


def has_attachment(payload: dict) -> bool:
    """True iff any part in the tree is an attachment."""
    return any(_is_attachment(part) for part in walk_parts(payload))


def _decode_part_text(part: dict) -> str:
    body = part.get("body") or {}
    data = body.get("data")
    if not data:
        return ""
    try:
        return _b64url_decode(data).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _collect_bodies(payload: dict) -> dict[str, str]:
    """Accumulate ``text/plain`` and ``text/html`` bodies across the tree.

    Multiple same-type parts are concatenated in document order (rare, but the
    contiguous shape stays deterministic). Attachment parts are skipped even if
    they carry a text mime type.
    """
    bodies: dict[str, str] = {}
    for part in walk_parts(payload):
        mime = part.get("mimeType", "")
        if mime not in ("text/plain", "text/html"):
            continue
        if _is_attachment(part):
            continue
        text = _decode_part_text(part)
        if not text:
            continue
        bodies[mime] = bodies.get(mime, "") + text
    return bodies


def _collect_attachments(payload: dict) -> list[dict]:
    out: list[dict] = []
    for part in walk_parts(payload):
        if not _is_attachment(part):
            continue
        body = part.get("body") or {}
        out.append({
            "filename": part.get("filename", ""),
            "mimeType": part.get("mimeType", ""),
            "size": body.get("size", 0),
            "attachmentId": body.get("attachmentId"),
        })
    return out


def to_selected_email(message: dict) -> dict:
    """Convert a ``messages.get(full)`` envelope to selected-email JSON.

    Deterministic dict shape::

        {
          "message_id": <gmail id>,
          "thread_id":  <gmail threadId | None>,
          "headers":    {subset of From/To/Cc/Subject/Date/Message-ID present},
          "bodies":     {"text/plain": ..., "text/html": ...},  # present only
          "attachments": [{"filename","mimeType","size","attachmentId"}, ...],
        }
    """
    payload = message.get("payload") or {}
    headers = _headers_map(payload)
    header_subset = {
        canonical: headers[canonical.lower()]
        for canonical in HEADER_SUBSET
        if canonical.lower() in headers
    }
    return {
        "message_id": message.get("id"),
        "thread_id": message.get("threadId"),
        "headers": header_subset,
        "bodies": _collect_bodies(payload),
        "attachments": _collect_attachments(payload),
    }
