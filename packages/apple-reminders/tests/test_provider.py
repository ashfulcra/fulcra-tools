"""Synthetic bridge protocol responses, never live Reminders data."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fulcra_apple_reminders.provider import AppleRemindersProvider, ProviderError


TASK = dict(
    id="task-a",
    collection_id="list-a",
    title="Example task",
    notes="",
    completed=False,
    due=None,
    completed_at=None,
    recurring=False,
    revision="r1",
)


def runner(result, calls):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"ok": True, "result": result}), stderr=""
        )

    return run


def test_source_bridge_uses_json_stdin_and_bounded_subprocess():
    calls = []
    provider = AppleRemindersProvider(runner=runner([TASK], calls))
    tasks = provider.tasks(["list-a"])
    assert tasks[0].title == "Example task"
    command, kwargs = calls[0]
    assert command == [sys.executable, "-m", "fulcra_apple_reminders._bridge"]
    assert json.loads(kwargs["input"]) == {
        "operation": "tasks",
        "selected_ids": ["list-a"],
    }
    assert 0 < kwargs["timeout"] <= 60
    assert "task-a" not in command


def test_frozen_runtime_uses_app_dispatch(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    calls = []
    provider = AppleRemindersProvider(
        runner=runner({"granted": False, "hint": "Allow access"}, calls)
    )
    assert provider.permission_check()["granted"] is False
    assert calls[0][0] == [sys.executable, "--reminders-bridge"]


def test_briefcase_runtime_uses_app_dispatch_without_sys_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(
        sys, "executable", "/Applications/Example.app/Contents/MacOS/Example"
    )
    calls = []
    provider = AppleRemindersProvider(
        runner=runner({"granted": False, "hint": "Allow access"}, calls)
    )
    provider.permission_check()
    assert calls[0][0] == [sys.executable, "--reminders-bridge"]


def test_collections_and_missing_get_are_validated():
    rows = [dict(id="list-a", name="Example list", writable=False)]
    provider = AppleRemindersProvider(runner=runner(rows, []))
    assert provider.collections()[0].writable is False
    assert AppleRemindersProvider(runner=runner(None, [])).get_task("task-a") is None
    for rows in [
        [{}],
        [None],
        "lists",
        [dict(id="list-a", name="Example", writable="false")],
    ]:
        with pytest.raises(ProviderError):
            AppleRemindersProvider(runner=runner(rows, [])).collections()


def test_invalid_completion_response_fails():
    for row in [
        TASK,
        dict(TASK, completed=True, id="task-b"),
        dict(TASK, completed=True, recurring=True),
    ]:
        with pytest.raises(ProviderError):
            AppleRemindersProvider(runner=runner(row, [])).complete(
                "task-a", "r1", "list-a"
            )


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"ok":false,"error":"failed"}',
        '{"ok":true}',
        '{"ok":true,"result":null}',
        '{"ok":true,"result":[{}]}',
    ],
)
def test_bad_response_never_becomes_successful_empty_snapshot(response):
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=response, stderr="")

    with pytest.raises(ProviderError):
        AppleRemindersProvider(runner=run).tasks(["list-a"])


def test_timeout_and_stderr_do_not_leak_source_payload():
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], 60, output="private payload", stderr="private payload"
        )

    with pytest.raises(ProviderError) as error:
        AppleRemindersProvider(runner=run).tasks(["list-a"])
    assert "private payload" not in str(error.value)


def test_unselected_and_duplicate_items_fail_entire_snapshot():
    for rows in [[dict(TASK, collection_id="list-b")], [TASK, TASK]]:
        with pytest.raises(ProviderError):
            AppleRemindersProvider(runner=runner(rows, [])).tasks(["list-a"])


def test_complete_and_permission_dispatch():
    calls = []
    provider = AppleRemindersProvider(runner=runner(dict(TASK, completed=True), calls))
    assert provider.complete("task-a", "r1", "list-a").completed is True
    assert json.loads(calls[0][1]["input"]) == dict(
        operation="complete",
        task_id="task-a",
        expected_revision="r1",
        collection_id="list-a",
    )
    calls.clear()
    provider = AppleRemindersProvider(
        runner=runner({"granted": True, "hint": ""}, calls)
    )
    assert provider.permission_check(request=True)["granted"]
    assert json.loads(calls[0][1]["input"])["operation"] == "authorize"
