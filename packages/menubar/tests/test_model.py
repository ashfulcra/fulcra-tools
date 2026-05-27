from __future__ import annotations

from fulcra_menubar.model import StatusModel, OverallState


HEALTHY = {
    "ok": True, "plugins": [
        {"id": "lastfm", "name": "Last.fm", "kind": "scheduled",
         "enabled": True, "last_run": "2026-05-23T12:00:00+00:00",
         "last_outcome": "done", "last_error": None,
         "consecutive_failures": 0},
    ], "load_errors": {},
}

FAILING = {
    "ok": True, "plugins": [
        {"id": "lastfm", "name": "Last.fm", "kind": "scheduled",
         "enabled": True, "last_run": "2026-05-23T12:05:00+00:00",
         "last_outcome": "error", "last_error": "401 unauthorized",
         "consecutive_failures": 3},
    ], "load_errors": {},
}


def test_initial_state_is_unknown():
    m = StatusModel()
    assert m.overall is OverallState.UNKNOWN
    assert m.plugins == []


def test_healthy_snapshot_yields_healthy_overall():
    m = StatusModel()
    m.update_from_status(HEALTHY)
    assert m.overall is OverallState.HEALTHY


def test_failing_snapshot_yields_failing_overall():
    m = StatusModel()
    m.update_from_status(FAILING)
    assert m.overall is OverallState.FAILING


def test_observers_called_on_change():
    m = StatusModel()
    calls = []
    m.add_observer(lambda model: calls.append(model.overall))
    m.update_from_status(HEALTHY)
    m.update_from_status(FAILING)
    assert calls == [OverallState.HEALTHY, OverallState.FAILING]


def test_observers_not_called_when_snapshot_unchanged():
    m = StatusModel()
    calls = []
    m.add_observer(lambda model: calls.append(model.overall))
    m.update_from_status(HEALTHY)
    m.update_from_status(HEALTHY)  # identical
    assert calls == [OverallState.HEALTHY]


def test_in_flight_set_drives_running_overall():
    m = StatusModel()
    m.update_from_status(HEALTHY)
    m.mark_in_flight("lastfm")
    assert m.overall is OverallState.RUNNING
    advanced = {**HEALTHY, "plugins": [{**HEALTHY["plugins"][0],
                                         "last_run": "2026-05-23T12:10:00+00:00"}]}
    m.update_from_status(advanced)
    assert m.overall is OverallState.HEALTHY
    assert "lastfm" not in m.in_flight


def test_daemon_stopped_overrides_everything():
    m = StatusModel()
    m.update_from_status(FAILING)
    m.mark_daemon_stopped()
    assert m.overall is OverallState.DAEMON_STOPPED


def test_failure_threshold_transitions():
    m = StatusModel()
    m.update_from_status(HEALTHY)
    transitions = []
    m.add_failure_transition_observer(transitions.append)
    m.update_from_status(FAILING)
    assert transitions == ["lastfm"]
    m.update_from_status(FAILING)
    assert transitions == ["lastfm"]


def test_failure_transition_only_on_first_crossing():
    m = StatusModel()
    transitions = []
    m.add_failure_transition_observer(transitions.append)
    m.update_from_status(FAILING)
    assert transitions == ["lastfm"]


def test_failure_transition_refires_after_recovery():
    """A plugin that recovers (consecutive_failures drops below 3) and
    then fails again should re-fire the failure observer — the user
    wants to know about the new failure even though they were already
    notified about the previous one."""
    m = StatusModel()
    transitions = []
    m.add_failure_transition_observer(transitions.append)

    m.update_from_status(FAILING)           # cross into >=3
    assert transitions == ["lastfm"]

    recovered = {**HEALTHY, "plugins": [{**HEALTHY["plugins"][0],
                                         "consecutive_failures": 0,
                                         "last_outcome": "done"}]}
    m.update_from_status(recovered)
    assert transitions == ["lastfm"]        # no extra fire on recovery

    m.update_from_status(FAILING)           # re-fail
    assert transitions == ["lastfm", "lastfm"]  # re-fired


