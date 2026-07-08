"""Resume brief — the one deterministic read a fresh session starts from.

Folds the engagement doc + artifact presence + the current phase's primary
artifact tail into markdown. No timestamps are generated here (pure fold of
store state), so the same store state always yields the same brief.
"""

from __future__ import annotations

from .engagement import EXPECTED_ARTIFACTS, NEXT_HINT, remote_path, status

_TAIL_LINES = 20


def resume_brief(transport, slug: str) -> str:
    st = status(transport, slug)
    lines = [
        f"# FDE engagement: {st['title']} ({st['slug']})",
        "",
        f"- phase: {st['phase']}",
        f"- updated: {st['updated_at']}",
        f"- history: {' -> '.join(e.split()[0] for e in st['phase_history'])}",
        "",
        "## Artifacts",
    ]
    for rel, present in st["artifacts"].items():
        lines.append(f"- [{'x' if present else ' '}] {rel}")
    lines += ["", "## Next", NEXT_HINT.get(st["phase"], "")]
    primary = EXPECTED_ARTIFACTS.get(st["phase"], [None])[0]
    if primary:
        content = transport.read(remote_path(slug, primary))
        if content:
            tail = content.splitlines()[-_TAIL_LINES:]
            lines += ["", f"## Tail of {primary}", "```"] + tail + ["```"]
    return "\n".join(lines) + "\n"
