"""Contract tests for the coord re-entrancy probe tables.

Pattern-doc checklist item 4 (`docs/skill-quality-pattern.md`): pin every prose
claim that code could falsify, so drift is caught at commit time instead of in
review. Wrong probe COMMANDS (verbs that don't exist) were CRITICAL review
findings twice — these tests make that class of drift impossible to merge.

Cheap-beats-clever: grep/parse assertions, not simulations.

Two invariants, per skill that ships a probe table:
  1. the "Where to start" probe heading exists; and
  2. every ``coord-engine <verb>`` token inside the probe section names a REAL
     verb — parsed from ``sub.add_parser("...")`` / ``*sub.add_parser("...")``
     in the CLI source text (so a typo'd or renamed verb fails CI).
"""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]            # packages/coord-engine
REPO = PKG.parents[1]                                # repo root
CLI_SRC = (PKG / "coord_engine" / "cli.py").read_text(encoding="utf-8")

#: skills that ship a probe table (repo-root skills/ tree)
PROBE_SKILLS = ("fulcra-agent-presence", "fulcra-agent-roles", "fulcra-agent-tasks",
                "fulcra-agent-automation", "fulcra-agent-continuity",
                "fulcra-agent-review", "fulcra-agent-directives",
                "fulcra-agent-reconcile", "fulcra-agent-forge", "fulcra-agent-operator",
                "fulcra-agent-atc", "fulcra-agent-durable-state")

PROBE_HEADING = "## Where to start — the re-entrancy probes"


def _skill_text(name: str) -> str:
    return (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def _probe_section(text: str) -> str:
    """The probe table section only: from the probe heading up to the next
    ``## `` heading (so verbs elsewhere in the SKILL can't launder a typo in)."""
    start = text.index(PROBE_HEADING)
    rest = text[start + len(PROBE_HEADING):]
    m = re.search(r"\n## ", rest)
    return rest[: m.start()] if m else rest


def _real_verbs() -> set[str]:
    """All sub-command names the CLI registers, from every
    ``add_parser("<name>")`` in the source text (top-level AND nested
    subparsers — ``roles status`` etc.)."""
    return set(re.findall(r'\.add_parser\(\s*"([a-z][a-z-]*)"', CLI_SRC))


def _probe_verbs(section: str) -> set[str]:
    """Every ``coord-engine <verb>`` (optionally ``<verb> <subverb>``) token in
    a probe section — the verbs a probe command actually invokes."""
    verbs: set[str] = set()
    for first, second in re.findall(
        r"coord-engine\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?", section
    ):
        verbs.add(first)
        # a bare word after a group verb is its subcommand (roles status);
        # a word that is itself a top-level verb is a separate mention, not a sub.
        if second:
            verbs.add(second)
    return verbs


def test_probe_heading_present_in_each_skill():
    for name in PROBE_SKILLS:
        assert PROBE_HEADING in _skill_text(name), (
            f"{name}/SKILL.md is missing the probe heading {PROBE_HEADING!r}"
        )


AGENTS_MD = REPO / "AGENTS.md"
ROUTER_HEADING = "## Where to start"


def _router_section() -> str:
    text = AGENTS_MD.read_text(encoding="utf-8")
    start = text.index(ROUTER_HEADING)
    rest = text[start + len(ROUTER_HEADING):]
    m = re.search(r"\n## ", rest)
    return rest[: m.start()] if m else rest


def test_agents_md_router_present_and_verbs_real():
    """The root AGENTS.md 'Where to start' router grid must exist and every
    ``coord-engine <verb>`` probe it names must be a real CLI verb."""
    section = _router_section()
    real = _real_verbs()
    assert {"doctor", "briefing"} <= real, f"verb parse broken — got {sorted(real)}"
    mentioned = _probe_verbs(section)
    unknown = {v for v in mentioned if v not in real}
    assert not unknown, (
        f"AGENTS.md router invokes coord-engine verb(s) not in cli.py: {sorted(unknown)}"
    )


def test_every_probe_verb_is_a_real_cli_verb():
    real = _real_verbs()
    # sanity: the parse found the CLI's own verbs, so a bug here can't vacuously pass
    assert {"doctor", "status", "presence", "roles", "needs-me"} <= real, (
        f"verb parse looks broken — got {sorted(real)}"
    )
    for name in PROBE_SKILLS:
        section = _probe_section(_skill_text(name))
        # LIMITATION: tokens are checked against one flat verb set — a wrong-group
        # pairing (e.g. 'presence claim') would pass; typos still fail CI.
        # Full group-verb pairing is future work.
        mentioned = _probe_verbs(section)
        unknown = {v for v in mentioned if v not in real}
        assert not unknown, (
            f"{name}/SKILL.md probe section invokes coord-engine verb(s) that do "
            f"not exist in cli.py: {sorted(unknown)}"
        )


def test_durable_state_probes_use_the_stash_verb():
    """durable-state's stash probes run on the engine's ``stash`` verb (BUS-75);
    only auth still probes on ``fulcra-api`` (the engine inherits its
    credentials). Pin both spellings — and pin the raw-file probe OUT — so the
    SKILL prose and the CLI move together, the same contract the pre-stash
    version of this test enforced in the other direction."""
    section = _probe_section(_skill_text("fulcra-agent-durable-state"))
    assert "fulcra-api auth print-access-token" in section, (
        "durable-state auth probe must use `fulcra-api auth print-access-token`"
    )
    assert "coord-engine stash list" in section, (
        "durable-state stash probe must use `coord-engine stash list`"
    )
    assert "fulcra-api file list" not in section, (
        "the raw-file stash probe was replaced by the stash verb — if it came "
        "back, update the SKILL probe table and this pin together"
    )
