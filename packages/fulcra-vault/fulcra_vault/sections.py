"""Owned-section parsing and mutation for vault notes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re


OPEN_RE = re.compile(
    r"^<!--\s*section:(?P<slug>[a-z][a-z0-9-]*)\s+owner:(?P<owner>[^>]+?)\s*-->\s*$"
)
CLOSE_RE = re.compile(r"^<!--\s*/section:(?P<slug>[a-z][a-z0-9-]*)\s*-->\s*$")
LOG_HEADING_RE = re.compile(r"^##\s+Log\s*$")
FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)")


class SectionError(ValueError):
    """Raised when a note's section structure cannot be safely edited."""


class MissingSectionError(SectionError):
    pass


class DuplicateSectionError(SectionError):
    pass


class OwnerMismatchError(SectionError):
    pass


@dataclass(frozen=True)
class Section:
    slug: str
    owner: str
    open_line: int
    body_start_line: int
    close_line: int


def parse_sections(markdown: str) -> list[Section]:
    """Parse owned section markers.

    Line indexes are zero-based and identify the marker/body boundaries in
    ``markdown.splitlines(keepends=True)``.
    """
    lines = markdown.splitlines(keepends=True)
    sections: list[Section] = []
    active: tuple[str, str, int] | None = None
    seen: set[str] = set()
    in_fence = False
    for idx, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if FENCE_RE.match(text):
            in_fence = not in_fence
            continue
        if in_fence:
            continue   # markers inside a fenced code block are documentation
        open_match = OPEN_RE.match(text)
        close_match = CLOSE_RE.match(text)
        if open_match:
            if active is not None:
                raise SectionError("nested section markers are not allowed")
            slug = open_match.group("slug")
            if slug in seen:
                raise DuplicateSectionError(f"duplicate section: {slug}")
            active = (slug, open_match.group("owner").strip(), idx)
            seen.add(slug)
            continue
        if close_match:
            if active is None:
                raise SectionError(f"closing marker without opener: {text}")
            slug, owner, open_idx = active
            if close_match.group("slug") != slug:
                raise SectionError(
                    f"section close mismatch: expected {slug}, got {close_match.group('slug')}"
                )
            sections.append(Section(slug=slug, owner=owner, open_line=open_idx,
                                    body_start_line=open_idx + 1,
                                    close_line=idx))
            active = None
    if active is not None:
        raise SectionError(f"section {active[0]} is missing a close marker")
    return sections


def replace_owned_section(markdown: str, slug: str, owner: str, body: str,
                          *, force: bool = False) -> str:
    section = _one_section(markdown, slug)
    if section.owner != owner and not force:
        raise OwnerMismatchError(
            f"section {slug} is owned by {section.owner}, not {owner}"
    )
    lines = markdown.splitlines(keepends=True)
    replacement = _body_lines(body)
    return "".join(
        lines[:section.body_start_line] + replacement + lines[section.close_line:]
    )


def append_log(markdown: str, entry: str, now: datetime, agent: str) -> str:
    if not entry.strip():
        raise SectionError("log entry must not be empty")
    lines = markdown.splitlines(keepends=True)
    insert_at = _log_insert_index(lines)
    stamp = now.isoformat()
    line = f"- {stamp} {agent}: {entry.strip()}\n"
    return "".join(lines[:insert_at] + [line] + lines[insert_at:])


def _one_section(markdown: str, slug: str) -> Section:
    matches = [s for s in parse_sections(markdown) if s.slug == slug]
    if not matches:
        raise MissingSectionError(f"missing section: {slug}")
    if len(matches) > 1:
        raise DuplicateSectionError(f"duplicate section: {slug}")
    return matches[0]


def _body_lines(body: str) -> list[str]:
    if body == "":
        return []
    text = body if body.endswith("\n") else body + "\n"
    return text.splitlines(keepends=True)


def _log_insert_index(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if LOG_HEADING_RE.match(line.rstrip("\r\n")):
            insert_at = idx + 1
            while insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            return insert_at
    raise MissingSectionError("missing ## Log section")
