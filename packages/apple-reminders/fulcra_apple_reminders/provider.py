"""Portable provider adapter for the isolated EventKit worker."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

from fulcra_task_sync.models import Collection, Task


class ProviderError(RuntimeError):
    """An incomplete or unsafe Reminders operation."""


def _id(value):
    if not isinstance(value, str) or not value:
        raise ProviderError("A nonempty source identifier is required.")
    return value


def _task(row):
    try:
        if not isinstance(row, dict):
            raise ValueError
        for key in ("id", "collection_id", "revision"):
            _id(row[key])
        for key in ("title", "notes"):
            if not isinstance(row[key], str):
                raise ValueError
        for key in ("completed", "recurring"):
            if type(row[key]) is not bool:
                raise ValueError
        for key in ("due", "completed_at"):
            if row[key] is not None and not isinstance(row[key], str):
                raise ValueError
        return Task(**row)
    except (TypeError, ValueError, KeyError):
        raise ProviderError("Reminders returned a malformed task.") from None


class AppleRemindersProvider:
    name = "apple-reminders"
    namespace = "eventkit"

    def __init__(self, *, timeout=60, runner=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Reminders timeout must be positive and finite.")
        self.timeout = min(timeout, 60)
        self._run = runner or subprocess.run

    def _call(self, operation, **fields):
        app_bin = Path(sys.executable).parent
        bundled = getattr(sys, "frozen", False) or (
            app_bin.name == "MacOS" and app_bin.parent.name == "Contents"
        )
        command = (
            [sys.executable, "--reminders-bridge"]
            if bundled
            else [sys.executable, "-m", "fulcra_apple_reminders._bridge"]
        )
        try:
            process = self._run(
                command,
                input=json.dumps(dict(operation=operation, **fields)),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ProviderError(
                "Reminders operation timed out; check access and retry."
            ) from None
        except OSError:
            raise ProviderError("The Reminders worker could not start.") from None
        try:
            response = json.loads(process.stdout)
            if (
                not isinstance(response, dict)
                or response.get("ok") is not True
                or "result" not in response
            ):
                raise ValueError
            if process.returncode != 0:
                raise ValueError
            return response["result"]
        except (ValueError, TypeError):
            # Do not relay stderr or arbitrary child payloads into Collect logs.
            raise ProviderError(
                "Reminders operation failed; check access, refresh the selection, and retry."
            ) from None

    def permission_check(self, request=False):
        result = self._call("authorize" if request else "status")
        if (
            not isinstance(result, dict)
            or type(result.get("granted")) is not bool
            or not isinstance(result.get("hint"), str)
        ):
            raise ProviderError("Reminders returned an invalid permission status.")
        return {"granted": result["granted"], "hint": result["hint"]}

    def collections(self):
        rows = self._call("lists")
        try:
            if not isinstance(rows, list):
                raise ValueError
            collections = []
            for row in rows:
                _id(row["id"])
                if (
                    not isinstance(row["name"], str)
                    or type(row["writable"]) is not bool
                ):
                    raise ValueError
                collections.append(Collection(**row))
            if len({row.id for row in collections}) != len(collections):
                raise ValueError
            return collections
        except (TypeError, ValueError, KeyError):
            raise ProviderError("Reminders returned malformed lists.") from None

    def tasks(self, selected_ids):
        if not isinstance(selected_ids, (list, tuple, set, frozenset)):
            raise ProviderError("Selected lists must be a collection of identifiers.")
        selected = [_id(value) for value in selected_ids]
        if len(set(selected)) != len(selected):
            raise ProviderError("Selected lists contain duplicate identifiers.")
        if not selected:
            return []
        rows = self._call("tasks", selected_ids=sorted(selected))
        if not isinstance(rows, list):
            raise ProviderError("Reminders did not return a complete snapshot.")
        tasks = [_task(row) for row in rows]
        if len({task.id for task in tasks}) != len(tasks) or any(
            task.collection_id not in selected for task in tasks
        ):
            raise ProviderError("Reminders returned an inconsistent snapshot.")
        return tasks

    def get_task(self, task_id):
        row = self._call("get", task_id=_id(task_id))
        task = None if row is None else _task(row)
        if task is not None and task.id != task_id:
            raise ProviderError("Reminders returned a mismatched task.")
        return task

    def complete(self, task_id, expected_revision, collection_id):
        row = self._call(
            "complete",
            task_id=_id(task_id),
            expected_revision=_id(expected_revision),
            collection_id=_id(collection_id),
        )
        task = _task(row)
        if (
            task.id != task_id
            or task.collection_id != collection_id
            or not task.completed
            or task.recurring
        ):
            raise ProviderError("Reminders did not confirm the requested completion.")
        return task
