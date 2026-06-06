"""Structured continuity checkpoints for agent handoff and compaction recovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = "fulcra.continuity.checkpoint.v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Artifact:
    path: str
    note: str = ""


@dataclass(frozen=True)
class MemoryWrite:
    claim: str
    scope: str = "task"
    source: str = "checkpoint"
    ttl: str = ""
    supersedes: str = ""


@dataclass(frozen=True)
class ContinuityCheckpoint:
    schema_version: str
    checkpoint_id: str
    task_id: str
    title: str
    objective: str
    created_at: str
    owner_agent: str = ""
    source: str = "manual"
    transcript_path: str = ""
    context_used_percent: int | None = None
    decisions: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    memory_writes: list[MemoryWrite] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["artifacts"] = [asdict(item) for item in self.artifacts]
        data["memory_writes"] = [asdict(item) for item in self.memory_writes]
        return data


def _slug(value: str) -> str:
    keep = []
    for ch in value.lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            keep.append(ch)
        elif keep and keep[-1] != "-":
            keep.append("-")
    return "".join(keep).strip("-")[:48] or "checkpoint"


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def make_checkpoint(
    *,
    task_id: str,
    title: str,
    objective: str,
    owner_agent: str = "",
    source: str = "manual",
    transcript_path: str = "",
    context_used_percent: int | None = None,
    decisions: list[str] | None = None,
    artifacts: list[Artifact] | None = None,
    open_questions: list[str] | None = None,
    next_actions: list[str] | None = None,
    memory_writes: list[MemoryWrite] | None = None,
    tags: list[str] | None = None,
    created_at: str | None = None,
) -> ContinuityCheckpoint:
    """Build a checkpoint with collision-resistant IDs for demos and logs."""
    created = created_at or utc_now_iso()
    stamp = created.replace(":", "").replace("-", "").replace("Z", "z")
    checkpoint_id = f"CHK-{stamp}-{_slug(task_id or title)}-{uuid4().hex[:8]}"
    return ContinuityCheckpoint(
        schema_version=SCHEMA_VERSION,
        checkpoint_id=checkpoint_id,
        task_id=task_id,
        title=title,
        objective=objective,
        created_at=created,
        owner_agent=owner_agent,
        source=source,
        transcript_path=transcript_path,
        context_used_percent=_optional_int(context_used_percent),
        decisions=decisions or [],
        artifacts=artifacts or [],
        open_questions=open_questions or [],
        next_actions=next_actions or [],
        memory_writes=memory_writes or [],
        tags=tags or [],
    )


def checkpoint_from_dict(data: dict[str, Any]) -> ContinuityCheckpoint:
    artifacts = [
        Artifact(path=str(item.get("path", "")), note=str(item.get("note", "")))
        for item in data.get("artifacts", [])
    ]
    memory_writes = [
        MemoryWrite(
            claim=str(item.get("claim", "")),
            scope=str(item.get("scope", "task")),
            source=str(item.get("source", "checkpoint")),
            ttl=str(item.get("ttl", "")),
            supersedes=str(item.get("supersedes", "")),
        )
        for item in data.get("memory_writes", [])
    ]
    return ContinuityCheckpoint(
        schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
        checkpoint_id=str(data.get("checkpoint_id", "")),
        task_id=str(data.get("task_id", "")),
        title=str(data.get("title", "")),
        objective=str(data.get("objective", "")),
        created_at=str(data.get("created_at", "")),
        owner_agent=str(data.get("owner_agent", "")),
        source=str(data.get("source", "manual")),
        transcript_path=str(data.get("transcript_path", "")),
        context_used_percent=_optional_int(data.get("context_used_percent")),
        decisions=[str(item) for item in data.get("decisions", [])],
        artifacts=artifacts,
        open_questions=[str(item) for item in data.get("open_questions", [])],
        next_actions=[str(item) for item in data.get("next_actions", [])],
        memory_writes=memory_writes,
        tags=[str(item) for item in data.get("tags", [])],
    )


def parse_artifact(value: str) -> Artifact:
    if "=" not in value:
        return Artifact(path=value)
    path, note = value.split("=", 1)
    return Artifact(path=path.strip(), note=note.strip())


def parse_memory_write(value: str) -> MemoryWrite:
    parts = [part.strip() for part in value.split("|")]
    claim = parts[0] if parts else ""
    scope = parts[1] if len(parts) > 1 and parts[1] else "task"
    ttl = parts[2] if len(parts) > 2 else ""
    supersedes = parts[3] if len(parts) > 3 else ""
    return MemoryWrite(claim=claim, scope=scope, ttl=ttl, supersedes=supersedes)


def render_resume_brief(checkpoint: ContinuityCheckpoint) -> str:
    lines = [
        f"Resume brief for {checkpoint.task_id or checkpoint.checkpoint_id}",
        f"Title: {checkpoint.title}",
        f"Objective: {checkpoint.objective}",
        f"Checkpoint: {checkpoint.checkpoint_id} at {checkpoint.created_at}",
    ]
    if checkpoint.owner_agent:
        lines.append(f"Owner agent: {checkpoint.owner_agent}")
    if checkpoint.context_used_percent is not None:
        lines.append(f"Context used: {checkpoint.context_used_percent}%")
    if checkpoint.transcript_path:
        lines.append(f"Transcript: {checkpoint.transcript_path}")

    def section(name: str, items: list[str]) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"{name}:")
        for item in items:
            lines.append(f"- {item}")

    section("Decisions", checkpoint.decisions)
    if checkpoint.artifacts:
        lines.append("")
        lines.append("Artifacts:")
        for artifact in checkpoint.artifacts:
            note = f" — {artifact.note}" if artifact.note else ""
            lines.append(f"- {artifact.path}{note}")
    section("Open questions", checkpoint.open_questions)
    section("Next actions", checkpoint.next_actions)
    if checkpoint.memory_writes:
        lines.append("")
        lines.append("Memory writes:")
        for memory in checkpoint.memory_writes:
            detail = f"scope={memory.scope}"
            if memory.ttl:
                detail += f", ttl={memory.ttl}"
            if memory.supersedes:
                detail += f", supersedes={memory.supersedes}"
            lines.append(f"- {memory.claim} ({detail})")
    return "\n".join(lines) + "\n"


def default_demo_checkpoint() -> ContinuityCheckpoint:
    return make_checkpoint(
        task_id="TASK-demo-context-cliff-rescue",
        title="Migrate daily check-ins onto fulcra-coord",
        objective=(
            "Move a spreadsheet-backed daily check-in parser into a low-noise "
            "fulcra-coord lifecycle flow without losing task state during handoff."
        ),
        owner_agent="openclaw:discord:main-comms",
        source="demo",
        context_used_percent=82,
        decisions=[
            "Use task lifecycle updates instead of broadcast messages.",
            "Keep spreadsheet parsing audit separate from bus write implementation.",
            "Checkpoint before compaction with decisions, artifacts, and next actions.",
        ],
        artifacts=[
            Artifact(path="packages/fulcra-coord/README.md", note="coordination CLI behavior"),
            Artifact(path="memory/2026-06-06.md", note="operator request trail"),
        ],
        open_questions=[
            "Which existing parser owns the Slack daily check-in spreadsheet?",
            "Should migration write only lifecycle tasks or also Agent Tasks annotations?",
        ],
        next_actions=[
            "Find the current spreadsheet parser entry point.",
            "Map parser output fields to fulcra-coord start/update/done lifecycle fields.",
            "Run one dry-run migration and verify no Discord broadcast noise.",
        ],
        memory_writes=[
            MemoryWrite(
                claim="Slack daily check-in migration should avoid broad broadcasts.",
                scope="project:fulcra",
                ttl="90d",
            ),
        ],
        tags=["demo", "context-cliff-rescue", "fulcra-coord"],
    )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
