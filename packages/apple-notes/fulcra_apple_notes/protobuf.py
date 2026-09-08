"""Minimal protobuf wire-format reader (stdlib only).

Apple Notes stores each note body as gzip-wrapped protobuf. We need four
fields out of that structure, not a full protobuf runtime, and taking a
`protobuf` dependency into the collect daemon for that would be a poor
trade. So this module decodes the wire format directly.

The wire format is self-describing enough to parse without a schema: every
field carries a number and a wire type. Unknown fields are preserved as raw
values rather than being an error, which is what keeps this robust across
the macOS releases that keep adding fields to these messages.
"""
from __future__ import annotations

from typing import Any, Iterator

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


class ProtobufError(ValueError):
    """Raised when a buffer is not decodable as protobuf wire format."""


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(buf):
            raise ProtobufError(f"truncated varint at offset {start}")
        if shift > 63:
            raise ProtobufError(f"varint too long at offset {start}")
        byte = buf[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not byte & 0x80:
            return result, pos
        shift += 7


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield (field_number, wire_type, value) for each field in `buf`.

    Length-delimited values come back as bytes; varints and fixed-width
    values as ints. The caller decides how to interpret them, because the
    wire format cannot distinguish a nested message from a string.
    """
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = _read_varint(buf, pos)
        field_number = key >> 3
        wire_type = key & 0x07
        if field_number == 0:
            raise ProtobufError("field number 0 is not valid")
        if wire_type == WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == WIRE_LEN:
            length, pos = _read_varint(buf, pos)
            if length < 0 or pos + length > end:
                raise ProtobufError(f"length-delimited field {field_number} overruns buffer")
            value = buf[pos:pos + length]
            pos += length
        elif wire_type == WIRE_64BIT:
            if pos + 8 > end:
                raise ProtobufError(f"truncated 64-bit field {field_number}")
            value = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wire_type == WIRE_32BIT:
            if pos + 4 > end:
                raise ProtobufError(f"truncated 32-bit field {field_number}")
            value = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        else:
            raise ProtobufError(f"unsupported wire type {wire_type} for field {field_number}")
        yield field_number, wire_type, value


def parse(buf: bytes) -> dict[int, list[Any]]:
    """Decode one message into {field_number: [values...]}.

    Every field maps to a LIST even when the schema says it is singular:
    protobuf allows a repeated wire encoding for any field, and last-wins
    guessing would silently drop data.
    """
    out: dict[int, list[Any]] = {}
    for number, _wire, value in iter_fields(buf):
        out.setdefault(number, []).append(value)
    return out


def first(msg: dict[int, list[Any]], number: int, default: Any = None) -> Any:
    """Return the first value of a field, or `default` when absent."""
    values = msg.get(number)
    if not values:
        return default
    return values[0]


def submessage(msg: dict[int, list[Any]], number: int) -> dict[int, list[Any]] | None:
    """Parse field `number` as a nested message, or None if absent/undecodable."""
    raw = first(msg, number)
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        return parse(bytes(raw))
    except ProtobufError:
        return None


def text(msg: dict[int, list[Any]], number: int, default: str = "") -> str:
    """Decode field `number` as UTF-8 text.

    Uses errors="replace": a body that is 99% readable with one bad byte is
    far more useful to the user than an exception that drops the whole note.
    """
    raw = first(msg, number)
    if not isinstance(raw, (bytes, bytearray)):
        return default
    return bytes(raw).decode("utf-8", errors="replace")
