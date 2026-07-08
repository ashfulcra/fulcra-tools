"""Engagement state model — the seven-phase machine and the engagement doc.

The phase graph is the spec's lifecycle: strictly forward, except that
prototype verification findings may legitimately invalidate earlier thinking,
so prototype has explicit backward edges to architecture and plan. Everything
else (skips, restarts) is rejected — an engagement that needs to restart is a
new engagement.
"""

from __future__ import annotations

import re

SCHEMA = "fulcra.fde.engagement.v1"

PHASES = [
    "intake", "interview", "architecture", "plan",
    "prototype", "build", "retro",
]

TRANSITIONS: dict[str, set[str]] = {
    "intake": {"interview"},
    "interview": {"architecture"},
    "architecture": {"plan"},
    "plan": {"prototype"},
    # prototype findings may reopen earlier phases (spec: backward edges)
    "prototype": {"build", "architecture", "plan"},
    "build": {"retro"},
    "retro": set(),
}


def valid_transition(current: str, new: str) -> bool:
    return new in TRANSITIONS.get(current, set())


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "engagement"


_SCALAR_KEYS = ("schema", "slug", "title", "phase", "created_at", "updated_at")


def render_engagement(meta: dict) -> str:
    """Render the machine-managed engagement.md. Frontmatter is ours; the body
    is a stub humans/agents may extend (parse ignores the body entirely)."""
    lines = ["---"]
    for key in _SCALAR_KEYS:
        lines.append(f"{key}: {meta.get(key, '')}")
    lines.append("phase_history:")
    for entry in meta.get("phase_history", []):
        lines.append(f"  - {entry}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Engagement: {meta.get('title') or meta.get('slug', '')}")
    lines.append("")
    lines.append(
        "State record owned by fde-engine — edit prose below freely; the "
        "frontmatter is machine-managed (use `fde-engine phase`)."
    )
    return "\n".join(lines) + "\n"


def parse_engagement(text) -> dict | None:
    """Parse engagement.md frontmatter. Flat `key: scalar` plus the
    `phase_history:` block list — deliberately the tiny YAML subset we render,
    nothing more (stdlib-only; no PyYAML). Returns None unless the schema
    matches, so callers can't mistake arbitrary markdown for state."""
    if not text:
        return None
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return None
    close = None
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            close = j
            break
    if close is None:
        return None
    meta: dict = {"phase_history": []}
    in_history = False
    for raw in lines[i + 1 : close]:
        if not raw.strip():
            continue
        if in_history and raw.lstrip().startswith("- "):
            meta["phase_history"].append(raw.lstrip()[2:].strip())
            continue
        in_history = False
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key, val = key.strip(), val.strip()
        if key == "phase_history":
            in_history = True
            continue
        if key in _SCALAR_KEYS:
            meta[key] = val
    if meta.get("schema") != SCHEMA:
        return None
    return meta
