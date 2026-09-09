"""EventKit protocol fakes below are synthetic; no native store is opened."""

from types import SimpleNamespace
from io import StringIO
import json
import sys

import pytest

from fulcra_apple_reminders import _bridge
from fulcra_apple_reminders._bridge import BridgeError, EventKitBridge, dispatch


class Calendar:
    def __init__(self, identifier="list-a", writable=True):
        self.identifier, self.writable = identifier, writable

    def calendarIdentifier(self):
        return self.identifier

    def title(self):
        return "Example list"

    def allowsContentModifications(self):
        return self.writable


class Reminder:
    def __init__(self, calendar=None):
        self.list = calendar or Calendar()
        self.done = False
        self.recurs = False
        self.title_value = "Example task"
        self.changed = None

    def calendarItemIdentifier(self):
        return "task-a"

    def calendar(self):
        return self.list

    def title(self):
        return self.title_value

    def notes(self):
        return "Synthetic detail"

    def isCompleted(self):
        return self.done

    def completionDate(self):
        return None

    def dueDateComponents(self):
        return None

    def recurrenceRules(self):
        return [object()] if self.recurs else []

    def lastModifiedDate(self):
        return self.changed

    def priority(self):
        return 0

    def setCompleted_(self, value):
        self.done = value


class Store:
    def __init__(self):
        self.calendars = [Calendar(), Calendar("list-b", False)]
        self.reminder = Reminder(self.calendars[0])
        self.rows = [self.reminder]
        self.fetch_mode = "success"
        self.saved = []
        self.save_result = (True, None)
        self.authorized_via = None
        self.reset_count = 0
        self.cancelled = []

    def reset(self):
        self.reset_count += 1

    def calendarsForEntityType_(self, entity):
        assert entity == 1
        return self.calendars

    def predicateForRemindersInCalendars_(self, calendars):
        assert calendars and all(isinstance(c, Calendar) for c in calendars)
        return {c.calendarIdentifier() for c in calendars}

    def fetchRemindersMatchingPredicate_completion_(self, predicate, callback):
        if self.fetch_mode == "success":
            callback(
                [r for r in self.rows if r.calendar().calendarIdentifier() in predicate]
            )
        elif self.fetch_mode == "nil":
            callback(None)
        return "fetch-token"

    def cancelFetchRequest_(self, token):
        self.cancelled.append(token)

    def calendarItemWithIdentifier_(self, identifier):
        assert identifier == "task-a"
        return self.reminder

    def saveReminder_commit_error_(self, reminder, commit, error):
        assert commit is True and error is None
        self.saved.append(reminder)
        return self.save_result

    def requestFullAccessToRemindersWithCompletion_(self, callback):
        self.authorized_via = "full"
        callback(True, None)

    def requestAccessToEntityType_completion_(self, entity, callback):
        assert entity == 1
        self.authorized_via = "legacy"
        callback(True, None)


def bridge(status=3, store=None):
    store = store or Store()
    ek = SimpleNamespace(
        EKEntityTypeReminder=1,
        EKAuthorizationStatusAuthorized=3,
        EKAuthorizationStatusFullAccess=3,
        EKReminder=Reminder,
        EKEventStore=SimpleNamespace(
            authorizationStatusForEntityType_=lambda entity: status
        ),
    )
    return EventKitBridge(ek, store, pump=lambda seconds: None, timeout=0.005), store


def test_status_never_requests_permission():
    native, store = bridge(0)
    assert native.permission_check()["granted"] is False
    assert store.authorized_via is None


@pytest.mark.parametrize("status", [0, 1, 2, 4])
def test_unreadable_permission_blocks_reads_and_writes(status):
    native, store = bridge(status)
    for call in [
        native.collections,
        lambda: native.tasks(["list-a"]),
        lambda: native.get_task("task-a"),
        lambda: native.complete("task-a", "revision", "list-a"),
    ]:
        with pytest.raises(BridgeError):
            call()
    assert store.saved == []


def test_permission_requests_full_access_and_resets_store():
    native, store = bridge(0)
    assert native.permission_check(request=True)["granted"] is True
    assert store.authorized_via == "full"
    assert store.reset_count > 0


def test_legacy_permission_fallback():
    native, store = bridge(0)
    store.requestFullAccessToRemindersWithCompletion_ = None
    assert native.permission_check(request=True)["granted"] is True
    assert store.authorized_via == "legacy"


def test_lists_include_read_only_and_tasks_filter_selection():
    native, store = bridge()
    assert native.collections() == [
        {"id": "list-a", "name": "Example list", "writable": True},
        {"id": "list-b", "name": "Example list", "writable": False},
    ]
    assert native.tasks([]) == []
    assert native.tasks(["list-b"]) == []
    rows = native.tasks(["list-a"])
    assert [r["id"] for r in rows] == ["task-a"]
    assert rows[0]["title"] == "Example task"
    assert rows[0]["revision"]
    with pytest.raises(BridgeError):
        native.tasks(["missing-list"])


