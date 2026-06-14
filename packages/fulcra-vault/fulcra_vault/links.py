"""Wikilink extraction, deterministic index building, and rename planning."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .schema import SchemaError, canonical_json, normalize_note_path


WIKILINK_RE = re.compile(r"\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")


def _strip_code(markdown: str) -> str:
    """Drop fenced code blocks and inline code spans so [[links]] shown as
    examples (in docs) don't become phantom backlinks / rename targets."""
    out: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append(INLINE_CODE_RE.sub(" ", line))
    return "\n".join(out)


@dataclass(frozen=True)
class RenamePlan:
    source: str
    destination: str
    rewrites: dict[str, str]
    dangling: tuple[str, ...] = ()


def extract_wikilinks(markdown: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in WIKILINK_RE.finditer(_strip_code(markdown)):
        try:
            target = normalize_note_path(match.group("target").strip())
        except SchemaError:
            continue
        if target not in seen:
            links.append(target)
            seen.add(target)
    return links


def build_index(note_map: dict[str, str]) -> dict[str, Any]:
    notes: dict[str, dict[str, list[str]]] = {}
    backlinks: dict[str, list[str]] = {}
    for raw_path in sorted(note_map):
        path = normalize_note_path(raw_path)
        links = sorted(extract_wikilinks(note_map[raw_path]))
        notes[path] = {"links": links}
        for target in links:
            backlinks.setdefault(target, []).append(path)
    return {
        "v": 1,
        "notes": notes,
        "backlinks": {k: sorted(v) for k, v in sorted(backlinks.items())},
    }


def index_json(note_map: dict[str, str]) -> str:
    return canonical_json(build_index(note_map))


def backlinks_for(index: dict[str, Any], note: str) -> list[str]:
    return list((index.get("backlinks") or {}).get(normalize_note_path(note), []))


def plan_rename(note_map: dict[str, str], source: str, destination: str) -> RenamePlan:
    source_path = normalize_note_path(source)
    dest_path = normalize_note_path(destination)
    normalized = {normalize_note_path(k): v for k, v in note_map.items()}
    if source_path not in normalized:
        raise ValueError(f"rename source does not exist: {source_path}")
    if dest_path in normalized:
        raise ValueError(f"rename destination already exists: {dest_path}")
    existing = set(normalized)
    rewrites: dict[str, str] = {dest_path: normalized[source_path]}
    dangling: set[str] = set()
    for path in sorted(normalized):
        if path == source_path:
            continue
        text = normalized[path]
        updated = _rewrite_links(text, source_path, dest_path)
        if updated != text:
            rewrites[path] = updated
        for link in extract_wikilinks(updated):
            if link != dest_path and link not in existing:
                dangling.add(link)
    return RenamePlan(source=source_path, destination=dest_path,
                      rewrites=rewrites, dangling=tuple(sorted(dangling)))


def _rewrite_links(markdown: str, source: str, destination: str) -> str:
    dest_stem = destination[:-3] if destination.endswith(".md") else destination

    def repl(match: re.Match[str]) -> str:
        try:
            target = normalize_note_path(match.group("target").strip())
        except SchemaError:
            return match.group(0)
        if target != source:
            return match.group(0)
        inner = match.group(0)[2:-2]
        suffix = inner[len(match.group("target")):]
        return "[[" + dest_stem + suffix + "]]"

    # Rewrite only real links: skip fenced code blocks entirely and inline code
    # spans within a line, so [[source]] shown as an example stays verbatim.
    out: list[str] = []
    in_fence = False
    for line in markdown.splitlines(keepends=True):
        if FENCE_RE.match(line.rstrip("\n")):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        last = 0
        pieces: list[str] = []
        for span in INLINE_CODE_RE.finditer(line):
            pieces.append(WIKILINK_RE.sub(repl, line[last:span.start()]))
            pieces.append(span.group(0))   # inline code span unchanged
            last = span.end()
        pieces.append(WIKILINK_RE.sub(repl, line[last:]))
        out.append("".join(pieces))
    return "".join(out)
