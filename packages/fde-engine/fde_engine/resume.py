"""Resume brief — the one deterministic read a fresh session starts from.

Folds the engagement doc + artifact presence + the current phase's primary
artifact tail into markdown. No timestamps are generated here (pure fold of
store state), so the same store state always yields the same brief.
"""

from __future__ import annotations

from .engagement import EXPECTED_ARTIFACTS, remote_path, status

_TAIL_LINES = 20

# Four backticks: a fence only closes on a backtick run at least as long as
# the opener, so three-backtick fences inside the tailed artifact nest safely.
_FENCE = "````"


def resume_brief(transport, slug: str) -> str:
    """Fold store state into markdown: identity/history, artifact checklist,
    next move, and the tail of the current phase's primary artifact."""
    st = status(transport, slug)
    lines = [
        f"# FDE engagement: {st['title']} ({st['slug']})",
        "",
        f"- phase: {st['phase']}",
        f"- updated: {st['updated_at']}",
        # Skip falsy/whitespace history entries — a hand-edited doc with a
        # stray empty "- " line must degrade gracefully, not crash the brief.
        f"- history: {' -> '.join(e.split()[0] for e in st['phase_history'] if e.split())}",
        "",
        "## Artifacts",
    ]
    for rel, present in st["artifacts"].items():
        lines.append(f"- [{'x' if present else ' '}] {rel}")
    lines += ["", "## Next", st["next"]]
    primary = EXPECTED_ARTIFACTS.get(st["phase"], [None])[0]
    if primary:
        content = transport.read(remote_path(slug, primary))
        if content:
            tail = content.splitlines()[-_TAIL_LINES:]
            lines += ["", f"## Tail of {primary}", _FENCE] + tail + [_FENCE]
    return "\n".join(lines) + "\n"
