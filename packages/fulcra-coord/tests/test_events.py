from fulcra_coord import events


def test_make_event_has_required_envelope_fields():
    e = events.make_event(
        family="tasks", task_id="TASK-1", kind="updated",
        actor="claude-code:Mac:fulcra-tools",
        payload={"summary": "did a thing"},
        idempotency_key="op-abc",
    )
    assert e["schema_version"] == events.EVENT_SCHEMA_VERSION
    assert e["family"] == "tasks"
    assert e["task_id"] == "TASK-1"
    assert e["kind"] == "updated"
    assert e["actor"] == "claude-code:Mac:fulcra-tools"
    assert e["idempotency_key"] == "op-abc"
    assert e["payload"] == {"summary": "did a thing"}
    assert e["event_id"]
    assert e["at"].endswith("Z")


def test_event_id_is_unique_per_call():
    ids = {events.make_event(family="tasks", task_id="T", kind="updated",
                             actor="a", payload={})["event_id"] for _ in range(200)}
    assert len(ids) == 200


def test_event_id_prefix_is_chronological():
    a = events.make_event(family="tasks", task_id="T", kind="updated", actor="a", payload={})
    b = events.make_event(family="tasks", task_id="T", kind="updated", actor="a", payload={})
    assert a["event_id"].split("-")[0] <= b["event_id"].split("-")[0]


def test_event_id_unique_within_same_microsecond():
    # Two events stamped at the IDENTICAL instant must still get distinct ids
    # (same sortable prefix, different random suffix) — guards against a future
    # refactor dropping the random suffix.
    at = "2026-06-08T15:30:45.000000Z"
    id1 = events.event_id(at=at)
    id2 = events.event_id(at=at)
    assert id1 != id2
    assert id1.split("-")[0] == id2.split("-")[0]  # same prefix
    assert id1.split("-")[1] != id2.split("-")[1]  # different suffix
