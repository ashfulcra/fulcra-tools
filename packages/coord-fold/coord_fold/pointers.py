"""Pointer resolution (coord-boss finding, 2026-09-05): emitters write `ptr` TEAM-RELATIVE (`task/<slug>.md`) while
the store holds the document at `team/<team>/task/<slug>.md`. A reader that takes the pointer verbatim gets `absent`
for every row — `absent` is a legitimate answer, so nothing flags it, and `--verify-pointers` reported 339 of 339 opens
unreadable on a healthy fold. Decision (option 2 of the two offered, chosen by the fold's owner): the READER resolves
a bare pointer against its team root and passes an already-qualified one through. No migration of emitted events."""
from __future__ import annotations

_QUALIFIED = "team/"


def qualify(team: str, ptr: str) -> str:
    """`task/x.md` -> `team/<team>/task/x.md`; `team/<any>/...` unchanged. A leading slash is stripped, never trusted."""
    p = (ptr or "").lstrip("/")
    if p.startswith(_QUALIFIED):
        return p
    return f"{_QUALIFIED}{team}/{p}"
