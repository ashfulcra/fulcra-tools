"""Pluggable OKF frontmatter parsing and emission.

The flat backend (stdlib only) handles the scalar / scalar-list subset and raises
FrontmatterError on structure it cannot represent (nested maps, anchors). Task 3 adds
a PyYAML backend for full fidelity.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import OKFError

KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
SCALAR_TYPES = (str, int, float, bool)


class FrontmatterError(OKFError, ValueError):
    """Raised when a frontmatter block cannot be parsed or emitted."""


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter, body)`` for markdown text. Flat backend (Task 2)."""
    if not text.startswith("---\n"):
        return {}, text
    close = text.find("\n---\n", 4)
    if close == -1:
        raise FrontmatterError("frontmatter is missing closing ---")
    raw = text[4:close]
    body = text[close + len("\n---\n"):]
    return _flat_parse(raw), body


def dump(mapping: dict[str, Any]) -> str:
    """Emit a frontmatter block body (without fences). Flat backend (Task 2)."""
    return _flat_dump(mapping)


def _flat_parse(raw: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip():
            continue
        if line.startswith((" ", "\t", "- ")):
            raise FrontmatterError("frontmatter must be flat key/value data")
        if ":" not in line:
            raise FrontmatterError(f"invalid frontmatter line: {line}")
        key, value_text = line.split(":", 1)
        key = key.strip()
        _validate_key(key)
        value_text = value_text.strip()
        if value_text == "":
            values: list[Any] = []
            while i < len(lines) and lines[i].startswith("- "):
                values.append(_parse_scalar(lines[i][2:].strip()))
                i += 1
            out[key] = values
        else:
            out[key] = _parse_scalar_or_inline_list(value_text)
        _validate_value(out[key], key=key)
    return out


def _parse_scalar_or_inline_list(text: str) -> Any:
    if text.startswith("["):
        if not text.endswith("]"):
            raise FrontmatterError("inline list is missing closing ]")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise FrontmatterError(f"invalid inline list: {e}") from e
    return _parse_scalar(text)


def _parse_scalar(text: str) -> Any:
    if text == "":
        return ""
    if text in ("true", "false"):
        return text == "true"
    if text == "null":
        raise FrontmatterError("null frontmatter values are not supported by the flat backend")
    if text.startswith("'"):
        if not text.endswith("'") or len(text) == 1:
            raise FrontmatterError("invalid quoted scalar: missing closing '")
        return text[1:-1].replace("''", "'")
    if text.startswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise FrontmatterError(f"invalid quoted scalar: {e}") from e
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _flat_dump(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in data:  # insertion order; new keys appended by caller
        value = data[key]
        _validate_key(key)
        _validate_value(value, key=key)
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"- {_format_scalar(item)}")
        else:
            lines.append(f"{key}: {_format_scalar(value)}")
    return "\n".join(lines) + "\n" if lines else ""


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value) if _needs_quoted_string(value) else value
    raise FrontmatterError(f"unsupported scalar type: {type(value).__name__}")


def _needs_quoted_string(value: str) -> bool:
    if value == "" or value.strip() != value or any(c in value for c in "\n:#[]{}"):
        return True
    if value[:1] in ("'", '"'):
        return True
    if value in ("true", "false", "null"):
        return True
    try:
        int(value)
        return True
    except ValueError:
        pass
    try:
        float(value)
        return True
    except ValueError:
        return False


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise FrontmatterError(f"invalid frontmatter key: {key!r}")


def _validate_value(value: Any, *, key: str) -> None:
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, SCALAR_TYPES) or item is None:
                raise FrontmatterError(f"{key} list contains unsupported value")
        return
    if not isinstance(value, SCALAR_TYPES) or value is None:
        raise FrontmatterError(f"{key} has unsupported frontmatter value")
