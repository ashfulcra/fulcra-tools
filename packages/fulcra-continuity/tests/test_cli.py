from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "fulcra_continuity", *args],
        check=False,
        text=True,
        capture_output=True,
    )


def test_checkpoint_command_writes_json_and_brief(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    brief_path = tmp_path / "resume.md"

    result = run_cli(
        "checkpoint",
        "--task-id",
        "TASK-123",
        "--title",
        "Migrate check-ins",
        "--objective",
        "Move parser to fulcra-coord",
        "--workstream-id",
        "openclaw:discord:main-comms",
        "--agent-id",
        "arc",
        "--coord-task-id",
        "TASK-123",
        "--coord-owner-agent",
        "openclaw:discord:main-comms",
        "--decision",
        "Avoid broad broadcasts",
        "--artifact",
        "parser.py=entry point",
        "--open-question",
        "Where is the spreadsheet source?",
        "--next",
        "Find parser",
        "--memory",
        "Use low-noise lifecycle updates|project:fulcra|90d",
        "--tag",
        "demo",
        "--out",
        str(checkpoint_path),
        "--resume-brief",
        str(brief_path),
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(checkpoint_path.read_text())
    assert data["task_id"] == "TASK-123"
    assert data["identity"]["workstream_id"] == "openclaw:discord:main-comms"
    assert data["identity"]["agent_id"] == "arc"
    assert data["identity"]["coord_task_id"] == "TASK-123"
    assert data["decisions"] == ["Avoid broad broadcasts"]
    assert data["artifacts"] == [{"path": "parser.py", "note": "entry point"}]
    assert data["memory_writes"][0]["scope"] == "project:fulcra"
    assert "Find parser" in brief_path.read_text()


def test_resume_command_prints_brief(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    run_cli(
        "checkpoint",
        "--task-id",
        "TASK-abc",
        "--title",
        "Test",
        "--objective",
        "Render brief",
        "--next",
        "Continue",
        "--out",
        str(checkpoint_path),
    )

    result = run_cli("resume", str(checkpoint_path))

    assert result.returncode == 0, result.stderr
    assert "Resume brief for TASK-abc" in result.stdout
    assert "- Continue" in result.stdout


def test_demo_command_writes_fixture(tmp_path: Path) -> None:
    result = run_cli("demo", "--out-dir", str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "context-cliff-rescue.checkpoint.json").exists()
    assert (tmp_path / "context-cliff-rescue.resume.md").exists()


def test_resume_missing_file_reports_clean_error(tmp_path: Path) -> None:
    result = run_cli("resume", str(tmp_path / "missing.json"))

    assert result.returncode == 1
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_resume_bad_json_reports_clean_error(tmp_path: Path) -> None:
    checkpoint = tmp_path / "bad.json"
    checkpoint.write_text("{not json", encoding="utf-8")

    result = run_cli("resume", str(checkpoint))

    assert result.returncode == 1
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_resume_non_dict_json_reports_clean_error(tmp_path: Path) -> None:
    # Valid JSON that is not an object (list / null / scalar) must produce a
    # clean error, not an AttributeError traceback. (PR #82 review: B1.)
    for payload in ("[1, 2, 3]", "null", "42", '"x"', "true"):
        checkpoint = tmp_path / "nondict.json"
        checkpoint.write_text(payload, encoding="utf-8")

        result = run_cli("resume", str(checkpoint))

        assert result.returncode == 1, payload
        assert "error:" in result.stderr, payload
        assert "Traceback" not in result.stderr, payload
