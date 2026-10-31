"""Agent guide drift checks against actual Click metadata and v1 recovery rules."""

import re
from pathlib import Path

import click

PKG = Path(__file__).resolve().parents[1]
SKILL = (PKG / "skills" / "fulcra-csv" / "SKILL.md").read_text(encoding="utf-8")
# These belong to the canonical uv sync recipe, not fulcra-csv.
FOREIGN_FLAGS = {"--all-packages", "--all-extras"}


def _cli_flags():
    from fulcra_csv.cli import cli

    result = set()
    for command in [cli, *cli.commands.values()]:
        params = list(command.params)
        option = command.get_help_option(click.Context(command))
        if option is not None:
            params.append(option)
        for param in params:
            if isinstance(param, click.Option):
                result.update(
                    flag for flag in param.opts + param.secondary_opts if flag.startswith("--")
                )
    return result


def _doc_flags():
    return set(re.findall(r"(--[a-z][a-z0-9-]+)", SKILL))


def test_every_doc_flag_exists_in_cli_or_allowlist():
    assert not (_doc_flags() - _cli_flags() - FOREIGN_FLAGS)


def test_every_cli_flag_is_documented():
    assert not (_cli_flags() - _doc_flags())


def test_foreign_allowlist_is_not_stale():
    assert not (FOREIGN_FLAGS - _doc_flags())


def test_probe_table_present_before_target_selection():
    probe_at = SKILL.index("## Where to start")
    target_at = SKILL.index("## Choose a v1 target")
    assert probe_at < target_at
    section = SKILL[probe_at:target_at]
    rows = "\n".join(line for line in section.splitlines() if line.startswith("|"))
    for token in (
        "fulcra-csv import --help",
        "Event/",
        "Metric/",
        "--data-type",
        "fulcra-csv bootstrap --base-type",
        "--dry-run",
        "UNKNOWN",
    ):
        assert token in rows
    assert "/user/v1alpha1/annotation" not in section


def test_subcommands_named_in_prose_are_real():
    from fulcra_csv.cli import cli

    referenced = set(re.findall(r"fulcra-csv ([a-z][a-z-]*)\b", SKILL))
    assert {"import", "bootstrap", "export", "soft-delete"} <= referenced
    assert referenced <= set(cli.commands)


def test_recovery_and_uncertainty_replace_unconditional_retry_claims():
    for required in (
        "before opening",
        "UNKNOWN",
        "lower bounds",
        "never blindly resent",
        "separate recovered",
        "no runtime/profile/DB/auth",
    ):
        assert required in SKILL
    for stale in (
        "re-running the same import is always safe",
        "server-side no-op",
        "export` is the inverse",
        "## Three target modes",
    ):
        assert stale not in SKILL


def test_source_id_prefix_default_matches_cli():
    from fulcra_csv.cli import cli

    option = next(p for p in cli.commands["import"].params if p.name == "source_id_prefix")
    assert option.default == "com.fulcradynamics.csv.v1"
    assert option.default in SKILL


def test_full_target_requirement_matches_actual_callback():
    from click.testing import CliRunner
    from fulcra_csv.cli import cli

    result = CliRunner().invoke(cli, ["import", "missing.csv", "--dry-run"])
    assert result.exit_code != 0
    assert "--data-type Event/" in result.output
    assert "Metric/" in result.output
    assert "required even for" in SKILL


def test_metric_and_legacy_boundaries_documented():
    for required in (
        "Canonical Metric unit is destination metadata",
        "No record-level unit/csv_unit",
        "exact incoming primary source first",
        "Legacy audit and administration",
        "not native v1 export",
        "not a native",
        "Retired client record methods fail",
    ):
        assert required in SKILL
