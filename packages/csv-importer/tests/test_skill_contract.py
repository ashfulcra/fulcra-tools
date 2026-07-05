"""SKILL.md contract tests for fulcra-csv — pattern-doc checklist item 4
(`docs/skill-quality-pattern.md`): pin every prose claim that code could
falsify, so drift is caught at commit time instead of in review.

Modeled on `packages/netflix-skill/tests/test_skill_contract.py`. This CLI is
click-based (not argparse), so flag discovery greps `click.option(...)`. Every
backticked/fenced `--flag` in the SKILL.md must be a real CLI flag (or an
allowlisted foreign-tool flag), in BOTH directions, plus a staleness guard on
the allowlist. Cheap-beats-clever: grep/parse assertions, not simulations.
"""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
SKILL_DIR = PKG / "skills" / "fulcra-csv"
SKILL = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
CLI_SRC = (PKG / "fulcra_csv" / "cli.py").read_text(encoding="utf-8")

#: flags in the prose that belong to OTHER tools (the separate fulcra-api CLI —
#: `fulcra auth`, `fulcra catalog` — and universal click builtins), not
#: fulcra-csv itself. Kept explicit so a typo'd fulcra-csv flag can't hide here.
FOREIGN_FLAGS: set[str] = set()


def _cli_flags() -> set[str]:
    """Every `--flag` declared via click.option in the CLI source."""
    return set(re.findall(r'click\.option\(\s*"(--[a-z0-9-]+)"', CLI_SRC))


def _doc_flags() -> set[str]:
    """Every `--flag` token anywhere in the SKILL.md — fenced code blocks AND
    inline backticks. Stricter than an inline-only grep: a bogus flag in a
    copy-paste recipe is still drift the user would hit."""
    return set(re.findall(r"(--[a-z][a-z0-9-]+)", SKILL))


def test_every_doc_flag_exists_in_cli_or_allowlist():
    unknown = _doc_flags() - _cli_flags() - FOREIGN_FLAGS
    assert not unknown, (
        f"SKILL.md mentions flags that are neither fulcra-csv CLI flags nor "
        f"allowlisted foreign-tool flags: {sorted(unknown)}")


def test_every_cli_flag_is_documented():
    undocumented = _cli_flags() - _doc_flags()
    assert not undocumented, (
        f"CLI flags never mentioned in the SKILL.md: {sorted(undocumented)} "
        f"— document them or remove them")


def test_foreign_allowlist_is_not_stale():
    # if a foreign flag disappears from the prose, prune the allowlist.
    gone = FOREIGN_FLAGS - _doc_flags()
    assert not gone, f"allowlisted flags no longer in prose: {sorted(gone)}"


def test_probe_table_present_before_target_modes():
    """The re-entrancy probe table must exist and sit ahead of the operational
    instructions ('Three target modes')."""
    probe_at = SKILL.find("## Where to start")
    modes_at = SKILL.find("## Three target modes")
    assert probe_at != -1, "probe preamble ('Where to start') missing"
    assert modes_at != -1, "'Three target modes' section missing"
    assert probe_at < modes_at, "probe table must precede the operational modes"


def test_probe_table_commands_reference_real_tools():
    # anchor to the probe section, not any table in the file.
    start = SKILL.index("## Where to start")
    section = SKILL[start:SKILL.index("## Three target modes")]
    rows = [ln for ln in section.splitlines() if ln.startswith("|") and "`" in ln]
    assert rows, "probe table missing from 'Where to start'"
    cmds = " ".join(rows)
    # auth + def-existence + dry-run + landed-evidence probes, per the pattern.
    for token in ("fulcra auth print-access-token", "fulcra catalog",
                  "fulcra-csv import", "--dry-run", "fulcra-csv export"):
        assert token in cmds, f"probe table lost its {token!r} probe"


def test_subcommands_named_in_prose_are_real():
    """Every fulcra-csv subcommand the SKILL tells the agent to run must be a
    real click command. Guards against renamed/removed commands."""
    cli_cmds = set(re.findall(r'@cli\.command\(\s*"([a-z-]+)"', CLI_SRC))
    # commands declared without an explicit name string take the function name.
    cli_cmds |= {"bootstrap"}  # @cli.command() def bootstrap
    referenced = set(re.findall(r"fulcra-csv (import|export|bootstrap|soft-delete)\b", SKILL))
    missing = referenced - cli_cmds
    assert not missing, f"SKILL references non-existent fulcra-csv subcommands: {sorted(missing)}"


def test_deterministic_reimport_claim_is_true():
    """The probe preamble and 'Critical invariant' both promise re-imports are
    safe because source_ids are deterministic. Pin that the invariant text is
    present (the parser-level determinism is exercised by test_parser.py)."""
    assert "re-running the same import is always safe" in SKILL
    assert "source_ids" in SKILL


def test_source_id_prefix_default_matches_cli():
    """The cheatsheet documents the default --source-id-prefix; pin it against
    the CLI's actual default so the two can't drift apart."""
    m = re.search(r'--source-id-prefix",\s*default="([^"]+)"', CLI_SRC)
    assert m, "could not find --source-id-prefix default in CLI"
    default = m.group(1)
    assert default == "com.fulcradynamics.csv.v1"
    assert default in SKILL, (
        f"SKILL.md no longer documents the real --source-id-prefix default "
        f"{default!r}")
