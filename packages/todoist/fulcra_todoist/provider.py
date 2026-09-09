"""Bounded Todoist API-v1 adapter.

Snapshots contain all selected active tasks and explicit completions in the last
30 days by default (configurable to 1..89 days, below the API's three-month cap).
Every page must succeed before any snapshot or completion authority is returned.
Older history and transitions between polls may be unavailable; absence never
means completion. Recurring tasks are imported read-only because item_close has
no server-side revision precondition. No live account validation is implied.

The engine must persist its account-scoped task/revision intent before calling
complete. UUIDv5 derives a stable command identity from exactly that intent, so
restarts and uncertain retries cannot silently create a second command UUID.
Todoist does not document UUID retention duration; no unbounded deduplication
claim is made. Fresh revision checks are not server-side compare-and-swap.

Optional load_state/save_state callbacks preserve the last observed active update
per account/task after a complete snapshot; dry_run never saves or completes.
This rejects older completion history across restarts. Positive history still
proves an event, not observation of every intervening transition: a reopen and
delete entirely between polls cannot be reconstructed. Offline gaps longer than
the history window (30 days by default) may also leave completions unknown.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from fulcra_task_sync.models import Collection, Task

_API = "https://api.todoist.com/api/v1/"
_COMMAND_NAMESPACE = uuid.UUID("53d27b5b-d0ca-46d7-9269-a02c7a57e383")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class TodoistError(RuntimeError):
    """Safe-to-display source failure; never contains API bodies or tokens."""


def _identity(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TodoistError("Todoist returned an invalid source identity.")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TodoistError("Todoist returned invalid completion evidence.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        raise TodoistError("Todoist returned invalid completion evidence.") from None
    return value


class TodoistProvider:
    name = "todoist"

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 20,
        history_days: int = 30,
        max_pages: int = 50,
        max_requests: int = 200,
        budget_s: float = 120,
        now: Callable[[], datetime] | None = None,
        load_state: Callable[[str], dict | None] | None = None,
        save_state: Callable[[str, dict], object] | None = None,
        dry_run: bool = False,
    ):
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise TodoistError("Connect Todoist with a valid API token.")
        if (
            not 1 <= history_days <= 89
            or timeout_s <= 0
            or budget_s <= 0
            or max_pages < 1
            or max_requests < 1
        ):
            raise ValueError("Invalid Todoist I/O or history limits.")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
            trust_env=False,
        )
        self._timeout_s = timeout_s
        self.history_days = history_days
        self._max_pages = max_pages
        self._remaining = max_requests
        self._deadline = time.monotonic() + budget_s
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._namespace: str | None = None
        self._selected: set[str] = set()
        self._known: dict[str, str] = {}
        self._active_seen: dict[str, datetime] = {}
        self._deleted_seen: set[str] = set()
        self._load_state = load_state
        self._save_state = save_state
        self._dry_run = dry_run
        self._loaded_observations: set[str] = set()
        self._persisted_active_seen: dict[str, datetime] = {}
        self._snapshot_active_seen: dict[str, datetime] | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _request(self, method: str, path: str, *, missing_ok=False, **kwargs):
        remaining_s = self._deadline - time.monotonic()
        if self._remaining <= 0 or remaining_s <= 0:
            raise TodoistError("Todoist read budget exhausted; snapshot is incomplete.")
        self._remaining -= 1
        try:
            with self._client.stream(
                method, _API + path, timeout=min(self._timeout_s, remaining_s), **kwargs
            ) as response:
                if response.status_code == 404 and missing_ok:
                    return None
                if not 200 <= response.status_code < 300:
                    raise TodoistError(f"Todoist request failed (HTTP {response.status_code}).")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= self._deadline:
                        raise TodoistError("Todoist read budget exhausted; snapshot is incomplete.")
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise TodoistError("Todoist response exceeds the safe read limit.")
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise TodoistError("Todoist returned an invalid response.")
                return value
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise TodoistError(
                "Todoist request failed or returned invalid data; retry safely."
            ) from None

    @property
    def namespace(self) -> str:
        if self._namespace is None:
            account = self._request("GET", "user")
            identity = _identity(account.get("id"))
            if account.get("is_deleted", False) is not False:
                raise TodoistError("Todoist account is unavailable.")
            self._namespace = "todoist:" + hashlib.sha256(identity.encode()).hexdigest()
        return self._namespace

    def _observation_key(self, task_id: str) -> str:
        # Fixed length, opaque IDs, and a prefix distinct from engine journals.
        digest = hashlib.sha256(_identity(task_id).encode()).hexdigest()
        return f"todoist-observation:v1:{self.namespace}:{digest}"

    def _load_observation(self, task_id: str) -> None:
        if task_id in self._loaded_observations:
            return
        if self._load_state is not None:
            try:
                value = self._load_state(self._observation_key(task_id))
                if value is not None:
                    if (
                        not isinstance(value, dict)
                        or set(value) != {"schema", "active_updated_at"}
                        or value["schema"] != "todoist-observation/v1"
                        or not isinstance(value["active_updated_at"], str)
                        or len(value["active_updated_at"]) > 128
                    ):
                        raise ValueError("Invalid observation")
                    observed = datetime.fromisoformat(
                        _timestamp(value["active_updated_at"]).replace("Z", "+00:00")
                    )
                    self._active_seen[task_id] = max(
                        observed, self._active_seen.get(task_id, observed)
                    )
                    self._persisted_active_seen[task_id] = observed
            except Exception:
                raise TodoistError("Todoist stored observation could not be verified.") from None
        self._loaded_observations.add(task_id)

    def _save_observations(self, observed: dict[str, datetime]) -> None:
        if self._dry_run or self._save_state is None:
            return
        for task_id, stamp in observed.items():
            if self._persisted_active_seen.get(task_id) == stamp:
                continue
            value = {"schema": "todoist-observation/v1", "active_updated_at": stamp.isoformat()}
            try:
                result = self._save_state(self._observation_key(task_id), value)
                if result is False:
                    raise ValueError("Observation save failed")
            except Exception:
                raise TodoistError("Todoist observation could not be saved safely.") from None
            self._persisted_active_seen[task_id] = stamp

    def _pages(self, path: str, key: str, **params) -> list[dict]:
        rows = []
        seen = set()
        params = dict(params, limit=50)
        for _ in range(self._max_pages):
            data = self._request("GET", path, params=params)
            page = data.get(key)
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise TodoistError("Todoist returned an incomplete page.")
            rows.extend(page)
            if len(rows) > 10000:
                raise TodoistError("Todoist snapshot exceeds the safe task limit.")
            # History documents omission as terminal; other paginated APIs require null.
            if key == "results" and "next_cursor" not in data:
                raise TodoistError("Todoist returned an incomplete page.")
            cursor = data.get("next_cursor")
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen:
                raise TodoistError("Todoist returned invalid pagination; snapshot is incomplete.")
            seen.add(cursor)
            params["cursor"] = cursor  # Opaque query value, never a URL to follow.
        raise TodoistError("Todoist pagination limit reached; snapshot is incomplete.")

    def collections(self) -> list[Collection]:
        self.namespace
        result = []
        seen = set()
        for row in self._pages("projects", "results"):
            identity = _identity(row.get("id"))
            if identity in seen or not isinstance(row.get("name"), str):
                raise TodoistError("Todoist returned invalid projects.")
            seen.add(identity)
            if row.get("is_deleted") is True or row.get("is_archived") is True:
                continue
            writable = row.get("is_frozen", False) is False and row.get("role") != "viewer"
            result.append(Collection(identity, row["name"], writable=writable))
        return result

    def _task(self, row: dict, *, history=False, active_only=True) -> Task | None:
        identity = _identity(row.get("id"))
        project_id = _identity(row.get("project_id"))
        self._load_observation(identity)
        if row.get("is_deleted") is True:
            self._deleted_seen.add(identity)
            return None
        if (
            row.get("is_deleted") is not False
            or type(row.get("checked")) is not bool
            or not isinstance(row.get("content"), str)
            or not isinstance(row.get("description"), str)
        ):
            raise TodoistError("Todoist returned an invalid task.")
        if active_only and not history and row["checked"]:
            raise TodoistError("Todoist active endpoint returned an inconsistent task.")
        if not history:
            self._deleted_seen.discard(identity)
        due = row.get("due")
        if due is not None and (
            not isinstance(due, dict)
            or type(due.get("is_recurring")) is not bool
            or not isinstance(due.get("date"), str)
        ):
            raise TodoistError("Todoist returned an invalid task due date.")
        completed = history or row["checked"]
        completed_at = _timestamp(row.get("completed_at")) if completed else None
        updated_at = row.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            raise TodoistError("Todoist returned an invalid task revision.")
        if not completed:
            observed = (
                datetime.fromisoformat(_timestamp(updated_at).replace("Z", "+00:00"))
                if updated_at
                else self._now()
            )
            self._active_seen[identity] = max(observed, self._active_seen.get(identity, observed))
            if self._snapshot_active_seen is not None:
                self._snapshot_active_seen[identity] = self._active_seen[identity]
        # Source update/counters distinguish close/reopen even when visible fields agree.
        baseline = {
            key: row.get(key)
            for key in (
                "id",
                "project_id",
                "content",
                "description",
                "due",
                "deadline",
                "checked",
                "updated_at",
                "completed_at",
                "completed_count",
                "postponed_count",
                "is_deleted",
                "parent_id",
            )
        }
        baseline["completed"] = completed
        revision = hashlib.sha256(
            json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return Task(
            identity,
            project_id,
            row["content"],
            row["description"],
            completed,
            due=due["date"] if due else None,
            completed_at=completed_at,
            recurring=due["is_recurring"] if due else False,
            revision=revision,
        )

    def _history(self, selected_ids: set[str]) -> dict[str, Task]:
        until = self._now()
        if until.tzinfo is None:
            raise TodoistError("Todoist history requires a timezone-aware clock.")
        since = until - timedelta(days=self.history_days)

        def format_time(dt):
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        found: dict[str, Task] = {}
        for project in sorted(selected_ids):
            rows = self._pages(
                "tasks/completed/by_completion_date",
                "items",
                project_id=project,
                since=format_time(since),
                until=format_time(until),
            )
            for row in rows:
                if row.get("project_id") not in selected_ids:
                    continue
                item = self._task(row, history=True)
                if item is not None:
                    if item.id in self._deleted_seen:
                        continue
                    stamp = datetime.fromisoformat(item.completed_at.replace("Z", "+00:00"))
                    if not since <= stamp < until:
                        raise TodoistError(
                            "Todoist returned history outside the requested interval."
                        )
                    if item.id in self._active_seen and stamp < self._active_seen[item.id]:
                        continue  # An older completion cannot close a known reopened task.
                    prior = found.get(item.id)
                    if prior is None or stamp > datetime.fromisoformat(
                        prior.completed_at.replace("Z", "+00:00")
                    ):
                        found[item.id] = item
        return found

    def tasks(self, selected_ids: set[str]) -> list[Task]:
        # Revoke authority immediately, including when refresh fails or user deselects.
        self._selected = set()
        self._known = {}
        selected = {_identity(value) for value in selected_ids}
        if not selected:
            return []
        self.namespace
        observed: dict[str, datetime] = {}
        self._snapshot_active_seen = observed
        try:
            found = self._history(selected)
            for project in sorted(selected):
                for row in self._pages("tasks", "results", project_id=project):
                    if row.get("project_id") not in selected:
                        continue
                    item = self._task(row)
                    if item is not None:
                        found[item.id] = item  # Fresh active task wins over historic completion.
                    else:
                        found.pop(row["id"], None)
            if len(found) > 10000:
                raise TodoistError("Todoist snapshot exceeds the safe task limit.")
        finally:
            self._snapshot_active_seen = None
        self._save_observations(observed)
        self._selected = selected
        self._known = {item.id: item.collection_id for item in found.values()}
        return list(found.values())

    def _lookup_task(self, task_id: str) -> Task | None:
        row = self._request("GET", "tasks/" + _identity(task_id), missing_ok=True)
        if row is None:
            return None
        if row.get("id") != task_id:
            raise TodoistError("Todoist returned a mismatched task identity.")
        # GET /tasks/{id} can return a completed task. Only the active-list
        # endpoint promises unchecked rows; explicit detail completion still
        # requires checked=True and a valid completed_at timestamp.
        return self._task(row, active_only=False)

    def get_task(self, task_id: str) -> Task | None:
        self.namespace
        current = self._lookup_task(task_id)
        if current is not None:
            return current
        if task_id in self._deleted_seen:
            return None
        project = self._known.get(task_id)
        # A 404 itself never proves anything; query fresh explicit completion history.
        if project not in self._selected:
            return None
        return self._history({project}).get(task_id)

    def complete(self, task_id: str, expected_revision: str, collection_id: str) -> Task:
        if self._dry_run:
            raise TodoistError("Todoist preview cannot complete source tasks.")
        self.namespace
        if collection_id not in self._selected or self._known.get(task_id) != collection_id:
            raise TodoistError(
                "Todoist completion requires a successful selected-project snapshot."
            )
        current = self.get_task(task_id)
        if current is None:
            raise TodoistError("Todoist task is unavailable; completion was not inferred.")
        if current.collection_id != collection_id:
            raise TodoistError("Todoist task moved outside the selected project.")
        if current.completed:
            return current
        if current.recurring:
            raise TodoistError("Todoist recurring tasks are read-only; complete them in Todoist.")
        if current.revision != expected_revision:
            raise TodoistError("Todoist task changed; refresh before completing it.")
        intent = json.dumps([self.namespace, task_id, expected_revision], separators=(",", ":"))
        command_id = str(uuid.uuid5(_COMMAND_NAMESPACE, intent))
        command = {"type": "item_close", "uuid": command_id, "args": {"id": task_id}}
        result = self._request(
            "POST",
            "sync",
            data={"commands": json.dumps([command]), "sync_token": "*", "resource_types": "[]"},
        )
        statuses = result.get("sync_status")
        if not isinstance(statuses, dict) or statuses.get(command_id) != "ok":
            raise TodoistError(
                "Todoist completion command was not confirmed; refresh and retry safely."
            )
        after = self._lookup_task(task_id)
        if after is not None:
            if after.completed:
                return after
            raise TodoistError(
                "Todoist task is still active or was reopened; completion is uncertain."
            )
        # A per-UUID success is explicit evidence even if history has not caught up.
        return replace(
            current,
            completed=True,
            completed_at=self._now().isoformat(),
            revision=hashlib.sha256(
                (current.revision + ":closed:" + command_id).encode()
            ).hexdigest(),
        )
