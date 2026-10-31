"""Guide/CLI parity and a synthetic native record checked against the real codec."""

import json
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
SKILL_DIR = PKG / "skills" / "fulcra-netflix"
SKILL = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
REFS = {p.name: p.read_text(encoding="utf-8")
        for p in (SKILL_DIR / "references").glob("*.md")}
ALL_PROSE = SKILL + "".join(REFS.values())

#: flags in the prose that belong to OTHER tools (fulcra-api auth), not the
#: import script — keep explicit so a typo'd script flag can't hide here
FOREIGN_FLAGS = {"--get-auth-url", "--device-code", "--poll-timeout"}


def _script_flags():
    src = (SKILL_DIR / "scripts" / "netflix_import.py").read_text(encoding="utf-8")
    return set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', src))


def _prose_flags():
    """EVERY backticked --flag anywhere in the prose (invocation lines AND
    explanatory sentences — the 'add `--flag` to...' idiom is where drift
    hid on this suite's first run)."""
    return set(re.findall(r"`(--[a-z-]+)(?:=[^`]*)?`", ALL_PROSE))  # `--flag` or `--flag=value`


def test_every_prose_flag_exists_in_script_or_allowlist():
    unknown = _prose_flags() - _script_flags() - FOREIGN_FLAGS
    assert not unknown, (
        f"prose mentions flags that are neither script flags nor allowlisted "
        f"foreign-tool flags: {sorted(unknown)}")


def test_every_script_flag_is_documented():
    undocumented = _script_flags() - _prose_flags()
    assert not undocumented, (
        f"script flags never mentioned (backticked) in any prose: "
        f"{sorted(undocumented)} — document them or remove them")


def test_foreign_allowlist_is_not_stale():
    # if a foreign flag disappears from the prose, prune the allowlist
    gone = FOREIGN_FLAGS - _prose_flags()
    assert not gone, f"allowlisted flags no longer in prose: {sorted(gone)}"


def test_five_state_machine_named_in_order():
    m = re.search(r"HELLO\s*→\s*AUTH\s*→\s*EXPORT\s*→\s*IMPORT\s*→\s*SHARE", SKILL)
    assert m, "the five-state machine line changed — update probes/states together"
    for state in ["HELLO", "AUTH", "EXPORT", "IMPORT", "SHARE"]:
        assert re.search(rf"^##.*\b{state}\b", SKILL, re.M), f"no section for state {state}"


def test_probe_table_commands_reference_real_tools():
    start = SKILL.index("## Where to start")
    section = SKILL[start:SKILL.index("## ", start + 5)]
    rows = [row for row in section.splitlines() if row.startswith("|") and "`" in row]
    assert rows, "probe table missing from 'Where to start'"
    commands = " ".join(rows)
    for tool in ("fulcra_netflix.native import require_runtime", "fulcra-api user-info",
                 "fulcra-netflix viewing-history.csv --check-only --json",
                 "fulcra-netflix viewing-history.csv --json"):
        assert tool in commands, f"probe table lost its {tool} command"
    assert "fulcra-api get-records" not in commands


def _wire_example():
    doc = REFS["record-schema.md"]
    start = doc.index("## Example native record")
    block = re.search(r"```json\n(.*?)```", doc[start:], re.S)
    assert block, "native-record example block missing"
    return json.loads(block.group(1))


def test_native_example_equals_actual_encoder(ni, tmp_path):
    from fulcra_netflix.native import build_netflix_record

    source = tmp_path / "synthetic.csv"
    source.write_text("Title,Date\nSynthetic Movie,3/1/24\n", encoding="utf-8")
    event, = ni.parse_slim(source)
    actual = build_netflix_record(
        event, "Event/00000000-0000-4000-8000-000000000001", "slim")
    assert _wire_example() == actual
    assert isinstance(actual["duration_seconds"], int)
    assert "data" not in actual and "specversion" not in actual


def test_deterministic_identity_matches_documented_destination_scope(ni, fixtures_dir):
    from fulcra_netflix.native import build_netflix_record

    assert "UUIDv5" in REFS["record-schema.md"]
    source = fixtures_dir / "slim.csv"
    first = list(ni.parse_slim(source))
    second = list(ni.parse_slim(source))
    target = "Event/00000000-0000-4000-8000-000000000001"
    other = "Event/00000000-0000-4000-8000-000000000002"
    assert [event.det_id for event in first] == [event.det_id for event in second]
    assert [build_netflix_record(event, target, "slim") for event in first] == [
        build_netflix_record(event, target, "slim") for event in second]
    assert build_netflix_record(first[0], target, "slim")["id"] != (
        build_netflix_record(first[0], other, "slim")["id"])


def test_guide_explains_native_progress_and_optional_legacy_sharing():
    assert "native-v1-complete-groups" in SKILL
    assert "skipped_existing" in SKILL and "recovery" in SKILL
    assert "Only the" in SKILL and "in-flight chunk is durable" in SKILL
    assert "all annotation data, not just Netflix" in SKILL
    assert "Native Event" in SKILL and "UNKNOWN" in SKILL
    assert "personal-information" in REFS["netflix-export.md"]