def test_plugin_snapshot_reads_description_from_dict():
    """PluginSnapshot.from_dict must populate description from the daemon reply."""
    from fulcra_menubar.model import PluginSnapshot
    d = {
        "id": "lastfm", "name": "Last.fm", "kind": "scheduled",
        "enabled": True, "last_run": None, "last_outcome": None,
        "last_error": None, "consecutive_failures": 0,
        "description": "Imports your Last.fm scrobble history.",
    }
    snap = PluginSnapshot.from_dict(d)
    assert snap.description == "Imports your Last.fm scrobble history."


def test_plugin_snapshot_description_defaults_to_empty():
    """Older daemon replies without description must not break PluginSnapshot."""
    from fulcra_menubar.model import PluginSnapshot
    d = {
        "id": "lastfm", "name": "Last.fm", "kind": "scheduled",
        "enabled": True, "last_run": None, "last_outcome": None,
        "last_error": None, "consecutive_failures": 0,
        # no "description" key — simulates a pre-description daemon
    }
    snap = PluginSnapshot.from_dict(d)
    assert snap.description == ""


def test_in_flight_holds_until_last_run_advances():
    """Bug 3 regression: for a plugin that already has a last_run, in_flight
    must NOT clear on the very next poll with the same snapshot. It must only
    clear when last_run actually advances past the value at trigger time."""
    m = StatusModel()
    m.update_from_status(HEALTHY)  # lastfm has last_run = 12:00
    m.mark_in_flight("lastfm")

    # Same snapshot — should stay in_flight.
    m.update_from_status(HEALTHY)
    assert "lastfm" in m.in_flight, (
        "in_flight should not clear when last_run is unchanged since trigger"
    )

    # New snapshot with an advanced timestamp — should now clear.
    advanced = {**HEALTHY, "plugins": [{**HEALTHY["plugins"][0],
                                        "last_run": "2026-05-23T12:10:00+00:00"}]}
    m.update_from_status(advanced)
    assert "lastfm" not in m.in_flight, (
        "in_flight should clear once last_run advances past the baseline"
    )


# ---------------------------------------------------------------------------
# Failure-tier counts (task #59) — distinguishes amber-tier (1-2 fails)
# from red-tier (≥3 fails) so the menubar icon can paint two severities.
# ---------------------------------------------------------------------------

def _snapshot_with_failure_counts(*per_plugin_failures: int) -> dict:
    plugins = []
    for i, n in enumerate(per_plugin_failures):
        plugins.append({
            "id": f"p{i}", "name": f"Plugin {i}", "kind": "scheduled",
            "enabled": True, "last_run": "2026-05-26T10:00:00+00:00",
            "last_outcome": "error" if n > 0 else "done",
            "last_error": "401" if n > 0 else None,
            "consecutive_failures": n,
        })
    return {"ok": True, "plugins": plugins, "load_errors": {}}


def test_failing_warning_count_is_one_to_two_failures():
    m = StatusModel()
    m.update_from_status(_snapshot_with_failure_counts(0, 1, 2, 3, 5))
    assert m.failing_warning_count == 2  # the two with 1 and 2 failures
    assert m.failing_critical_count == 2  # the two with 3 and 5


def test_failing_counts_skip_disabled_plugins():
    m = StatusModel()
    snap = _snapshot_with_failure_counts(5)
    snap["plugins"][0]["enabled"] = False
    m.update_from_status(snap)
    assert m.failing_warning_count == 0
    assert m.failing_critical_count == 0


def test_failing_counts_match_legacy_failing_count_total():
    """The legacy failing_count == warning + critical; preserving back-compat."""
    m = StatusModel()
    m.update_from_status(_snapshot_with_failure_counts(0, 1, 1, 3, 4))
    assert m.failing_count == m.failing_warning_count + m.failing_critical_count
