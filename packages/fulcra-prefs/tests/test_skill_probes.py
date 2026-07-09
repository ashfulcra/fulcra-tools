"""Contract test for the fulcra-prefs re-entrancy probe grid.

Pattern-doc checklist item 4 (`docs/skill-quality-pattern.md`): pin every prose
claim that code could falsify. Wrong probe COMMANDS (verbs that don't exist)
were CRITICAL review findings on the coord skills twice — this makes that class
of drift impossible to merge for prefs too.

Two invariants:
  1. the "Where to start" probe heading exists; and
  2. every ``fulcra-prefs <verb>`` token inside the probe section names a REAL
     subcommand — parsed from ``add_parser("...")`` in the CLI source text.
"""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]  # packages/fulcra-prefs
SKILL = PKG / "skill" / "SKILL.md"
CLI_SRC = (PKG / "fulcra_prefs" / "cli.py").read_text(encoding="utf-8")

PROBE_HEADING = "## Where to start — the re-entrancy probes"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _probe_section(text: str) -> str:
    """Probe section only: probe heading up to the next ``## `` heading, so a
    verb elsewhere in the SKILL can't launder a typo into the grid."""
    start = text.index(PROBE_HEADING)
    rest = text[start + len(PROBE_HEADING):]
    m = re.search(r"\n## ", rest)
    return rest[: m.start()] if m else rest


def _real_verbs() -> set[str]:
    return set(re.findall(r'\.add_parser\(\s*"([a-z][a-z-]*)"', CLI_SRC))


def _probe_verbs(section: str) -> set[str]:
    return set(re.findall(r"fulcra-prefs\s+([a-z][a-z-]*)", section))


def test_probe_heading_present():
    assert PROBE_HEADING in _skill_text(), (
        f"fulcra-prefs SKILL.md is missing the probe heading {PROBE_HEADING!r}"
    )


def test_every_probe_verb_is_real():
    real = _real_verbs()
    # sanity: parse actually found the CLI's verbs, so a bug can't vacuously pass
    assert {"compile", "inject", "onboard", "install-hooks"} <= real, (
        f"verb parse looks broken — got {sorted(real)}"
    )
    unknown = {v for v in _probe_verbs(_probe_section(_skill_text())) if v not in real}
    assert not unknown, (
        f"probe grid invokes fulcra-prefs verb(s) not in cli.py: {sorted(unknown)}"
    )