@pytest.mark.parametrize("mode", ["nil", "timeout"])
def test_failed_fetch_is_not_successful_empty_snapshot(mode):
    native, store = bridge()
    store.fetch_mode = mode
    with pytest.raises(BridgeError):
        native.tasks(["list-a"])
    if mode == "timeout":
        assert store.cancelled == ["fetch-token"]


def test_get_missing_is_none_and_non_reminder_is_error():
    native, store = bridge()
    store.reminder = None
    assert native.get_task("task-a") is None
    store.reminder = object()
    with pytest.raises(BridgeError):
        native.get_task("task-a")


def test_complete_only_sets_completion_after_fresh_revision_check():
    native, store = bridge()
    before = native.get_task("task-a")
    result = native.complete("task-a", before["revision"], "list-a")
    assert result["completed"] is True
    assert result["title"] == before["title"]
    assert result["notes"] == before["notes"]
    assert store.saved == [store.reminder]
    assert store.reset_count >= 2


@pytest.mark.parametrize(
    "change", ["title", "moved", "readonly", "recurring", "missing"]
)
def test_stale_or_unsafe_completion_does_not_write(change):
    native, store = bridge()
    revision = native.get_task("task-a")["revision"]
    if change == "title":
        store.reminder.title_value = "Changed task"
    if change == "moved":
        store.reminder.list = store.calendars[1]
    if change == "readonly":
        store.reminder.list.writable = False
    if change == "recurring":
        store.reminder.recurs = True
    if change == "missing":
        store.reminder = None
    with pytest.raises(BridgeError):
        native.complete("task-a", revision, "list-a")
    assert store.saved == []


def test_failed_save_is_not_success():
    native, store = bridge()
    store.save_result = (False, "Synthetic error")
    revision = native.get_task("task-a")["revision"]
    with pytest.raises(BridgeError):
        native.complete("task-a", revision, "list-a")


def test_recurring_completion_with_current_revision_still_refuses():
    native, store = bridge()
    store.reminder.recurs = True
    task = native.get_task("task-a")
    assert task["recurring"] is True
    with pytest.raises(BridgeError, match="Recurring"):
        native.complete("task-a", task["revision"], "list-a")
    assert store.saved == []


def test_already_completed_current_revision_does_not_write_again():
    native, store = bridge()
    store.reminder.done = True
    task = native.get_task("task-a")
    assert native.complete("task-a", task["revision"], "list-a")["completed"] is True
    assert store.saved == []


def test_due_date_without_time_preserves_calendar_day():
    native, store = bridge()
    store.reminder.dueDateComponents = lambda: SimpleNamespace(
        year=lambda: 2030, month=lambda: 6, day=lambda: 15, hour=lambda: sys.maxsize
    )
    assert native.get_task("task-a")["due"] == "2030-06-15"


def test_timed_due_date_and_modified_date_are_utc():
    native, store = bridge()
    date = SimpleNamespace(timeIntervalSince1970=lambda: 0)
    calendar = SimpleNamespace(dateFromComponents_=lambda components: date)
    store.reminder.dueDateComponents = lambda: SimpleNamespace(
        year=lambda: 1970,
        month=lambda: 1,
        day=lambda: 1,
        hour=lambda: 0,
        calendar=lambda: calendar,
    )
    task = native.get_task("task-a")
    assert task["due"] == "1970-01-01T00:00:00+00:00"
    store.reminder.changed = date
    assert native.get_task("task-a")["revision"] != task["revision"]


@pytest.mark.parametrize("mode", ["error", "timeout", "denied"])
def test_permission_failure_never_reports_granted(mode):
    native, store = bridge(0)

    def request(callback):
        if mode == "error":
            callback(False, "Synthetic error")
        if mode == "denied":
            callback(False, None)

    store.requestFullAccessToRemindersWithCompletion_ = request
    if mode == "denied":
        assert native.permission_check(request=True)["granted"] is False
    else:
        with pytest.raises(BridgeError):
            native.permission_check(request=True)


def test_cli_sanitizes_unexpected_native_exception(monkeypatch, capsys):
    def native():
        raise RuntimeError("private payload")

    monkeypatch.setattr(_bridge, "native_bridge", native)
    monkeypatch.setattr(sys, "stdin", StringIO('{"operation":"lists"}'))
    assert _bridge.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["ok"] is False
    assert "private payload" not in output.out + output.err


def test_cli_success_is_single_json_document(monkeypatch, capsys):
    native, _ = bridge()
    monkeypatch.setattr(_bridge, "native_bridge", lambda: native)
    monkeypatch.setattr(
        sys, "stdin", StringIO('{"operation":"get","task_id":"task-a"}')
    )
    assert _bridge.main() == 0
    assert json.loads(capsys.readouterr().out)["result"]["id"] == "task-a"


def test_dispatch_rejects_unrecognized_operation():
    native, _ = bridge()
    with pytest.raises(BridgeError):
        dispatch({"operation": "delete"}, native)
