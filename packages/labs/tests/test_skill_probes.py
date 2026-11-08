"""Keep the stateful labs guide's recovery probes tied to real CLI metadata."""

from pathlib import Path
import re

from fulcra_labs.cli import cli

SKILL = Path(__file__).resolve().parents[3] / "skills" / "fulcra-lab-results" / "SKILL.md"


def test_recovery_probe_grid_replaces_stateless_restart_claim():
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("## Where to start")
    section = text[start:text.index("## The flow", start)]
    rows = [line for line in section.splitlines() if line.startswith("|") and "`" in line]
    assert len(rows) >= 4
    assert "Stateless — no probes" not in section
    for required in ("status --json", "--marker-target", "pending", "UNKNOWN", "--dry-run"):
        assert required in section
    assert "restarts from Intake" not in section


def test_documented_command_names_and_flags_exist_on_actual_click_cli():
    text = SKILL.read_text(encoding="utf-8")
    names = set(re.findall(r"(?:^|`)(?:uv run )?fulcra-labs ([a-z][a-z-]*)", text, re.M))
    assert names <= set(cli.commands)
    options = set()
    for command in [cli, *cli.commands.values()]:
        for parameter in command.params:
            options.update(getattr(parameter, "opts", ()))
            options.update(getattr(parameter, "secondary_opts", ()))
    flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", text))
    assert flags <= options


def test_guide_distinguishes_native_progress_from_legacy_cache_counts():
    text = SKILL.read_text(encoding="utf-8")
    for required in ("Metric", "last_attempt", "last_verified_ingest", "last_recovery",
                     "numeric_annotation_tracks", "recovery", "UNKNOWN"):
        assert required in text
    assert "Use `fulcra-labs status` to show\nper-track observation counts" not in text
    assert "a re-ingest a server-side no-op" not in text


def test_guide_preserves_two_pass_review_and_local_pdf_boundary():
    text = SKILL.read_text(encoding="utf-8")
    for required in ("TWICE, independently", "--yes-reviewed", "unit_raw",
                     "--source-doc", "sha256:basename", "PDF bytes stay local"):
        assert required in text
