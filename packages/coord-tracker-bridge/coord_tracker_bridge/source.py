"""Source adapters for normalized bridge snapshots."""

from __future__ import annotations

import hashlib
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


class TeamsTransportError(RuntimeError):
    pass


class TeamsTransport(Protocol):
    def list_dir(self, prefix: str) -> list[Mapping[str, Any]]: ...

    def read(self, path: str) -> str | None: ...


class FulcraTeamsTransport:
    """Read-only, bounded Fulcra Files transport for the teams adapter."""

    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess_runner,
        timeout: float = 30.0,
        command: tuple[str, ...] = ("fulcra-api",),
    ) -> None:
        self.runner = runner
        self.timeout = timeout
        self.command = command

    def list_dir(self, prefix: str) -> list[Mapping[str, Any]]:
        code, stdout, _stderr = self.runner(
            (*self.command, "file", "list", prefix), self.timeout
        )
        if code != 0:
            raise TeamsTransportError(f"list {prefix!r} failed")
        entries: list[Mapping[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) == 1 and parts[0].endswith("/"):
                entries.append({"name": parts[0], "is_dir": True})
                continue
            if len(parts) < 5:
                raise TeamsTransportError(f"ambiguous list response for {prefix!r}")
            name = " ".join(parts[4:])
            entries.append({
                "name": name,
                "size": parts[0],
                "mtime": " ".join(parts[1:4]),
                "is_dir": name.endswith("/"),
            })
        return sorted(entries, key=lambda entry: str(entry.get("name") or ""))

    def read(self, path: str) -> str | None:
        code, stdout, _stderr = self.runner(
            (*self.command, "file", "download", path, "-"), self.timeout
        )
        return stdout if code == 0 else None


def _parse_teams_frontmatter(text: str) -> Mapping[str, Any] | None:
    """Parse the strict scalar/list subset used by teams concept documents."""

    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return None
    result: dict[str, Any] = {}
    index = 1
    while index < end:
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if line[:1].isspace() or ":" not in line:
            return None
        key, raw = line.split(":", 1)
        key = key.strip()
        if not key or key in result:
            return None
        raw = raw.strip()
        if raw == "":
            values: list[str] = []
            index += 1
            while index < end and lines[index][:1].isspace():
                item = lines[index].strip()
                if not item.startswith("- "):
                    return None
                values.append(item[2:].strip().strip("\"'"))
                index += 1
            result[key] = values
            continue
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            result[key] = [
                value.strip().strip("\"'") for value in inner.split(",") if value.strip()
            ]
        elif raw.lower() in {"true", "false"}:
            result[key] = raw.lower() == "true"
        elif raw.lower() in {"null", "~"}:
            result[key] = None
        elif raw in {"|", "|-", "|+"}:
            block: list[str] = []
            index += 1
            while index < end and lines[index][:1].isspace():
                block.append(lines[index][2:] if lines[index].startswith("  ") else lines[index].lstrip())
                index += 1
            result[key] = "\n".join(block)
            continue
        else:
            result[key] = raw.strip("\"'")
        index += 1
    return result


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


class TeamsSourceAdapter:
    """Strict read-only source over the base teams file convention.

    Only typed task concept documents under ``team/<team>/task/`` are read.
    Derived indexes and logs are ignored, and no arbitrary prose is inferred.
    """

    provider = "teams"
    VALID_LANES = frozenset({"proposed", "active", "waiting", "blocked", "done", "abandoned"})
    DERIVED_FILES = frozenset({"index.md", "log.md"})

    def __init__(
        self,
        team: str,
        *,
        transport: TeamsTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        max_files: int = 1_000,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        self.team = team
        self.transport = transport or FulcraTeamsTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_files = max_files

    @property
    def source_id(self) -> str:
        return f"{self.provider}:{self.team}"

    @property
    def task_root(self) -> str:
        return f"team/{self.team}/task/"

    @staticmethod
    def _diagnostic(path: str, code: str) -> Diagnostic:
        return Diagnostic("tasks", code, sanitize_text(path, limit=500))

    def _record(self, name: str, document: str) -> tuple[WorkRecord | None, Diagnostic | None]:
        fields = _parse_teams_frontmatter(document)
        path = f"{self.task_root}{name}"
        if fields is None:
            return None, self._diagnostic(path, "teams-parse-degraded")
        if fields.get("type") != "Task":
            return None, self._diagnostic(path, "teams-type-degraded")
        item_id = sanitize_text(fields.get("id"), limit=500).strip()
        title = sanitize_text(fields.get("title"), limit=500).strip()
        lane = sanitize_text(fields.get("status"), limit=50).strip()
        tags = fields.get("tags")
        if not item_id or not title or lane not in self.VALID_LANES or not isinstance(tags, list):
            return None, self._diagnostic(path, "teams-schema-degraded")
        if any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            return None, self._diagnostic(path, "teams-schema-degraded")
        archived = fields.get("archived", False)
        if not isinstance(archived, bool):
            return None, self._diagnostic(path, "teams-schema-degraded")
        origin = sanitize_text(fields.get("origin"), limit=100).strip()
        workstream = sanitize_text(fields.get("workstream"), limit=200).strip()
        return WorkRecord(
            source=SourceIdentity(self.provider, f"{self.team}/tasks", item_id),
            capability="tasks",
            title=title,
            lane=lane,
            priority=sanitize_text(fields.get("priority") or "P2", limit=20),
            description=sanitize_text(fields.get("description"), limit=10_000),
            owner=sanitize_text(fields.get("owner"), limit=200) or None,
            assignee=sanitize_text(fields.get("assignee"), limit=200) or None,
            workstream=workstream or None,
            origin=origin or "fleet",
            tags=tuple(sanitize_text(tag, limit=100) for tag in tags),
            archived=archived,
            due_at=None,
        ), None

    def snapshot(self) -> Snapshot:
        unsupported = {
            name: CapabilityState.UNSUPPORTED
            for name in (
                "asks", "threads", "health", "due_dates",
                "expectations", "command_intake",
            )
        }
        diagnostics: list[Diagnostic] = []
        items: list[WorkRecord] = []
        revision_parts: list[str] = []
        degraded = False
        try:
            entries = self.transport.list_dir(self.task_root)
        except Exception:
            entries = []
            degraded = True
            diagnostics.append(self._diagnostic(self.task_root, "teams-list-degraded"))
        if len(entries) > self.max_files:
            degraded = True
            diagnostics.append(self._diagnostic(self.task_root, "teams-list-truncated"))
            entries = entries[:self.max_files]
        seen_ids: set[str] = set()
        for entry in entries:
            name = str(entry.get("name") or "")
            revision_parts.append(
                f"{name}\0{entry.get('size') or ''}\0{entry.get('mtime') or ''}"
            )
            if name in self.DERIVED_FILES:
                continue
            if not name.endswith(".md") or bool(entry.get("is_dir")) or "/" in name:
                degraded = True
                diagnostics.append(self._diagnostic(f"{self.task_root}{name}", "teams-entry-degraded"))
                continue
            path = f"{self.task_root}{name}"
            try:
                document = self.transport.read(path)
            except Exception:
                document = None
            if document is None:
                degraded = True
                diagnostics.append(self._diagnostic(path, "teams-read-degraded"))
                continue
            revision_parts.append(hashlib.sha256(document.encode()).hexdigest())
            record, error = self._record(name, document)
            if error is not None:
                degraded = True
                diagnostics.append(error)
                continue
            assert record is not None
            if record.source.item_id in seen_ids:
                degraded = True
                diagnostics.append(self._diagnostic(path, "teams-duplicate-id-degraded"))
                continue
            seen_ids.add(record.source.item_id)
            items.append(record)
        states = {"tasks": CapabilityState.DEGRADED if degraded else CapabilityState.COMPLETE}
        states.update(unsupported)
        revision = hashlib.sha256("\n".join(revision_parts).encode()).hexdigest()
        return Snapshot(
            tuple(items),
            not degraded,
            tuple(diagnostics),
            states,
            self.clock(),
            source_revision=revision,
        )
