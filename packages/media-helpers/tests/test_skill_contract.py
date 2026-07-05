"""SKILL.md contract tests for fulcra-media — pattern-doc checklist item 4
(`docs/skill-quality-pattern.md`): pin every prose claim that code could
falsify, so drift is caught at commit time instead of in review.

Modeled on `packages/netflix-skill/tests/test_skill_contract.py`. This CLI is
click-based (not argparse), so flag discovery greps `click.option(...)`.

Two directions on flags:
  * forward  (doc → CLI): every `--flag` the SKILL.md mentions must be a real
    CLI flag or an allowlisted foreign-tool flag. This is the drift that bites
    users — a renamed/removed/misspelled flag in a recipe.
  * reverse  (CLI → doc): the SKILL.md deliberately does NOT enumerate every
    per-importer flag (it points the agent at `--help`). So the reverse check
    is a subset assertion against an explicit UNDOCUMENTED_OK allowlist — a
    newly-added CLI flag forces a conscious "document it or allowlist it"
    decision instead of silently drifting.

Cheap-beats-clever: grep/parse assertions, not simulations.
"""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
SKILL_DIR = PKG / "skills" / "fulcra-media"
SKILL = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
CLI_SRC = (PKG / "fulcra_media" / "cli.py").read_text(encoding="utf-8")

#: flags in the prose that belong to OTHER tools or are click builtins, not
#: fulcra-media itself. `--help` is click's universal builtin (the roster/wizard
#: sections tell the agent to run `... --help` for the live list).
FOREIGN_FLAGS = {"--help"}

#: real fulcra-media CLI flags the SKILL.md intentionally leaves to `--help`
#: rather than documenting inline. Adding a new CLI flag makes this test fail
#: until you either document it in SKILL.md or add it here on purpose.
UNDOCUMENTED_OK = {
    "--bearer-token", "--category", "--confidence", "--db", "--duration-col",
    "--end-col", "--fingerprint", "--host", "--id-col", "--keep-listened",
    "--keep-read", "--keep-watched", "--max-entries", "--max-pages", "--port",
    "--service", "--since", "--subtitle-col", "--title-col", "--ts-col",
    "--tz", "--watermark-overlap-hours",
}


def _cli_flags() -> set[str]:
    return set(re.findall(r'click\.option\(\s*"(--[a-z0-9-]+)"', CLI_SRC))


def _doc_flags() -> set[str]:
    """Every `--flag` token anywhere in SKILL.md — fenced blocks AND inline."""
    return set(re.findall(r"(--[a-z][a-z0-9-]+)", SKILL))


def test_every_doc_flag_exists_in_cli_or_allowlist():
    unknown = _doc_flags() - _cli_flags() - FOREIGN_FLAGS
    assert not unknown, (
        f"SKILL.md mentions flags that are neither fulcra-media CLI flags nor "
        f"allowlisted foreign/builtin flags: {sorted(unknown)}")


def test_documented_and_allowlisted_flags_partition_the_cli():
    """Every real CLI flag is either documented in the SKILL.md or explicitly
    listed in UNDOCUMENTED_OK — no CLI flag falls through the cracks."""
    uncovered = _cli_flags() - _doc_flags() - UNDOCUMENTED_OK
    assert not uncovered, (
        f"CLI flags neither documented nor allowlisted: {sorted(uncovered)} "
        f"— document them in SKILL.md or add to UNDOCUMENTED_OK")


def test_undocumented_allowlist_is_not_stale():
    """UNDOCUMENTED_OK must only name real CLI flags that are in fact NOT
    documented — prune it when a flag gets documented or removed."""
    not_real = UNDOCUMENTED_OK - _cli_flags()
    assert not not_real, (
        f"UNDOCUMENTED_OK names flags that are not CLI flags anymore: "
        f"{sorted(not_real)}")
    now_documented = UNDOCUMENTED_OK & _doc_flags()
    assert not now_documented, (
        f"UNDOCUMENTED_OK names flags that ARE now documented — remove them: "
        f"{sorted(now_documented)}")


def test_foreign_allowlist_is_not_stale():
    gone = FOREIGN_FLAGS - _doc_flags()
    assert not gone, f"allowlisted foreign flags no longer in prose: {sorted(gone)}"


