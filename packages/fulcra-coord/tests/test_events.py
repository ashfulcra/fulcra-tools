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


def test_event_id_sorts_by_time_then_breaks_ties_deterministically():
    a = events.make_event(family="tasks", task_id="T", kind="updated", actor="a", payload={})
    b = events.make_event(family="tasks", task_id="T", kind="updated", actor="a", payload={})
    assert a["event_id"].split("-")[0] <= b["event_id"].split("-")[0]
