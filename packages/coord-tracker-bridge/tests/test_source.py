import json
from datetime import datetime, timezone

from coord_tracker_bridge import CapabilityState, EngineSourceAdapter


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


def runner_for(payloads):
    def run(argv, _timeout):
        capability = argv[1]
        value = payloads[capability]
        if isinstance(value, Exception):
            return 1, "", str(value)
        return 0, json.dumps(value), ""
    return run


def test_engine_source_normalizes_each_capability_and_sanitizes_text():
    adapter = EngineSourceAdapter(
        "fulcra",
        runner=runner_for({
            "board": {"active": [{"id": "task-1", "title": "Task\u0000 title", "tags": ["kind:task"]}]},
            "asks": [{"id": "ask-1", "title": "Question"}],
            "threads": [],
            "health": [],
        }),
        clock=lambda: NOW,
    )

    snapshot = adapter.snapshot()

    assert snapshot.complete
    assert [item.source.item_id for item in snapshot.items] == ["task-1", "ask-1"]
    assert snapshot.items[0].title == "Task  title"
    assert snapshot.capabilities["expectations"] is CapabilityState.UNSUPPORTED


def test_engine_source_degrades_only_failed_capability_and_never_returns_clean_complete():
    adapter = EngineSourceAdapter(
        "fulcra",
        runner=runner_for({"board": RuntimeError("secret source failure"), "asks": [], "threads": [], "health": []}),
        clock=lambda: NOW,
    )

    snapshot = adapter.snapshot()

    assert not snapshot.complete
    assert snapshot.capabilities["tasks"] is CapabilityState.DEGRADED
    assert snapshot.capabilities["asks"] is CapabilityState.COMPLETE
    assert snapshot.diagnostics[0].scope == "tasks"


def test_engine_source_honors_embedded_degraded_rows():
    adapter = EngineSourceAdapter(
        "fulcra",
        runner=runner_for({
            "board": {"active": [], "read-degraded": {"reason": "unknown"}},
            "asks": [], "threads": [], "health": [],
        }),
        clock=lambda: NOW,
    )

    snapshot = adapter.snapshot()
    assert snapshot.capabilities["tasks"] is CapabilityState.DEGRADED
    assert snapshot.diagnostics[0].message == "$.read-degraded: unknown"


def test_engine_source_parses_jsonl_folds_and_uses_slow_health_bound():
    seen = {}

    def run(argv, timeout):
        seen[argv[1]] = timeout
        if argv[1] == "threads":
            return 0, '{"id":"thread-1","title":"One"}\n{"id":"thread-2","title":"Two"}\n', ""
        return 0, json.dumps({"active": []} if argv[1] == "board" else []), ""

    snapshot = EngineSourceAdapter(
        "fulcra", runner=run, timeout=12.0, health_timeout=345.0, clock=lambda: NOW
    ).snapshot()

    assert [item.source.item_id for item in snapshot.items] == ["thread-1", "thread-2"]
    assert seen == {"board": 12.0, "asks": 12.0, "threads": 12.0, "health": 345.0}
