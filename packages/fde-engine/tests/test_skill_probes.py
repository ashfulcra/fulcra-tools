"""Keep the skill's prose honest against the CLI it documents.

Same idea as coord-engine's skill probes: if a verb is renamed or removed,
this test fails before a user's agent discovers the drift in production.
"""

import os

SKILL = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "skills", "fulcra-fde", "SKILL.md"
)


def _skill_text() -> str:
    with open(os.path.normpath(SKILL), encoding="utf-8") as fh:
        return fh.read()


def test_skill_documents_every_cli_verb():
    from fde_engine.cli import build_parser
    text = _skill_text()
    subparsers = next(
        a for a in build_parser()._actions
        if a.__class__.__name__ == "_SubParsersAction"
    )
    for verb in subparsers.choices:
        assert f"fde-engine {verb}" in text, (
            f"SKILL.md never shows `fde-engine {verb}` — document it or drop the verb"
        )


def test_skill_names_all_seven_phases():
    from fde_engine import model
    text = _skill_text()
    for phase in model.PHASES:
        assert phase in text


def test_references_exist():
    text = _skill_text()
    ref_dir = os.path.normpath(os.path.join(os.path.dirname(SKILL), "references"))
    for name in ("interview.md", "capability-mapping.md",
                 "build-doctrine.md", "file-layout.md"):
        assert f"references/{name}" in text
        assert os.path.exists(os.path.join(ref_dir, name)), f"missing {name}"