def test_probe_table_present_before_operational_instructions():
    probe_at = SKILL.find("## Where to start")
    quick_at = SKILL.find("## Quick orientation")
    assert probe_at != -1, "probe preamble ('Where to start') missing"
    assert quick_at != -1, "'Quick orientation' section missing"
    assert probe_at < quick_at, "probe table must precede the operational sections"


def test_probe_table_commands_reference_real_tools():
    start = SKILL.index("## Where to start")
    section = SKILL[start:SKILL.index("## Quick orientation")]
    rows = [ln for ln in section.splitlines() if ln.startswith("|") and "`" in ln]
    assert rows, "probe table missing from 'Where to start'"
    cmds = " ".join(rows)
    for token in ("fulcra auth print-access-token", "fulcra-media status",
                  "fulcra-media import", "--check-only", "would_post",
                  "fulcra-media bootstrap"):
        assert token in cmds, f"probe table lost its {token!r} probe"


def _import_commands() -> set[str]:
    return set(re.findall(r'@import_group\.command\(\s*"([a-z-]+)"', CLI_SRC))


def test_importer_roster_names_are_real_commands():
    """Every importer named as a backticked command in the roster table +
    decision tree must be a real `fulcra-media import <name>` subcommand.
    Guards the roster against a renamed/removed importer."""
    real = _import_commands()
    # backticked bare importer names in the roster table (| `lastfm` | ...).
    roster_start = SKILL.index("## Importer roster")
    roster = SKILL[roster_start:SKILL.index("## Recipes", roster_start)]
    named = set(re.findall(r"^\|\s*`([a-z-]+)`", roster, re.M))
    # 'webhook' is explicitly called out as NOT an import subcommand.
    named.discard("webhook")
    missing = named - real
    assert not missing, (
        f"roster lists importers that are not real import subcommands: "
        f"{sorted(missing)}")


def test_all_real_importers_appear_in_the_skill():
    """Every real import subcommand should be discoverable in the SKILL.md so
    the agent knows it exists (roster, decision tree, or recipes)."""
    real = _import_commands()
    for cmd in real:
        assert f"`{cmd}`" in SKILL, (
            f"import subcommand {cmd!r} exists in the CLI but is never "
            f"mentioned (backticked) in the SKILL.md")


def test_state_keys_match_status_output():
    """The 'Quick orientation' + credential-file sections name state.json keys;
    pin them against what `status` actually emits."""
    status_keys = set(re.findall(r'"([a-z_]+)":\s*s\.[a-z_]+', CLI_SRC))
    # the definition-id + watermarks keys the SKILL relies on for probes.
    for key in ("watched_definition_id", "listened_definition_id",
                "read_definition_id", "tag_ids", "watermarks"):
        assert key in status_keys, (
            f"status no longer emits {key!r} — the SKILL's probe/orientation "
            f"prose references it")
        assert key in SKILL, f"SKILL.md stopped documenting the state key {key!r}"


def test_reset_and_bootstrap_are_real_commands():
    for cmd in ("bootstrap", "reset", "status", "webhook", "setup"):
        assert (f'@cli.command("{cmd}"' in CLI_SRC
                or f'def {cmd}(' in CLI_SRC
                or f'name="{cmd}"' in CLI_SRC), f"CLI lost the {cmd!r} command"
        # accept either the fully-qualified invocation or the backticked bare
        # command name (the roster/decision-tree style, e.g. `webhook`).
        assert (f"fulcra-media {cmd}" in SKILL or f"`{cmd}`" in SKILL), (
            f"SKILL.md no longer references the {cmd!r} command")


def test_envelope_stages_documented_match_cli():
    """The SKILL documents the error `stage` values; pin the ones it enumerates
    against stages the CLI actually emits."""
    cli_stages = set(re.findall(r'"stage":\s*"([a-z]+)"', CLI_SRC))
    for stage in ("setup", "auth", "args", "fetch"):
        assert stage in cli_stages, f"CLI no longer emits stage {stage!r}"
        assert stage in SKILL, f"SKILL.md stopped documenting stage {stage!r}"
