import pytest

from coord_tracker_bridge import (
    BridgeLedger,
    Change,
    ChangeKind,
    GraphQLResponse,
    LinearClient,
    LinearTrackerAdapter,
    SourceIdentity,
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
        "operator-visible body", source, {"policy_version": "2", "owner": "ash"}
    )

    assert parse_source_metadata(description) == source
    assert parse_bridge_metadata(description)["fields"] == {"policy_version": "2", "owner": "ash"}
    assert strip_source_metadata(description) == "operator-visible body"
    assert "alpha-12345678" not in description


def test_created_before_ledger_write_is_rediscovered_from_provider_metadata():
    source = SourceIdentity("coord-engine", "fulcra", "task-1")
    issue = {
        "id": "LIN-1", "title": "Task",
        "description": append_source_metadata("body", source),
        "state": {"type": "started"}, "labels": {"nodes": []}, "project": None,
    }
    transport = FakeTransport([
        response({"issues": {"nodes": [issue], "pageInfo": {"hasNextPage": False, "endCursor": None}}}),
        response({"issue": {"labels": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")

    records = adapter.list_managed_records(BridgeLedger())

    assert [(record.provider_id, record.source) for record in records] == [("LIN-1", source)]


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


def test_comment_and_due_date_are_semantic_operations():
    transport = FakeTransport([
        response({"commentCreate": {"comment": {"id": "comment-1"}}}),
        response({"issueUpdate": {"success": True}}),
    ])
    adapter = LinearTrackerAdapter(LinearClient(transport), "team")

    assert adapter.add_comment("LIN-1", "hello") == "comment-1"
    adapter.set_due_date("LIN-1", "2026-07-18")

    assert [call["operationName"] for call in transport.payloads] == ["AddComment", "SetDueDate"]
