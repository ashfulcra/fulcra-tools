import time
from datetime import datetime, timezone

import pytest

from coord_tracker_bridge import (
    BridgeLedger,
    CapabilityState,
    FulcraTeamsTransport,
    TeamsSourceAdapter,
    TeamsTransportError,
    build_plan,
    load_policy,
)


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


def task(item_id="task-1", *, status="active", tags="[kind:task]", kind="Task"):
    return f"""---
type: {kind}
id: {item_id}
title: A task
status: {status}
priority: P1
origin: fleet
workstream: bridge
tags: {tags}
---
arbitrary body is deliberately ignored
"""


class MemoryTransport:
    def __init__(self, entries, documents, *, list_error=False, read_error=()):
        self.entries = entries
        self.documents = documents
        self.list_error = list_error
        self.read_error = set(read_error)
        self.reads = []

    def list_dir(self, _prefix):
        if self.list_error:
            raise RuntimeError("offline")
        return self.entries

    def read(self, path):
        self.reads.append(path)
        if path in self.read_error:
            raise RuntimeError("offline")
        return self.documents.get(path)


def adapter(transport, **kwargs):
    return TeamsSourceAdapter("fulcra", transport=transport, clock=lambda: NOW, **kwargs)


def test_strict_teams_source_reads_only_typed_task_documents():
    root = "team/fulcra/task/"
    transport = MemoryTransport(
        [
            {"name": "index.md", "size": "1B", "mtime": "now"},
            {"name": "log.md", "size": "1B", "mtime": "now"},
            {"name": "task.md", "size": "2B", "mtime": "now"},
        ],
        {root + "task.md": task()},
    )

    snapshot = adapter(transport).snapshot()

    assert snapshot.complete
    assert [item.source.to_dict() for item in snapshot.items] == [{
        "provider": "teams", "namespace": "fulcra/tasks", "item_id": "task-1"
    }]
    assert snapshot.items[0].workstream == "bridge"
    assert snapshot.items[0].tags == ("kind:task",)
    assert transport.reads == [root + "task.md"]
    assert snapshot.source_revision


@pytest.mark.parametrize(
    ("name", "document", "code"),
    [
        ("bad.md", "not frontmatter", "teams-parse-degraded"),
        ("bad.md", task(kind="Reference"), "teams-type-degraded"),
        ("bad.md", task(status="mystery"), "teams-schema-degraded"),
        ("bad.md", task(tags="kind:task"), "teams-schema-degraded"),
    ],
)
def test_ambiguous_document_degrades_tasks(name, document, code):
    path = f"team/fulcra/task/{name}"
    snapshot = adapter(MemoryTransport([{"name": name}], {path: document})).snapshot()

    assert not snapshot.complete
    assert snapshot.capabilities["tasks"] is CapabilityState.DEGRADED
    assert snapshot.diagnostics[0].code == code


def test_listed_but_unreadable_document_degrades_tasks():
    path = "team/fulcra/task/task.md"
    snapshot = adapter(MemoryTransport([{"name": "task.md"}], {}, read_error={path})).snapshot()

    assert snapshot.capabilities["tasks"] is CapabilityState.DEGRADED
    assert snapshot.diagnostics[0].code == "teams-read-degraded"


def test_duplicate_explicit_ids_degrade_without_emitting_duplicate():
    root = "team/fulcra/task/"
    snapshot = adapter(MemoryTransport(
        [{"name": "a.md"}, {"name": "b.md"}],
        {root + "a.md": task("same"), root + "b.md": task("same")},
    )).snapshot()

    assert not snapshot.complete
    assert not snapshot.items
    assert snapshot.diagnostics[0].code == "teams-duplicate-id-degraded"
    assert not build_plan(snapshot, [], BridgeLedger(), load_policy()).changes


def test_unexpected_entry_and_read_cap_degrade_instead_of_authorizing_absence():
    transport = MemoryTransport(
        [{"name": "nested/", "is_dir": True}, {"name": "task.md"}],
        {"team/fulcra/task/task.md": task()},
    )
    snapshot = adapter(transport, max_files=1).snapshot()

    assert not snapshot.complete
    assert {value.code for value in snapshot.diagnostics} == {
        "teams-list-truncated", "teams-entry-degraded"
    }


def test_teams_transport_rejects_ambiguous_listing_and_never_echoes_stderr():
    def runner(_argv, _timeout):
        return 0, "ambiguous output", "SECRET"

    with pytest.raises(TeamsTransportError, match="ambiguous list response") as error:
        FulcraTeamsTransport(runner=runner).list_dir("team/fulcra/task/")

    assert "SECRET" not in str(error.value)


def test_default_teams_transport_uses_only_read_only_fulcra_file_commands():
    calls = []

    def runner(argv, _timeout):
        calls.append(tuple(argv))
        if argv[2] == "list":
            return 0, "10B 2026-07-17 12:00PM UTC task.md\n", ""
        return 0, task(), ""

    transport = FulcraTeamsTransport(runner=runner)

    assert transport.list_dir("team/fulcra/task/")[0]["name"] == "task.md"
    assert transport.read("team/fulcra/task/task.md") == task()
    assert calls == [
        ("fulcra-api", "file", "list", "team/fulcra/task/"),
        ("fulcra-api", "file", "download", "team/fulcra/task/task.md", "-"),
    ]


def test_many_slow_downloads_respect_one_snapshot_deadline():
    names = [f"task-{index}.md" for index in range(40)]
    observed_timeouts = []

    def runner(argv, timeout):
        if argv[2] == "list":
            lines = [f"10B 2026-07-17 12:00PM UTC {name}" for name in names]
            return 0, "\n".join(lines), ""
        observed_timeouts.append(timeout)
        time.sleep(0.05)
        name = argv[3].rsplit("/", 1)[-1]
        return 0, task(name.removesuffix(".md")), ""

    source = TeamsSourceAdapter(
        "fulcra",
        transport=FulcraTeamsTransport(runner=runner, timeout=5.0),
        clock=lambda: NOW,
        snapshot_timeout=0.08,
        read_workers=2,
    )
    started = time.monotonic()

    snapshot = source.snapshot()

    elapsed = time.monotonic() - started
    assert elapsed < 0.3
    assert not snapshot.complete
    assert snapshot.capabilities["tasks"] is CapabilityState.DEGRADED
    assert any(value.code == "teams-snapshot-timeout" for value in snapshot.diagnostics)
    assert observed_timeouts and max(observed_timeouts) <= 0.08
