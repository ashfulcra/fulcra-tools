"""Structured continuity checkpoints for agent handoff and compaction recovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = "fulcra.continuity.checkpoint.v1"
DEFAULT_BOOTSTRAP_PRIMER = (
    "This is a Fulcra Continuity checkpoint. Resume it with "
    "`fulcra-continuity resume <checkpoint>` or read this JSON directly. "
    "Use objective, identity, decisions, artifacts, open_questions, "
    "next_actions, and memory_writes to continue without the original "
    "transcript."
)
DEFAULT_SESSION_CONTEXT = (
    "No additional session context was provided; treat this checkpoint as the "
    "portable resume state for the task named by task_id/title."
)


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
class WorkstreamIdentity:
    workstream_id: str = ""
    agent_id: str = ""
    coord_task_id: str = ""
    coord_owner_agent: str = ""

    def is_empty(self) -> bool:
        return not any((self.workstream_id, self.agent_id, self.coord_task_id, self.coord_owner_agent))


@dataclass(frozen=True)
class ContinuityCheckpoint:
    schema_version: str
    checkpoint_id: str
    task_id: str
    title: str
    objective: str
    created_at: str
    owner_agent: str = ""
    identity: WorkstreamIdentity = field(default_factory=WorkstreamIdentity)
    source: str = "manual"
    transcript_path: str = ""
    context_used_percent: int | None = None
    bootstrap_primer: str = DEFAULT_BOOTSTRAP_PRIMER
    session_context: str = DEFAULT_SESSION_CONTEXT
    decisions: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    memory_writes: list[MemoryWrite] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.identity.is_empty():
            data.pop("identity", None)
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
    bootstrap_primer: str = DEFAULT_BOOTSTRAP_PRIMER,
    session_context: str = DEFAULT_SESSION_CONTEXT,
    decisions: list[str] | None = None,
    artifacts: list[Artifact] | None = None,
    open_questions: list[str] | None = None,
    next_actions: list[str] | None = None,
    memory_writes: list[MemoryWrite] | None = None,
    tags: list[str] | None = None,
    created_at: str | None = None,
    identity: WorkstreamIdentity | None = None,
    workstream_id: str = "",
    agent_id: str = "",
    coord_task_id: str = "",
    coord_owner_agent: str = "",
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
        identity=identity
        or WorkstreamIdentity(
            workstream_id=workstream_id,
            agent_id=agent_id,
            coord_task_id=coord_task_id,
            coord_owner_agent=coord_owner_agent,
        ),
        source=source,
        transcript_path=transcript_path,
        context_used_percent=_optional_int(context_used_percent),
        bootstrap_primer=bootstrap_primer or DEFAULT_BOOTSTRAP_PRIMER,
        session_context=session_context or DEFAULT_SESSION_CONTEXT,
        decisions=decisions or [],
        artifacts=artifacts or [],
        open_questions=open_questions or [],
        next_actions=next_actions or [],
        memory_writes=memory_writes or [],
        tags=tags or [],
    )


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _coerce_str_list(value: Any) -> list[str]:
    return [str(item) for item in _coerce_list(value)]


def _str_or_default(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value) or default


def checkpoint_from_dict(data: dict[str, Any]) -> ContinuityCheckpoint:
    # Valid JSON that isn't an object (a list, null, or a scalar) would
    # otherwise reach `data.get(...)` below and raise AttributeError/TypeError
    # — an uncaught traceback out of the CLI. Reject it with a clean ValueError
    # (which main() turns into an `error:` line + exit 1). (PR #82 review: B1.)
    if not isinstance(data, dict):
        raise ValueError(
            f"checkpoint must be a JSON object, got {type(data).__name__}"
        )
    artifacts = [
        Artifact(
            path=str(item.get("path", "")) if isinstance(item, dict) else str(item),
            note=str(item.get("note", "")) if isinstance(item, dict) else "",
        )
        for item in _coerce_list(data.get("artifacts"))
    ]
    memory_writes = [
        (
            MemoryWrite(
                claim=_str_or_default(item.get("claim"), ""),
                scope=_str_or_default(item.get("scope"), "task"),
                source=_str_or_default(item.get("source"), "checkpoint"),
                ttl=_str_or_default(item.get("ttl"), ""),
                supersedes=_str_or_default(item.get("supersedes"), ""),
            )
            if isinstance(item, dict)
            else MemoryWrite(claim=str(item))
        )
        for item in _coerce_list(data.get("memory_writes"))
    ]
    identity_data = data.get("identity", {})
    if not isinstance(identity_data, dict):
        identity_data = {}
    identity = WorkstreamIdentity(
        workstream_id=_str_or_default(identity_data.get("workstream_id"), ""),
        agent_id=_str_or_default(identity_data.get("agent_id"), ""),
        coord_task_id=_str_or_default(identity_data.get("coord_task_id"), ""),
        coord_owner_agent=_str_or_default(identity_data.get("coord_owner_agent"), ""),
    )
    return ContinuityCheckpoint(
        schema_version=_str_or_default(data.get("schema_version"), SCHEMA_VERSION),
        checkpoint_id=_str_or_default(data.get("checkpoint_id"), ""),
        task_id=_str_or_default(data.get("task_id"), ""),
        title=_str_or_default(data.get("title"), ""),
        objective=_str_or_default(data.get("objective"), ""),
        created_at=_str_or_default(data.get("created_at"), ""),
        owner_agent=_str_or_default(data.get("owner_agent"), ""),
        identity=identity,
        source=_str_or_default(data.get("source"), "manual"),
        transcript_path=_str_or_default(data.get("transcript_path"), ""),
        context_used_percent=_optional_int(data.get("context_used_percent")),
        bootstrap_primer=_str_or_default(
            data.get("bootstrap_primer"), DEFAULT_BOOTSTRAP_PRIMER),
        session_context=_str_or_default(
            data.get("session_context"), DEFAULT_SESSION_CONTEXT),
        decisions=_coerce_str_list(data.get("decisions")),
        artifacts=artifacts,
        open_questions=_coerce_str_list(data.get("open_questions")),
        next_actions=_coerce_str_list(data.get("next_actions")),
        memory_writes=memory_writes,
        tags=_coerce_str_list(data.get("tags")),
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
    if not checkpoint.identity.is_empty():
        lines.append("Identity:")
        if checkpoint.identity.workstream_id:
            lines.append(f"- Workstream: {checkpoint.identity.workstream_id}")
        if checkpoint.identity.agent_id:
            lines.append(f"- Agent: {checkpoint.identity.agent_id}")
        if checkpoint.identity.coord_task_id:
            lines.append(f"- Coord task: {checkpoint.identity.coord_task_id}")
        if checkpoint.identity.coord_owner_agent:
            lines.append(f"- Coord owner: {checkpoint.identity.coord_owner_agent}")
    if checkpoint.context_used_percent is not None:
        lines.append(f"Context used: {checkpoint.context_used_percent}%")
    if checkpoint.transcript_path:
        lines.append(f"Transcript: {checkpoint.transcript_path}")
    if checkpoint.bootstrap_primer:
        lines.append("")
        lines.append("Bootstrap primer:")
        lines.append(checkpoint.bootstrap_primer)
    if checkpoint.session_context:
        lines.append("")
        lines.append("Session context:")
        lines.append(checkpoint.session_context)

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
        title="Migrate daily check-ins onto coord-engine",
        objective=(
            "Move a spreadsheet-backed daily check-in parser into a low-noise "
            "coord-engine task lifecycle without losing task state during handoff."
        ),
        owner_agent="openclaw:discord:main-comms",
        workstream_id="openclaw:discord:main-comms",
        agent_id="arc",
        coord_task_id="TASK-demo-context-cliff-rescue",
        coord_owner_agent="openclaw:discord:main-comms",
        source="demo",
        context_used_percent=82,
        session_context=(
            "Demo checkpoint for a context-cliff handoff: the next agent should "
            "continue the migration from the structured state below, not from "
            "the original chat transcript."
        ),
        decisions=[
            "Use task lifecycle updates instead of broadcast messages.",
            "Keep spreadsheet parsing audit separate from bus write implementation.",
            "Checkpoint before compaction with decisions, artifacts, and next actions.",
        ],
        artifacts=[
            Artifact(path="packages/coord-engine/README.md", note="coordination CLI behavior"),
            Artifact(path="memory/2026-06-06.md", note="operator request trail"),
        ],
        open_questions=[
            "Which existing parser owns the Slack daily check-in spreadsheet?",
            "Should migration write only lifecycle tasks or also Agent Tasks annotations?",
        ],
        next_actions=[
            "Find the current spreadsheet parser entry point.",
            "Map parser output fields to coord-engine task create/update/done lifecycle fields.",
            "Run one dry-run migration and verify no Discord broadcast noise.",
        ],
        memory_writes=[
            MemoryWrite(
                claim="Slack daily check-in migration should avoid broad broadcasts.",
                scope="project:fulcra",
                ttl="90d",
            ),
        ],
        tags=["demo", "context-cliff-rescue", "coord-engine"],
    )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
