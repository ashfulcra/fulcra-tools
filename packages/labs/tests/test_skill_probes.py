"""Contract test for the fulcra-lab-results skill's re-entrancy declaration.

The pipeline is per-invocation and idempotent, so it carries the pattern doc's
sanctioned **stateless — no probes** declaration rather than a probe grid
(`docs/skill-quality-pattern.md`, checklist item 1). Pin that the declaration
stays put — if someone later makes the skill stateful, they replace this with a
grid and update this test deliberately.
"""

from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[3]
    / "skills" / "fulcra-lab-results" / "SKILL.md"
)


def test_stateless_declaration_present():
    text = SKILL.read_text(encoding="utf-8")
    assert "## Where to start" in text, "missing the 'Where to start' section"
    assert "Stateless — no probes" in text, (
        "the sanctioned stateless declaration is gone — add a probe grid and a "
        "grid contract test if the skill became stateful"
    )
