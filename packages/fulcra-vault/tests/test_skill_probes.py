"""Contract test for the fulcra-vault re-entrancy probe grid.

Pattern-doc checklist item 4 (`docs/skill-quality-pattern.md`): pin the probe
heading and keep every ``fulcra-vault <verb>`` in the grid honest against the
CLI, so a renamed/typo'd verb fails CI instead of a user's agent in production.
"""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]  # packages/fulcra-vault
SKILL = PKG / "skill" / "SKILL.md"
CLI_SRC = (PKG / "fulcra_vault" / "cli.py").read_text(encoding="utf-8")

PROBE_HEADING = "## Where to start — the re-entrancy probes"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _probe_section(text: str) -> str:
    start = text.index(PROBE_HEADING)
    rest = text[start + len(PROBE_HEADING):]
    m = re.search(r"\n## ", rest)
    return rest[: m.start()] if m else rest


def _real_verbs() -> set[str]:
    return set(re.findall(r'\.add_parser\(\s*"([a-z][a-z-]*)"', CLI_SRC))


def _probe_verbs(section: str) -> set[str]:
    return set(re.findall(r"fulcra-vault\s+([a-z][a-z-]*)", section))


def test_probe_heading_present():
    assert PROBE_HEADING in _skill_text(), (
        f"fulcra-vault SKILL.md is missing the probe heading {PROBE_HEADING!r}"
    )


def test_every_probe_verb_is_real():
    real = _real_verbs()
    assert {"map", "read", "init", "install-hooks"} <= real, (
        f"verb parse looks broken — got {sorted(real)}"
    )
    unknown = {v for v in _probe_verbs(_probe_section(_skill_text())) if v not in real}
    assert not unknown, (
        f"probe grid invokes fulcra-vault verb(s) not in cli.py: {sorted(unknown)}"
    )
