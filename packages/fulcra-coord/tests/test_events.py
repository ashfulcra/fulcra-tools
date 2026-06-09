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


# ---------------------------------------------------------------------------
# Phase 2a: snapshot semantics + fold_is_complete
# ---------------------------------------------------------------------------

def test_fold_latest_snapshot_replaces_accumulated_state():
    s1 = events.make_event(family="tasks", task_id="T", kind="start", actor="a",
                           payload={"id": "T", "schema": "fulcra.coordination.task.v1",
                                    "status": "active", "title": "v1", "x": 1},
                           at="2026-06-09T10:00:00.000000Z")
    s2 = events.make_event(family="tasks", task_id="T", kind="update", actor="a",
                           payload={"id": "T", "schema": "fulcra.coordination.task.v1",
                                    "status": "active", "title": "v2"},
                           at="2026-06-09T11:00:00.000000Z")
    state = events.fold_task([s1, s2])
    assert state["title"] == "v2"
    assert "x" not in state          # latest snapshot REPLACED — stale field dropped
    assert state["id"] == "T"


def test_fold_is_complete_true_when_snapshot_present():
    s = events.make_event(family="tasks", task_id="T", kind="start", actor="a",
                          payload={"id": "T", "schema": "fulcra.coordination.task.v1", "status": "active"})
    assert events.fold_is_complete(events.fold_task([s])) is True


def test_fold_is_complete_false_for_legacy_delta_only_events():
    d = events.make_event(family="tasks", task_id="T", kind="update", actor="a",
                          payload={"summary": "s"})
    assert events.fold_is_complete(events.fold_task([d])) is False


def test_fold_snapshot_then_legacy_delta_merges_on_top():
    # A delta arriving AFTER a snapshot still field-merges onto it (back-compat;
    # in practice snapshots come last, but the reducer must be order-correct).
    s = events.make_event(family="tasks", task_id="T", kind="start", actor="a",
                          payload={"id": "T", "schema": "fulcra.coordination.task.v1",
                                   "status": "active", "current_summary": "s0"},
                          at="2026-06-09T10:00:00.000000Z")
    d = events.make_event(family="tasks", task_id="T", kind="update", actor="a",
                          payload={"current_summary": "s1"},
                          at="2026-06-09T11:00:00.000000Z")
    state = events.fold_task([s, d])
    assert state["current_summary"] == "s1"
    assert state["status"] == "active"   # snapshot fields survive the partial delta


def test_fold_orders_by_numeric_instant_not_raw_string():
    # D1: ``at`` is an ISO-8601 string, but a raw lexical string sort INVERTS
    # when two timestamps differ only in trailing precision: ``...00Z`` vs
    # ``...00.000001Z``. Lexically, ``.`` (0x2E) < ``Z`` (0x5A), so the bare-Z
    # form sorts AFTER the higher-precision form — even though the
    # ``.000001Z`` instant is LATER in real time. The fold must order by the
    # numeric microsecond instant (the same normalization ``event_id`` uses),
    # so the genuinely-later ``.000001Z`` event wins last-write.
    earlier = events.make_event(
        family="tasks", task_id="T", kind="completed", actor="a",
        payload={"status": "done"}, at="2026-06-08T00:00:00Z",
    )
    later = events.make_event(
        family="tasks", task_id="T", kind="updated", actor="a",
        payload={"status": "active"}, at="2026-06-08T00:00:00.000001Z",
    )
    # Feed them in BOTH orders so the result depends only on sort, not input order.
    assert events.fold_task([earlier, later])["status"] == "active"
    assert events.fold_task([later, earlier])["status"] == "active"


def test_at_sort_key_collapses_equivalent_iso_spellings():
    # The sort key is a canonical UTC microsecond instant, not just punctuation
    # stripping. Same-instant spellings must tie so event_id, not representation,
    # is the deterministic tie-breaker.
    assert (
        events._at_sort_key("2026-06-08T00:00:00Z")
        == events._at_sort_key("2026-06-08T00:00:00.000000Z")
    )
    assert (
        events._at_sort_key("2026-06-08T00:00:00+00:00")
        == events._at_sort_key("2026-06-08T00:00:00.000000Z")
    )


def test_at_sort_key_normalizes_offsets_to_utc():
    assert (
        events._at_sort_key("2026-06-08T01:30:00+01:30")
        == events._at_sort_key("2026-06-08T00:00:00.000000Z")
    )


def test_at_sort_key_malformed_input_falls_back_to_legacy_strip():
    assert events._at_sort_key("not-a-date:Z") == "notadate"


def test_at_sort_key_empty_string():
    # Empty ``at`` returns empty key, sorts first. This characterizes the
    # edge case where a fold receives an event with no timestamp (malformed),
    # ensuring it sorts predictably to the front.
    assert events._at_sort_key("") == ""
