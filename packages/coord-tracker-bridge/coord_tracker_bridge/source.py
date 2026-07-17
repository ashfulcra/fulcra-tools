"""Source adapters for normalized bridge snapshots."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from .model import CapabilityState, Diagnostic, Snapshot, SourceIdentity, WorkRecord


class CommandRunner(Protocol):
    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str, str]: ...


def subprocess_runner(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    completed = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )
    return completed.returncode, completed.stdout, completed.stderr


def sanitize_text(value: Any, *, limit: int) -> str:
    """Bound source text and remove controls before it reaches a tracker payload."""

    text = "" if value is None else str(value)
    text = "".join(char if char in "\n\t" or ord(char) >= 32 else " " for char in text)
    return text[:limit]


@dataclass(frozen=True, slots=True)
class EngineCapability:
    name: str
    argv: tuple[str, ...]


class EngineSourceAdapter:
    """Read coord-engine through its JSON process boundary, capability by capability."""

    provider = "coord-engine"

    def __init__(
        self,
        team: str,
        *,
        principal: str = "ash",
        runner: CommandRunner = subprocess_runner,
        timeout: float = 180.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.team = team
        self.principal = principal
        self.runner = runner
        self.timeout = timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def source_id(self) -> str:
        return f"{self.provider}:{self.team}"

    def _capabilities(self) -> tuple[EngineCapability, ...]:
        return (
            EngineCapability("tasks", ("board", self.team)),
            EngineCapability("asks", ("asks", self.team)),
            EngineCapability("threads", ("threads", self.team, "--for", self.principal)),
            EngineCapability("health", ("health", self.team)),
        )

    def _read(self, capability: EngineCapability) -> tuple[Any | None, Diagnostic | None]:
        argv = ("coord-engine", *capability.argv, "--json")
        try:
            code, stdout, stderr = self.runner(argv, self.timeout)
            if code != 0:
                raise RuntimeError(sanitize_text(stderr, limit=300) or f"exit {code}")
            return json.loads(stdout), None
        except Exception as exc:
            # Source stderr can contain task text. Diagnostics stay useful but
            # redact payload-bearing exception strings by default.
            return None, Diagnostic(capability.name, "source-degraded", type(exc).__name__)

    @staticmethod
    def _degraded(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("type", "").endswith("degraded") or "read-degraded" in value:
                return True
            return any(EngineSourceAdapter._degraded(item) for item in value.values())
        if isinstance(value, list):
            return any(EngineSourceAdapter._degraded(item) for item in value)
        return False

    def _work_record(self, row: Mapping[str, Any], capability: str, lane: str) -> WorkRecord | None:
        item_id = sanitize_text(row.get("id") or row.get("name"), limit=500)
        if not item_id:
            return None
        tags = tuple(sanitize_text(tag, limit=100) for tag in row.get("tags") or ())
        due = row.get("due") or row.get("due_at")
        due_at = None
        if due:
            try:
                due_at = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
            except ValueError:
                due_at = None
        return WorkRecord(
            source=SourceIdentity(self.provider, f"{self.team}/{capability}", item_id),
            capability=capability,
            title=sanitize_text(row.get("title") or item_id, limit=500),
            lane=lane,
            priority=sanitize_text(row.get("priority") or "P2", limit=20),
            description=sanitize_text(row.get("description"), limit=10_000),
            owner=sanitize_text(row.get("owner"), limit=200) or None,
            assignee=sanitize_text(row.get("assignee"), limit=200) or None,
            workstream=sanitize_text(row.get("workstream"), limit=200) or None,
            origin=sanitize_text(row.get("origin"), limit=100) or "fleet",
            tags=tags,
            archived=bool(row.get("archived")),
            due_at=due_at,
        )

    def snapshot(self) -> Snapshot:
        items: list[WorkRecord] = []
        diagnostics: list[Diagnostic] = []
        states: dict[str, CapabilityState] = {
            "due_dates": CapabilityState.UNSUPPORTED,
            "expectations": CapabilityState.UNSUPPORTED,
            "command_intake": CapabilityState.UNSUPPORTED,
        }
        for capability in self._capabilities():
            payload, error = self._read(capability)
            if error is not None or self._degraded(payload):
                states[capability.name] = CapabilityState.DEGRADED
                diagnostics.append(error or Diagnostic(capability.name, "source-degraded", "degraded row"))
                continue
            states[capability.name] = CapabilityState.COMPLETE
            if capability.name == "tasks" and isinstance(payload, dict):
                for lane, rows in payload.items():
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if isinstance(row, dict):
                            record = self._work_record(row, "tasks", str(lane))
                            if record:
                                items.append(record)
            elif isinstance(payload, list):
                lane = "ask" if capability.name == "asks" else capability.name
                for row in payload:
                    if isinstance(row, dict) and not str(row.get("type", "")).endswith("degraded"):
                        record = self._work_record(row, capability.name, lane)
                        if record:
                            items.append(record)
        complete = all(state is CapabilityState.COMPLETE for name, state in states.items()
                       if name in {"tasks", "asks", "threads", "health"})
        return Snapshot(tuple(items), complete, tuple(diagnostics), states, self.clock())
