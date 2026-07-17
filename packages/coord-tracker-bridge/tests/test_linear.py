import base64
import json
from datetime import datetime, timezone

import pytest

from coord_tracker_bridge import (
    BridgeLedger,
    CapabilityState,
    Change,
    ChangeKind,
    GraphQLResponse,
    LinearClient,
    LinearError,
    LinearTrackerAdapter,
    LedgerEntry,
    ResourcePlan,
    Snapshot,
    SourceIdentity,
    WorkRecord,
    load_policy,
)
from coord_tracker_bridge.linear import (
    append_source_metadata,
    parse_bridge_metadata,
    parse_source_metadata,
    strip_source_metadata,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def post(self, payload):
        self.payloads.append(payload)
        return self.responses.pop(0)


def response(data, *, status=200, headers=None, errors=None):
    body = {"data": data}
    if errors:
        body = {"errors": errors}
    return GraphQLResponse(status, body, headers or {})


@pytest.mark.parametrize(
    ("method", "root", "kwargs"),
    [
        ("list_issues", "issues", {}),
        ("list_labels", "issueLabels", {}),
        ("list_projects", "projects", {}),
        ("list_comments", "comments", {"issue_id": "i1"}),
        ("list_inbound_events", "auditEntries", {}),
    ],
)
def test_every_collection_paginates(method, root, kwargs):
    transport = FakeTransport([
        response({root: {"nodes": [{"id": "one"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}),
        response({root: {"nodes": [{"id": "two"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")

    values = getattr(adapter, method)(**kwargs)

    assert [value["id"] for value in values] == ["one", "two"]
    assert [call["variables"]["after"] for call in transport.payloads] == [None, "c1"]


def test_rate_limit_retry_is_bounded_and_honors_retry_after():
    sleeps = []
    transport = FakeTransport([
        response({}, status=429, headers={"retry-after": "0.5"}),
        response({"ok": True}),
    ])
    client = LinearClient(transport, max_attempts=2, sleeper=sleeps.append)

    assert client.execute("Probe", "query Probe{ok}") == {"ok": True}
    assert sleeps == [0.5]
    assert len(transport.payloads) == 2


def test_errors_never_echo_graphql_variables():
    transport = FakeTransport([response({}, status=400)])
    client = LinearClient(transport, max_attempts=1)

    with pytest.raises(Exception) as error:
        client.execute("CreateIssue", "mutation", {"description": "TOP SECRET"})

    assert "TOP SECRET" not in str(error.value)


def test_provider_metadata_round_trip_uses_full_identity_not_title():
    source = SourceIdentity("coord-engine", "fulcra", "alpha-12345678")
    description = append_source_metadata(
        "operator-visible body",
        source,
        {"policy_version": "2", "owner": "ash"},
        capability="asks",
    )

    assert parse_source_metadata(description) == source
    assert parse_bridge_metadata(description)["fields"] == {"policy_version": "2", "owner": "ash"}
    assert parse_bridge_metadata(description)["capability"] == "asks"
    assert strip_source_metadata(description) == "operator-visible body"
    assert "alpha-12345678" not in description


@pytest.mark.parametrize("capability", ["asks", "threads"])
def test_created_before_ledger_write_preserves_capability_from_provider_metadata(capability):
    source = SourceIdentity("coord-engine", f"fulcra/{capability}", "item-1")
    issue = {
        "id": "LIN-1", "title": "Task",
        "description": append_source_metadata("body", source, capability=capability),
        "state": {"type": "started"}, "labels": {"nodes": []}, "project": None,
    }
    transport = FakeTransport([
        response({"issues": {"nodes": [issue], "pageInfo": {"hasNextPage": False, "endCursor": None}}}),
        response({"issue": {"labels": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")

    records = adapter.list_managed_records(BridgeLedger())

    assert [(record.provider_id, record.source, record.capability) for record in records] == [
        ("LIN-1", source, capability)
    ]


def test_provider_metadata_without_capability_fails_closed_without_ledger():
    source = SourceIdentity("coord-engine", "fulcra/asks", "ask-1")
    description = append_source_metadata("body", source, capability="asks")
    decoded = dict(parse_bridge_metadata(description))
    decoded.pop("capability")
    encoded = base64.urlsafe_b64encode(
        json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    issue = {
        "id": "LIN-1",
        "title": "Ask",
        "description": f"<!-- coord-tracker-bridge:source={encoded} -->",
        "state": {"type": "started"},
    }
    transport = FakeTransport([
        response({"issues": {"nodes": [issue], "pageInfo": {"hasNextPage": False}}}),
    ])

    with pytest.raises(LinearError, match="no trusted source capability"):
        LinearTrackerAdapter(LinearClient(transport), "team").list_managed_records(BridgeLedger())


def test_provider_capability_conflict_with_ledger_fails_closed():
    source = SourceIdentity("coord-engine", "fulcra/asks", "ask-1")
    issue = {
        "id": "LIN-1",
        "title": "Ask",
        "description": append_source_metadata("body", source, capability="threads"),
        "state": {"type": "started"},
    }
    transport = FakeTransport([
        response({"issues": {"nodes": [issue], "pageInfo": {"hasNextPage": False}}}),
    ])
    ledger = BridgeLedger([
        LedgerEntry(source, "asks", "linear", "LIN-1", "1", "hash")
    ])

    with pytest.raises(LinearError, match="conflicts with ledger"):
        LinearTrackerAdapter(LinearClient(transport), "team").list_managed_records(ledger)


def test_issue_labels_paginate_independently_of_issue_page():
    transport = FakeTransport([
        response({"issue": {"labels": {"nodes": [{"name": "one"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}}),
        response({"issue": {"labels": {"nodes": [{"name": "two"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")

    assert [label["name"] for label in adapter.list_issue_labels("issue")] == ["one", "two"]
    assert [call["variables"]["after"] for call in transport.payloads] == [None, "c1"]


def test_partial_update_does_not_wipe_description_or_labels():
    transport = FakeTransport([response({"issueUpdate": {"success": True}})])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")
    source = SourceIdentity("coord-engine", "fulcra", "task-1")

    adapter.apply_change(Change(ChangeKind.UPDATE, source, "LIN-1", {"title": "Renamed"}))

    assert transport.payloads[0]["variables"]["input"] == {"title": "Renamed"}


def test_false_success_update_is_rejected():
    transport = FakeTransport([response({"issueUpdate": {"success": False}})])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")
    source = SourceIdentity("coord-engine", "fulcra", "task-1")

    with pytest.raises(LinearError, match="mutation did not succeed"):
        adapter.apply_change(Change(ChangeKind.UPDATE, source, "LIN-1", {"title": "Renamed"}))


def test_create_persists_capability_in_provider_metadata():
    transport = FakeTransport([
        response({"issueCreate": {"success": True, "issue": {"id": "LIN-1"}}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")
    source = SourceIdentity("coord-engine", "fulcra/asks", "ask-1")

    provider_id = adapter.apply_change(Change(
        ChangeKind.CREATE,
        source,
        None,
        {"title": "Ask", "description": "body", "source_capability": "asks"},
    ))

    description = transport.payloads[0]["variables"]["input"]["description"]
    assert provider_id == "LIN-1"
    assert parse_bridge_metadata(description)["capability"] == "asks"


def test_false_success_close_is_rejected():
    transport = FakeTransport([
        response({"team": {"states": {"nodes": [{"id": "done", "type": "completed"}]}}}),
        response({"issueUpdate": {"success": False}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")
    source = SourceIdentity("coord-engine", "fulcra", "task-1")

    with pytest.raises(LinearError, match="mutation did not succeed"):
        adapter.apply_change(Change(ChangeKind.CLOSE, source, "LIN-1", {}))


@pytest.mark.parametrize(
    ("plan", "root"),
    [
        (ResourcePlan(("lane:active",), ()), "issueLabelCreate"),
        (ResourcePlan((), ("Workstream",)), "projectCreate"),
    ],
)
def test_false_success_resource_creation_is_rejected(plan, root):
    transport = FakeTransport([response({root: {"success": False}})])

    with pytest.raises(LinearError, match="mutation did not succeed"):
        LinearTrackerAdapter(LinearClient(transport), "team").apply_resources(plan)


def test_legacy_marker_adoption_uses_footer_and_checks_arbitrary_slug_suffix():
    source = SourceIdentity("coord-engine", "fulcra/tasks", "role-vacant-example-h24h-sla")
    snapshot = Snapshot(
        (WorkRecord(source, "tasks", "Canonical", "active", origin="fleet"),),
        True, (), {"tasks": CapabilityState.COMPLETE},
        datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    issue = {
        "id": "LIN-1",
        "title": "Legacy title [bus:h24h-sla]",
        "description": "body\n\n---\nbus slug: `role-vacant-example-h24h-sla`",
    }
    transport = FakeTransport([
        response({"issues": {"nodes": [issue], "pageInfo": {"hasNextPage": False}}}),
        response({"issueUpdate": {"success": True}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")

    adoptions = adapter.plan_marker_adoptions(snapshot, BridgeLedger(), load_policy())
    adapter.apply_marker_adoption(adoptions[0])

    assert adoptions[0].source == source
    mutation = transport.payloads[1]["variables"]
    assert mutation["input"]["title"] == "Legacy title"
    metadata = parse_bridge_metadata(mutation["input"]["description"])
    assert SourceIdentity.from_dict(metadata["source"]) == source
    assert metadata["capability"] == "tasks"


def test_unknown_legacy_marker_fails_before_any_mutation():
    snapshot = Snapshot(
        (), True, (), {"tasks": CapabilityState.COMPLETE},
        datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    issue = {
        "id": "LIN-1",
        "title": "Unknown [bus:deadbeef]",
        "description": "bus slug: `unknown-task-deadbeef`",
    }
    transport = FakeTransport([
        response({"issues": {"nodes": [issue], "pageInfo": {"hasNextPage": False}}}),
    ])

    with pytest.raises(LinearError, match="no source row"):
        LinearTrackerAdapter(LinearClient(transport), "team").plan_marker_adoptions(
            snapshot, BridgeLedger(), load_policy()
        )

    assert len(transport.payloads) == 1


def test_legacy_adoption_resolves_terminal_task_absent_from_hot_snapshot():
    source = SourceIdentity("coord-engine", "fulcra/tasks", "completed-task-deadbeef")
    terminal = WorkRecord(
        source, "tasks", "Completed", "done", origin="fleet", archived=True
    )
    snapshot = Snapshot(
        (), True, (), {"tasks": CapabilityState.COMPLETE},
        datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    issue = {
        "id": "LIN-legacy",
        "title": "Completed [bus:deadbeef]",
        "description": "bus slug: `completed-task-deadbeef`",
    }
    transport = FakeTransport([
        response({"issues": {"nodes": [issue], "pageInfo": {"hasNextPage": False}}}),
    ])
    resolved = []

    def resolve(slug):
        resolved.append(slug)
        return terminal

    adoptions = LinearTrackerAdapter(
        LinearClient(transport), "team"
    ).plan_marker_adoptions(snapshot, BridgeLedger(), load_policy(), resolve)

    assert [adoption.source for adoption in adoptions] == [source]
    assert resolved == ["completed-task-deadbeef"]


def test_legacy_adoption_rejects_source_already_mapped_to_another_issue():
    source = SourceIdentity("coord-engine", "fulcra/tasks", "role-vacant-example-h24h-sla")
    snapshot = Snapshot(
        (WorkRecord(source, "tasks", "Canonical", "active", origin="fleet"),),
        True, (), {"tasks": CapabilityState.COMPLETE},
        datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    ledger = BridgeLedger([LedgerEntry(
        source, "tasks", "linear", "LIN-owned", "2", "policy-hash"
    )])
    issue = {
        "id": "LIN-legacy",
        "title": "Legacy [bus:h24h-sla]",
        "description": "bus slug: `role-vacant-example-h24h-sla`",
    }
    transport = FakeTransport([
        response({"issues": {"nodes": [issue], "pageInfo": {"hasNextPage": False}}}),
    ])

    with pytest.raises(LinearError, match="already mapped to another issue"):
        LinearTrackerAdapter(LinearClient(transport), "team").plan_marker_adoptions(
            snapshot, ledger, load_policy()
        )

    assert len(transport.payloads) == 1


@pytest.mark.parametrize(
    ("title", "description", "message"),
    [
        ("Legacy [bus:h24h-sla]", "body", "exactly one bus slug footer"),
        (
            "Legacy [bus:deadbeef]",
            "bus slug: `role-vacant-example-h24h-sla`",
            "marker does not match footer slug suffix",
        ),
    ],
)
def test_legacy_adoption_rejects_missing_footer_or_marker_mismatch(
    title, description, message
):
    source = SourceIdentity("coord-engine", "fulcra/tasks", "role-vacant-example-h24h-sla")
    snapshot = Snapshot(
        (WorkRecord(source, "tasks", "Canonical", "active", origin="fleet"),),
        True, (), {"tasks": CapabilityState.COMPLETE},
        datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    transport = FakeTransport([
        response({"issues": {"nodes": [{
            "id": "LIN-1", "title": title, "description": description,
        }], "pageInfo": {"hasNextPage": False}}}),
    ])

    with pytest.raises(LinearError, match=message):
        LinearTrackerAdapter(LinearClient(transport), "team").plan_marker_adoptions(
            snapshot, BridgeLedger(), load_policy()
        )

    assert len(transport.payloads) == 1


def test_comment_and_due_date_are_semantic_operations():
    transport = FakeTransport([
        response({"commentCreate": {"success": True, "comment": {"id": "comment-1"}}}),
        response({"issueUpdate": {"success": True}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")

    assert adapter.add_comment("LIN-1", "hello") == "comment-1"
    adapter.set_due_date("LIN-1", "2026-07-18")

    assert [call["operationName"] for call in transport.payloads] == ["AddComment", "SetDueDate"]
