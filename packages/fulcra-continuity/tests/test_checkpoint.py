from __future__ import annotations

import json

from fulcra_continuity.checkpoint import (
    SCHEMA_VERSION,
    Artifact,
    MemoryWrite,
    WorkstreamIdentity,
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
        identity=WorkstreamIdentity(
            workstream_id="openclaw:discord:main-comms",
            agent_id="arc",
            coord_task_id="TASK-1",
            coord_owner_agent="openclaw:discord:main-comms",
        ),
        tags=["demo"],
    )

    data = checkpoint.to_dict()

    assert data["schema_version"] == SCHEMA_VERSION
    assert data["checkpoint_id"].startswith("CHK-20260606T140000z-task-1-")
    assert data["task_id"] == "TASK-1"
    assert "Fulcra Continuity checkpoint" in data["bootstrap_primer"]
    assert "portable resume state" in data["session_context"]
    assert data["identity"] == {
        "agent_id": "arc",
        "coord_owner_agent": "openclaw:discord:main-comms",
        "coord_task_id": "TASK-1",
        "workstream_id": "openclaw:discord:main-comms",
    }
    assert data["artifacts"] == [{"path": "README.md", "note": "demo notes"}]
    assert data["memory_writes"][0]["claim"] == "Continuity needs receipts"

    loaded = checkpoint_from_dict(json.loads(json.dumps(data)))
    assert loaded == checkpoint


def test_empty_identity_is_omitted_from_json() -> None:
    checkpoint = make_checkpoint(
        task_id="TASK-1",
        title="Keep work alive",
        objective="Resume after compaction",
        created_at="2026-06-06T14:00:00Z",
    )

    assert "identity" not in checkpoint.to_dict()


def test_resume_brief_highlights_operating_state() -> None:
    checkpoint = make_checkpoint(
        task_id="TASK-2",
        title="Context Cliff Rescue",
        objective="Prove continuity",
        created_at="2026-06-06T14:00:00Z",
        workstream_id="openclaw:discord:main-comms",
        agent_id="arc",
        coord_task_id="TASK-2",
        decisions=["Checkpoint before compaction"],
        next_actions=["Resume from checkpoint"],
    )

    brief = render_resume_brief(checkpoint)

    assert "Resume brief for TASK-2" in brief
    assert "Objective: Prove continuity" in brief
    assert "Bootstrap primer:" in brief
    assert "Fulcra Continuity checkpoint" in brief
    assert "Session context:" in brief
    assert "- Workstream: openclaw:discord:main-comms" in brief
    assert "- Agent: arc" in brief
    assert "- Coord task: TASK-2" in brief
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


def test_checkpoint_from_dict_defaults_self_contained_fields() -> None:
    checkpoint = checkpoint_from_dict(
        {
            "task_id": "TASK-1",
            "title": "x",
            "objective": "y",
            "created_at": "2026-06-06T14:00:00Z",
        }
    )

    assert "Fulcra Continuity checkpoint" in checkpoint.bootstrap_primer
    assert "portable resume state" in checkpoint.session_context


def test_checkpoint_from_dict_treats_null_defaulted_strings_as_missing() -> None:
    checkpoint = checkpoint_from_dict(
        {
            "schema_version": None,
            "checkpoint_id": None,
            "task_id": "TASK-1",
            "title": "x",
            "objective": "y",
            "created_at": "2026-06-06T14:00:00Z",
            "source": None,
            "bootstrap_primer": None,
            "session_context": None,
            "memory_writes": [
                {
                    "claim": "persist this",
                    "scope": None,
                    "source": None,
                    "ttl": None,
                    "supersedes": None,
                }
            ],
            "identity": {
                "workstream_id": None,
                "agent_id": None,
                "coord_task_id": None,
                "coord_owner_agent": None,
            },
        }
    )

    assert checkpoint.schema_version == SCHEMA_VERSION
    assert checkpoint.checkpoint_id == ""
    assert checkpoint.source == "manual"
    assert "Fulcra Continuity checkpoint" in checkpoint.bootstrap_primer
    assert "portable resume state" in checkpoint.session_context
    assert checkpoint.identity.is_empty()
    assert checkpoint.memory_writes == [
        MemoryWrite(claim="persist this", scope="task", source="checkpoint")
    ]


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


def test_checkpoint_from_dict_tolerates_scalar_and_malformed_lists() -> None:
    checkpoint = checkpoint_from_dict(
        {
            "task_id": "TASK-1",
            "title": "x",
            "objective": "y",
            "created_at": "2026-06-06T14:00:00Z",
            "decisions": "single decision",
            "artifacts": ["README.md", {"path": "plan.md", "note": "draft"}],
            "open_questions": "one question",
            "next_actions": "continue",
            "memory_writes": ["remember this", {"claim": "structured", "scope": "project"}],
            "tags": "handoff",
        }
    )

    assert checkpoint.decisions == ["single decision"]
    assert checkpoint.artifacts == [
        Artifact(path="README.md"),
        Artifact(path="plan.md", note="draft"),
    ]
    assert checkpoint.open_questions == ["one question"]
    assert checkpoint.next_actions == ["continue"]
    assert checkpoint.memory_writes == [
        MemoryWrite(claim="remember this"),
        MemoryWrite(claim="structured", scope="project"),
    ]
    assert checkpoint.tags == ["handoff"]


def test_parse_memory_write_supports_optional_fields() -> None:
    memory = parse_memory_write("claim|project:fulcra|90d|old-claim")

    assert memory.claim == "claim"
    assert memory.scope == "project:fulcra"
    assert memory.ttl == "90d"
    assert memory.supersedes == "old-claim"
