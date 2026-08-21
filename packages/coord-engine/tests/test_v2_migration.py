"""Unit 6 migration and release gates, written before their implementation."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
import hashlib
import time

import pytest

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


EVIDENCE_AT = "2026-08-21T00:00:00Z"
AUTHORITY = {
    "data_type": "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041",
    "api_version": "v1alpha1",
}


def _binding(row):
    payload = {key: value for key, value in row.items()
               if key != "credential_provenance"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "evidence-sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _host(name: str, *, credentialed: bool = True, label: str | None = None):
    probe_id = hashlib.md5(name.encode()).hexdigest()
    row = {
        "schema": "coord.feed-visibility-lag.v1",
        "state": "DATA",
        "team": "fulcra",
        "host_identity": f"coord-reconcile:{name}",
        "display_label": label or name,
        "principal_identity": f"agent-{name}",
        "transport_authority": dict(AUTHORITY),
        "credential_provenance": None,
        "credentialed": credentialed,
        "observed_seconds": 2.5,
        "measured_at": EVIDENCE_AT,
        "probe_id": probe_id,
        "probe_path": f"team/fulcra/_coord/projections/lag-probes/{probe_id}.json",
        "event_at": "2026-08-21T00:00:01Z",
        "observed_at": "2026-08-21T00:00:03.500000Z",
        "update_id": "3185bd09-9500-4407-bd87-013832fe55f3",
        "reason": None,
    }
    row["credential_provenance"] = _binding(row)
    return row


def _exclusions():
    return {
        name: ({"reason": reason,
                "provenance": "Ash operator ruling",
                "host_identity": name}
               if name == "MacBookPro.localdomain"
               else {"reason": reason,
                     "provenance": "measured fleet census",
                     "host_identity": name,
                     "last_reconciled_at": "2026-08-20T00:00:00Z",
                     "age_seconds": 86400})
        for name, reason in migration.REQUIRED_EXCLUSIONS.items()
    }


def _fleet(*, host_b_version="2.0.0"):
    return {
        "host-a": {"host_identity": "host-a", "engine_version": "2.0.0",
                   "age_seconds": 30,
                   "last_reconciled_at": "2026-08-20T23:59:30Z"},
        "host-b": {"host_identity": "host-b", "engine_version": host_b_version,
                   "age_seconds": 30,
                   "last_reconciled_at": "2026-08-20T23:59:30Z"},
        "MacBookPro.localdomain": {
            "host_identity": "MacBookPro.localdomain",
            "engine_version": "1.6.9", "age_seconds": 30,
            "last_reconciled_at": "2026-08-20T23:59:30Z",
        },
    }


def test_unmeasured_or_one_host_epsilon_fails_closed():
    for hosts in ([], [_host("host-a")]):
        decision = migration.evaluate_activation(
            hosts=hosts, configured_epsilon_seconds=3.0,
            fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
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
        evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(),
        cas_supported=True,
    )
    no_cas = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(),
        cas_supported=False,
    )
    assert (mixed.state, mixed.ready) == ("REFUSED", False)
    assert (no_cas.state, no_cas.ready) == ("REFUSED", False)


def test_activation_requires_named_exclusions_and_epsilon_above_measurement():
    measured = [_host("host-a"), _host("host-b")]
    missing_exclusions = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions={}, cas_supported=True,
    )
    too_small = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=2.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )
    assert missing_exclusions.state == "UNKNOWN"
    assert too_small.state == "REFUSED"


def test_two_credentialed_hosts_can_prove_the_phase1_gate_without_claiming_release():
    decision = migration.evaluate_activation(
        hosts=[_host("host-a"), _host("host-b")],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )
    assert decision.state == "READY"
    assert decision.ready is True
    assert decision.release_complete is False


def test_display_labels_cannot_turn_one_attested_machine_into_two_hosts():
    first = _host("same-machine", label="host-a")
    second = _host("same-machine", label="host-b")
    decision = migration.evaluate_activation(
        hosts=[first, second], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert "two distinct credentialed host measurements required" in decision.reasons


def test_measurement_host_identity_must_have_canonical_machine_shape():
    forged = _host("host-a")
    forged["host_identity"] = "caller-controlled-label"
    decision = migration.evaluate_activation(
        hosts=[forged, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_activation_consumes_exact_lag_measurement_schema_not_synthetic_maximum():
    synthetic = _host("host-a")
    synthetic["observed_max_seconds"] = synthetic.pop("observed_seconds")
    synthetic["credential_provenance"] = _binding(synthetic)
    decision = migration.evaluate_activation(
        hosts=[synthetic, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


@pytest.mark.parametrize("missing", [
    "event_at", "observed_at", "update_id", "probe_path", "transport_authority",
])
def test_handcrafted_measurement_missing_lifecycle_or_probe_evidence_is_unknown(missing):
    row = _host("host-a")
    row.pop(missing)
    row["credential_provenance"] = _binding(row)
    decision = migration.evaluate_activation(
        hosts=[row, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_arbitrary_sha256_provenance_cannot_pass_measurement_gate():
    row = _host("host-a")
    row["credential_provenance"] = "evidence-sha256:" + "a" * 64
    decision = migration.evaluate_activation(
        hosts=[row, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_non_json_measurement_field_is_unknown_not_an_exception():
    row = _host("host-a")
    row["display_label"] = object()
    row["credential_provenance"] = "evidence-sha256:" + "a" * 64

    decision = migration.evaluate_activation(
        hosts=[row, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


@pytest.mark.parametrize(("field", "value"), [
    ("last_reconciled_at", "not-a-time"),
    ("last_reconciled_at", "2026-08-20T23:59:30"),
    ("age_seconds", float("nan")),
    ("age_seconds", float("inf")),
    ("age_seconds", -1),
    ("age_seconds", 10),
])
def test_fleet_evidence_rejects_malformed_nonfinite_negative_or_inconsistent_age(
        field, value):
    fleet = _fleet()
    fleet["host-a"][field] = value
    decision = migration.evaluate_activation(
        hosts=[_host("host-a"), _host("host-b")],
        configured_epsilon_seconds=3.0,
        fleet=fleet, fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_exclusion_requires_ruling_provenance_and_consistent_age():
    exclusions = _exclusions()
    exclusions["MacBookPro.localdomain"]["provenance"] = ""
    exclusions["coord-boss"]["age_seconds"] = float("nan")
    decision = migration.evaluate_activation(
        hosts=[_host("host-a"), _host("host-b")],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=exclusions, cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


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

    def read_classified(self, _path, *, deadline=None):
        return json.dumps(AUTHORITY), "ok"

    def write(self, path, content, *, deadline=None):
        self.writes.append((path, content))
        return True

    def data_updates(self, since, *, deadline=None):
        if self.clock.value < self.visible_after:
            return {"after": since, "through": since, "file_changes": []}
        path = self.writes[-1][0]
        return {"after": since, "through": "2026-08-21T00:00:10Z",
                "file_changes": [{"id": "3185bd09-9500-4407-bd87-013832fe55f3",
                                  "path": path,
                                  "state": "uploaded",
                                  "uploaded_at": "2026-08-21T00:00:01Z"}]}


class _Now:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        second = 0 if self.calls == 1 else 2
        return datetime(2026, 8, 21, 0, 0, second, tzinfo=timezone.utc)


def test_feed_visibility_measurement_is_bounded_and_reports_observed_lag():
    clock = _Clock()
    transport = _LagTransport(0.3, clock)
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "host-a",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine",
        timeout_seconds=1.0, poll_seconds=0.1,
        monotonic=clock.monotonic, sleep=clock.sleep,
        now=_Now(),
    )
    assert result.state == "DATA"
    assert result.observed_seconds == 1.0
    assert result.credentialed is True
    assert result.host_identity == "coord-reconcile:same-machine"
    assert result.event_at == "2026-08-21T00:00:01Z"
    assert "observed_max_seconds" not in result.as_dict()
    assert result.as_dict()["credential_provenance"] == _binding(result.as_dict())

    decision = migration.evaluate_activation(
        hosts=[result.as_dict(), _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )
    assert decision.state == "READY"


def test_feed_visibility_measurement_refuses_non_finite_bounds_before_write():
    clock = _Clock()
    transport = _LagTransport(0.3, clock)
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "host-a",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine",
        timeout_seconds=1.0, poll_seconds=float("inf"),
        monotonic=clock.monotonic, sleep=clock.sleep,
        now=lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert result.state == "UNKNOWN"
    assert result.rc == 3
    assert transport.writes == []


@pytest.mark.parametrize("envelope", [
    {"files": [{"path": "PROBE"}]},
    {"through": "2026-08-21T00:00:10Z", "file_changes": []},
    {"after": "not-a-time", "through": "2026-08-21T00:00:10Z",
     "file_changes": []},
    {"after": "2026-08-21T00:00:00Z", "through": "2016-08-21T00:00:10Z",
     "file_changes": []},
    {"after": "2026-08-21T00:00:00Z", "through": "2026-08-21T00:00:10Z",
     "file_changes": [{"id": "u", "path": "PROBE",
                       "uploaded_at": "2026-08-21T00:00:01Z"}]},
    {"after": "2026-08-21T00:00:00Z", "through": "2026-08-21T00:00:10Z",
     "file_changes": [{"path": "PROBE", "state": "uploaded",
                       "uploaded_at": "2026-08-21T00:00:01Z"}]},
    {"after": "2026-08-21T00:00:00Z", "through": "2026-08-21T00:00:10Z",
     "file_changes": [{"id": "u", "path": "PROBE", "state": "uploaded"}]},
])
def test_lag_harness_rejects_unattested_or_malformed_feed_body_and_rc(envelope):
    clock = _Clock()

    class Malformed(_LagTransport):
        def data_updates(self, since, *, deadline=None):
            path = self.writes[-1][0]
            return json.loads(json.dumps(envelope).replace("PROBE", path))

    transport = Malformed(0, clock)
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "display-only",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine",
        timeout_seconds=1.0, poll_seconds=0.1,
        monotonic=clock.monotonic, sleep=clock.sleep, now=_Now(),
    )

    body = result.as_dict()
    assert body["state"] == "UNKNOWN"
    assert body["reason"]
    assert result.rc == 3


def test_cli_host_id_is_display_only_and_cannot_mint_attested_identity(
        monkeypatch, capsys):
    captured = []

    def capture_measure(_transport, _team, display_label, *, persisted, hostname,
                        **_kwargs):
        saved = persisted()
        captured.append((display_label, hostname(), saved.identity))
        return migration.LagMeasurement.unknown(
            display_label=display_label, reason="test stop",
            measured_at=EVIDENCE_AT, probe_id="probe",
        )

    transport = _LagTransport(0, _Clock())
    monkeypatch.setattr(cli.config, "persisted_identity", lambda: classifier.PersistedIdentity(
        classifier.PersistedIdentityState.PRESENT, "agent-a"))
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "same-machine")
    monkeypatch.setattr(migration, "measure_feed_visibility_lag", capture_measure)
    for label in ("host-a", "host-b"):
        rc = cli.cmd_measure_feed_lag(
            Namespace(team="fulcra", host_id=label, timeout=1.0, poll=0.1),
            transport,
        )
        assert rc == 3
        capsys.readouterr()

    assert captured == [
        ("host-a", "same-machine", "agent-a"),
        ("host-b", "same-machine", "agent-a"),
    ]


@pytest.mark.parametrize("auth_mode", ["cli", "http"])
def test_token_refresh_or_auth_mode_is_not_credential_provenance(auth_mode):
    clock = _Clock()

    class RotatingTokenTransport(_LagTransport):
        mode = auth_mode

        def _access_token(self):
            raise AssertionError("bearer token must not be read or hashed")

    result = migration.measure_feed_visibility_lag(
        RotatingTokenTransport(0.3, clock), "fulcra", "display",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine", timeout_seconds=1.0, poll_seconds=0.1,
        monotonic=clock.monotonic, sleep=clock.sleep, now=_Now(),
    )
    assert result.state == "DATA"
    assert result.rc == 0


@pytest.mark.parametrize("slow_phase", ["auth", "write"])
def test_entire_harness_timeout_covers_slow_preflight_and_upload(slow_phase):
    class Slow(_LagTransport):
        def read_classified(self, path, *, deadline=None):
            if slow_phase == "auth":
                time.sleep(0.08)
            return super().read_classified(path, deadline=deadline)

        def write(self, path, content, *, deadline=None):
            if slow_phase == "write":
                time.sleep(0.08)
            return super().write(path, content, deadline=deadline)

    transport = Slow(0, _Clock())
    started = time.monotonic()
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "display",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine", timeout_seconds=0.01, poll_seconds=0.001,
    )
    elapsed = time.monotonic() - started

    assert result.as_dict()["state"] == "UNKNOWN"
    assert result.rc == 3
    assert elapsed < 0.20
    if slow_phase == "auth":
        assert transport.writes == []
