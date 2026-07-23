"""OKF v0.1 read/render for L1 — stdlib-only, never-raises.

- ``parse_frontmatter`` reads a concept doc's YAML-subset frontmatter.
- ``render_index`` produces an engine-owned ``index.md`` (OKF §6, no frontmatter).
- ``render_log`` / ``merge_log`` produce ``log.md`` entries (OKF §7).

We cannot depend on PyYAML (stdlib-only), so ``parse_frontmatter`` implements the
small YAML subset OKF frontmatter actually uses: ``key: scalar``, ``key: [a, b]``
inline lists, and ``key:`` + indented ``- item`` block lists. Anything it can't
parse degrades to ``None`` (caller keeps the prior row) rather than raising.
"""

from __future__ import annotations

from typing import Any, Optional

_DELIM = "---"


def split_frontmatter(text: Optional[str]) -> Optional[tuple[str, str]]:
    """Return ``(frontmatter_text, body)`` or ``None`` if no leading ``---`` block."""
    if not text:
        return None
    lines = text.lstrip("﻿").splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip() != _DELIM:
        return None
    start = i + 1
    for j in range(start, len(lines)):
        if lines[j].strip() == _DELIM:
            return "\n".join(lines[start:j]), "\n".join(lines[j + 1 :])
    return None  # no closing delimiter


def parse_frontmatter(text: Optional[str]) -> Optional[dict[str, Any]]:
    """Parse the frontmatter block to a dict, or ``None`` if absent/unparseable."""
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        return _parse_yaml_subset(split[0])
    except Exception:
        return None


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if s == "" or s.lower() in ("null", "~"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_unquote(x) for x in inner.split(",") if x.strip() != ""]
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    return s


