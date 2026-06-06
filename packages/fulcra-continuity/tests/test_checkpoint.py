from __future__ import annotations

import json

from fulcra_continuity.checkpoint import (
    SCHEMA_VERSION,
    Artifact,
    MemoryWrite,
    checkpoint_from_dict,
    make_checkpoint,
    parse_memory_write,
    render_resume_brief,
)


def test_make_checkpoint_round_trips_to_dict() -> None:
    checkpoint = make_checkpoint(
        task_id="TASK-1",
        title="Keep work alive",
        objective="Resume after compaction",
        created_at="2026-06-06T14:00:00Z",
        decisions=["Use structured checkpoint"],
        artifacts=[Artifact(path="README.md", note="demo notes")],
        open_questions=["Who owns the next action?"],
        next_actions=["Run tests"],
        memory_writes=[MemoryWrite(claim="Continuity needs receipts", ttl="30d")],
        tags=["demo"],
    )

    data = checkpoint.to_dict()

    assert data["schema_version"] == SCHEMA_VERSION
    assert data["checkpoint_id"].startswith("CHK-20260606T140000z-task-1-")
    assert data["task_id"] == "TASK-1"
    assert data["artifacts"] == [{"path": "README.md", "note": "demo notes"}]
    assert data["memory_writes"][0]["claim"] == "Continuity needs receipts"

    loaded = checkpoint_from_dict(json.loads(json.dumps(data)))
    assert loaded == checkpoint


def test_resume_brief_highlights_operating_state() -> None:
    checkpoint = make_checkpoint(
        task_id="TASK-2",
        title="Context Cliff Rescue",
        objective="Prove continuity",
        created_at="2026-06-06T14:00:00Z",
        decisions=["Checkpoint before compaction"],
        next_actions=["Resume from checkpoint"],
    )

    brief = render_resume_brief(checkpoint)

    assert "Resume brief for TASK-2" in brief
    assert "Objective: Prove continuity" in brief
    assert "- Checkpoint before compaction" in brief
    assert "- Resume from checkpoint" in brief


def test_checkpoint_id_slug_is_ascii_and_collision_resistant() -> None:
    first = make_checkpoint(
        task_id="TASK-你好-1",
        title="Unicode",
        objective="Keep IDs portable",
        created_at="2026-06-06T14:00:00Z",
    )
    second = make_checkpoint(
        task_id="TASK-你好-1",
        title="Unicode",
        objective="Keep IDs portable",
        created_at="2026-06-06T14:00:00Z",
    )

    assert first.checkpoint_id.startswith("CHK-20260606T140000z-task-1-")
    assert first.checkpoint_id.isascii()
    assert first.checkpoint_id != second.checkpoint_id


def test_checkpoint_from_dict_coerces_context_percent() -> None:
    checkpoint = checkpoint_from_dict(
        {
            "task_id": "TASK-1",
            "title": "x",
            "objective": "y",
            "created_at": "2026-06-06T14:00:00Z",
            "context_used_percent": "82",
        }
    )

    assert checkpoint.context_used_percent == 82


def test_checkpoint_from_dict_ignores_invalid_context_percent() -> None:
    checkpoint = checkpoint_from_dict(
        {
            "task_id": "TASK-1",
            "title": "x",
            "objective": "y",
            "created_at": "2026-06-06T14:00:00Z",
            "context_used_percent": "full",
        }
    )

    assert checkpoint.context_used_percent is None


def test_parse_memory_write_supports_optional_fields() -> None:
    memory = parse_memory_write("claim|project:fulcra|90d|old-claim")

    assert memory.claim == "claim"
    assert memory.scope == "project:fulcra"
    assert memory.ttl == "90d"
    assert memory.supersedes == "old-claim"
