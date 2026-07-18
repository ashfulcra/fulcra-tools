"""Source adapters for normalized bridge snapshots."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait
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
        return self.list_dir_bounded(prefix, timeout=self.timeout)

    def list_dir_bounded(self, prefix: str, *, timeout: float) -> list[Mapping[str, Any]]:
        code, stdout, _stderr = self.runner(
            (*self.command, "file", "list", prefix), min(self.timeout, timeout)
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

    def read_many(
        self,
        paths: Sequence[str],
        *,
        timeout: float,
        max_workers: int,
    ) -> tuple[Mapping[str, str | None], bool]:
        """Download a bounded concurrent batch under one aggregate deadline."""

        deadline = time.monotonic() + max(timeout, 0.0)

        def read_one(path: str) -> str | None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            code, stdout, _stderr = self.runner(
                (*self.command, "file", "download", path, "-"),
                min(self.timeout, remaining),
            )
            return stdout if code == 0 else None

        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {executor.submit(read_one, path): path for path in paths}
        done, pending = wait(futures, timeout=max(timeout, 0.0))
        for future in pending:
            future.cancel()
        values: dict[str, str | None] = {}
        for future in done:
            path = futures[future]
            try:
                values[path] = future.result()
            except Exception:
                values[path] = None
        for future in pending:
            values[futures[future]] = None
        executor.shutdown(wait=False, cancel_futures=True)
        complete = not pending
        return values, complete


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
    timeout: float | None = None


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
        health_timeout: float = 360.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout <= 0 or health_timeout <= 0:
            raise ValueError("source timeouts must be positive")
        self.team = team
        self.principal = principal
        self.runner = runner
        self.timeout = timeout
        self.health_timeout = health_timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def source_id(self) -> str:
        return f"{self.provider}:{self.team}"

    def resolve_legacy_slug(self, slug: str) -> WorkRecord | None:
        """Resolve one hot or archived task slug for one-time tracker adoption."""

        payload, error = self._read(
            EngineCapability("tasks", ("search", self.team, slug, "--archived"))
        )
        if (
            error is not None
            or not isinstance(payload, list)
            or self._degraded_evidence(payload) is not None
        ):
            raise ValueError(f"legacy slug lookup failed for {slug!r}")
        matches = [
            row
            for row in payload
            if isinstance(row, dict)
            and str(row.get("id") or row.get("name") or "") == slug
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError(f"legacy slug lookup is ambiguous for {slug!r}")
        row = matches[0]
        record = self._work_record(row, "tasks", str(row.get("status") or "unknown"))
        if record is None:
            raise ValueError(f"legacy slug lookup returned an invalid row for {slug!r}")
        return record

    def resolve_legacy_slugs(
        self, slugs: Sequence[str]
    ) -> dict[str, WorkRecord | None]:
        """Resolve legacy slugs concurrently for the one-time adoption gate."""

        ordered = tuple(dict.fromkeys(slugs))
        if not ordered:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(ordered))) as pool:
            futures = {
                slug: pool.submit(self.resolve_legacy_slug, slug) for slug in ordered
            }
            return {slug: futures[slug].result() for slug in ordered}

    def _capabilities(self) -> tuple[EngineCapability, ...]:
        return (
            EngineCapability("tasks", ("board", self.team)),
            EngineCapability("asks", ("asks", self.team)),
            EngineCapability("threads", ("threads", self.team, "--for", self.principal)),
            # Fleet health is intentionally slow: the live 13-host fold takes
            # 2-5 minutes, so it gets a separate bounded allowance.
            EngineCapability("health", ("health", self.team), self.health_timeout),
        )

    def _read(self, capability: EngineCapability) -> tuple[Any | None, Diagnostic | None]:
        argv = ("coord-engine", *capability.argv, "--json")
        try:
            code, stdout, stderr = self.runner(argv, capability.timeout or self.timeout)
            if code != 0:
                raise RuntimeError(sanitize_text(stderr, limit=300) or f"exit {code}")
            try:
                return json.loads(stdout), None
            except json.JSONDecodeError:
                # Some engine folds (notably `threads --json`) are JSONL, not
                # one JSON document. Preserve every valid ordered row. Engine
                # budget markers can also arrive as prose lines interleaved in
                # stdout; retain the rows but degrade the capability loudly.
                lines = [line for line in stdout.splitlines() if line.strip()]
                if not lines:
                    raise
                values: list[Any] = []
                degraded: list[str] = []
                for line_number, line in enumerate(lines, 1):
                    try:
                        values.append(json.loads(line))
                    except json.JSONDecodeError:
                        degraded.append(
                            f"line {line_number}: {sanitize_text(line, limit=300)}"
                        )
                if not values:
                    raise
                payload: Any = values[0] if len(values) == 1 else values
                diagnostic = None
                if degraded:
                    diagnostic = Diagnostic(
                        capability.name,
                        "source-line-degraded",
                        sanitize_text("; ".join(degraded), limit=500),
                    )
                return payload, diagnostic
        except Exception as exc:
            # Source stderr can contain task text. Diagnostics stay useful but
            # redact payload-bearing exception strings by default.
            return None, Diagnostic(capability.name, "source-degraded", type(exc).__name__)

    @staticmethod
    def _degraded_evidence(value: Any, path: str = "$") -> str | None:
        if isinstance(value, dict):
            marker_type = str(value.get("type", ""))
            if marker_type.endswith("degraded"):
                reason = sanitize_text(value.get("reason"), limit=240)
                return f"{path}: type={marker_type}" + (f" reason={reason}" if reason else "")
            if "read-degraded" in value:
                marker = value["read-degraded"]
                reason = sanitize_text(marker.get("reason"), limit=240) if isinstance(marker, dict) else sanitize_text(marker, limit=240)
                return f"{path}.read-degraded" + (f": {reason}" if reason else "")
            for key, item in value.items():
                evidence = EngineSourceAdapter._degraded_evidence(item, f"{path}.{key}")
                if evidence:
                    return evidence
        if isinstance(value, list):
            for index, item in enumerate(value):
                evidence = EngineSourceAdapter._degraded_evidence(item, f"{path}[{index}]")
                if evidence:
                    return evidence
        return None

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
            degraded_evidence = self._degraded_evidence(payload)
            if payload is None or degraded_evidence:
                states[capability.name] = CapabilityState.DEGRADED
                diagnostics.append(error or Diagnostic(
                    capability.name, "source-degraded",
                    sanitize_text(degraded_evidence, limit=500),
                ))
                continue
            normalized: list[WorkRecord] = []
            normalization_error: str | None = None
            if capability.name == "tasks" and isinstance(payload, dict):
                for lane, rows in payload.items():
                    if not isinstance(rows, list):
                        normalization_error = f"$.{lane}: expected list, got {type(rows).__name__}"
                        break
                    for index, row in enumerate(rows):
                        path = f"$.{lane}[{index}]"
                        if not isinstance(row, dict):
                            normalization_error = f"{path}: expected object, got {type(row).__name__}"
                            break
                        try:
                            derived_lane = str(lane)
                            if (
                                derived_lane in {"proposed", "waiting"}
                                and sanitize_text(row.get("assignee"), limit=200) == "@backlog"
                            ):
                                derived_lane = "backlog"
                            record = self._work_record(row, "tasks", derived_lane)
                        except (TypeError, ValueError) as exc:
                            normalization_error = f"{path}: {type(exc).__name__}"
                            break
                        if record is None:
                            normalization_error = f"{path}: missing stable id/name"
                            break
                        normalized.append(record)
                    if normalization_error:
                        break
            elif capability.name == "tasks":
                normalization_error = f"$: expected object, got {type(payload).__name__}"
            elif capability.name == "health" and isinstance(payload, dict):
                hosts = payload.get("hosts")
                if not isinstance(hosts, list):
                    normalization_error = f"$.hosts: expected list, got {type(hosts).__name__}"
                else:
                    for index, row in enumerate(hosts):
                        path = f"$.hosts[{index}]"
                        if not isinstance(row, dict):
                            normalization_error = f"{path}: expected object, got {type(row).__name__}"
                            break
                        host = sanitize_text(row.get("host"), limit=500)
                        if not host.strip():
                            normalization_error = f"{path}: missing stable host"
                            break
                        normalized_row = dict(row)
                        normalized_row["id"] = host
                        normalized_row.setdefault("title", host)
                        try:
                            record = self._work_record(normalized_row, "health", "health")
                        except (TypeError, ValueError) as exc:
                            normalization_error = f"{path}: {type(exc).__name__}"
                            break
                        if record is None:
                            normalization_error = f"{path}: missing stable host"
                            break
                        normalized.append(record)
            elif capability.name == "health":
                normalization_error = f"$: expected object, got {type(payload).__name__}"
            elif isinstance(payload, list):
                lane = {
                    "asks": "asks",
                    "threads": "threads-missed",
                }.get(capability.name, capability.name)
                for index, row in enumerate(payload):
                    path = f"$[{index}]"
                    if not isinstance(row, dict):
                        normalization_error = f"{path}: expected object, got {type(row).__name__}"
                        break
                    try:
                        record = self._work_record(row, capability.name, lane)
                    except (TypeError, ValueError) as exc:
                        normalization_error = f"{path}: {type(exc).__name__}"
                        break
                    if record is None:
                        normalization_error = f"{path}: missing stable id/name"
                        break
                    normalized.append(record)
            else:
                normalization_error = f"$: expected list, got {type(payload).__name__}"
            items.extend(normalized)
            if normalization_error or error is not None:
                states[capability.name] = CapabilityState.DEGRADED
                if error is not None:
                    diagnostics.append(error)
                if normalization_error:
                    diagnostics.append(Diagnostic(
                        capability.name,
                        "source-schema-degraded",
                        sanitize_text(normalization_error, limit=500),
                    ))
            else:
                states[capability.name] = CapabilityState.COMPLETE
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
        snapshot_timeout: float = 30.0,
        read_workers: int = 32,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_files < 1 or snapshot_timeout <= 0 or read_workers < 1:
            raise ValueError("max_files, snapshot_timeout, and read_workers must be positive")
        self.team = team
        self.transport = transport or FulcraTeamsTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_files = max_files
        self.snapshot_timeout = snapshot_timeout
        self.read_workers = read_workers
        self.monotonic = monotonic

    @property
    def source_id(self) -> str:
        return f"{self.provider}:{self.team}"

    @property
    def task_root(self) -> str:
        return f"team/{self.team}/task/"

    @staticmethod
    def _diagnostic(path: str, code: str) -> Diagnostic:
        return Diagnostic("tasks", code, sanitize_text(path, limit=500))

    def _record(
        self, name: str, document: str
    ) -> tuple[WorkRecord | None, Diagnostic | None, str | None]:
        fields = _parse_teams_frontmatter(document)
        path = f"{self.task_root}{name}"
        if fields is None:
            return None, self._diagnostic(path, "teams-parse-degraded"), None
        if fields.get("type") != "Task":
            return None, self._diagnostic(path, "teams-type-degraded"), None
        item_id = sanitize_text(fields.get("id"), limit=500).strip()
        claimed_id = item_id or None
        title = sanitize_text(fields.get("title"), limit=500).strip()
        lane = sanitize_text(fields.get("status"), limit=50).strip()
        tags = fields.get("tags")
        if not item_id or not title or lane not in self.VALID_LANES or not isinstance(tags, list):
            return None, self._diagnostic(path, "teams-schema-degraded"), claimed_id
        if any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            return None, self._diagnostic(path, "teams-schema-degraded"), claimed_id
        archived = fields.get("archived", False)
        if not isinstance(archived, bool):
            return None, self._diagnostic(path, "teams-schema-degraded"), claimed_id
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
        ), None, claimed_id

    def snapshot(self) -> Snapshot:
        deadline = self.monotonic() + self.snapshot_timeout
        unsupported = {
            name: CapabilityState.UNSUPPORTED
            for name in (
                "asks", "threads", "health", "due_dates",
                "expectations", "command_intake",
            )
        }
        diagnostics: list[Diagnostic] = []
        records_by_id: dict[str, WorkRecord] = {}
        claimed_ids: set[str] = set()
        colliding_ids: set[str] = set()
        revision_parts: list[str] = []
        degraded = False
        remaining = max(0.0, deadline - self.monotonic())
        try:
            list_bounded = getattr(self.transport, "list_dir_bounded", None)
            if callable(list_bounded):
                entries = list_bounded(self.task_root, timeout=remaining)
            else:
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(self.transport.list_dir, self.task_root)
                try:
                    done, _pending = wait((future,), timeout=remaining)
                    if not done:
                        future.cancel()
                        raise TimeoutError("teams listing exceeded snapshot deadline")
                    entries = future.result()
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
        except (TimeoutError, subprocess.TimeoutExpired):
            entries = []
            degraded = True
            diagnostics.append(self._diagnostic(self.task_root, "teams-snapshot-timeout"))
        except Exception:
            entries = []
            degraded = True
            diagnostics.append(self._diagnostic(self.task_root, "teams-list-degraded"))
        if len(entries) > self.max_files:
            degraded = True
            diagnostics.append(self._diagnostic(self.task_root, "teams-list-truncated"))
            entries = entries[:self.max_files]
        candidate_entries: list[tuple[Mapping[str, Any], str, str]] = []
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
            candidate_entries.append((entry, name, path))

        remaining = max(0.0, deadline - self.monotonic())
        documents: Mapping[str, str | None] = {}
        batch_complete = True
        if candidate_entries and remaining <= 0:
            batch_complete = False
        elif candidate_entries:
            paths = [path for _entry, _name, path in candidate_entries]
            read_many = getattr(self.transport, "read_many", None)
            if callable(read_many):
                try:
                    documents, batch_complete = read_many(
                        paths, timeout=remaining, max_workers=self.read_workers
                    )
                except Exception:
                    documents = {path: None for path in paths}
                    batch_complete = False
            else:
                executor = ThreadPoolExecutor(max_workers=self.read_workers)
                futures = {executor.submit(self.transport.read, path): path for path in paths}
                done, pending = wait(futures, timeout=remaining)
                for future in pending:
                    future.cancel()
                mutable: dict[str, str | None] = {}
                for future in done:
                    try:
                        mutable[futures[future]] = future.result()
                    except Exception:
                        mutable[futures[future]] = None
                for future in pending:
                    mutable[futures[future]] = None
                executor.shutdown(wait=False, cancel_futures=True)
                documents = mutable
                batch_complete = not pending
        if not batch_complete:
            degraded = True
            diagnostics.append(self._diagnostic(self.task_root, "teams-snapshot-timeout"))

        for _entry, name, path in candidate_entries:
            if self.monotonic() >= deadline:
                degraded = True
                if not any(value.code == "teams-snapshot-timeout" for value in diagnostics):
                    diagnostics.append(self._diagnostic(self.task_root, "teams-snapshot-timeout"))
                break
            document = documents.get(path)
            if document is None:
                degraded = True
                diagnostics.append(self._diagnostic(path, "teams-read-degraded"))
                continue
            revision_parts.append(hashlib.sha256(document.encode()).hexdigest())
            record, error, claimed_id = self._record(name, document)
            if claimed_id is not None:
                if claimed_id in claimed_ids:
                    colliding_ids.add(claimed_id)
                    degraded = True
                    diagnostics.append(self._diagnostic(path, "teams-duplicate-id-degraded"))
                claimed_ids.add(claimed_id)
            if error is not None:
                degraded = True
                diagnostics.append(error)
                continue
            assert record is not None
            if record.source.item_id in colliding_ids:
                continue
            records_by_id[record.source.item_id] = record
        for item_id in colliding_ids:
            records_by_id.pop(item_id, None)
        states = {"tasks": CapabilityState.DEGRADED if degraded else CapabilityState.COMPLETE}
        states.update(unsupported)
        revision = hashlib.sha256("\n".join(revision_parts).encode()).hexdigest()
        return Snapshot(
            tuple(records_by_id[item_id] for item_id in sorted(records_by_id)),
            not degraded,
            tuple(diagnostics),
            states,
            self.clock(),
            source_revision=revision,
        )
