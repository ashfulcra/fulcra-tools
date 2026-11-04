"""Normalize common text transcript exports into plain Markdown."""
from __future__ import annotations

import json
import re
from pathlib import Path


_TIMING = re.compile(
    r"^\s*\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3}\s+-->\s+"
    r"\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3}.*$"
)
_SAFE_YAML = re.compile(r"^[A-Za-z0-9._/-]+$")


def _frontmatter(relative_path: Path) -> str:
    relative = relative_path.as_posix()
    rendered = relative if _SAFE_YAML.fullmatch(relative) else json.dumps(relative)
    return (
        "---\n"
        "source: meeting-transcript\n"
        f"source-relative-path: {rendered}\n"
        "---\n\n"
    )


def _subtitle_text(text: str) -> str:
    lines: list[str] = []
    for raw in text.replace("\ufeff", "").splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.isdigit() or _TIMING.match(line):
            continue
        if line.startswith(("NOTE ", "STYLE", "REGION")):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return "\n\n".join(lines)


def _json_text(value: object, fallback_title: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ValueError("JSON is not a supported transcript object")
    title = value.get("title") or value.get("name") or fallback_title
    if not isinstance(title, str) or not title.strip():
        title = fallback_title
    for key in ("transcript", "text", "content"):
        body = value.get(key)
        if isinstance(body, str) and body.strip():
            return title.strip(), body.strip()
    segments = value.get("segments") or value.get("utterances")
    if not isinstance(segments, list):
        raise ValueError("JSON does not contain a supported transcript")
    paragraphs: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        body = segment.get("text") or segment.get("content")
        if not isinstance(body, str) or not body.strip():
            continue
        speaker = segment.get("speaker") or segment.get("speaker_name")
        if isinstance(speaker, str) and speaker.strip():
            paragraphs.append(f"**{speaker.strip()}:** {body.strip()}")
        else:
            paragraphs.append(body.strip())
    if not paragraphs:
        raise ValueError("JSON does not contain any transcript text")
    return title.strip(), "\n\n".join(paragraphs)


def render_transcript(path: Path, *, relative_path: Path) -> str:
    """Render one supported transcript without exposing its absolute path."""
    suffix = path.suffix.casefold()
    text = path.read_text(encoding="utf-8-sig")
    title = path.stem
    if suffix in {".vtt", ".srt"}:
        body = _subtitle_text(text)
    elif suffix == ".json":
        title, body = _json_text(json.loads(text), title)
    elif suffix in {".md", ".txt"}:
        body = text.strip()
    else:
        raise ValueError(f"unsupported transcript type: {suffix}")
    if not body.strip():
        raise ValueError("transcript is empty")
    heading = "" if suffix == ".md" and body.lstrip().startswith("#") else f"# {title}\n\n"
    return _frontmatter(relative_path) + heading + body.rstrip() + "\n"
