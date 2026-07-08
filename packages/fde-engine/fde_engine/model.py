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
