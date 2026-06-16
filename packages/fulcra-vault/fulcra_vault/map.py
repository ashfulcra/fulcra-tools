"""Deterministic MAP.md and HOT.md rendering."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from .frontmatter import parse_note
from .links import backlinks_for, extract_wikilinks
from .schema import StructureSpec, normalize_note_path


TRUNCATION_MARKER = "(truncated - run fulcra-vault map)"
LOG_HEADING_RE = re.compile(r"^##\s+Log\s*$")
LOG_LINE_RE = re.compile(r"^-\s+(?P<stamp>\d{4}-\d{2}-\d{2}T\S+)\s+[^:]+:\s+(?P<text>.+)$")
NO_TIMESTAMP_SORT_KEY = "\xff" * 32


class BudgetError(ValueError):
    """Raised when rendered markdown exceeds a configured budget."""


@dataclass(frozen=True)
class HotItem:
    path: str
    title: str
    summary: str
    reasons: tuple[str, ...]
    updated_at: str
    backlink_count: int


def render_map(spec: StructureSpec, notes: dict[str, str], links: dict[str, Any]) -> str:
    normalized = {normalize_note_path(path): text for path, text in notes.items()}
    lines = ["# Vault Map", ""]
    highlights = {normalize_note_path(path) for path in spec.map_highlights}
    used: set[str] = set()
    for section in spec.sections:
        lines.extend([f"## {section.title}", ""])
        if section.description:
            lines.extend([section.description, ""])
        for path in section.seed_notes:
            lines.append(
                _map_line(path, normalized.get(path, ""), links,
                          hot=path in highlights)
            )
            used.add(path)
        lines.append("")
    extras = sorted(path for path in normalized if path not in used)
    if extras:
        lines.extend(["## Unmapped", ""])
        for path in extras:
            lines.append(_map_line(path, normalized[path], links, hot=path in highlights))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def select_hot_items(note_map: dict[str, str], links: dict[str, Any],
                     now: datetime, max_items: int | None = None) -> list[HotItem]:
    scored: list[tuple[tuple[int, str, str], HotItem]] = []
    for raw_path, markdown in note_map.items():
        path = normalize_note_path(raw_path)
        fm, body = parse_note(markdown)
        title = _title(path, fm, body)
        summary = _summary(body)
        reasons = _hot_reasons(fm, body, now)
        updated_at = _updated_at(fm, body)
        backlinks = len(backlinks_for(links, path))
        score = _hot_score(reasons, updated_at, path)
        item = HotItem(path=path, title=title, summary=summary,
                       reasons=reasons or ("background",),
                       updated_at=updated_at, backlink_count=backlinks)
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0])
    items = [item for _, item in scored]
    if max_items is not None:
        return items[:max_items]
    return items


def render_hot(items: list[HotItem], max_words: int = 500) -> str:
    if not items:
        return "# Hot\n\nNo hot items.\n"
    sections: list[str] = ["# Hot", ""]
    for item in items:
        sections.extend([
            f"## [[{_link_target(item.path)}|{item.title}]]",
            f"- Reasons: {', '.join(item.reasons)}",
            f"- Updated: {item.updated_at or 'unknown'}",
        ])
        if item.backlink_count:
            sections.append(f"- Backlinks: {item.backlink_count}")
        sections.extend([f"- Summary: {item.summary}", ""])
    return truncate_markdown("\n".join(sections).rstrip() + "\n", max_words=max_words)


def check_budget(markdown: str, *, max_words: int, label: str) -> str:
    count = _word_count(markdown)
    if count > max_words:
        over = count - max_words
        raise BudgetError(f"{label} is {over} words over budget ({count}/{max_words})")
    return markdown


def truncate_markdown(markdown: str, *, max_words: int) -> str:
    if _word_count(markdown) <= max_words:
        return markdown
    marker_words = _word_count(TRUNCATION_MARKER)
    content_budget = max_words - marker_words
    lines = markdown.splitlines(keepends=True)
    output: list[str] = []
    words = 0
    current_section: list[str] = []
    current_words = 0
    for line in lines:
        if line.startswith("## ") and current_section:
            if words + current_words > content_budget:
                break
            output.extend(current_section)
            words += current_words
            current_section = []
            current_words = 0
        if not current_section and not line.startswith("## "):
            line_words = _word_count(line)
            if words + line_words > content_budget:
                break
            output.append(line)
            words += line_words
            continue
        current_section.append(line)
        current_words += _word_count(line)
    else:
        if current_section and words + current_words <= content_budget:
            output.extend(current_section)
            return "".join(output)
    if content_budget >= 0:
        return "".join(output).rstrip() + "\n\n" + TRUNCATION_MARKER + "\n"
    return _truncate_lines(markdown, max_words=max_words)


def _truncate_lines(markdown: str, *, max_words: int) -> str:
    if max_words <= 0:
        return ""
    output: list[str] = []
    words = 0
    for line in markdown.splitlines(keepends=True):
        line_words = _word_count(line)
        if words + line_words > max_words:
            break
        output.append(line)
        words += line_words
    if output:
        return "".join(output)
    words = re.findall(r"\b[\w'-]+\b", markdown)
    return " ".join(words[:max_words]) + "\n"


def _map_line(path: str, markdown: str, links: dict[str, Any], *, hot: bool) -> str:
    fm, body = parse_note(markdown) if markdown else ({}, "")
    title = _title(path, fm, body)
    summary = _summary(body)
    badges: list[str] = []
    if hot:
        badges.append("hot")
    link_count = len(extract_wikilinks(markdown)) if markdown else 0
    backlink_count = len(backlinks_for(links, path))
    if link_count:
        badges.append(_plural(link_count, "link"))
    if backlink_count:
        badges.append(_plural(backlink_count, "backlink"))
    suffix = f" ({', '.join(badges)})" if badges else ""
    return f"- [[{_link_target(path)}|{title}]] — {summary}{suffix}"


def _hot_reasons(fm: dict[str, Any], body: str, now: datetime) -> tuple[str, ...]:
    reasons: list[str] = []
    status = str(fm.get("status", "")).lower()
    raw_tags = fm.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]  # a scalar `tags:` value is a single tag, not chars
    tags = {str(tag).lower() for tag in raw_tags if isinstance(tag, str)}
    if status in {"active", "current", "open"}:
        reasons.append("active")
    if "standing-correction" in tags or "correction" in tags:
        reasons.append("standing-correction")
    if _has_recent_decision(body, now):
        reasons.append("recent-decision")
    return tuple(reasons)


def _hot_score(reasons: tuple[str, ...], updated_at: str, path: str) -> tuple[int, str, str]:
    priority = 100
    if "active" in reasons:
        priority -= 60
    if "standing-correction" in reasons:
        priority -= 25
    if "recent-decision" in reasons:
        priority -= 15
    return (priority, _reverse_timestamp(updated_at), path)


def _has_recent_decision(body: str, now: datetime) -> bool:
    cutoff = now.astimezone(timezone.utc).timestamp() - (14 * 24 * 60 * 60)
    in_log = False
    for raw in body.splitlines():
        if LOG_HEADING_RE.match(raw):
            in_log = True
            continue
        if in_log and raw.startswith("## "):
            break
        match = LOG_LINE_RE.match(raw)
        if not match:
            continue
        text = match.group("text").lower()
        if not re.search(r"\bdecid", text):  # word-boundary: "decided", not "undecided"
            continue
        stamp = _parse_stamp(match.group("stamp"))
        if stamp is not None and stamp.timestamp() >= cutoff:
            return True
    return False


def _updated_at(fm: dict[str, Any], body: str) -> str:
    value = fm.get("updated_at") or fm.get("updated")
    if isinstance(value, str) and value:
        return value
    for raw in body.splitlines():
        match = LOG_LINE_RE.match(raw)
        if match:
            return match.group("stamp")
    return ""


def _title(path: str, fm: dict[str, Any], body: str) -> str:
    value = fm.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def _summary(body: str) -> str:
    for line in body.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("<!--") or text.startswith("- "):
            continue
        return text
    return "No summary yet."


def _link_target(path: str) -> str:
    return path[:-3] if path.endswith(".md") else path


def _plural(count: int, noun: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {noun}{suffix}"


def _reverse_timestamp(value: str) -> str:
    # Key on the parsed UTC instant so equal instants spelled differently
    # (e.g. "...Z" vs "...+00:00") produce an identical key; fall back to the
    # raw string only when it isn't a parseable timestamp.
    stamp = _parse_stamp(value) if value else None
    basis = stamp.isoformat() if stamp is not None else value
    if not basis or any(ord(ch) > 255 for ch in basis):
        return NO_TIMESTAMP_SORT_KEY
    return "".join(chr(255 - ord(ch)) for ch in basis)


def _parse_stamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _word_count(markdown: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", markdown))
