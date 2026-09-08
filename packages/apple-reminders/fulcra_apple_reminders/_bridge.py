"""Bounded, single-operation EventKit worker; stdout is a JSON protocol.

Native imports stay inside ``native_bridge`` so portable tests never load TCC.
The enclosing provider also bounds the process, including blocking native calls.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timezone


class BridgeError(RuntimeError):
    """A source operation cannot safely complete."""


def _identifier(value):
    if not isinstance(value, str) or not value:
        raise BridgeError("A nonempty source identifier is required.")
    return value


def _date(value):
    if value is None:
        return None
    return datetime.fromtimestamp(
        value.timeIntervalSince1970(), timezone.utc
    ).isoformat()


class EventKitBridge:
    def __init__(self, eventkit, store, *, pump, foundation=None, timeout=45):
        self.ek, self.store = eventkit, store
        self.pump, self.foundation = pump, foundation
        self.timeout = min(45, max(0.001, timeout))

    def _wait(self, start, cancel=None):
        finished = threading.Event()
        result = []

        def callback(*args):
            result.append(args)
            finished.set()

        token = start(callback)
        deadline = time.monotonic() + self.timeout
        while not finished.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if cancel is not None:
                    cancel(token)
                raise BridgeError(
                    "Reminders operation timed out; retry after checking access."
                )
            self.pump(min(0.05, remaining))
        return result[0]

    def permission_check(self, request=False):
        status = self.ek.EKEventStore.authorizationStatusForEntityType_(
            self.ek.EKEntityTypeReminder
        )
        granted = status in {
            self.ek.EKAuthorizationStatusAuthorized,
            getattr(
                self.ek,
                "EKAuthorizationStatusFullAccess",
                self.ek.EKAuthorizationStatusAuthorized,
            ),
        }
        if request and not granted:
            full_access = getattr(
                self.store, "requestFullAccessToRemindersWithCompletion_", None
            )
            if callable(full_access):
                granted, error = self._wait(full_access)
            else:
                granted, error = self._wait(
                    lambda callback: self.store.requestAccessToEntityType_completion_(
                        self.ek.EKEntityTypeReminder, callback
                    )
                )
            if error is not None:
                raise BridgeError(
                    "Reminders authorization failed; check System Settings."
                )
            if granted:
                self.store.reset()
        return {
            "granted": bool(granted),
            "hint": ""
            if granted
            else (
                "Allow Reminders access, or enable it in System Settings > Privacy & Security > Reminders."
            ),
        }

    def _require_access(self):
        if not self.permission_check()["granted"]:
            raise BridgeError(
                "Reminders access is required; enable it in System Settings."
            )

    def _calendars(self):
        calendars = self.store.calendarsForEntityType_(self.ek.EKEntityTypeReminder)
        if calendars is None:
            raise BridgeError("Reminders list discovery failed.")
        result = {}
        for calendar in calendars:
            identifier = _identifier(calendar.calendarIdentifier())
            if identifier in result:
                raise BridgeError("Reminders returned duplicate list identifiers.")
            result[identifier] = calendar
        return result

    def collections(self):
        self._require_access()
        self.store.reset()
        return [
            {
                "id": identifier,
                "name": calendar.title() or "Untitled list",
                "writable": bool(calendar.allowsContentModifications()),
            }
            for identifier, calendar in self._calendars().items()
        ]

    def _due(self, components):
        if components is None:
            return None
        year, month, day = components.year(), components.month(), components.day()
        hour = components.hour()
        if hour == sys.maxsize:  # NSDateComponentUndefined: date-only reminder.
            return datetime(year, month, day).date().isoformat()
        calendar = components.calendar() or self.foundation.NSCalendar.currentCalendar()
        value = calendar.dateFromComponents_(components)
        if value is None:
            raise BridgeError("Reminders returned an invalid due date.")
        return _date(value)

    def _task(self, reminder):
        if not isinstance(reminder, self.ek.EKReminder):
            raise BridgeError("The source item is not a reminder.")
        task = {
            "id": _identifier(reminder.calendarItemIdentifier()),
            "collection_id": _identifier(reminder.calendar().calendarIdentifier()),
            "title": reminder.title() or "",
            "notes": reminder.notes() or "",
            "completed": bool(reminder.isCompleted()),
            "due": self._due(reminder.dueDateComponents()),
            "completed_at": _date(reminder.completionDate()),
            "recurring": bool(reminder.recurrenceRules()),
        }
        revision = dict(
            task,
            modified=_date(reminder.lastModifiedDate()),
            priority=int(reminder.priority()),
        )
        task["revision"] = hashlib.sha256(
            json.dumps(revision, sort_keys=True).encode()
        ).hexdigest()
        return task

    def tasks(self, selected_ids):
        self._require_access()
        if not isinstance(selected_ids, list):
            raise BridgeError("Selected lists must be an array of identifiers.")
        selected = {_identifier(value) for value in selected_ids}
        if len(selected) != len(selected_ids):
            raise BridgeError("Selected lists contain duplicate identifiers.")
        if not selected:
            return []  # Never pass an empty calendar list to EventKit (it means all).
        self.store.reset()
        calendars = self._calendars()
        if not selected <= calendars.keys():
            raise BridgeError(
                "A selected Reminders list is unavailable; review the selection."
            )
        predicate = self.store.predicateForRemindersInCalendars_(
            [calendars[key] for key in sorted(selected)]
        )
        (rows,) = self._wait(
            lambda callback: self.store.fetchRemindersMatchingPredicate_completion_(
                predicate, callback
            ),
            cancel=self.store.cancelFetchRequest_,
        )
        if rows is None:
            raise BridgeError("Reminders fetch failed; no snapshot was accepted.")
        tasks = [self._task(row) for row in rows]
        if len({task["id"] for task in tasks}) != len(tasks):
            raise BridgeError("Reminders returned duplicate task identifiers.")
        if any(task["collection_id"] not in selected for task in tasks):
            raise BridgeError("Reminders changed lists during the fetch; retry.")
        self._require_access()
        return tasks

    def _get(self, task_id):
        reminder = self.store.calendarItemWithIdentifier_(_identifier(task_id))
        if reminder is None:
            return None, None
        task = self._task(reminder)
        if task["id"] != task_id:
            raise BridgeError("Reminders returned a mismatched identifier.")
        return reminder, task

    def get_task(self, task_id):
        self._require_access()
        self.store.reset()
        _, task = self._get(task_id)
        self._require_access()
        return task

    def complete(self, task_id, expected_revision, collection_id):
        self._require_access()
        _identifier(expected_revision)
        _identifier(collection_id)
        self.store.reset()
        reminder, task = self._get(task_id)
        if task is None:
            raise BridgeError("The reminder is missing; no completion was written.")
        if (
            task["collection_id"] != collection_id
            or task["revision"] != expected_revision
        ):
            raise BridgeError("The reminder changed; refresh before completing it.")
        if task["recurring"]:
            raise BridgeError("Recurring reminders must be completed in Reminders.")
        if not reminder.calendar().allowsContentModifications():
            raise BridgeError("The selected Reminders list is read-only.")
        if task["completed"]:
            return task
        self._require_access()
        reminder.setCompleted_(True)
        saved, error = self.store.saveReminder_commit_error_(reminder, True, None)
        if not saved or error is not None:
            raise BridgeError(
                "Reminders could not confirm the save; refresh before retrying."
            )
        result = self._task(reminder)
        if not result["completed"]:
            raise BridgeError(
                "Reminders did not confirm completion; refresh before retrying."
            )
        return result


def native_bridge():
    if sys.platform != "darwin":
        raise BridgeError("Apple Reminders requires macOS.")
    try:
        import EventKit
        import Foundation
    except ImportError:
        raise BridgeError(
            "The EventKit runtime is unavailable; reinstall Collect."
        ) from None

    def pump(seconds):
        Foundation.NSRunLoop.currentRunLoop().runUntilDate_(
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(seconds)
        )

    return EventKitBridge(
        EventKit, EventKit.EKEventStore.alloc().init(), pump=pump, foundation=Foundation
    )


def dispatch(request, bridge):
    if not isinstance(request, dict):
        raise BridgeError("Invalid Reminders request.")
    operation = request.get("operation")
    if operation == "status":
        return bridge.permission_check()
    if operation == "authorize":
        return bridge.permission_check(request=True)
    if operation == "lists":
        return bridge.collections()
    if operation == "tasks":
        return bridge.tasks(request.get("selected_ids"))
    if operation == "get":
        return bridge.get_task(request.get("task_id"))
    if operation == "complete":
        return bridge.complete(
            request.get("task_id"),
            request.get("expected_revision"),
            request.get("collection_id"),
        )
    raise BridgeError("Unsupported Reminders operation.")


def main():
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise BridgeError("Reminders request exceeds the size limit.")
        request = json.loads(raw)
        result = dispatch(request, native_bridge())
        response = {"ok": True, "result": result}
    except BridgeError as error:
        response = {"ok": False, "error": str(error)}
    except Exception:
        # Native exceptions may contain source titles, identifiers or other private data.
        response = {
            "ok": False,
            "error": "Reminders operation failed; check access and retry.",
        }
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
