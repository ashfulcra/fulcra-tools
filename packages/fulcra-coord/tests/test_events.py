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


# ---------------------------------------------------------------------------
# fold_task tests
# ---------------------------------------------------------------------------

def test_fold_applies_events_in_time_order_last_write_wins_on_fields():
    evs = [
        events.make_event(family="tasks", task_id="T", kind="created", actor="a",
                          payload={"title": "Do X", "status": "active", "summary": "start"}),
        events.make_event(family="tasks", task_id="T", kind="updated", actor="a",
                          payload={"summary": "midway"}),
        events.make_event(family="tasks", task_id="T", kind="done", actor="a",
                          payload={"status": "done", "evidence": "shipped"}),
    ]
    state = events.fold_task(evs)
    assert state["id"] == "T"
    assert state["title"] == "Do X"
    assert state["status"] == "done"
    assert state["summary"] == "midway"
    assert state["evidence"] == "shipped"


def test_fold_dedups_by_actor_idempotency_key():
    # Two independent make_event calls with the same actor + idempotency_key
    # represent a re-delivered event.  The second has an explicitly later ``at``
    # so sort order is deterministic and we know which one "wins" the dedup slot.
    e1 = events.make_event(
        family="tasks", task_id="T", kind="updated", actor="a",
        payload={"summary": "v1"}, idempotency_key="op-1",
        at="2026-06-08T10:00:00.000000Z",
    )
    e2 = events.make_event(
        family="tasks", task_id="T", kind="updated", actor="a",
        payload={"summary": "v1-redelivered"}, idempotency_key="op-1",
        at="2026-06-08T10:00:01.000000Z",
    )
    state = events.fold_task([e1, e2])
    # Only the first occurrence (in sort order) is applied; the duplicate is skipped.
    assert state["summary"] == "v1"
    assert state["_applied_event_count"] == 1


def test_fold_terminal_state_not_clobbered_by_late_nonterminal():
    evs = [
        events.make_event(family="tasks", task_id="T", kind="done", actor="a",
                          payload={"status": "done"}, at="2026-06-08T10:00:00.000000Z"),
        events.make_event(family="tasks", task_id="T", kind="updated", actor="b",
                          payload={"status": "active", "summary": "late"}, at="2026-06-08T09:00:00.000000Z"),
    ]
    state = events.fold_task(evs)
    # Events are sorted by `at`: 09:00 active is applied first, then 10:00 done.
    # The final status must be "done" — an earlier-timestamped non-terminal event
    # cannot revive a later terminal one.
    assert state["status"] == "done"


def test_fold_empty_list_returns_sentinel():
    state = events.fold_task([])
    assert state == {"id": None, "_applied_event_count": 0}


def test_fold_terminal_persists_when_later_event_has_no_status():
    # A ``done`` event followed by an update whose payload has NO ``status`` key
    # must leave ``status`` as ``"done"`` — the merge simply does not touch the key.
    # This locks the emergent terminal-stickiness property.
    evs = [
        events.make_event(
            family="tasks", task_id="T", kind="done", actor="a",
            payload={"status": "done"}, at="2026-06-08T10:00:00.000000Z",
        ),
        events.make_event(
            family="tasks", task_id="T", kind="updated", actor="a",
            payload={"summary": "note"}, at="2026-06-08T11:00:00.000000Z",
        ),
    ]
    state = events.fold_task(evs)
    assert state["status"] == "done"
    assert state["summary"] == "note"


def test_fold_later_explicit_nonterminal_reopens():
    # A ``done`` followed by a later event that explicitly sets ``status`` to a
    # non-terminal value must reopen the task — the later write wins.
    evs = [
        events.make_event(
            family="tasks", task_id="T", kind="done", actor="a",
            payload={"status": "done"}, at="2026-06-08T10:00:00.000000Z",
        ),
        events.make_event(
            family="tasks", task_id="T", kind="updated", actor="a",
            payload={"status": "active"}, at="2026-06-08T11:00:00.000000Z",
        ),
    ]
    state = events.fold_task(evs)
    assert state["status"] == "active"