def _parse_yaml_subset(fm_text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lines = fm_text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        # skip blanks, comment lines, and stray block-list items (no key)
        if stripped == "" or stripped.startswith("#") or ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        if not key:
            i += 1
            continue
        val_raw = rest.strip()
        if val_raw in ("|", "|-", "|+"):
            block: list[str] = []
            j = i + 1
            while j < n:
                lj = lines[j]
                sj = lj.strip()
                if sj == "":
                    block.append("")
                    j += 1
                    continue
                if lj[:1].isspace():
                    block.append(lj[2:] if lj.startswith("  ") else lj.lstrip())
                    j += 1
                    continue
                break
            out[key] = "\n".join(block)
            i = j
            continue
        if val_raw == "":
            # An empty scalar may introduce an indented block: either a block list
            # ("- item" lines) or a one-level nested map ("subkey: value" lines).
            # The first meaningful indented line decides which; the two never mix.
            items: list[str] = []
            pairs: dict[str, Any] = {}
            kind: Optional[str] = None
            j = i + 1
            while j < n:
                lj = lines[j]
                sj = lj.strip()
                if sj == "" or sj.startswith("#"):
                    j += 1
                    continue
                if not lj[:1].isspace():
                    break
                if sj.startswith("- "):
                    if kind == "map":
                        break
                    kind = "list"
                    items.append(_unquote(sj[2:]))
                    j += 1
                    continue
                if ":" in lj:
                    if kind == "list":
                        break
                    kind = "map"
                    sub_key, _, sub_rest = lj.partition(":")
                    pairs[sub_key.strip()] = _parse_scalar(sub_rest.strip())
                    j += 1
                    continue
                break
            if kind == "map":
                out[key] = pairs
            elif kind == "list":
                out[key] = items
            else:
                out[key] = None
            i = j if kind else i + 1
            continue
        out[key] = _parse_scalar(val_raw)
        i += 1
    return out


def _needs_quote(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    return s[0] in "[{!&*#?|>%@`\"'" or ": " in s or s.lower() in ("null", "true", "false", "~")


def _render_scalar(s: str) -> str:
    return f"{chr(34)}{s}{chr(34)}" if _needs_quote(s) else s


def render_frontmatter(fields: dict[str, Any]) -> str:
    """Serialize a frontmatter dict to an OKF ``---`` block (inverse of
    ``parse_frontmatter`` for the subset we use). ``None`` values are omitted.
    Round-trips with ``parse_frontmatter`` for scalars and lists.
    """
    lines = ["---"]
    for key, val in fields.items():
        if val is None:
            continue
        if isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, dict):
            # One-level nested map (e.g. the presence ``engagement`` object). Nulls
            # are rendered EXPLICITLY as ``null`` here (unlike the top level, which
            # omits them) so the object round-trips with every key present — the
            # schema depicts ``until``/``lapsed_at`` as literal ``null``.
            lines.append(f"{key}:")
            for sub_key, sub_val in val.items():
                if sub_val is None:
                    lines.append(f"  {sub_key}: null")
                elif isinstance(sub_val, bool):
                    lines.append(f"  {sub_key}: {'true' if sub_val else 'false'}")
                else:
                    lines.append(f"  {sub_key}: {_render_scalar(str(sub_val))}")
        elif isinstance(val, list):
            if not val:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in val:
                    lines.append(f"  - {_render_scalar(str(item))}")
        else:
            s = str(val)
            if "\n" in s:
                lines.append(f"{key}: |")
                lines.extend(f"  {part}" for part in s.split("\n"))
            else:
                lines.append(f"{key}: {_render_scalar(s)}")
    lines.append("---")
    return "\n".join(lines)


# --- rendering (OKF §6 index, §7 log) ---------------------------------------

#: In-band guardrail so a hand-editing agent (or human) sees the ownership
#: boundary in the file itself, not only in a SKILL.md they may never read.
ENGINE_BANNER = (
    "<!-- ENGINE-OWNED: regenerated by fulcra-agent-reconcile (coord-engine). "
    "Edit task/*.md, not this file; manual edits here are overwritten. -->"
)


#: (Section heading, statuses that fall under it) in fixed display order.
INDEX_SECTIONS = (
    ("Active", ("active",)),
    ("Waiting", ("waiting",)),
    ("Blocked", ("blocked",)),
    ("Proposed", ("proposed",)),
    ("Recently Done", ("done", "abandoned")),
)


def _bullet(row: dict[str, Any]) -> str:
    title = row.get("title") or row.get("name") or row.get("id") or "untitled"
    link = row.get("name") or row.get("id") or "untitled"
    desc = (row.get("description") or "").strip()
    href = link if str(link).endswith(".md") else f"{link}.md"
    return f"* [{title}]({href}) - {desc}" if desc else f"* [{title}]({href})"


def render_index(
    rows: list[dict[str, Any]],
    *,
    heading: str = "Tasks",
    sort_fn=None,
) -> str:
    """Render an OKF §6 index: sections by status, bullets carrying the
    concept's description. Index files carry no frontmatter. Empty sections and
    unknown statuses are handled: unknowns collect under a trailing "Other".
    """
    from .model import sort_rows as _default_sort

    sort_fn = sort_fn or _default_sort
    known = {s for _, statuses in INDEX_SECTIONS for s in statuses}
    parts: list[str] = [ENGINE_BANNER, "", f"# {heading}", ""]
    for title, statuses in INDEX_SECTIONS:
        group = [r for r in rows if r.get("status") in statuses]
        if not group:
            continue
        parts.append(f"## {title}")
        for r in sort_fn(group):
            parts.append(_bullet(r))
        parts.append("")
    other = [r for r in rows if r.get("status") not in known]
    if other:
        parts.append("## Other")
        for r in sort_fn(other):
            parts.append(_bullet(r))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_log_section(transitions: list[str], *, date: str) -> str:
    """Render one OKF §7 log section (a ``## <date>`` heading + prose bullets)."""
    lines = [f"## {date}"]
    lines.extend(transitions)
    return "\n".join(lines)


def merge_log(existing: Optional[str], transitions: list[str], *, date: str,
              heading: str = "Task Update Log") -> str:
    """Prepend today's transitions to ``log.md`` (OKF §7, newest-first).

    If the newest existing section is already today's date, the new bullets are
    inserted under it; otherwise a fresh ``## <date>`` section is prepended.
    """
    if not transitions:
        return existing if existing else f"{ENGINE_BANNER}\n\n# {heading}\n"
    section = render_log_section(transitions, date=date)
    if not existing or not existing.strip():
        return f"{ENGINE_BANNER}\n\n# {heading}\n\n{section}\n"
    body = existing.rstrip("\n")
    lines = body.splitlines()
    # locate the title line (first '# ') then the first '## ' section
    title_idx = next((k for k, ln in enumerate(lines) if ln.startswith("# ")), -1)
    first_sec = next((k for k, ln in enumerate(lines) if ln.startswith("## ")), -1)
    if first_sec != -1 and lines[first_sec].strip() == f"## {date}":
        # insert new bullets right after today's heading (newest-first within day)
        head = lines[: first_sec + 1]
        tail = lines[first_sec + 1 :]
        return "\n".join(head + transitions + tail) + "\n"
    # prepend a new section after the title (or at top if no title)
    insert_at = title_idx + 1 if title_idx != -1 else 0
    head = lines[:insert_at]
    tail = lines[insert_at:]
    merged = head + ["", section] + (tail if tail else [])
    # tidy leading blank if we inserted at very top
    return "\n".join(x for x in merged).lstrip("\n").rstrip("\n") + "\n"
