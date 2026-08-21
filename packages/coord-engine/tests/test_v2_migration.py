"""Unit 6 migration and release gates, written before their implementation."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from coord_engine import classifier, migration
from coord_engine import cli


def test_v1_bootstrap_is_input_only_and_never_claims_v2_authority():
    result = migration.read_v1_bootstrap(json.dumps({
        "schema": "coord.teams.summaries.v1",
        "rows": [{"name": "one", "status": "active"}],
        "reviews": {"schema": "coord.reviews.projection.v3", "complete": True,
                    "rows": [], "orphans": [], "orphans_unknown": [],
                    "tombstones": []},
        "forge": {"schema": "coord.forge.projection.v1", "complete": True,
                  "responsible": {}, "feedback": {}},
    }))

    assert result.state == "DATA"
    assert result.authoritative is False
    assert result.sections["tasks"] == {"rows": [{"name": "one", "status": "active"}]}
    assert result.sections["reviews"]["complete"] is True
    assert result.sections["forge"]["complete"] is True


def test_malformed_v1_bootstrap_is_unknown_not_empty():
    result = migration.read_v1_bootstrap('{"schema":"coord.teams.summaries.v1"}')
    assert result.state == "UNKNOWN"
    assert result.sections == {}


def test_unstamped_early_v1_bootstrap_remains_readable_for_downgrade_rebuilds():
    result = migration.read_v1_bootstrap(json.dumps({
        "rows": [{"name": "one", "status": "active"}],
    }))

    assert result.state == "DATA"
    assert result.authoritative is False
    assert result.sections["tasks"]["rows"][0]["name"] == "one"


def test_explicit_non_v1_bootstrap_schema_fails_closed():
    result = migration.read_v1_bootstrap(json.dumps({
        "schema": "coord.teams.summaries.v2",
        "rows": [],
    }))

    assert result.state == "UNKNOWN"


def _host(name: str, version: str = "2.0.0", *, credentialed: bool = True):
    return {
        "host": name,
        "engine_version": version,
        "credentialed": credentialed,
        "observed_max_seconds": 2.5,
        "measured_at": "2026-08-21T00:00:00Z",
    }


def _exclusions():
    return {
        name: ({"reason": reason}
               if name == "MacBookPro.localdomain"
               else {"reason": reason,
                     "last_reconciled_at": "2026-08-20T00:00:00Z",
                     "age_seconds": 86400})
        for name, reason in migration.REQUIRED_EXCLUSIONS.items()
    }


def _fleet(*, host_b_version="2.0.0"):
    return {
        "host-a": {"engine_version": "2.0.0", "age_seconds": 30,
                   "last_reconciled_at": "2026-08-21T00:00:00Z"},
        "host-b": {"engine_version": host_b_version, "age_seconds": 30,
                   "last_reconciled_at": "2026-08-21T00:00:00Z"},
        "MacBookPro.localdomain": {
            "engine_version": "1.6.9", "age_seconds": 30,
            "last_reconciled_at": "2026-08-21T00:00:00Z",
        },
    }


def test_unmeasured_or_one_host_epsilon_fails_closed():
    for hosts in ([], [_host("host-a")]):
        decision = migration.evaluate_activation(
            hosts=hosts, configured_epsilon_seconds=3.0,
            fleet=_fleet(), fleet_sla_seconds=300,
            exclusions=_exclusions(),
            cas_supported=True,
        )
        assert decision.state == "UNKNOWN"
        assert decision.ready is False


def test_mixed_fleet_and_schema2_without_cas_are_refused():
    measured = [_host("host-a"), _host("host-b")]
    mixed = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=3.0,
        fleet=_fleet(host_b_version="1.11.0"), fleet_sla_seconds=300,
        exclusions=_exclusions(),
        cas_supported=True,
    )
    no_cas = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300,
        exclusions=_exclusions(),
        cas_supported=False,
    )
    assert (mixed.state, mixed.ready) == ("REFUSED", False)
    assert (no_cas.state, no_cas.ready) == ("REFUSED", False)


def test_activation_requires_named_exclusions_and_epsilon_above_measurement():
    measured = [_host("host-a"), _host("host-b")]
    missing_exclusions = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300,
        exclusions={}, cas_supported=True,
    )
    too_small = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=2.0,
        fleet=_fleet(), fleet_sla_seconds=300,
        exclusions=_exclusions(), cas_supported=True,
    )
    assert missing_exclusions.state == "UNKNOWN"
    assert too_small.state == "REFUSED"


def test_two_credentialed_hosts_can_prove_the_phase1_gate_without_claiming_release():
    decision = migration.evaluate_activation(
        hosts=[_host("host-a"), _host("host-b")],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300,
        exclusions=_exclusions(), cas_supported=True,
    )
    assert decision.state == "READY"
    assert decision.ready is True
    assert decision.release_complete is False


def test_identity_uncertainty_is_typed_instead_of_raising():
    outcome = classifier.resolve_identity_outcome(
        environ={},
        persisted=classifier.PersistedIdentity(
            classifier.PersistedIdentityState.UNKNOWN),
        hostname=lambda: "host-a",
    )
    assert outcome.state == "UNKNOWN"
    assert outcome.identity is None


def test_cli_identity_uncertainty_is_unknown_rc3_not_runtime_error(
        tmp_path, monkeypatch, capsys):
    path = tmp_path / "state"
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(path))
    monkeypatch.chdir(tmp_path)
    # Use the real persisted-identity path and make it unreadable-as-a-file.
    from coord_engine import config
    config.identity_path().mkdir(parents=True)

    class T:
        def write(self, *_args):
            raise AssertionError("identity doubt must stop before a store write")

    rc = cli.main(["presence", "beat", "fulcra", "-s", "working"], T())
    captured = capsys.readouterr()
    assert rc == 3
    assert "identity UNKNOWN" in captured.err
    assert "RuntimeError" not in captured.err


class _Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class _LagTransport:
    def __init__(self, visible_after: float, clock: _Clock):
        self.visible_after = visible_after
        self.clock = clock
        self.writes = []

    def write(self, path, content):
        self.writes.append((path, content))
        return True

    def updates(self, since, *, team=None):
        if self.clock.value < self.visible_after:
            return {"after": since, "through": since, "files": []}
        path = self.writes[-1][0]
        return {"after": since, "through": "2026-08-21T00:00:10Z",
                "files": [{"id": "probe-1", "path": path, "state": "uploaded",
                           "uploaded_at": "2026-08-21T00:00:01Z"}]}


def test_feed_visibility_measurement_is_bounded_and_reports_observed_lag():
    clock = _Clock()
    result = migration.measure_feed_visibility_lag(
        _LagTransport(0.3, clock), "fulcra", "host-a",
        timeout_seconds=1.0, poll_seconds=0.1,
        monotonic=clock.monotonic, sleep=clock.sleep,
        now=lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    assert result.state == "DATA"
    assert 0.3 <= result.observed_seconds < 0.4
    assert result.credentialed is True


def test_feed_visibility_measurement_refuses_non_finite_bounds_before_write():
    clock = _Clock()
    transport = _LagTransport(0.3, clock)
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "host-a",
        timeout_seconds=1.0, poll_seconds=float("inf"),
        monotonic=clock.monotonic, sleep=clock.sleep,
        now=lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert result.state == "UNKNOWN"
    assert result.rc == 3
    assert transport.writes == []
